"""The repo half of `coverage` -- collateral on disk, not fields in Salesforce.

Until 9/16/26 the field side had `sweep` and then `coverage`, and the collateral
side had a paragraph of prose. These pin the half that closed that.

Fixture-based: `cli.sf_query` is monkeypatched and `cli.REPO` is pointed at a
tempdir, so no Salesforce call and no real opp directory is touched.
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from opp_axi import cli        # noqa: E402


def _args(**kw):
    ns = argparse.Namespace(stage=None, limit=0, json=False, half="artifacts")
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _opp(oid, stage, slug, **fields):
    r = {"Id": oid, "Name": f"Opp {slug}", "StageName": stage,
         "Amount": 1000, "CloseDate": "2026-12-31"}
    for api in cli.COVERAGE_FIELDS:
        r.setdefault(api, None)
    r.update(fields)
    return r, slug


class ArtifactCoverage(unittest.TestCase):

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo)

    def _scaffold(self, slug, opp_md=True, claude_md=True, docs_site=False):
        d = os.path.join(self.repo, slug)
        os.makedirs(d, exist_ok=True)
        if opp_md:
            with open(os.path.join(d, "OPP.md"), "w") as fh:
                fh.write("# note\n")
        if claude_md:
            with open(os.path.join(d, "CLAUDE.md"), "w") as fh:
                fh.write("pointer\n")
        if docs_site:
            os.makedirs(os.path.join(d, "docs-site"), exist_ok=True)

    def _run(self, pairs, **kw):
        recs = [r for r, _ in pairs]
        idx = {slug: {"account_name": slug,
                      "opportunities": [{"id": r["Id"], "primary": True,
                                         "status": "open"}]}
               for r, slug in pairs}
        buf = io.StringIO()
        with mock.patch.object(cli, "sf_query", return_value=recs), \
             mock.patch.object(cli, "opp_index", return_value=idx), \
             mock.patch.object(cli, "REPO", self.repo), \
             contextlib.redirect_stdout(buf):
            cli.cmd_coverage(_args(**kw))
        return buf.getvalue()

    def test_scaffolded_early_stage_opp_is_clean(self):
        self._scaffold("acme")
        out = self._run([_opp("006A", "Qualification", "acme")])
        self.assertIn("OPP.md,1,1,100%", out)
        self.assertIn("clean on collateral: 1/1", out)

    def test_docs_site_not_due_before_prove_value(self):
        self._scaffold("acme")
        out = self._run([_opp("006A", "Alignment", "acme")])
        self.assertIn("docs-site,0,0,-,Prove Value", out)

    def test_docs_site_due_at_prove_value(self):
        self._scaffold("acme")
        out = self._run([_opp("006A", "Prove Value", "acme")])
        self.assertIn("docs-site,1,0,0%", out)
        self.assertIn("docs-site", out.split("artifactGaps")[1])

    def test_missing_claude_md_is_a_gap(self):
        self._scaffold("acme", claude_md=False)
        out = self._run([_opp("006A", "Qualification", "acme")])
        self.assertIn("CLAUDE.md", out.split("artifactGaps")[1].split("why:")[0])

    def test_no_repo_dir_is_not_a_collateral_gap(self):
        """An opp nobody has scaffolded has not FAILED to produce collateral.
        Counting those as gaps buries the real ones under accounts that do not
        exist yet, and the fix is /new-opp, a different job."""
        out = self._run([_opp("006A", "Prove Value", "ghost")])
        self.assertIn("noRepoDir", out)
        self.assertIn("ghost", out.split("noRepoDir")[1])
        gaps = out.split("artifactGaps")[1].split("clean on")[0]
        self.assertIn("(none)", gaps)

    def test_unscaffolded_opp_excluded_from_the_rate_denominator(self):
        """It must not drag OPP.md's percentage down -- that would report a
        documentation problem where there is a scaffolding one."""
        self._scaffold("acme")
        out = self._run([_opp("006A", "Prove Value", "acme"),
                         _opp("006B", "Prove Value", "ghost")])
        self.assertIn("OPP.md,1,1,100%", out)

    def test_site_built_but_url_empty_is_flagged(self):
        """The cross-check. Neither half sees this alone: the Salesforce field is
        empty and the directory is present."""
        self._scaffold("acme", docs_site=True)
        out = self._run([_opp("006A", "Prove Value", "acme")])
        self.assertIn("docs-site/ built, POV URL empty", out)

    def test_url_set_elsewhere_does_not_also_report_a_missing_docs_site(self):
        """A filled POV URL means the plan exists, on EvalForce or in a Doc.
        Reporting a missing docs-site/ beside it reads as two problems and is
        none."""
        self._scaffold("acme")
        out = self._run([_opp("006A", "Prove Value", "acme",
                              Hands_on_Eval_POV_URL__c="https://docs.google.com/x")])
        gaps = out.split("artifactGaps")[1].split("why:")[0]
        self.assertIn("(none)", gaps)

    def test_site_built_and_url_set_is_clean(self):
        self._scaffold("acme", docs_site=True)
        out = self._run([_opp("006A", "Prove Value", "acme",
                              Hands_on_Eval_POV_URL__c="https://x")])
        self.assertIn("clean on collateral: 1/1", out)

    def test_every_artifact_carries_a_reason(self):
        for _path, _gate, _kind, why in cli.EXPECT_ARTIFACT:
            self.assertTrue(why and len(why) > 20)

    def test_meeting_notes_is_not_gated(self):
        """It is owed after a customer call, which is not a stage. Gating it by
        stage would invent a rule nobody stated."""
        self.assertNotIn("meeting-notes",
                         {p for p, _g, _k, _w in cli.EXPECT_ARTIFACT})


class BothHalves(unittest.TestCase):

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo)
        os.makedirs(os.path.join(self.repo, "acme"))
        for name in ("OPP.md", "CLAUDE.md"):
            with open(os.path.join(self.repo, "acme", name), "w") as fh:
                fh.write("x")

    def _run(self, half):
        rec, slug = _opp("006A", "Prove Value", "acme")
        idx = {"acme": {"account_name": "acme",
                        "opportunities": [{"id": "006A", "primary": True,
                                           "status": "open"}]}}
        buf = io.StringIO()
        with mock.patch.object(cli, "sf_query", return_value=[rec]), \
             mock.patch.object(cli, "opp_index", return_value=idx), \
             mock.patch.object(cli, "REPO", self.repo), \
             contextlib.redirect_stdout(buf):
            cli.cmd_coverage(_args(half=half))
        return buf.getvalue()

    def test_default_shows_both(self):
        out = self._run("both")
        self.assertIn("rates[", out)
        self.assertIn("artifactRates[", out)

    def test_fields_only(self):
        out = self._run("fields")
        self.assertIn("rates[", out)
        self.assertNotIn("artifactRates[", out)

    def test_artifacts_only(self):
        out = self._run("artifacts")
        self.assertIn("artifactRates[", out)
        self.assertNotIn("\nrates[", out)

    def test_json_carries_both_halves(self):
        rec, _ = _opp("006A", "Prove Value", "acme")
        idx = {"acme": {"account_name": "acme",
                        "opportunities": [{"id": "006A", "primary": True,
                                           "status": "open"}]}}
        buf = io.StringIO()
        with mock.patch.object(cli, "sf_query", return_value=[rec]), \
             mock.patch.object(cli, "opp_index", return_value=idx), \
             mock.patch.object(cli, "REPO", self.repo), \
             contextlib.redirect_stdout(buf):
            cli.cmd_coverage(_args(half="both", json=True))
        d = json.loads(buf.getvalue())
        for k in ("rates", "gaps", "artifactRates", "artifactGaps", "unscaffolded"):
            self.assertIn(k, d)


if __name__ == "__main__":
    unittest.main()
