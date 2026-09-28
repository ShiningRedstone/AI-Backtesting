"""Phase 2: feature numerics on small hand-computed cases.

Every expected number below is derived by hand in the comments, independently of the
implementation."""
import unittest

import numpy as np
import pandas as pd

from edgelab.data.synthetic import bars_from_ohlc
from edgelab.data.validation import validate_and_freeze
from edgelab.features.engine import FeatureEngine
from edgelab.features.spec import FeatureError, FeatureSpec, FeatureUnavailable, all_defs
from tests.helpers import CME, NQ, UTC247
from tests.phase2_helpers import SESSIONS


def engine(rows, volume=None, cal=UTC247, start="2024-01-02 00:00", tf=1, volume_type="synthetic"):
    df = bars_from_ohlc(rows, start=start, tf_minutes=tf)
    if volume is not None:
        df["volume"] = np.asarray(volume, float)
    ds = validate_and_freeze(df, NQ, cal, f"{tf}m", tf, "test", "NUM", volume_type=volume_type)
    return FeatureEngine.for_dataset(ds, SESSIONS)


def out(eng, fid, params=None, tf=None):
    return eng.compute(FeatureSpec.make(fid, params, tf)).arrays


ROWS = [(10, 12, 9, 11), (11, 13, 10, 12), (12, 12.5, 11.5, 12), (12, 16, 12, 15), (20, 21, 19, 20)]


class TestPriceFeatures(unittest.TestCase):
    def test_candle(self):
        a = out(engine([(10, 14, 9, 13), (12, 12, 12, 12)]), "candle")
        self.assertEqual(a["body"][0], 3)
        self.assertEqual(a["range"][0], 5)
        self.assertEqual(a["upper_wick"][0], 1)          # 14 - max(10, 13)
        self.assertEqual(a["lower_wick"][0], 1)          # min(10, 13) - 9
        self.assertAlmostEqual(a["body_frac"][0], 0.6)
        self.assertAlmostEqual(a["close_pos"][0], 0.8)   # (13 - 9) / 5
        self.assertTrue(np.isnan(a["gap"][0]))
        self.assertEqual(a["gap"][1], -1)                # 12 - 13
        self.assertTrue(np.isnan(a["body_frac"][1]))     # zero range -> NaN, not a crash

    def test_atr_wilder(self):
        # TR = [3, 3, 1, 4, 6] (bar 4 gaps up: |21 - 15| = 6). ATR(3): seed mean(3,3,1) = 7/3,
        # then 7/3 + (4 - 7/3)/3 = 26/9, then 26/9 + (6 - 26/9)/3 = 106/27
        a = out(engine(ROWS), "atr", {"period": 3})
        np.testing.assert_allclose(a["tr"], [3, 3, 1, 4, 6])
        self.assertTrue(np.isnan(a["atr"][:2]).all())
        np.testing.assert_allclose(a["atr"][2:], [7 / 3, 26 / 9, 106 / 27])

    def test_ema_sma_seeded(self):
        # closes 11, 12, 12, 15, 20; alpha = 2/(3+1) = 0.5; seed mean(11,12,12) = 35/3
        a = out(engine(ROWS), "ema", {"period": 3, "slope_bars": 1})
        np.testing.assert_allclose(a["ema"][2:], [35 / 3, 40 / 3, 50 / 3])
        np.testing.assert_allclose(a["slope"][3:], [5 / 3, 10 / 3])
        s = out(engine(ROWS), "sma", {"period": 3})
        np.testing.assert_allclose(s["sma"][2:], [35 / 3, 13, 47 / 3])

    def test_rsi_wilder(self):
        # closes 10, 11, 10, 12, 12 -> gains [1,0,2,0], losses [0,1,0,0]; period 2
        # seed (bar 2): ag = al = 0.5 -> 50; bar 3: ag = 1.25, al = 0.25 -> 100 - 100/6
        a = out(engine([(c, c + 1, c - 1, c) for c in (10, 11, 10, 12, 12)]), "rsi", {"period": 2})
        self.assertTrue(np.isnan(a["rsi"][:2]).all())
        np.testing.assert_allclose(a["rsi"][2:], [50, 100 - 100 / 6, 100 - 100 / 6])

    def test_rsi_edge_cases(self):
        up = out(engine([(c, c + 1, c - 1, c) for c in range(10, 20)]), "rsi", {"period": 3})
        self.assertTrue((up["rsi"][3:] == 100).all())    # no losses
        flat = out(engine([(10, 11, 9, 10)] * 8), "rsi", {"period": 3})
        self.assertTrue((flat["rsi"][3:] == 50).all())   # no movement at all

    def test_roc(self):
        a = out(engine(ROWS), "roc", {"period": 2})
        np.testing.assert_allclose(a["momentum"][2:], [1, 3, 8])   # 12-11, 15-12, 20-12
        self.assertAlmostEqual(a["roc"][4], 20 / 12 - 1)


