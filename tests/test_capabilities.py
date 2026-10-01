"""ADR-62: capability audit. Every dimension the factory varies must be executed by the engine; the matrix is
data, its evidence resolves to real code, its document is generated, and each capability added here
(equity-based risk, no-progress exit, per-strategy cap, exit-based re-entry, volume gate) has known answers.
SYNTHETIC data only."""
import copy
import math
import unittest
from pathlib import Path

import numpy as np

from edgelab.data.synthetic import generate_bars
from edgelab.data.validation import validate_and_freeze
from edgelab.engine.backtester import BacktestError, run_backtest
from edgelab.engine.fills import Entry, FillPolicy, MarketArrays, simulate_exit, simulate_exit_trailing
from edgelab.engine.signals import OrderSpec, SignalSet, Strategy, TrailSpec, check_causality
from edgelab.features.engine import FeatureEngine
from edgelab.features.spec import FeatureSpec
from edgelab.features.strategy_api import FeatureContext
from edgelab.strategy import capabilities as CAP
from edgelab.strategy import factory as FX
from edgelab.strategy import factory_space as S
from edgelab.strategy.compiler import compile_definition, compile_strategy
from edgelab.strategy.dsl import identity, load_definition, resolve, canonical_logic, validate
from tests.helpers import CFG, CME, INSTRUMENTS, NQ, UTC247, ZERO_COSTS, bt_cfg, dataset
from tests.phase2_helpers import SESSIONS

REPO = Path(__file__).resolve().parents[1]
NAN = math.nan
POL = FillPolicy()
FLAT = (100, 100, 100, 100)


# ===================================================================================== matrix
class TestMatrix(unittest.TestCase):
    def test_every_row_has_one_valid_status_and_resolvable_evidence(self):
        rows = CAP.matrix()
        self.assertGreaterEqual(len(rows), 25)
        for r in rows:
            self.assertIn(r["status"], CAP.STATUSES, r["dimension"])
            if r["status"] in (CAP.EXEC, CAP.PARTIAL):
                self.assertTrue(r["evidence"], f"{r['dimension']}: an executable claim needs code evidence")
        self.assertEqual(CAP.verify_evidence(), [])
        self.assertEqual(len({r["dimension"] for r in rows}), len(rows))

    def test_document_is_generated_from_the_matrix(self):
        doc = REPO / "FACTORY_CAPABILITIES.md"
        self.assertEqual(doc.read_text(), CAP.render_markdown(),
                         "FACTORY_CAPABILITIES.md is stale: regenerate from edgelab.strategy.capabilities")

    def test_the_six_reported_gaps_are_classified(self):
        by = {r["dimension"]: r["status"] for r in CAP.matrix()}
        self.assertEqual(by["15 Dynamic / equity-based risk sizing"], CAP.EXEC)
        self.assertEqual(by["16b VWAP / volume filters and levels"], CAP.NO_DATA)
        self.assertEqual(by["16c Event / news filters"], CAP.NO_DATA)
        self.assertEqual(by["19a No-progress exit"], CAP.EXEC)
        self.assertEqual(by["18a Per-strategy trade cap"], CAP.EXEC)
        self.assertEqual(by["17b Re-entry: after a trade exit"], CAP.EXEC)

    def test_nothing_declared_unavailable_is_ever_varied(self):
        res = FX.generate(seed=31, quotas={f.fid: 6 for f in S.FAMILIES})
        volume_features = {"vwap", "volume_stats", "rvol_tod"}
        for r in res.strategies:
            feats = {s.feature_id for s in compile_definition(r["definition"], {}).feature_specs}
            self.assertFalse(feats & volume_features, r["strategy_id"])
            t = r["tags"]
            self.assertNotIn(t["regime_filter"], S.NOT_EXECUTABLE["regime"])
            self.assertNotIn(t["trailing_type"], S.NOT_EXECUTABLE["trailing"])
            self.assertNotIn(t["risk_model"], S.NOT_EXECUTABLE["sizing"])
            self.assertNotIn(t["reentry"], S.NOT_EXECUTABLE["frequency"])
            self.assertNotIn("atr_normalized", t["risk_model"])
        hdr = res.header["capability_matrix"]
        self.assertEqual(hdr, CAP.matrix())
        self.assertIn("capability_sha256", res.header["identity"])
        for k in ("sizing_mode", "no_progress", "max_trades_per_day", "reentry"):
            self.assertGreater(len(res.header["distributions"][k]), 1, k)


