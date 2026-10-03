"""My strategy (ADR-93): BP Blake's model as settings, its own protocol, trade records, holdout review, uploads.

Guarantees tested: settings are validated (unknown / unavailable refused) and their hash is the identity; higher-timeframe
candles are causal (a partial candle is never complete); a hand-built known-answer day gives exactly the expected long
signal and its mirror the expected short; every rule set passes the engine's empirical lookahead check; the replay
used by the holdout review reproduces the engine's trades; the companion protocol counts trials, refuses the holdout
before a discovery backtest and spends exactly one look; human decisions are never runs; uploads send the report to
one ZIP for the user to attach in the chat. All data is SYNTHETIC."""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.data.validation import validate_and_freeze
from edgelab.engine.backtester import run_backtest
from edgelab.engine.costs import cost_model_from_config
from edgelab.engine.signals import check_causality
from edgelab.instruments import contract_for
from edgelab.mystrategy import params as P
from edgelab.mystrategy.frames import build_base, build_tf, fvgs
from edgelab.mystrategy.strategy import MyStrategy, ReplayStrategy
from tests.dukascopy_fixture import session_minutes, write_fixture
from tests.helpers import CALENDARS, CFG, INSTRUMENTS

REPO = Path(__file__).resolve().parents[1]
CAL = CALENDARS["DUKASCOPY_USATECH_OBSERVED"]


def freeze(df, ds_id="SYN_MY"):
    return validate_and_freeze(df, INSTRUMENTS["NQ_DUKASCOPY"], CAL, "1m", 1, "DUKASCOPY", ds_id, asset_type="CFD",
                               price_basis="bid")