class TestVolumeFeatures(unittest.TestCase):
    def test_vwap_and_std_reset_daily(self):
        # flat bars at TP 10, 12, 14 with volume 1, 1, 2 -> VWAP 10, 11, 12.5
        # std at bar 2: (100 + 144 + 2*196)/4 - 12.5^2 = 159 - 156.25 = 2.75
        rows = [(p, p, p, p) for p in (10, 12, 14)]
        eng = engine(rows, volume=[1, 1, 2])
        a = out(eng, "vwap")
        np.testing.assert_allclose(a["vwap"], [10, 11, 12.5])
        self.assertAlmostEqual(a["vwap_std"][2], np.sqrt(2.75))
        self.assertAlmostEqual(a["dist"][2], 1.5)
        # two trading days on a 24h UTC calendar: resets at midnight
        df = bars_from_ohlc([(p, p, p, p) for p in (10, 20, 30, 40)], start="2024-01-02 23:58")
        ds = validate_and_freeze(df, NQ, UTC247, "1m", 1, "t", "V2", volume_type="synthetic")
        v = FeatureEngine.for_dataset(ds, SESSIONS).compute(FeatureSpec.make("vwap")).arrays["vwap"]
        np.testing.assert_allclose(v, [10, 15, 30, 35])

    def test_volume_stats(self):
        a = out(engine([(10, 11, 9, 10)] * 5, volume=[1, 2, 3, 4, 10]), "volume_stats", {"period": 3})
        self.assertAlmostEqual(a["rel_volume"][3], 2.0)          # 4 / mean(1,2,3)
        self.assertAlmostEqual(a["rel_volume"][4], 10 / 3)       # 10 / mean(2,3,4)
        self.assertAlmostEqual(a["volume_z"][3], 2.0)            # (4-2)/std([1,2,3], ddof=1)
        b = out(engine([(10, 11, 9, 10)] * 3, volume=[5, 1, 3]), "volume_stats", {"period": 3})
        self.assertAlmostEqual(b["volume_pct"][2], 2 / 3)        # 1 and 3 are <= 3; 5 is not

    def test_volume_features_unavailable_without_volume(self):
        eng = engine([(10, 11, 9, 10)] * 30, volume=[np.nan] * 30, volume_type="none")
        for fid in ("vwap", "volume_stats", "rvol_tod"):
            ok, why = eng.availability(FeatureSpec.make(fid))
            self.assertFalse(ok)
            self.assertIn("volume", why)
            with self.assertRaises(FeatureUnavailable):
                eng.compute(FeatureSpec.make(fid))
        # price features still work on the same dataset
        self.assertTrue(np.isfinite(out(eng, "atr", {"period": 5})["atr"][10:]).all())

    def test_tick_volume_is_recorded(self):
        eng = engine([(10, 11, 9, 10)] * 5, volume=[1] * 5, volume_type="tick")
        r = eng.compute(FeatureSpec.make("vwap"))
        self.assertEqual(r.meta["volume_type"], "tick")


