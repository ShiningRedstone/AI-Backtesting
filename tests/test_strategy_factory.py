"""Strategy factory (ADR-60): allocation, determinism, identity, rejection, day-trading invariants,
MTF causality, manifest reproducibility, lineage, and separation from numerical testing.

A small deterministic fixture generation (a few variants per family) proves the factory; the
10,000-variant manifest is never generated or backtested here."""
import copy
import inspect
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from edgelab.data.synthetic import generate_bars
from edgelab.data.validation import validate_and_freeze
from edgelab.engine.backtester import run_backtest
from edgelab.engine.signals import check_causality
from edgelab.features.strategy_api import FeatureContext
from edgelab.strategy import daytrading as DT
from edgelab.strategy import factory as FX
from edgelab.strategy import factory_space as S
from edgelab.strategy.compiler import compile_definition, compile_strategy
from edgelab.strategy.dsl import identity as dsl_identity
from edgelab.strategy.lineage import LineageRecord
from tests.helpers import CFG, CME, INSTRUMENTS, NQ, ZERO_COSTS

REPO = Path(__file__).resolve().parents[1]
FROZEN_ALLOCATION = {
    "ema_crossover": 312, "sma_crossover": 298, "price_vs_ma": 312, "ma_slope": 291, "adx_trend": 305,
    "macd_crossover": 298, "roc_momentum": 298, "rsi_trend": 291, "rsi_mean_reversion": 319,
    "stochastic_reversion": 291, "bollinger_mean_reversion": 312, "zscore_reversion": 291,
    "atr_channel_reversion": 298, "ma_distance_reversion": 284, "range_reversion": 308, "donchian_breakout": 316,
    "opening_range_breakout": 330, "prev_day_breakout": 308, "session_hl_breakout": 323,
    "overnight_range_breakout": 315, "bollinger_squeeze_breakout": 294, "volatility_contraction_breakout": 287,
    "nr_breakout": 294, "range_expansion_breakout": 301, "ma_pullback": 315, "breakout_retest": 308,
    "momentum_pullback": 301, "bos_continuation": 400, "choch_reversal": 400, "ict_liquidity_fvg": 1000}
SMALL = {f.fid: 2 for f in S.FAMILIES}          # 60 strategies: every family represented


def choice(fam_id: str, **over) -> dict:
    """A hand-built, valid choice (tests override single dimensions)."""
    fam = S.FAMILY_BY_ID[fam_id]
    ch = {"family_params": {k: v[0] for k, v in fam.params.items()}, "timeframe": "5m",
          "session": None if fam.window else "ny_rth", "flat_rule": "session_flat", "direction": "both",
          "weekdays": "all", "mtf": None, "regime": "none", "confirm": "none", "order": {"type": "market"},
          "entry_delay": 0, "stop": {"type": "atr", "multiple": 2.0}, "target": {"type": "rr", "multiple": 2.0},
          "stop_bounds": "none", "time_exit": 120, "signal_exit": "none", "trailing": {"type": "none"},
          "sizing": "fixed_1", "cooldown": "none", "no_progress": {"type": "none"}, "max_trades": 0,
          "reentry": {"type": "none", "minutes": None}}
    fp = over.pop("family_params", {})
    ch["family_params"].update(fp)
    ch.update(over)
    return ch


def C(fam_id: str, **over) -> tuple[str, dict]:
    return fam_id, choice(fam_id, **over)


def npg(bars=3, kind="r", value=0.25) -> dict:
    return {"type": "yes", "bars": bars, "kind": kind, "value": value}


def trail(kind: str, **kw) -> dict:
    """A trailing choice with neutral defaults for the dimensions the test does not care about."""
    t = {"type": kind}
    if kind == "breakeven":
        return {**t, "trigger": {"type": "r", "value": 1.0}, "offset": 0.0, **kw}
    return {**t, "activation": {"type": "immediate", "value": None}, "every_bars": 1, "only_new_extreme": False,
            "min_step": 0.0, **kw}


def reject_code(fam_id: str, ch: dict) -> tuple[str, str]:
    with unittest.TestCase().assertRaises(S.Reject) as cm:
        FX.validate_candidate(S.FAMILY_BY_ID[fam_id], ch)
    return cm.exception.stage, cm.exception.code


def synthetic(tf: int, start="2024-03-04", end="2024-03-22", seed=3):
    df, _ = generate_bars(CME, start, end, tf_minutes=tf, seed=seed)
    return validate_and_freeze(df, INSTRUMENTS["NAS100_CFD"], CME, f"{tf}m", tf, "synthetic", f"FX{tf}",
                               asset_type="CFD", volume_type="synthetic")


class TestFamiliesAndAllocation(unittest.TestCase):
    def test_thirty_family_universe(self):
        self.assertEqual([f.num for f in S.FAMILIES], list(range(1, 31)))
        self.assertEqual(len({f.fid for f in S.FAMILIES}), 30)
        self.assertEqual({f.group for f in S.FAMILIES}, set(S.GROUPS))
        ict = S.FAMILY_BY_ID["ict_liquidity_fvg"]
        self.assertEqual(set(ict.params["setup"]), set(S.ICT_PARAMS))

    def test_exact_target_and_frozen_allocation(self):
        a = S.allocate()
        self.assertEqual(sum(a.values()), S.TARGET_TOTAL)
        self.assertEqual(sum(a.values()), 10_000)
        self.assertEqual(a, FROZEN_ALLOCATION, "the allocation is frozen: changing it needs a new ALLOCATION_VERSION")
        self.assertTrue(all(v >= S.MIN_PER_FAMILY for v in a.values()))
        for g in S.GROUPS:
            self.assertEqual(sum(a[f.fid] for f in S.FAMILIES if f.group == g), S.GROUP_SHARE[g])
        self.assertFalse(S.ALLOCATION_METHOD["results_used"])

    def test_small_fixture_hits_every_quota_exactly(self):
        res = FX.generate(quotas=SMALL)
        c = res.header["counts"]
        self.assertEqual(c["valid_unique"], 60)
        self.assertEqual(c["per_family_valid"], {k: 2 for k in sorted(SMALL)})
        self.assertEqual(c["candidates_generated"], c["valid_unique"] + c["rejected"] + c["duplicates"])


