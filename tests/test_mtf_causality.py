"""Phase 2: multi-timeframe mapping and feature causality.

The checker is only trustworthy if it demonstrably FAILS on leaky code, so each class of
leak named in the Phase 2 brief is implemented deliberately below and must be caught:
centered windows, future shifts, full-sample normalisation, future session ranges, and the
resample-then-forward-fill 'unfinished higher-timeframe candle' leak."""
import unittest
from unittest import mock

import numpy as np

from edgelab.data.synthetic import generate_bars
from edgelab.data.validation import validate_and_freeze
from edgelab.engine.signals import OrderSpec, check_causality
from edgelab.features import engine as engine_mod
from edgelab.features.causality import check_feature_causality
from edgelab.features.engine import FeatureEngine
from edgelab.features.spec import REGISTRY, FeatureDef, FeatureError, FeatureSpec, all_defs, register
from edgelab.features.strategy_api import FeatureContext, FeatureStrategy
from edgelab.engine.signals import SignalSet
from edgelab.strategies.examples import TrendBreakoutATR
from tests.helpers import CME, NQ
from tests.phase2_helpers import SESSIONS

NS_MIN = 60_000_000_000


def _ds(tf=1, start="2024-03-06", end="2024-03-13", seed=5, ds_id="MTF"):
    df, _ = generate_bars(CME, start, end, tf_minutes=tf, seed=seed, session_gap_sigma=4)
    return validate_and_freeze(df, NQ, CME, f"{tf}m", tf, "synthetic", ds_id, volume_type="synthetic")


# ------------------------------------------------------------------ deliberately leaky features
def _leak_centered(inp, p):
    c = inp.bars.close
    out = np.full(len(c), np.nan)
    out[1:-1] = (c[:-2] + c[1:-1] + c[2:]) / 3          # uses c[t+1]
    return {"x": out}


def _leak_future_shift(inp, p):
    c = inp.bars.close
    return {"x": np.r_[c[1:], np.nan]}                   # next bar's close


def _leak_full_sample_z(inp, p):
    c = inp.bars.close
    return {"x": (c - c.mean()) / c.std()}                # mean/std of the WHOLE history


def _leak_session_final_high(inp, p):
    w = inp.session("NY_RTH")
    member, inst, _ = w.membership(inp.bars.ts_ns)
    out = np.full(len(member), np.nan)
    for i in np.unique(inst[member]):
        m = inst == i
        out[m] = inp.bars.high[m].max()                   # final session high, shown all session
    return {"x": out}


LEAKS = {"_leak_centered": _leak_centered, "_leak_future_shift": _leak_future_shift,
         "_leak_full_sample_z": _leak_full_sample_z, "_leak_session_final_high": _leak_session_final_high}


def _register_leaks():
    for fid, fn in LEAKS.items():
        if fid not in REGISTRY:
            register(FeatureDef(fid, 1, "test", (), (("x", "leaky"),), fn, "TEST ONLY - leaky",
                                "leaky", "leaky", "0"))


def _unregister_leaks():
    for fid in LEAKS:
        REGISTRY.pop(fid, None)


class TestMultiTimeframe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = _ds()
        cls.eng = FeatureEngine.for_dataset(cls.ds, SESSIONS)
        cls.close_ns = cls.ds.bars.ts_close_ns

    def htf_close(self, tf):
        # sma(period=1) on the HTF bar = that bar's close: a transparent probe of the mapping
        return self.eng.compute(FeatureSpec.make("sma", {"period": 1}, timeframe=tf))

    def test_value_switches_exactly_at_htf_close(self):
        r = self.htf_close("15m")
        v, c = r.arrays["sma"], self.ds.bars.close
        self.assertTrue(r.mapped_from_higher_timeframe)
        self.assertIn("completed", r.known_at)
        # CME sessions open at 18:00 ET, so 15m buckets end on clock quarter-hours
        boundary = (self.close_ns % (15 * NS_MIN)) == 0
        idx = np.flatnonzero(boundary)
        idx = idx[idx > 0]
        np.testing.assert_array_equal(v[idx], c[idx])        # complete at the boundary bar's close
        # between boundaries the value is constant and equals the LAST completed bucket
        for b0, b1 in zip(idx[:-1], idx[1:]):
            if b1 - b0 == 15:
                self.assertTrue((v[b0:b1] == c[b0]).all())
        # one bar before a boundary, the (still unfinished) bucket's close must NOT be visible
        leaked = [i for i in idx[1:] if v[i - 1] == c[i] and c[i] != c[i - 15]]
        self.assertEqual(leaked, [])

    def test_partial_session_end_bucket_is_exposed_at_session_close(self):
        v = self.htf_close("45m").arrays["sma"]
        loc = self.ds.bars.ts.tz_convert("America/New_York")
        last_bar = np.flatnonzero((loc.hour == 16) & (loc.minute == 59))  # closes 17:00 = session close
        self.assertTrue(len(last_bar) > 3)
        # the 16:30-17:00 bucket (30 of 45 minutes) is complete at 17:00, not at 17:15
        np.testing.assert_array_equal(v[last_bar], self.ds.bars.close[last_bar])
        np.testing.assert_array_equal(v[last_bar - 1], self.ds.bars.close[last_bar - 30])   # bar opening 16:29 closes the 16:30 bucket

    def test_nothing_before_first_completed_htf_bar(self):
        v = self.htf_close("60m").arrays["sma"]
        self.assertTrue(np.isnan(v[:59]).all())
        self.assertFalse(np.isnan(v[59]))

    def test_htf_features_are_truncation_invariant(self):
        for tf in ("5m", "15m", "45m", "60m"):
            for fid in ("atr", "ema", "rsi", "swings", "fvg", "session", "vwap"):
                r = check_feature_causality(self.eng, FeatureSpec.make(fid).at(tf), n_cuts=6)
                self.assertTrue(r.passed, r.detail)

    def test_naive_resample_ffill_leak_is_caught(self):
        """The classic bug: map each base bar to the HTF bar CONTAINING it (unfinished)."""
        def leaky_map(values, known, base_close):
            k = np.searchsorted(known, base_close, side="left")
            out = np.full(len(base_close), np.nan)
            ok = k < len(values)
            out[ok] = values[k[ok]]
            return out
        spec = FeatureSpec.make("sma", {"period": 1}, timeframe="15m")
        self.assertTrue(check_feature_causality(self.eng, spec).passed)
        with mock.patch.object(engine_mod, "map_to_base", leaky_map):
            r = check_feature_causality(self.eng, spec)
        self.assertFalse(r.passed)
        self.assertIn("after the bar", r.detail)

    def test_timeframe_must_be_a_multiple(self):
        eng5 = FeatureEngine.for_dataset(_ds(tf=5, ds_id="MTF5"), SESSIONS)
        with self.assertRaises(FeatureError):
            eng5.compute(FeatureSpec.make("atr", timeframe="7m"))
        with self.assertRaises(FeatureError):
            eng5.compute(FeatureSpec.make("atr", timeframe="1m"))


