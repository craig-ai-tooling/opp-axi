"""An SE Activity stamp is Craig's calendar day, not the box's.

The box runs Etc/UTC and Craig reads Pacific. `datetime.now()` is naive local time,
so from 17:00 Pacific onward it already reads as tomorrow. That window is not an
edge case here -- it is exactly when the automation runs. lawnmower-dayclose fires
at 18:00 Pacific (01:00 UTC the next day), so every SE Activity entry the nightly
chain writes lands inside it.

Measured: the per-opp sessions dayclose queued on 9/16 Pacific wrote tesla and
texas-instruments at 02:08-02:25 UTC, which is 19:08-19:25 Pacific on the 16th.
Both entries carry `9/17/26`.
"""
import unittest
from datetime import datetime
from unittest import mock

from opp_axi import cli


class StampIsCraigsCalendarDay(unittest.TestCase):
    def test_evening_pacific_is_still_todays_date(self):
        """18:30 Pacific on the 17th is 01:30 UTC on the 18th. The stamp says 17."""
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            self.skipTest("no zoneinfo")
        utc_instant = datetime(2026, 9, 18, 1, 30, tzinfo=ZoneInfo("UTC"))
        pacific = utc_instant.astimezone(ZoneInfo("America/Los_Angeles"))
        self.assertEqual("9/17/26", pacific.strftime("%-m/%-d/%y"))
        self.assertEqual(18, utc_instant.day, "the UTC clock has already rolled over")

    def test_now_local_and_today_stamp_agree(self):
        self.assertEqual(cli.now_local().strftime("%-m/%-d/%y"), cli.today_stamp())

    def test_stamp_matches_the_configured_zone_not_the_box(self):
        """Pin the behaviour through the real helper by moving the zone, rather than
        by mocking the clock: at any instant, Pacific and Tokyo are frequently on
        different calendar days, and the stamp must follow the zone."""
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            self.skipTest("no zoneinfo")
        with mock.patch.object(cli, "OPP_TZ", "America/Los_Angeles"):
            pacific = cli.today_stamp()
        with mock.patch.object(cli, "OPP_TZ", "UTC"):
            utc = cli.today_stamp()
        expected = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%-m/%-d/%y")
        self.assertEqual(expected, pacific)
        self.assertEqual(datetime.now(ZoneInfo("UTC")).strftime("%-m/%-d/%y"), utc)

    def test_an_unknown_zone_does_not_stop_a_write(self):
        """A bad OPP_TZ or a system with no tzdata must degrade, never raise -- this
        sits in the write path."""
        with mock.patch.object(cli, "OPP_TZ", "Not/AZone"):
            self.assertRegex(cli.today_stamp(), r"^\d{1,2}/\d{1,2}/\d{2}$")

    def test_no_naive_date_stamp_is_left_in_the_source(self):
        import inspect
        src = inspect.getsource(cli)
        for bad in ('datetime.now().strftime("%-m/%-d/%y")',
                    "datetime.now().strftime('%-m/%-d/%y')"):
            self.assertNotIn(bad, src,
                             "a date stamp must go through today_stamp(), which "
                             "knows Craig's timezone")


if __name__ == "__main__":
    unittest.main(verbosity=2)
