"""The write guard: every Salesforce write goes through guard.guarded_patch(), which
locks the opp, compare-and-swaps against what the caller last read, and audits BEFORE
and AFTER the PATCH. These tests pin the behaviour that used to be missing entirely:
two sessions racing the same field used to lose one write with no trace it happened.

All fixture-based. `cli.sf_query` and `guard.sf_patch` are monkeypatched to a tiny
in-memory stand-in for Salesforce (`_fake_sf` below) so a CAS re-read and a read-back
see real, mutating state instead of a brittle, call-order-dependent side_effect list.
The `sf` CLI is never invoked.
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from opp_axi import __version__      # noqa: E402
from opp_axi import cli, guard       # noqa: E402

OID = "006AAAAAAAAAAAAAAA"
IDX = {"acme": {"account_name": "Acme Corp",
                "opportunities": [{"id": OID, "primary": True, "status": "open"}]}}


def _fake_sf(record, owner="CraigSmith"):
    """A minimal stand-in for one Opportunity record: `sf_query` always returns its
    current state, `sf_patch` mutates it in place. Lets a test watch a CAS re-read
    and a read-back see the SAME state a real org would, instead of guessing how
    many times sf_query gets called and in what order."""
    record.setdefault("Id", OID)
    record.setdefault("SA_Assignment_Oppty__c", owner)

    def fake_query(soql, timeout=180):
        return [dict(record)]

    def fake_patch(opp_id, body):
        record.update(body)

    return fake_query, fake_patch


def _ns_activity(**kw):
    base = dict(ref="acme", add="did a thing", amend=False, allow=[], dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _ns_field(fields=("Tech_Risk_Status__c=High",), **kw):
    base = dict(ref="acme", fields=list(fields), if_empty=False, reason=None, dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


class StateDirCase(unittest.TestCase):
    """Isolates every test's audit log + locks under its own OPP_AXI_STATE, so a run
    never reads or pollutes the real ~/.local/state/opp-axi/."""

    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"OPP_AXI_STATE": self.state_dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.state_dir, ignore_errors=True)


class TestVersionBump(unittest.TestCase):
    def test_version_is_0_1_15(self):
        # This test exists so a version bump is a decision rather than a side effect.
        # 0.1.11 was the multipicklist set-comparison fix (#15). Ungating the
        # customer-artifact signal needs its own bump, because the box runs a BUILT
        # zipapp at ~/.local/bin/opp-axi -- merging alone changes nothing there until
        # `make install` copies a new one over: 0.1.15.
        self.assertEqual(__version__, "0.1.15")


class TestStateDir(StateDirCase):
    def test_opp_axi_state_override_wins(self):
        self.assertEqual(guard.state_dir(), self.state_dir)

    def test_xdg_state_home_used_when_no_override(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdg-test"}, clear=False):
            del os.environ["OPP_AXI_STATE"]
            self.assertEqual(guard.state_dir(), "/tmp/xdg-test/opp-axi")


class TestGuardedPatch(StateDirCase):
    def test_cas_abort_on_changed_field_no_patch_called(self):
        """The field moved between the caller's read and the write: refuse loudly,
        touch nothing."""
        with mock.patch.object(cli, "sf_query",
                               return_value=[{"Sales_Engineer_Overview__c": "CHANGED"}]), \
             mock.patch.object(guard, "sf_patch") as m_patch:
            with self.assertRaises(SystemExit) as cm:
                guard.guarded_patch(OID, "acme", "Sales_Engineer_Overview__c",
                                    "new text", "OLD TEXT", reason="test")
            self.assertEqual(cm.exception.code, cli.E_REFUSED)
        m_patch.assert_not_called()
        self.assertEqual(guard._load_writes(), [], "a CAS abort must not write an audit line")

    def test_audit_pending_then_verified_with_full_old_new(self):
        record = {"Sales_Engineer_Overview__c": "OLD VALUE"}
        fake_query, fake_patch = _fake_sf(record)
        with mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch) as m_patch:
            rec = guard.guarded_patch(OID, "acme", "Sales_Engineer_Overview__c",
                                      "NEW VALUE", "OLD VALUE", reason="test",
                                      allow=["internal-pricing"])
        m_patch.assert_called_once_with(OID, {"Sales_Engineer_Overview__c": "NEW VALUE"})
        self.assertEqual(rec["status"], "verified")

        lines = guard._load_writes()
        self.assertEqual(len(lines), 2, "one pending line, one outcome line")
        pending, verified = lines
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(pending["id"], verified["id"])
        for line in (pending, verified):
            self.assertEqual(line["old"], "OLD VALUE")
            self.assertEqual(line["new"], "NEW VALUE")
            self.assertEqual(line["opp"], OID)
            self.assertEqual(line["slug"], "acme")
            self.assertEqual(line["field"], "Sales_Engineer_Overview__c")
            self.assertEqual(line["reason"], "test")
            self.assertEqual(line["allow"], ["internal-pricing"])
        self.assertRegex(pending["id"], r"^w-\d{8}T\d{6}Z-[0-9a-f]{4}$")

    def test_readback_mismatch_is_recorded_not_raised(self):
        record = {"Sales_Engineer_Overview__c": "OLD"}
        fake_query, _ = _fake_sf(record)

        def fake_patch_wrong(opp_id, body):
            record["Sales_Engineer_Overview__c"] = "SOMETHING ELSE ENTIRELY"

        with mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch_wrong):
            rec = guard.guarded_patch(OID, "acme", "Sales_Engineer_Overview__c",
                                      "NEW", "OLD", reason="test")
        self.assertEqual(rec["status"], "mismatch")
        self.assertEqual(guard._load_writes()[-1]["status"], "mismatch")

    def test_readback_crlf_and_trimmed_trailing_whitespace_still_verifies(self):
        """Salesforce round-trips \\n as \\r\\n and trims trailing whitespace on
        save. Without _same(), that reads back as a false `mismatch` on every
        single write."""
        record = {"Sales_Engineer_Overview__c": "OLD"}
        fake_query, _ = _fake_sf(record)

        def fake_patch_sf_style(opp_id, body):
            sent = body["Sales_Engineer_Overview__c"]
            record["Sales_Engineer_Overview__c"] = sent.replace("\n", "\r\n").rstrip()

        with mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch_sf_style):
            rec = guard.guarded_patch(OID, "acme", "Sales_Engineer_Overview__c",
                                      "line one\nline two  \n", "OLD", reason="test")
        self.assertEqual(rec["status"], "verified")
        self.assertNotEqual(record["Sales_Engineer_Overview__c"], "line one\nline two  \n",
                            "the fake backend must actually normalize, or this proves nothing")

    def test_readback_reordered_multipicklist_verifies(self):
        """Salesforce stores a multipicklist in picklist-DEFINITION order, not send
        order. w-20260915T061536Z-3988 wrote 'Product complexity;Missing features'
        to Tech_Risk_Rational__c (a declared multipicklist) and Salesforce held
        'Missing features;Product complexity' — same members, different order —
        and that used to read back as a false `mismatch`."""
        record = {"Tech_Risk_Rational__c": "OLD"}
        fake_query, _ = _fake_sf(record)

        def fake_patch_reorders(opp_id, body):
            record["Tech_Risk_Rational__c"] = "Missing features;Product complexity"

        with mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch_reorders):
            rec = guard.guarded_patch(OID, "acme", "Tech_Risk_Rational__c",
                                      "Product complexity;Missing features", "OLD",
                                      reason="test")
        self.assertEqual(rec["status"], "verified")
        self.assertNotEqual(record["Tech_Risk_Rational__c"], rec["new"],
                            "the fake backend must actually reorder, or this proves nothing")

    def test_readback_multipicklist_different_set_is_still_mismatch(self):
        """A genuinely different multipicklist value — not just reordered — must
        still be flagged. Set-comparison must not become 'anything with a
        semicolon matches'."""
        record = {"Tech_Risk_Rational__c": "OLD"}
        fake_query, _ = _fake_sf(record)

        def fake_patch_wrong_set(opp_id, body):
            record["Tech_Risk_Rational__c"] = "Missing features;Complex licensing"

        with mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch_wrong_set):
            rec = guard.guarded_patch(OID, "acme", "Tech_Risk_Rational__c",
                                      "Product complexity;Missing features", "OLD",
                                      reason="test")
        self.assertEqual(rec["status"], "mismatch")

    def test_readback_reordered_semicolons_on_non_multipicklist_field_still_mismatch(self):
        """Only a field declared `multipicklist` in cli.FIELDS["write"] gets set
        comparison. A plain text field that happens to contain ';' (not declared
        multipicklist) must still compare as an exact string, or a genuine mismatch
        on a text field would be silently forgiven."""
        record = {"Sales_Engineer_Overview__c": "OLD"}
        fake_query, _ = _fake_sf(record)

        def fake_patch_reorders(opp_id, body):
            record["Sales_Engineer_Overview__c"] = "b;a"

        with mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch_reorders):
            rec = guard.guarded_patch(OID, "acme", "Sales_Engineer_Overview__c",
                                      "a;b", "OLD", reason="test")
        self.assertEqual(rec["status"], "mismatch")

    def test_patch_failure_appends_failed_line_and_reraises(self):
        """sf_patch dies via cli.die() -> SystemExit. A pending line with no
        outcome would claim a write is still in flight when it never happened."""
        record = {"Sales_Engineer_Overview__c": "OLD"}
        fake_query, _ = _fake_sf(record)

        def fake_patch_dies(opp_id, body):
            cli.die("PATCH failed: simulated 500", cli.E_ERR)

        with mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch_dies):
            with self.assertRaises(SystemExit) as cm:
                guard.guarded_patch(OID, "acme", "Sales_Engineer_Overview__c",
                                    "NEW", "OLD", reason="test")
            self.assertEqual(cm.exception.code, cli.E_ERR)

        lines = guard._load_writes()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["status"], "pending")
        self.assertEqual(lines[1]["status"], "failed")
        self.assertEqual(lines[0]["id"], lines[1]["id"])

    def test_dry_run_writes_nothing(self):
        record = {"Sales_Engineer_Overview__c": "OLD"}
        fake_query, fake_patch = _fake_sf(record)
        with mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch) as m_patch:
            rec = guard.guarded_patch(OID, "acme", "Sales_Engineer_Overview__c",
                                      "NEW", "OLD", reason="test", dry_run=True)
        m_patch.assert_not_called()
        self.assertTrue(rec["dry_run"])
        self.assertEqual(rec["old"], "OLD")
        self.assertEqual(rec["new"], "NEW")
        self.assertEqual(guard._load_writes(), [])

    def test_state_files_have_tight_permissions(self):
        record = {"Sales_Engineer_Overview__c": "OLD"}
        fake_query, fake_patch = _fake_sf(record)
        with mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch):
            guard.guarded_patch(OID, "acme", "Sales_Engineer_Overview__c",
                               "NEW", "OLD", reason="test")
        writes_path = os.path.join(self.state_dir, "writes.jsonl")
        locks_dir = os.path.join(self.state_dir, "locks")
        self.assertEqual(os.stat(writes_path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(locks_dir).st_mode & 0o777, 0o700)


class TestAmendTopEntry(unittest.TestCase):
    def test_replaces_up_to_the_next_dated_line(self):
        existing = ("9/14/26 CS: today's first take\n"
                    "still part of today's entry\n"
                    "9/1/26 CS: older, untouched")
        out = guard.amend_top_entry(existing, "9/14/26 CS: replaced")
        self.assertEqual(out, "9/14/26 CS: replaced\n9/1/26 CS: older, untouched")

    def test_no_second_dated_line_replaces_everything(self):
        existing = "9/14/26 CS: only entry\nsecond line, still today's"
        out = guard.amend_top_entry(existing, "9/14/26 CS: replaced")
        self.assertEqual(out, "9/14/26 CS: replaced")


class TestSame(unittest.TestCase):
    """The value-equality helper every comparison in guard.py routes through."""

    def test_none_and_empty_string_are_equal(self):
        self.assertTrue(guard._same(None, ""))
        self.assertTrue(guard._same("", None))
        self.assertTrue(guard._same(None, None))

    def test_crlf_normalized_before_comparing(self):
        self.assertTrue(guard._same("a\r\nb", "a\nb"))

    def test_trailing_whitespace_ignored(self):
        self.assertTrue(guard._same("a\nb  \n", "a\nb"))

    def test_genuinely_different_text_is_not_equal(self):
        self.assertFalse(guard._same("a\nb", "a\nc"))

    def test_non_string_values_compare_by_equality(self):
        self.assertTrue(guard._same(True, True))
        self.assertFalse(guard._same(True, False))
        self.assertFalse(guard._same(False, ""))   # False is a fact, not "empty"

    def test_multipicklist_field_compares_members_as_a_set(self):
        self.assertTrue(guard._same("Product complexity;Missing features",
                                    "Missing features;Product complexity",
                                    field="Tech_Risk_Rational__c"))
        self.assertTrue(guard._same("AWS;Azure", "Azure ; AWS",
                                    field="Secondary_Environment_s__c"),
                        "whitespace around a member must be stripped too")

    def test_multipicklist_field_still_catches_a_real_mismatch(self):
        self.assertFalse(guard._same("Product complexity;Missing features",
                                     "Missing features;Complex licensing",
                                     field="Tech_Risk_Rational__c"))

    def test_no_field_or_non_multipicklist_field_compares_literally(self):
        # No declared type at all: never guessed into a multipicklist just
        # because the string contains ';'.
        self.assertFalse(guard._same("a;b", "b;a"))
        # A declared type that is NOT multipicklist: still literal.
        self.assertFalse(guard._same("a;b", "b;a", field="Sales_Engineer_Overview__c"))
        # Unknown field name: no type info available, so literal too.
        self.assertFalse(guard._same("a;b", "b;a", field="Not_A_Real_Field__c"))

    def test_is_multipicklist(self):
        self.assertTrue(guard._is_multipicklist("Tech_Risk_Rational__c"))
        self.assertTrue(guard._is_multipicklist("Secondary_Environment_s__c"))
        self.assertFalse(guard._is_multipicklist("Sales_Engineer_Overview__c"))
        self.assertFalse(guard._is_multipicklist("Not_A_Real_Field__c"))


class TestLint(unittest.TestCase):
    def test_correction_framing_fires(self):
        self.assertIn("correction-framing", guard.lint("correcting my earlier note"))
        self.assertIn("correction-framing", guard.lint("one thing that genuinely landed"))

    def test_process_critique_fires_on_first_person_or_plan_phrasing(self):
        self.assertIn("process-critique", guard.lint("We never sent the follow-up deck."))
        self.assertIn("process-critique", guard.lint("We haven't followed up since the demo."))
        self.assertIn("process-critique",
                      guard.lint("Not following our process for POV signoff."))
        self.assertIn("process-critique",
                      guard.lint("The process was never documented for this account."))

    def test_process_critique_does_not_fire_on_ordinary_customer_facts(self):
        """A fact about the CUSTOMER's own progress is not process critique, even
        though it contains 'not sent' / 'not followed'."""
        self.assertEqual(guard.lint("Customer has not sent the RVTools export yet."), [])
        self.assertEqual(guard.lint("Not followed up by the customer since 9/2."), [])

    def test_internal_pricing_fires(self):
        self.assertIn("internal-pricing", guard.lint("gave them a $5000 discount"))

    def test_internal_roadmap_fires(self):
        self.assertIn("internal-roadmap", guard.lint("this is on our roadmap for Q3"))
        self.assertIn("internal-roadmap", guard.lint("tracked as PE-1234"))

    def test_internal_competitive_fires_on_positioning_language(self):
        self.assertIn("internal-competitive", guard.lint("Positioned to displace Rancher"))
        self.assertIn("internal-competitive",
                      guard.lint("Building the battlecard for this deal"))
        self.assertIn("internal-competitive",
                      guard.lint("Discussed our competitive takeout plan"))

    def test_internal_competitive_does_not_fire_on_customer_platform_facts(self):
        """The customer's current platform is a fact for Salesforce, not internal
        battle-plan language -- Secondary_Environment_s__c even lists 'Nutanix
        AHV' as a valid value."""
        self.assertEqual(guard.lint("Customer runs OpenShift 4.14 and Nutanix AHV today."), [])

    def test_clean_text_lints_clean(self):
        self.assertEqual(guard.lint("met with the platform team, demoed live migration"), [])

    def test_allow_overrides_the_rule(self):
        text = "correcting my earlier note"
        self.assertIn("correction-framing", guard.lint(text))
        self.assertEqual(guard.lint(text, allow=["correction-framing"]), [])


class TestCmdActivity(StateDirCase):
    def test_same_day_dedupe_refused_without_amend(self):
        stamp = datetime.now().strftime("%-m/%-d/%y")
        record = {"Sales_Engineer_Overview__c":
                  f"{stamp} CS: already wrote today\n9/1/26 CS: older"}
        fake_query, fake_patch = _fake_sf(record)
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch) as m_patch:
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_activity(_ns_activity())
            self.assertEqual(cm.exception.code, cli.E_REFUSED)
        m_patch.assert_not_called()

    def test_amend_replaces_only_todays_entry(self):
        stamp = datetime.now().strftime("%-m/%-d/%y")
        record = {"Sales_Engineer_Overview__c":
                  f"{stamp} CS: already wrote today\n9/1/26 CS: older, untouched"}
        fake_query, fake_patch = _fake_sf(record)
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch):
            cli.cmd_activity(_ns_activity(add="replacement text", amend=True))
        new_val = record["Sales_Engineer_Overview__c"]
        self.assertIn("replacement text", new_val)
        self.assertIn("9/1/26 CS: older, untouched", new_val)
        self.assertNotIn("already wrote today", new_val)

    def test_lint_blocks_without_allow(self):
        record = {"Sales_Engineer_Overview__c": ""}
        fake_query, fake_patch = _fake_sf(record)
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch):
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_activity(_ns_activity(add="correcting my earlier note"))
            self.assertEqual(cm.exception.code, cli.E_REFUSED)
        self.assertEqual(record["Sales_Engineer_Overview__c"], "",
                         "a lint-blocked write must never reach Salesforce")

    def test_allow_overrides_lint_and_is_recorded_in_the_audit_line(self):
        record = {"Sales_Engineer_Overview__c": ""}
        fake_query, fake_patch = _fake_sf(record)
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch):
            cli.cmd_activity(_ns_activity(add="correcting my earlier note",
                                          allow=["correction-framing"]))
        self.assertIn("correcting my earlier note", record["Sales_Engineer_Overview__c"])
        self.assertEqual(guard._load_writes()[-1]["allow"], ["correction-framing"])

    def test_dry_run_activity_writes_nothing(self):
        record = {"Sales_Engineer_Overview__c": "OLD"}
        fake_query, fake_patch = _fake_sf(record)
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch) as m_patch:
            cli.cmd_activity(_ns_activity(dry_run=True))
        m_patch.assert_not_called()
        self.assertEqual(record["Sales_Engineer_Overview__c"], "OLD")
        self.assertEqual(guard._load_writes(), [])

    def test_secondary_se_still_refused_through_the_guard(self):
        """Same refusal cmd_activity always had -- pinned again post-rewrite."""
        record = {"Sales_Engineer_Overview__c": "x"}
        fake_query, fake_patch = _fake_sf(record, owner="SomeoneElse")
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch) as m_patch:
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_activity(_ns_activity())
            self.assertEqual(cm.exception.code, cli.E_REFUSED)
        m_patch.assert_not_called()


