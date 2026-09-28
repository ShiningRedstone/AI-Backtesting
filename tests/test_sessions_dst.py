"""Phase 2: session windows across daylight-saving transitions.

US DST 2024 starts Sun Mar 10 and ends Sun Nov 3; UK DST starts Sun Mar 31. Between Mar 10
and Mar 31 New York and London are only 4 hours apart instead of 5."""
import unittest

import numpy as np
import pandas as pd

from edgelab.data.synthetic import generate_bars
from edgelab.data.validation import validate_and_freeze
from edgelab.features.engine import FeatureEngine
from edgelab.features.sessions import SessionWindow
from edgelab.features.spec import FeatureSpec
from tests.helpers import CME, NQ
from tests.phase2_helpers import SESSIONS


def minute_grid(day: str, tz: str = "UTC") -> np.ndarray:
    """All 1-minute bar opens of one UTC calendar day (plus neighbours), as UTC ns."""
    start = pd.Timestamp(day, tz="UTC") - pd.Timedelta(hours=12)
    return pd.date_range(start, periods=48 * 60, freq="1min").as_unit("ns").asi8


def utc_hours_in(window: SessionWindow, day_local: str) -> tuple[int, pd.Timestamp, pd.Timestamp]:
    ts = minute_grid(day_local)
    inside, inst, _ = window.membership(ts)
    target = (pd.Timestamp(day_local).date() - pd.Timestamp("1970-01-01").date()).days
    sel = ts[inside & (inst == target)]
    return len(sel), pd.Timestamp(sel[0], tz="UTC"), pd.Timestamp(sel[-1], tz="UTC")


class TestNewYorkWindowsAcrossDST(unittest.TestCase):
    def test_ny_0900_1000_is_60_bars_on_both_sides_of_spring_forward(self):
        w = SESSIONS["NY_0900_1000"]
        n, first, last = utc_hours_in(w, "2024-03-08")           # EST (UTC-5)
        self.assertEqual((n, first.hour, last.hour, last.minute), (60, 14, 14, 59))
        n, first, last = utc_hours_in(w, "2024-03-11")           # EDT (UTC-4)
        self.assertEqual((n, first.hour, last.hour, last.minute), (60, 13, 13, 59))

    def test_fall_back(self):
        w = SESSIONS["NY_0930_1030"]
        self.assertEqual(utc_hours_in(w, "2024-11-01")[1], pd.Timestamp("2024-11-01 13:30", tz="UTC"))
        self.assertEqual(utc_hours_in(w, "2024-11-04")[1], pd.Timestamp("2024-11-04 14:30", tz="UTC"))
        self.assertEqual(utc_hours_in(w, "2024-11-04")[0], 60)

    def test_windows_are_distinct_and_configurable(self):
        a = SESSIONS["NY_0900_1000"].membership(minute_grid("2024-06-03"))[0]
        b = SESSIONS["NY_1000_1100"].membership(minute_grid("2024-06-03"))[0]
        self.assertEqual(a.sum(), 60)
        self.assertEqual(b.sum(), 60)
        self.assertFalse((a & b).any())
        custom = SessionWindow("X", "America/New_York", "09:45", "09:50")
        self.assertEqual(custom.membership(minute_grid("2024-06-03"))[0].sum(), 5)


class TestLondonVsNewYork(unittest.TestCase):
    def ny_hour_of_london_open(self, day):
        ts = minute_grid(day)
        inside, inst, _ = SESSIONS["LONDON"].membership(ts)
        target = (pd.Timestamp(day).date() - pd.Timestamp("1970-01-01").date()).days
        first = pd.Timestamp(ts[inside & (inst == target)][0], tz="UTC").tz_convert("America/New_York")
        return first.hour

    def test_mismatch_weeks(self):
        self.assertEqual(self.ny_hour_of_london_open("2024-03-05"), 3)   # both on standard time
        self.assertEqual(self.ny_hour_of_london_open("2024-03-19"), 4)   # US summer, UK winter
        self.assertEqual(self.ny_hour_of_london_open("2024-04-02"), 3)   # both on summer time
        n, first, _ = utc_hours_in(SESSIONS["LONDON"], "2024-03-19")
        self.assertEqual((n, first.hour), (510, 8))                     # 08:00-16:30 GMT

    def test_tokyo_has_no_dst(self):
        for day in ("2024-01-10", "2024-07-10"):
            ts = minute_grid(day)
            inside, inst, _ = SESSIONS["ASIA_TOKYO"].membership(ts)
            target = (pd.Timestamp(day).date() - pd.Timestamp("1970-01-01").date()).days
            sel = ts[inside & (inst == target)]
            self.assertEqual(pd.Timestamp(sel[0], tz="UTC").hour, 0)     # 09:00 JST = 00:00 UTC
            self.assertEqual(len(sel), 360)


