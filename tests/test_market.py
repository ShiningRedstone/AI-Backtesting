"""Market simulator (ADR-106). All data is SYNTHETIC (random walks; news and new days from tests/market_fixture.py).

Guarantees tested:
* bars of every timeframe are anchored at the 18:00 New York session open (DST-correct) and known only at their end;
  the trading date of a minute is the NY date of (time + 6 h);
* patterns are causal: every event known before a cut-off is identical when everything after the cut-off is replaced;
* outcomes carry no selection bias: on a random walk every pattern's '1 ATR its way first' rate is ~50 % (the touch
  minute and 'first event per window' bugs found during development stay fixed), and the edge scan finds NOTHING on a
  random walk while it finds a planted effect;
* forecast inputs are live: the inputs at time t are identical when every minute from t on is replaced; walk-forward
  predictions of a month never change when later months change; the boosting model is deterministic;
* news: the time zone is proven from fixed-time releases (a trading-server clock and plain UTC are both found, too few
  releases are refused), the surprise uses only earlier releases, the API key lives in the user settings file (never
  the workspace) and is never returned;
* the job reads the discovery period only (never the holdout), records no run, caches by its inputs; new days start
  after the research data; the API answers;
* ADR-107: chance levels from sign-flipped real moves match the real statistics on a random walk; shocks are the top
  0.1 % per timeframe; opening gaps get their 50 % fill; divergence closings split into NQ / ES / both; confirmed edge
  cells are grouped and checked against costs; the holdout prediction test records its ONE look BEFORE any holdout
  minute is loaded, predicts only holdout candles, refuses a second look and shows holdout days only after the look.
* ADR-108 level map: the levels and inputs at a decision are identical when every minute from then on is replaced;
  EQ / OTE are the stated fractions of the last confirmed swing range; on a random walk the nearest-level race matches
  the gambler's-ruin probability, reactions are near 50 %, no model shows real skill; the holdout look scores the
  level map and writes the mistakes report.
* ADR-109 direction calls: inputs at a decision are identical with another future; on a random walk no stage makes
  calls that beat the baseline; a planted inside-candle reversal is found at minute 5 (and not at the open, where it
  cannot be known); the SECOND holdout look is its own one-look companion protocol whose exposure note names the first
  look, is recorded before any holdout minute is read and refuses a second use.
* ADR-110: once a holdout look is used, new days are predicted from the FIRST new day (the holdout minutes are only the
  history in front of them); minute series join without duplicates; the report card lists every forecast with its
  scores on discovery, the holdout and new days; per-day new-day scores exist.
"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.market import data as D
from edgelab.market import edges as E
from edgelab.market import forecast as F
from edgelab.market import gbm as G
from edgelab.market import news as N
from edgelab.market import patterns as P
from tests.dukascopy_fixture import session_minutes
from tests.market_fixture import fake_minutes, fake_news_fetch, news_json


def walk(start="2023-01-02", end="2023-03-31", seed=1, sd=2.0):
    ts = session_minutes(start, end).as_unit("ns")
    rng = np.random.default_rng(seed)
    c = 18000 + np.cumsum(rng.normal(0, sd, len(ts)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0, 1, len(ts))
    lo = np.minimum(o, c) - rng.uniform(0, 1, len(ts))
    return D.Minute(ts.asi8, o, h, lo, c)


class TestBars(unittest.TestCase):
    def test_anchor_known_and_trading_date(self):
        m = walk("2023-03-08", "2023-03-15")                         # the US DST switch (12 March) inside
        for tf in (15, 60, 240):
            b = D.resample(m, tf)
            ny = pd.DatetimeIndex(b.ts, tz="UTC").tz_convert(D.NY)
            mins = (ny.hour * 60 + ny.minute - 18 * 60) % 1440
            self.assertTrue((np.asarray(mins) % tf == 0).all(), tf)  # buckets start on the 18:00 grid, DST or not
            self.assertTrue((b.known_ns - b.ts == tf * D.MIN_NS).all())
        t = pd.Timestamp("2023-03-09 18:30", tz=D.NY).tz_convert("UTC").value
        self.assertEqual(str(D.trading_day([t])[0]), "2023-03-10")    # the 18:00 session carries the next date
        d = D.resample(m, D.DAY)
        self.assertTrue((d.known_ns > d.ts).all())


class TestPatternsCausalAndUnbiased(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = walk()
        cls.ev, cls.facts, cls.bars = P.detect_all(cls.m)

    def test_causal(self):
        m = self.m
        cut = int(m.ts[len(m) * 2 // 3])
        rng = np.random.default_rng(5)
        later = m.ts >= cut
        c2 = m.c.copy()
        c2[later] = m.c[later] + np.cumsum(rng.normal(0, 3, later.sum()))
        o2 = np.r_[c2[0], c2[:-1]]
        m2 = D.Minute(m.ts, np.where(later, o2, m.o), np.where(later, np.maximum(o2, c2) + 0.5, m.h),
                      np.where(later, np.minimum(o2, c2) - 0.5, m.l), c2)
        ev2, _, _ = P.detect_all(m2)
        cols = ["kind", "tf", "dir", "known_ns", "top", "bottom"]
        a = self.ev[self.ev["known_ns"] < cut][cols].astype(str).sort_values(cols).reset_index(drop=True)
        b = ev2[ev2["known_ns"] < cut][cols].astype(str).sort_values(cols).reset_index(drop=True)
        pd.testing.assert_frame_equal(a, b)                           # nothing known before the cut changes

    def test_random_walk_outcomes_are_fifty_fifty(self):
        ev = self.ev
        for kind in ("FVG", "IFVG", "OB", "BOS", "SWING_SWEEP", "OTE"):
            for d in (1, -1):
                e = ev[(ev["kind"] == kind) & (ev["dir"] == d)]["edge"].to_numpy()
                e = e[e != 0]
                if len(e) < 400:
                    continue
                rate = (e > 0).mean()
                self.assertLess(abs(rate - 0.5), 4 * 0.5 / np.sqrt(len(e)) + 0.01, (kind, d, rate, len(e)))

    def test_first_event_per_window_is_first_in_time(self):
        """Regression: picking the first row per 15-minute window in STORAGE order biased the rate (0.30 instead of 0.50)."""
        from edgelab.market.analysis import measurable
        f = measurable(self.ev, self.m)
        act = np.where(f["entry_i"] >= 0, self.m.ts[np.clip(f["entry_i"], 0, len(self.m) - 1)], 0)
        for kind in ("IFVG_FORMED", "FVG"):
            g = np.flatnonzero((f["kind"] == kind).to_numpy() & (f["tf"] == 1).to_numpy())
            g = g[np.argsort(act[g], kind="stable")]
            _, first = np.unique(act[g] // (15 * D.MIN_NS), return_index=True)
            e = f["edge"].to_numpy()[g[first]]
            e = e[e != 0]
            self.assertLess(abs((e > 0).mean() - 0.5), 0.05, kind)

    def test_chance_levels_match_on_random_walk(self):
        from edgelab.market import trend as T
        c = T.chance_levels(self.m, self.facts)
        for k in ("up_days_low_first", "high_or_low_in_first_hour"):
            real, ch = c["real"][k], c["chance"][k]
            self.assertGreaterEqual(real["n"], 15)
            self.assertTrue(real["ci"][0] - 0.03 <= ch["p"] <= real["ci"][1] + 0.03, (k, real, ch))

    def test_gap_half_fill(self):
        g = self.ev[self.ev["kind"].isin(["NDOG", "NWOG", "RTH_GAP"])]
        if len(g):
            fill, ce = g["fill_min"].to_numpy(float), g["ce_min"].to_numpy(float)
            both = np.isfinite(fill) & np.isfinite(ce)
            self.assertTrue((ce[both] <= fill[both]).all())        # half the gap closes no later than all of it
            self.assertTrue((np.isfinite(ce) | ~np.isfinite(fill)).all())

    def test_shocks_are_rare(self):
        from edgelab.market import shocks as S
        typ = {tf: D.typical_by_slot(b, b.h - b.l) for tf, b in self.bars.items() if tf != D.DAY}
        sh = S.detect(self.m, self.bars, typ, None, None, None, [], P.day_levels(self.m))
        days = len(np.unique(self.m.day))
        self.assertLess(len(sh) / days, 3.0)                       # the old 3x rule gave ~20 a day on real data
        self.assertGreater(len(sh), 0)

    def test_zone_stats(self):
        rows = P.summarize(self.ev, int(self.m.ts[-1]), 60)
        fvg = [r for r in rows if r["kind"] == "FVG" and r["tf"] == 15]
        self.assertEqual(len(fvg), 2)
        for r in fvg:
            self.assertGreater(r["n"], 50)
            self.assertLessEqual(r["filled"], r["ce"] + 1e-9)
            self.assertLessEqual(r["ce"], r["touched"] + 1e-9)
            self.assertLessEqual(r["filled_1h"], r["filled_1d"] + 1e-9)


class TestEdgeScan(unittest.TestCase):
    def _cells(self, plant: float):
        rng = np.random.default_rng(7)
        cells, pvals = [], []
        for g in range(40):
            n = 12000
            sess = rng.integers(0, 6, n).astype(np.int8)
            ctx = {c: rng.choice([-1, 0, 1], n).astype(np.int8) for c in ("trend_1h", "pd", "es", "vol")}
            p = np.full(n, 0.5)
            if g == 0 and plant:
                p[(sess == 3) & (ctx["trend_1h"] == 1)] += plant          # a real effect in one condition
            y = np.where(rng.random(n) < p, 1, -1).astype(np.int8)
            outs = {"edge": y, "next15": np.where(rng.random(n) < 0.5, 1, -1).astype(np.int8)}
            base = {"edge": np.full(n, 0.5), "next15": np.full(n, 0.5)}
            find = np.arange(n) < int(0.7 * n)
            cells += E.scan_group({"kind": f"K{g}", "tf": "5m", "dir": 1}, outs, base, ctx, sess, find, 10.0, 3.0, pvals,
                                  np.arange(n))
        return E.finish(cells, pvals)

    def test_groups_and_costs(self):
        planted = self._cells(0.15)
        self.assertTrue(planted["groups"])
        self.assertEqual(sum(g["cells"] for g in planted["groups"]), len(planted["candidates"]))
        for c in planted["candidates"]:
            if c["outcome"] == "edge":
                better = c["rate_confirm"] if c["rate_find"] > c["base_find"] else 1 - c["rate_confirm"]
                self.assertEqual(c["tradeable"], better > c["breakeven"])
                self.assertAlmostEqual(c["breakeven"], 0.5 + 3.0 / (2 * 10.0))

    def test_nothing_on_noise_planted_effect_found(self):
        noise = self._cells(0.0)
        self.assertEqual(noise["confirmed"], 0)
        self.assertGreater(noise["cells_tested"], 1000)
        planted = self._cells(0.15)
        self.assertGreater(planted["confirmed"], 0)
        self.assertTrue(all(c["kind"] == "K0" for c in planted["candidates"]))
        self.assertTrue(any("NY AM" in c["condition"] and "with the 1h trend" in c["condition"] for c in planted["candidates"]))


class TestForecast(unittest.TestCase):
    def test_inputs_are_live(self):
        m = walk("2023-01-02", "2023-02-28", seed=4)
        es = walk("2023-01-02", "2023-02-28", seed=5, sd=0.5)
        news = [e for e in json.loads(news_json("2023-01-01", "2023-03-31"))["USD"]["Events"]]
        cal = N.build(news_json("2023-01-01", "2023-03-31"), {"sha256": "x"})
        cx = F.Context(m, es, cal["events"])
        b = cx.bars[15]
        picks = b.ts[[len(b) - 300, len(b) - 120, len(b) - 20]]
        X, names, _ = F.features(cx, picks)
        for q, t in enumerate(picks):
            keep = m.ts < t
            mt = D.Minute(m.ts[keep], m.o[keep], m.h[keep], m.l[keep], m.c[keep])
            ke = es.ts < t
            et = D.Minute(es.ts[ke], es.o[ke], es.h[ke], es.l[ke], es.c[ke])
            Xt, _, _ = F.features(F.Context(mt, et, cal["events"]), np.array([t]))
            np.testing.assert_allclose(Xt[0], X[q], rtol=1e-9, atol=1e-9, err_msg=str(pd.Timestamp(t)))
        self.assertIn("news_in_this_candle", names)

    def test_walk_forward_isolation_and_determinism(self):
        rng = np.random.default_rng(1)
        days = np.repeat(pd.bdate_range("2022-01-03", periods=300).values.astype("datetime64[D]"), 20)
        n = len(days)
        X = rng.normal(size=(n, 6))
        y = (rng.random(n) < 1 / (1 + np.exp(-X[:, 0]))).astype(float)
        slot = np.tile(np.arange(20), 300)
        a = F.walk_forward(X, y, days, slot, "binary")
        y2 = y.copy()
        late = days >= np.datetime64("2023-01-01")
        y2[late] = 1 - y2[late]                                       # change every later month
        b = F.walk_forward(X, y2, days, slot, "binary")
        early = (days < np.datetime64("2023-01-01")) & np.isfinite(a["pred"]["logistic"])
        self.assertGreater(early.sum(), 100)
        for k in F.MODELS:
            np.testing.assert_array_equal(a["pred"][k][early], b["pred"][k][early])
        months = days.astype("datetime64[M]")                         # the baseline is ONE constant per month (the
        for mo in np.unique(months[np.isfinite(a["pred"]["baseline"])]):   # overall up-rate): never a noisy slot rate
            self.assertEqual(np.unique(a["pred"]["baseline"][months == mo]).size, 1)
        g1 = G.GBM(n_trees=20, seed=3).fit(X[:2000], y[:2000]).predict(X[2000:2100])
        g2 = G.GBM(n_trees=20, seed=3).fit(X[:2000], y[:2000]).predict(X[2000:2100])
        np.testing.assert_array_equal(g1, g2)
        sc = F.score_binary(a["pred"]["logistic"][np.isfinite(a["pred"]["logistic"])], y[np.isfinite(a["pred"]["logistic"])],
                            a["pred"]["baseline"][np.isfinite(a["pred"]["logistic"])], days[np.isfinite(a["pred"]["logistic"])])
        self.assertTrue(sc["real"])                                   # X0 really predicts y: the skill is found
        self.assertGreater(sc["skill_ci"][0], 0)


class TestLevelMap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.market import levelmap as L
        cls.m = walk("2023-01-02", "2023-08-31", seed=7)
        cls.cx = F.Context(cls.m, None, [])
        cls.b = L.build(cls.cx)

    def test_levels_and_inputs_are_live(self):
        from edgelab.market import levelmap as L
        m, b = self.m, self.b
        XL, _ = L.level_matrix(b)
        for q in (len(b["dec"]["t"]) - 40, len(b["dec"]["t"]) - 7):
            t = int(b["dec"]["t"][q])
            cut = np.searchsorted(m.ts, t)
            rng = np.random.default_rng(q)
            c = m.c.copy()
            c[cut:] = c[cut - 1] + np.cumsum(rng.normal(0, 3, len(c) - cut))      # another future
            o = np.r_[m.o[:cut], c[cut - 1], c[cut:-1]]
            h = np.r_[m.h[:cut], np.maximum(o[cut:], c[cut:]) + 0.5]
            lo = np.r_[m.l[:cut], np.minimum(o[cut:], c[cut:]) - 0.5]
            m2 = D.Minute(m.ts, o, h, lo, c)
            b2 = L.build(F.Context(m2, None, []), start_ns=t)
            self.assertEqual(int(b2["dec"]["t"][0]), t)
            X2, _ = L.level_matrix(b2)
            r1 = np.flatnonzero(b["rows"]["dec"] == q)
            r2 = np.flatnonzero(b2["rows"]["dec"] == 0)
            np.testing.assert_allclose(b2["rows"]["price"][r2], b["rows"]["price"][r1])
            self.assertEqual([b2["rows"]["label_words"][i] for i in b2["rows"]["label"][r2]],
                             [b["rows"]["label_words"][i] for i in b["rows"]["label"][r1]])
            np.testing.assert_allclose(X2[r2], XL[r1], rtol=1e-9, atol=1e-9)

    def test_dealing_range_levels(self):
        from edgelab.market import levelmap as L
        src = L.Source(self.cx)
        t = int(self.b["dec"]["t"][-30])
        q = len(self.b["dec"]["t"]) - 30
        info = F.features(self.cx, np.array([t]))[2]
        cand = L._candidates(src, t, info["px"][0], info["a15"][0], int(info["di"][0]), info["hi_so"][0],
                             info["lo_so"][0], int(np.searchsorted(self.m.day, self.m.day[np.searchsorted(self.m.ts, t)])))
        sw = src.swing[60]
        jh = np.searchsorted(sw["hi"]["known"], t, side="right") - 1
        jl = np.searchsorted(sw["lo"]["known"], t, side="right") - 1
        h, lo = sw["hi"]["price"][jh], sw["lo"]["price"][jl]
        up = sw["hi"]["k"][jh] > sw["lo"]["k"][jl]
        got = {c[8]: c[0] for c in cand}
        self.assertAlmostEqual(got["1h equilibrium (50 %)"], (h + lo) / 2)
        for r in L.OTE:
            self.assertAlmostEqual(got[f"1h OTE {r:g}"], h - r * (h - lo) if up else lo + r * (h - lo))
        self.assertTrue(any(c[1] == "fvg" for c in cand))
        self.assertGreater(q, 0)

    def test_random_walk_chance(self):
        from edgelab.market import levelmap as L
        r, d = self.b["rows"], self.b["dec"]
        rc = r["react"][np.isfinite(r["react"])]
        self.assertGreater(len(rc), 3000)
        self.assertTrue(0.42 < rc.mean() < 0.55, rc.mean())       # ~50 % (minute bars: a touch minute closes a bit past)
        ok = np.isfinite(d["first"])
        self.assertLess(abs(d["first"][ok].mean() - d["gambler"][ok].mean()), 0.04)
        out = tempfile.mkdtemp()
        res = L.run(self.cx, out)
        with np.load(Path(out) / "levelmap.npz") as z:                 # a reaction is forecast for EVERY level (live:
            pr = z[f"p_react_{res['targets']['react']['chosen']}"]       # nobody knows yet which will be touched)
            have = np.isfinite(z["p_reach_logistic"])
            self.assertTrue(np.isfinite(pr[have]).all())
            self.assertTrue((have & ~z["row_reach"]).any())
        for tgt, ev in res["targets"].items():
            if tgt == "turn":
                self.assertFalse(ev.get("real"), ev)
                continue
            for k, sc in ev["scores"].items():
                if sc.get("all") and k != "baseline":
                    self.assertFalse(sc["all"]["real"], (tgt, k, sc["all"]["skill_ci"]))
        self.assertIn("groups", res["mistakes"]["reach"])
        self.assertTrue(any(x["kind"] == "stack" for x in res["by_kind"]))


def planted_reversal(start, end, seed=3, k=0.6):
    """Random walk in which minutes 5-14 of every 15-minute candle drift against the move of its minutes 0-4."""
    ts = session_minutes(start, end).as_unit("ns").asi8
    z = np.zeros(len(ts))
    m0 = D.Minute(ts, z, z, z, z)
    rng = np.random.default_rng(seed)
    step = rng.normal(0, 2.0, len(ts))
    pos = m0.sess_min % 15
    out = np.empty(len(ts))
    x, start_px, first5 = 18000.0, 18000.0, 0.0
    for q in range(len(ts)):
        if pos[q] == 0:
            start_px, first5 = x, 0.0
        if pos[q] == 5:
            first5 = x - start_px
        x += step[q] + (-k * first5 / 10 if pos[q] >= 5 else 0.0)
        out[q] = x
    o = np.r_[out[0], out[:-1]]
    return D.Minute(ts, o, np.maximum(o, out) + rng.uniform(0, 1, len(ts)), np.minimum(o, out) - rng.uniform(0, 1, len(ts)), out)


class TestDirection(unittest.TestCase):
    def test_inputs_are_live(self):
        from edgelab.market import direction as DR
        m = walk("2023-01-02", "2023-02-28", seed=6)
        b = DR.build(F.Context(m, None, []))
        r = b["r"]
        for q in (len(r["t"]) - 500, len(r["t"]) - 101):
            t = int(r["t"][q])
            cut = np.searchsorted(m.ts, t)
            rng = np.random.default_rng(q)
            c = m.c.copy()
            c[cut:] = c[cut - 1] + np.cumsum(rng.normal(0, 3, len(c) - cut))      # another future from t on
            o = np.r_[m.o[:cut], c[cut - 1], c[cut:-1]]
            m2 = D.Minute(m.ts, o, np.r_[m.h[:cut], np.maximum(o[cut:], c[cut:]) + 0.5],
                          np.r_[m.l[:cut], np.minimum(o[cut:], c[cut:]) - 0.5], c)
            b2 = DR.build(F.Context(m2, None, []), start_ns=t)
            self.assertEqual(int(b2["r"]["t"][0]), t)
            j = int(np.flatnonzero(b2["r"]["stage"] == r["stage"][q])[0])
            np.testing.assert_allclose(b2["X"][j], b["X"][q], rtol=1e-9, atol=1e-9)

    def test_random_walk_no_calls_planted_found(self):
        from edgelab.market import direction as DR
        res, _, _ = DR.analyse(DR.build(F.Context(walk("2023-01-02", "2023-09-30", seed=4), None, [])))
        for s, ev in res["stages"].items():
            self.assertFalse((ev["calls"]["late"] or {}).get("real"), (s, ev["calls"]))
        res, _, _ = DR.analyse(DR.build(F.Context(planted_reversal("2023-01-02", "2023-09-30"), None, [])))
        c5 = res["stages"]["5"]["calls"]
        self.assertIsNotNone(c5["rule"]["tau"])
        self.assertTrue(c5["late"]["real"], c5)
        self.assertGreater(c5["late"]["accuracy"], 0.65)
        self.assertFalse((res["stages"]["0"]["calls"]["late"] or {}).get("real"))     # at the open it cannot be known
        self.assertEqual(res["top_inputs"][0]["input"], "so_move")


class TestNewDaysJoin(unittest.TestCase):
    def test_concat_sorted_without_duplicates(self):
        from edgelab.market import newdays as ND
        a = walk("2023-01-02", "2023-01-06", seed=1)
        b = walk("2023-01-05", "2023-01-11", seed=2)
        j = ND._concat(a, b)
        self.assertTrue((np.diff(j.ts) > 0).all())
        self.assertEqual(len(j), len(np.union1d(a.ts, b.ts)))
        k = np.searchsorted(j.ts, a.ts)
        np.testing.assert_array_equal(j.c[k], a.c)                            # a minute in both is kept from the first
        self.assertIs(ND._concat(None, b), b)


class TestNews(unittest.TestCase):
    def test_timezone_proof(self):
        cal = N.build(news_json("2022-01-01", "2023-12-31", zone="NY+7"), {"sha256": "x"})
        self.assertTrue(cal["timezone"]["zone"].startswith("NY+7"))
        e = [x for x in cal["events"] if x["name"] == "CPI m/m"][3]
        ny = pd.Timestamp(e["ts"], tz="UTC").tz_convert(D.NY)
        self.assertEqual((ny.hour, ny.minute), (8, 30))
        cal2 = N.build(news_json("2022-01-01", "2023-12-31", zone="UTC"), {"sha256": "x"})
        self.assertEqual(cal2["timezone"]["zone"], "UTC+0")
        few = N.build(news_json("2023-01-01", "2023-02-10"), {"sha256": "x"})
        self.assertEqual(few["refused"]["code"], "NEWS_TIMEZONE_UNPROVEN")
        self.assertEqual(few["events"], [])

    def test_surprise_uses_earlier_releases_only(self):
        cal = N.build(news_json("2022-01-01", "2023-12-31"), {"sha256": "x"})
        claims = [x for x in cal["events"] if x["name"] == "Unemployment Claims"]
        self.assertIsNone(claims[0]["surprise_z"])
        k = 20
        past = [c["surprise"] for c in claims[:k]]
        self.assertAlmostEqual(claims[k]["surprise_z"], claims[k]["surprise"] / np.std(past, ddof=1), places=9)
        self.assertEqual(N.number("250K"), 250000.0)
        self.assertEqual(N.number("-0.3%"), -0.3)
        self.assertIsNone(N.number("n/a"))

    def test_key_outside_workspace(self):
        tmp = Path(tempfile.mkdtemp())
        old = os.environ.get("EDGELAB_SETTINGS")
        os.environ["EDGELAB_SETTINGS"] = str(tmp / "settings.json")
        try:
            with self.assertRaises(N.NewsError):
                N.set_key("short")
            st = N.set_key("a" * 40)
            self.assertTrue(st["set"])
            self.assertNotIn("a" * 40, json.dumps(st))
            self.assertIn("a" * 40, (tmp / "settings.json").read_text())
        finally:
            if old is None:
                os.environ.pop("EDGELAB_SETTINGS", None)
            else:
                os.environ["EDGELAB_SETTINGS"] = old
            shutil.rmtree(tmp, ignore_errors=True)


class TestMarketService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.mystrategy import es as ES
        from edgelab.services import Services
        from tests.dukascopy_fixture import write_fixture
        from tests.test_my_strategy import REPO
        cls.root = Path(tempfile.mkdtemp())
        cls.old_settings = os.environ.get("EDGELAB_SETTINGS")
        os.environ["EDGELAB_SETTINGS"] = str(cls.root / "user_settings.json")
        shutil.copytree(REPO / "configs", cls.root / "configs")
        csv = cls.root / "nq.csv"
        write_fixture(csv, start="2022-12-25", end="2023-10-28", seed=11)
        f = pd.read_csv(csv, dtype=str)
        s = 1.0 + 0.25 * (np.arange(len(f)) % 7)
        for k, extra in (("open", 0.0), ("high", 0.5), ("low", 0.0), ("close", 0.0)):
            f[f"ask_{k}"] = (f[k].astype(float) + s + extra).map(lambda x: f"{x:.3f}")
        f.to_csv(csv, index=False)
        svc = Services(root=cls.root)
        imp = svc.import_file(dict(
            file=str(csv), profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY", provider="DUKASCOPY", asset_type="CFD",
            symbol="USATECH.IDX/USD", price_basis="bid", timeframe="1m", derive_timeframes=["5m"], build_features=False,
            bid_close_column="close", ask_close_column="ask_close", ask_open_column="ask_open", ask_high_column="ask_high",
            ask_low_column="ask_low", dataset_name="DUKA_SYN"))
        cls.parent = svc.create_protocol(imp["derived"][0], ("2023-01-02", "2023-08-31"), ("2023-09-01", "2023-10-27"),
                                         name="parent", exposure_statement="none", trial_budget=20)
        es_csv = cls.root / "es.csv"
        write_fixture(es_csv, start="2022-12-25", end="2023-10-28", seed=12)
        ES.import_csv(svc.data_root, str(es_csv), identity_confirmed=True)
        N.set_key("k" * 40)
        N.download(svc.data_root, fetch=fake_news_fetch("2022-06-01", "2023-12-31"))
        from edgelab.market import analysis as A
        runs = svc.store.list_runs() if hasattr(svc.store, "list_runs") else None
        cls.runs_before = len(runs) if runs is not None else None
        cls.res = A.run(svc, lock=svc.lock)
        svc.store.close()

    @classmethod
    def tearDownClass(cls):
        if cls.old_settings is None:
            os.environ.pop("EDGELAB_SETTINGS", None)
        else:
            os.environ["EDGELAB_SETTINGS"] = cls.old_settings
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_discovery_only_cached_no_runs(self):
        from edgelab.market import analysis as A
        from edgelab.services import Services
        r = self.res
        hold = pd.Timestamp(r["source"]["holdout_start"])
        self.assertLess(pd.Timestamp(r["source"]["window"]["end"]), hold)
        self.assertTrue(r["news"]["used"])
        self.assertGreater(r["news"]["events"], 20)
        self.assertGreater(r["edges"]["cells_tested"], 1000)
        self.assertEqual(r["edges"]["confirmed"], 0)                 # a random walk has no edge
        self.assertGreater(len(r["patterns"]), 50)
        self.assertIn("relationship", r["nqes"])
        cb = r["nqes"]["divergence"]["closed_by"]
        self.assertAlmostEqual(sum(v["p"] or 0 for v in cb.values()), 1.0, places=9)
        self.assertIn("chance", r["trend"])
        self.assertEqual(len(r["cost_points"]["by_session"]), 6)
        self.assertIn("by_cause", r["shocks"])
        self.assertTrue(r["forecast"]["up"]["months"])
        svc = Services(root=self.root)
        try:
            mk = D.discovery(svc)
            self.assertLess(int(mk.nq.ts[-1]), hold.value)            # never a holdout minute
            self.assertLess(int(mk.es.ts[-1]), hold.value)
            again = A.run(svc, lock=svc.lock)
            self.assertEqual(again["computed_at"], r["computed_at"])   # cached by its inputs
            if self.runs_before is not None:
                self.assertEqual(len(svc.store.list_runs()), self.runs_before)
        finally:
            svc.store.close()

    def test_new_days_after_research_data(self):
        from edgelab.market import newdays as ND
        from edgelab.services import Services
        svc = Services(root=self.root)
        try:
            self.assertEqual(ND.first_date(svc).isoformat(), "2023-10-30")
            u = ND.update(svc, fetch=fake_minutes, now=datetime(2023, 12, 20, 23, tzinfo=timezone.utc))
            self.assertGreater(u["nq"]["days"], ND.WARMUP_DAYS)
            self.assertEqual(u["nq"]["first"], "2023-10-30")
            sc = ND.predict(svc, self.res["key"])
            self.assertGreater(sc["candles"], 100)
            self.assertIn("logistic", sc["up"])
            self.assertEqual(sc["history"], "holdout")                       # the looks are used (tests above)
            self.assertEqual(sc["warmup_days"], 0)
            self.assertEqual(len(sc["days"]), u["nq"]["days"])                 # every new day predicted, from day 1
            self.assertEqual([d["date"] for d in sc["by_day"]], sc["days"])
            with np.load(ND.folder(svc.data_root, "") / f"predictions_{self.res['key']}.npz") as z:
                self.assertTrue(np.isfinite(z["size_q"]).any())                 # size ranges for new days too
            rep = svc.market_report()
            ids = {t["id"] for t in rep["targets"]}
            self.assertTrue({"size", "up", "reach2h", "dir0"} <= ids)
            size = next(t for t in rep["targets"] if t["id"] == "size")
            self.assertIn("new", size["sources"])
            self.assertIn("holdout", size["sources"])
            self.assertTrue(size["monthly"]["discovery"])
        finally:
            svc.store.close()

    def test_new_days_zlive_predictor(self):
        """ADR-113: the chart predictor = the new-days models (identical predictions on the same minutes); live only
        (cutting the day at 11:00 changes nothing before it; outcomes appear only once known); NQ / MNQ only."""
        from edgelab.charts import predictor as PR
        from edgelab.market import analysis as A
        from edgelab.market import newdays as ND
        from edgelab.services import Services
        svc = Services(root=self.root)
        try:
            summary = A.latest(svc.data_root)
            fz = PR.train(svc, summary)
            mk = D.discovery(svc)
            hist = ND._history(svc, mk)
            nq = ND._concat(hist[0], ND.load(svc.data_root, "nq"))
            es = ND._concat(hist[1], ND.load(svc.data_root, "es"))
            news = N.events(svc.data_root, int(nq.ts[0]), int(nq.ts[-1]) + 86_400 * 10**9) if fz["ok_news"] else []
            work = self.root / "pred_tmp"
            full = PR.predict_live(fz, nq, es, news, work, int(nq.ts[-1]) + 60 * 10**9)
            self.assertGreater(len(full["candles"]), 20)
            with np.load(ND.folder(svc.data_root, "") / f"predictions_{self.res['key']}.npz") as z:
                ref = {int(t) // 10**9: (float(p), float(sz)) for t, p, sz in
                       zip(z["t"], z[f"p_up_{fz['up_chosen']}"], z[f"size_{fz['size_chosen']}"])}
            got = [c for c in full["candles"] if c["t"] in ref]
            self.assertGreater(len(got), 20)
            for c in got:                                                   # the same models, the same inputs
                self.assertAlmostEqual(c["p_up"], ref[c["t"]][0], places=9)
            # live: cut the last day at 11:00 New York
            day = nq.day[-1]
            cut = int(pd.Timestamp(f"{day} 11:00", tz="America/New_York").tz_convert("UTC").value)
            keep = nq.ts < cut
            nq_cut = D.Minute(nq.ts[keep], nq.o[keep], nq.h[keep], nq.l[keep], nq.c[keep])
            part = PR.predict_live(fz, nq_cut, es, news, work, cut)
            fullc = {c["t"]: c for c in full["candles"]}
            for c in part["candles"]:
                self.assertAlmostEqual(c["p_up"], fullc[c["t"]]["p_up"], places=12)
                self.assertAlmostEqual(c["size_pts"], fullc[c["t"]]["size_pts"], places=9)
            self.assertFalse(part["candles"][-1]["complete"]) if part["candles"][-1]["t"] * 10**9 + 15 * 60 * 10**9 > cut else None
            fulll = {d["t"]: d for d in full["levelmaps"]}
            self.assertTrue(part["levelmaps"])
            for d in part["levelmaps"]:
                self.assertLess(d["t"] * 10**9, cut)
                f = fulll[d["t"]]
                self.assertEqual([x["price"] for x in d["levels"]], [x["price"] for x in f["levels"]])
                for x, y in zip(d["levels"], f["levels"]):
                    self.assertAlmostEqual(x["p_reach2h"] or 0, y["p_reach2h"] or 0, places=6)
                    if x["touched_at"] is not None:
                        self.assertLess(x["touched_at"] * 10**9, cut)
                    if d["t"] * 10**9 + 2 * 3600 * 10**9 > cut and x["touched_at"] is None:
                        self.assertIsNone(x["within2h"])                 # not known yet at 11:00
                self.assertIsNone(d["land"]["land"]["actual"])
            lp = PR.LivePredictor(svc)
            self.assertFalse(lp.get("ES")["available"])
        finally:
            svc.store.close()

    def test_holdout_test_one_look_recorded_first(self):
        from edgelab.market import holdout as H
        from edgelab.services import Services
        from edgelab.web.app import create_app
        c = create_app(self.root).test_client()
        self.assertEqual(c.get("/api/market/days?src=holdout").get_json()["days"], [])     # nothing before the look
        self.assertEqual(c.post("/api/market/holdout", json={"confirm": "nope"}).status_code, 422)
        svc = Services(root=self.root)
        events = []
        real_cell, real_add = svc._cell_dataset, svc.store.add_holdout_access
        hold = pd.Timestamp(self.res["source"]["holdout_start"])

        def cell(ds_id, period=None, lock=None):
            if period is not None and pd.Timestamp(period[0]) >= hold:
                events.append("holdout data")
            return real_cell(ds_id, period, lock)

        def add(row):
            events.append("look recorded")
            return real_add(row)
        svc._cell_dataset = cell
        svc.store.add_holdout_access = add
        try:
            st0 = H.status(svc)
            self.assertFalse(st0["used"])
            res = H.run(svc, lock=svc.lock)
            self.assertEqual(events[:2], ["look recorded", "holdout data"])          # the look first, then the holdout
            self.assertGreater(res["candles"], 100)
            self.assertEqual(set(res["targets"]), {"up", "size", "bias", "levels"})
            lm = res["levelmap"]                                                         # ADR-108: the level map too
            self.assertEqual(lm["reach"]["official_model"], self.res["levelmap"]["targets"]["reach"]["chosen"])
            self.assertIn("turn", lm)
            self.assertIn("groups", lm["mistakes"]["react"])
            self.assertIn("groups", res["mistakes"]["up"])
            self.assertEqual(res["targets"]["size"]["official_model"], self.res["forecast"]["size"]["chosen"])
            with np.load(H.home(svc.data_root) / "predictions.npz") as z:
                self.assertGreaterEqual(int(z["t"].min()), hold.value)                  # only holdout candles predicted
            with self.assertRaises(H.HoldoutTestError) as e:
                H.run(svc, lock=svc.lock)
            self.assertEqual(e.exception.code, "HOLDOUT_LOOK_USED")
            st = H.status(svc)
            self.assertTrue(st["used"])
            self.assertEqual(st["look"]["status"], "completed")
        finally:
            svc.store.close()
        days = c.get("/api/market/days?src=holdout").get_json()["days"]
        self.assertTrue(days)
        d = c.get(f"/api/market/day/{days[0]}?src=holdout").get_json()
        self.assertTrue(d["candles"])
        self.assertTrue(d["levelmap"] and d["levelmap"][0]["levels"])
        self.assertEqual(c.post("/api/market/holdout", json={"confirm": "HOLDOUT"}).status_code, 202)   # job starts, then refuses

    def test_holdout_zdirection_second_look(self):
        from edgelab.market import direction as DR
        from edgelab.market import holdout as H
        from edgelab.services import Services
        from edgelab.web.app import create_app
        svc = Services(root=self.root)
        events = []
        real_cell, real_add = svc._cell_dataset, svc.store.add_holdout_access
        hold = pd.Timestamp(self.res["source"]["holdout_start"])

        def cell(ds_id, period=None, lock=None):
            if period is not None and pd.Timestamp(period[0]) >= hold:
                events.append("holdout data")
            return real_cell(ds_id, period, lock)

        def add(row):
            events.append("look recorded")
            return real_add(row)
        try:
            with self.assertRaises(H.HoldoutTestError) as e:
                DR.holdout_run(svc, lock=svc.lock)
            self.assertEqual(e.exception.code, "NO_DIRECTION")              # the direction analysis comes first
            summ = DR.run(svc, lock=svc.lock)
            self.assertEqual(set(summ["stages"]), {"0", "5", "10"})
            self.assertFalse(DR.holdout_status(svc)["used"])
            first = H.status(svc)
            svc._cell_dataset = cell
            svc.store.add_holdout_access = add
            res = DR.holdout_run(svc, lock=svc.lock)
            self.assertEqual(events[:2], ["look recorded", "holdout data"])
            self.assertTrue(res["second_look"])
            with np.load(DR.holdout_home(svc.data_root) / "predictions.npz") as z:
                self.assertGreaterEqual(int(z["t"].min()), hold.value)
            _, mine = DR.holdout_protocol(svc, create=False)
            self.assertEqual(mine["material"]["role"], "market_sim_direction")
            self.assertIn("SECOND look", mine["material"]["pre_protocol_exposure"]["statement"])
            if first.get("used"):
                self.assertIn(first["look"]["access_id"], mine["material"]["pre_protocol_exposure"]["statement"])
            with self.assertRaises(H.HoldoutTestError) as e:
                DR.holdout_run(svc, lock=svc.lock)
            self.assertEqual(e.exception.code, "HOLDOUT_LOOK_USED")
            self.assertEqual(H.status(svc).get("used"), first.get("used"))  # the first look's record is untouched
        finally:
            svc.store.close()
        c = create_app(self.root).test_client()
        st = c.get("/api/market/direction").get_json()
        self.assertTrue(st["holdout"]["used"])
        self.assertIn("5", st["summary"]["stages"])
        days = c.get("/api/market/days?src=holdout").get_json()["days"]
        d = c.get(f"/api/market/day/{days[0]}?src=holdout").get_json()
        self.assertTrue(d["direction"])
        self.assertEqual(c.post("/api/market/direction/holdout", json={"confirm": "x"}).status_code, 422)

    def test_api(self):
        from edgelab.web.app import create_app
        c = create_app(self.root).test_client()
        st = c.get("/api/market").get_json()
        self.assertEqual(st["analysis"]["key"], self.res["key"])
        self.assertTrue(st["news"]["key"]["set"])
        self.assertNotIn("k" * 40, json.dumps(st))                    # the key never leaves the computer's settings
        for sec in ("patterns", "edges", "trend", "nqes", "shocks", "forecast", "effect_matrix", "newsfx"):
            self.assertEqual(c.get(f"/api/market/section/{sec}").status_code, 200, sec)
        self.assertEqual(c.get("/api/market/section/nope").status_code, 404)
        days = c.get("/api/market/days").get_json()["days"]
        self.assertTrue(days)
        d = c.get(f"/api/market/day/{days[-1]}").get_json()
        self.assertGreater(len(d["candles"]), 40)
        self.assertIn("p_up", d["candles"][-1])
        lm = d["levelmap"]                                                            # ADR-108
        self.assertTrue(lm)
        lv = lm[0]["levels"]
        self.assertTrue(all(x["side"] == (1 if x["price"] > lm[0]["px"] else -1) for x in lv))
        self.assertIn("land2h", lm[0]["land"])
        self.assertEqual(c.get("/api/market/day/2023-99-99x").status_code, 400)
        self.assertEqual(c.post("/api/market/news/key", json={"key": "short"}).status_code, 422)
        self.assertEqual(c.get("/api/market/jobs/nope").status_code, 400)
        rep = c.get("/api/market/report").get_json()                         # ADR-110 report card
        self.assertTrue(rep["targets"])
        self.assertTrue(all("works" in t and "monthly" in t for t in rep["targets"]))
        st = c.get("/api/market").get_json()
        self.assertIn("holdout_used", st)
        self.assertIn("last_problem", st["newdays"])


if __name__ == "__main__":
    unittest.main()