class TestCmdField(StateDirCase):
    def test_picklist_rejects_non_emoji_value(self):
        """The exact case in the spec: Tech_Risk_Status__c only accepts the emoji
        literals, so a plain 'High' is a usage error caught before any SF call."""
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query") as m_query, \
             mock.patch.object(guard, "sf_patch") as m_patch:
            with self.assertRaises(SystemExit) as cm:
                guard.cmd_field(_ns_field(fields=["Tech_Risk_Status__c=High"]))
            self.assertEqual(cm.exception.code, cli.E_USAGE)
        m_query.assert_not_called()
        m_patch.assert_not_called()

    def test_picklist_accepts_the_emoji_value(self):
        record = {"Tech_Risk_Status__c": None}
        fake_query, fake_patch = _fake_sf(record)
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch):
            guard.cmd_field(_ns_field(fields=["Tech_Risk_Status__c=\U0001f534 High"]))
        self.assertEqual(record["Tech_Risk_Status__c"], "\U0001f534 High")

    def test_boolean_field_coerced(self):
        record = {"POV_Pass__c": False}
        fake_query, fake_patch = _fake_sf(record)
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch):
            guard.cmd_field(_ns_field(fields=["POV_Pass__c=true"]))
        self.assertIs(record["POV_Pass__c"], True)

    def test_bad_date_rejected(self):
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query") as m_query:
            with self.assertRaises(SystemExit) as cm:
                guard.cmd_field(_ns_field(fields=["Hands_on_Eval_EstStartDate__c=not-a-date"]))
            self.assertEqual(cm.exception.code, cli.E_USAGE)
        m_query.assert_not_called()

    def test_if_empty_skips_when_already_populated(self):
        record = {"Hands_on_Eval_POV_URL__c": "https://existing"}
        fake_query, fake_patch = _fake_sf(record)
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch) as m_patch:
            guard.cmd_field(_ns_field(fields=["Hands_on_Eval_POV_URL__c=https://new"],
                                     if_empty=True))
        m_patch.assert_not_called()
        self.assertEqual(record["Hands_on_Eval_POV_URL__c"], "https://existing")

    def test_if_empty_writes_when_currently_empty(self):
        record = {"Hands_on_Eval_POV_URL__c": ""}
        fake_query, fake_patch = _fake_sf(record)
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch):
            guard.cmd_field(_ns_field(fields=["Hands_on_Eval_POV_URL__c=https://new"],
                                     if_empty=True))
        self.assertEqual(record["Hands_on_Eval_POV_URL__c"], "https://new")

    def test_secondary_se_refused(self):
        record = {"Tech_Risk_Status__c": None}
        fake_query, fake_patch = _fake_sf(record, owner="SomeoneElse")
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch) as m_patch:
            with self.assertRaises(SystemExit) as cm:
                guard.cmd_field(_ns_field(fields=["Tech_Risk_Status__c=\U0001f534 High"]))
            self.assertEqual(cm.exception.code, cli.E_REFUSED)
        m_patch.assert_not_called()

    def test_never_owned_field_refused(self):
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query") as m_query:
            with self.assertRaises(SystemExit) as cm:
                guard.cmd_field(_ns_field(fields=["Amount=500000"]))
            self.assertEqual(cm.exception.code, cli.E_REFUSED)
        m_query.assert_not_called()

    def test_dead_field_refused(self):
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query") as m_query:
            with self.assertRaises(SystemExit) as cm:
                guard.cmd_field(_ns_field(fields=["SE_Activity__c=x"]))
            self.assertEqual(cm.exception.code, cli.E_REFUSED)
        m_query.assert_not_called()

    def test_unknown_field_is_a_usage_error(self):
        with mock.patch.object(cli, "opp_index", return_value=IDX):
            with self.assertRaises(SystemExit) as cm:
                guard.cmd_field(_ns_field(fields=["Not_A_Real_Field__c=x"]))
            self.assertEqual(cm.exception.code, cli.E_USAGE)

    def test_dry_run_field_writes_nothing(self):
        record = {"Tech_Risk_Status__c": None}
        fake_query, fake_patch = _fake_sf(record)
        with mock.patch.object(cli, "opp_index", return_value=IDX), \
             mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch) as m_patch:
            guard.cmd_field(_ns_field(fields=["Tech_Risk_Status__c=\U0001f534 High"],
                                     dry_run=True))
        m_patch.assert_not_called()
        self.assertIsNone(record["Tech_Risk_Status__c"])
        self.assertEqual(guard._load_writes(), [])


