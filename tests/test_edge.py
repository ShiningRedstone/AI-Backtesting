"""Edge lab (ADR-104): the edge check and the trade anatomy.

Guarantees tested: the day table is causal (the typical range uses only earlier days; yesterday's session is yesterday's);
every hypothesis decides only from bars complete before its entry (changing every later minute never changes a signal);
a planted effect is found and a pure random walk is not (fixed seeds); the net result buys on ASK / sells on BID and
subtracts exactly the configured costs; the hypothesis set is frozen (known-answer fingerprint) and the Bonferroni family
is the whole set; the run reads only the discovery period, caches by its inputs and never records a run or a trial; the
anatomy's numbers and verdicts on hand-made trades. All data is SYNTHETIC."""
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.edge import anatomy as AN
from edgelab.edge import check as C
from edgelab.edge import hypotheses as HY
from tests.dukascopy_fixture import session_minutes

SET_FINGERPRINT = "5a279fa8b338ff0d"       # known answer: change it only together with hypotheses.VERSION


def walk(start="2022-01-03", end="2023-12-29", seed=1, spread=0.5, plant=0.0):
    """A pure random walk on the provisional calendar (BID, ASK = BID + spread). ``plant`` adds a 10:00-10:59 drift of
    plant x (direction of the first 30 minutes) points per minute: a known H1 effect."""
    ts = session_minutes(start, end).as_unit("ns")
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 2.0, len(ts))
    if plant:
        ny = ts.tz_convert("America/New_York")
        mins = np.asarray(ny.hour * 60 + ny.minute)
        dates = np.asarray(ny.tz_localize(None).normalize())
        c0 = 18000 + np.cumsum(steps)
        for d in np.unique(dates):
            m = dates == d
            i930 = np.flatnonzero(m & (mins == 570))
            i959 = np.flatnonzero(m & (mins == 599))
            if len(i930) and len(i959):
                sgn = np.sign(c0[i959[0]] - (c0[i930[0] - 1] if i930[0] else c0[i930[0]]))
                steps[m & (mins >= 600) & (mins < 660)] += plant * sgn
    c = 18000 + np.cumsum(steps)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0, 1, len(ts))
    lo = np.minimum(o, c) - rng.uniform(0, 1, len(ts))
    return ts, o, h, lo, c, o + spread, c + spread


