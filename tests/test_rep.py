"""opp_axi/rep.py -- rep/AE-entered Salesforce data, READ ONLY. These tests pin that
split structurally (every rep field is refused as a write, the source carries no write
path), pin the Next_Steps__c parser against every real-world format it must handle, and
exercise `cmd_rep` (both the single-opp detail view and the no-ref rollup) against a
mocked `cli.sf_query` router -- no network, the `sf` CLI is never invoked except where
a test deliberately makes it fail to prove the gap-not-blank contract.
"""
import argparse
import contextlib
import io
import json
import os
import sys
import unittest
from datetime import date
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from opp_axi import cli, guard, rep       # noqa: E402

OID = "006AAAAAAAAAAAAAAA"
IDX = {"acme": {"account_name": "Acme Corp",
                "opportunities": [{"id": OID, "primary": True, "status": "open"}]}}


def _ns_rep(ref="acme", since=None, full=False, section=None, json=False):     # noqa: A002
    return argparse.Namespace(ref=ref, since=since, full=full, section=section, json=json)


# ── fixture: one fully-populated Opportunity + its related-object rows ───────
OPP_REC = {
    "Id": OID, "Name": "Acme Corp - Platform",
    "Owner": {"Name": "Matthew Byram"}, "SA_Assignment_Oppty__c": "CraigSmith",
    # deal
    "StageName": "Prove Value", "Amount": 250000, "CloseDate": "2026-10-31",
    "ForecastCategoryName": "Pipeline", "Forecast_Status__c": "On Track",
    "Probability__c": "60", "Confidence__c": "High",
    "Deal_Qualification_Health__c": "SS3 Criteria Incomplete",
    "Next_Steps_Last_Updated__c": "2026-09-10T12:00:00.000+0000",
    "Days_Since_Next_Steps_Update__c": 4, "Account_Executive__c": "Dana Fields",
    "Use_Case_Primary__c": "VM Modernization", "LeadSource": "Partner",
    "Partner__r": {"Name": "SHI"}, "SDR_of_Record__r": {"Name": "Jordan Lee"},
    # nextSteps
    "Next_Steps__c": ("9/14/2026 MB Deep dive went well, next call scheduled.\n"
                      "9/1/26 DF - Followed up on pricing."),
    # meddpicc
    "Metrics__c": "Reduce VM sprawl by 30%", "Economic_Buyer__c": "Jane CFO",
    "Economic_Buyer_Status__c": "Identified", "Decision_Criteria__c": "",
    "Decision_Process__c": "", "Paper_Process__c": "",
    "Identified_Pain__c": "Licensing costs", "Champion__c": "Bob ITDirector",
    "Champion_Status__c": "Confirmed", "Last_Time_Testing_Champion__c": "2026-08-01",
    "Competition__c": "", "Coach__c": "", "Coach_Status__c": "",
    "Compelling_Event__c": "Renewal in Q1",
    # qualification
    "Why_Anything__c": "Cost pressure", "Why_Now__c": "",
    "Why_Us__c": "Best fit for VMware exit", "Budget_Description__c": "",
    "Authority_Description__c": "", "Need_Description__c": "", "Timeline_Description__c": "",
    # narrative
    "Description": "Top opp for the region", "Use_Case_Notes__c": "",
    "Executive_Summary_Narrative__c": "", "Account_Plan_Narrative__c": "",
    "Mutual_Action_Plan_Narrative__c": "", "Value_Hypothesis_Narrative__c": "",
    "Channel_Notes__c": "", "Current_Technical_State__c": "VMware",
    "Desired_Technical_State__c": "Palette",
    # links
    "Executive_Summary__c": "", "Account_Plan__c": "",
    "EW_Mutual_Action_Plan__c": "", "Value_Hypothesis__c": "",
}

