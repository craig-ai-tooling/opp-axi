"""`coverage` -- which SE-owned fields are expected by now and are empty.

`sweep` asks that of one field (SE Activity). These pin the version that asks it of
the whole SE section, plus the five fields opp-axi could not write before 9/16/26.

All fixture-based: `cli.sf_query` is monkeypatched, the `sf` CLI is never invoked
and no Salesforce record is touched.
"""
import argparse
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from opp_axi import cli, guard        # noqa: E402

IDX = {"acme": {"account_name": "Acme Corp",
                "opportunities": [{"id": "006AAAAAAAAAAAAAAA", "primary": True,
                                   "status": "open"}]}}


def _args(**kw):
    # half="fields": these predate the repo-collateral half and assert on the
    # Salesforce tables, which the artifact section would otherwise sit beside.
    ns = argparse.Namespace(stage=None, limit=0, json=False, half="fields")
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _run(recs, **kw):
    """Run cmd_coverage over `recs` and return its stdout."""
    buf = io.StringIO()
    with mock.patch.object(cli, "sf_query", return_value=recs), \
         mock.patch.object(cli, "opp_index", return_value=IDX), \
         contextlib.redirect_stdout(buf):
        cli.cmd_coverage(_args(**kw))
    return buf.getvalue()


def _opp(oid, stage, **fields):
    r = {"Id": oid, "Name": f"Opp {oid}", "StageName": stage,
         "Amount": 1000, "CloseDate": "2026-12-31"}
    for api in cli.COVERAGE_FIELDS:
        r.setdefault(api, None)
    r.update(fields)
    return r


class NewWritableFields(unittest.TestCase):
    """The five fields Salesforce had and opp-axi did not know about."""

    NEW = ["Technical_Risk__c", "Technical_Risk_Reasoning__c",
           "Has_Technical_Success_Plan__c", "Hands_on_Eval_ActEndDate__c",
           "Technical_Win_Date__c"]

    def test_all_five_are_writable(self):
        apis = {api for _l, api, _k, _v in cli.FIELDS["write"]}
        for api in self.NEW:
            self.assertIn(api, apis, f"{api} must be in FIELDS['write']")

    def test_technical_risk_is_plain_high_low_not_emoji(self):
        """The emoji twin Tech_Risk_Status__c keeps its own values; this one must
        not inherit them, or a write goes in with an emoji the field rejects."""
        self.assertEqual(guard.PICKLISTS["Technical_Risk__c"], ["High", "Low"])
        api, kind = guard._resolve_field("Technical Risk")
        self.assertEqual((api, kind), ("Technical_Risk__c", "picklist"))
        self.assertEqual(guard._validate(api, kind, "High"), "High")

    def test_technical_risk_rejects_medium(self):
        """Medium is on neither risk field's active value set."""
        with self.assertRaises(SystemExit):
            guard._validate("Technical_Risk__c", "picklist", "Medium")

    def test_both_risk_pairs_resolve_distinctly(self):
        """Two live pairs under different API names. A label lookup must never
        silently land on the other one."""
        self.assertEqual(guard._resolve_field("Technical Risk")[0], "Technical_Risk__c")
        self.assertEqual(guard._resolve_field("Tech Risk Status")[0], "Tech_Risk_Status__c")
        self.assertEqual(guard._resolve_field("Technical Risk Reasoning")[0],
                         "Technical_Risk_Reasoning__c")
        self.assertEqual(guard._resolve_field("Tech Risk Rational")[0],
                         "Tech_Risk_Rational__c")


class StageGating(unittest.TestCase):

    def test_none_from_stage_applies_to_every_stage(self):
        for stage in cli.STAGE_ORDER:
            self.assertTrue(cli._stage_reached(stage, None))

    def test_later_stage_counts_as_reached(self):
        self.assertTrue(cli._stage_reached("Contract & Negotiation", "Prove Value"))
        self.assertTrue(cli._stage_reached("Prove Value", "Prove Value"))
        self.assertFalse(cli._stage_reached("Qualification", "Prove Value"))

    def test_unknown_stage_is_not_reached(self):
        """An unrecognised stage must not manufacture a gap -- that would be a
        finding about our own STAGE_ORDER list, reported against the customer."""
        self.assertFalse(cli._stage_reached("Some New Stage", "Prove Value"))

    def test_unchecked_box_counts_as_empty(self):
        """A checkbox is never null in Salesforce, it is False. Treating False as
        'filled' would hide every unchecked-box gap there is."""
        self.assertFalse(cli._filled(False))
        self.assertFalse(cli._filled(None))
        self.assertFalse(cli._filled(""))
        self.assertTrue(cli._filled(True))
        self.assertTrue(cli._filled("High"))