class TestDayTableAndSignals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ts, o, h, lo, c, ao, ac = walk()
        cls.raw = (ts, o, h, lo, c)
        cls.t = C.day_table(ts.asi8, o, h, lo, c, ao, ac)

    def test_frozen_set(self):
        self.assertEqual(HY.fingerprint(), SET_FINGERPRINT)
        self.assertEqual(HY.FAMILY, len(HY.H))
        for h in HY.H:
            self.assertGreaterEqual(len(h.against), 3)            # reasons NOT to, written before the test

    def test_table_is_causal(self):
        t = self.t
        rng_ = np.nanmax(t["h"], axis=1) - np.nanmin(t["l"], axis=1)
        ok = np.flatnonzero(t["window_ok"])
        k = ok[40]                                                # the typical range = mean of the 20 earlier days
        prev = [i for i in ok if i < k][-20:]
        self.assertAlmostEqual(t["typical"][k], float(np.mean(rng_[prev])), places=9)
        ts, o, h, lo, c = self.raw
        ny = ts.tz_convert("America/New_York")
        mins = np.asarray(ny.hour * 60 + ny.minute)
        day = pd.Timestamp(t["days"][k])
        y = pd.Timestamp(t["days"][k - 1])
        m = (ny.tz_localize(None).normalize() == y) & (mins >= 570) & (mins < 960)
        self.assertEqual(t["prev_high"][k], float(h[m].max()))
        self.assertEqual(t["prev_low"][k], float(lo[m].min()))
        self.assertEqual(t["prev_close"][k], float(c[m & (mins == 959)][0]))
        self.assertGreater(day, y)

    def test_signals_never_look_ahead(self):
        """Changing every minute from the entry on never changes a signal or its entry."""
        rng = np.random.default_rng(9)
        for h in HY.H:
            d, e = HY.signals(h.id, self.t)
            days = np.flatnonzero(d != 0)
            self.assertGreater(len(days), 5, h.id)
            for i in days[:: max(1, len(days) // 25)]:
                t2 = {k: (v.copy() if isinstance(v, np.ndarray) else v) for k, v in self.t.items()}
                for k in ("o", "h", "l", "c", "ao", "ac"):
                    t2[k][i, e[i]:] = rng.normal(18000, 500, HY.SLOTS - e[i])
                d2, e2 = HY.signals(h.id, t2)
                self.assertEqual((d2[i], e2[i]), (d[i], e[i]), (h.id, i))

    def test_random_walk_finds_nothing_and_a_planted_effect_is_found(self):
        for h in HY.H:
            r = C.evaluate(h.id, self.t)
            self.assertGreaterEqual(r["p_bonf"], 0.05, h.id)
            self.assertIn(r["verdict"], ("NO_EVIDENCE", "TOO_FEW"))
        ts, o, hh, lo, c, ao, ac = walk(seed=1, plant=0.25)
        t = C.day_table(ts.asi8, o, hh, lo, c, ao, ac)
        r = C.evaluate("H1", t)
        self.assertLess(r["p_bonf"], 0.01)
        self.assertGreater(r["gross_pts"]["mean"], 10)            # ~0.25 x 60 minutes = 15 points planted
        self.assertEqual(r["direction"], "as stated")

    def test_net_buys_on_ask_sells_on_bid_and_subtracts_costs(self):
        cost_pts = 0.7
        tr = C.trades_of("H1", self.t, cost=lambda a, b, i, s, d: cost_pts)
        spread = 0.5
        for j in range(20):
            self.assertAlmostEqual(tr["gross_pts"][j] - tr["net_pts"][j], spread + cost_pts, places=9)
            self.assertAlmostEqual(-tr["gross_pts"][j] - tr["net_opp_pts"][j], spread + cost_pts, places=9)

    def test_es_cross_check(self):
        ts, o, h, lo, c, _, _ = walk(seed=5)
        es_t = C.day_table(ts.asi8, o, h, lo, c)
        r = C.evaluate("H1", self.t, None, es_t)
        self.assertIn("es", r)
        self.assertGreater(r["es"]["n"], 100)


class TestAnatomy(unittest.TestCase):
    def test_numbers_and_verdicts(self):
        rng = np.random.default_rng(3)
        trades = [{"trade_no": k, "direction": 1 if k % 2 else -1, "exit_reason": "STOP" if g < 0 else "TARGET",
                   "gross_r": g, "net_r": g - 0.1, "mfe_r": abs(g) + 0.3 if g < 0 else g, "mae_r": 0.2, "model": "judas"}
                  for k, g in enumerate(rng.choice([-1.0, 1.0], 300))]
        a = AN.analyse(trades)
        self.assertEqual(a["all"]["n"], 300)
        self.assertAlmostEqual(a["all"]["cost_r"], 0.1, places=9)
        self.assertEqual(a["verdict"]["code"], "NO_DIRECTION")    # +1 / -1 at random: no direction
        self.assertAlmostEqual(a["excursion"]["reached"]["1.0"]["losers"], 1.0)   # every loser had 1.3 R first
        self.assertIn("exits give back", a["verdict"]["text"])
        good = [{**t, "gross_r": 0.6, "net_r": 0.5} for t in trades]
        good = [{**t, "gross_r": t["gross_r"] + rng.normal(0, 0.1)} for t in good]
        self.assertEqual(AN.analyse(good)["verdict"]["code"], "POSITIVE")
        self.assertEqual(AN.analyse(trades[:10])["verdict"]["code"], "TOO_FEW")
        self.assertEqual({g["group"] for g in a["by"]["direction"]}, {"Long", "Short"})


class TestEdgeService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.services import Services
        from tests.dukascopy_fixture import write_fixture
        from tests.test_my_strategy import REPO
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.root / "configs")
        csv = cls.root / "combined.csv"
        write_fixture(csv, start="2023-06-04", end="2024-04-27")
        f = pd.read_csv(csv, dtype=str)
        s = 1.0 + 0.25 * (np.arange(len(f)) % 7)
        for k, extra in (("open", 0.0), ("high", 0.5), ("low", 0.0), ("close", 0.0)):
            f[f"ask_{k}"] = (f[k].astype(float) + s + extra).map(lambda x: f"{x:.3f}")
        f.to_csv(csv, index=False)
        svc = Services(root=cls.root)
        imp = svc.import_file(dict(
            file=str(csv), profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY", provider="DUKASCOPY", asset_type="CFD",
            symbol="USATECH.IDX/USD", price_basis="bid", timeframe="1m", derive_timeframes=["5m"],
            build_features=False, bid_close_column="close", ask_close_column="ask_close", ask_open_column="ask_open",
            ask_high_column="ask_high", ask_low_column="ask_low", dataset_name="DUKA_SYN"))
        cls.parent = svc.create_protocol(imp["derived"][0], ("2023-06-05", "2024-03-01"), ("2024-03-04", "2024-04-26"),
                                         name="parent", exposure_statement="none", trial_budget=20)
        svc.store.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_run_cache_discovery_only(self):
        from edgelab.services import Services
        svc = Services(root=self.root)
        runs_before = len(svc.store.list_runs()) if hasattr(svc.store, "list_runs") else None
        out = C.run(svc, lock=svc.lock)
        self.assertEqual([r["id"] for r in out["results"]], ["H1", "H2", "H3", "H4"])
        self.assertEqual(out["family"], 4)
        self.assertLess(pd.Timestamp(out["window"]["end"]), pd.Timestamp("2024-03-04 09:30", tz="America/New_York"))  # never the holdout
        self.assertGreater(out["days"]["usable"], 100)
        r1 = out["results"][0]
        self.assertIn(r1["verdict"], C.VERDICT_TEXT)
        self.assertGreater(r1["cost_pts"], 0)                                        # spread + configured costs
        self.assertEqual(C.run(svc, lock=svc.lock)["computed_at"], out["computed_at"])   # cached by its inputs
        self.assertEqual(C.latest(svc)["key"], out["key"])
        if runs_before is not None:
            self.assertEqual(len(svc.store.list_runs()), runs_before)               # never a run
        st = svc.edge_status()
        self.assertEqual(st["latest"]["key"], out["key"])
        job = svc.edge_run()                                                         # the job path (twice: no clash)
        while svc.edge_job(job["job_id"])["state"] == "running":
            import time
            time.sleep(0.2)
        self.assertEqual(svc.edge_job(job["job_id"])["state"], "completed")
        self.assertIsNone(svc.edge_status()["job"])
        self.assertEqual(st["set"]["family"], 4)
        svc.store.close()

    def test_api(self):
        from edgelab.web.app import create_app
        c = create_app(self.root).test_client()
        self.assertEqual(c.get("/api/edge").status_code, 200)
        self.assertEqual(c.get("/api/edge/anatomy/../x").status_code, 404)
        self.assertEqual(c.get("/api/edge/anatomy/BT_20240101_000000_abcd").status_code, 404)
        self.assertEqual(c.get("/api/edge/jobs/nope").status_code, 400)


if __name__ == "__main__":
    unittest.main()