TASK_ROWS = [
    {"Id": "00T1", "Subject": "Call w/ customer",
     "Description": ("-----------Notes from Clari Copilot---------------\n"
                     "Smart Summary (Powered by RevAI) : Discussed pricing and timeline."),
     "TaskSubtype": "Call", "ActivityDate": "2026-08-13",
     "CreatedDate": "2026-08-13T15:00:00.000+0000", "Owner": {"Name": "Matthew Byram"}},
    {"Id": "00T2", "Subject": "AE Nudge — Stale Next Steps: Acme", "Description": "",
     "TaskSubtype": "Task", "ActivityDate": "2026-09-01",
     "CreatedDate": "2026-09-01T10:00:00.000+0000", "Owner": {"Name": "System"}},
]
EVENT_ROWS = [
    {"Id": "00E1", "Subject": "QBR", "Description": "Quarterly review",
     "ActivityDateTime": "2026-08-20T14:00:00.000+0000", "ActivityDate": "2026-08-20",
     "CreatedDate": "2026-08-19T10:00:00.000+0000", "Owner": {"Name": "Matthew Byram"}},
]
CDL_ROWS = [{"ContentDocumentId": "069AAA"}]
CV_ROWS = [
    {"Id": "068NOTE1", "ContentDocumentId": "069AAA", "Title": "Call w/ SHI 6/4/26",
     "FileType": "SNOTE", "TextPreview": "Discussed integration timeline with SHI.",
     "CreatedBy": {"Name": "Matthew Byram"}, "CreatedDate": "2026-06-04T10:00:00.000+0000",
     "LastModifiedDate": "2026-06-04T10:00:00.000+0000", "ContentSize": 512},
    {"Id": "068FILE1", "ContentDocumentId": "069AAA", "Title": "MSA.pdf", "FileType": "PDF",
     "TextPreview": "", "CreatedBy": {"Name": "Dana Fields"},
     "CreatedDate": "2026-07-01T10:00:00.000+0000",
     "LastModifiedDate": "2026-07-01T10:00:00.000+0000", "ContentSize": 20480},
]
HIST_ROWS = [
    {"Field": "StageName", "OldValue": "Alignment", "NewValue": "Prove Value",
     "CreatedDate": "2026-08-13T12:00:00.000+0000", "CreatedBy": {"Name": "Matthew Byram"}},
    {"Field": "created", "OldValue": None, "NewValue": None,
     "CreatedDate": "2026-01-05T09:00:00.000+0000", "CreatedBy": {"Name": "Matthew Byram"}},
]
OCR_ROWS = [
    {"Contact": {"Name": "Jane CFO", "Title": "CFO"}, "Role": "Economic Buyer",
     "IsPrimary": True},
]


def _router():
    """Dispatch a fake `cli.sf_query` on the SOQL's FROM clause -- a stand-in for
    Salesforce with no network involved."""
    def fn(soql, timeout=180):
        if "FROM Opportunity WHERE Id" in soql:
            return [dict(OPP_REC)]
        if "FROM Task WHERE WhatId" in soql:
            return [dict(x) for x in TASK_ROWS]
        if "FROM Event WHERE WhatId" in soql:
            return [dict(x) for x in EVENT_ROWS]
        if "FROM ContentDocumentLink WHERE LinkedEntityId" in soql:
            return [dict(x) for x in CDL_ROWS]
        if "FROM ContentVersion WHERE ContentDocumentId IN" in soql:
            return [dict(x) for x in CV_ROWS]
        if "FROM OpportunityFieldHistory WHERE OpportunityId" in soql:
            return [dict(x) for x in HIST_ROWS]
        if "FROM OpportunityContactRole WHERE OpportunityId" in soql:
            return [dict(x) for x in OCR_ROWS]
        raise AssertionError(f"unexpected SOQL: {soql}")
    return fn


class GapCase(unittest.TestCase):
    """Isolates `cli._GAPS` per test, same reason StateDirCase isolates writes.jsonl
    in test_guard.py: a leftover gap from one test must never leak into the next."""

    def setUp(self):
        cli._GAPS.clear()

    def tearDown(self):
        cli._GAPS.clear()