# ===================================================================================== volume gate
class TestVolumeGate(unittest.TestCase):
    def test_provider_defined_volume_never_feeds_volume_features(self):
        df, _ = generate_bars(CME, "2024-03-04", "2024-03-08", tf_minutes=5, seed=3)
        cfd = INSTRUMENTS["NQ_DUKASCOPY"]
        self.assertIn("NOT CME", (cfd.extra or {}).get("volume_semantics", ""))
        ds = validate_and_freeze(df, cfd, CME, "5m", 5, "synthetic", "VG1", asset_type="CFD", volume_type="unknown")
        eng = FeatureEngine.for_dataset(ds, SESSIONS)
        ok, why = eng.availability(FeatureSpec.make("vwap", {"anchor": "NY_RTH"}))
        self.assertFalse(ok)
        self.assertIn("refused", why)
        self.assertFalse(eng.availability(FeatureSpec.make("volume_stats"))[0])
        self.assertFalse(eng.availability(FeatureSpec.make("rvol_tod"))[0])
        self.assertTrue(eng.availability(FeatureSpec.make("atr"))[0])          # price features are unaffected
        with self.assertRaises(Exception):
            eng.frame([FeatureSpec.make("volume_stats")])
        fut = validate_and_freeze(df, NQ, CME, "5m", 5, "synthetic", "VG2", volume_type="synthetic")
        self.assertTrue(FeatureEngine.for_dataset(fut, SESSIONS).availability(FeatureSpec.make("volume_stats"))[0])


# ===================================================================================== equity sizing
class Seq(Strategy):
    """Scripted signals with an absolute stop 5 points below and target 10 above the signal close, plus the new
    strategy-level trade-management fields."""
    family = "capseq"

    def __init__(self, signals, sizing_tag="", **kw):
        super().__init__(OrderSpec("market"), signals=str(sorted(signals)), tag=sizing_tag, kw=str(sorted(kw.items())))
        self._sig, self._kw = signals, kw

    def generate_signals(self, bars):
        s = SignalSet.empty(len(bars))
        for i in self._sig:
            if i < len(bars):
                s.direction[i] = 1
                s.stop_price[i] = bars.close[i] - 5
                s.target_price[i] = bars.close[i] + 10
        for k, v in self._kw.items():
            setattr(s, k, v)
        return s


T_WIN = (100, 111, 99, 110)       # hits the +10 target
T_LOSS = (100, 100, 94, 95)       # hits the -5 stop
# layout: signal 0 -> entry 1 -> exit 2; signal 3 -> entry 4 -> exit 5; signal 6 -> entry 7 -> exit 8
def layout(o2, o5, o8):
    return [FLAT, FLAT, o2, FLAT, FLAT, o5, FLAT, FLAT, o8, FLAT]


EQ = {"mode": "equity_risk", "risk_pct": 5.0}             # the account size is a RUN parameter, never part of a strategy
ACCT_100K = {"name": "test_100k", "starting_equity": 100_000.0}


def contracts(rows, sizing, account=ACCT_100K, **kw):
    res = run_backtest(dataset(rows), Seq({0, 3, 6}, **kw), ZERO_COSTS, bt_cfg(), sizing=sizing, account=account)
    return res, ([] if res.trades.empty else [float(c) for c in res.trades.contracts])


