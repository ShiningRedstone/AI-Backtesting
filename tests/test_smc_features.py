"""ADR-61: order_block / breaker and daily_nr, known answers on hand-built bars (SYNTHETIC) + causality."""
import unittest

import numpy as np

from edgelab.data.synthetic import generate_bars
from edgelab.data.validation import validate_and_freeze
from edgelab.features.causality import check_feature_causality
from edgelab.features.engine import FeatureEngine
from edgelab.features.spec import FeatureSpec
from tests.helpers import CME, NQ, dataset
from tests.phase2_helpers import SESSIONS

NAN = np.nan
# bar2 = the last BEARISH candle [99.4, 100.2]; bar3 = bullish displacement (body 2.8 >= 1.0 x ATR)
ROWS = [
    (100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100), (100, 100.2, 99.4, 99.6),        # 0 1 2
    (99.6, 102.5, 99.5, 102.4),                                                          # 3 displacement (up)
    (102.4, 102.6, 101.9, 102.2),                                                        # 4 no touch
    (102.2, 102.3, 100.0, 101.0),                                                        # 5 low 100.0 <= top 100.2: touch
    (101.0, 101.0, 99.0, 99.2),                                                          # 6 close 99.2 <= bottom 99.4: break
    (99.2, 99.5, 98.8, 99.0),                                                            # 7 touches the bear breaker
    (99.0, 99.3, 98.5, 98.9),                                                            # 8
]
P = dict(atr_period=2, disp=1.0, require_bos=False, lookback=5, zone="range", max_age_bars=50)


def out(first_touch, name, **kw):
    ds = dataset(ROWS)
    eng = FeatureEngine.for_dataset(ds, {})
    spec = FeatureSpec.make("order_block", {**P, "first_touch_only": first_touch, **kw})
    return eng.frame([spec]).get_output(spec, name)


class TestOrderBlock(unittest.TestCase):
    def test_zone_is_the_last_opposite_candle_before_the_displacement(self):
        top, bot = out(True, "ob_bull_top"), out(True, "ob_bull_bottom")
        self.assertEqual(out(True, "ob_bull_new").tolist(), [0, 0, 0, 1, 0, 0, 0, 0, 0])
        for t in (4, 5):
            self.assertEqual((top[t], bot[t]), (100.2, 99.4), t)
        self.assertTrue(np.isnan(top[:4]).all())                       # nothing known at or before the event bar
        self.assertEqual(out(True, "ob_bull_age")[4], 1.0)

    def test_first_touch_consumes_the_zone_but_the_reaction_bar_still_sees_it(self):
        top = out(True, "ob_bull_top")
        self.assertEqual(top[5], 100.2)                                 # bar 5 touches it and can react
        self.assertTrue(np.isnan(top[6:]).all())                        # consumed afterwards
        persistent = out(False, "ob_bull_top")
        self.assertEqual(persistent[5], 100.2)
        self.assertEqual(persistent[6], 100.2)                          # bar 6 (the failing bar) still sees the zone
        self.assertTrue(np.isnan(persistent[7:]).all())                 # a CLOSE <= bottom invalidated it at bar 6

    def test_failed_block_becomes_a_breaker_of_opposite_polarity(self):
        new, top = out(False, "brk_bear_new"), out(False, "brk_bear_top")
        self.assertEqual(new.tolist(), [0, 0, 0, 0, 0, 0, 1, 0, 0])      # forms at the break bar
        self.assertTrue(np.isnan(top[:7]).all())
        self.assertEqual((top[7], out(False, "brk_bear_bottom")[7]), (100.2, 99.4))
        self.assertEqual(top[8], 100.2)                                  # first_touch False: persists
        consumed = out(True, "brk_bear_top")
        self.assertEqual(consumed[7], 100.2)                             # bar 7 touches (high 99.5 >= 99.4) and reacts
        self.assertTrue(np.isnan(consumed[8]))
        self.assertTrue(np.isnan(out(False, "brk_bull_top")).all())

    def test_body_zone_and_displacement_threshold(self):
        top, bot = out(True, "ob_bull_top", zone="body"), out(True, "ob_bull_bottom", zone="body")
        self.assertEqual((top[4], bot[4]), (100.0, 99.6))                # bearish body: open 100 .. close 99.6
        self.assertTrue(np.isnan(out(True, "ob_bull_top", disp=3.0)).all())   # 2.8 / ATR is not >= 3.0 x ATR

    def test_zone_outputs_never_depend_on_the_bar_itself(self):
        a = out(True, "ob_bull_top")
        rows2 = ROWS[:5] + [(102.2, 500, 0.1, 500.0)] + ROWS[6:]        # rewrite bar 5 (and only its own values)
        ds = dataset(rows2)
        eng = FeatureEngine.for_dataset(ds, {})
        spec = FeatureSpec.make("order_block", {**P, "first_touch_only": True})
        b = eng.frame([spec]).get_output(spec, "ob_bull_top")
        np.testing.assert_array_equal(a[:6], b[:6])                      # bars 0..5 see the same state


