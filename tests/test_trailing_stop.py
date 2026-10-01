"""ADR-61: trailing / breakeven stops through the real execution kernel, DSL and compiler.

Kernel tests use hand-computed bars (every expected price is derived in the comment beside it).
SYNTHETIC data only; nothing here is a market result."""
import copy
import math
import unittest

import numpy as np
import pandas as pd

from edgelab.data.synthetic import bars_from_ohlc, generate_bars
from edgelab.data.validation import validate_and_freeze
from edgelab.engine.backtester import run_backtest
from edgelab.engine.costs import CostModel
from edgelab.engine.fills import Entry, FillPolicy, MarketArrays, simulate_exit, simulate_exit_trailing
from edgelab.engine.signals import OrderSpec, SignalSet, Strategy, TrailSpec, check_causality
from edgelab.features.strategy_api import FeatureContext
from edgelab.strategy.compiler import compile_definition, compile_strategy
from edgelab.strategy.dsl import canonical_logic, identity, load_definition, resolve, validate
from tests.helpers import CFG, CME, INSTRUMENTS, NQ, UTC247, ZERO_COSTS, bt_cfg, dataset

NAN = math.nan
POL = FillPolicy()


def arrays(rows, force=(), entry_allowed=True):
    o, h, l, c = (np.array(x, float) for x in zip(*rows))
    n = len(rows)
    f = np.zeros(n, bool)
    for i in force:
        f[i] = True
    return MarketArrays(o, h, l, c, f, np.full(n, entry_allowed), np.zeros(n, np.int64))


def run(rows, d=1, stop=95.0, target=NAN, trail=None, atr=None, level=None, fill=100.0, kind="open",
        force=(), max_hold=None, time_bars=None, sx=None, pol=POL, entry_bar=1):
    A = arrays(rows, force)
    e = Entry(True, "", entry_bar, fill, kind, "market" if kind == "open" else "limit", entry_bar)
    return simulate_exit_trailing(A, None, pol, e, d, stop, target, time_bars, max_hold, "market", NAN,
                                  trail or TrailSpec(mode="distance", distance=5.0), atr, level, 0,
                                  signal_exit_bar=sx)


FLAT = (100, 100, 100, 100)
# long, fill 100 at the open of bar 1, initial stop 95
UP = [FLAT, (100, 102, 99, 101), (101, 104, 100, 103)]


def T(**kw):
    return TrailSpec(mode=kw.pop("mode", "distance"), **({"distance": 5.0} if "distance" not in kw and
                                                           kw.get("mode", "distance") == "distance" else {}), **kw)