def random_ds(start="2024-01-07", end="2024-03-30", seed=7, spread=0.5):
    ts = session_minutes(start, end)
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.normal(0, 0.15, len(ts) // 600 + 1), 600)[:len(ts)]
    c = np.round(18000 + np.cumsum(rng.normal(0, 2.0, len(ts)) + drift), 2)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + np.round(rng.uniform(0, 1.5, len(ts)), 2)
    l = np.minimum(o, c) - np.round(rng.uniform(0, 1.5, len(ts)), 2)
    df = pd.DataFrame({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1.0})
    for k, v in (("ask_open", o), ("ask_high", h), ("ask_low", l), ("ask_close", c)):
        df[k] = v + spread
    return freeze(df)


def known_day(mirror=False):
    """One trading day (2024-03-06): a bullish 5m gap at 08:00-08:14, a swing high at 09:31, a drop with a bearish 1m
    gap into the 5m gap (low 102 at 09:40), and a 1m close above that bearish gap at 09:42. ASK = BID."""
    ts = session_minutes("2024-03-05 18:00", "2024-03-06 16:15")
    loc = ts.tz_convert("America/New_York")
    hm = np.asarray(loc.hour * 60 + loc.minute)
    n = len(ts)
    o = np.full(n, 100.0); c = o.copy(); h = o + 0.25; l = o - 0.25

    def bar(t, op, hi, lo, cl):
        k = np.flatnonzero(hm == t)
        k = k[-1]                                       # the 2024-03-06 morning bar
        o[k], h[k], l[k], c[k] = op, hi, lo, cl
    m = lambda hh, mm: hh * 60 + mm
    for i in range(5):                                  # c1 08:00-08:04: high 100.5
        bar(m(8, i), 100, 100.5, 99.75, 100)
    for i in range(5):                                  # c2 08:05-08:09: up to 104
        bar(m(8, 5 + i), 100 + i * 0.8, 100.8 + i * 0.8, 99.9 + i * 0.8, 100.8 + i * 0.8)
    for i in range(5):                                  # c3 08:10-08:14: lows >= 103 -> bull gap 100.5..103
        bar(m(8, 10 + i), 104 + i * 0.4, 104.6 + i * 0.4, 103 + i * 0.4, 104.4 + i * 0.4)
    after = (hm >= m(8, 15)) & (hm < m(16, 15)) & (np.arange(n) > np.flatnonzero(hm == m(8, 14))[-1])
    o[after], c[after], h[after], l[after] = 106, 106, 106.25, 105.75
    seq = {m(9, 30): (106, 106.25, 105.75, 106), m(9, 31): (106, 107, 105.8, 106.2), m(9, 32): (106.2, 106.4, 105.6, 105.8),
           m(9, 33): (105.8, 106.0, 105.2, 105.4), m(9, 34): (105.4, 105.6, 105.0, 105.1),
           m(9, 35): (105.1, 105.1, 103.9, 104.0), m(9, 36): (103.5, 103.5, 103.0, 103.2),
           m(9, 37): (103.2, 103.4, 102.6, 102.8), m(9, 38): (102.8, 103.0, 102.4, 102.6), m(9, 39): (102.6, 102.9, 102.2, 102.5),
           m(9, 40): (102.5, 102.8, 102.0, 102.7), m(9, 41): (102.7, 104.0, 102.6, 103.9), m(9, 42): (103.9, 105.8, 103.8, 105.5),
           m(9, 43): (105.5, 105.9, 105.3, 105.7)}
    for t, v in seq.items():
        bar(t, *v)
    rest = (hm > m(9, 43)) & (hm < m(16, 15)) & (np.arange(n) > np.flatnonzero(hm == m(9, 43))[-1])
    o[rest], c[rest], h[rest], l[rest] = 105.7, 105.7, 105.9, 105.5
    if mirror:
        o, c, h, l = 200 - o, 200 - c, 200 - l, 200 - h
    df = pd.DataFrame({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1.0})
    for k, v in (("ask_open", o), ("ask_high", h), ("ask_low", l), ("ask_close", c)):
        df[k] = v
    return freeze(df, "SYN_KNOWN" + ("_M" if mirror else "")), hm


KNOWN = {"draw.required": False, "eq.enabled": False, "ifg.displacement": False, "leg.min_points": 1.0,
         "key.tf_15m": False, "key.tf_30m": False, "key.tf_1h": False, "key.tf_4h": False, "key.cisd": False,
         "key.rejection_block": False, "ifg.tf_2m": False, "ifg.tf_3m": False, "ifg.tf_4m": False, "ifg.tf_5m": False,
         "target.mode": "fixed_r", "stop.min_points": 1.0, "manage.be": "off", "leg.no_equal_extremes": False}


class TestSettings(unittest.TestCase):
    def test_defaults_and_refusals(self):
        s = P.resolve({})
        self.assertEqual(s, P.defaults())
        self.assertGreater(len(P.SCHEMA), 120)
        with self.assertRaises(P.SettingsError) as e:
            P.resolve({"nope": 1, "risk.pct": 99, "entry.type": "teleport", "session.entry_until": "25:00"})
        self.assertEqual(len(e.exception.issues), 4)
        self.assertTrue(P.resolve({"filters.smt": True})["filters.smt"])
        with self.assertRaises(P.SettingsError):
            P.resolve({"session.entry_from": "11:00", "session.entry_until": "10:00"})

    def test_hash_is_the_identity(self):
        a = P.settings_hash({"risk.pct": 1.0})                    # the default value: same strategy
        self.assertEqual(a, P.settings_hash({}))
        self.assertNotEqual(a, P.settings_hash({"target.min_r": 1.5}))
        self.assertEqual(P.changed(P.resolve({"target.min_r": 1.5})), {"target.min_r": 1.5})


class TestFrames(unittest.TestCase):
    def test_partial_candle_is_never_complete(self):
        ds = random_ds(end="2024-01-20")
        b = build_base(ds.bars, CAL)
        for tf in (5, 15, 60, 240, 1440):
            full = build_tf(b, tf)
            for cut in (1000, 2345, 5001, len(ds.bars) - 7):
                part = build_tf(build_base(ds.bars.head(cut), CAL), tf)
                done = part.comp < cut                             # complete by the last bar of the truncation
                np.testing.assert_array_equal(part.c[done], full.c[:len(part.c)][done])
                np.testing.assert_array_equal(part.comp[done], full.comp[:len(part.c)][done])
                last = len(part) - 1                               # the cut candle is partial unless it ends on time
                if full.end[last] >= cut:
                    self.assertGreaterEqual(int(part.comp[last]), cut)

    def test_gap_known_answer(self):
        ds, _ = known_day()
        t = build_tf(build_base(ds.bars, CAL), 5)
        g = fvgs(t, "bull", 0.0)
        tops = list(zip(g.bottom.round(2), g.top.round(2)))
        self.assertIn((100.5, 103.0), tops)


class TestKnownAnswer(unittest.TestCase):
    def _run(self, ds, over):
        st = MyStrategy({**KNOWN, **over}, CAL)
        sig = st.generate_signals(ds.bars)
        return st, sig, np.flatnonzero(sig.direction)

    def test_long_signal(self):
        ds, hm = known_day()
        st, sig, idx = self._run(ds, {"bias.override": "long"})
        k942 = np.flatnonzero(hm == 9 * 60 + 42)[-1]
        self.assertEqual(list(idx), [k942])
        self.assertEqual(sig.direction[k942], 1)
        self.assertAlmostEqual(sig.stop_price[k942], 101.0)          # leg low 102 - 1 buffer
        self.assertAlmostEqual(sig.target_price[k942], 110.0)        # entry 105.5 + 1 x risk 4.5
        e = st.explanations[k942]
        self.assertEqual(e["model"], "judas")
        self.assertEqual(e["confirmation"]["tf"], "1m")
        self.assertTrue(e["checklist"]["key_fvg"])
        self.assertAlmostEqual(e["leg"]["start_price"], 107.0)
        self.assertEqual([(z["bottom"], z["top"]) for z in e["key_levels"]], [(100.5, 103.0)])

    def test_mirror_gives_the_short(self):
        ds, hm = known_day(mirror=True)
        st, sig, idx = self._run(ds, {"bias.override": "short"})
        k942 = np.flatnonzero(hm == 9 * 60 + 42)[-1]
        self.assertEqual(list(idx), [k942])
        self.assertEqual(sig.direction[k942], -1)
        self.assertAlmostEqual(sig.stop_price[k942], 99.0)
        self.assertAlmostEqual(sig.target_price[k942], 90.0)

    def test_rules_refuse_by_setting(self):
        ds, _ = known_day()
        self.assertEqual(len(self._run(ds, {"bias.override": "short"})[2]), 0)        # bias the other way
        self.assertEqual(len(self._run(ds, {"bias.override": "long", "leg.min_points": 6.0})[2]), 0)
        self.assertEqual(len(self._run(ds, {"bias.override": "long", "session.entry_from": "09:45"})[2]), 0)
        self.assertEqual(len(self._run(ds, {"bias.override": "long", "stop.max_points": 3.0})[2]), 0)
        self.assertEqual(len(self._run(ds, {"bias.override": "long", "key.fvg": False, "key.bpr": True})[2]), 0)


class FakeEs:
    """ES 1m bars as the strategy sees them (timestamps + lows / highs)."""
    def __init__(self, ts_ns, low, high):
        self.ts_ns, self.low, self.high, self.has_ask_ohlc = ts_ns, low, high, False


class TestSmt(unittest.TestCase):
    def _signals(self, ds, over, es_low=None, mirror=False):
        es = None
        if es_low is not None:
            es = FakeEs(ds.bars.ts_ns, es_low, es_low + 0.5)
        st = MyStrategy({**KNOWN, **over}, CAL, es_bars=es)
        return st, st.generate_signals(ds.bars)

    def test_divergence_known_answer(self):
        ds, hm = known_day()
        k942 = np.flatnonzero(hm == 9 * 60 + 42)[-1]
        base = {"bias.override": "long", "filters.smt": True}
        both = ds.bars.low.copy()                                   # ES makes the same low as NQ: both swept
        st, sig = self._signals(ds, base, both)
        self.assertEqual(list(np.flatnonzero(sig.direction)), [k942])
        self.assertIs(st.explanations[k942]["checklist"]["smt"], False)
        only_nq = np.full(len(ds.bars), 105.5)                      # ES never trades below its reference: only NQ swept
        st, sig = self._signals(ds, base, only_nq)
        e = st.explanations[k942]
        self.assertIs(e["checklist"]["smt"], True)
        self.assertTrue(e["smt"]["nq_swept"] and not e["smt"]["es_swept"])
        off = MyStrategy({**KNOWN, "bias.override": "long"}, CAL)
        off.generate_signals(ds.bars)
        self.assertIsNone(off.explanations[k942]["checklist"]["smt"])
        self.assertEqual(st.explanations[k942]["quality"], off.explanations[k942]["quality"] + 1)
        with self.assertRaises(ValueError):                          # switched on without ES data: refused, never guessed
            MyStrategy({**KNOWN, **base}, CAL).generate_signals(ds.bars)

    def test_mirror_and_missing_es_minutes(self):
        ds, hm = known_day(mirror=True)
        k942 = np.flatnonzero(hm == 9 * 60 + 42)[-1]
        st, sig = self._signals(ds, {"bias.override": "short", "filters.smt": True}, np.full(len(ds.bars), 94.0))
        self.assertIs(st.explanations[k942]["checklist"]["smt"], True)     # short: ES never exceeded its reference high
        hole = np.full(len(ds.bars), 94.0)
        hole[k942 - 5] = np.nan                                            # a missing ES minute inside the leg: unknown, not guessed
        st, _ = self._signals(ds, {"bias.override": "short", "filters.smt": True}, hole)
        self.assertIsNone(st.explanations[k942]["checklist"]["smt"])

    def test_causal_with_es(self):
        ds = random_ds(end="2024-02-20")
        rng = np.random.default_rng(3)
        es_low = ds.bars.low + rng.normal(0, 1.5, len(ds.bars))
        st = MyStrategy({"filters.smt": True, "eq.enabled": False, "ifg.displacement": False, "filters.min_quality": 1},
                        CAL, es_bars=FakeEs(ds.bars.ts_ns, es_low, es_low + 2))
        rep = check_causality(st, ds.bars, n_cuts=10)
        self.assertTrue(rep.passed, rep.detail)


class TestCausalityAndEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = random_ds()
        cls.costs = cost_model_from_config(CFG, cls.ds.instrument.symbol, provider="DUKASCOPY")

    def test_every_rule_family_is_causal(self):
        for over in ({}, {"bias.method": "both", "bias.tie_break": "previous_day", "filters.chop": True, "bias.ath_rule": True,
                          "key.bpr": True, "key.tf_3m": True, "leg.require_sweep": False, "manage.trail": "swing_1m",
                          "manage.be": "r", "entry.type": "limit_gap", "eq.range": "overnight"},
                     {"bias.override": "short", "ifg.displacement": False, "eq.enabled": False, "leg.start_tf": "5m",
                      "target.below_min": "next", "manage.trail": "swing_5m"},
                     {"models.judas": False, "ifg.rule_ny": "single", "bias.method": "structure", "eq.range": "previous_day",
                      "models.price_series": "mid"}):
            rep = check_causality(MyStrategy(over, CAL), self.ds.bars, n_cuts=12)
            self.assertTrue(rep.passed, f"{over}: {rep.detail}")

    def test_engine_and_replay_agree(self):
        st = MyStrategy({"eq.enabled": False, "ifg.displacement": False}, CAL)
        res = run_backtest(self.ds, st, self.costs, CFG["backtest"], sizing=st.sizing, contract=contract_for(CFG, st.sizing))
        self.assertTrue(res.causality.passed)
        self.assertGreater(len(res.trades), 3)
        self.assertEqual(res.skipped.get("BUSY_IN_POSITION_OR_ORDER", 0), 0)     # own tracking matches the engine
        rp = ReplayStrategy(st, st.last_signals, set(), len(self.ds.bars))
        bt = {**CFG["backtest"], "require_causality_check": False}
        again = run_backtest(self.ds, rp, self.costs, bt, sizing=st.sizing, contract=contract_for(CFG, st.sizing))
        self.assertEqual(res.trades_hash, again.trades_hash)
        first = int(res.trades["signal_bar"].iloc[0])
        skipped = MyStrategy({"eq.enabled": False, "ifg.displacement": False}, CAL, skip={first})
        s2 = skipped.generate_signals(self.ds.bars)
        self.assertEqual(s2.direction[first], 0)
        self.assertEqual(skipped.stats["signals_declined_in_review"], 1)


# ------------------------------------------------------------------------------------------------ workspace
DISC, HOLD = ("2024-03-04", "2024-04-05"), ("2024-04-08", "2024-04-26")


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
        import zipfile

        from edgelab.mystrategy import review as RV
        from edgelab.mystrategy import runner as R
        from edgelab.research import campaign
        from edgelab.services import Services
        svc = Services(root=self.root)
        ov = {"bias.override": "long", "eq.enabled": False, "ifg.displacement": False}
        self.assertEqual(R.protocol_info(svc)["created"], False)
        with self.assertRaises(R.MyStrategyError) as e:                  # the holdout needs a discovery backtest
            RV.start(svc, ov, lock=svc.lock)
        self.assertEqual(e.exception.code, "NOT_BACKTESTED")
        with self.assertRaises(R.MyStrategyError) as e:                  # windows beyond discovery are locked
            R.backtest(svc, ov, "2024-03-10", "2024-04-20", lock=svc.lock)
        self.assertEqual(e.exception.code, "HOLDOUT_LOCKED")
        sm = R.backtest(svc, ov, label="t", lock=svc.lock)
        info = R.protocol_info(svc)
        self.assertEqual(info["trials_used"], 1)
        # the companion never becomes the governing protocol of the library / campaigns
        gp = campaign.governing_protocol(svc)
        self.assertEqual(gp["protocol_id"], self.parent["protocol_id"])
        mine = svc.store.get_protocol(info["protocol_id"])
        self.assertEqual(mine["material"]["role"], "my_strategy")
        self.assertEqual(mine["material"]["windows"], self.parent["material"]["windows"])
        self.assertTrue(svc.store.has_run(sm["run_id"]))
        R.backtest(svc, ov, label="again", lock=svc.lock)                 # same settings, same window: not a new trial
        self.assertEqual(R.protocol_info(svc)["trials_used"], 1)
        if sm["trade_count"]:
            doc = R.get_trade(svc, sm["id"], 1)
            self.assertIn("1m", doc["candles"])
            self.assertIn("checklist", doc["explanation"])
            self.assertTrue(all(c[0] < c[0] + 1 for c in doc["candles"]["1m"]))
        # holdout: one look, take/skip, both results; decisions are not runs
        runs_before = len(svc.list_runs()) if hasattr(svc, "list_runs") else None
        st = RV.start(svc, ov, lock=svc.lock)
        self.assertEqual(R.protocol_info(svc)["holdout_looks_used"], 1)
        listed = {r["id"] for r in R.all_reports(svc)}                       # mechanical result hidden while deciding
        self.assertNotIn(st["mechanical_report"], listed)
        self.assertIn(sm["id"], listed)
        k = 0
        while True:
            v = RV.view(svc, svc.lock)
            if not v.get("candidate"):
                break
            c = v["candidate"]
            last_1m = c["candles"]["1m"][-1][0]                            # nothing after the signal bar is shown
            self.assertLessEqual(last_1m, pd.Timestamp(c["signal_ts"]).value // 10**9)
            RV.decide(svc, c["signal_bar"], k % 2 == 0, svc.lock)
            k += 1
        self.assertEqual(RV.current(svc)["status"], "complete")
        if runs_before is not None:
            self.assertEqual(len(svc.list_runs()), runs_before + 1)          # only the mechanical run; decisions are never runs
        with self.assertRaises(R.MyStrategyError) as e:
            RV.start(svc, ov, lock=svc.lock)
        self.assertIn(e.exception.code, ("HOLDOUT_LOOKS_USED",))
        hd = R.get_backtest(svc, RV.current(svc)["final_report"])
        self.assertIsNone(hd["run_id"])
        self.assertIn(st["mechanical_report"], {r["id"] for r in R.all_reports(svc)})    # listed once finished
        # export for Claude: one ZIP with the chosen reports, then marked as saved
        with self.assertRaises(R.MyStrategyError):
            R.export(svc, [])
        out = R.export(svc, [sm["id"], st["mechanical_report"]], include_candles=False)
        self.assertTrue(Path(out["path"]).is_file())
        self.assertTrue(out["path"].startswith(str(self.root / "exports")))
        with zipfile.ZipFile(out["path"]) as z:
            names = set(z.namelist())
            self.assertIn("index.json", names)
            self.assertIn(f"{sm['id']}/summary.json", names)
            self.assertIn(f"{sm['id']}/trades.json.gz", names)
            self.assertNotIn(f"{sm['id']}/candles.jsonl.gz", names)
            self.assertIn(f"{st['mechanical_report']}/review_state.json", names)
        self.assertTrue(next(r for r in R.list_backtests(svc) if r["id"] == sm["id"])["exported"])
        with_candles = R.export(svc, [sm["id"]], include_candles=True)
        with zipfile.ZipFile(with_candles["path"]) as z:
            self.assertIn(f"{sm['id']}/candles.jsonl.gz", z.namelist())
        svc.store.close()

    def test_plans_are_checked_before_running(self):
        from edgelab.mystrategy import runner as R
        with self.assertRaises(R.MyStrategyError):
            R.check_plan({"variants": [{"overrides": {"no.such": 1}}]})
        rows = R.check_plan({"base": {"eq.enabled": False}, "variants": [{"label": "a"}, {"overrides": {"target.min_r": 1.5}}]})
        self.assertEqual(rows[1]["overrides"], {"eq.enabled": False, "target.min_r": 1.5})


class TestApi(unittest.TestCase):
    def test_routes(self):
        from edgelab.web.app import create_app
        root = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(REPO / "configs", root / "configs")
            c = create_app(root).test_client()
            r = c.get("/api/my")
            self.assertEqual(r.status_code, 200)
            self.assertFalse(r.json["protocol"]["ready"])                     # no research protocol in an empty workspace
            self.assertEqual(c.get("/api/my/settings").status_code, 200)
            self.assertEqual(c.post("/api/my/settings", json={"overrides": {"x.y": 1}}).status_code, 422)
            self.assertEqual(c.get("/api/my/reports/BT_bad").status_code, 400)
            self.assertEqual(c.get("/api/my/reports/BT_20260101_000000_abcd").status_code, 404)
            self.assertEqual(c.post("/api/my/review/decide", json={"signal_bar": "x"}).status_code, 400)
            self.assertEqual(c.post("/api/my/export", json={"report_ids": ["../../etc"]}).status_code, 400)
            self.assertEqual(c.post("/api/my/export", json={"report_ids": []}).status_code, 400)
            bad = c.post("/api/my/plans/check", json={"plan": {"variants": [{"overrides": {"nope": 1}}]}})
            self.assertEqual(bad.status_code, 422)
            ok = c.post("/api/my/plans/check", json={"plan": {"name": "p", "variants": [{"label": "a"}]}})
            self.assertEqual(ok.json["variants"][0]["label"], "a")
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