class TestEquitySizing(unittest.TestCase):
    def test_known_answers_follow_realised_equity(self):
        res, n = contracts(layout(T_WIN, T_LOSS, T_WIN), EQ)
        # T1: 5% x 100,000 = 5,000; risk/contract = 5 pts x $20 = $100 -> 50;  net +10 x 50 x 20 = +10,000
        # T2: 5% x 110,000 = 5,500 -> 55;  net -5 x 55 x 20 = -5,500
        # T3: 5% x 104,500 = 5,225 -> 52.25 -> 52 (rounded DOWN)
        self.assertEqual(n, [50.0, 55.0, 52.0])
        self.assertEqual(list(res.trades.equity_before), [100_000.0, 110_000.0, 104_500.0])
        self.assertAlmostEqual(res.assumptions["equity_sizing"]["final_equity"], 104_500 + 10 * 52 * 20)
        for _, t in res.trades.iterrows():                      # risk comes from the actual INITIAL stop, never above budget
            self.assertLessEqual(t.risk_usd, 0.05 * t.equity_before + 1e-9)
            self.assertGreater(t.risk_usd, 0.05 * t.equity_before - 100.0 - 1e-9)
            self.assertEqual(t.risk_points, 5.0)

    def test_size_depends_only_on_earlier_trades(self):
        _, base = contracts(layout(T_WIN, T_LOSS, T_WIN), EQ)
        _, later_changed = contracts(layout(T_WIN, T_LOSS, T_LOSS), EQ)        # only the LAST trade's outcome differs
        self.assertEqual(base, later_changed)
        _, earlier_changed = contracts(layout(T_WIN, T_WIN, T_WIN), EQ)        # T2 wins instead: T3 must be re-sized
        self.assertEqual(earlier_changed[:2], [50.0, 55.0])
        self.assertEqual(earlier_changed[2], 60.0)                              # 5% x 121,000 = 6,050 / 100
        # the sizing of trade k never sees its own result: rewriting a future bar cannot change an earlier size
        _, edited_future = contracts(layout(T_WIN, T_LOSS, (100, 500, 0.1, 0.1)), EQ)
        self.assertEqual(edited_future[:3], base[:3])

    def test_cap_nonpositive_equity_and_min_size(self):
        _, capped = contracts(layout(T_WIN, T_LOSS, T_WIN), {**EQ, "max_contracts": 40})
        self.assertEqual(capped, [40.0, 40.0, 40.0])
        res, n = contracts(layout(T_WIN, T_LOSS, T_WIN), EQ, account={"starting_equity": 1_000.0})   # 50 / 100 < 1 contract
        self.assertEqual(n, [])
        self.assertEqual(res.skipped.get("SIZE_ZERO"), 3)
        ruin = {**EQ, "risk_pct": 10.0}
        self.assertEqual(contracts(layout(T_WIN, T_LOSS, T_WIN), ruin, account={"starting_equity": 1.0})[1], [])
        with self.assertRaises(BacktestError):
            contracts(layout(T_WIN, T_LOSS, T_WIN), {**EQ, "starting_equity": 100_000.0})        # not a strategy property
        with self.assertRaises(BacktestError):
            contracts(layout(T_WIN, T_LOSS, T_WIN), EQ, account={"starting_equity": 0.0})

    def test_the_default_research_account_is_50k(self):
        from edgelab.engine.sizing import DEFAULT_RESEARCH_ACCOUNT
        self.assertEqual(DEFAULT_RESEARCH_ACCOUNT["starting_equity"], 50_000.0)
        res = run_backtest(dataset(layout(T_WIN, T_LOSS, T_WIN)), Seq({0, 3, 6}), ZERO_COSTS, bt_cfg(), sizing=EQ)
        # 5% x 50,000 = 2,500 / $100 per contract = 25; +10 x 25 x 20 = +5,000 -> 55,000 -> 2,750 / 100 = 27; -5 x 27 x 20 = -2,700
        self.assertEqual(list(res.trades.contracts), [25, 27, 26])      # 5% x 52,300 = 2,615 -> 26 (rounded down)
        self.assertEqual(list(res.trades.equity_before), [50_000.0, 55_000.0, 52_300.0])
        self.assertEqual(res.assumptions["equity_sizing"]["starting_equity"], 50_000.0)
        self.assertEqual(res.assumptions["equity_sizing"]["account"], "research_50k")

    def test_fixed_and_fixed_risk_modes_are_unchanged(self):
        _, fixed = contracts(layout(T_WIN, T_LOSS, T_WIN), {"mode": "fixed", "contracts": 3})
        self.assertEqual(fixed, [3.0, 3.0, 3.0])
        res, risk = contracts(layout(T_WIN, T_LOSS, T_WIN), {"mode": "risk", "risk_usd": 1000.0})
        self.assertEqual(risk, [10.0, 10.0, 10.0])
        self.assertNotIn("equity_before", res.trades.columns)
        self.assertNotIn("equity_sizing", res.assumptions)

    def test_net_of_costs_equity(self):
        from edgelab.engine.costs import CostModel
        costs = CostModel(commission_per_side=10.0)
        res = run_backtest(dataset(layout(T_WIN, T_LOSS, T_WIN)), Seq({0, 3, 6}), costs, bt_cfg(), sizing=EQ)
        t1 = res.trades.iloc[0]
        self.assertAlmostEqual(res.trades.equity_before.iloc[1], 50_000 + t1.net_usd)        # AFTER costs
        self.assertLess(t1.net_usd, t1.gross_usd)

    def test_dsl_and_compiler(self):
        d = load_definition(DSL_BASE)
        d["sizing"] = {"mode": "equity_risk", "risk_pct": 0.5, "max_quantity": 20}
        self.assertTrue(validate(d, {}).valid)
        self.assertEqual(compile_definition(d, {}).sizing, {"mode": "equity_risk", "risk_pct": 0.5, "max_contracts": 20})
        for bad, path in (({"mode": "equity_risk", "risk_pct": 0}, "sizing.risk_pct"),
                          ({"mode": "equity_risk", "risk_pct": 11}, "sizing.risk_pct"),
                          ({"mode": "equity_risk", "risk_pct": 1, "starting_equity": 50000}, "sizing.starting_equity"),
                          ({"mode": "equity_risk", "risk_pct": 1, "risk_usd": 5}, "sizing.risk_usd")):
            d["sizing"] = bad
            self.assertIn(path, {i.path for i in validate(d, {}).errors}, bad)
        a = load_definition(DSL_BASE); a["sizing"] = {"mode": "equity_risk", "risk_pct": 1}
        b = copy.deepcopy(a); b["sizing"]["risk_pct"] = 1.0
        self.assertEqual(identity(a, {}).logic_hash, identity(b, {}).logic_hash)
        b["sizing"]["risk_pct"] = 2.0
        self.assertNotEqual(identity(a, {}).logic_hash, identity(b, {}).logic_hash)
        self.assertNotIn("starting_equity", str(canonical_logic(resolve(a), {})))             # the account never enters identity