# ── 1 & 2: every rep field is refused as a write; write/rep never overlap ────
class TestRepFieldsAreWriteRefused(unittest.TestCase):
    def test_write_and_rep_apis_are_disjoint(self):
        write_apis = {api for _l, api, _k, _v in cli.FIELDS["write"]}
        rep_apis = {api for _l, api, _s in cli.FIELDS["rep"]}
        overlap = write_apis & rep_apis
        self.assertEqual(overlap, set(),
                         f"a rep-owned field must never also be SE-writable: {overlap}")

    def test_every_rep_field_is_refused_by_resolve_field(self):
        for label, api, _section in cli.FIELDS["rep"]:
            with self.assertRaises(SystemExit) as cm:
                guard._resolve_field(api)
            self.assertEqual(cm.exception.code, cli.E_REFUSED,
                             f"'{api}' ({label}) must be refused, not silently accepted")
            with self.assertRaises(SystemExit) as cm2:
                guard._resolve_field(label)
            self.assertEqual(cm2.exception.code, cli.E_REFUSED)


# ── 3: static -- no write surface in the source at all ───────────────────────
class TestReadOnlyStatic(unittest.TestCase):
    def test_source_carries_no_write_path(self):
        with open(os.path.join(ROOT, "opp_axi", "rep.py"), encoding="utf-8") as f:
            src = f.read()
        for token in ("sf_patch", "guarded_patch", "PATCH", "import guard", "--method"):
            self.assertNotIn(token, src, f"opp_axi/rep.py must never contain {token!r}")