class TestKernelSemantics(unittest.TestCase):
    def test_distance_trail_ratchets_and_exits_on_the_trailed_stop(self):
        # bar1 ext 102 -> stop 97 for bar2; bar2 ext 104 -> stop 99 for bar3; bar3 low 98.5 <= 99 -> exit AT 99
        r = run(UP + [(103, 103.5, 98.5, 99)], trail=T())
        self.assertEqual((r["exit_bar"], r["exit_price_theo"], r["exit_reason"]), (3, 99.0, "TRAIL_STOP"))
        self.assertEqual((r["final_stop"], r["trail_updates"]), (99.0, 2))

    def test_stop_never_moves_backward(self):
        # after bar2 the stop is 99; bar3 makes no new high (candidate 99 is not an improvement) and price
        # sags; the stop stays 99 and bar4 (low 98.9) exits at 99, not at a lower re-computed level
        rows = UP + [(103, 103, 100, 100.5), (100.5, 101, 98.9, 99.2)]
        r = run(rows, trail=T())
        self.assertEqual((r["exit_bar"], r["exit_price_theo"], r["final_stop"]), (4, 99.0, 99.0))
        # a later LOWER candidate (shrinking ATR distance growth) can never loosen: level mode with a falling level
        lvl = np.array([NAN, 99.0, 98.0, 96.0, 90.0, 90.0])
        r = run(rows + [(99, 99, 99, 99)], trail=TrailSpec(mode="level"), level=lvl)
        self.assertEqual(r["final_stop"], 99.0)

    def test_bar_extreme_cannot_raise_the_stop_of_its_own_bar(self):
        # bar1 closes 101.5, ext 102 -> stop 101 from bar2. Bar2's 110 high cannot rescue bar2: its low 100.5 <= 101
        rows = [FLAT, (100, 102, 99, 101.5), (101.5, 110, 100.5, 109)]
        r = run(rows, trail=T(distance=1.0))
        self.assertEqual((r["exit_bar"], r["exit_price_theo"], r["exit_reason"]), (2, 101.0, "TRAIL_STOP"))

    def test_candidate_not_below_the_close_is_ignored(self):
        # entry bar: high 110, close 100 -> candidate 105 >= close: ignored, the initial stop stays
        rows = [FLAT, (100, 110, 99, 100), (100, 101, 94, 95)]
        r = run(rows, trail=T())
        self.assertEqual((r["exit_price_theo"], r["exit_reason"], r["trail_updates"]), (95.0, "STOP", 0))

    def test_gap_through_the_trailed_stop_fills_at_the_open(self):
        r = run(UP[:2] + [(96, 97, 95, 96.5)], trail=T())            # stop 97 after bar1; bar2 opens 96
        self.assertEqual((r["exit_bar"], r["exit_price_theo"], r["exit_reason"]), (2, 96.0, "TRAIL_STOP_GAP"))

    def test_untouched_initial_stop_keeps_the_legacy_reason(self):
        r = run([FLAT, (100, 100.5, 99, 100), (100, 100, 94, 95)], trail=T(distance=6.0))
        self.assertEqual((r["exit_price_theo"], r["exit_reason"]), (95.0, "STOP"))

    def test_short_is_the_mirror_image(self):
        rows = [FLAT, (100, 101, 98, 99), (99, 100, 96, 97), (97, 101.5, 97, 101)]
        r = run(rows, d=-1, stop=105.0, trail=T())
        # bar1 ext(low) 98 -> stop 103; bar2 ext 96 -> stop 101; bar3 high 101.5 >= 101 -> exit AT 101
        self.assertEqual((r["exit_bar"], r["exit_price_theo"], r["exit_reason"], r["final_stop"]),
                         (3, 101.0, "TRAIL_STOP", 101.0))
        g = run(rows[:2] + [(104, 104.5, 103, 103.5)], d=-1, stop=105.0, trail=T())   # opens above the 103 stop
        self.assertEqual((g["exit_price_theo"], g["exit_reason"]), (104.0, "TRAIL_STOP_GAP"))

    def test_activation_after_points_r_or_atr_delays_trailing(self):
        rows = [FLAT, (100, 102, 99, 101), (101, 104, 96.5, 103), (103, 103, 98.5, 99)]
        immediate = run(rows, trail=T())                              # stop 97 after bar1 -> bar2 low 96.5 exits
        self.assertEqual((immediate["exit_bar"], immediate["exit_price_theo"]), (2, 97.0))
        atr = np.full(len(rows), 2.0)
        for trail in (T(activation_kind="points", activation=4.0),
                      T(activation_kind="r", activation=0.8),                           # 0.8 x |100-95| = 4
                      T(activation_kind="atr", activation=2.0)):                        # 2.0 x ATR(2) = 4
            r = run(rows, trail=trail, atr=atr)
            # profit reaches 4 only at the bar2 close (ext 104): stop stays 95 through bar2, then 99; bar3 exits
            self.assertEqual((r["exit_bar"], r["exit_price_theo"], r["trail_updates"]), (3, 99.0, 1), trail)
        r = run(rows, trail=T(distance_kind="atr", distance=2.5), atr=atr)           # 2.5 x 2 = 5 points
        self.assertEqual((r["exit_bar"], r["exit_price_theo"]), (2, 97.0))

    def test_breakeven_with_offset_then_normal_trailing(self):
        rows = UP + [(103, 103, 100.5, 101.2)]
        be = TrailSpec(mode="breakeven", be_kind="points", be_trigger=3.0, be_offset=1.0)
        r = run(rows, trail=be)           # profit 4 at the bar2 close -> stop 101 (fill + 1) for bar3; low 100.5 exits
        self.assertEqual((r["exit_bar"], r["exit_price_theo"], r["exit_reason"]), (3, 101.0, "TRAIL_STOP"))
        # breakeven BEFORE normal trailing: the distance trail activates only at 8 points, breakeven at 3 -> stop 100
        both = TrailSpec(mode="distance", distance=5.0, activation_kind="points", activation=8.0, be_kind="points",
                         be_trigger=3.0, be_offset=0.0)
        r = run(UP + [(103, 103, 99.5, 100.2)], trail=both)
        self.assertEqual((r["exit_bar"], r["exit_price_theo"], r["trail_updates"]), (3, 100.0, 1))
        r = run(UP[:2] + [(101, 101.5, 100.2, 101)], trail=be)       # 100.2 > 101? no: trigger not met (profit 2)
        self.assertEqual(r["trail_updates"], 0)

    def test_level_mode_follows_the_strategy_level(self):
        rows = UP + [(103, 103.5, 99.8, 100.2)]
        level = np.array([x[2] for x in rows], float)                 # previous-bar-low trailing
        r = run(rows, trail=TrailSpec(mode="level"), level=level)
        # bar1 low 99 -> stop 99; bar2 low 100 -> stop 100; bar3 low 99.8 <= 100 -> exit at 100
        self.assertEqual((r["exit_bar"], r["exit_price_theo"], r["trail_updates"]), (3, 100.0, 2))

    def test_update_frequency_and_step_rules(self):
        rows = [FLAT, (100, 102, 99, 101), (101, 104, 96.5, 103), (103, 103, 98.5, 99)]
        every1 = run(rows, trail=T())
        every2 = run(rows, trail=T(every_bars=2))            # bar1 (n=1) skipped: stop 95 through bar2, then 99
        self.assertEqual((every1["exit_bar"], every2["exit_bar"], every2["exit_price_theo"]), (2, 3, 99.0))
        step = run(rows, trail=T(min_step=3.0))              # 97 improves by 2 (< 3) -> skipped; 99 by 4 -> taken
        self.assertEqual((step["exit_bar"], step["final_stop"], step["trail_updates"]), (3, 99.0, 1))
        flat_rows = [FLAT, (100, 102, 99, 101), (101, 101.5, 100.5, 101), (101, 101.5, 100.5, 101)]
        lvl = np.array([NAN, 96.0, 98.0, 99.0])
        a = run(flat_rows, trail=TrailSpec(mode="level"), level=lvl, max_hold=3)
        b = run(flat_rows, trail=TrailSpec(mode="level", only_new_extreme=True), level=lvl, max_hold=3)
        # bars 2 and 3 set no new high: the level keeps rising only when new extremes are not required
        self.assertEqual((a["final_stop"], b["final_stop"]), (98.0, 96.0))

    def test_level_fill_does_not_count_the_entry_bars_pre_fill_extreme(self):
        rows = [FLAT, (101, 106, 99, 105.5), (105.5, 106, 105, 105.6)]
        opened = run(rows, trail=T(), kind="open", max_hold=2)
        level = run(rows, trail=T(), kind="level", max_hold=2)
        self.assertEqual(opened["final_stop"], 101.0)        # ext 106 - 5
        self.assertEqual(level["final_stop"], 95.0)          # ext starts at the fill (100): candidate 95 never improves

    def test_target_interaction_and_conflict_policy(self):
        rows = UP + [(103, 106, 98.5, 100)]                  # bar3 touches the trailed stop 99 AND the 105 target
        cons = run(rows, target=105.0, trail=T())
        self.assertEqual((cons["exit_reason"], cons["exit_price_theo"], cons["conflict_resolution"]),
                         ("TRAIL_STOP", 99.0, "CONSERVATIVE"))
        opt = run(rows, target=105.0, trail=T(), pol=FillPolicy(same_bar="optimistic"))
        self.assertEqual((opt["exit_reason"], opt["exit_price_theo"]), ("TARGET", 105.0))
        clean = run(UP + [(103, 106, 100, 105.5)], target=105.0, trail=T())
        self.assertEqual((clean["exit_reason"], clean["exit_price_theo"]), ("TARGET", 105.0))

    def test_time_exit_max_hold_and_forced_close_keep_their_precedence(self):
        rows = UP + [(103, 103.2, 100, 102), (102, 102, 101, 101.5)]
        self.assertEqual(run(rows, trail=T(), force=(3,))["exit_reason"], "SESSION_CLOSE")
        self.assertEqual(run(rows, trail=T(), force=(3,))["exit_price_theo"], 102.0)
        self.assertEqual(run(rows, trail=T(), max_hold=3)["exit_reason"], "MAX_HOLD")
        self.assertEqual(run(rows, trail=T(), time_bars=3)["exit_reason"], "TIME")
        # a stop touch on the forced-close bar still exits at the (trailed) stop: stops are resolved first
        touched = UP + [(103, 103.2, 98, 99)]
        r = run(touched, trail=T(), force=(3,))
        self.assertEqual((r["exit_reason"], r["exit_price_theo"]), ("TRAIL_STOP", 99.0))

    def test_signal_exit_executes_at_the_open_against_the_trailed_stop(self):
        rows = UP + [(103, 103.5, 100, 101), (102, 103, 101, 102)]
        r = run(rows, trail=T(), sx=4)                        # stop 99 after bar2; bar4 opens 102 -> SIGNAL at 102
        self.assertEqual((r["exit_bar"], r["exit_price_theo"], r["exit_reason"]), (4, 102.0, "SIGNAL"))
        gap = UP + [(103, 103.5, 100, 101), (98, 99, 97, 98)]
        r = run(gap, trail=T(), sx=4)                         # bar4 opens 98 below the trailed stop 99
        self.assertEqual((r["exit_price_theo"], r["exit_reason"]), (98.0, "TRAIL_STOP_GAP"))

    def test_exit_side_arrays_drive_the_extreme(self):
        """The kernel trades on the arrays it is handed: for a directional short these are the ASK arrays."""
        ask = [FLAT, (100, 101, 98, 99), (99.6, 100.6, 99.2, 99.8)]
        bid = [(x[0] - 1, x[1] - 1, x[2] - 1, x[3] - 1) for x in ask]
        on_ask = run(ask, d=-1, stop=105.0, trail=T(), max_hold=2)
        on_bid = run(bid, d=-1, stop=105.0, trail=T(), max_hold=2)
        self.assertEqual(on_ask["final_stop"], 103.0)         # ASK low 98 + 5
        self.assertEqual(on_bid["final_stop"], 102.0)         # BID low 97 + 5

    def test_a_trail_that_never_moves_equals_the_legacy_fixed_stop_kernel(self):
        """Differential test: distance 1e9 never improves the stop, so every outcome must equal simulate_exit."""
        rng = np.random.default_rng(7)
        n = 400
        c = 100 + np.cumsum(rng.normal(0, 0.6, n))
        o = np.r_[c[0], c[:-1]] + rng.normal(0, 0.15, n)
        h = np.maximum(o, c) + rng.random(n) * 0.5
        l = np.minimum(o, c) - rng.random(n) * 0.5
        force = np.zeros(n, bool)
        force[::97] = True
        A = MarketArrays(o, h, l, c, force, np.ones(n, bool), np.zeros(n, np.int64))
        never = TrailSpec(mode="distance", distance=1e9)
        sx_all = [None, 9, 30]
        checked = 0
        for i in range(2, n - 60, 7):
            for d in (1, -1):
                e = Entry(True, "", i + 1, float(o[i + 1]), "open", "market", i + 1)
                stop = e.price - d * 1.5
                target = e.price + d * 2.5 if i % 2 else NAN
                for sx in sx_all:
                    for mh in (None, 12):
                        sxb = None if sx is None else e.bar + sx
                        a = simulate_exit(A, None, POL, e, d, stop, target, None, mh, "market", NAN, signal_exit_bar=sxb)
                        b = simulate_exit_trailing(A, None, POL, e, d, stop, target, None, mh, "market", NAN, never,
                                                   None, None, i, signal_exit_bar=sxb)
                        for k in ("exit_bar", "exit_price_theo", "exit_reason", "conflict_resolution", "mfe_points",
                                  "mae_points"):
                            self.assertEqual(a[k], b[k], (i, d, sx, mh, k))
                        self.assertEqual(b["trail_updates"], 0)
                        checked += 1
        self.assertGreater(checked, 300)

    def test_spec_validation(self):
        for kw in ({"mode": "distance"}, {"mode": "bogus"}, {"mode": "breakeven"}, {"mode": "distance", "distance": 1,
                   "every_bars": 0}, {"mode": "distance", "distance": 1, "activation_kind": "points"},
                   {"mode": "distance", "distance": 1, "be_kind": "points"}, {"mode": "level", "min_step": -1},
                   {"mode": "distance", "distance": 1, "be_offset": -1}):
            with self.assertRaises(ValueError, msg=kw):
                TrailSpec(**kw)


