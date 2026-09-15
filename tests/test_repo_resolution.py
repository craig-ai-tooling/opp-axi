"""opp-axi must follow the worktree it is run from (cos/W3, 9/15/26).

`REPO` used to be `os.environ.get("OPP_REPO") or ~/code/customer-opportunities`,
full stop. A customer-chief session running in its OWN git worktree (a separate
checkout of customer-opportunities elsewhere on disk, on its own branch) would
therefore have `opp-axi log`/`activity`/`field` write into the SHARED primary
checkout instead of its own worktree -- the entry would miss its PR and leave
the primary dirty. crswd sessions cannot be given env vars, and shell state
does not persist between tool calls, so this cannot be fixed by exporting
OPP_REPO -- it has to be detected from cwd.

These tests exercise `cli._cwd_customer_opportunities_root()` and
`cli._resolve_repo()` directly, passing a synthetic directory as `start`
rather than actually `chdir`-ing the test process (real git commands are run
against real, disposable synthetic repos under tempfile.TemporaryDirectory()
-- never a mock of `git` itself, since the whole point is that a REAL git
worktree is what has to be detected).
"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from opp_axi import cli  # noqa: E402

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Repo Resolution Test", "GIT_AUTHOR_EMAIL": "repo-res-test@example.com",
    "GIT_COMMITTER_NAME": "Repo Resolution Test", "GIT_COMMITTER_EMAIL": "repo-res-test@example.com",
}


def _git(cwd, *args):
    env = dict(os.environ)
    env.update(GIT_ENV)
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                          env=env, check=True)


def make_customer_opportunities_checkout(root, with_opp_dirs=True, with_origin=False):
    """A real git repo at ROOT shaped like customer-opportunities: tracked
    `automation/checks.py` and (optionally) a `<slug>/.salesforce.json`, so
    the two independent detection paths can each be tested alone."""
    os.makedirs(os.path.join(root, "automation"), exist_ok=True)
    with open(os.path.join(root, "automation", "checks.py"), "w") as fh:
        fh.write("# stand-in for the real checker\n")
    if with_opp_dirs:
        os.makedirs(os.path.join(root, "acme"), exist_ok=True)
        with open(os.path.join(root, "acme", ".salesforce.json"), "w") as fh:
            fh.write("{}")
    _git(root, "init", "-q", "-b", "main")
    if with_origin:
        _git(root, "remote", "add", "origin",
            "git@github.com:craig-ai-tooling/customer-opportunities.git")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def make_unrelated_repo(root):
    os.makedirs(root, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    with open(os.path.join(root, "README.md"), "w") as fh:
        fh.write("some other project\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


class CwdDetection(unittest.TestCase):
    """cli._cwd_customer_opportunities_root(start) -- the pure detector,
    independent of OPP_REPO."""

    def test_a_checkout_with_automation_checks_and_salesforce_json_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_customer_opportunities_checkout(tmp)
            root, why = cli._cwd_customer_opportunities_root(start=repo)
            self.assertEqual(os.path.realpath(root), os.path.realpath(repo))
            self.assertIn("automation/checks.py", why)

    def test_a_subdirectory_of_the_checkout_still_resolves_to_the_toplevel(self):
        """A session's cwd is typically <repo>/<slug>, not the repo root."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_customer_opportunities_checkout(tmp)
            sub = os.path.join(repo, "acme")
            root, why = cli._cwd_customer_opportunities_root(start=sub)
            self.assertEqual(os.path.realpath(root), os.path.realpath(repo))

    def test_a_worktree_with_no_opp_dirs_yet_is_detected_via_origin_remote(self):
        """A freshly cut worktree (cos/W3's per-session lease) has automation/
        checks.py (it is a checkout of the same repo) but may not yet have any
        `<slug>/.salesforce.json` if the branch predates that slug -- the
        origin-remote check is the fallback that still recognizes it."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_customer_opportunities_checkout(
                tmp, with_opp_dirs=False, with_origin=True)
            root, why = cli._cwd_customer_opportunities_root(start=repo)
            self.assertEqual(os.path.realpath(root), os.path.realpath(repo))
            self.assertIn("origin remote", why)

    def test_a_repo_with_neither_shape_nor_origin_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_unrelated_repo(tmp)
            root, why = cli._cwd_customer_opportunities_root(start=repo)
            self.assertIsNone(root)
            self.assertIsNone(why)

    def test_an_unrelated_repo_with_a_different_origin_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_unrelated_repo(tmp)
            _git(repo, "remote", "add", "origin",
                "git@github.com:craig-ai-tooling/ai-lawnmower.git")
            root, why = cli._cwd_customer_opportunities_root(start=repo)
            self.assertIsNone(root)

    def test_a_plain_directory_that_is_not_a_git_repo_at_all_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = os.path.join(tmp, "not-a-repo")
            os.makedirs(plain)
            root, why = cli._cwd_customer_opportunities_root(start=plain)
            self.assertIsNone(root)
            self.assertIsNone(why)


class ResolveRepoPrecedence(unittest.TestCase):
    """cli._resolve_repo() -- OPP_REPO first, then cwd detection, then the
    hardcoded default. Exercises the three acceptance cases from the spec
    directly: cwd-in-worktree -> that root; cwd-elsewhere -> default;
    OPP_REPO set -> wins."""

    def setUp(self):
        self._old_opp_repo = os.environ.pop("OPP_REPO", None)
        self._old_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._old_cwd)
        if self._old_opp_repo is not None:
            os.environ["OPP_REPO"] = self._old_opp_repo
        else:
            os.environ.pop("OPP_REPO", None)

    def test_cwd_inside_a_customer_opportunities_worktree_resolves_to_that_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_customer_opportunities_checkout(tmp)
            os.chdir(repo)
            path, source = cli._resolve_repo()
            self.assertEqual(os.path.realpath(path), os.path.realpath(repo))
            self.assertNotEqual(source, "OPP_REPO env var")
            self.assertIn("customer-opportunities", source)

    def test_cwd_elsewhere_falls_back_to_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            unrelated = make_unrelated_repo(os.path.join(tmp, "unrelated"))
            os.chdir(unrelated)
            path, source = cli._resolve_repo()
            self.assertEqual(path, cli._DEFAULT_REPO)
            self.assertIn("default", source)
            self.assertIn(cli._DEFAULT_REPO, source)

    def test_a_plain_tmp_dir_with_no_git_at_all_falls_back_to_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = os.path.join(tmp, "plain")
            os.makedirs(plain)
            os.chdir(plain)
            path, source = cli._resolve_repo()
            self.assertEqual(path, cli._DEFAULT_REPO)

    def test_opp_repo_env_var_wins_even_inside_a_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_customer_opportunities_checkout(tmp)
            os.chdir(repo)
            os.environ["OPP_REPO"] = "/explicit/override/path"
            path, source = cli._resolve_repo()
            self.assertEqual(path, "/explicit/override/path")
            self.assertEqual(source, "OPP_REPO env var")

    def test_opp_repo_env_var_wins_from_an_unrelated_cwd_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            unrelated = make_unrelated_repo(os.path.join(tmp, "unrelated"))
            os.chdir(unrelated)
            os.environ["OPP_REPO"] = "/explicit/override/path"
            path, source = cli._resolve_repo()
            self.assertEqual(path, "/explicit/override/path")
            self.assertEqual(source, "OPP_REPO env var")


class DoctorReportsTheResolvedRoot(unittest.TestCase):
    """`opp-axi doctor` must say which root is in use and why -- not merely
    print the path, the way every other config row does."""

    def test_doctor_json_reports_the_repo_source(self):
        import contextlib
        import io
        import json
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            fake_repo = os.path.join(tmp, "customer-opportunities")
            os.makedirs(fake_repo)
            with mock.patch.object(cli, "REPO", fake_repo), \
                 mock.patch.object(cli, "REPO_SOURCE", "OPP_REPO env var"):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    with self.assertRaises(SystemExit):
                        cli.cmd_doctor(type("A", (), {"json": True})())
                out = json.loads(buf.getvalue())
        repo_row = next(c for c in out["config"] if c["var"] == "OPP_REPO")
        self.assertEqual(repo_row["value"], fake_repo)
        self.assertEqual(repo_row["source"], "OPP_REPO env var")


if __name__ == "__main__":
    unittest.main()
