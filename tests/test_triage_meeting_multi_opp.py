"""`meeting-without-record` must look at the whole account, not one opp.

Measured 10/2/26: loves has five open opps. The 10/1 call was logged on the Expand
Datacenter opp, but triage evaluated the Inference Launchpad opp (entry 9/21) and the
VMO opps (empty / April) in turn, and each one re-filed the finding against the account.
"""
import inspect
import unittest
from datetime import date

from opp_axi import cli

I2S = {"A" * 15: "loves", "B" * 15: "loves", "C" * 15: "toyota"}


def rec(i, text):
    return {"Id": i * 15 + "XXX", "Sales_Engineer_Overview__c": text}


class LatestEntryBySlug(unittest.TestCase):
    def test_newest_entry_on_any_sibling_opp_wins(self):
        recs = [rec("A", "9/21/26 CS: older"), rec("B", "10/1/26 CS: newer"), rec("A", "")]
        self.assertEqual(cli.latest_entry_by_slug(recs, I2S)["loves"], date(2026, 10, 1))

    def test_order_does_not_matter(self):
        recs = [rec("B", "10/1/26 CS: newer"), rec("A", "9/21/26 CS: older")]
        self.assertEqual(cli.latest_entry_by_slug(recs, I2S)["loves"], date(2026, 10, 1))

    def test_undated_and_unmapped_opps_are_ignored(self):
        recs = [rec("A", ""), rec("Z", "10/1/26 CS: no slug")]
        self.assertEqual(cli.latest_entry_by_slug(recs, I2S), {})

    def test_accounts_stay_separate(self):
        recs = [rec("B", "10/1/26 CS: x"), rec("C", "8/1/26 CS: y")]
        out = cli.latest_entry_by_slug(recs, I2S)
        self.assertEqual(out["toyota"], date(2026, 8, 1))


class TriageUsesTheAccountDate(unittest.TestCase):
    def test_the_finding_reads_the_account_date_not_the_opp_date(self):
        src = inspect.getsource(cli.cmd_triage)
        self.assertIn("if found and (not acct_d or acct_d < since):", src)
        self.assertNotIn("(not d or d < since)", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
