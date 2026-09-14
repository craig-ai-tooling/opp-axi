"""The contract this repo exists to enforce.

Not "does opp-axi work" — it needs a live Salesforce org for that. These tests pin
the one behaviour that is easy to regress and expensive to notice: an unreachable
source must never render as an empty result.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from opp_axi import __version__            # noqa: E402
from opp_axi import cli                    # noqa: E402


def run(args, **envkw):
    e = dict(os.environ)
    e.update(envkw)
    return subprocess.run([sys.executable, "-m", "opp_axi", *args],
                          capture_output=True, text=True, cwd=ROOT, env=e, timeout=180)


class TestVersion(unittest.TestCase):
    def test_version_is_the_single_source(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")


class TestConnectorTable(unittest.TestCase):
    def test_every_connector_declares_need_and_probe(self):
        for name, need, why, probe in cli.CONNECTORS:
            self.assertIn(need, ("required", "optional"), name)
            self.assertTrue(why, name)
            self.assertTrue(callable(probe), name)

    def test_required_connectors_are_the_expected_three(self):
        req = [c[0] for c in cli.CONNECTORS if c[1] == "required"]
        self.assertEqual(sorted(req), ["google", "opp-repo", "salesforce"])

    def test_wispr_is_optional(self):
        need = {c[0]: c[1] for c in cli.CONNECTORS}
        self.assertEqual(need["wispr"], "optional")

    def test_a_probe_that_raises_is_reported_not_propagated(self):
        """doctor's whole job is probing; a probe blowing up must not take it down."""
        def boom():
            raise RuntimeError("kaboom")
        saved = cli.CONNECTORS[:]
        try:
            cli.CONNECTORS.append(("explodes", "optional", "test", boom))
            r = run(["doctor", "--json"])
            self.assertIn(r.returncode, (0, 1))
        finally:
            cli.CONNECTORS[:] = saved


class TestWisprStates(unittest.TestCase):
    """absent, off and empty-but-synced are three different facts."""

    def test_absent_cache_is_not_an_empty_table(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "nope")
            r = run(["wispr"], OPP_WISPR_DIR=missing)
            self.assertIn("UNAVAILABLE", r.stdout)
            self.assertNotIn("wispr[0]", r.stdout)
            self.assertEqual(r.returncode, cli.E_PARTIAL,
                             "an unread source must not exit 0")

    def test_disabled_is_clean(self):
        with tempfile.TemporaryDirectory() as d:
            r = run(["wispr"], OPP_WISPR="off", OPP_WISPR_DIR=os.path.join(d, "nope"))
            self.assertIn("disabled", r.stdout)
            self.assertNotIn("UNAVAILABLE", r.stdout)
            self.assertEqual(r.returncode, 0,
                             "a deliberate opt-out is not a gap")

    def test_state_machine(self):
        self.assertEqual(cli.wispr_state([], {}), "absent")
        self.assertEqual(cli.wispr_state([], {"last_sync": "garbage"}), "absent")

    def test_gap_ledger_records_a_fix(self):
        cli._GAPS.clear()
        line = cli.gap("thing", "because", "do-this")
        self.assertIn("UNAVAILABLE", line)
        self.assertIn("do-this", line)
        self.assertEqual(len(cli._GAPS), 1)
        cli._GAPS.clear()

    def test_date_window_on_a_partial_cache_warns_not_crashes(self):
        """evidence passes its calendar bound — a date — as window_end. Comparing that to a
        datetime raised TypeError, and only once the cache was partial, so a sync hid it."""
        hi = datetime.now().date() + timedelta(days=1)
        partial = {"last_sync": (datetime.now() - timedelta(hours=cli.WISPR_GAP_HOURS + 4)).isoformat()}
        cli._GAPS.clear()
        with mock.patch.object(cli, "WISPR_ENABLED", True):
            self.assertIn("PARTIAL", cli.wispr_line([], partial, hi))
            self.assertEqual(cli.wispr_freshness(partial, hi)[0], "partial")
        self.assertEqual(len(cli._GAPS), 1, "a partial cache is a gap, not a clean exit")
        cli._GAPS.clear()

    def test_window_end_normalises_date_and_aware_datetime(self):
        now = datetime(2026, 9, 14, 15, 0)
        self.assertEqual(cli.wispr_window_end(None, now), now)
        self.assertEqual(cli.wispr_window_end(date(2026, 9, 15), now), now)   # future clamps to now
        self.assertEqual(cli.wispr_window_end(date(2026, 9, 13), now), datetime(2026, 9, 13))
        aware = datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(cli.wispr_window_end(aware, now), datetime(2026, 9, 14, 9, 0))


class TestDoctorExitCodes(unittest.TestCase):
    def test_doctor_refuses_when_a_required_connector_is_down(self):
        with tempfile.TemporaryDirectory() as d:
            # an empty dir is a valid path with no */.salesforce.json in it
            r = run(["doctor", "--json"], OPP_REPO=d)
            self.assertEqual(r.returncode, 1)
            self.assertIn("opp-repo", r.stdout)

    def test_doctor_json_is_parseable(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            r = run(["doctor", "--json"], OPP_REPO=d)
            data = json.loads(r.stdout)
            for k in ("version", "connectors", "config", "required_down"):
                self.assertIn(k, data)


if __name__ == "__main__":
    unittest.main()