# ===================================================================================== engine integration
class Scripted(Strategy):
    family = "scripted_trail"

    def __init__(self, order, signals, stop, trail, atr=None, level=None):
        super().__init__(order, signals={str(k): v for k, v in signals.items()}, stop=stop, trail=repr(trail))
        self._sig, self._stop, self._trail, self._atr, self._level = signals, stop, trail, atr, level

    def generate_signals(self, bars):
        s = SignalSet.empty(len(bars))
        for i, d in self._sig.items():
            if i < len(bars):
                s.direction[i], s.stop_price[i] = d, self._stop
        s.trail = self._trail
        if self._atr is not None:
            s.trail_atr = np.full(len(bars), self._atr)
        if self._level is not None:
            s.trail_level_long = self._level(bars)
        return s


class TestEngineIntegration(unittest.TestCase):
    def test_trailing_trade_flows_through_the_backtester(self):
        ds = dataset(UP + [(103, 103.5, 98.5, 99), (99, 99, 98, 98)])
        res = run_backtest(ds, Scripted(OrderSpec("market"), {0: 1}, 95.0, TrailSpec(mode="distance", distance=5.0)),
                           ZERO_COSTS, bt_cfg())
        t = res.trades.iloc[0]
        self.assertEqual((t.exit_reason, t.exit_price_theo, t.final_stop_price, t.trail_updates),
                         ("TRAIL_STOP", 99.0, 99.0, 2))
        self.assertEqual(t.risk_points, 5.0)                   # R stays relative to the INITIAL stop
        self.assertAlmostEqual(t.gross_r, -0.2)
        self.assertIn("trailing_stop", res.assumptions)
        plain = run_backtest(ds, Scripted(OrderSpec("market"), {0: 1}, 95.0, TrailSpec(mode="distance", distance=1e9)),
                             ZERO_COSTS, bt_cfg())
        self.assertEqual(plain.trades.iloc[0].exit_reason, "END_OF_DATA")

    def test_directional_quotes_long_exits_on_bid_after_an_ask_fill(self):
        cfd = INSTRUMENTS["NAS100_CFD"]
        bid = [FLAT, (100, 102, 99, 101), (101, 102, 98.8, 99.5), (99.5, 100, 99, 99.5)]
        df = bars_from_ohlc(bid)
        s = 1.0
        df["spread"] = s
        df["ask_open"], df["ask_high"], df["ask_low"], df["ask_close"] = (df["open"] + s, df["high"] + s + 0.25,
                                                                         df["low"] + s, df["close"] + s)
        ds = validate_and_freeze(df, cfd, UTC247, "1m", 1, "test", "TRQ", asset_type="CFD")
        strat = Scripted(OrderSpec("market"), {0: 1}, 96.0, TrailSpec(mode="distance", distance=3.0))
        res = run_backtest(ds, strat, CostModel(spread_source="quotes"), bt_cfg(), sizing={"mode": "fixed", "contracts": 1})
        t = res.trades.iloc[0]
        self.assertEqual(t.entry_price_theo, 101.0)            # ASK open of bar1
        # extreme on the BID highs: max(fill 101, bid high 102) = 102 -> stop 99; bar2 BID low 98.8 <= 99 -> exit at 99 on BID
        self.assertEqual((t.exit_reason, t.exit_price_theo, t.exit_quote_side), ("TRAIL_STOP", 99.0, "bid"))

    def test_trailing_arrays_are_checked_by_the_truncation_test(self):
        df, _ = generate_bars(CME, "2024-03-04", "2024-03-08", tf_minutes=5, seed=3)
        ds = validate_and_freeze(df, NQ, CME, "5m", 5, "synthetic", "TRC", volume_type="synthetic")

        class Clean(Scripted):
            pass

        ok = Clean(OrderSpec("market"), {50: 1}, 1.0, TrailSpec(mode="level"), level=lambda b: b.low.astype(float))
        self.assertTrue(check_causality(ok, ds.bars, n_cuts=6).passed)
        leaky = Clean(OrderSpec("market"), {50: 1}, 1.0, TrailSpec(mode="level"),
                      level=lambda b: np.r_[b.low[1:], np.nan].astype(float))        # next bar's low
        rep = check_causality(leaky, ds.bars, n_cuts=6)
        self.assertFalse(rep.passed)
        self.assertIn("trailing", rep.detail)


