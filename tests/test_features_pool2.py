"""Pool-2 indicators (ADR-87): causality with non-default parameters and known answers against direct reference
implementations (synthetic data, labelled as such)."""
from __future__ import annotations

import math
import unittest

import numpy as np
import pandas as pd

from edgelab.data.synthetic import generate_bars
from edgelab.data.validation import validate_and_freeze
from edgelab.features.causality import check_feature_causality
from edgelab.features.engine import FeatureEngine
from edgelab.features.spec import FeatureSpec
from tests.helpers import CME, NQ
from tests.phase2_helpers import SESSIONS

NEW = ("supertrend", "psar", "ichimoku", "cci", "divergence", "weekly_levels", "hma", "kama", "heikin_ashi")


def _ds(tf=5, start="2024-03-01", end="2024-03-27", seed=11):
    df, _ = generate_bars(CME, start, end, tf_minutes=tf, seed=seed, session_gap_sigma=4)
    return validate_and_freeze(df, NQ, CME, f"{tf}m", tf, "synthetic", "P2FEAT", volume_type="synthetic")


class TestPool2Causality(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = _ds()
        cls.eng = FeatureEngine.for_dataset(cls.ds, SESSIONS)

    def test_every_new_feature_is_causal_with_non_default_parameters(self):
        specs = [FeatureSpec.make(f) for f in NEW] + [
            FeatureSpec.make("supertrend", {"period": 7, "multiplier": 2.0}),
            FeatureSpec.make("psar", {"step": 0.03, "max_step": 0.3}),
            FeatureSpec.make("ichimoku", {"tenkan": 7, "kijun": 22, "senkou_b": 44, "displacement": 22}),
            FeatureSpec.make("cci", {"period": 14}),
            FeatureSpec.make("divergence", {"rsi_period": 9, "left": 5, "right": 3, "max_gap": 30}),
            FeatureSpec.make("hma", {"period": 55}), FeatureSpec.make("kama", {"period": 20, "fast": 3, "slow": 40})]
        for s in specs:
            r = check_feature_causality(self.eng, s, n_cuts=8)
            self.assertTrue(r.passed, r.detail)


class TestPool2KnownAnswers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = _ds()
        cls.eng = FeatureEngine.for_dataset(cls.ds, SESSIONS)
        b = cls.eng.bars
        cls.o, cls.h, cls.l, cls.c = (np.asarray(getattr(b, k), float) for k in ("open", "high", "low", "close"))
        cls.ts = pd.DatetimeIndex(pd.to_datetime(b.ts_ns, utc=True))

    def val(self, fid, out, **params):
        return self.eng.compute(FeatureSpec.make(fid, params)).arrays[out]

    def test_cci_matches_pandas(self):
        tp = pd.Series((self.h + self.l + self.c) / 3)
        sma = tp.rolling(20).mean()
        md = tp.rolling(20).apply(lambda w: np.abs(w - w.mean()).mean(), raw=True)
        ref = ((tp - sma) / (0.015 * md)).to_numpy()
        np.testing.assert_allclose(self.val("cci", "cci"), ref, rtol=1e-9, atol=1e-9, equal_nan=True)

    def test_hma_matches_direct_wma(self):
        def wma(x, n):
            out = np.full(len(x), np.nan)
            w = np.arange(1, n + 1)
            for t in range(n - 1, len(x)):
                out[t] = (x[t - n + 1:t + 1] * w).sum() / w.sum()
            return out
        raw = 2 * wma(self.c, 10) - wma(self.c, 20)
        ref = np.full(len(raw), np.nan)
        ref[19:] = wma(raw[19:], 4)
        np.testing.assert_allclose(self.val("hma", "hma", period=20), ref, rtol=1e-10, equal_nan=True)

    def test_kama_matches_direct_loop(self):
        n, c = 10, self.c
        ref = np.full(len(c), np.nan)
        k = c[n - 1]
        ref[n - 1] = k
        for t in range(n, len(c)):
            vol = np.abs(np.diff(c[t - n:t + 1])).sum()
            er = abs(c[t] - c[t - n]) / vol if vol > 0 else 0.0
            sc = (er * (2 / 3 - 2 / 31) + 2 / 31) ** 2
            k = k + sc * (c[t] - k)
            ref[t] = k
        np.testing.assert_allclose(self.val("kama", "kama"), ref, rtol=1e-10, equal_nan=True)

    def test_ichimoku_lines_and_cloud_shift(self):
        tk = self.val("ichimoku", "tenkan")
        for t in (8, 100, len(self.c) - 1):
            self.assertAlmostEqual(tk[t], (self.h[t - 8:t + 1].max() + self.l[t - 8:t + 1].min()) / 2)
        ca, lead = self.val("ichimoku", "cloud_a"), self.val("ichimoku", "span_a_lead")
        np.testing.assert_array_equal(ca[26:], lead[:-26])
        self.assertTrue(np.isnan(ca[:26]).all())

    def test_heikin_ashi_matches_direct_loop(self):
        ho = self.val("heikin_ashi", "ha_open")
        hc = (self.o + self.h + self.l + self.c) / 4
        ref = np.empty(len(hc))
        ref[0] = (self.o[0] + self.c[0]) / 2
        for t in range(1, len(hc)):
            ref[t] = (ref[t - 1] + hc[t - 1]) / 2
        np.testing.assert_allclose(ho, ref, rtol=1e-12)
        st, d = self.val("heikin_ashi", "streak"), self.val("heikin_ashi", "direction")
        for t in range(1, len(st)):
            if d[t] and d[t] == d[t - 1]:
                self.assertEqual(st[t], st[t - 1] + d[t])

    def test_supertrend_side_of_price(self):
        line, d = self.val("supertrend", "supertrend"), self.val("supertrend", "direction")
        ok = np.isfinite(line)
        self.assertTrue((self.c[ok & (d > 0)] >= line[ok & (d > 0)]).all())
        self.assertTrue((self.c[ok & (d < 0)] <= line[ok & (d < 0)]).all())
        fl = self.val("supertrend", "flip")
        self.assertGreater(int((fl != 0).sum()), 10)
        np.testing.assert_array_equal(fl[ok][1:] != 0, np.diff(d[ok]) != 0)

    def test_psar_never_inside_the_last_two_bars(self):
        sar, d = self.val("psar", "sar"), self.val("psar", "direction")
        for t in range(2, len(sar)):
            if d[t] > 0:
                self.assertLessEqual(sar[t], min(self.l[t], self.l[t - 1]) + 1e-9)
            else:
                self.assertGreaterEqual(sar[t], max(self.h[t], self.h[t - 1]) - 1e-9)
        for t in range(3, len(sar)):                     # a phase only reverses when the bar touched the prior SAR
            if d[t] != d[t - 1]:
                touched = self.l[t] <= sar[t - 1] if d[t - 1] > 0 else self.h[t] >= sar[t - 1]
                self.assertTrue(touched, t)

    def test_weekly_levels_previous_week(self):
        td = CME.trading_dates(self.ts)
        wk = pd.to_datetime(td).to_period("W-SUN")
        weeks = sorted(set(wk))
        pwh = self.val("weekly_levels", "prev_week_high")
        third = np.flatnonzero(wk == weeks[2])
        exp = self.h[np.flatnonzero(wk == weeks[1])].max()
        self.assertEqual(pwh[third[0]], exp)
        first = np.flatnonzero(wk == weeks[0])
        self.assertTrue(np.isnan(pwh[first[:-1]]).all())
        # like daily_levels: the bar that CLOSES at the week's scheduled end already sees that week as completed
        self.assertEqual(pwh[first[-1]], self.h[first].max())


class TestDivergenceKnownCase(unittest.TestCase):
    def test_bullish_divergence_on_a_constructed_series(self):
        from types import SimpleNamespace
        from edgelab.features.library.trend_extra import _divergence
        # a long fall (RSI very low), a first low at bar 40, a rebound, then a slow drift to a slightly LOWER low at 60:
        c = np.r_[np.linspace(200, 100, 41), np.linspace(101, 110, 9), np.linspace(109, 99.5, 11), np.linspace(101, 108, 10)]
        bars = SimpleNamespace(close=c, high=c + 0.5, low=c - 0.5, open=c)
        out = _divergence(SimpleNamespace(bars=bars), {"rsi_period": 14, "left": 3, "right": 2, "max_gap": 50})
        lows = [q for q in range(3, len(c) - 2) if c[q] < c[q - 3:q].min() and c[q] <= c[q + 1:q + 3].min()]
        self.assertEqual(lows, [40, 60])
        self.assertEqual(out["bull"][62], 1.0)           # confirmed at pivot + right
        self.assertEqual(out["bull"].sum(), 1.0)
        self.assertLess(out["rsi"][40], out["rsi"][60])
        self.assertEqual(out["bear"].sum(), 0.0)


if __name__ == "__main__":
    unittest.main()
