"""Phase 3: DSL -> compiler -> Strategy -> existing backtester."""
import copy
import glob
import unittest

import numpy as np

from edgelab.data.synthetic import generate_bars
from edgelab.data.validation import validate_and_freeze
from edgelab.engine.backtester import BacktestError, run_backtest
from edgelab.engine.costs import CostConfigError, CostModel, cost_model_from_config
from edgelab.engine.signals import Strategy, check_causality
from edgelab.features.cache import FeatureCache
from edgelab.features.engine import FeatureEngine
from edgelab.features.spec import REGISTRY, FeatureDef, FeatureSpec, register
from edgelab.features.strategy_api import FeatureContext, FeatureStrategy
from edgelab.strategy.compiler import StrategyCompileError, compile_definition, compile_strategy, explain
from tests.helpers import CFG, CME, INSTRUMENTS, NQ
from tests.phase2_helpers import SESSIONS
from tests.test_strategy_dsl import EMA20, base_doc

FIXTURES = sorted(f for f in glob.glob("strategies/fixtures/*.yaml")
                  if "variations" not in f and "proposals" not in f)
COSTS = CostModel(commission_per_side=1.0, slippage_ticks_market=1, slippage_ticks_stop=1)
BT = {**CFG["backtest"], "causality_cuts": 8}


def make_ds(inst=NQ, seed=7, ds_id="P3", start="2024-01-08", end="2024-02-09", asset_type="FUTURE"):
    df, _ = generate_bars(CME, start, end, tf_minutes=5, seed=seed)
    return validate_and_freeze(df, inst, CME, "5m", 5, "synthetic", ds_id, asset_type=asset_type,
                               volume_type="synthetic")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = make_ds()
        cls.eng = FeatureEngine.for_dataset(cls.ds, SESSIONS)

    def bound(self, doc, ds=None):
        ds = ds or self.ds
        return compile_strategy(doc, SESSIONS).bind(FeatureContext(ds, SESSIONS))

    def sig(self, doc):
        s = self.bound(doc)
        return s, s.generate_signals(self.ds.bars)

    def feat(self, fid, out, params=None, tf=None):
        return self.eng.compute(FeatureSpec.make(fid, params, tf)).arrays[out]