# ── 4: Next_Steps__c parser ───────────────────────────────────────────────────
class TestParseNextSteps(unittest.TestCase):
    TODAY = date(2026, 9, 14)

    def setUp(self):
        patcher = mock.patch.object(cli, "INITIALS", "CS")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_slash_date_with_year_and_initials(self):
        e = rep.parse_next_steps("9/14/2026 MB Deep Dive went well.",
                                 owner_initials=("MB",), today=self.TODAY)
        self.assertEqual(e, [{"date": "2026-09-14", "by": "MB", "text": "Deep Dive went well."}])

    def test_slash_short_year_dash_separator(self):
        e = rep.parse_next_steps("9/14/26 DF - Followed up on pricing.",
                                 owner_initials=("DF",), today=self.TODAY)
        self.assertEqual(e, [{"date": "2026-09-14", "by": "DF",
                              "text": "Followed up on pricing."}])

    def test_iso_date_colon_no_initials(self):
        e = rep.parse_next_steps("2026-09-14: Speaking with Jamie next week.",
                                 today=self.TODAY)
        self.assertEqual(e, [{"date": "2026-09-14", "by": "",
                              "text": "Speaking with Jamie next week."}])

    def test_dash_date_bracketed_initials(self):
        e = rep.parse_next_steps("9-12-2026 - MN - Customer wants a demo.",
                                 owner_initials=("MN",), today=self.TODAY)
        self.assertEqual(e, [{"date": "2026-09-12", "by": "MN",
                              "text": "Customer wants a demo."}])

    def test_yearless_date_and_mnda_is_not_captured_as_initials(self):
        e = rep.parse_next_steps("8/17 MNDA signed- prepping for kickoff.",
                                 owner_initials=("MB",), today=self.TODAY)
        self.assertEqual(e, [{"date": "2026-08-17", "by": "",
                              "text": "MNDA signed- prepping for kickoff."}])

    def test_yearless_date_far_future_rolls_back_a_year(self):
        e = rep.parse_next_steps("12/20 MB Renewal conversation.",
                                 owner_initials=("MB",), today=date(2026, 1, 5))
        self.assertEqual(e[0]["date"], "2025-12-20")
        self.assertEqual(e[0]["by"], "MB")

    def test_dotted_date_with_year_parses(self):
        e = rep.parse_next_steps("7.27.26 - Mesh - Spoke with Kurt at Blacklake.",
                                 today=self.TODAY)
        self.assertEqual(e, [{"date": "2026-07-27", "by": "",
                              "text": "Mesh - Spoke with Kurt at Blacklake."}])

    def test_dotted_date_full_year_parses(self):
        e = rep.parse_next_steps("7.27.2026 update from partner.", today=self.TODAY)
        self.assertEqual(e[0]["date"], "2026-07-27")

    def test_yearless_dotted_number_is_not_a_date_stays_continuation(self):
        """'1.5 million' must never be misread as a month.day date -- the dotted
        format requires a year, unlike the slash/dash formats."""
        text = "9/14/26 MB First entry.\n1.5 million VMs is the target."
        e = rep.parse_next_steps(text, owner_initials=("MB",), today=self.TODAY)
        self.assertEqual(len(e), 1, "the dotted year-less line must not start a new entry")
        self.assertIn("1.5 million VMs is the target.", e[0]["text"])

    def test_yearless_dotted_number_as_sole_undated_text(self):
        e = rep.parse_next_steps("1.5 million VMs across decentralized sites.",
                                 today=self.TODAY)
        self.assertEqual(e, [{"date": None, "by": "",
                              "text": "1.5 million VMs across decentralized sites."}])

    def test_crlf_line_endings(self):
        text = "9/14/26 MB First.\r\n9/1/26 MB Second.\r\n"
        e = rep.parse_next_steps(text, owner_initials=("MB",), today=self.TODAY)
        self.assertEqual([x["date"] for x in e], ["2026-09-14", "2026-09-01"])

    def test_blank_lines_do_not_create_empty_entries(self):
        text = "9/14/26 MB First.\n\n\n9/1/26 MB Second."
        e = rep.parse_next_steps(text, owner_initials=("MB",), today=self.TODAY)
        self.assertEqual(len(e), 2)

    def test_undated_leading_text_kept_never_dropped(self):
        text = "Overall doing well.\n9/1/26 MB Called customer."
        e = rep.parse_next_steps(text, owner_initials=("MB",), today=self.TODAY)
        self.assertEqual(e[0], {"date": None, "by": "", "text": "Overall doing well."})
        self.assertEqual(e[1]["date"], "2026-09-01")

    def test_shi_partner_name_is_never_an_author(self):
        e = rep.parse_next_steps("7/21/2026 SHI meeting with customer",
                                 owner_initials=("MB",), today=self.TODAY)
        self.assertEqual(e, [{"date": "2026-07-21", "by": "",
                              "text": "SHI meeting with customer"}])

    def test_tesla_inline_runon_splits_into_two_entries(self):
        text = ("9/12/2026 MB Great deep dive with Tesla team on 9/15.  "
               "8/22/2026 MB We have an internal SHI call scheduled.")
        e = rep.parse_next_steps(text, owner_initials=("MB",), today=self.TODAY)
        self.assertEqual(len(e), 2)
        self.assertEqual(e[0]["date"], "2026-09-12")
        self.assertEqual(e[0]["by"], "MB")
        self.assertIn("deep dive with Tesla team on 9/15.", e[0]["text"])
        self.assertEqual(e[1]["date"], "2026-08-22")
        self.assertEqual(e[1]["by"], "MB")
        self.assertIn("SHI call scheduled.", e[1]["text"])

    def test_non_dated_continuation_line_appends_to_previous_entry(self):
        text = "9/14/26 MB First line.\nstill part of the same entry."
        e = rep.parse_next_steps(text, owner_initials=("MB",), today=self.TODAY)
        self.assertEqual(len(e), 1)
        self.assertIn("still part of the same entry.", e[0]["text"])

    def test_empty_text_yields_no_entries(self):
        self.assertEqual(rep.parse_next_steps(""), [])
        self.assertEqual(rep.parse_next_steps(None), [])