class TestDailyNr(unittest.TestCase):
    def test_narrowest_of_the_last_n_completed_dates(self):
        ranges = [10, 8, 9, 5, 7, 6, 6.5, 6.2]                          # one range per UTC trading date (24 hourly bars)
        rows = []
        for r in ranges:
            day = [(100, 100, 100, 100)] * 24
            day[5] = (100, 100 + r / 2, 100, 100)
            day[10] = (100, 100, 100 - r / 2, 100)
            rows += day
        ds = dataset(rows, tf=60)
        eng = FeatureEngine.for_dataset(ds, {})
        spec = FeatureSpec.make("daily_nr", {"n": 3})
        f = eng.frame([spec])
        got, rng = f.get_output(spec, "prev_is_nr"), f.get_output(spec, "prev_range")
        day = np.repeat(np.arange(len(ranges)), 24)
        inner = np.tile(np.arange(24) < 23, len(ranges))                  # a date counts as completed at the close of
        for q in range(1, len(ranges)):                                   # its own LAST bar (daily_levels convention)
            np.testing.assert_allclose(rng[(day == q) & inner], ranges[q - 1])   # the PREVIOUS date, never today
        self.assertTrue(np.isnan(got[day < 3]).all())
        per_day = [got[day == q][0] for q in range(3, len(ranges))]
        # R_q = range of day q-1; NR-3 at q: R_q < min(R_{q-1}, R_{q-2}); only day 4 (R=5 < 9 and 8) qualifies
        self.assertEqual(per_day, [0.0, 1.0, 0.0, 0.0, 0.0])
        for q in range(3, len(ranges)):                                   # constant across the bars of a date
            self.assertEqual(len(set(got[(day == q) & inner].tolist())), 1)


class TestCausality(unittest.TestCase):
    def test_truncation_invariance(self):
        df, _ = generate_bars(CME, "2024-03-01", "2024-03-20", tf_minutes=5, seed=5, session_gap_sigma=4)
        ds = validate_and_freeze(df, NQ, CME, "5m", 5, "synthetic", "SMC", volume_type="synthetic")
        eng = FeatureEngine.for_dataset(ds, SESSIONS)
        for spec in (FeatureSpec.make("order_block"),
                     FeatureSpec.make("order_block", {"zone": "body", "first_touch_only": False, "disp": 1.0,
                                                      "require_bos": False}),
                     FeatureSpec.make("daily_nr"), FeatureSpec.make("daily_nr", {"n": 3}),
                     FeatureSpec.make("order_block", {"disp": 1.0, "require_bos": False}, "15m")):
            r = check_feature_causality(eng, spec, n_cuts=10)
            self.assertTrue(r.passed, (spec.label, r.detail))


if __name__ == "__main__":
    unittest.main()