class TestFeatureCausality(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = _ds(tf=5, start="2024-03-01", end="2024-03-20", ds_id="CAUS")   # spans Mar 10 DST
        cls.eng = FeatureEngine.for_dataset(cls.ds, SESSIONS)
        _register_leaks()

    @classmethod
    def tearDownClass(cls):
        _unregister_leaks()

    def test_every_registered_feature_is_causal(self):
        specs = [FeatureSpec.make(d.feature_id) for d in all_defs() if not d.feature_id.startswith("_leak")]
        specs += [FeatureSpec.make("session", {"session": s}) for s in SESSIONS]
        specs += [FeatureSpec.make("vwap", {"anchor": "NY_RTH"}),
                  FeatureSpec.make("fvg", {"fill_rule": "close", "max_age_bars": 40, "min_size_atr": 0.2}),
                  FeatureSpec.make("swings", {"left": 5, "right": 0}),
                  FeatureSpec.make("rvol_tod", {"days": 3})]
        for s in specs:
            r = check_feature_causality(self.eng, s, n_cuts=10)
            self.assertTrue(r.passed, r.detail)

    def test_each_leak_class_is_caught(self):
        for fid in LEAKS:
            r = check_feature_causality(self.eng, FeatureSpec.make(fid), n_cuts=10)
            self.assertFalse(r.passed, f"checker missed leak {fid}")

    def test_feature_strategy_passes_phase1_causality_check(self):
        strat = TrendBreakoutATR(OrderSpec("market"), trend_tf="60m").bind(
            FeatureContext(self.ds, SESSIONS))
        rep = check_causality(strat, self.ds.bars, n_cuts=8)
        self.assertTrue(rep.passed, rep.detail)

    def test_strategy_built_on_a_leaky_feature_fails_phase1_check(self):
        class Leaky(FeatureStrategy):
            family = "leaky_test"

            def feature_specs(self):
                return [FeatureSpec.make("_leak_future_shift")]

            def signals_from_features(self, bars, f):
                s = SignalSet.empty(len(bars))
                nxt = f.get_output(self.feature_specs()[0], "x")
                up = np.isfinite(nxt) & (nxt > bars.close)
                s.direction[up] = 1
                s.stop_price[up] = bars.close[up] - 10
                return s
        rep = check_causality(Leaky(OrderSpec("market")).bind(FeatureContext(self.ds, SESSIONS)),
                              self.ds.bars, n_cuts=8)
        self.assertFalse(rep.passed)

    def test_unbound_feature_strategy_refuses(self):
        with self.assertRaises(RuntimeError):
            TrendBreakoutATR(OrderSpec("market")).generate_signals(self.ds.bars)

    def test_strategy_identity_is_dataset_agnostic(self):
        s = TrendBreakoutATR(OrderSpec("market"))
        b1 = s.bind(FeatureContext(self.ds, SESSIONS))
        b2 = s.bind(FeatureContext(_ds(tf=5, seed=99, ds_id="OTHER"), SESSIONS))
        self.assertEqual(s.strategy_id, b1.strategy_id)
        self.assertEqual(b1.strategy_id, b2.strategy_id)
        self.assertIn("features", s.spec)
        self.assertEqual(s.spec["features"][0]["timeframe"], "60m")


if __name__ == "__main__":
    unittest.main()