# ── 5: Clari call summary extraction ──────────────────────────────────────────
class TestClariSummary(unittest.TestCase):
    def test_extracts_text_after_smart_summary_header(self):
        desc = ("-----------Notes from Clari Copilot---------------\n"
               "Smart Summary (Powered by RevAI) : Discussed  pricing   and next steps.")
        self.assertEqual(rep.clari_summary(desc), "Discussed pricing and next steps.")

    def test_header_without_parenthetical_still_matches(self):
        self.assertEqual(rep.clari_summary("Smart Summary: quick recap here"),
                         "quick recap here")

    def test_passthrough_when_no_header_present(self):
        self.assertEqual(rep.clari_summary("Just a  plain   note."), "Just a plain note.")

    def test_none_description_is_empty_string_not_an_error(self):
        self.assertEqual(rep.clari_summary(None), "")


# ── 6 & 8: cmd_rep detail rendering ───────────────────────────────────────────
class TestCmdRepDetail(GapCase):
    def test_renders_every_section_and_exits_clean_no_write_calls(self):
        buf = io.StringIO()
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=_router()), \
             mock.patch.object(cli, "_run") as m_run, \
             contextlib.redirect_stdout(buf):
            rep.cmd_rep(_ns_rep())
        out = buf.getvalue()
        for name in ("deal", "nextSteps", "meddpicc", "qualification", "narrative",
                     "links", "activity", "notes", "files", "changes", "contacts"):
            self.assertIn(f"{name}[", out, f"missing section block: {name}")
        self.assertIn("source=salesforce (read-only)", out)
        self.assertIn("narrative.empty:", out)
        self.assertIn("links.empty:", out)
        self.assertEqual(cli._GAPS, [], "the happy path must not record a gap")
        m_run.assert_not_called()
        for call in m_run.call_args_list:
            self.assertNotIn("PATCH", " ".join(str(x) for x in call.args))

    def test_narrative_and_links_only_render_populated_rows(self):
        """OPP_REC: narrative has 3 populated fields (Description, Current/Desired
        Technical State) of 9; all 4 link fields are empty."""
        buf = io.StringIO()
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=_router()), \
             contextlib.redirect_stdout(buf):
            rep.cmd_rep(_ns_rep(section=["narrative"]))
        out = buf.getvalue()
        self.assertIn("narrative[3]", out)
        self.assertIn("links[0]", out)
        self.assertIn("links.empty: Executive Summary, Account Plan, "
                      "Mutual Action Plan, Value Hypothesis", out)
        self.assertNotIn("(all populated)", out.split("links.empty:")[0].split("\n")[-1])

    def test_narrative_and_links_json_shape_is_rows_and_empty(self):
        buf = io.StringIO()
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=_router()), \
             contextlib.redirect_stdout(buf):
            rep.cmd_rep(_ns_rep(section=["narrative"], json=True))
        out = json.loads(buf.getvalue())
        self.assertEqual(set(out["narrative"].keys()), {"rows", "empty"})
        self.assertEqual(set(out["links"].keys()), {"rows", "empty"})
        self.assertEqual(len(out["links"]["rows"]), 0)
        self.assertEqual(len(out["links"]["empty"]), 4)

    def test_sibling_open_opps_listed_and_excluded_when_closed(self):
        """tesla/toyota/aunalytics each carry more than one open opp under one slug --
        the resolved opp's siblings must be surfaced (id + name), and a non-open
        sibling must not appear."""
        oid2, oid3 = "006BBBBBBBBBBBBBBB", "006CCCCCCCCCCCCCCC"
        idx2 = {"acme": {"account_name": "Acme Corp", "opportunities": [
            {"id": OID, "primary": True, "status": "open"},
            {"id": oid2, "name": "Acme Corp - Expansion", "status": "open"},
            {"id": oid3, "name": "Acme Corp - Closed Deal", "status": "closed"},
        ]}}
        buf = io.StringIO()
        with mock.patch.object(cli, "opp_index", return_value=idx2), \
             mock.patch.object(cli, "sf_query", side_effect=_router()), \
             contextlib.redirect_stdout(buf):
            rep.cmd_rep(_ns_rep(section=["deal"]))
        out = buf.getvalue()
        expect = (f"also open under acme: {oid2[:15]} Acme Corp - Expansion — "
                 f"opp-axi rep {oid2[:15]}")
        self.assertIn(expect, out)
        self.assertNotIn(oid3[:15], out, "a closed sibling must not be listed")

        buf2 = io.StringIO()
        with mock.patch.object(cli, "opp_index", return_value=idx2), \
             mock.patch.object(cli, "sf_query", side_effect=_router()), \
             contextlib.redirect_stdout(buf2):
            rep.cmd_rep(_ns_rep(section=["deal"], json=True))
        out2 = json.loads(buf2.getvalue())
        self.assertEqual(out2["opp"]["siblings"],
                         [{"id": oid2[:15], "name": "Acme Corp - Expansion"}])

    def test_no_siblings_when_only_one_open_opp(self):
        buf = io.StringIO()
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=_router()), \
             contextlib.redirect_stdout(buf):
            rep.cmd_rep(_ns_rep(section=["deal"]))
        self.assertNotIn("also open under", buf.getvalue())

    def test_next_hints_use_the_opp_id_not_the_slug(self):
        """`rep <slug>` resolves to the primary opp. On a multi-opp account a slug hint
        would silently switch a caller who came in by id to a different opp."""
        idx2 = {"acme": {"account_name": "Acme Corp", "opportunities": [
            {"id": OID, "primary": True, "status": "open"},
            {"id": "006BBBBBBBBBBBBBBB", "name": "Acme Corp - Expansion", "status": "open"},
        ]}}
        buf = io.StringIO()
        with mock.patch.object(cli, "opp_index", return_value=idx2), \
             mock.patch.object(cli, "sf_query", side_effect=_router()), \
             contextlib.redirect_stdout(buf):
            rep.cmd_rep(_ns_rep(section=["deal"]))
        hint = next(ln for ln in buf.getvalue().splitlines() if ln.startswith("next:"))
        self.assertIn(f"opp-axi rep {OID[:15]} --full", hint)
        self.assertIn(f"opp-axi rep {OID[:15]} --since ", hint)
        self.assertNotIn("opp-axi rep acme", hint)

    def test_meddpicc_status_companion_is_populated(self):
        """Regression: the SOQL field list must select the *_Status__c companion
        columns too, or `status` always reads back empty even when Salesforce has it."""
        buf = io.StringIO()
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=_router()), \
             contextlib.redirect_stdout(buf):
            rep.cmd_rep(_ns_rep(section=["meddpicc"]))
        self.assertIn("Identified", buf.getvalue())   # Economic_Buyer_Status__c value

    def test_section_nextsteps_issues_only_the_opportunity_query(self):
        calls = []

        def fake_query(soql, timeout=180):
            calls.append(soql)
            return [dict(OPP_REC)]

        buf = io.StringIO()
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             contextlib.redirect_stdout(buf):
            rep.cmd_rep(_ns_rep(section=["nextSteps"]))
        self.assertEqual(len(calls), 1)
        self.assertIn("FROM Opportunity WHERE Id", calls[0])

    def test_full_notes_with_failing_body_fetch_records_gap_never_blank(self):
        class FakeProc:
            returncode = 1
            stdout = ""
            stderr = "unauthorized"

        buf = io.StringIO()
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=_router()), \
             mock.patch.object(cli, "_run", return_value=FakeProc()), \
             contextlib.redirect_stdout(buf):
            rep.cmd_rep(_ns_rep(section=["notes"], full=True))
        out = buf.getvalue()
        self.assertIn("UNAVAILABLE", out)
        self.assertTrue(cli._GAPS, "a failed note-body fetch must record a gap")

    def test_not_found_dies_e_notfound(self):
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", return_value=[]):
            with self.assertRaises(SystemExit) as cm:
                rep.cmd_rep(_ns_rep())
            self.assertEqual(cm.exception.code, cli.E_NOTFOUND)