class CoverageReport(unittest.TestCase):

    def test_early_stage_opp_not_asked_for_pov_fields(self):
        """A Qualification opp owes the any-stage fields and nothing else. The
        POV rows still APPEAR in `rates`, at due=0 -- a field that is not yet due
        is a different fact from a field nobody tracks, and the table says which."""
        out = _run([_opp("006A", "Qualification",
                         Sales_Engineer_Overview__c="9/16/26 CS: note",
                         Technical_Risk__c="Low",
                         Technical_Risk_Reasoning__c="single-node appliance, known fixes")])
        rates = out.split("rates")[1].split("gaps")[0]
        gaps = out.split("gaps")[1].split("why:")[0]
        self.assertIn("Hands_on_Eval_POV_URL__c,0,0,-,Prove Value", rates)
        self.assertNotIn("Hands_on_Eval_POV_URL__c", gaps)
        self.assertIn("(none)", gaps)
        self.assertIn("clean on fields: 1/1", out)

    def test_prove_value_opp_is_asked_for_the_pov_fields(self):
        out = _run([_opp("006A", "Prove Value",
                         Sales_Engineer_Overview__c="9/16/26 CS: note",
                         Technical_Risk__c="Low",
                         Technical_Risk_Reasoning__c="because")])
        gaps = out.split("gaps")[1]
        self.assertIn("Hands_on_Eval_POV_URL__c", gaps)
        self.assertIn("Hands_on_Eval_ActStartDate__c", gaps)
        self.assertIn("clean on fields: 0/1", out)

    def test_risk_rating_without_reasoning_is_a_gap(self):
        out = _run([_opp("006A", "Qualification",
                         Sales_Engineer_Overview__c="x", Technical_Risk__c="High")])
        self.assertIn("Technical_Risk_Reasoning__c", out.split("gaps")[1])

    def test_no_risk_rating_means_no_reasoning_is_owed(self):
        """The conditional rows must not fire on an opp whose predicate is empty,
        or every unassessed opp reports two gaps for the price of one."""
        out = _run([_opp("006A", "Qualification", Sales_Engineer_Overview__c="x")])
        gaps = out.split("gaps")[1].split("why:")[0]
        self.assertIn("Technical_Risk__c", gaps)
        self.assertNotIn("Technical_Risk_Reasoning__c", gaps)

    def test_rates_denominator_is_opps_due_not_all_opps(self):
        """A Contract & Negotiation field must not be scored against opps that
        have not reached it -- that reads as a failure nobody could have avoided."""
        out = _run([_opp("006A", "Qualification", Sales_Engineer_Overview__c="x",
                         Technical_Risk__c="Low", Technical_Risk_Reasoning__c="y"),
                    _opp("006B", "Contract & Negotiation",
                         Sales_Engineer_Overview__c="x", Technical_Risk__c="Low",
                         Technical_Risk_Reasoning__c="y",
                         Hands_on_Eval_POV_URL__c="http://x",
                         Hands_on_Eval_ActStartDate__c="2026-01-01",
                         Has_Technical_Success_Plan__c=True)])
        rates = out.split("rates")[1].split("gaps")[0]
        row = [ln for ln in rates.splitlines()
               if "Has_Technical_Success_Plan__c" in ln][0]
        self.assertIn(",1,1,100%,", row)
        self.assertIn("clean on fields: 2/2", out)

    def test_stage_filter(self):
        recs = [_opp("006A", "Qualification"), _opp("006B", "Prove Value")]
        self.assertIn("opps=1", _run(recs, stage="Prove Value"))
        self.assertIn("opps=2", _run(recs))

    def test_empty_pipeline_says_none_not_a_blank_table(self):
        """`toon` renders [0] ... (none). An empty result must never be
        indistinguishable from a source that was not read."""
        out = _run([])
        self.assertIn("gaps[0]", out)
        self.assertIn("(none)", out)
        self.assertIn("clean on fields: 0/0", out)

    def test_json_carries_every_gap_even_when_the_table_is_limited(self):
        recs = [_opp(f"006{i}", "Prove Value") for i in range(5)]
        import json as _json
        buf = io.StringIO()
        with mock.patch.object(cli, "sf_query", return_value=recs), \
             mock.patch.object(cli, "opp_index", return_value=IDX), \
             contextlib.redirect_stdout(buf):
            cli.cmd_coverage(_args(limit=2, json=True))
        self.assertEqual(len(_json.loads(buf.getvalue())["gaps"]), 5)

    def test_table_limit_is_disclosed_not_silent(self):
        recs = [_opp(f"006{i}", "Prove Value") for i in range(5)]
        self.assertIn("+3 more", _run(recs, limit=2))

    def test_every_expected_field_carries_a_reason(self):
        """A coverage report that cannot say who asked for a field is a scold."""
        for _api, _gate, why in cli.EXPECT + cli.EXPECT_IF:
            self.assertTrue(why and len(why) > 20)

    def test_every_expected_field_is_writable_through_opp_axi(self):
        """Reporting a gap in a field the sanctioned write path refuses would
        leave no way to close it. This is the invariant that broke before 9/16/26."""
        apis = {api for _l, api, _k, _v in cli.FIELDS["write"]}
        for api, _gate, _why in cli.EXPECT + cli.EXPECT_IF:
            self.assertIn(api, apis, f"coverage expects {api} but field cannot write it")


class AmountSort(unittest.TestCase):

    def test_parses_money_shorthand(self):
        self.assertEqual(cli._amount("$1.2M"), 1_200_000)
        self.assertEqual(cli._amount("$750K"), 750_000)
        self.assertEqual(cli._amount("1,000"), 1000)
        self.assertEqual(cli._amount("-"), 0.0)
        self.assertEqual(cli._amount(None), 0.0)

    def test_worst_first(self):
        """Most gaps first, then biggest deal -- so the tail is what gets cut by
        --limit, never the $3M opp."""
        recs = [_opp("006A", "Prove Value", Amount=9_000_000,
                     Sales_Engineer_Overview__c="x", Technical_Risk__c="Low",
                     Technical_Risk_Reasoning__c="y",
                     Hands_on_Eval_ActStartDate__c="2026-01-01"),
                _opp("006B", "Prove Value", Amount=1000)]
        gaps = _run(recs).split("gaps")[1]
        self.assertLess(gaps.index("006B".lower()) if "006b" in gaps.lower() else 0,
                        len(gaps))
        first = [ln for ln in gaps.splitlines() if ln.startswith("  ")][0]
        self.assertIn("1,000", first)


if __name__ == "__main__":
    unittest.main()