# ===================================================================================== no-progress exit
def arrays(rows, force=()):
    o, h, l, c = (np.array(x, float) for x in zip(*rows))
    f = np.zeros(len(rows), bool)
    for i in force:
        f[i] = True
    return MarketArrays(o, h, l, c, f, np.ones(len(rows), bool), np.zeros(len(rows), np.int64))


def npx(rows, trail, d=1, stop=95.0, target=NAN, force=(), atr=None, max_hold=None, fill=100.0):
    e = Entry(True, "", 1, fill, "open", "market", 1)
    return simulate_exit_trailing(arrays(rows, force), None, POL, e, d, stop, target, None, max_hold, "market", NAN,
                                  trail, atr, None, 0)


class TestNoProgress(unittest.TestCase):
    NP = TrailSpec(mode="none", np_bars=3, np_kind="points", np_value=3.0)

    def test_exit_at_the_close_of_bar_n_when_progress_is_short(self):
        rows = [FLAT, (100, 101, 99.5, 100.4), (100.4, 101.5, 99.6, 100.2), (100.2, 101.0, 99.8, 100.6), FLAT, FLAT]
        r = npx(rows, self.NP)              # excursion by bar 3 = 101.5 - 100 = 1.5 < 3: exit at bar 3's CLOSE
        self.assertEqual((r["exit_bar"], r["exit_price_theo"], r["exit_reason"]), (3, 100.6, "NO_PROGRESS"))
        self.assertEqual(r["final_stop"], 95.0)

    def test_enough_progress_keeps_the_trade(self):
        rows = [FLAT, (100, 101, 99.5, 100.4), (100.4, 103.2, 99.6, 103.0), (103, 103.1, 102, 102.5), FLAT, FLAT]
        r = npx(rows, self.NP, max_hold=5)  # 103.2 - 100 = 3.2 >= 3: never a no-progress exit
        self.assertEqual(r["exit_reason"], "MAX_HOLD")

    def test_progress_is_measured_on_the_exit_side_up_to_and_including_bar_n(self):
        rows = [FLAT, (100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100), (100, 103.4, 99.5, 100)]
        r = npx(rows, self.NP)              # bar 3's own high counts: 3.4 >= 3
        self.assertNotEqual(r["exit_reason"], "NO_PROGRESS")
        s = npx([FLAT, (100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100), (100, 100.5, 96.0, 97.0)], self.NP, d=-1,
                stop=105.0)                 # short: lowest low 96.0 -> excursion 4.0 >= 3
        self.assertNotEqual(s["exit_reason"], "NO_PROGRESS")

    def test_units_r_and_atr(self):
        rows = [FLAT, (100, 101, 99.5, 100.4), (100.4, 101.2, 99.6, 100.2), (100.2, 101.0, 99.8, 100.6), FLAT]
        r_ok = npx(rows, TrailSpec(mode="none", np_bars=3, np_kind="r", np_value=0.2), max_hold=4)     # 0.2 x 5 = 1.0 <= 1.2
        r_bad = npx(rows, TrailSpec(mode="none", np_bars=3, np_kind="r", np_value=0.3), max_hold=4)    # 1.5 > 1.2
        self.assertEqual((r_ok["exit_reason"], r_bad["exit_reason"]), ("MAX_HOLD", "NO_PROGRESS"))
        atr = np.full(5, 0.5)
        a_bad = npx(rows, TrailSpec(mode="none", np_bars=3, np_kind="atr", np_value=3.0), atr=atr, max_hold=4)   # 1.5 > 1.2
        self.assertEqual(a_bad["exit_reason"], "NO_PROGRESS")

    def test_precedence_stop_and_forced_close_come_first(self):
        rows = [FLAT, (100, 101, 99.5, 100.4), (100.4, 101.5, 99.6, 100.2), (100.2, 101.0, 94.0, 100.6), FLAT]
        self.assertEqual(npx(rows, self.NP)["exit_reason"], "STOP")                    # the stop touch on bar 3 wins
        rows2 = [FLAT, (100, 101, 99.5, 100.4), (100.4, 101.5, 99.6, 100.2), (100.2, 101.0, 99.8, 100.6), FLAT]
        self.assertEqual(npx(rows2, self.NP, force=(3,))["exit_reason"], "SESSION_CLOSE")
        self.assertEqual(npx(rows2, self.NP, max_hold=3)["exit_reason"], "MAX_HOLD")

    def test_combines_with_trailing_and_with_a_never_binding_check_equals_the_legacy_kernel(self):
        both = TrailSpec(mode="distance", distance=5.0, np_bars=3, np_kind="points", np_value=3.0)
        rows = [FLAT, (100, 102, 99, 101), (101, 104, 100, 103), (103, 103.5, 98.5, 99)]
        self.assertEqual(npx(rows, both)["exit_reason"], "TRAIL_STOP")                # progress 4 >= 3: trailing runs on
        rng = np.random.default_rng(3)
        n = 300
        c = 100 + np.cumsum(rng.normal(0, 0.6, n))
        o = np.r_[c[0], c[:-1]] + rng.normal(0, 0.15, n)
        h, l = np.maximum(o, c) + rng.random(n) * 0.5, np.minimum(o, c) - rng.random(n) * 0.5
        A = MarketArrays(o, h, l, c, np.zeros(n, bool), np.ones(n, bool), np.zeros(n, np.int64))
        idle = TrailSpec(mode="none", np_bars=10 ** 6, np_kind="points", np_value=1.0)
        for i in range(2, n - 50, 9):
            for d in (1, -1):
                e = Entry(True, "", i + 1, float(o[i + 1]), "open", "market", i + 1)
                a = simulate_exit(A, None, POL, e, d, e.price - d * 1.5, e.price + d * 2.5, None, 20, "market", NAN)
                b = simulate_exit_trailing(A, None, POL, e, d, e.price - d * 1.5, e.price + d * 2.5, None, 20, "market",
                                           NAN, idle, None, None, i)
                for k in ("exit_bar", "exit_price_theo", "exit_reason", "mfe_points", "mae_points"):
                    self.assertEqual(a[k], b[k], (i, d, k))

    def test_spec_validation_and_backtest_reason(self):
        for kw in ({"mode": "none"}, {"mode": "none", "np_bars": 3}, {"mode": "none", "np_bars": 3, "np_kind": "x",
                   "np_value": 1}, {"mode": "distance", "distance": 1, "np_bars": -1}):
            with self.assertRaises(ValueError, msg=kw):
                TrailSpec(**kw)
        rows = [FLAT, FLAT, (100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100), (100, 100.5, 99.5, 100.1), FLAT, FLAT]

        class S1(Seq):
            def generate_signals(self, bars):
                s = super().generate_signals(bars)
                s.trail = TrailSpec(mode="none", np_bars=2, np_kind="points", np_value=4.0)
                return s
        res = run_backtest(dataset(rows), S1({0}), ZERO_COSTS, bt_cfg(), sizing={"mode": "fixed", "contracts": 1})
        t = res.trades.iloc[0]
        self.assertEqual((t.exit_reason, t.exit_bar), ("NO_PROGRESS", 2))
        self.assertEqual(t.holding_minutes, 2)


