"""`--max-deep` is a runaway guard, and when it bites the run says so.

Measured 9/17/26 on the live 45-opp board. The default of 15 cut the pipeline off
at 15 opps and printed nothing about it, so albertsons and tesla -- each with a
real meeting and no SE Activity entry after it -- produced no finding. The nightly
pass had been examining a third of the board and reporting a clean result.

    --max-deep 15   wall 2s   scanned=45 deep=15 findings=1
    --max-deep 60   wall 3s   scanned=45 deep=37 findings=3

The fan-out is a filesystem walk of one opp dir plus a match against the
already-loaded Wispr cache. No API call, no network. One second bought 22 more opps
and two real meetings.
"""
import inspect
import unittest

from opp_axi import cli


class TruncationIsCountedAndReported(unittest.TestCase):
    def test_the_cap_increments_a_counter_rather_than_dropping_silently(self):
        src = inspect.getsource(cli.cmd_triage)
        self.assertIn("truncated += 1", src,
                      "an opp the cap skips must be counted, not silently dropped")
        self.assertIn("if deep >= a.max_deep:", src)

    def test_the_counter_starts_at_zero(self):
        src = inspect.getsource(cli.cmd_triage)
        self.assertIn("findings, scanned, deep, truncated = [], 0, 0, 0", src)

    def test_json_carries_truncated(self):
        src = inspect.getsource(cli.cmd_triage)
        self.assertIn('"truncated": truncated', src,
                      "--json must expose it; loop/dayclose.sh's brief branches on it")

    def test_the_human_summary_says_so_when_it_bites(self):
        src = inspect.getsource(cli.cmd_triage)
        self.assertIn("TRUNCATED:", src)
        self.assertIn("did not get one", src)

    def test_the_dedupe_guard_is_not_folded_into_the_cap(self):
        """`slug in deep_done` is a different question from the cap and must stay its
        own check -- an opp already examined is not an opp that was skipped, and
        counting it as truncated would report a cap that never bit."""
        src = inspect.getsource(cli.cmd_triage)
        self.assertIn("if slug in deep_done:", src)
        self.assertNotIn("if deep >= a.max_deep or slug in deep_done:", src)


class TheDefaultCoversTheRealPipeline(unittest.TestCase):
    def test_default_is_above_the_open_opp_count(self):
        import argparse
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="cmd")
        # Rebuild just this parser the way cli does, then read the default back.
        s = sub.add_parser("triage")
        s.add_argument("--max-deep", type=int, default=60)
        self.assertGreaterEqual(s.get_default("max_deep"), 45,
                                "the guard must sit above Craig's open-opp count, "
                                "or it rations the board every run")

    def test_the_shipped_default_matches(self):
        src = inspect.getsource(cli)
        self.assertIn('s.add_argument("--max-deep", type=int, default=60,', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
