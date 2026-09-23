"""Internal Spectro meetings must not match customer opps.

Measured 9/17/26: `Craig / Matt Weekly touchpoint on accounts` matched optum, `Brad / Craig
Weekly Sync` matched blue-yonder, `Dane/Craig Weekly Sync` matched emerson. The matcher read
title + summary only, and an internal 1:1 summary names the customers it discussed.
"""
import unittest
from datetime import date

from opp_axi.cli import wispr_internal_names, wispr_is_internal, wispr_match


def _m(title, participants, summary="Discussed Optum renewal and next steps."):
    m = {"id": title, "title": title, "start": "2026-09-16T17:00:00Z",
         "attendees": ["Craig Smith"], "summary": summary}
    if participants is not None:
        m["participants"] = participants
    return m


DANE = {"name": "Dane Ferguson", "emails": ["dane.ferguson@spectrocloud.com"]}
CUST = {"name": "Pat Buyer", "emails": ["pat@optum.com"]}


class IsInternal(unittest.TestCase):
    def test_all_spectro_emails_is_internal(self):
        self.assertTrue(wispr_is_internal(_m("Dane/Craig Weekly Sync", [DANE]), set()))

    def test_one_external_email_is_not_internal(self):
        self.assertFalse(wispr_is_internal(_m("Optum sync", [DANE, CUST]), set()))

    def test_no_participants_key_is_not_internal(self):
        # An old cache row predates the field; it must match exactly as before.
        self.assertFalse(wispr_is_internal(_m("Craig / Matt", None), set()))

    def test_empty_participants_is_not_internal(self):
        self.assertFalse(wispr_is_internal(_m("Solo", []), set()))

    def test_unknown_bare_name_is_not_internal(self):
        p = [DANE, {"name": "Someone New", "emails": []}]
        self.assertFalse(wispr_is_internal(_m("Zoom call", p), set()))

    def test_known_colleague_name_without_email_is_internal(self):
        recs = [_m("Dane/Craig", [DANE]),
                _m("Monday Muster", [{"name": "Dane Ferguson", "emails": []}])]
        known = wispr_internal_names(recs)
        self.assertIn("dane ferguson", known)
        self.assertTrue(wispr_is_internal(recs[1], known))

    def test_lookalike_domain_is_external(self):
        p = [{"name": "X", "emails": ["x@notspectrocloud.com"]}]
        self.assertFalse(wispr_is_internal(_m("X", p), set()))


class MatchSkipsInternal(unittest.TestCase):
    def test_internal_meeting_about_customer_is_dropped(self):
        recs = [_m("Craig / Matt Weekly touchpoint on accounts", [DANE]),
                _m("Optum architecture review", [DANE, CUST])]
        rows = wispr_match(recs, ["optum"], date(2026, 9, 1), 10, 80)
        self.assertEqual([r["title"] for r in rows], ["Optum architecture review"])


if __name__ == "__main__":
    unittest.main()
