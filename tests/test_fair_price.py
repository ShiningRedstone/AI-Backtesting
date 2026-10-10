"""Fair price (ADR-114): the "fair pricing theory" strategy from a video transcript, run through the My strategy machinery.

Guarantees tested: settings are validated and hashed; a hand-built day gives exactly the expected break-of-structure short
back to the 9:30 fair price (static 38 / 25 points in evaluations, a 50-point step target sized for the funded dollar win
in funded accounts); every rule variant passes the engine's empirical lookahead check in both phases; the engine's
per-signal risk budget (ADR-114) sizes each signal and is part of the lookahead check, and refuses non-risk sizing; the
phase chain takes evaluation trades until the pass day ends and funded trades after it (never overlapping); the
autotuner never tweaks the fixed settings; a workspace flow counts trials in the Fair price protocol only, records both
phases, spends holdout looks of its own allowance and serves everything under /api/fair. All data is SYNTHETIC."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.engine.backtester import BacktestError, run_backtest
from edgelab.engine.costs import cost_model_from_config
from edgelab.engine.signals import OrderSpec, SignalSet, Strategy, check_causality
from edgelab.fairprice import params as P
from edgelab.fairprice.chain import combined_from, phase_chain
from edgelab.fairprice.logic import News
from edgelab.fairprice.strategy import FairStrategy
from edgelab.instruments import contract_for
from tests.dukascopy_fixture import session_minutes, write_fixture
from tests.helpers import CFG
from tests.test_my_strategy import CAL, DISC, HOLD, REPO, freeze, random_ds

NO_NEWS = News([], None, None, "none")


def known_ds():
    """Flat 18,000 everywhere except 9:30-10:00 New York on 9 Jan 2024: a rise of 6 points a minute from the 9:30 open,
    a swing low at 9:42 (low 18,050), a close below it at 9:44 (18,045), then a fall back to 18,000 by 10:00."""
    ts = session_minutes("2024-01-07", "2024-01-11")
    n = len(ts)
    o = np.full(n, 18000.0)
    h, lo, c = o.copy(), o.copy(), o.copy()
    ny = ts.tz_convert("America/New_York")
    day = ny.normalize() == pd.Timestamp("2024-01-09", tz="America/New_York")
    m = ny.hour * 60 + ny.minute

    def at(hh, mm):
        k = np.flatnonzero(day & (m == hh * 60 + mm))
        return int(k[0])
    p = 18000.0
    for mm in range(30, 41):                                   # 9:30 ... 9:40: green, rising, lows rising
        k = at(9, mm)
        o[k], c[k], h[k], lo[k] = p, p + 6, p + 7, p - 1 if mm > 30 else p
        p += 6
    rows = {41: (18066, 18060, 18067, 18059), 42: (18060, 18063, 18064, 18050), 43: (18063, 18065, 18066, 18061),
            44: (18065, 18045, 18065, 18044)}
    for mm, (a, b, hi, low) in rows.items():
        k = at(9, mm)
        o[k], c[k], h[k], lo[k] = a, b, hi, low
    q = 18045.0
    for mm in range(45, 60):                                   # back down to the fair price
        k = at(9, mm)
        nxt = max(18000.0, q - 3.0)
        o[k], c[k], h[k], lo[k] = q, nxt, q, nxt
        q = nxt
    df = pd.DataFrame({"ts": ts, "open": o, "high": h, "low": lo, "close": c, "volume": 1.0})
    for k, v in (("ask_open", o), ("ask_high", h), ("ask_low", lo), ("ask_close", c)):
        df[k] = v + 0.5
    return freeze(df, "SYN_FAIR"), at


class TestSettings(unittest.TestCase):
    def test_defaults_and_validation(self):
        s = P.resolve()
        self.assertEqual((s["eval.target_points"], s["eval.stop_points"], s["funded.win_usd"]), (38.0, 25.0, 1500.0))
        self.assertEqual(s["session.asia_open"], "20:00")
        self.assertTrue(P.news_used(s))
        with self.assertRaises(P.SettingsError):
            P.resolve({"nope": 1})
        with self.assertRaises(P.SettingsError):
            P.resolve({"session.ny_am": False, "session.ny_pm": False, "session.asia": False, "session.london": False})
        with self.assertRaises(P.SettingsError):
            P.resolve({"funded.max_target": 10, "funded.min_target": 25})
        self.assertEqual(P.settings_hash({}), P.settings_hash(P.changed(s)))
        self.assertNotEqual(P.settings_hash({}), P.settings_hash({"eval.target_points": 40}))
        self.assertNotEqual(P.settings_hash({}), __import__("edgelab.mystrategy.params", fromlist=["x"]).settings_hash({}))


class TestKnownAnswer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds, at = known_ds()
        cls.at = staticmethod(at)
        cls.costs = cost_model_from_config(CFG, cls.ds.instrument.symbol, provider="DUKASCOPY")

    def run_phase(self, phase, over=None):
        st = FairStrategy(over or {}, CAL, phase, news=NO_NEWS)
        res = run_backtest(self.ds, st, self.costs, CFG["backtest"], sizing=st.sizing, contract=contract_for(CFG, st.sizing))
        return st, res

    def test_break_of_structure_short_back_to_fair(self):
        i = self.at(9, 44)
        st, res = self.run_phase("eval")
        self.assertTrue(res.causality.passed)
        sig = st.last_signals
        self.assertEqual(list(np.flatnonzero(sig.direction)), [i])
        self.assertEqual(sig.direction[i], -1)
        self.assertAlmostEqual(sig.stop_price[i], 18045.0 + 25)          # planned entry = BID close (short sells on BID)
        self.assertAlmostEqual(sig.target_price[i], 18045.0 - 38)
        e = st.explanations[i]
        self.assertEqual((e["setup"], e["session"], e["fair"]["price"], e["fair"]["source"]),
                         ("bos", "New York open", 18000.0, "session_open"))
        self.assertAlmostEqual(e["structure"]["price"], 18050.0)
        self.assertEqual(len(res.trades), 1)
        t = res.trades.iloc[0]
        self.assertEqual(t["exit_reason"], "TARGET")
        self.assertEqual(int(t["contracts"]), 10)                          # $500 / (25 points x $2)
        self.assertEqual(st.stats["cont_against_bias"] + st.stats["cont_doji_open"] + st.stats["cont_no_bias_history"]
                         + st.stats["cont_no_structure"] >= 1, True)       # no continuation: no bias on a flat night

    def test_funded_target_from_room_and_dollar_win_sizing(self):
        i = self.at(9, 44)
        st, res = self.run_phase("funded")
        sig = st.last_signals
        self.assertEqual(list(np.flatnonzero(sig.direction)), [i])
        self.assertAlmostEqual(sig.target_price[i], 18045.0 - 50)        # room 45 / 0.8 = 56.25 -> step 50
        self.assertAlmostEqual(sig.risk_usd[i], 1500 * 25 / 50)
        self.assertEqual(int(res.trades.iloc[0]["contracts"]), 15)        # $1,500 / (50 points x $2)
        _, none = self.run_phase("funded", {"funded.bos": False, "funded.displacement": True})
        self.assertEqual(len(none.trades), 1)                              # the 9:44 candle is also a displacement
        st2, res2 = self.run_phase("eval", {"entry.min_distance_share": 1.25})   # 45 < 1.25 x 38: too close
        self.assertEqual(len(res2.trades), 0)
        self.assertGreater(st2.stats["skipped_too_close_to_fair"], 0)


class TestFlip(unittest.TestCase):
    """The user's flip: the same setups traded the other way, stop at the old target, target at the old stop, same micros."""

    @classmethod
    def setUpClass(cls):
        cls.ds, at = known_ds()
        cls.at = staticmethod(at)
        cls.costs = cost_model_from_config(CFG, cls.ds.instrument.symbol, provider="DUKASCOPY")
        cls.pv = contract_for(CFG, {"contract": "MNQ"}).point_value

    def run_phase(self, phase, over):
        st = FairStrategy(over, CAL, phase, news=NO_NEWS, point_value=self.pv)
        res = run_backtest(self.ds, st, self.costs, CFG["backtest"], sizing=st.sizing, contract=contract_for(CFG, st.sizing))
        return st, res

    def test_known_day_flipped(self):
        i = self.at(9, 44)
        for phase, n in (("eval", 10), ("funded", 15)):
            st, res = self.run_phase(phase, {"models.flip": True})
            self.assertTrue(res.causality.passed)
            sig = st.last_signals
            self.assertEqual(list(np.flatnonzero(sig.direction)), [i])
            self.assertEqual(sig.direction[i], 1)                         # the short setup, bought
            tgt = 18045.0 - (38 if phase == "eval" else 50)
            self.assertAlmostEqual(sig.stop_price[i], tgt)                 # stop at the setup's target
            self.assertAlmostEqual(sig.target_price[i], 18045.0 + 25)     # target at the setup's stop
            t = res.trades.iloc[0]
            self.assertEqual(int(t["contracts"]), n)                       # the setup's contracts kept
            # eval: price falls back to 18,000, through the flipped stop at 18,007; funded: the flipped stop (17,995) is
            # never reached, the trade is closed at the session's time exit, below its entry
            self.assertEqual(t["exit_reason"], "STOP" if phase == "eval" else "SIGNAL")
            self.assertLess(float(t["net_r"]), 0)
            e = st.explanations[i]
            self.assertEqual((e["direction"], e["flip"]["setup_direction"], e["flip"]["contracts_kept"]), (1, -1, n))

    def test_same_setups_inverted_and_causal(self):
        ds = random_ds()
        costs = cost_model_from_config(CFG, ds.instrument.symbol, provider="DUKASCOPY")
        for phase in ("eval", "funded"):
            a = FairStrategy({}, CAL, phase, news=NO_NEWS)
            b = FairStrategy({"models.flip": True}, CAL, phase, news=NO_NEWS, point_value=self.pv)
            sa, sb = a.generate_signals(ds.bars), b.generate_signals(ds.bars)
            on = np.flatnonzero(sa.direction)
            self.assertGreater(len(on), 10)
            self.assertTrue(np.array_equal(on, np.flatnonzero(sb.direction)))       # the same setups
            self.assertTrue(np.array_equal(sa.direction[on], -sb.direction[on]))    # inverted
            self.assertTrue(np.allclose(sa.stop_price[on], sb.target_price[on]))    # levels swapped
            self.assertTrue(np.allclose(sa.target_price[on], sb.stop_price[on]))
            self.assertTrue(check_causality(b, ds.bars, n_cuts=8).passed)
            ra = run_backtest(ds, a, costs, CFG["backtest"], sizing=a.sizing, contract=contract_for(CFG, a.sizing))
            rb = run_backtest(ds, b, costs, CFG["backtest"], sizing=b.sizing, contract=contract_for(CFG, b.sizing))
            ka = dict(zip(ra.trades["signal_bar"], ra.trades["contracts"]))
            kb = dict(zip(rb.trades["signal_bar"], rb.trades["contracts"]))
            common = set(ka) & set(kb)
            self.assertGreater(len(common), 10)
            self.assertTrue(all(ka[k] == kb[k] for k in common))                     # same micros
        with self.assertRaises(ValueError):
            FairStrategy({"models.flip": True}, CAL, "eval", news=NO_NEWS)           # needs the point value

    def test_whole_trade_flipped_with_the_same_distances(self):
        i = self.at(9, 44)
        over = {"models.flip": True, "models.flip_levels": "same_distances"}
        for phase, n, T in (("eval", 10, 38), ("funded", 15, 50)):
            st, res = self.run_phase(phase, over)
            self.assertTrue(res.causality.passed)
            sig = st.last_signals
            self.assertEqual(list(np.flatnonzero(sig.direction)), [i])
            self.assertEqual(sig.direction[i], 1)                         # the short setup, bought
            pe = 18045.5                                                   # a long is planned from the ASK close
            self.assertAlmostEqual(sig.stop_price[i], pe - 25)            # the same 25-point stop, below
            self.assertAlmostEqual(sig.target_price[i], pe + T)           # the same target distance, above
            t = res.trades.iloc[0]
            self.assertEqual(int(t["contracts"]), n)                       # normal sizing: same micros
            self.assertEqual(t["exit_reason"], "STOP")                     # price falls back to fair: the long is stopped
            self.assertEqual(st.explanations[i]["flip"]["style"], "same_distances")
        ds = random_ds()
        costs = cost_model_from_config(CFG, ds.instrument.symbol, provider="DUKASCOPY")
        for phase in ("eval", "funded"):
            b = FairStrategy(over, CAL, phase, news=NO_NEWS, point_value=self.pv)
            self.assertTrue(check_causality(b, ds.bars, n_cuts=8).passed)
            res = run_backtest(ds, b, costs, CFG["backtest"], sizing=b.sizing, contract=contract_for(CFG, b.sizing))
            self.assertGreater(len(res.trades), 10)
            self.assertEqual(res.skipped.get("BUSY_IN_POSITION_OR_ORDER", 0), 0)   # own tracking follows the flipped trade
            for k in np.flatnonzero(b.last_signals.direction):
                e = b.explanations[int(k)]
                sg = b.last_signals
                self.assertAlmostEqual(abs(e["planned_entry"] - sg.stop_price[k]), e["flip"]["setup_stop_points"])
                self.assertAlmostEqual(abs(e["planned_entry"] - sg.target_price[k]), e["flip"]["setup_target_points"])

    def test_hash_unchanged_while_off(self):
        self.assertEqual(P.settings_hash({}), P.settings_hash({"models.flip": False}))
        self.assertNotEqual(P.settings_hash({}), P.settings_hash({"models.flip": True}))
        self.assertEqual(P.settings_hash({"models.flip": True}), "6a6ecb19f2b01883")     # the first flip's results keep theirs
        self.assertEqual(P.settings_hash({}), P.settings_hash({"models.flip_levels": "same_distances"}))   # inert while off
        self.assertNotEqual(P.settings_hash({"models.flip": True}),
                            P.settings_hash({"models.flip": True, "models.flip_levels": "same_distances"}))


