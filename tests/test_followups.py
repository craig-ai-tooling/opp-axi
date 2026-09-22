"""`opp-axi followups` -- which open opps deserve a look today, ranked and
explainable, for the ai-lawnmower morning brief to consume as JSON.

Pins three things separately: the pure text helpers (extract_next, next_names_me,
guard.top_entry -- "only the newest SE Activity entry counts"), the scoring/sort
rules against a mocked cli.sf_query (no live Salesforce), and the --json contract
the morning brief actually reads.
"""
import argparse
import contextlib
import io
import json
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from opp_axi import cli, guard         # noqa: E402

TODAY = datetime(2026, 9, 22, 8, 0, 0)   # naive, as cli.now_local() returns


def _ns(limit=5, json=False):          # noqa: A002
    return argparse.Namespace(limit=limit, json=json)


# ── pure helpers ──────────────────────────────────────────────────────────────
class TestExtractNext(unittest.TestCase):
    def test_next_colon_label(self):
        text = "9/17/26 CS: Talked to customer. Next: schedule POV kickoff with security team."
        self.assertEqual(cli.extract_next(text),
                         "schedule POV kickoff with security team.")

    def test_next_steps_colon_label_case_insensitive(self):
        text = "9/17/26 CS: Deep dive done. NEXT STEPS: AE to loop in legal."
        self.assertEqual(cli.extract_next(text), "AE to loop in legal.")

    def test_no_label_is_empty(self):
        self.assertEqual(cli.extract_next("9/17/26 CS: Good call, no blockers."), "")

    def test_bare_word_next_without_colon_is_not_a_label(self):
        """'the next meeting is Tuesday' is prose, not a Next: label."""
        text = "9/17/26 CS: the next meeting is Tuesday, nothing else pending."
        self.assertEqual(cli.extract_next(text), "")

    def test_trims_to_200_chars_at_a_word_boundary(self):
        long_tail = "word " * 60   # 300 chars, well past the 200-char limit
        text = f"9/17/26 CS: call went well. Next: {long_tail}"
        out = cli.extract_next(text)
        self.assertLessEqual(len(out), 200)
        self.assertFalse(out.endswith("wor"), "must not cut mid-word")
        self.assertTrue(long_tail.startswith(out.split(" ")[0]))