class TestStructure(unittest.TestCase):
    def test_confirmed_swings_and_age(self):
        # highs 1,2,5,3,2,4,6,1,1,1 with left=right=2: pivot high at bar 2 (5), confirmed at 4;
        # pivot at bar 6 (6), confirmed at 8. Never visible before confirmation.
        highs = [101, 102, 105, 103, 102, 104, 106, 101, 101, 101]
        rows = [(h - 0.5, h, h - 1, h - 0.5) for h in highs]
        a = out(engine(rows), "swings", {"left": 2, "right": 2})
        self.assertTrue(np.isnan(a["swing_high"][:4]).all())
        np.testing.assert_array_equal(a["swing_high"][4:], [105, 105, 105, 105, 106, 106])
        np.testing.assert_array_equal(a["swing_high_age"][4:], [2, 3, 4, 5, 2, 3])

    def test_bos_and_sweep(self):
        # swing high 5 (bar 2) confirmed at bar 3 with left=2, right=1.
        # bar 4: high 6 but close 4.5 -> sweep_high (wick through, close back below)
        # bar 5: close 5.5 > 5 with previous close 4.5 <= 5 -> bos_up, trend +1
        rows = [(1, 2, 0.5, 1.5), (1.5, 3, 1, 2.5), (2.5, 5, 2, 4), (4, 4.5, 3, 3.5),
                (3.5, 6, 3.4, 4.5), (4.5, 5.8, 4.4, 5.5), (5.5, 5.9, 5.2, 5.6)]
        a = out(engine(rows), "swings", {"left": 2, "right": 1})
        np.testing.assert_array_equal(a["sweep_high"], [0, 0, 0, 0, 1, 0, 0])
        np.testing.assert_array_equal(a["bos_up"], [0, 0, 0, 0, 0, 1, 0])
        np.testing.assert_array_equal(a["trend"], [0, 0, 0, 0, 0, 1, 1])

    def test_range_stats(self):
        rows = [(10, 11, 9, 10)] * 5 + [(10, 16, 10, 16)]   # constant TR 2, then a 6-point bar
        a = out(engine(rows), "range_stats", {"atr_period": 3, "lookback": 3})
        atr5 = 2 + (6 - 2) / 3                               # Wilder update on the big bar
        self.assertAlmostEqual(a["body_atr"][5], 6 / atr5)
        self.assertAlmostEqual(a["range_expansion"][5], 3.0)  # 6 / mean(2,2,2)
        self.assertAlmostEqual(a["consolidation_atr"][4], 2 / 2)


class TestFVG(unittest.TestCase):
    # bar2 forms a bullish gap: L2 = 11 > H0 = 10 -> zone [10, 11], size 1.
    # bar3 low 10.6 -> fill 0.4; bar4 low 10.8 -> still 0.4; bar5 low 9.9 <= 10 -> filled.
    ROWS = [(9.5, 10, 9, 9.8), (10, 13, 10, 12.8), (12.8, 14, 11, 13.5), (11.5, 12, 10.6, 11),
            (11, 12, 10.8, 11.5), (10.5, 11, 9.9, 10.2), (10.2, 10.9, 10, 10.4)]
    # (bar 6 high 10.9 >= L4 10.8, so no bearish gap forms there)

    def test_bullish_gap_lifecycle(self):
        a = out(engine(self.ROWS), "fvg", {"atr_period": 2})
        np.testing.assert_array_equal(a["bull_new"], [0, 0, 1, 0, 0, 0, 0])
        np.testing.assert_array_equal(a["bull_active"], [0, 0, 1, 1, 1, 0, 0])
        np.testing.assert_allclose(a["bull_fill"][2:5], [0.0, 0.4, 0.4])
        np.testing.assert_allclose(a["bull_top"][2:5], 11)
        np.testing.assert_allclose(a["bull_bottom"][2:5], 10)
        np.testing.assert_array_equal(a["bull_age"][2:5], [0, 1, 2])
        self.assertAlmostEqual(a["bull_dist"][3], 11 - 11)      # close 11 at the gap top
        self.assertTrue(np.isnan(a["bull_top"][5:]).all())      # filled -> no active gap
        self.assertEqual(a["bear_active"].sum(), 0)

    def test_close_fill_rule_and_expiry(self):
        c = out(engine(self.ROWS), "fvg", {"atr_period": 2, "fill_rule": "close"})
        self.assertEqual(c["bull_active"][5], 1)      # wick to 9.9 but close 10.2 > 10: still open
        e = out(engine(self.ROWS), "fvg", {"atr_period": 2, "max_age_bars": 2})
        np.testing.assert_array_equal(e["bull_active"], [0, 0, 1, 1, 0, 0, 0])

    def test_min_size_filter(self):
        a = out(engine(self.ROWS), "fvg", {"atr_period": 2, "min_size_points": 1.5})
        self.assertEqual(a["bull_new"].sum(), 0)

    def test_bearish_mirror(self):
        mirrored = [(-o, -l, -h, -c) for o, h, l, c in self.ROWS]
        a = out(engine([(o + 100, h + 100, l + 100, c + 100) for o, h, l, c in mirrored]), "fvg",
                {"atr_period": 2})
        np.testing.assert_array_equal(a["bear_active"], [0, 0, 1, 1, 1, 0, 0])
        np.testing.assert_allclose(a["bear_fill"][2:5], [0.0, 0.4, 0.4])