# ── 10: --json shape ──────────────────────────────────────────────────────────
class TestCmdRepJson(GapCase):
    def test_json_keys_match_selected_sections_with_iso_dates(self):
        buf = io.StringIO()
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=_router()), \
             contextlib.redirect_stdout(buf):
            rep.cmd_rep(_ns_rep(section=["deal", "nextSteps"], json=True))
        out = json.loads(buf.getvalue())
        self.assertIn("opp", out)
        self.assertIn("deal", out)
        self.assertIn("nextSteps", out)
        self.assertIn("gaps", out)
        for extra in ("meddpicc", "qualification", "narrative", "activity", "notes",
                     "files", "changes", "contacts"):
            self.assertNotIn(extra, out, f"unselected section '{extra}' leaked into --json")
        self.assertRegex(out["deal"]["close"], r"^\d{4}-\d{2}-\d{2}$")
        for e in out["nextSteps"]["entries"]:
            if e["date"] is not None:
                self.assertRegex(e["date"], r"^\d{4}-\d{2}-\d{2}$")


# ── 9: rollup ──────────────────────────────────────────────────────────────────
class TestRollup(GapCase):
    def test_renders_rows_and_footer(self):
        recs = [{"Id": OID, "Name": "Acme", "Owner": {"Name": "Matthew Byram"},
                "StageName": "Prove Value", "Forecast_Status__c": "On Track",
                "Next_Steps_Last_Updated__c": "2026-09-10T00:00:00.000+0000",
                "Next_Steps__c": "9/10/26 MB Checked in."}]
        buf = io.StringIO()
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", return_value=recs), \
             contextlib.redirect_stdout(buf):
            rep.cmd_rep(_ns_rep(ref=None))
        out = buf.getvalue()
        self.assertIn("reps[1]", out)
        self.assertIn(OID[:15], out)   # id column -- a slug alone can be ambiguous
        self.assertIn("acme", out)
        self.assertIn("open:1", out)
        self.assertIn("opp-axi rep <id>", out)

    def test_zero_records_renders_definitive_none(self):
        buf = io.StringIO()
        with mock.patch.object(cli, "opp_index", return_value={}), \
             mock.patch.object(cli, "sf_query", return_value=[]), \
             contextlib.redirect_stdout(buf):
            rep.cmd_rep(_ns_rep(ref=None))
        self.assertIn(
            "reps[0]{id,slug,owner,stage,forecast,updated,daysStale,latest}: (none)",
            buf.getvalue())

    def test_json_rows_carry_id(self):
        recs = [{"Id": OID, "Name": "Acme", "Owner": {"Name": "Matthew Byram"},
                "StageName": "Prove Value", "Forecast_Status__c": "On Track",
                "Next_Steps_Last_Updated__c": "2026-09-10T00:00:00.000+0000",
                "Next_Steps__c": "9/10/26 MB Checked in."}]
        buf = io.StringIO()
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", return_value=recs), \
             contextlib.redirect_stdout(buf):
            rep.cmd_rep(_ns_rep(ref=None, json=True))
        out = json.loads(buf.getvalue())
        self.assertEqual(out["reps"][0]["id"], OID[:15])


# ── 11: fields rep ────────────────────────────────────────────────────────────
class TestFieldsRep(unittest.TestCase):
    def test_fields_rep_lists_repreadonly_and_the_rule(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_fields(argparse.Namespace(section="rep"))
        out = buf.getvalue()
        self.assertIn("repReadOnly[", out)
        self.assertIn("read-only: opp-axi reads rep fields, never writes them", out)

    def test_fields_rep_alone_omits_the_se_write_rules_line(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_fields(argparse.Namespace(section="rep"))
        self.assertNotIn("REST PATCH only", buf.getvalue())

    def test_fields_bare_still_shows_write_rules_line(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_fields(argparse.Namespace(section=None))
        self.assertIn("REST PATCH only", buf.getvalue())

    def test_fields_write_shows_write_rules_line(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_fields(argparse.Namespace(section="write"))
        self.assertIn("REST PATCH only", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