# ===================================================================================== caps and re-entry
def days(n_days, per_day=24, tf=60):
    """Hourly UTC bars, one trading date per 24 bars (flat prices)."""
    return [FLAT] * (n_days * per_day)


def put(rows, i, bar):
    rows = list(rows)
    rows[i] = bar
    return rows


class TestTradeManagement(unittest.TestCase):
    def run_ds(self, rows, signals, cfg=None, **kw):
        ds = dataset(rows, tf=60)
        return run_backtest(ds, Seq(set(signals), **kw), ZERO_COSTS, cfg or bt_cfg(), sizing={"mode": "fixed", "contracts": 1})

    def test_strategy_cap_counts_executed_trades_per_trading_date(self):
        rows = days(2)
        for i in (2, 5, 8):
            rows[i] = T_WIN                                           # three quick wins on day 0
        rows[26] = T_WIN                                              # and one on day 1
        res = self.run_ds(rows, (0, 3, 6, 24), max_trades_per_day=2)
        self.assertEqual(list(res.trades.signal_bar), [0, 3, 24])
        self.assertEqual(res.skipped.get("MAX_TRADES_PER_DAY"), 1)
        self.assertEqual(res.assumptions["strategy_trade_management"]["max_trades_per_day"], 2)
        uncapped = self.run_ds(rows, (0, 3, 6, 24))
        self.assertEqual(len(uncapped.trades), 4)
        cfg = bt_cfg(max_trades_per_day=1)                            # the config cap and the strategy cap: the minimum
        self.assertEqual(len(self.run_ds(rows, (0, 3, 6, 24), cfg=cfg, max_trades_per_day=3).trades), 2)

    def test_block_day_after_a_stop_out_only(self):
        rows = days(2)
        rows[2], rows[5], rows[8], rows[26], rows[29] = T_WIN, T_LOSS, T_WIN, T_LOSS, T_WIN
        sig = (0, 3, 6, 24, 27)
        res = self.run_ds(rows, sig, block_after="stop")
        # day 0: win (no block) -> trade 2 stops out -> trade 3 (bar 6) blocked; day 1 starts fresh
        self.assertEqual(list(res.trades.signal_bar), [0, 3, 24])
        self.assertEqual(res.skipped.get("REENTRY_BLOCKED"), 2)      # bar 6 (day 0) and bar 27 (day 1, after its stop)
        tgt = self.run_ds(rows, sig, block_after="target")
        # day 0: the first trade hit its TARGET -> day blocked; day 1: its first trade STOPPED, so a target block
        # does not apply and the later signal (27) trades
        self.assertEqual(list(tgt.trades.signal_bar), [0, 24, 27])
        anyb = self.run_ds(rows, sig, block_after="any")
        self.assertEqual(list(anyb.trades.signal_bar), [0, 24])

    def test_cooldown_after_exit_is_measured_from_the_exit_bar(self):
        rows = days(1)
        rows[2], rows[5], rows[9] = T_WIN, T_WIN, T_WIN
        res = self.run_ds(rows, (0, 3, 4, 6, 9), exit_cooldown_bars=3)
        # trade 0 exits at bar 2: signals 3 (1 bar later) and 4 (2 bars) are inside the 3-bar cooldown; signal 6 (4 bars)
        # trades and exits at bar 9, so signal 9 (0 bars after that exit) is skipped too
        self.assertEqual(list(res.trades.signal_bar), [0, 6])
        self.assertEqual(res.skipped.get("REENTRY_COOLDOWN"), 3)
        self.assertEqual(self.run_ds(rows, (0, 3), exit_cooldown_bars=0).trades.shape[0], 2)

    def test_dsl_declarations_compile_and_keep_identity(self):
        base = load_definition(DSL_BASE)
        d = copy.deepcopy(base)
        d["entry"]["max_trades_per_day"] = 2
        d["entry"]["reentry"] = {"cooldown_bars": 3, "block_day_after": "stop"}
        d["exit"]["no_progress"] = {"bars": 6, "min_progress": {"type": "r", "value": 0.5}}
        self.assertTrue(validate(d, {}).valid, validate(d, {}).report())
        cd = compile_definition(d, {})
        self.assertEqual(cd.logic["entry"]["max_trades_per_day"], 2)
        self.assertEqual(cd.logic["entry"]["reentry"], {"cooldown_bars": 3, "block_day_after": "stop"})
        self.assertEqual(cd.logic["exit"]["no_progress"]["bars"], 6)
        self.assertNotIn("max_trades_per_day", canonical_logic(resolve(base), {})["entry"])       # absent stays absent
        self.assertNotIn("no_progress", canonical_logic(resolve(base), {})["exit"])
        ids = {identity(x, {}).logic_hash for x in (base, d)}
        self.assertEqual(len(ids), 2)
        for path, edit in (("entry.max_trades_per_day", lambda x: x["entry"].__setitem__("max_trades_per_day", 0)),
                           ("entry.reentry", lambda x: x["entry"].__setitem__("reentry", {})),
                           ("entry.reentry.block_day_after", lambda x: x["entry"].__setitem__("reentry", {"block_day_after": "x"})),
                           ("exit.no_progress.bars", lambda x: x["exit"].__setitem__("no_progress", {"bars": 1, "min_progress": {"type": "r", "value": 1}})),
                           ("exit.no_progress.min_progress", lambda x: x["exit"].__setitem__("no_progress", {"bars": 3}))):
            bad = copy.deepcopy(base)
            edit(bad)
            self.assertIn(path, {i.path for i in validate(bad, {}).errors}, path)
        legacy = copy.deepcopy(base)
        legacy["entry"]["cooldown_after_exit"] = {"bars": 3}
        e = validate(legacy, {}).errors[0]
        self.assertEqual(e.path, "entry.cooldown_after_exit")
        self.assertIn("not supported", e.message)
        both = copy.deepcopy(d)
        both["exit"]["trailing"] = {"mode": "distance", "distance": {"type": "atr", "multiple": 2.0}, "atr_period": 10}
        both["exit"]["no_progress"] = {"bars": 6, "min_progress": {"type": "atr", "value": 1.0}, "atr_period": 14}
        self.assertIn("exit.no_progress.atr_period", {i.path for i in validate(both, {}).errors})

    def test_compiled_strategy_is_causal_and_carries_the_fields(self):
        df, _ = generate_bars(CME, "2024-03-04", "2024-03-12", tf_minutes=5, seed=3)
        ds = validate_and_freeze(df, NQ, CME, "5m", 5, "synthetic", "TM1", volume_type="synthetic")
        d = load_definition(DSL_BASE)
        d["entry"]["max_trades_per_day"] = 2
        d["entry"]["reentry"] = {"block_day_after": "target"}
        d["exit"]["no_progress"] = {"bars": 6, "min_progress": {"type": "atr", "value": 1.0}}
        st = compile_strategy(d, {}).bind(FeatureContext(ds, {}))
        sig = st.generate_signals(ds.bars)
        self.assertEqual((sig.max_trades_per_day, sig.exit_cooldown_bars, sig.block_after), (2, 0, "target"))
        self.assertEqual((sig.trail.mode, sig.trail.np_bars, sig.trail.np_kind), ("none", 6, "atr"))
        self.assertEqual(len(sig.trail_atr), len(ds.bars))
        self.assertTrue(check_causality(st, ds.bars, n_cuts=6).passed)