class TestDeterminismAndIdentity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = FX.generate(seed=11, quotas=SMALL)

    def test_same_inputs_same_universe(self):
        b = FX.generate(seed=11, quotas=SMALL)
        self.assertEqual(self.a.manifest_id, b.manifest_id)
        self.assertEqual([r["strategy_id"] for r in self.a.strategies], [r["strategy_id"] for r in b.strategies])
        self.assertEqual(self.a.rejections, b.rejections)
        c = FX.generate(seed=12, quotas=SMALL)
        self.assertNotEqual(self.a.manifest_id, c.manifest_id)

    def test_draws_do_not_depend_on_family_order(self):
        only = FX.generate(seed=11, quotas={"macd_crossover": 2})
        mine = [r for r in self.a.strategies if r["family_id"] == "macd_crossover"]
        self.assertEqual([r["strategy_id"] for r in only.strategies], [r["strategy_id"] for r in mine])

    def test_unique_logic_hashes_and_identity(self):
        lh = [r["logic_hash"] for r in self.a.strategies]
        self.assertEqual(len(lh), len(set(lh)))
        for r in self.a.strategies[:15]:
            ident = dsl_identity(r["definition"], {})
            self.assertEqual(ident.strategy_id, r["strategy_id"])
            self.assertEqual(r["strategy_id"], "STR_" + r["logic_hash"][:12].upper())
            self.assertEqual(ident.definition_hash, r["definition_hash"])

    def test_cosmetic_changes_do_not_create_new_identity(self):
        d = copy.deepcopy(self.a.strategies[0]["definition"])
        before = dsl_identity(d, {})
        d["name"], d["description"] = "renamed", "different words"
        d["family"]["hypothesis"] = "reworded"
        after = dsl_identity(d, {})
        self.assertEqual(before.logic_hash, after.logic_hash)
        self.assertNotEqual(before.definition_hash, after.definition_hash)

    def test_duplicate_logic_is_rejected_and_recorded(self):
        fam = S.FAMILY_BY_ID["ema_crossover"]
        first = choice("ema_crossover")
        other = choice("ema_crossover", family_params={"fast": 8})
        seq = iter([first, copy.deepcopy(first), other])
        with mock.patch.object(FX, "sample_choice", side_effect=lambda f, s, q: next(seq)):
            res = FX.generate(quotas={fam.fid: 2})
        self.assertEqual(len(res.strategies), 2)
        self.assertEqual(len(res.duplicates), 1)
        dup = res.duplicates[0]
        self.assertEqual(dup["duplicate_of"], res.strategies[0]["strategy_id"])
        self.assertTrue(dup["same_definition_hash"])
        self.assertEqual(res.header["counts"]["duplicates"], 1)

    def test_underfilled_family_fails_loudly(self):
        bad = choice("ema_crossover", trailing={"type": "zigzag"})
        with mock.patch.object(FX, "sample_choice", side_effect=lambda f, s, q: copy.deepcopy(bad)):
            with self.assertRaises(FX.FactoryError):
                FX.generate(quotas={"ema_crossover": 1})


