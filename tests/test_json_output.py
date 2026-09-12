"""`--json` for `triage` and `wispr`.

Two Lawnmower call sites (loop/hush.py, loop/lawnmower-night) regex free prose out of
these two subcommands because there was no structured alternative. These tests pin the
machine-readable contract that replaces the regex: suppression state for `triage`, cache
freshness for `wispr` — and the exit-code contract (a never-synced cache still exits
E_PARTIAL, never 0, even under --json).

All fixture-based: no live Salesforce, no live Google, no network.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from opp_axi import cli  # noqa: E402


def run(args, **envkw):
    import subprocess
    e = dict(os.environ)
    e.update(envkw)
    return subprocess.run([sys.executable, "-m", "opp_axi", *args],
                          capture_output=True, text=True, cwd=ROOT, env=e, timeout=180)


def _write_wispr_cache(d, last_sync=None):
    open(os.path.join(d, "meetings.jsonl"), "w").close()
    if last_sync is not None:
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump({"last_sync": last_sync}, f)


class TestWisprJSONShape(unittest.TestCase):
    """Cache freshness as FIELDS: last_sync, a status enum, and the query window."""

    def test_never_synced_json_shape_and_exit_partial(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "nope")
            r = run(["wispr", "--json"], OPP_WISPR_DIR=missing)
            self.assertEqual(r.returncode, cli.E_PARTIAL,
                             f"never-synced must exit E_PARTIAL, not 0. stdout={r.stdout!r}")
            data = json.loads(r.stdout)
            self.assertEqual(data["cache"]["status"], "absent")
            self.assertIsNone(data["cache"]["last_sync"])
            self.assertIn("window", data)
            self.assertIn("since", data["window"])

    def test_disabled_json_is_clean_exit_zero(self):
        with tempfile.TemporaryDirectory() as d:
            r = run(["wispr", "--json"], OPP_WISPR="off", OPP_WISPR_DIR=os.path.join(d, "nope"))
            self.assertEqual(r.returncode, 0, "a deliberate opt-out is not a gap")
            data = json.loads(r.stdout)
            self.assertEqual(data["cache"]["status"], "off")

    def test_stale_cache_json_and_exit_partial(self):
        with tempfile.TemporaryDirectory() as d:
            old = (datetime.now() - timedelta(days=cli.WISPR_STALE_DAYS + 5)).isoformat()
            _write_wispr_cache(d, old)
            r = run(["wispr", "--json"], OPP_WISPR_DIR=d)
            data = json.loads(r.stdout)
            self.assertEqual(data["cache"]["status"], "stale")
            self.assertEqual(data["cache"]["last_sync"], old)
            self.assertEqual(r.returncode, cli.E_PARTIAL)

    def test_partial_cache_json_and_exit_partial(self):
        """Fresh enough by days, but the sync predates the tail of the window asked about."""
        with tempfile.TemporaryDirectory() as d:
            recent = (datetime.now() - timedelta(hours=cli.WISPR_GAP_HOURS + 4)).isoformat()
            _write_wispr_cache(d, recent)
            r = run(["wispr", "--json"], OPP_WISPR_DIR=d)
            data = json.loads(r.stdout)
            self.assertEqual(data["cache"]["status"], "partial")
            self.assertEqual(r.returncode, cli.E_PARTIAL)

    def test_ok_cache_json_and_exit_zero(self):
        with tempfile.TemporaryDirectory() as d:
            fresh = (datetime.now() - timedelta(hours=1)).isoformat()
            _write_wispr_cache(d, fresh)
            r = run(["wispr", "--json"], OPP_WISPR_DIR=d)
            data = json.loads(r.stdout)
            self.assertEqual(data["cache"]["status"], "ok")
            self.assertEqual(r.returncode, 0)

    def test_json_output_is_pure_json_no_toon(self):
        """A caller doing json.loads(stdout) must not have to strip anything first."""
        with tempfile.TemporaryDirectory() as d:
            fresh = (datetime.now() - timedelta(hours=1)).isoformat()
            _write_wispr_cache(d, fresh)
            r = run(["wispr", "--json"], OPP_WISPR_DIR=d)
            json.loads(r.stdout)  # must not raise

    def test_default_human_output_unchanged_by_json_flag_existing(self):
        """--json must not perturb the default (no --json) rendering."""
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "nope")
            r = run(["wispr"], OPP_WISPR_DIR=missing)
            self.assertIn("UNAVAILABLE", r.stdout)
            self.assertNotIn("wispr[0]", r.stdout)


class TestWisprFreshnessEnum(unittest.TestCase):
    """The enum function directly, so the five states are pinned independent of the CLI."""

    def test_off(self):
        with mock.patch.object(cli, "WISPR_ENABLED", False):
            status, _ = cli.wispr_freshness({})
        self.assertEqual(status, "off")

    def test_absent_never_synced(self):
        status, _ = cli.wispr_freshness({})
        self.assertEqual(status, "absent")

    def test_stale(self):
        old = datetime.now() - timedelta(days=cli.WISPR_STALE_DAYS + 1)
        status, _ = cli.wispr_freshness({"last_sync": old.isoformat()})
        self.assertEqual(status, "stale")

    def test_partial(self):
        recent = datetime.now() - timedelta(hours=cli.WISPR_GAP_HOURS + 1)
        status, _ = cli.wispr_freshness({"last_sync": recent.isoformat()})
        self.assertEqual(status, "partial")

    def test_ok(self):
        fresh = datetime.now() - timedelta(minutes=5)
        status, _ = cli.wispr_freshness({"last_sync": fresh.isoformat()})
        self.assertEqual(status, "ok")


class TestTriageJSONShape(unittest.TestCase):
    """Suppression state as FIELDS: what (pattern), for whom (slug), until when.

    All fixture-based: sf_query / load_patterns / opp_index / wispr_load are monkeypatched,
    so this never touches live Salesforce, and REPO is pointed at a directory with no
    account dirs so repo_touched_since() cannot pick up real customer-opportunities files.
    """

    def _ns(self, **kw):
        import argparse
        base = dict(since=None, days=7, max_deep=15, push=False, json=True)
        base.update(kw)
        return argparse.Namespace(**base)

    def setUp(self):
        cli._GAPS.clear()
        self.addCleanup(cli._GAPS.clear)

    def _run_triage(self, patterns, recs, idx, wispr=([], {})):
        with mock.patch.object(cli, "load_patterns", return_value=patterns), \
             mock.patch.object(cli, "sf_query", return_value=recs), \
             mock.patch.object(cli, "opp_index", return_value=idx), \
             mock.patch.object(cli, "wispr_load", return_value=wispr), \
             mock.patch.object(cli, "WISPR_ENABLED", True), \
             tempfile.TemporaryDirectory() as empty_repo, \
             mock.patch.object(cli, "REPO", empty_repo):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.cmd_triage(self._ns())
        return json.loads(buf.getvalue())

    def test_suppressed_finding_reports_pattern_slug_until(self):
        patterns = [{
            "id": "entering-prove-value", "priority": 2, "work": "add a plan",
            "suppress": [{"target": "accounts/acme", "until": "2099-01-01",
                          "why": "new product, process is loose by design"}],
        }]
        recs = [{
            "Id": "006AAAAAAAAAAAAAAA", "Name": "Acme - Land",
            "Amount": 1000, "CloseDate": "2026-12-01", "StageName": "Prove Value",
            "Sales_Engineer_Overview__c": "", "POV_Pass__c": False,
            "Hands_on_Eval_POV_URL__c": "",
        }]
        idx = {"acme": {"account_name": "Acme Corp",
                        "opportunities": [{"id": "006AAAAAAAAAAAAAAA", "primary": True,
                                           "status": "open"}]}}
        data = self._run_triage(patterns, recs, idx)

        self.assertIn("findings", data)
        self.assertIn("suppressed", data)
        self.assertEqual(data["findings"], [],
                         "a suppressed finding must be withheld, not reported as a finding")
        self.assertEqual(len(data["suppressed"]), 1)
        s = data["suppressed"][0]
        self.assertEqual(s["pattern"], "entering-prove-value")   # WHAT is suppressed
        self.assertEqual(s["slug"], "acme")                      # for WHOM
        self.assertEqual(s["until"], "2099-01-01")               # UNTIL when

    def test_unsuppressed_finding_reported_not_withheld(self):
        patterns = [{"id": "entering-prove-value", "priority": 2, "work": "add a plan"}]
        recs = [{
            "Id": "006BBBBBBBBBBBBBBB", "Name": "Beta - Land",
            "Amount": 500, "CloseDate": "2026-12-01", "StageName": "Prove Value",
            "Sales_Engineer_Overview__c": "", "POV_Pass__c": False,
            "Hands_on_Eval_POV_URL__c": "",
        }]
        idx = {"beta": {"account_name": "Beta Corp",
                        "opportunities": [{"id": "006BBBBBBBBBBBBBBB", "primary": True,
                                           "status": "open"}]}}
        data = self._run_triage(patterns, recs, idx)
        self.assertEqual(data["suppressed"], [])
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["findings"][0]["pattern"], "entering-prove-value")
        self.assertEqual(data["findings"][0]["slug"], "beta")

    def test_wispr_cache_state_surfaced_as_never_synced(self):
        data = self._run_triage([], [], {}, wispr=([], {}))
        self.assertEqual(data["wispr_cache"]["status"], "absent")

    def test_wispr_cache_state_surfaced_as_stale(self):
        old = (datetime.now() - timedelta(days=cli.WISPR_STALE_DAYS + 3)).isoformat()
        data = self._run_triage([], [], {}, wispr=([], {"last_sync": old}))
        self.assertEqual(data["wispr_cache"]["status"], "stale")

    def test_json_output_is_pure_json_no_toon(self):
        data = self._run_triage([], [], {})
        self.assertIsInstance(data, dict)

    def test_default_human_output_still_toon_when_no_json_flag(self):
        patterns = [{"id": "entering-prove-value", "priority": 2, "work": "add a plan"}]
        recs = []
        idx = {}
        with mock.patch.object(cli, "load_patterns", return_value=patterns), \
             mock.patch.object(cli, "sf_query", return_value=recs), \
             mock.patch.object(cli, "opp_index", return_value=idx), \
             mock.patch.object(cli, "wispr_load", return_value=([], {})), \
             mock.patch.object(cli, "WISPR_ENABLED", True):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.cmd_triage(self._ns(json=False))
        out = buf.getvalue()
        self.assertTrue(out.startswith("triage since="))
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)


if __name__ == "__main__":
    unittest.main()