class TestCompiledForm(Base):
    def test_implements_existing_interfaces(self):
        s = compile_strategy(FIXTURES[0], SESSIONS)
        self.assertIsInstance(s, Strategy)
        self.assertIsInstance(s, FeatureStrategy)
        self.assertTrue(s.strategy_id.startswith("STR_"))
        spec = s.spec
        self.assertEqual(spec["dsl"]["strategy_id"], s.strategy_id)
        self.assertIn("features", spec)
        self.assertIn("compiler_version", spec["dsl"])

    def test_order_and_sizing_mapping(self):
        d = base_doc(exit={"stop": {"type": "points", "points": 10}, "target": {"type": "risk_reward", "multiple": 2},
                           "time_stop_bars": 6, "max_hold_bars": 3}, sizing={"mode": "risk", "risk_usd": 400})
        c = compile_definition(d, SESSIONS)
        o = c.order
        self.assertEqual((o.entry_type, o.stop_points, o.target_points, o.time_exit_bars, o.max_hold_bars),
                         ("market", 10.0, 20.0, 6, 3))
        self.assertEqual(c.sizing, {"mode": "risk", "risk_usd": 400.0, "max_contracts": None})
        d = copy.deepcopy(d)
        d["entry"]["order"] = {"type": "limit", "long_price": {"bar": "low"}, "expiry_bars": 4}
        d["exit"]["stop"] = {"type": "atr", "multiple": 1.0}
        o = compile_definition(d, SESSIONS).order
        self.assertEqual((o.entry_type, o.entry_expiry_bars, o.stop_points, o.target_points), ("limit", 4, None, None))

    def test_feature_specs_are_deduplicated(self):
        labels = [s.label for s in compile_definition("strategies/fixtures/ema_crossover.yaml", SESSIONS).feature_specs]
        self.assertEqual(sorted(labels), sorted(["ema(period=9,slope_bars=5)", "ema(period=21,slope_bars=5)",
                                                 "atr(period=14)", "session(session=NY_RTH)"]))

    def test_deterministic(self):
        a, sa = self.sig("strategies/fixtures/atr_breakout.yaml")
        b, sb = self.sig("strategies/fixtures/atr_breakout.yaml")
        self.assertEqual(a.strategy_id, b.strategy_id)
        for x, y in zip(sa.arrays(), sb.arrays()):
            np.testing.assert_array_equal(x, y)
        self.assertEqual(compile_definition(FIXTURES[1], SESSIONS).provenance,
                         compile_definition(FIXTURES[1], SESSIONS).provenance)

    def test_binding_rules(self):
        with self.assertRaises(StrategyCompileError):
            self.bound(base_doc(timeframe="15m"))                                 # 15m strategy, 5m data
        with self.assertRaises(StrategyCompileError):
            self.bound(base_doc(sizing={"mode": "fixed", "quantity": 0.5}))       # NQ: whole contracts
        cfd = make_ds(INSTRUMENTS["NAS100_CFD"], ds_id="P3_CFD", asset_type="CFD")
        self.assertIsNotNone(self.bound(base_doc(sizing={"mode": "fixed", "quantity": 0.5}), cfd))
        with self.assertRaises(RuntimeError):
            compile_strategy(base_doc(), SESSIONS).generate_signals(self.ds.bars)  # unbound

    def test_explain_is_descriptive_only(self):
        text = explain(compile_definition("strategies/fixtures/rsi_threshold.yaml", SESSIONS)).lower()
        self.assertIn("rsi(period=14).rsi < 25", text)
        for word in ("profitable", "edge", "win rate", "good strategy"):
            self.assertNotIn(word, text)