class TestRejections(unittest.TestCase):
    def test_invalid_variations_are_rejected_by_name(self):
        cases = [
            (C("ema_crossover", trailing={"type": "zigzag"}), ("trailing", "TRAILING_UNKNOWN")),
            (C("ema_crossover", trailing={"type": "fixed_ticks"}), ("trailing", "EQUIVALENT_REPRESENTATION")),
            (C("ema_crossover", trailing=trail("fixed_points", points=7)), ("trailing", "TRAILING_PARAM_OUT_OF_DOMAIN")),
            (C("ema_crossover", target={"type": "rr", "multiple": 1.0},
               trailing=trail("atr", multiple=2.0, activation={"type": "r", "value": 1.0})),
             ("trailing", "REDUNDANT_TRAIL_ACTIVATION")),
            (C("ema_crossover", trailing=trail("atr", multiple=2.0, every_bars=3), timeframe="30m", session="ny_first30",
               time_exit="window", target={"type": "none"}, signal_exit="opposite_signal"),      # 3 bars in the window
             ("trailing", "TRAIL_NEVER_UPDATES")),
            (C("ema_crossover", trailing=trail("atr", multiple=2.0, only_new_extreme=True)),
             ("trailing", "REDUNDANT_ONLY_NEW_EXTREME")),
            (C("ema_crossover", trailing=trail("atr", multiple=2.0, activation={"type": "r", "value": 0.5},
                                              breakeven={"trigger": {"type": "r", "value": 0.5}, "offset": 0.0})),
             ("trailing", "BE_NOT_BEFORE_TRAIL")),
            (C("ema_crossover", timeframe="15m", time_exit=30, target={"type": "none"}, signal_exit="opposite_signal",
               trailing=trail("atr", multiple=2.0)), ("trailing", "TRAIL_NO_ROOM")),
            (C("ema_crossover", sizing="tiered_scaling"), ("sizing", "ENGINE_UNSUPPORTED_SCALING")),
            (C("ema_crossover", sizing="pct_equity"), ("sizing", "UNKNOWN_SIZING")),
            (C("ema_crossover", target={"type": "vwap"}), ("stop_target", "DATA_UNSUPPORTED_VOLUME")),
            (C("ema_crossover", regime="event_news"), ("entry_exit", "DATA_UNSUPPORTED_EVENTS")),
            (C("ema_crossover", no_progress=npg(bars=5)), ("entry_exit", "NO_PROGRESS_PARAM_OUT_OF_DOMAIN")),
            (C("ema_crossover", timeframe="30m", session="ny_first30", time_exit="window", target={"type": "none"},
               signal_exit="opposite_signal", no_progress=npg(bars=6)), ("entry_exit", "REDUNDANT_NO_PROGRESS")),
            (C("ema_crossover", target={"type": "rr", "multiple": 1.0}, no_progress=npg(kind="r", value=0.5, bars=3)),
             None),
            (C("ema_crossover", max_trades=1, reentry={"type": "block_after_stop", "minutes": None}),
             ("entry_exit", "REDUNDANT_REENTRY")),
            (C("ema_crossover", max_trades=1, cooldown="one_per_window"), ("entry_exit", "REDUNDANT_REENTRY")),
            (C("ema_crossover", target={"type": "none"}, signal_exit="opposite_signal",
               reentry={"type": "block_after_target", "minutes": None}), ("entry_exit", "REDUNDANT_REENTRY")),
            (C("ema_crossover", session="ny_first30", reentry={"type": "cooldown_after_exit", "minutes": 60}, time_exit=60),
             ("entry_exit", "REDUNDANT_REENTRY")),
            (C("ema_crossover", max_trades=5), ("entry_exit", "MAX_TRADES_OUT_OF_DOMAIN")),
            (C("ema_crossover", reentry={"type": "one_direction_per_session", "minutes": None}),
             ("entry_exit", "ENGINE_UNSUPPORTED_PATH_STATE")),
            (C("ema_crossover", family_params={"fast": 7}), ("parameter_domain", "PARAMETER_OUT_OF_DOMAIN")),
            (C("ema_crossover", family_params={"fast": 20, "slow": 21}), ("parameter_domain", "FAST_SLOW_TOO_CLOSE")),
            (C("ema_crossover", timeframe="2m"), ("dataset_timeframe", "TIMEFRAME_NOT_DERIVABLE")),
            (C("ema_crossover", timeframe="60m", session="ny_open"), ("session", "SESSION_TF_MISALIGNED")),
            (C("ema_crossover", mtf={"filter": "htf_rsi", "htf": "5m"}), ("mtf_causality", "MTF_PAIR_NOT_ALLOWED")),
            (C("ema_crossover", timeframe="15m", mtf={"filter": "htf_rsi", "htf": "30m"}),
             ("mtf_causality", "MTF_PAIR_NOT_ALLOWED")),
            (C("ema_crossover", stop={"type": "points", "points": 10}, target={"type": "points", "points": 30}),
             ("stop_target", "EQUIVALENT_REPRESENTATION")),
            (C("ema_crossover", target={"type": "atr", "multiple": 4.0}, stop={"type": "points", "points": 10},
                    time_exit=30), ("stop_target", "TARGET_UNREACHABLE_IN_WINDOW")),
            (C("ema_crossover", target={"type": "none"}, time_exit="window"),
             ("entry_exit", "NO_TARGET_WITHOUT_EXIT_RULE")),
            (C("ema_crossover", session="ny_first30", time_exit=120), ("entry_exit", "REDUNDANT_TIME_EXIT")),
            (C("ema_crossover", session="ny_first30", cooldown="60min", time_exit=60), ("entry_exit", "REDUNDANT_COOLDOWN")),
            (C("ema_crossover", session="ny_morning", flat_rule="early_flat_30", time_exit=60),
             ("entry_exit", "REDUNDANT_FLAT_RULE")),
            (C("ema_crossover", order={"type": "stop", "expiry_bars": 1}), ("entry_exit", "ORDER_TYPE_NOT_APPLICABLE")),
            (C("adx_trend", regime="adx_trend"), ("entry_exit", "REDUNDANT_OR_INAPPLICABLE_FILTER")),
            (C("nr_breakout", order={"type": "stop", "expiry_bars": 1}), ("entry_exit", "AMBIGUOUS_BOTH_DIRECTIONS")),
            (C("ict_liquidity_fvg", family_params={"setup": "killzone_momentum"}),
             ("session", "KILLZONE_REQUIRES_KILLZONE_SESSION")),
            (C("session_hl_breakout", family_params={"reference": "ny_am"}, session="ny_1000_1400"),
             ("session", "REFERENCE_SESSION_OVERLAPS_ENTRY")),
            (C("ict_liquidity_fvg", family_params={"ote": 0.79}), ("parameter_domain", "INACTIVE_PARAMETER_VARIED")),
        ]
        for (fid, ch), expected in cases:
            if expected is None:                     # a valid counterpart (progress below the target): must be accepted
                FX.validate_candidate(S.FAMILY_BY_ID[fid], ch)
                continue
            with self.subTest(fid=fid, expected=expected):
                self.assertEqual(reject_code(fid, ch), expected)

    def test_valid_counterparts_pass(self):
        for fid, ch in (("ema_crossover", choice("ema_crossover")),
                        ("session_hl_breakout", choice("session_hl_breakout", family_params={"reference": "asia_tokyo"},
                                                       session="ny_open")),
                        ("ict_liquidity_fvg", choice("ict_liquidity_fvg", family_params={"setup": "killzone_momentum"},
                                                     session="ny_open")),
                        ("nr_breakout", choice("nr_breakout", direction="long", order={"type": "stop", "expiry_bars": 1})),
                        ("opening_range_breakout", choice("opening_range_breakout", stop={"type": "range_side", "buffer": 0.0},
                                                           time_exit=60))):
            with self.subTest(fid=fid):
                defn, ident = FX.validate_candidate(S.FAMILY_BY_ID[fid], ch)
                self.assertTrue(ident["strategy_id"].startswith("STR_"))

    def test_rejections_are_machine_readable(self):
        res = FX.generate(quotas={"session_hl_breakout": 3, "opening_range_breakout": 3})
        for r in res.rejections:
            self.assertIn(r["rejection"]["stage"], FX.STAGES)
            self.assertRegex(r["rejection"]["code"], r"^[A-Z0-9_]+$")
            self.assertFalse(r["valid"])


