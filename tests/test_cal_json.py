"""`opp-axi cal`: Craig's calendar day (not the box's UTC day), --json, and the
declined-self-attendee filter.

The box runs Etc/UTC; Craig reads America/Los_Angeles. The old `...Z` day window
was midnight-to-midnight UTC, so at 5:30am Pacific "today" actually returned
5pm-yesterday..5pm-today Pacific. craig_day_bounds() (shared with now_local()/
today_stamp(), see tests/test_entry_date_is_craigs_day.py) fixes that; this file
pins cmd_cal's use of it plus the new --json contract.

All fixture-based: cli.gcli/_run/opp_index/build_matcher are monkeypatched. The
`gws`/`gog` CLI is never invoked and no network call is made.
"""
import argparse
import contextlib
import io
import json
import os
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from opp_axi import cli        # noqa: E402

TODAY_LOCAL = datetime(2026, 9, 22, 5, 30, 0)   # 5:30am Pacific -- the bug's exact window


def _ns(date="today", json=False):     # noqa: A002
    return argparse.Namespace(date=date, json=json)


LONG_SUMMARY = ("Acme Corp quarterly business review, roadmap alignment and "
                "renewal planning call with the full account team")   # > 52 chars

EVENTS = [
    {"status": "confirmed", "summary": "All-day planning day",
     "start": {"date": "2026-09-22"}, "end": {"date": "2026-09-23"}},
    {"status": "confirmed", "summary": "Skip me, declined",
     "start": {"dateTime": "2026-09-22T09:00:00-07:00"},
     "end": {"dateTime": "2026-09-22T09:30:00-07:00"},
     "attendees": [{"email": "craig.smith@spectrocloud.com", "self": True,
                    "responseStatus": "declined"}]},
    {"status": "cancelled", "summary": "Cancelled hold",
     "start": {"dateTime": "2026-09-22T11:00:00-07:00"},
     "end": {"dateTime": "2026-09-22T11:30:00-07:00"}},
    {"status": "confirmed", "summary": LONG_SUMMARY,
     "start": {"dateTime": "2026-09-22T14:00:00-07:00"},
     "end": {"dateTime": "2026-09-22T15:00:00-07:00"},
     "attendees": [
         {"email": "craig.smith@spectrocloud.com", "self": True, "responseStatus": "accepted"},
         {"email": "buyer@acme.com", "responseStatus": "needsAction"},
         {"email": "partner@othercorp.com", "responseStatus": "tentative"},
     ]},
]


def _fake_run(cmd, *a, **kw):
    assert "calendar" in cmd
    return SimpleNamespace(stdout=json.dumps({"items": EVENTS}), returncode=0)


def _run_cal(**kw):
    buf = io.StringIO()
    with mock.patch.object(cli, "gcli", return_value="gws"), \
         mock.patch.object(cli, "_run", side_effect=_fake_run), \
         mock.patch.object(cli, "opp_index", return_value={}), \
         mock.patch.object(cli, "build_matcher",
                           return_value=lambda text, domains: "acme" if "acme.com" in domains else ""), \
         mock.patch.object(cli, "now_local", return_value=TODAY_LOCAL), \
         contextlib.redirect_stdout(buf):
        cli.cmd_cal(_ns(**kw))
    return buf.getvalue()


class TestCraigDayNotBoxDay(unittest.TestCase):
    """--date today|tomorrow resolves against now_local(), never datetime.now()."""

    def test_today_uses_craigs_calendar_date_not_utc(self):
        captured = {}

        def capture_run(cmd, *a, **kw):
            captured["params"] = json.loads(cmd[cmd.index("--params") + 1])
            return SimpleNamespace(stdout=json.dumps({"items": []}), returncode=0)

        with mock.patch.object(cli, "gcli", return_value="gws"), \
             mock.patch.object(cli, "_run", side_effect=capture_run), \
             mock.patch.object(cli, "opp_index", return_value={}), \
             mock.patch.object(cli, "now_local", return_value=TODAY_LOCAL), \
             mock.patch.object(cli, "OPP_TZ", "America/Los_Angeles"), \
             contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_cal(_ns(date="today"))
        self.assertEqual(captured["params"]["timeMin"], "2026-09-22T00:00:00-07:00")
        self.assertEqual(captured["params"]["timeMax"], "2026-09-23T00:00:00-07:00")

    def test_tomorrow_is_one_day_after_craigs_today(self):
        captured = {}

        def capture_run(cmd, *a, **kw):
            captured["params"] = json.loads(cmd[cmd.index("--params") + 1])
            return SimpleNamespace(stdout=json.dumps({"items": []}), returncode=0)

        with mock.patch.object(cli, "gcli", return_value="gws"), \
             mock.patch.object(cli, "_run", side_effect=capture_run), \
             mock.patch.object(cli, "opp_index", return_value={}), \
             mock.patch.object(cli, "now_local", return_value=TODAY_LOCAL), \
             mock.patch.object(cli, "OPP_TZ", "America/Los_Angeles"), \
             contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_cal(_ns(date="tomorrow"))
        self.assertEqual(captured["params"]["timeMin"], "2026-09-23T00:00:00-07:00")
        self.assertEqual(captured["params"]["timeMax"], "2026-09-24T00:00:00-07:00")