class TestCmdUndo(StateDirCase):
    def _ns(self, write_id, dry_run=False):
        return argparse.Namespace(write_id=write_id, dry_run=dry_run)

    def test_undo_success(self):
        record = {"Sales_Engineer_Overview__c": "OLD"}
        fake_query, fake_patch = _fake_sf(record)
        with mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch):
            rec = guard.guarded_patch(OID, "acme", "Sales_Engineer_Overview__c",
                                      "NEW", "OLD", reason="test")
            self.assertEqual(record["Sales_Engineer_Overview__c"], "NEW")
            guard.cmd_undo(self._ns(rec["id"]))
        self.assertEqual(record["Sales_Engineer_Overview__c"], "OLD")
        undo_line = guard._load_writes()[-1]
        self.assertEqual(undo_line["reason"], f"undo {rec['id']}")
        self.assertEqual(undo_line["status"], "verified")

    def test_undo_refused_after_a_later_change(self):
        record = {"Sales_Engineer_Overview__c": "OLD"}
        fake_query, fake_patch = _fake_sf(record)
        with mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch):
            rec = guard.guarded_patch(OID, "acme", "Sales_Engineer_Overview__c",
                                      "NEW", "OLD", reason="test")
            record["Sales_Engineer_Overview__c"] = "SOMETHING ELSE ENTIRELY"
            with self.assertRaises(SystemExit) as cm:
                guard.cmd_undo(self._ns(rec["id"]))
            self.assertEqual(cm.exception.code, cli.E_REFUSED)
        self.assertEqual(record["Sales_Engineer_Overview__c"], "SOMETHING ELSE ENTIRELY")

    def test_undo_unknown_id_not_found(self):
        with self.assertRaises(SystemExit) as cm:
            guard.cmd_undo(self._ns("w-does-not-exist"))
        self.assertEqual(cm.exception.code, cli.E_NOTFOUND)

    def test_undo_proceeds_despite_sf_crlf_normalization(self):
        """The field's live value differs from the recorded `new` only by CRLF +
        trimmed trailing whitespace -- exactly what Salesforce does on save.
        Without _same(), undo would refuse FOREVER because a raw compare never
        matches again."""
        record = {"Sales_Engineer_Overview__c": "OLD"}

        def fake_query(soql, timeout=180):
            return [dict(record)]

        def fake_patch_sf_style(opp_id, body):
            sent = body["Sales_Engineer_Overview__c"]
            record["Sales_Engineer_Overview__c"] = sent.replace("\n", "\r\n").rstrip()

        with mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch_sf_style):
            rec = guard.guarded_patch(OID, "acme", "Sales_Engineer_Overview__c",
                                      "new text\n", "OLD", reason="test")
            self.assertEqual(rec["status"], "verified")
            self.assertNotEqual(record["Sales_Engineer_Overview__c"], rec["new"],
                                "the live value must genuinely differ byte-for-byte "
                                "from the recorded `new`, or this proves nothing")
            guard.cmd_undo(self._ns(rec["id"]))
        self.assertEqual(record["Sales_Engineer_Overview__c"], "OLD")

    def test_undo_proceeds_despite_multipicklist_reorder(self):
        """The repro this task fixes: w-20260915T061536Z-3988 wrote 'Product
        complexity;Missing features' to Tech_Risk_Rational__c and Salesforce held
        'Missing features;Product complexity'. That used to mark the write
        `mismatch` at write time, so `undo` refused it outright ("is 'mismatch',
        not verified") -- never even reaching the current-value comparison this
        test targets."""
        record = {"Tech_Risk_Rational__c": "OLD"}

        def fake_query(soql, timeout=180):
            return [dict(record)]

        def fake_patch_reorders(opp_id, body):
            """Models Salesforce's picklist-definition ordering for exactly the two
            members this write sends; any other value (e.g. undo restoring "OLD")
            is stored as sent, same as a real single-value save would be."""
            sent = body["Tech_Risk_Rational__c"]
            members = {part.strip() for part in sent.split(";") if part.strip()}
            if members == {"Product complexity", "Missing features"}:
                record["Tech_Risk_Rational__c"] = "Missing features;Product complexity"
            else:
                record["Tech_Risk_Rational__c"] = sent

        with mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch_reorders):
            rec = guard.guarded_patch(OID, "acme", "Tech_Risk_Rational__c",
                                      "Product complexity;Missing features", "OLD",
                                      reason="test")
            self.assertEqual(rec["status"], "verified")
            guard.cmd_undo(self._ns(rec["id"]))
        self.assertEqual(record["Tech_Risk_Rational__c"], "OLD")

    def test_undo_refuses_a_non_verified_write(self):
        with mock.patch.object(cli, "sf_query",
                               return_value=[{"Sales_Engineer_Overview__c": "CHANGED"}]):
            with self.assertRaises(SystemExit):
                guard.guarded_patch(OID, "acme", "Sales_Engineer_Overview__c",
                                    "NEW", "OLD", reason="test")   # CAS-aborts, no write
        with self.assertRaises(SystemExit) as cm:
            guard.cmd_undo(self._ns("w-anything"))
        self.assertEqual(cm.exception.code, cli.E_NOTFOUND)