class TestSemantics(Base):
    def test_unknown_is_not_false(self):
        """not(close > ema200) must NOT fire during warm-up, where the comparison is unknown."""
        d = base_doc()
        d["entry"]["long"] = {"not": {"left": {"bar": "close"}, "op": ">",
                                      "right": {"feature": "ema", "params": {"period": 200}, "output": "ema"}}}
        _, s = self.sig(d)
        ema = self.feat("ema", "ema", {"period": 200})
        expected = np.isfinite(ema) & ~(self.ds.bars.close > ema)
        np.testing.assert_array_equal(s.direction == 1, expected)
        self.assertFalse((s.direction[:199] != 0).any())
        d["entry"]["long"] = {"any": [d["entry"]["long"]["not"], {"left": {"bar": "close"}, "op": ">", "right": 0}]}
        _, s = self.sig(d)
        self.assertTrue((s.direction == 1).all())                                 # known-true dominates

    def test_ambiguous_both_directions_dropped(self):
        d = base_doc()
        d["entry"] = {"direction": "both", "long": {"left": {"bar": "close"}, "op": ">", "right": 0},
                      "short": {"left": {"bar": "close"}, "op": ">", "right": 1}}
        st, s = self.sig(d)
        self.assertFalse(s.direction.any())
        self.assertEqual(st.last_diagnostics["ambiguous_both"], len(self.ds.bars))

    def test_crossover_lag_and_derived_reference(self):
        d = base_doc()
        d["entry"]["long"] = {"left": {"bar": "close"}, "op": "crosses_above", "right": EMA20}
        _, s = self.sig(d)
        c, e = self.ds.bars.close, self.feat("ema", "ema", {"period": 20})
        exp = np.zeros(len(c), bool)
        exp[1:] = (c[1:] > e[1:]) & (c[:-1] <= e[:-1])
        np.testing.assert_array_equal(s.direction == 1, exp & np.isfinite(e) & np.r_[False, np.isfinite(e[:-1])])
        d["entry"]["long"] = {"left": {"bar": "close"}, "op": ">", "right": {"bar": "high", "lag": 1}}
        _, s = self.sig(d)
        np.testing.assert_array_equal(s.direction[1:] == 1, c[1:] > self.ds.bars.high[:-1])
        self.assertEqual(s.direction[0], 0)                                      # lag unknown on bar 0
        band = {"arith": "add", "args": [{"feature": "sma", "output": "sma"},
                                         {"arith": "mul", "args": [1.5, {"feature": "atr", "output": "atr"}]}]}
        d["entry"]["long"] = {"left": {"bar": "close"}, "op": ">", "right": band}
        _, s = self.sig(d)
        ref = self.feat("sma", "sma", {"period": 20}) + 1.5 * self.feat("atr", "atr", {"period": 14})
        np.testing.assert_array_equal(s.direction == 1, np.isfinite(ref) & (c > ref))

    def test_stop_and_target_prices(self):
        d = base_doc(exit={"stop": {"type": "atr", "multiple": 2.0}, "target": {"type": "risk_reward", "multiple": 1.5}})
        _, s = self.sig(d)
        on = s.direction == 1
        atr, c = self.feat("atr", "atr"), self.ds.bars.close
        np.testing.assert_allclose(s.stop_price[on], (c - 2.0 * atr)[on])
        np.testing.assert_allclose(s.target_price[on], (c + 1.5 * 2.0 * atr)[on])
        d = base_doc(exit={"stop": {"type": "price", "long": {"bar": "high"}}})           # stop ABOVE a long
        st, s = self.sig(d)
        self.assertFalse(s.direction.any())
        self.assertGreater(st.last_diagnostics["invalid_stop"], 0)

    def test_windows_weekdays_cooldown(self):
        d = base_doc()
        d["entry"]["session"] = "NY_AM"
        d["entry"]["trading_weekdays"] = ["tue"]
        d["entry"]["cooldown_bars"] = 10
        st, s = self.sig(d)
        idx = np.flatnonzero(s.direction)
        self.assertTrue(len(idx) > 5)
        self.assertTrue((self.feat("session", "in_session", {"session": "NY_AM"})[idx] == 1).all())
        self.assertTrue((self.feat("time_of_day", "trading_weekday")[idx] == 1).all())
        self.assertTrue((np.diff(idx) > 10).all())
        self.assertGreater(st.last_diagnostics["cooldown_suppressed"], 0)

    def test_pending_order_prices_and_exit_arrays(self):
        _, s = self.sig("strategies/fixtures/atr_breakout.yaml")
        on_l, on_s = s.direction == 1, s.direction == -1
        np.testing.assert_array_equal(s.entry_price[on_l], self.ds.bars.high[on_l])
        np.testing.assert_array_equal(s.entry_price[on_s], self.ds.bars.low[on_s])
        self.assertIsNone(s.exit_long)
        _, s = self.sig("strategies/fixtures/rsi_threshold.yaml")
        np.testing.assert_array_equal(s.exit_long, self.feat("rsi", "rsi") > 50)