class TestCausality(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = random_ds()
        t0 = int(cls.ds.bars.ts_ns[0])
        ny = pd.DatetimeIndex(cls.ds.bars.ts_ns.astype("datetime64[ns]")).tz_localize("UTC").tz_convert("America/New_York")
        rel = np.flatnonzero((ny.hour == 8) & (ny.minute == 30))[::3]
        ev = [{"ts": int(cls.ds.bars.ts_ns[k]), "name": "CPI m/m", "impact": 3, "surprise_z": 0.5 * (j % 5)}
              for j, k in enumerate(rel)]
        cls.news = News(ev, t0, int(cls.ds.bars.ts_ns[-1]), "syn")

    def test_every_rule_variant_is_causal_in_both_phases(self):
        for over in ({}, {"fair.adapt": True, "fair.cons_max_points": 200.0, "cont.structure": "prev3", "cont.funded": True},
                     {"cont.bias_hours": 0, "cont.structure": "prev1", "funded.displacement": True, "news.max_surprise_z": 1.0,
                      "models.price_series": "mid", "day.max_losses_in_row": 1, "day.cooldown": 5},
                     {"fair.pm_target": "session_open", "session.exit_after_minutes": 0, "entry.swing_strength": 2,
                      "day.max_trades_per_session": 2}):
            for phase in ("eval", "funded"):
                rep = check_causality(FairStrategy(over, CAL, phase, news=self.news), self.ds.bars, n_cuts=10)
                self.assertTrue(rep.passed, f"{phase} {over}: {rep.detail}")

    def test_news_days_use_the_pre_news_price(self):
        st = FairStrategy({}, CAL, "eval", news=self.news)
        st.generate_signals(self.ds.bars)
        self.assertGreater(st.stats["news_days"], 0)
        srcs = {e["fair"]["source"] for e in st.explanations.values()}
        self.assertTrue(srcs <= {"session_open", "ny_am_open", "pre_news"})
        with self.assertRaises(ValueError):
            FairStrategy({}, CAL, "eval", news=None)                      # news settings need the (maybe empty) input


class _Fixed(Strategy):
    family = "test_fixed"

    def __init__(self, risk):
        super().__init__(OrderSpec(entry_type="market"), r=str(risk))
        self.risk = risk
        self.sizing = {"mode": "risk", "risk_usd": 500.0, "contract": "MNQ"}

    def generate_signals(self, bars):
        n = len(bars)
        s = SignalSet.empty(n)
        for k in range(300, n, 400):                       # a fixed grid: independent of how much history exists
            s.direction[k] = 1
            s.stop_price[k] = bars.ask_close[k] - 10
            s.target_price[k] = bars.ask_close[k] + 10
        if self.risk == "future":
            s.risk_usd = np.where(np.r_[bars.close[1:], bars.close[-1]] > bars.close, 1000.0, 200.0)   # peeks ahead
        elif self.risk is not None:
            s.risk_usd = np.full(n, np.nan)
            s.risk_usd[300] = float(self.risk)
        return s


class TestPerSignalRisk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = random_ds()
        cls.costs = cost_model_from_config(CFG, cls.ds.instrument.symbol, provider="DUKASCOPY")

    def bt(self, st, sizing=None):
        sz = sizing or st.sizing
        return run_backtest(self.ds, st, self.costs, CFG["backtest"], sizing=sz, contract=contract_for(CFG, sz))

    def test_budget_per_signal(self):
        base, per = self.bt(_Fixed(None)), self.bt(_Fixed(200))
        self.assertEqual(int(base.trades.iloc[0]["contracts"]), 25)           # 500 / (10 x 2)
        self.assertEqual(int(per.trades.iloc[0]["contracts"]), 10)            # 200 / (10 x 2)
        self.assertEqual(list(base.trades["contracts"].iloc[1:]), list(per.trades["contracts"].iloc[1:]))   # NaN = default
        self.assertNotIn("per_signal_risk_usd", base.assumptions)
        self.assertIn("per_signal_risk_usd", per.assumptions)

    def test_lookahead_in_the_budget_is_caught_and_non_risk_sizing_refused(self):
        with self.assertRaises(BacktestError) as e:
            self.bt(_Fixed("future"))
        self.assertIn("risk_usd", str(e.exception))
        with self.assertRaises(BacktestError):
            self.bt(_Fixed(200), {"mode": "fixed", "contracts": 1, "contract": "MNQ"})


def _trades(rows, contracts=10.0):
    t = pd.DataFrame(rows, columns=["entry_ts", "exit_ts", "net_usd"])
    t["entry_ts"] = pd.to_datetime(t["entry_ts"], utc=True)
    t["exit_ts"] = pd.to_datetime(t["exit_ts"], utc=True)
    t["trade_no"] = np.arange(1, len(t) + 1)
    t["contracts"] = float(contracts)
    t["gross_usd"] = t["net_usd"] + 5.0
    t["cost_usd"] = 5.0
    for k in ("cost_usd", "net_r", "risk_points", "mae_points", "risk_usd"):
        t[k] = 1.0
    t["exit_reason"] = "TARGET"
    return t


class TestPhaseChain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.prop.service import default_profiles
        cls.prof = {p["profile_id"]: p for p in default_profiles(REPO)}["LUCID_LUCIDFLEX_50K"]

    def test_eval_trades_until_the_pass_day_ends_then_funded_trades(self):
        days = pd.bdate_range("2024-03-04", periods=30)
        ev = _trades([(f"{d.date()} 14:35", f"{d.date()} 14:50", 800.0) for d in days])
        fu = _trades([(f"{d.date()} 14:40", f"{d.date()} 14:55", -100.0 if k % 3 else 400.0) for k, d in enumerate(days)])
        comb = combined_from(ev, fu, self.prof, pd.Timestamp("2024-03-04", tz="UTC"))
        self.assertIn("funded", set(comb["phase"]))
        first_f = comb[comb["phase"] == "funded"]["entry_ts"].min()
        last_e = comb[comb["phase"] == "eval"]["exit_ts"].max()
        self.assertGreater(first_f, last_e)                                # never overlapping
        self.assertLessEqual(len(comb[comb["phase"] == "eval"]), 6)       # $3,000 target at $800 a day: passes early
        raw, tr = phase_chain(ev, fu, self.prof, "2024-03-04")
        self.assertTrue(raw["attempts"][0]["passed"])
        self.assertGreater(raw["attempts"][0]["funded_trades"], 0)
        self.assertEqual(len(tr), sum(a["eval_trades"] + a["funded_trades"] for a in raw["attempts"]))

    def test_trades_bigger_than_the_account_allows_are_cut_and_the_chain_goes_on(self):
        # the user's report: a passed evaluation, funded trades of 30 micros, LucidFlex funded starts at 20 micros. Before
        # the fix the account was "incompatible" and the chain stopped there for good.
        days = pd.bdate_range("2024-03-04", periods=60)
        ev = _trades([(f"{d.date()} 14:35", f"{d.date()} 14:50", 800.0) for d in days])
        fu = _trades([(f"{d.date()} 14:40", f"{d.date()} 14:55", 300.0 if k % 2 else -150.0) for k, d in enumerate(days)],
                     contracts=30)
        raw, tr = phase_chain(ev, fu, self.prof, "2024-03-04")
        self.assertFalse(any(a["status"] == "incompatible" for a in raw["attempts"]))
        f = tr[tr["phase"] == "funded"]
        self.assertGreater(len(f), 20)
        self.assertEqual(pd.Timestamp(tr["entry_ts"].max()).date(), days[-1].date())     # trades to the end of the data
        first = f.iloc[0]
        self.assertEqual(float(first["contracts"]), 20.0)                  # cut to the funded starting limit
        self.assertEqual(float(first["cut_from"]), 30.0)
        self.assertAlmostEqual(float(first["net_usd"]), -150.0 * 20 / 30)  # dollars in proportion
        self.assertAlmostEqual(float(first["gross_usd"]), (-150.0 + 5) * 20 / 30)
        self.assertAlmostEqual(float(first["net_r"]), 1.0)                 # R unchanged
        self.assertTrue(set(f["contracts"]) <= {20.0, 30.0})              # never more than asked, at least the start tier
        self.assertGreater(raw["attempts"][0]["cut_to_limit"], 0)
        untouched = phase_chain(ev, fu.assign(contracts=10.0), self.prof, "2024-03-04")[1]
        self.assertTrue(untouched["cut_from"].isna().all())               # trades that fit are never changed

    def test_failed_evaluations_only_take_evaluation_trades(self):
        days = pd.bdate_range("2024-03-04", periods=20)
        ev = _trades([(f"{d.date()} 14:35", f"{d.date()} 14:50", -700.0) for d in days])
        fu = _trades([(f"{d.date()} 14:40", f"{d.date()} 14:55", 900.0) for d in days])
        raw, tr = phase_chain(ev, fu, self.prof, "2024-03-04")
        self.assertTrue(all(not a["passed"] for a in raw["attempts"]))
        self.assertEqual(set(tr["phase"]), {"eval"})
        self.assertGreater(len(raw["attempts"]), 1)


class TestOptimizerTweaks(unittest.TestCase):
    def test_neighbours_keep_fixed_settings(self):
        from edgelab.mystrategy import kind as KD, optimizer as O
        K = KD.get("fair")
        nbs = O.neighbours(P.resolve(), False, K)
        self.assertGreater(len(nbs), 20)
        fixed = set(K.optimizer["fixed"])
        self.assertFalse({n["key"] for n in nbs} & fixed)
        self.assertIn("funded.win_usd", {n["key"] for n in nbs})
        self.assertNotIn("fair.cons_minutes", {n["key"] for n in nbs})   # inert while fair.adapt is off
        self.assertEqual(len({n["settings_hash"] for n in nbs}), len(nbs))


class TestWorkspaceFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.services import Services
        cls.root = Path(tempfile.mkdtemp())
        cls._env = os.environ.get("EDGELAB_EXPORT_DIR")
        os.environ["EDGELAB_EXPORT_DIR"] = str(cls.root / "exports")
        shutil.copytree(REPO / "configs", cls.root / "configs")
        csv = cls.root / "combined.csv"
        write_fixture(csv, end="2024-04-27")
        f = pd.read_csv(csv, dtype=str)
        s = 1.0 + 0.25 * (np.arange(len(f)) % 7)
        for k, extra in (("open", 0.0), ("high", 0.5), ("low", 0.0), ("close", 0.0)):
            f[f"ask_{k}"] = (f[k].astype(float) + s + extra).map(lambda x: f"{x:.3f}")
        f.to_csv(csv, index=False)
        svc = Services(root=cls.root)
        imp = svc.import_file(dict(
            file=str(csv), profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY", provider="DUKASCOPY", asset_type="CFD",
            symbol="USATECH.IDX/USD", price_basis="bid", timeframe="1m", derive_timeframes=["5m", "15m"],
            build_features=False, bid_close_column="close", ask_close_column="ask_close", ask_open_column="ask_open",
            ask_high_column="ask_high", ask_low_column="ask_low", dataset_name="DUKA_SYN"))
        cls.parent = svc.create_protocol(imp["derived"][0], DISC, HOLD, name="parent", exposure_statement="none",
                                         trial_budget=20)
        svc.store.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)
        if cls._env is None:
            os.environ.pop("EDGELAB_EXPORT_DIR", None)
        else:
            os.environ["EDGELAB_EXPORT_DIR"] = cls._env

    def test_flow(self):
        from edgelab.mystrategy import kind as KD, review as RV, runner as R, setup_review as SR
        from edgelab.research import campaign
        from edgelab.services import Services
        svc = Services(root=self.root)
        K = KD.get("fair")
        sm = R.backtest(svc, {}, label="fair", lock=svc.lock, K=K)
        self.assertEqual(R.protocol_info(svc, K)["trials_used"], 1)
        self.assertEqual(R.protocol_info(svc)["trials_used"], 0)            # My strategy's protocol untouched
        mine = svc.store.get_protocol(R.protocol_info(svc, K)["protocol_id"])
        self.assertEqual(mine["material"]["role"], "fair_price")
        self.assertEqual(campaign.governing_protocol(svc)["protocol_id"], self.parent["protocol_id"])
        self.assertEqual(set(sm["phases"]), {"eval", "funded"})
        self.assertEqual(sm["criteria_profile"], "LUCID_LUCIDFLEX_50K")
        self.assertTrue(svc.store.has_run(sm["phase_runs"]["eval"]) and svc.store.has_run(sm["phase_runs"]["funded"]))
        self.assertTrue(sm["causality_passed"])
        self.assertEqual(R.list_backtests(svc), [])                          # nothing in My strategy's folder
        if sm["trade_count"]:
            doc = R.get_trade(svc, sm["id"], 1, K)
            self.assertIn(doc["phase"], ("eval", "funded"))
            self.assertIn("fair", doc["explanation"])
        R.backtest(svc, {}, label="again", lock=svc.lock, K=K)              # same settings: not a new trial
        self.assertEqual(R.protocol_info(svc, K)["trials_used"], 1)
        st = SR.start(svc, sm["id"], 3, K)
        self.assertIn("trend_day", SR.view(svc, st["id"], svc.lock, K)["reasons"])
        cands = RV.candidates(svc, K)
        slot = RV.start_automatic(svc, cands[0]["ref"], lock=svc.lock, K=K)
        RV.start_manual(svc, slot["n"], lock=svc.lock, K=K)
        k = 0
        while True:
            v = RV.allowance_view(svc, svc.lock, K)
            if not v.get("candidate"):
                break
            RV.decide(svc, v["candidate"]["signal_bar"], k % 2 == 0, svc.lock, K)
            k += 1
        self.assertEqual(v["review"]["status"], "complete")
        _, hp = RV.holdout_protocol(svc, create=False, K=K)
        self.assertEqual(hp["material"]["role"], "fair_holdout")
        self.assertEqual(len([a for a in svc.store.list_holdout_access(hp["protocol_id"]) if a["status"] != "refused"]), 2)
        _, my_hp = RV.holdout_protocol(svc, create=False)
        self.assertIsNone(my_hp)                                              # My strategy's allowance untouched
        svc.store.close()

    def test_api(self):
        from edgelab.web.app import create_app
        c = create_app(self.root).test_client()
        ov = c.get("/api/fair").get_json()
        self.assertEqual(ov["strategy_kind"], "fair")
        self.assertIn("news", ov)
        st = c.get("/api/fair/settings").get_json()
        self.assertIn("eval.target_points", st["resolved"])
        r = c.post("/api/fair/settings", json={"overrides": {"eval.target_points": 40}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["overrides"], {"eval.target_points": 40.0})
        self.assertEqual(c.post("/api/fair/settings", json={"overrides": {"ifg.tf_1m": True}}).status_code, 422)
        self.assertNotIn("eval.target_points", c.get("/api/my/settings").get_json()["resolved"])
        self.assertEqual(c.get("/api/fair/es").status_code, 404)              # ES for SMT belongs to My strategy
        self.assertEqual(c.get("/api/fair/autotune").status_code, 200)
        self.assertEqual(c.get("/api/fair/holdout").status_code, 200)
        c.post("/api/fair/settings", json={"overrides": {}})


if __name__ == "__main__":
    unittest.main()