DSL_BASE = """
dsl_version: 1
name: cap_t
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


# ===================================================================================== factory integration
class TestFactoryUsesOnlyRealCapabilities(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = FX.generate(seed=41, quotas={"ema_crossover": 40, "donchian_breakout": 40, "ict_liquidity_fvg": 40})

    def test_new_dimensions_are_sampled_and_reach_the_definition(self):
        seen = {"equity": 0, "no_progress": 0, "cap": 0, "block": 0, "cooldown": 0}
        for r in self.res.strategies:
            d, t = r["definition"], r["tags"]
            seen["equity"] += d["sizing"]["mode"] == "equity_risk"
            seen["no_progress"] += "no_progress" in d["exit"]
            seen["cap"] += "max_trades_per_day" in d["entry"]
            seen["block"] += "block_day_after" in d["entry"].get("reentry", {})
            seen["cooldown"] += "cooldown_bars" in d["entry"].get("reentry", {})
            self.assertEqual(t["sizing_mode"] == "equity_risk", d["sizing"]["mode"] == "equity_risk")
            if d["sizing"]["mode"] == "equity_risk":
                self.assertNotIn("starting_equity", d["sizing"])                 # the account is a run parameter
                self.assertEqual(d["sizing"]["max_quantity"], S.RISK_QUANTITY_CAP)
        self.assertTrue(all(v > 0 for v in seen.values()), seen)

    def test_generated_strategies_with_the_new_capabilities_execute_under_the_invariants(self):
        pick = [r for r in self.res.strategies if r["tags"]["timeframe"] == "5m" and (
            r["tags"]["sizing_mode"] == "equity_risk" or r["tags"]["no_progress"] != "none"
            or r["tags"]["max_trades_per_day"] != "none" or r["tags"]["reentry"] != "none")][:14]
        self.assertGreater(len(pick), 5)
        df, _ = generate_bars(CME, "2024-03-04", "2024-03-22", tf_minutes=5, seed=3)
        ds = validate_and_freeze(df, INSTRUMENTS["NAS100_CFD"], CME, "5m", 5, "synthetic", "TMF", asset_type="CFD",
                                 volume_type="synthetic")
        cfg = copy.deepcopy(CFG["backtest"])
        cfg["session"] = {"flatten_daily": False, "hold_overnight": True}
        cfg["same_bar_policy"] = "conservative"
        td = np.asarray(ds.calendar.trading_dates(ds.bars.ts))
        trades = 0
        for r in pick:
            st = compile_strategy(r["definition"], {}).bind(FeatureContext(ds, {}))
            res = run_backtest(ds, st, ZERO_COSTS, cfg, sizing=st.sizing, contract=INSTRUMENTS["MNQ"])
            self.assertTrue(res.causality.passed, r["strategy_id"])
            t = res.trades
            if len(t):
                trades += len(t)
                np.testing.assert_array_equal(td[t.entry_bar.values], td[t.exit_bar.values], r["strategy_id"])
                self.assertTrue((t.holding_minutes < 23 * 60).all())
                cap = r["definition"]["entry"].get("max_trades_per_day")
                if cap:
                    self.assertLessEqual(int(pd_counts(td[t.entry_bar.values]).max()), cap)
        self.assertGreater(trades, 0)

    def test_htf_breakout_and_prior_day_nr_are_real_filters(self):
        fam = S.FAMILY_BY_ID["ema_crossover"]
        from tests.test_strategy_factory import choice
        d1, _ = FX.validate_candidate(fam, choice("ema_crossover", timeframe="5m",
                                                  mtf={"filter": "htf_breakout", "htf": "30m"}))
        self.assertIn("donchian", {s.feature_id for s in compile_definition(d1, {}).feature_specs})
        d2, _ = FX.validate_candidate(fam, choice("ema_crossover", regime="prior_day_nr"))
        self.assertIn("daily_nr", {s.feature_id for s in compile_definition(d2, {}).feature_specs})


def pd_counts(x):
    _, c = np.unique(x, return_counts=True)
    return c


if __name__ == "__main__":
    unittest.main()