class TestCausality(Base):
    def test_every_fixture_passes_the_truncation_check(self):
        for f in FIXTURES:
            rep = check_causality(self.bound(f), self.ds.bars, n_cuts=8)
            self.assertTrue(rep.passed, f"{f}: {rep.detail}")

    def test_mtf_signals_identical_with_future_removed(self):
        s = self.bound("strategies/fixtures/mtf_trend_filter.yaml")
        full = s.generate_signals(self.ds.bars)
        close_ns = self.ds.bars.ts_close_ns
        mid_hour = np.flatnonzero(close_ns % (60 * 60_000_000_000) != 0)
        cuts = mid_hour[np.linspace(len(mid_hour) // 4, len(mid_hour) - 1, 15).astype(int)]
        for k in cuts:                      # cut INSIDE an unfinished 60m candle
            part = s.generate_signals(self.ds.bars.head(int(k)))
            for a, b in zip(full.arrays(), part.arrays()):
                np.testing.assert_array_equal(a[:k], b)
        self.assertTrue(full.direction.any())

    def test_lying_feature_is_caught_before_any_trade(self):
        def leak(inp, p):
            return {"x": np.r_[inp.bars.close[1:], np.nan]}
        register(FeatureDef("zz_liar_test", 1, "test", (), (("x", "next close"),), leak, "t", "t", "t", "0"))
        try:
            d = base_doc()
            d["entry"]["long"] = {"left": {"feature": "zz_liar_test", "output": "x"}, "op": ">", "right": {"bar": "close"}}
            with self.assertRaises(BacktestError) as cm:
                run_backtest(self.ds, self.bound(d), COSTS, BT)
            self.assertIn("LOOKAHEAD", str(cm.exception))
        finally:
            REGISTRY.pop("zz_liar_test", None)


class TestExecution(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cache = FeatureCache(None)
        cls.trades = {}
        for f in FIXTURES:
            s = compile_strategy(f, SESSIONS).bind(FeatureContext(cls.ds, SESSIONS, cls.cache))
            cls.trades[f.split("/")[-1][:-5]] = run_backtest(cls.ds, s, COSTS, BT, sizing=s.sizing).trades

    def test_directions_and_entry_types(self):
        t = self.trades
        self.assertEqual(set(t["structure_bos"]["direction"]), {-1})
        self.assertEqual(set(t["ema_crossover"]["direction"]), {-1, 1})
        self.assertEqual(set(t["atr_breakout"]["entry_type"]), {"stop"})
        self.assertEqual(set(t["fvg_entry"]["entry_type"]), {"limit"})
        self.assertEqual(set(t["mtf_trend_filter"]["entry_type"]), {"market"})

    def test_exit_types(self):
        reasons = set()
        for df in self.trades.values():
            reasons |= set(df["exit_reason"])
        for r in ("STOP", "TARGET", "TIME", "SIGNAL", "MAX_HOLD"):
            self.assertIn(r, reasons)
        self.assertIn("SIGNAL", set(self.trades["rsi_threshold"]["exit_reason"]))

    def test_risk_sizing(self):
        c = self.trades["atr_breakout"]["contracts"]
        self.assertTrue(((c >= 1) & (c <= 5)).all())
        self.assertGreater(c.nunique(), 1)

    def test_features_shared_through_the_cache(self):
        cache = FeatureCache(None)
        ctx = FeatureContext(self.ds, SESSIONS, cache)
        a = compile_strategy(base_doc(exit={"stop": {"type": "atr", "multiple": 1.0}}), SESSIONS).bind(ctx)
        a.generate_signals(self.ds.bars)
        writes = cache.stats["writes"]
        b = compile_strategy(base_doc(exit={"stop": {"type": "atr", "multiple": 3.0}}), SESSIONS).bind(ctx)
        b.generate_signals(self.ds.bars)
        self.assertNotEqual(a.strategy_id, b.strategy_id)
        self.assertEqual(cache.stats["writes"], writes)                 # nothing recomputed

    def test_same_definition_runs_on_futures_and_cfd(self):
        cfd = make_ds(INSTRUMENTS["NAS100_CFD"], ds_id="P3_CFD2", asset_type="CFD")
        s = compile_strategy("strategies/fixtures/ema_crossover.yaml", SESSIONS)
        fut = run_backtest(self.ds, s.bind(FeatureContext(self.ds, SESSIONS)), COSTS, BT, sizing=s.sizing)
        cfd_costs = CostModel(spread_points=1.0, slippage_unit="points", slippage_ticks_market=0.25,
                              slippage_ticks_stop=0.25, status="assumed", profile="test-only")
        res = run_backtest(cfd, s.bind(FeatureContext(cfd, SESSIONS)), cfd_costs, BT, sizing=s.sizing)
        self.assertEqual(fut.strategy_id, res.strategy_id)
        self.assertGreater(len(res.trades), 0)
        with self.assertRaises(CostConfigError):                          # no invented broker costs
            cost_model_from_config(CFG, cfd.instrument.symbol)


if __name__ == "__main__":
    unittest.main()