class TestCmdWrites(StateDirCase):
    def test_writes_lists_latest_status_per_id(self):
        record = {"Sales_Engineer_Overview__c": "OLD"}
        fake_query, fake_patch = _fake_sf(record)
        with mock.patch.object(cli, "sf_query", side_effect=fake_query), \
             mock.patch.object(guard, "sf_patch", side_effect=fake_patch):
            rec = guard.guarded_patch(OID, "acme", "Sales_Engineer_Overview__c",
                                      "9/14/26 CS: hi\nOLD", "OLD", reason="test")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            guard.cmd_writes(argparse.Namespace(since=None, opp=None, json=True))
        rows = json.loads(buf.getvalue())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], rec["id"])
        self.assertEqual(rows[0]["slug"], "acme")
        self.assertEqual(rows[0]["status"], "verified")
        self.assertEqual(rows[0]["new"], "9/14/26 CS: hi")   # first line only

    def test_writes_filters_by_opp(self):
        rec_a = {"Sales_Engineer_Overview__c": "OLD"}
        fq_a, fp_a = _fake_sf(rec_a)
        with mock.patch.object(cli, "sf_query", side_effect=fq_a), \
             mock.patch.object(guard, "sf_patch", side_effect=fp_a):
            guard.guarded_patch(OID, "acme", "Sales_Engineer_Overview__c", "NEW", "OLD",
                               reason="test")
        rec_b = {"Sales_Engineer_Overview__c": "OLD"}
        fq_b, fp_b = _fake_sf(rec_b)
        with mock.patch.object(cli, "sf_query", side_effect=fq_b), \
             mock.patch.object(guard, "sf_patch", side_effect=fp_b):
            guard.guarded_patch("006BBBBBBBBBBBBBBB", "beta", "Sales_Engineer_Overview__c",
                               "NEW", "OLD", reason="test")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            guard.cmd_writes(argparse.Namespace(since=None, opp="beta", json=True))
        rows = json.loads(buf.getvalue())
        self.assertEqual([r["slug"] for r in rows], ["beta"])

    def test_since_buckets_by_local_time_not_utc_date(self):
        """A write at 7pm Pacific time is stored as ~2am UTC the NEXT calendar
        day. --since must compare against the day it happened LOCALLY, or a
        late evening write gets filed under tomorrow."""
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        try:
            at_utc = "2026-09-15T02:00:00+00:00"   # 2026-09-14 19:00 PDT
            with open(os.path.join(self.state_dir, "writes.jsonl"), "w") as f:
                f.write(json.dumps({"id": "w-1", "at": at_utc, "slug": "acme",
                                    "field": "Sales_Engineer_Overview__c",
                                    "new": "hi", "status": "verified"}) + "\n")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                guard.cmd_writes(argparse.Namespace(since="9/15/26", opp=None, json=True))
            self.assertEqual(json.loads(buf.getvalue()), [],
                             "a 7pm PT write must not be filed under the next UTC day")

            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                guard.cmd_writes(argparse.Namespace(since="9/14/26", opp=None, json=True))
            self.assertEqual(len(json.loads(buf2.getvalue())), 1,
                             "the same write IS on-or-after 9/14 in local time")
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()


if __name__ == "__main__":
    unittest.main()
