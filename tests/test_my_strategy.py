"""My strategy (ADR-93): BP Blake's model as settings, its own protocol, trade records, holdout review, uploads.

Guarantees tested: settings are validated (unknown / unavailable refused) and their hash is the identity (SMT settings
keep pre-SMT hashes while SMT cannot change the trades); SMT with ES: off = identical signals, identical markets never
diverge, a hand-made divergence is found, a missing ES minute = unknown, SMT stays causal; the ES import refuses bad files; higher-timeframe
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
        self.assertTrue(P.resolve({"filters.smt": True})["filters.smt"])          # ADR-95: available (needs ES to run)
        with self.assertRaises(ValueError):
            MyStrategy({"filters.smt": True}, CAL)                                  # refused without the ES data
        with self.assertRaises(P.SettingsError):
            P.resolve({"session.entry_from": "11:00", "session.entry_until": "10:00"})

    def test_hash_is_the_identity(self):
        a = P.settings_hash({"risk.pct": 1.0})                    # the default value: same strategy
        self.assertEqual(a, P.settings_hash({}))
        self.assertNotEqual(a, P.settings_hash({"target.min_r": 1.5}))
        self.assertEqual(P.changed(P.resolve({"target.min_r": 1.5})), {"target.min_r": 1.5})

    def test_smt_settings_keep_old_identities(self):
        """ADR-95 known answers: hashes recorded with the code before SMT existed stay the same while SMT cannot change
        the trades (filter off, and the score setting irrelevant because the minimum score is 0 or it is off)."""
        self.assertEqual(P.settings_hash({}), "83a7d49ee4f9d441")
        self.assertEqual(P.settings_hash({"filters.smt_in_score": False}), "83a7d49ee4f9d441")
        q2 = P.settings_hash({"filters.min_quality": 2, "filters.smt_in_score": False})
        self.assertNotEqual(q2, P.settings_hash({"filters.min_quality": 2}))       # SMT in the score: another strategy
        self.assertFalse(P.smt_used(P.resolve({})))
        self.assertTrue(P.smt_used(P.resolve({"filters.min_quality": 1})))
        self.assertTrue(P.smt_used(P.resolve({"filters.smt": True, "filters.smt_in_score": False})))


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


# ------------------------------------------------------------------------------------------------ SMT with ES (ADR-95)
def es_like(ds, scale=0.28, drop=(), lift=None, cap=None):
    """A SYNTHETIC ES series built from the NQ bars (a monotonic copy: it takes exactly the lows / highs NQ takes).
    ``drop`` removes minutes; ``lift`` = (a, b, level) keeps ES lows in bars a..b at or above ``level``; ``cap`` = (a, b,
    level) keeps ES highs in bars a..b at or below ``level`` (ES 'holds')."""
    from edgelab.mystrategy.es import EsSeries, content_hash
    b = ds.bars
    keep = np.ones(len(b), bool)
    keep[list(drop)] = False
    o, h, lo, c = (np.asarray(x, float) * scale for x in (b.open, b.high, b.low, b.close))
    if lift is not None:
        a, z, level = lift
        lo[a:z + 1] = np.maximum(lo[a:z + 1], level)
        h[a:z + 1] = np.maximum(h[a:z + 1], lo[a:z + 1])
    if cap is not None:
        a, z, level = cap
        h[a:z + 1] = np.minimum(h[a:z + 1], level)
        lo[a:z + 1] = np.minimum(lo[a:z + 1], h[a:z + 1])
    ts = b.ts_ns[keep]
    return EsSeries(ts, h[keep], lo[keep], content_hash(ts, o[keep], h[keep], lo[keep], c[keep]), {"es_id": "ES_SYN"})


class TestSmt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = random_ds()
        cls.base = {"eq.enabled": False, "ifg.displacement": False}

    def _sig(self, over, es):
        st = MyStrategy({**self.base, **over}, CAL, es=es)
        sig = st.generate_signals(self.ds.bars)
        return st, sig

    def test_off_is_identical_with_or_without_es(self):
        a, sa = self._sig({}, None)
        b, sb = self._sig({}, es_like(self.ds))
        for x, y in ((sa.direction, sb.direction), (sa.stop_price, sb.stop_price), (sa.target_price, sb.target_price),
                     (sa.entry_price, sb.entry_price)):
            np.testing.assert_array_equal(x, y)
        self.assertEqual(a.strategy_id, b.strategy_id)
        self.assertGreater(len(b.explanations), 3)
        self.assertTrue(all(e["checklist"]["smt"] is None for e in a.explanations.values()))

    def test_identical_markets_never_diverge(self):
        st, _ = self._sig({}, es_like(self.ds))
        self.assertNotIn(True, {e["checklist"]["smt"] for e in st.explanations.values()})   # ES takes what NQ takes
        st2, sig2 = self._sig({"filters.smt": True}, es_like(self.ds))
        self.assertEqual(int((sig2.direction != 0).sum()), 0)
        self.assertGreater(st2.stats["setup_rejected_no_smt"], 0)

    def test_known_divergence_and_missing_minutes(self):
        st, _ = self._sig({}, es_like(self.ds))
        bars = self.ds.bars
        cases = [(i, e) for i, e in st.explanations.items() if e["smt"].get("nq_took")]
        self.assertTrue(cases, "no signal whose leg took a prior swing")
        i, e = cases[0]
        m = int(np.searchsorted(bars.ts_ns, pd.Timestamp(e["leg"]["end_ts"]).value))
        ref = int(np.searchsorted(bars.ts_ns, pd.Timestamp(e["smt"]["ref_ts"]).value))
        k = ref + int(e["smt"]["tf"].rstrip("m"))          # first bar after the swing candle
        d = e["direction"]
        # ES holds its swing low (longs) / high (shorts) of that candle while NQ takes it -> divergence on this signal
        if d > 0:
            es = es_like(self.ds, lift=(k, m, float(np.min(bars.low[ref:k])) * 0.28))
        else:
            es = es_like(self.ds, cap=(k, m, float(np.max(bars.high[ref:k])) * 0.28))
        st2, _ = self._sig({}, es)
        self.assertIs(st2.explanations[i]["checklist"]["smt"], True)
        self.assertIn("ES held", st2.explanations[i]["smt"]["reason"])
        st3, sig3 = self._sig({"filters.smt": True}, es)
        self.assertEqual(int(sig3.direction[i]), d)      # required SMT keeps exactly this signal
        self.assertTrue(all(x["checklist"]["smt"] is True for x in st3.explanations.values()))
        # one missing ES minute inside the compared span -> unknown, never "yes"
        st4, _ = self._sig({}, es_like(self.ds, drop=[m]))
        self.assertIsNone(st4.explanations[i]["checklist"]["smt"])
        self.assertEqual(st4.explanations[i]["smt"]["reason"], "ES minutes missing")

    def test_smt_in_score_and_causality(self):
        es = es_like(self.ds, drop=range(500, 520))
        st, _ = self._sig({}, es)
        st_no, _ = self._sig({"filters.smt_in_score": False}, es)
        for k, e in st.explanations.items():
            self.assertEqual(e["quality"] - st_no.explanations[k]["quality"], int(e["checklist"]["smt"] is True))
        for over in ({"filters.smt": True}, {"filters.min_quality": 3}):
            rep = check_causality(MyStrategy({**self.base, **over}, CAL, es=es), self.ds.bars, n_cuts=12)
            self.assertTrue(rep.passed, f"{over}: {rep.detail}")


class TestEsImport(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _csv(self, rows, header="timestamp,open,high,low,close,volume"):
        p = self.root / "es.csv"
        p.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
        return p

    def test_import_and_refusals(self):
        from edgelab.mystrategy import es as ES
        good = ["2024-01-02 14:30:00+00:00,4700.5,4701.25,4699.75,4700.0,0.1",
                "2024-01-02 14:31:00+00:00,4700.0,4702.0,4699.5,4701.5,0.2",
                "2024-01-02 14:33:00+00:00,4701.5,4701.5,4700.25,4700.75,0.1"]
        with self.assertRaises(ES.EsError) as e:
            ES.import_csv(self.root, self._csv(good), identity_confirmed=False)
        self.assertEqual(e.exception.code, "ES_IDENTITY_UNCONFIRMED")
        man = ES.import_csv(self.root, self._csv(good), identity_confirmed=True)
        self.assertEqual(man["bars"], 3)
        self.assertEqual(ES.import_csv(self.root, self._csv(good), identity_confirmed=True)["es_id"], man["es_id"])
        es = ES.load(self.root)
        h, lo = es.align(np.array([pd.Timestamp("2024-01-02 14:31", tz="UTC").value,
                                   pd.Timestamp("2024-01-02 14:32", tz="UTC").value]))
        self.assertEqual((h[0], lo[0]), (4702.0, 4699.5))
        self.assertTrue(np.isnan(h[1]) and np.isnan(lo[1]))       # missing minute: NaN, never filled
        bad = {"ES_BAD_HEADER": (good, "time,open,high,low,close,volume"),
               "ES_TIMEZONE": ([good[0].replace("+00:00", "+01:00")] + good[1:], None),
               "ES_ORDER": ([good[1], good[0]], None),
               "ES_OHLC": (["2024-01-02 14:30:00+00:00,4700.5,4700.0,4699.75,4700.0,0.1"], None),
               "ES_NOT_1M": (["2024-01-02 14:30:30+00:00,4700.5,4701.25,4699.75,4700.0,0.1"], None)}
        for code, (rows, head) in bad.items():
            with self.assertRaises(ES.EsError, msg=code) as e:
                ES.read_csv(self._csv(rows, head) if head else self._csv(rows))
            self.assertEqual(e.exception.code, code)


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

    def test_setup_review_on_discovery(self):
        """ADR-96: blind take/skip on a fixed-seed sample of a discovery backtest; no trial, no run, no holdout look."""
        import zipfile

        from edgelab.mystrategy import runner as R
        from edgelab.mystrategy import setup_review as SR
        from edgelab.services import Services
        svc = Services(root=self.root)
        ov = {"bias.override": "long", "eq.enabled": False, "ifg.displacement": False}
        sm = R.backtest(svc, ov, label="for review", lock=svc.lock)
        self.assertGreater(sm["trade_count"], 2)
        info0 = R.protocol_info(svc)
        runs0 = len(svc.store.list_runs()) if hasattr(svc.store, "list_runs") else None
        size = sm["trade_count"] - 1
        all_nos = list(range(1, sm["trade_count"] + 1))
        self.assertEqual(SR.sample_of(sm["id"], all_nos, size), SR.sample_of(sm["id"], all_nos, size))   # fixed seed
        st = SR.start(svc, sm["id"], size=size)
        self.assertEqual(st["order"], sorted(st["order"]))
        self.assertEqual(len(st["order"]), size)
        with self.assertRaises(R.MyStrategyError) as e:                    # one open review at a time
            SR.start(svc, sm["id"])
        self.assertEqual(e.exception.code, "SETUP_REVIEW_OPEN")
        with self.assertRaises(R.MyStrategyError):                         # not finished: nothing to save yet
            R.export(svc, [st["id"]])
        trades = {t["trade_no"]: t for t in R.get_backtest(svc, sm["id"])["trades"]}
        k = 0
        while True:
            v = SR.view(svc, st["id"], svc.lock)
            c = v.get("candidate")
            if not c:
                break
            self.assertNotIn("results", v)                                 # outcomes hidden until the end
            self.assertNotIn("net_r", c)
            for tf, rows in c["candles"].items():                          # nothing after the signal bar is shown
                self.assertLessEqual(rows[-1][0], pd.Timestamp(c["signal_ts"]).value // 10**9, tf)
            self.assertLess(pd.Timestamp(c["signal_ts"]), pd.Timestamp(trades[c["trade_no"]]["entry_ts"]))
            if k == 0:
                with self.assertRaises(R.MyStrategyError) as e:
                    SR.decide(svc, st["id"], c["trade_no"], False, [])
                self.assertEqual(e.exception.code, "REASON_REQUIRED")
                with self.assertRaises(R.MyStrategyError):
                    SR.decide(svc, st["id"], c["trade_no"], False, ["nope"])
                SR.decide(svc, st["id"], c["trade_no"], True)
                self.assertEqual(SR.undo(svc, st["id"])["trade_no"], c["trade_no"])   # undo the last decision
            if k % 2:
                SR.decide(svc, st["id"], c["trade_no"], False, ["choppy", "other"], "too slow")
            else:
                SR.decide(svc, st["id"], c["trade_no"], True, ["choppy"])  # reasons dropped for a take
            k += 1
        self.assertEqual(k, size)
        res = v["results"]
        self.assertEqual(v["review"]["status"], "complete")
        self.assertEqual(res["taken"]["trades"] + res["skipped"]["trades"], size)
        for row in res["rows"]:                                            # outcomes are the backtest's own
            self.assertAlmostEqual(row["net_r"], trades[row["trade_no"]]["net_r"], places=4)
            self.assertEqual(row["reasons"], [] if row["take"] else ["choppy", "other"])
        self.assertAlmostEqual(res["all"]["net_r"], sum(trades[n]["net_r"] for n in st["order"]), places=3)
        self.assertEqual({b["reason"] for b in res["by_reason"]}, {"choppy", "other"} if res["skipped"]["trades"] else set())
        # no trial, no run, no holdout look
        info1 = R.protocol_info(svc)
        self.assertEqual((info1["trials_used"], info1["holdout_looks_used"]), (info0["trials_used"], info0["holdout_looks_used"]))
        if runs0 is not None:
            self.assertEqual(len(svc.store.list_runs()), runs0)
        out = R.export(svc, [st["id"]])
        with zipfile.ZipFile(out["path"]) as z:
            doc = json.loads(z.read(f"{st['id']}/setup_review.json"))
        self.assertEqual(len(doc["results"]["rows"]), size)
        self.assertTrue(SR.list_reviews(svc)[0]["exported"])
        with self.assertRaises(R.MyStrategyError) as e:                    # holdout reports are refused
            SR.start(svc, "HO_20260101_000000_abcd")
        self.assertEqual(e.exception.code, "NO_REPORT")
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
            self.assertFalse(c.get("/api/my/es").json["imported"])                  # ADR-95
            self.assertEqual(c.post("/api/my/es/import", json={"path": "x.csv"}).status_code, 422)   # identity unconfirmed
            self.assertEqual(c.post("/api/my/es/import", json={"path": 5}).status_code, 400)
            self.assertEqual(c.post("/api/my/settings", json={"overrides": {"x.y": 1}}).status_code, 422)
            self.assertEqual(c.get("/api/my/reports/BT_bad").status_code, 400)
            self.assertEqual(c.get("/api/my/reports/BT_20260101_000000_abcd").status_code, 404)
            self.assertEqual(c.post("/api/my/review/decide", json={"signal_bar": "x"}).status_code, 400)
            self.assertEqual(c.post("/api/my/export", json={"report_ids": ["../../etc"]}).status_code, 400)
            self.assertEqual(c.post("/api/my/export", json={"report_ids": []}).status_code, 400)
            sr = c.get("/api/my/setup-reviews")                                    # ADR-96
            self.assertEqual(sr.status_code, 200)
            self.assertIn("choppy", sr.json["reasons"])
            self.assertEqual(c.post("/api/my/setup-reviews", json={"report_id": "../x"}).status_code, 400)
            self.assertEqual(c.post("/api/my/setup-reviews", json={"report_id": "BT_20260101_000000_abcd"}).status_code, 422)
            self.assertEqual(c.get("/api/my/setup-reviews/SR_bad").status_code, 400)
            self.assertEqual(c.get("/api/my/setup-reviews/SR_20260101_000000_abcd").status_code, 404)
            self.assertEqual(c.post("/api/my/setup-reviews/SR_20260101_000000_abcd/decide",
                                    json={"trade_no": "1", "take": True}).status_code, 400)
            bad = c.post("/api/my/plans/check", json={"plan": {"variants": [{"overrides": {"nope": 1}}]}})
            self.assertEqual(bad.status_code, 422)
            ok = c.post("/api/my/plans/check", json={"plan": {"name": "p", "variants": [{"label": "a"}]}})
            self.assertEqual(ok.json["variants"][0]["label"], "a")
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