class TestCalDeclinedFilter(unittest.TestCase):
    def test_declined_and_cancelled_are_both_skipped(self):
        data = json.loads(_run_cal(json=True))
        summaries = [e["summary"] for e in data["events"]]
        self.assertNotIn("Skip me, declined", summaries)
        self.assertNotIn("Cancelled hold", summaries)
        self.assertEqual(len(data["events"]), 2)

    def test_my_response_reported_for_kept_events(self):
        data = json.loads(_run_cal(json=True))
        by_summary = {e["summary"]: e for e in data["events"]}
        self.assertEqual(by_summary["All-day planning day"]["my_response"], "")
        self.assertEqual(by_summary[LONG_SUMMARY]["my_response"], "accepted")


class TestCalJSONShape(unittest.TestCase):
    def test_top_level_keys(self):
        data = json.loads(_run_cal(json=True))
        self.assertEqual(set(data.keys()), {"date", "events"})
        self.assertEqual(data["date"], "2026-09-22")

    def test_event_keys(self):
        data = json.loads(_run_cal(json=True))
        for ev in data["events"]:
            self.assertEqual(set(ev.keys()), {
                "start", "end", "all_day", "summary", "attendees",
                "ext_domains", "opp", "my_response",
            })

    def test_all_day_flag(self):
        data = json.loads(_run_cal(json=True))
        by_summary = {e["summary"]: e for e in data["events"]}
        self.assertTrue(by_summary["All-day planning day"]["all_day"])
        self.assertEqual(by_summary["All-day planning day"]["start"], "2026-09-22")
        self.assertFalse(by_summary[LONG_SUMMARY]["all_day"])

    def test_summary_not_truncated_in_json(self):
        data = json.loads(_run_cal(json=True))
        by_summary = {e["summary"]: e for e in data["events"]}
        self.assertIn(LONG_SUMMARY, by_summary)
        self.assertEqual(by_summary[LONG_SUMMARY]["summary"], LONG_SUMMARY)
        self.assertGreater(len(LONG_SUMMARY), 52)

    def test_ext_domains_not_capped_at_3(self):
        data = json.loads(_run_cal(json=True))
        by_summary = {e["summary"]: e for e in data["events"]}
        self.assertEqual(sorted(by_summary[LONG_SUMMARY]["ext_domains"]),
                         ["acme.com", "othercorp.com"])

    def test_opp_match_surfaced(self):
        data = json.loads(_run_cal(json=True))
        by_summary = {e["summary"]: e for e in data["events"]}
        self.assertEqual(by_summary[LONG_SUMMARY]["opp"], "acme")

    def test_attendee_count(self):
        data = json.loads(_run_cal(json=True))
        by_summary = {e["summary"]: e for e in data["events"]}
        self.assertEqual(by_summary[LONG_SUMMARY]["attendees"], 3)


class TestCalTOONUnchanged(unittest.TestCase):
    def test_toon_summary_still_truncated_to_52(self):
        out = _run_cal(json=False)
        self.assertNotIn(LONG_SUMMARY, out)
        self.assertIn(LONG_SUMMARY[:52], out)

    def test_toon_output_is_not_json(self):
        out = _run_cal(json=False)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)
        self.assertIn("events[2]", out)

    def test_declined_excluded_from_toon_counts_too(self):
        out = _run_cal(json=False)
        self.assertIn("events:2", out)


class TestUnreadableSourceNeverEmptyResult(unittest.TestCase):
    """AGENTS.md: a source that could not be read is never rendered as an empty
    result. A bad response from the calendar API must exit non-zero and print
    nothing -- never an empty `events[0]` or `{"events": []}`."""

    def test_bad_json_from_calendar_exits_nonzero_and_prints_nothing(self):
        def broken_run(cmd, *a, **kw):
            return SimpleNamespace(stdout="not json at all", returncode=1)

        buf = io.StringIO()
        with mock.patch.object(cli, "gcli", return_value="gws"), \
             mock.patch.object(cli, "_run", side_effect=broken_run), \
             mock.patch.object(cli, "opp_index", return_value={}), \
             mock.patch.object(cli, "now_local", return_value=TODAY_LOCAL), \
             contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_cal(_ns(date="today", json=True))
        self.assertNotEqual(cm.exception.code, 0)
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