# ===================================================================================== DSL / compiler
BASE = """
dsl_version: 1
name: trail_t
timeframe: 5m
sessions:
  ENT: {timezone: America/New_York, start: "09:30", end: "11:30"}
  HLD: {timezone: America/New_York, start: "09:30", end: "15:55"}
entry:
  direction: long
  session: ENT
  long: {left: {bar: close}, op: crosses_above, right: {feature: ema, params: {period: 20}, output: ema}}
exit:
  stop: {type: atr, multiple: 2.0, period: 14}
  max_hold_bars: 60
  signal:
    long: {left: {feature: session, params: {session: HLD}, output: in_session}, op: "==", right: 0}
sizing: {mode: fixed, quantity: 1}
"""


def doc(**trailing):
    d = load_definition(BASE)
    if trailing:
        d["exit"]["trailing"] = trailing
    return d


def errs(d):
    return {i.path: i.message for i in validate(d, {}).errors}


class TestDsl(unittest.TestCase):
    def test_valid_declarations(self):
        for tr in ({"mode": "distance", "distance": {"type": "points", "points": 10}},
                   {"mode": "distance", "distance": {"type": "atr", "multiple": 2.0}, "atr_period": 14,
                    "activation": {"type": "r", "value": 1.0}, "update": {"every_bars": 2, "only_new_extreme": True}},
                   {"mode": "level", "level": {"long": {"bar": "low"}}},
                   {"mode": "level", "level": {"long": {"arith": "sub", "args": [{"feature": "ema", "params": {"period": 20},
                                                                                   "output": "ema"}, 1.0]}}},
                   {"mode": "breakeven", "breakeven": {"trigger": {"type": "points", "value": 8}, "offset_points": 1}},
                   {"mode": "distance", "distance": {"type": "points", "points": 10},
                    "breakeven": {"trigger": {"type": "atr", "value": 1.0}}, "atr_period": 10}):
            self.assertEqual(errs(doc(**tr)), {}, tr)

    def test_invalid_declarations_are_refused_by_name(self):
        bad = [({"mode": "zigzag"}, "exit.trailing.mode"),
               ({"mode": "distance"}, "exit.trailing.distance"),
               ({"mode": "distance", "distance": {"type": "points", "points": 0}}, "exit.trailing.distance.points"),
               ({"mode": "level"}, "exit.trailing.level"),
               ({"mode": "level", "level": {"short": {"bar": "high"}}}, "exit.trailing.level.short"),
               ({"mode": "level", "level": {"long": {"bar": "low", "lag": -1}}}, "exit.trailing.level.long.lag"),
               ({"mode": "distance", "distance": {"type": "points", "points": 5}, "activation": {"type": "points"}},
                "exit.trailing.activation.value"),
               ({"mode": "distance", "distance": {"type": "points", "points": 5}, "atr_period": 14},
                "exit.trailing.atr_period"),
               ({"mode": "breakeven"}, "exit.trailing.breakeven"),
               ({"mode": "breakeven", "breakeven": {"trigger": {"type": "points", "value": 5}},
                 "activation": {"type": "points", "value": 3}}, "exit.trailing.activation"),
               ({"mode": "distance", "distance": {"type": "points", "points": 5}, "update": {"every_bars": 0}},
                "exit.trailing.update.every_bars"),
               ({"mode": "distance", "distance": {"type": "points", "points": 5}, "loosen": True}, "exit.trailing.loosen")]
        for tr, path in bad:
            self.assertIn(path, errs(doc(**tr)), tr)

    def test_legacy_top_level_keys_are_still_refused(self):
        for key in ("trailing_stop", "breakeven"):
            d = doc()
            d["exit"][key] = {"x": 1}
            e = validate(d, {}).errors[0]
            self.assertEqual(e.path, f"exit.{key}")
            self.assertIn("not supported", e.message)

    def test_identity_is_unchanged_without_trailing_and_changes_with_it(self):
        d0 = doc()
        canon = canonical_logic(resolve(d0), {})
        self.assertNotIn("trailing", canon["exit"])
        d1 = doc(mode="distance", distance={"type": "points", "points": 10})
        d2 = doc(mode="distance", distance={"type": "points", "points": 12})
        ids = {identity(x, {}).logic_hash for x in (d0, d1, d2)}
        self.assertEqual(len(ids), 3)

    def test_cosmetic_trailing_forms_share_one_identity(self):
        a = doc(mode="distance", distance={"type": "points", "points": 10})
        b = doc(mode="distance", distance={"type": "points", "points": 10.0}, activation={"type": "immediate"},
                update={"every_bars": 1, "only_new_extreme": False, "min_step_points": 0})
        self.assertEqual(identity(a, {}).logic_hash, identity(b, {}).logic_hash)
        c = doc(mode="breakeven", breakeven={"trigger": {"type": "points", "value": 5}})
        d = doc(mode="breakeven", breakeven={"trigger": {"type": "points", "value": 5}, "offset_points": 0})
        self.assertEqual(identity(c, {}).logic_hash, identity(d, {}).logic_hash)

    def test_compiled_strategy_emits_causal_trailing_arrays(self):
        df, _ = generate_bars(CME, "2024-03-04", "2024-03-12", tf_minutes=5, seed=3)
        ds = validate_and_freeze(df, NQ, CME, "5m", 5, "synthetic", "TRD", volume_type="synthetic")
        d = doc(mode="level", level={"long": {"arith": "sub", "args": [{"feature": "ema", "params": {"period": 20},
                                                                         "output": "ema"}, 1.0]}},
                activation={"type": "atr", "value": 1.0}, atr_period=10)
        cd = compile_definition(d, {})
        self.assertIn("atr(period=10)", [s.label for s in cd.feature_specs])
        st = compile_strategy(d, {}).bind(FeatureContext(ds, {}))
        sig = st.generate_signals(ds.bars)
        self.assertEqual(sig.trail.mode, "level")
        self.assertEqual(sig.trail.activation_kind, "atr")
        self.assertEqual(len(sig.trail_atr), len(ds.bars))
        self.assertIsNone(sig.trail_level_short)
        self.assertTrue(check_causality(st, ds.bars, n_cuts=6).passed)

    def test_backtest_keeps_trailing_trades_inside_their_trading_date(self):
        df, _ = generate_bars(CME, "2024-03-04", "2024-03-22", tf_minutes=5, seed=3)
        ds = validate_and_freeze(df, NQ, CME, "5m", 5, "synthetic", "TRE", volume_type="synthetic")
        cfg = copy.deepcopy(CFG["backtest"])
        cfg["session"] = {"flatten_daily": False, "hold_overnight": True}     # the strategy's own flat alone
        cfg["same_bar_policy"] = "conservative"
        d = doc(mode="distance", distance={"type": "atr", "multiple": 1.0}, atr_period=14)
        d["entry"]["direction"] = "both"
        d["entry"]["short"] = {"left": {"bar": "close"}, "op": "crosses_below",
                               "right": {"feature": "ema", "params": {"period": 20}, "output": "ema"}}
        d["exit"]["signal"]["short"] = copy.deepcopy(d["exit"]["signal"]["long"])
        st = compile_strategy(d, {}).bind(FeatureContext(ds, {}))
        res = run_backtest(ds, st, ZERO_COSTS, cfg, sizing=st.sizing)
        t = res.trades
        self.assertGreater(len(t), 5)
        self.assertIn("TRAIL_STOP", set(t.exit_reason))
        td = np.asarray(ds.calendar.trading_dates(ds.bars.ts))
        np.testing.assert_array_equal(td[t.entry_bar.values], td[t.exit_bar.values])
        self.assertTrue((t.holding_minutes < 23 * 60).all())
        self.assertTrue((t.trail_updates >= 0).all())


if __name__ == "__main__":
    unittest.main()