class TestTrimWordBoundary(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(cli.trim_word_boundary("short text", 200), "short text")

    def test_cuts_at_last_space_within_limit(self):
        text = "a" * 195 + " " + "b" * 20
        out = cli.trim_word_boundary(text, 200)
        self.assertEqual(out, "a" * 195)

    def test_no_space_in_range_hard_cuts(self):
        text = "a" * 250
        self.assertEqual(cli.trim_word_boundary(text, 200), "a" * 200)


class TestNextNamesMe(unittest.TestCase):
    def test_cs_whole_word_matches(self):
        self.assertTrue(cli.next_names_me("assign to CS for review"))

    def test_craig_matches_case_insensitively(self):
        self.assertTrue(cli.next_names_me("craig to send the deck"))
        self.assertTrue(cli.next_names_me("Craig to send the deck"))

    def test_csv_does_not_false_match_cs(self):
        """'CS' must be a whole word -- CSV, CST etc. must not match."""
        self.assertFalse(cli.next_names_me("export the CSV for finance"))

    def test_someone_elses_initials_do_not_match(self):
        self.assertFalse(cli.next_names_me("AE to send updated pricing"))

    def test_empty_text_does_not_match(self):
        self.assertFalse(cli.next_names_me(""))


class TestGuardTopEntry(unittest.TestCase):
    """Only the newest entry counts -- the same boundary amend_top_entry replaces."""

    def test_single_entry_field_returns_whole_text(self):
        text = "9/20/26 CS: Kickoff went well. Next: schedule POV."
        self.assertEqual(guard.top_entry(text), text)

    def test_multi_entry_field_returns_only_the_first(self):
        text = ("9/20/26 CS: Customer confirmed budget. Next: waiting on legal review.\n"
               "9/10/26 CS: Kickoff went well. Next: CS to send POV plan by Friday.\n"
               "9/1/26 CS: Intro call.")
        top = guard.top_entry(text)
        self.assertEqual(top, "9/20/26 CS: Customer confirmed budget. "
                              "Next: waiting on legal review.")
        self.assertNotIn("Kickoff", top)

    def test_iso_dated_second_entry_is_also_a_boundary(self):
        text = "9/20/26 CS: newest.\n2026-09-10 - older entry."
        self.assertEqual(guard.top_entry(text), "9/20/26 CS: newest.")

    def test_amend_top_entry_unchanged_by_the_refactor(self):
        existing = "9/20/26 CS: old top.\n9/10/26 CS: older."
        out = guard.amend_top_entry(existing, "9/20/26 CS: replaced top.")
        self.assertEqual(out, "9/20/26 CS: replaced top.\n9/10/26 CS: older.")


class TestNextMineOnlyReadsNewestEntry(unittest.TestCase):
    """A CS mention buried in an OLDER entry must not flip next_mine for today."""

    def test_older_entry_mention_is_ignored(self):
        act = ("9/20/26 CS: Customer confirmed budget. Next: waiting on legal review.\n"
              "9/10/26 CS: Kickoff went well. Next: CS to send POV plan by Friday.")
        top = guard.top_entry(act)
        next_text = cli.extract_next(top)
        self.assertEqual(next_text, "waiting on legal review.")
        self.assertFalse(cli.next_names_me(next_text))

    def test_newest_entry_mention_is_honored(self):
        act = ("9/20/26 CS: Next: CS to send updated pricing.\n"
              "9/10/26 CS: Next: nothing outstanding.")
        top = guard.top_entry(act)
        next_text = cli.extract_next(top)
        self.assertTrue(cli.next_names_me(next_text))


# ── craig_day_bounds (used by cal, but a pure function pinned here too since
#    followups shares now_local()/today as its "today" source) ────────────────
class TestCraigDayBounds(unittest.TestCase):
    def test_pdt_offset_in_september(self):
        from datetime import date
        with mock.patch.object(cli, "OPP_TZ", "America/Los_Angeles"):
            lo, hi = cli.craig_day_bounds(date(2026, 9, 22))
        self.assertEqual(lo, "2026-09-22T00:00:00-07:00")
        self.assertEqual(hi, "2026-09-23T00:00:00-07:00")

    def test_pst_offset_in_january(self):
        from datetime import date
        with mock.patch.object(cli, "OPP_TZ", "America/Los_Angeles"):
            lo, hi = cli.craig_day_bounds(date(2026, 1, 15))
        self.assertEqual(lo, "2026-01-15T00:00:00-08:00")
        self.assertEqual(hi, "2026-01-16T00:00:00-08:00")

    def test_unknown_zone_falls_back_to_z_not_a_crash(self):
        from datetime import date
        with mock.patch.object(cli, "OPP_TZ", "Not/AZone"):
            lo, hi = cli.craig_day_bounds(date(2026, 9, 22))
        self.assertEqual(lo, "2026-09-22T00:00:00Z")
        self.assertEqual(hi, "2026-09-23T00:00:00Z")


# ── cmd_followups: scoring, sort, JSON contract ────────────────────────────────
IDX = {
    "acme":    {"account_name": "Acme Corp",
               "opportunities": [{"id": "006AAAAAAAAAAAAAAA", "primary": True, "status": "open"}]},
    "beta":    {"account_name": "Beta Inc",
               "opportunities": [{"id": "006BBBBBBBBBBBBBBB", "primary": True, "status": "open"}]},
    "gamma":   {"account_name": "Gamma LLC",
               "opportunities": [{"id": "006CCCCCCCCCCCCCCC", "primary": True, "status": "open"}]},
    "delta":   {"account_name": "Delta Co",
               "opportunities": [{"id": "006DDDDDDDDDDDDDDD", "primary": True, "status": "open"}]},
    "epsilon": {"account_name": "Epsilon Corp",
               "opportunities": [{"id": "006EEEEEEEEEEEEEEE", "primary": True, "status": "open"}]},
}


def _opp(oid, name, amount, close, act="", next_steps="", forecast="", risk=""):
    return {"Id": oid, "Name": name, "StageName": "Prove Value", "Amount": amount,
           "CloseDate": close, "SE_Forecast__c": forecast, "Tech_Risk_Status__c": risk,
           "Sales_Engineer_Overview__c": act, "Next_Steps__c": next_steps}


RECS = [
    # acme: 5 days PAST DUE -> +4. Fresh entry today -> no stale point. score=4.
    _opp("006AAAAAAAAAAAAAAA", "Acme - Platform", 100000, "2026-09-17",
        act="9/22/26 CS: On track. Next: nothing outstanding."),
    # beta: closes in 7 days -> +3. Next: names CS -> +3. forecast At Risk -> +1.
    # fresh entry today -> no stale point. score=7.
    _opp("006BBBBBBBBBBBBBBB", "Beta - Migration", 50000, "2026-09-29",
        act="9/22/26 CS: Called champion. Next: CS to send updated pricing.",
        forecast="At Risk"),
    # gamma: far-future close -> no close points. tech risk High -> +1. entry 15d
    # stale (>10) -> +1. score=2. Amount LOWER than epsilon (tie-break loser).
    _opp("006CCCCCCCCCCCCCCC", "Gamma - Expansion", 20000, "2026-11-21",
        act="9/7/26 CS: Waiting on customer.", risk="\U0001F534 High"),
    # delta: everything neutral -> score 0, excluded entirely.
    _opp("006DDDDDDDDDDDDDDD", "Delta - Renewal", 999999, "2026-11-21",
        act="9/22/26 CS: Nothing to report."),
    # epsilon: forecast Needs Attention -> +1, fresh entry -> no stale point.
    # score=1... bump to 2 by also using a stale (11d) entry so it ties gamma's
    # score=2 but with a HIGHER amount, to pin the score-desc/amount-desc sort.
    _opp("006EEEEEEEEEEEEEEE", "Epsilon - New Logo", 500000, "2026-11-21",
        act="9/11/26 CS: Quiet week.", forecast="Needs Attention"),
]


def _run_followups(recs, **kw):
    buf = io.StringIO()
    with mock.patch.object(cli, "sf_query", return_value=recs), \
         mock.patch.object(cli, "opp_index", return_value=IDX), \
         mock.patch.object(cli, "now_local", return_value=TODAY), \
         contextlib.redirect_stdout(buf):
        cli.cmd_followups(_ns(**kw))
    return buf.getvalue()


class TestFollowupsScoring(unittest.TestCase):
    def _json(self, recs, **kw):
        out = _run_followups(recs, json=True, **kw)
        return json.loads(out)

    def test_past_due_scores_4_with_reason(self):
        data = self._json(RECS, limit=0)
        acme = next(f for f in data["followups"] if f["slug"] == "acme")
        self.assertEqual(acme["score"], 4)
        self.assertIn("close date passed 9/17/26", acme["reasons"])
        self.assertEqual(acme["close_days"], -5)

    def test_closing_soon_plus_next_mine_plus_at_risk(self):
        data = self._json(RECS, limit=0)
        beta = next(f for f in data["followups"] if f["slug"] == "beta")
        self.assertEqual(beta["score"], 7)
        self.assertIn("closes 9/29/26", beta["reasons"])
        self.assertIn("your next step", beta["reasons"])
        self.assertIn("forecast At Risk", beta["reasons"])
        self.assertTrue(beta["next_mine"])
        self.assertEqual(beta["next"], "CS to send updated pricing.")

    def test_tech_risk_high_plus_stale_entry(self):
        data = self._json(RECS, limit=0)
        gamma = next(f for f in data["followups"] if f["slug"] == "gamma")
        self.assertEqual(gamma["score"], 2)
        self.assertIn("tech risk High", gamma["reasons"])
        self.assertIn("no SE entry since 9/7/26", gamma["reasons"])

    def test_zero_score_opp_is_excluded(self):
        data = self._json(RECS, limit=0)
        slugs = [f["slug"] for f in data["followups"]]
        self.assertNotIn("delta", slugs)
        # count is the total SCORED opps, not the raw open-pipeline count.
        self.assertEqual(data["count"], 4)

    def test_sort_is_score_desc_then_amount_desc(self):
        data = self._json(RECS, limit=0)
        slugs = [f["slug"] for f in data["followups"]]
        # beta(7) > acme(4) > {epsilon, gamma}(2, epsilon has the bigger amount)
        self.assertEqual(slugs, ["beta", "acme", "epsilon", "gamma"])

    def test_default_limit_is_5_but_count_is_uncapped(self):
        out = _run_followups(RECS)  # TOON, default limit
        self.assertIn("scored:4", out)
        self.assertIn("shown:4", out)  # only 4 ever score > 0 here

    def test_limit_truncates_json_followups_but_not_count(self):
        data = self._json(RECS, limit=2)
        self.assertEqual(len(data["followups"]), 2)
        self.assertEqual(data["count"], 4)

    def test_limit_zero_returns_every_scored_opp(self):
        data = self._json(RECS, limit=0)
        self.assertEqual(len(data["followups"]), data["count"])

    def test_stale_boundary_exactly_10_days_does_not_trigger(self):
        recs = [_opp("006FFFFFFFFFFFFFFF", "Zeta - Boundary", 1000, "2026-11-21",
                     act="9/12/26 CS: 10 days old exactly.")]
        data = self._json(recs, limit=0)
        self.assertEqual(data["followups"], [], "exactly 10 days is not OLDER than 10")

    def test_stale_11_days_triggers(self):
        recs = [_opp("006FFFFFFFFFFFFFFF", "Zeta - Boundary", 1000, "2026-11-21",
                     act="9/11/26 CS: 11 days old.")]
        data = self._json(recs, limit=0)
        self.assertEqual(len(data["followups"]), 1)
        self.assertIn("no SE entry since 9/11/26", data["followups"][0]["reasons"])

    def test_no_entry_at_all_still_scores_a_stale_point(self):
        recs = [_opp("006FFFFFFFFFFFFFFF", "Zeta - NoEntry", 1000, "2026-11-21", act="")]
        data = self._json(recs, limit=0)
        self.assertEqual(len(data["followups"]), 1)
        self.assertIsNone(data["followups"][0]["last_entry"])
        self.assertIn("no SE entry logged", data["followups"][0]["reasons"])

    def test_rep_next_comes_from_next_steps_newest_entry_via_rep_parser(self):
        """followups queries no Owner name (one query, no fan-out), so rep.py's
        parser -- with no owner_initials supplied -- leaves an unrecognized
        initials token (here 'MB') as part of the text rather than stripping it
        as an author. What matters here: only the NEWEST Next_Steps__c entry is
        used, never the older 9/1 one."""
        recs = [_opp("006FFFFFFFFFFFFFFF", "Zeta - RepNext", 1000, "2026-09-17",
                     act="9/22/26 CS: fine.",
                     next_steps="9/20/2026 MB Sent proposal, awaiting signature.\n"
                                "9/1/2026 MB Kickoff.")]
        data = self._json(recs, limit=0)
        self.assertEqual(data["followups"][0]["rep_next"],
                         "MB Sent proposal, awaiting signature.")
        self.assertNotIn("Kickoff", data["followups"][0]["rep_next"])

    def test_json_keys_match_the_morning_brief_contract(self):
        data = self._json(RECS, limit=0)
        self.assertEqual(set(data.keys()), {"today", "count", "followups"})
        self.assertEqual(data["today"], "2026-09-22")
        row = data["followups"][0]
        self.assertEqual(set(row.keys()), {
            "slug", "name", "stage", "amount", "close", "close_days", "score",
            "reasons", "next", "next_mine", "rep_next", "last_entry",
        })
        self.assertIsInstance(row["amount"], (int, float))
        self.assertIsInstance(row["reasons"], list)

    def test_toon_default_output_shape(self):
        out = _run_followups(RECS, limit=0)
        self.assertIn("followups[4]{slug,score,close,reasons,next}", out)
        self.assertIn("beta,7,9/29/26,", out)
        self.assertIn("\nnext: ", out)
        # --json must not leak into the default TOON path
        with self.assertRaises(json.JSONDecodeError):
            json.loads(out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