class TestSessionLevels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.data.synthetic import generate_bars
        df, _ = generate_bars(CME, "2024-03-05", "2024-03-15", tf_minutes=1, seed=8)
        cls.ds = validate_and_freeze(df, NQ, CME, "1m", 1, "synthetic", "SL", volume_type="synthetic")
        cls.eng = FeatureEngine.for_dataset(cls.ds, SESSIONS)
        cls.loc = cls.ds.bars.ts.tz_convert("America/New_York")

    def test_prev_session_levels_match_independent_groupby(self):
        a = self.eng.compute(FeatureSpec.make("session", {"session": "NY_RTH"})).arrays
        b = self.ds.bars
        mins = self.loc.hour * 60 + self.loc.minute
        rth = (mins >= 570) & (mins < 960)
        day = pd.Series(self.loc.date)[rth]
        highs = pd.Series(b.high[rth]).groupby(day.values).max()
        # The bar CLOSING at 16:00 (the 15:59 bar) is the first to see today's full session high;
        # the bar one minute earlier still sees the PREVIOUS session's high.
        days = sorted(highs.index)
        for prev, d in zip(days[:-1], days[1:]):
            last = np.flatnonzero(rth & (self.loc.date == d))[-1]
            self.assertEqual(a["prev_high"][last], highs[d])
            self.assertEqual(a["prev_high"][last - 1], highs[prev])
        np.testing.assert_array_equal(a["in_session"] == 1, rth)
        self.assertEqual(a["bars_into"][rth][0], 0)
        self.assertEqual(np.nanmax(a["minutes_into"]), 389)

    def test_prev_day_levels_match_groupby(self):
        a = self.eng.compute(FeatureSpec.make("daily_levels")).arrays
        td = CME.trading_dates(self.ds.bars.ts)
        s = pd.Series(self.ds.bars.close).groupby(td).last()
        dates = sorted(s.index)
        for prev, cur in zip(dates[:-1], dates[1:]):
            first_of_cur = np.flatnonzero(td == cur)[0]
            self.assertEqual(a["prev_day_close"][first_of_cur], s[prev])
            self.assertAlmostEqual(a["gap"][first_of_cur], self.ds.bars.open[first_of_cur] - s[prev])


class TestSpecs(unittest.TestCase):
    def test_every_feature_declares_complete_metadata(self):
        for d in all_defs():
            self.assertTrue(d.causal, d.feature_id)
            for field in ("summary", "calculation", "edge_cases", "warmup"):
                self.assertTrue(getattr(d, field).strip(), f"{d.feature_id}.{field}")
            self.assertTrue(d.outputs)

    def test_param_validation(self):
        with self.assertRaises(FeatureError):
            FeatureSpec.make("atr", {"period": 0})
        with self.assertRaises(FeatureError):
            FeatureSpec.make("atr", {"lenght": 14})
        with self.assertRaises(FeatureError):
            FeatureSpec.make("fvg", {"fill_rule": "body"})
        with self.assertRaises(FeatureError):
            FeatureSpec.make("atr", version=99)
        with self.assertRaises(FeatureError):
            FeatureSpec.make("nope")

    def test_unknown_session(self):
        eng = engine([(10, 11, 9, 10)] * 20)
        ok, why = eng.availability(FeatureSpec.make("session", {"session": "MARS"}))
        self.assertFalse(ok)
        self.assertIn("MARS", why)
        with self.assertRaises(FeatureError):
            eng.compute(FeatureSpec.make("session", {"session": "MARS"}))


if __name__ == "__main__":
    unittest.main()