class TestDayTrading(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = FX.generate(quotas=SMALL)

    def test_every_generated_strategy_satisfies_the_policy(self):
        for r in self.res.strategies:
            d = r["definition"]
            self.assertEqual(DT.validate_day_trading(d), [], r["strategy_id"])
            tf = int(d["timeframe"][:-1])
            self.assertLess(d["exit"]["max_hold_bars"] * tf, 23 * 60)
            self.assertEqual(r["prop_inputs"]["overnight"], False)
            self.assertIn(r["tags"]["trailing_type"], S.TRAIL_KINDS)

    def _def(self, **over):
        d, _ = FX.validate_candidate(S.FAMILY_BY_ID["ema_crossover"], choice("ema_crossover", **over))
        return copy.deepcopy(d)

    def test_forced_flat_condition_is_required(self):
        d = self._def()
        d["exit"]["signal"]["long"] = {"left": {"bar": "close"}, "op": "<", "right": {"feature": "ema",
                                                                                       "params": {"period": 50},
                                                                                       "output": "ema"}}
        self.assertIn("DT_NO_FORCED_FLAT", [x["code"] for x in DT.validate_day_trading(d)])
        d = self._def(signal_exit="opposite_signal")
        flat = d["exit"]["signal"]["short"]["any"]
        d["exit"]["signal"]["short"] = {"all": flat}            # nested under 'all' forces nothing
        self.assertIn("DT_NO_FORCED_FLAT", [x["code"] for x in DT.validate_day_trading(d)])

    def test_overnight_windows_are_refused(self):
        d = self._def()
        hold = [k for k in d["sessions"] if k.startswith("FXH")][0]
        d["sessions"][hold]["end"] = "17:30"                     # flat 17:35 NY: past the cutoff
        self.assertIn("DT_CROSSES_TRADING_DATE", [x["code"] for x in DT.validate_day_trading(d)])
        d["sessions"][hold]["end"] = "02:00"                     # wraps through 18:00 into the next date
        self.assertIn("DT_CROSSES_TRADING_DATE", [x["code"] for x in DT.validate_day_trading(d)])
        ldn = {"timezone": "Europe/London", "start": "08:00", "end": "21:55"}
        d = self._def()
        d["sessions"][hold] = ldn
        d["sessions"][d["entry"]["session"]] = {"timezone": "Europe/London", "start": "08:00", "end": "12:00"}
        self.assertIn("DT_CROSSES_TRADING_DATE", [x["code"] for x in DT.validate_day_trading(d)])

    def test_tokyo_and_london_windows_stay_in_one_trading_date_under_every_dst_regime(self):
        for key in ("asia", "london", "london_morning"):
            w = S.SESSION_PRESETS[key]
            for dd in DT.DST_PROBE_DATES:
                s, f = DT.ny_span({"timezone": w.tz, "start": w.entry_start, "end": w.flat}, 0, dd)
                since = lambda t: (t.hour * 60 + t.minute - 18 * 60) % 1440
                self.assertLessEqual(since(s) + (f - s).total_seconds() / 60, 22 * 60, (key, dd))
        s, f = DT.ny_span({"timezone": "Europe/London", "start": "08:00", "end": "16:00"}, 0, date(2024, 3, 13))
        self.assertEqual((s.hour, f.hour), (4, 12))              # US DST, UK not yet: 4 h offset

    def test_max_hold_guard(self):
        d = self._def()
        del d["exit"]["max_hold_bars"]
        self.assertIn("DT_NO_MAX_HOLD", [x["code"] for x in DT.validate_day_trading(d)])
        d["exit"]["max_hold_bars"] = 276                         # 276 x 5m = 23 h
        self.assertIn("DT_MAX_HOLD_TOO_LONG", [x["code"] for x in DT.validate_day_trading(d)])
        d["exit"]["max_hold_bars"] = 275
        self.assertEqual(DT.validate_day_trading(d), [])

    def test_entry_window_and_resting_orders_stay_inside_hold(self):
        d = self._def()
        d["sessions"][d["entry"]["session"]]["end"] = "15:59"
        self.assertIn("DT_ENTRY_OUTSIDE_HOLD", [x["code"] for x in DT.validate_day_trading(d)])
        d, _ = FX.validate_candidate(S.FAMILY_BY_ID["donchian_breakout"],
                                     choice("donchian_breakout", order={"type": "stop", "expiry_bars": 3}))
        d = copy.deepcopy(d)
        d["entry"]["order"]["expiry_bars"] = 10
        self.assertIn("DT_ORDER_OUTLIVES_WINDOW", [x["code"] for x in DT.validate_day_trading(d)])

    def test_engine_config_check(self):
        self.assertEqual(DT.check_engine_config(CFG["backtest"]), [])
        bad = copy.deepcopy(CFG["backtest"])
        bad["session"] = {"flatten_daily": False, "hold_overnight": True}
        self.assertEqual({x["code"] for x in DT.check_engine_config(bad)},
                         {"DT_ENGINE_HOLD_OVERNIGHT", "DT_ENGINE_NO_DAILY_FLATTEN"})
        late = copy.deepcopy(CFG["backtest"])
        late["session"]["flatten_time"] = "16:30"
        self.assertEqual([x["code"] for x in DT.check_engine_config(late)], ["DT_ENGINE_FLATTEN_LATE"])

    def test_sizing_cannot_circumvent_the_policy(self):
        for sz in ("fixed_1", "risk_500", "eq_05"):
            d = self._def(sizing=sz, stop={"type": "swing", "left": 3, "right": 1, "buffer": 0.25})
            self.assertEqual(DT.validate_day_trading(d), [])
        cd = compile_definition(self._def(sizing="risk_500"), {})
        self.assertEqual(cd.sizing, {"mode": "risk", "risk_usd": 500.0, "max_contracts": 40, "contract": "MNQ"})
        self.assertEqual(compile_definition(self._def(), {}).sizing, {"mode": "fixed", "contracts": 1.0, "contract": "MNQ"})


class TestForcedFlatBacktest(unittest.TestCase):
    """The strategy-level flat alone keeps every trade inside its trading date: the engine's own
    daily flatten / overnight protection is DISABLED here (synthetic data, not a research trial)."""

    def test_trades_are_flat_by_the_strategy_cutoff(self):
        ds = synthetic(5)
        cfg = copy.deepcopy(CFG["backtest"])
        cfg["session"] = {"flatten_daily": False, "hold_overnight": True}
        cfg["same_bar_policy"] = "conservative"
        ch = choice("ema_crossover", session="ny_last90", target={"type": "none"}, time_exit="window",
                    signal_exit="opposite_signal", stop={"type": "atr", "multiple": 3.0})
        defn, _ = FX.validate_candidate(S.FAMILY_BY_ID["ema_crossover"], ch)
        strat = compile_strategy(defn, {}).bind(FeatureContext(ds, {}))
        res = run_backtest(ds, strat, ZERO_COSTS, cfg, sizing=strat.sizing, contract=INSTRUMENTS["MNQ"])
        t = res.trades
        self.assertGreater(len(t), 3)
        self.assertTrue(res.causality.passed)
        td = np.asarray(ds.calendar.trading_dates(ds.bars.ts))
        np.testing.assert_array_equal(td[t.entry_bar.values], td[t.exit_bar.values])
        ny = pd.DatetimeIndex(ds.bars.ts).tz_convert("America/New_York")
        exit_open = ny[t.exit_bar.values]
        self.assertTrue(((exit_open.hour * 60 + exit_open.minute) <= 16 * 60).all())
        self.assertTrue((t.holding_minutes < 23 * 60).all())
        flat_exits = t[(exit_open.hour == 16) & (exit_open.minute == 0)]
        self.assertGreater(len(flat_exits), 0)
        self.assertTrue((flat_exits.exit_reason == "SIGNAL").all())

    def test_fixture_strategies_bind_and_pass_causality(self):
        res = FX.generate(quotas={"opening_range_breakout": 2, "ict_liquidity_fvg": 3, "zscore_reversion": 1})
        dss = {}
        for r in res.strategies:
            tf = int(r["definition"]["timeframe"][:-1])
            ds = dss.setdefault(tf, synthetic(tf, end="2024-03-14"))
            strat = compile_strategy(r["definition"], {}).bind(FeatureContext(ds, {}))
            rep = check_causality(strat, ds.bars, n_cuts=4)
            self.assertTrue(rep.passed, (r["strategy_id"], rep.detail))


class TestMTF(unittest.TestCase):
    def test_htf_context_is_causal(self):
        ch = choice("rsi_trend", mtf={"filter": "htf_ema_trend", "htf": "60m"})
        defn, _ = FX.validate_candidate(S.FAMILY_BY_ID["rsi_trend"], ch)
        cd = compile_definition(defn, {})
        self.assertIn("60m", {s.timeframe for s in cd.feature_specs})
        ds = synthetic(5, end="2024-03-14")
        strat = compile_strategy(defn, {}).bind(FeatureContext(ds, {}))
        rep = check_causality(strat, ds.bars, n_cuts=8)
        self.assertTrue(rep.passed, rep.detail)

    def test_only_declared_pairs(self):
        for tf, htfs in S.MTF_PAIRS.items():
            for h in htfs:
                self.assertGreater(int(h[:-1]), int(tf[:-1]))
                self.assertEqual(int(h[:-1]) % int(tf[:-1]), 0)
        self.assertEqual(S.MTF_PAIRS["60m"], ())


class TestManifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.res = FX.generate(quotas=SMALL)
        cls.dir = FX.write_manifest(cls.res, cls.tmp / cls.res.manifest_id)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_manifest_reproducible(self):
        v = FX.verify(self.dir)
        self.assertTrue(v["reproducible"], v)
        h = FX.read_header(self.dir)
        self.assertEqual(h["manifest_id"], self.res.manifest_id)
        self.assertEqual(h["counts"]["valid_unique"], 60)
        for k in ("seed", "factory_version", "variation_space_version", "allocation_version", "dsl_version",
                  "compiler_version", "strategies_sha256"):
            self.assertIn(k, h["identity"])
        self.assertEqual(h["protocol"]["numerical_trials_executed"], 0)
        self.assertEqual(h["protocol"]["holdout_looks"], 0)

    def test_full_lineage_is_preserved(self):
        rows = list(FX.iter_rows(self.dir))
        self.assertEqual(len(rows), 60)
        dims = {"family_params", "timeframe", "session", "flat_rule", "direction", "weekdays", "mtf", "regime",
                "confirm", "order", "entry_delay", "stop", "target", "stop_bounds", "time_exit", "signal_exit",
                "trailing", "sizing", "cooldown", "no_progress", "max_trades", "reentry"}
        for r in rows[:20]:
            self.assertEqual(set(r["variation"]), dims)
            self.assertEqual(FX.validate_candidate(S.FAMILY_BY_ID[r["family_id"]], r["variation"])[1]["logic_hash"],
                             r["logic_hash"])                   # the manifest row regenerates the strategy
            rec = FX.lineage_record(r)
            self.assertIsInstance(rec, LineageRecord)
            self.assertEqual(rec.generation_method, "factory_variant")
            self.assertEqual(r["allocation_bucket"], f"{r['group']}/{r['family_id']}")
            self.assertEqual(r["lineage"]["seed"], FX.DEFAULT_SEED)

    def test_explorer_queries(self):
        q = FX.query(self.dir, {"family_id": "ema_crossover"})
        self.assertEqual(q["total"], 2)
        self.assertNotIn("definition", q["rows"][0])
        sid = q["rows"][0]["strategy_id"]
        self.assertEqual(FX.query(self.dir, {"strategy_id": sid})["total"], 1)
        self.assertEqual(FX.query(self.dir, {"logic_hash": q["rows"][0]["logic_hash"][:10]})["total"], 1)
        by_trail = sum(FX.query(self.dir, {"trailing_type": k})["total"] for k in S.TRAIL_KINDS)
        self.assertEqual(by_trail, 60)
        n_mtf = FX.query(self.dir, {"mtf": "true"})["total"]
        self.assertEqual(n_mtf + FX.query(self.dir, {"mtf": "false"})["total"], 60)
        with self.assertRaises(ValueError):
            FX.query(self.dir, {"pnl": ">0"})
        dist = FX.summary(self.dir)["distributions"]
        for k in ("family_id", "timeframe", "session", "risk_model", "stop_type", "target_type", "weekday_filter",
                  "regime_filter", "direction", "trailing_type"):
            self.assertEqual(sum(dist[k].values()), 60, k)


class TestSeparationFromTesting(unittest.TestCase):
    def test_generation_runs_no_trials_and_touches_no_data_or_holdout(self):
        boom = mock.Mock(side_effect=AssertionError("numerical / data / protocol access during generation"))
        with mock.patch("edgelab.engine.backtester.run_backtest", boom), \
                mock.patch("edgelab.engine.signals.check_causality", boom), \
                mock.patch("edgelab.data.importer.load_validated", boom), \
                mock.patch("edgelab.research.protocol.resolve_windows", boom), \
                mock.patch("edgelab.research.protocol.trial_key", boom), \
                mock.patch("edgelab.services.Services.evaluate_holdout", boom), \
                mock.patch("edgelab.services.Services._protocol_gate", boom):
            res = FX.generate(quotas={"ema_crossover": 2, "ict_liquidity_fvg": 2})
        self.assertEqual(len(res.strategies), 4)
        boom.assert_not_called()

    def test_factory_modules_do_not_import_the_engine_or_research(self):
        for mod in (FX, S, DT):
            imports = [ln for ln in inspect.getsource(mod).splitlines() if ln.strip().startswith(("import ", "from "))]
            for banned in ("backtester", "edgelab.services", "edgelab.research", "importer", "edgelab.data.store"):
                self.assertFalse([ln for ln in imports if banned in ln], (mod.__name__, banned))


class TestServicesAndCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.root / "configs")
        from edgelab.services import Services
        cls.svc = Services(root=cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_generate_list_summary_query(self):
        r = self.svc.factory_generate(quotas={"ema_crossover": 2, "donchian_breakout": 1})
        mid = r["manifest_id"]
        self.assertEqual(r["counts"]["valid_unique"], 3)
        self.assertEqual([m["manifest_id"] for m in self.svc.factory_manifests()], [mid])
        self.assertEqual(self.svc.factory_summary(mid)["counts"]["valid_unique"], 3)
        self.assertEqual(self.svc.factory_query(mid, {"family_id": "donchian_breakout"})["total"], 1)
        self.assertTrue(self.svc.factory_verify(mid)["reproducible"])
        with self.assertRaises(ValueError):
            self.svc.factory_summary("../etc")

    def test_http_routes(self):
        from edgelab.web.app import create_app
        r = self.svc.factory_generate(quotas={"rsi_trend": 1})
        app = create_app(self.root)
        c = app.test_client()
        self.assertEqual(c.get(f"/api/factory/{r['manifest_id']}").status_code, 200)
        q = c.get(f"/api/factory/{r['manifest_id']}/strategies?family_id=rsi_trend&limit=5").get_json()
        self.assertEqual(q["total"], 1)
        self.assertEqual(c.get("/api/factory/FM_bad/strategies").status_code, 400)


class TestCatalog(unittest.TestCase):
    """The agreed 30-family catalog, stable identifiers, pinned ICT setups, lineage of the exact definition."""
    IDS = ["ema_crossover", "sma_crossover", "price_vs_ma", "ma_slope", "adx_trend", "macd_crossover", "roc_momentum",
           "rsi_trend", "rsi_mean_reversion", "stochastic_reversion", "bollinger_mean_reversion", "zscore_reversion",
           "atr_channel_reversion", "ma_distance_reversion", "range_reversion", "donchian_breakout",
           "opening_range_breakout", "prev_day_breakout", "session_hl_breakout", "overnight_range_breakout",
           "bollinger_squeeze_breakout", "volatility_contraction_breakout", "nr_breakout", "range_expansion_breakout",
           "ma_pullback", "breakout_retest", "momentum_pullback", "bos_continuation", "choch_reversal",
           "ict_liquidity_fvg"]
    ICT = {"liquidity_sweep_reversal", "liquidity_raid_reversal", "fvg_reaction", "killzone_momentum", "ict_ote",
           "order_block_reaction", "breaker_block", "ict_opening_range"}

    def test_identifiers_are_stable(self):
        self.assertEqual([f.fid for f in S.FAMILIES], self.IDS)

    def test_ict_bundle_carries_every_agreed_setup(self):
        ict = S.FAMILY_BY_ID["ict_liquidity_fvg"]
        self.assertEqual(set(ict.params["setup"]), self.ICT)
        self.assertEqual(set(S.ICT_PARAMS), self.ICT)
        for setup in sorted(self.ICT):
            with self.subTest(setup=setup):
                session = {"killzone_momentum": "ny_open", "ict_opening_range": "ny_afternoon"}.get(setup, "ny_rth")
                ch = choice("ict_liquidity_fvg", family_params={"setup": setup}, session=session,
                            stop={"type": "zone", "buffer": 0.25}, target={"type": "rr", "multiple": 2.0})
                defn, ident = FX.validate_candidate(ict, ch)
                self.assertEqual(DT.validate_day_trading(defn), [])
                self.assertTrue(ident["strategy_id"].startswith("STR_"))
        feats = {setup: {s.feature_id for s in compile_definition(
            FX.validate_candidate(ict, choice("ict_liquidity_fvg", family_params={"setup": setup},
                                              session={"killzone_momentum": "ny_open",
                                                       "ict_opening_range": "ny_afternoon"}.get(setup, "ny_rth"),
                                              stop={"type": "zone", "buffer": 0.0}))[0], {}).feature_specs}
                 for setup in self.ICT}
        self.assertIn("order_block", feats["order_block_reaction"])
        self.assertIn("order_block", feats["breaker_block"])
        self.assertIn("fvg", feats["fvg_reaction"])
        self.assertIn("session", feats["ict_opening_range"])

    def test_ict_setups_differ_in_logic_and_inactive_params_are_pinned(self):
        ict = S.FAMILY_BY_ID["ict_liquidity_fvg"]
        ids = set()
        for setup in sorted(self.ICT):
            ses = {"killzone_momentum": "ny_open", "ict_opening_range": "ny_afternoon"}.get(setup, "ny_rth")
            ids.add(FX.validate_candidate(ict, choice("ict_liquidity_fvg", family_params={"setup": setup}, session=ses))[1]["logic_hash"])
        self.assertEqual(len(ids), 8)
        stage, code = reject_code("ict_liquidity_fvg", choice("ict_liquidity_fvg",
                                  family_params={"setup": "ict_opening_range", "ob_disp": 1.5}, session="ny_afternoon"))
        self.assertEqual(code, "INACTIVE_PARAMETER_VARIED")
        # an opening-range sweep needs the range to be COMPLETED before the entry window opens
        self.assertEqual(reject_code("ict_liquidity_fvg", choice(
            "ict_liquidity_fvg", family_params={"setup": "ict_opening_range", "ib": 60}, session="ny_1000_1400"))[1],
            "REFERENCE_SESSION_OVERLAPS_ENTRY")

    def test_roc_percentage_and_atr_units_and_nr_bar_and_day_scopes(self):
        roc = S.FAMILY_BY_ID["roc_momentum"]
        a = FX.validate_candidate(roc, choice("roc_momentum", family_params={"unit": "atr", "atr_k": 2.0}))[1]
        b = FX.validate_candidate(roc, choice("roc_momentum", family_params={"unit": "pct", "atr_k": 1.0, "pct": 0.1}))[1]
        self.assertNotEqual(a["logic_hash"], b["logic_hash"])
        self.assertEqual(reject_code("roc_momentum", choice("roc_momentum", family_params={"unit": "pct", "atr_k": 2.0,
                                                                                         "pct": 0.1}))[1],
                         "INACTIVE_PARAMETER_VARIED")
        nr = S.FAMILY_BY_ID["nr_breakout"]
        d_bar, _ = FX.validate_candidate(nr, choice("nr_breakout", family_params={"scope": "bar", "n": 7}, direction="long"))
        d_day, _ = FX.validate_candidate(nr, choice("nr_breakout", family_params={"scope": "day", "n": 7}, direction="long"))
        self.assertEqual({s.feature_id for s in compile_definition(d_day, {}).feature_specs} & {"daily_nr", "narrow_range"},
                         {"daily_nr"})
        self.assertIn("narrow_range", {s.feature_id for s in compile_definition(d_bar, {}).feature_specs})

    def test_lineage_pins_the_exact_family_definition(self):
        res = FX.generate(seed=3, quotas={"ict_liquidity_fvg": 8, "ema_crossover": 1})
        for r in res.strategies:
            self.assertEqual(r["lineage"]["family_spec_sha256"], FX.family_hash(S.FAMILY_BY_ID[r["family_id"]]))
        self.assertEqual({r["lineage"]["setup"] for r in res.strategies if r["family_id"] == "ict_liquidity_fvg"} <= self.ICT, True)
        hdr = {f["family_id"]: f["family_spec_sha256"] for f in res.header["families"]}
        self.assertEqual(len(hdr), 30)
        self.assertEqual(res.header["identity"]["catalog_sha256"], FX.catalog_hash())
        changed = FX.family_spec(S.FAMILY_BY_ID["ema_crossover"])
        self.assertEqual(FX.family_hash(S.FAMILY_BY_ID["ema_crossover"]), hdr["ema_crossover"])
        self.assertIn("fast", changed["parameters"])

    def test_allocation_is_frozen_and_independent_of_results(self):
        self.assertEqual(S.allocate(), FROZEN_ALLOCATION)
        self.assertFalse(S.ALLOCATION_METHOD["results_used"])
        src = inspect.getsource(S.allocate)
        for banned in ("trades", "metrics", "backtest", "expectancy", "profit"):
            self.assertNotIn(banned, src)
        self.assertEqual(S.ALLOCATION_VERSION, "edgelab-dt-allocation/1")      # numbers unchanged by ADR-61


class TestTrailingVariation(unittest.TestCase):
    """Trailing is a real variation dimension and every sampled kind builds a valid, causal, day-trading strategy."""

    def test_every_trailing_kind_builds_compiles_and_keeps_the_invariants(self):
        fam = S.FAMILY_BY_ID["ema_crossover"]
        doms = {"fixed_points": trail("fixed_points", points=10), "atr": trail("atr", multiple=2.0),
                "breakeven": trail("breakeven"), "prev_bar": trail("prev_bar", buffer=0.0),
                "swing": trail("swing", left=3, buffer=0.25), "ma": trail("ma", period=20, buffer=0.0),
                "channel": trail("channel", channel="bollinger", n=10),
                "chandelier": trail("chandelier", n=10, multiple=2.0)}
        self.assertEqual(set(doms), set(S.TRAIL_KINDS) - {"none"})
        seen = set()
        for kind, tr in doms.items():
            for direction in ("long", "short", "both"):
                with self.subTest(kind=kind, direction=direction):
                    defn, ident = FX.validate_candidate(fam, choice("ema_crossover", trailing=tr, direction=direction))
                    self.assertEqual(DT.validate_day_trading(defn), [])
                    self.assertIn("trailing", defn["exit"])
                    cd = compile_definition(defn, {})
                    self.assertIn("trailing", cd.logic["exit"])
                    seen.add(ident["logic_hash"])
        none = FX.validate_candidate(fam, choice("ema_crossover"))[1]["logic_hash"]
        self.assertNotIn(none, seen)
        self.assertEqual(len(seen), 24)

    def test_trailing_changes_identity_and_is_not_forced_to_none(self):
        res = FX.generate(seed=9, quotas={f.fid: 8 for f in S.FAMILIES})
        kinds = res.header["distributions"]["trailing_type"]
        self.assertGreater(len(kinds), 5, kinds)
        self.assertGreater(sum(v for k, v in kinds.items() if k != "none"), 60)
        acts = res.header["distributions"]["trailing_activation"]
        self.assertTrue({"immediate", "r", "atr", "points"} <= set(acts), acts)
        self.assertEqual(len({r["logic_hash"] for r in res.strategies}), len(res.strategies))
        for r in res.strategies:
            d = r["definition"]
            self.assertEqual("trailing" in d["exit"], r["tags"]["trailing_type"] != "none")
            self.assertEqual(DT.validate_day_trading(d), [])

    def test_trailing_equals_its_fixed_twin_only_in_logic_not_in_identity(self):
        fam = S.FAMILY_BY_ID["ema_crossover"]
        a = FX.validate_candidate(fam, choice("ema_crossover"))[1]["logic_hash"]
        b = FX.validate_candidate(fam, choice("ema_crossover", trailing=trail("atr", multiple=2.0)))[1]["logic_hash"]
        c = FX.validate_candidate(fam, choice("ema_crossover", trailing=trail("atr", multiple=3.0)))[1]["logic_hash"]
        self.assertEqual(len({a, b, c}), 3)

    def test_sampled_trailing_strategies_trade_inside_one_trading_date(self):
        res = FX.generate(seed=21, quotas={"ema_crossover": 30, "donchian_breakout": 30})
        cand = [r for r in res.strategies if r["tags"]["trailing_type"] != "none" and r["tags"]["timeframe"] == "5m"]
        self.assertGreater(len(cand), 5)
        ds = synthetic(5)
        cfg = copy.deepcopy(CFG["backtest"])
        cfg["session"] = {"flatten_daily": False, "hold_overnight": True}     # the strategy's own flat alone
        cfg["same_bar_policy"] = "conservative"
        td = np.asarray(ds.calendar.trading_dates(ds.bars.ts))
        trades = 0
        for r in cand[:12]:
            st = compile_strategy(r["definition"], {}).bind(FeatureContext(ds, {}))
            res_bt = run_backtest(ds, st, ZERO_COSTS, cfg, sizing=st.sizing, contract=INSTRUMENTS["MNQ"])
            self.assertTrue(res_bt.causality.passed, r["strategy_id"])
            t = res_bt.trades
            if len(t):
                trades += len(t)
                np.testing.assert_array_equal(td[t.entry_bar.values], td[t.exit_bar.values], r["strategy_id"])
                self.assertTrue((t.holding_minutes < 23 * 60).all())
        self.assertGreater(trades, 0)


if __name__ == "__main__":
    unittest.main()