class TestWrapAndEdges(unittest.TestCase):
    w = SESSIONS["ASIA_NY_EVENING"]          # 19:00-02:00 New York, starts Sun..Thu

    def at(self, local: str):
        ts = np.array([pd.Timestamp(local, tz="America/New_York").tz_convert("UTC").value])
        inside, inst, into = self.w.membership(ts)
        start = None if inst[0] < 0 else (pd.Timestamp("1970-01-01") + pd.Timedelta(days=int(inst[0]))).date()
        return bool(inside[0]), start, into[0]

    def test_midnight_wrap_assigns_start_date(self):
        self.assertEqual(self.at("2024-06-10 01:30")[:2], (True, pd.Timestamp("2024-06-09").date()))  # Mon -> Sun instance
        self.assertEqual(self.at("2024-06-10 01:30")[2], 390)
        self.assertEqual(self.at("2024-06-10 19:00")[:2], (True, pd.Timestamp("2024-06-10").date()))
        self.assertFalse(self.at("2024-06-10 02:00")[0])            # end is exclusive
        self.assertFalse(self.at("2024-06-14 20:00")[0])            # Friday start not configured
        self.assertFalse(self.at("2024-06-15 01:00")[0])            # ...nor its after-midnight part

    def test_instance_end_across_dst_day(self):
        # instance starting Sun 2024-03-10 (DST starts that morning) ends Mon 02:00 EDT = 06:00 UTC
        inst = (pd.Timestamp("2024-03-10") - pd.Timestamp("1970-01-01")).days
        self.assertEqual(pd.Timestamp(self.w.end_ns(np.array([inst]))[0], tz="UTC"),
                         pd.Timestamp("2024-03-11 06:00", tz="UTC"))

    def test_nonexistent_and_ambiguous_end_times(self):
        gap = SessionWindow("G", "America/New_York", "01:00", "02:30", tuple(range(7)))
        d = (pd.Timestamp("2024-03-10") - pd.Timestamp("1970-01-01")).days
        # 02:30 does not exist on Mar 10 -> shifted forward to 03:00 EDT = 07:00 UTC
        self.assertEqual(pd.Timestamp(gap.end_ns(np.array([d]))[0], tz="UTC"),
                         pd.Timestamp("2024-03-10 07:00", tz="UTC"))
        amb = SessionWindow("A", "America/New_York", "00:30", "01:30", tuple(range(7)))
        d = (pd.Timestamp("2024-11-03") - pd.Timestamp("1970-01-01")).days
        # 01:30 happens twice on Nov 3 -> the LATER instant (EST) = 06:30 UTC (conservative)
        self.assertEqual(pd.Timestamp(amb.end_ns(np.array([d]))[0], tz="UTC"),
                         pd.Timestamp("2024-11-03 06:30", tz="UTC"))

    def test_fingerprint_changes_with_definition(self):
        a = SessionWindow("S", "America/New_York", "09:30", "10:30")
        self.assertNotEqual(a.fingerprint(), SessionWindow("S", "America/New_York", "09:30", "10:31").fingerprint())
        self.assertNotEqual(a.fingerprint(), SessionWindow("S", "Europe/London", "09:30", "10:30").fingerprint())


class TestSessionFeatureAcrossDST(unittest.TestCase):
    """End to end on a 1m dataset spanning Mar 10 2024."""

    @classmethod
    def setUpClass(cls):
        df, _ = generate_bars(CME, "2024-03-06", "2024-03-14", tf_minutes=1, seed=2)
        cls.ds = validate_and_freeze(df, NQ, CME, "1m", 1, "synthetic", "DST1", volume_type="synthetic")
        cls.a = FeatureEngine.for_dataset(cls.ds, SESSIONS).compute(
            FeatureSpec.make("session", {"session": "NY_0900_1000"})).arrays
        cls.loc = cls.ds.bars.ts.tz_convert("America/New_York")

    def test_sixty_bars_per_instance_and_local_times(self):
        ins = self.a["in_session"] == 1
        counts = pd.Series(self.loc.date[ins]).value_counts()
        self.assertTrue((counts == 60).all(), counts)
        self.assertTrue(set(self.loc.hour[ins]) == {9})
        utc_hours = set(self.ds.bars.ts[ins].hour)
        self.assertEqual(utc_hours, {13, 14})             # both offsets appear: DST handled

    def test_prev_levels_exposed_exactly_at_scheduled_end(self):
        closes_at_10 = np.flatnonzero((self.loc.hour == 9) & (self.loc.minute == 59))
        prev_day_high = np.nan
        for i in closes_at_10:
            ins = (self.a["in_session"] == 1) & (self.loc.date == self.loc[i].date())
            today_high = self.ds.bars.high[ins].max()
            self.assertEqual(self.a["prev_high"][i], today_high)          # known at 10:00 exactly
            np.testing.assert_equal(self.a["prev_high"][i - 1], prev_day_high)   # not one bar earlier
            prev_day_high = today_high


if __name__ == "__main__":
    unittest.main()
