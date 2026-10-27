"""ADR-116: the noise band set. SYNTHETIC data only (the Dukascopy-shaped BID/ASK fixture of the holdout tests).

- the feature ``noise_area``: known answer against a plain re-computation (the average |close / 9:30 open - 1| of the
  same slot over the previous dates, the band around max / min(9:30 open, previous close)), prefix invariance, and the
  engine's lookahead check on a strategy that reads it;
- the space: deterministic, the user's fixed rules in every version (NQ datasets, entry window from 9:30, entry only on
  a close outside the band with the break), 1-3 trades a day, distinct logic;
- the set protocol: its own budget (= the set) and holdout looks, its strategies governed by it wherever they are
  evaluated (never by the parent, and ordinary strategies never by it), its holdout gate, retired with the parent;
- the services / API: create job, run job, results picker."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from edgelab.research import protocol as rp
from edgelab.research.protocol import ProtocolRefusal
from edgelab.services import Services
from edgelab.strategy import noiseband_space as N
from tests.dukascopy_fixture import write_fixture

REPO = Path(__file__).resolve().parents[1]
DISC, HOLD = ("2024-03-04", "2024-04-05"), ("2024-04-08", "2024-04-26")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.root / "configs")
        csv = cls.root / "combined.csv"
        write_fixture(csv, end="2024-04-27")
        f = pd.read_csv(csv, dtype=str)
        s = 1.0 + 0.25 * (np.arange(len(f)) % 7)
        for k, extra in (("open", 0.0), ("high", 0.5), ("low", 0.0), ("close", 0.0)):
            f[f"ask_{k}"] = (f[k].astype(float) + s + extra).map(lambda x: f"{x:.3f}")
        f.to_csv(csv, index=False)
        svc = Services(root=cls.root)
        r = svc.import_file(dict(
            file=str(csv), profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY", provider="DUKASCOPY",
            asset_type="CFD", symbol="USATECH.IDX/USD", price_basis="bid", timeframe="1m", derive_timeframes=["5m"],
            build_features=False, bid_close_column="close", ask_close_column="ask_close", ask_open_column="ask_open",
            ask_high_column="ask_high", ask_low_column="ask_low", dataset_name="DUKA_SYN"))
        cls.src1m, cls.d5 = r["dataset_id"], r["derived"][0]
        svc.store.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)


def _inp(ds):
    from edgelab.features.context import FeatureInput
    b = ds.bars
    close_ns = np.asarray(b.ts_ns, dtype=np.int64) + int(b.tf_minutes) * 60_000_000_000
    return FeatureInput(bars=b, ts_close_ns=close_ns, calendar=ds.calendar, sessions={}, volume_type="none",
                        tick_size=0.25, dep=None)


class TestFeature(Base):
    def test_known_answer_and_prefix_invariance(self):
        from edgelab.features.library.noise import _noise
        svc = Services(root=self.root)
        self.addCleanup(svc.store.close)
        ds = svc.load_dataset(self.d5)
        p = {"lookback": 5, "mult": 1.2, "gap": True}
        out = _noise(_inp(ds), p)
        b = ds.bars
        ny = b.ts.tz_convert("America/New_York")
        td = ds.calendar.trading_dates_of(b).astype("datetime64[D]")
        frame = pd.DataFrame({"o": b.open, "c": b.close, "td": td, "mod": ny.hour * 60 + ny.minute,
                              "nyday": ny.tz_localize(None).normalize().values.astype("datetime64[D]")})
        rth = frame[(frame["mod"] >= 570) & (frame["mod"] < 960) & (frame["nyday"] == frame["td"])]
        days = sorted(rth[rth["mod"] == 570]["td"].unique())
        checked = 0
        for d in days[8:12]:                                    # dates with 5 earlier dates available
            prev = days[days.index(d) - 5: days.index(d)]
            today = rth[rth["td"] == d]
            o930 = float(today[today["mod"] == 570]["o"].iloc[0])
            prev_close = float(rth[rth["td"] == days[days.index(d) - 1]]["c"].iloc[-1])
            for mod in (570, 600, 720, 900):
                row = today[today["mod"] == mod]
                if row.empty:
                    continue
                moves = []
                for pd_ in prev:
                    pday = rth[rth["td"] == pd_]
                    po = float(pday[pday["mod"] == 570]["o"].iloc[0])
                    pr = pday[pday["mod"] == mod]
                    if not pr.empty:
                        moves.append(abs(float(pr["c"].iloc[0]) / po - 1.0))
                sigma = float(np.mean(moves))
                i = int(row.index[0])
                self.assertAlmostEqual(out["sigma"][i], sigma, places=12)
                self.assertAlmostEqual(out["upper"][i], max(o930, prev_close) * (1 + 1.2 * sigma), places=6)
                self.assertAlmostEqual(out["lower"][i], min(o930, prev_close) * (1 - 1.2 * sigma), places=6)
                checked += 1
        self.assertGreaterEqual(checked, 12)
        self.assertTrue(np.isnan(out["upper"][frame["mod"].to_numpy() < 570]).all())          # outside 9:30-16:00
        off = _noise(_inp(ds), {**p, "gap": False})
        k = np.isfinite(off["upper"])
        np.testing.assert_allclose(off["upper"][k], off["rth_open"][k] * (1 + 1.2 * off["sigma"][k]))
        # prefix invariance: the outputs of a truncated history equal the full history's first bars
        from edgelab.data.schema import BarArrays
        cut = int(len(b) * 0.6)
        names = ("ts_ns", "open", "high", "low", "close", "volume", "spread", "ask_open", "ask_high", "ask_low", "ask_close")
        head = BarArrays(tf_minutes=b.tf_minutes, **{n: getattr(b, n)[:cut] for n in names if getattr(b, n, None) is not None})

        class D:
            pass
        dd = D()
        dd.bars, dd.calendar = head, ds.calendar
        part = _noise(_inp(dd), p)
        for key in ("upper", "lower", "sigma"):
            np.testing.assert_array_equal(part[key], out[key][:cut])

    def test_a_noise_band_strategy_passes_the_lookahead_check(self):
        svc = Services(root=self.root)
        self.addCleanup(svc.store.close)
        v = {"timeframe": "5m", "lookback": 5, "mult": 1.0, "gap": True, "trigger": "close_outside", "entry_until": "15:30",
             "close_time": "session_end", "direction": "both", "stop": "band", "target": "rr_2.0", "trailing": "band",
             "max_trades": 3}
        r = svc.backtest_strategy(N.build_definition(v), self.d5, False, ("2024-03-04", "2024-04-26"))
        self.assertTrue(r["metrics"] is not None)
        self.assertGreater(r["n_signals"], 0)


class TestSpace(Base):
    def test_fixed_rules_variations_and_determinism(self):
        svc = Services(root=self.root)
        self.addCleanup(svc.store.close)
        g = N.generate(svc.sessions, svc._config_hash(), total=300)
        g2 = N.generate(svc.sessions, svc._config_hash(), total=300)
        self.assertEqual([r["identity"]["strategy_id"] for r in g["rows"]], [r["identity"]["strategy_id"] for r in g2["rows"]])
        self.assertEqual(len({r["identity"]["logic_hash"] for r in g["rows"]}), 300)
        seen = {k: set() for k in N.DIMENSIONS}
        for r in g["rows"]:
            v, d = r["variation"], r["definition"]
            for k in N.DIMENSIONS:
                seen[k].add(v[k])
            e = d["entry"]
            self.assertEqual(d["sessions"][e["session"]]["start"], "09:30")
            self.assertIn(e["max_trades_per_day"], (1, 2, 3))
            if "long" in e:
                self.assertEqual(e["long"]["left"], {"bar": "close"})
                self.assertIn(e["long"]["op"], (">", "crosses_above"))
                self.assertEqual((e["long"]["right"]["feature"], e["long"]["right"]["output"]), ("noise_area", "upper"))
            if "short" in e:
                self.assertIn(e["short"]["op"], ("<", "crosses_below"))
                self.assertEqual((e["short"]["right"]["feature"], e["short"]["right"]["output"]), ("noise_area", "lower"))
            self.assertTrue(N.valid(v))
            self.assertEqual(d["sizing"], N.SIZING)
        self.assertEqual(seen["max_trades"], {1, 2, 3})
        self.assertEqual(seen["timeframe"], set(N.TIMEFRAMES))
        self.assertFalse(N.valid({**g["rows"][0]["variation"], "timeframe": "30m", "entry_until": "14:00", "close_time": "14:00"}))


class TestSetProtocol(Base):
    def setUp(self):
        pt = mock.patch.object(N, "TARGET_TOTAL", 12)
        pt.start()
        self.addCleanup(pt.stop)
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, True)
        shutil.copytree(self.root, self.work, dirs_exist_ok=True)

    def svc(self):
        s = Services(root=self.work)
        self.addCleanup(s.store.close)
        return s

    def test_create_run_route_holdout_retire(self):
        from edgelab.research import campaign as C, holdout as H, noiseband as NB
        s = self.svc()
        self.assertFalse(s.noise_band()["ready"])                              # no protocol: a state, not an error
        p = s.create_protocol(self.src1m, DISC, HOLD, name="nb")
        pid = p["protocol_id"]
        st = s.noise_band()
        self.assertTrue(st["ready"])
        self.assertIsNone(st["set"])
        out = NB.create(s)
        sid_p = out["protocol_id"]
        self.assertEqual((out["count"], out["skipped_timeframes"]), (12, ["15m", "30m"]))   # only 1m / 5m datasets here
        sp = s.get_protocol(sid_p)
        mat = sp["material"]
        self.assertTrue(rp.is_noise_set(sp) and rp.is_companion(sp))
        self.assertEqual(mat["trial_budget"]["max_unique_trials"], 12)
        self.assertEqual(rp.family_size(mat, 0), 12)
        self.assertEqual((mat["windows"], mat["config_hash"]), (p["material"]["windows"], p["material"]["config_hash"]))
        self.assertEqual(mat["design"]["design_hash"], N.design_hash())
        with self.assertRaises(NB.NoiseBandError) as cm:
            NB.create(s)
        self.assertEqual(cm.exception.code, "SET_EXISTS")
        self.assertEqual(C.governing_protocol(s)["protocol_id"], pid)          # never the set protocol
        rows = NB.load_set(s, sp)
        ids = [r["strategy_id"] for r in rows]
        for r in rows:
            doc = s.library.load(r["strategy_id"])
            self.assertEqual(doc["logic_hash"], r["logic_hash"])
        # the set file is checked against the protocol
        path = NB._path(s, mat["strategy_set"]["hash"])
        NB._SETS.clear()
        txt = path.read_text()
        path.write_text(txt.replace('"long"', '"short"', 1))
        with self.assertRaises(NB.NoiseBandError) as cm:
            NB.load_set(s, sp)
        self.assertEqual(cm.exception.code, "SET_FILE_CHANGED")
        path.write_text(txt)
        NB._SETS.clear()

        res = NB.run(s, sid_p)
        b = s.store.get_search_batch(res["search_id"])
        self.assertEqual(b["protocol_id"], sid_p)
        self.assertEqual(s.store.count_trials(sid_p), 12)                       # one trial per registered strategy
        self.assertEqual(s.store.count_trials(pid), 0)                          # the parent is untouched
        again = NB.run(s, sid_p)
        self.assertEqual((again["search_id"], s.store.count_trials(sid_p)), (res["search_id"], 12))
        self.assertEqual(s.noise_band()["results"]["backtested"], 12)
        self.assertIn({"pool": "noise", "ref": "set:noise", "label": "Noise band", "n_strategies": 12}, s.strategy_pools())

        per = C.discovery_period(p["material"])
        ref = {"instrument": "NQ_DUKASCOPY", "provider": "DUKASCOPY"}
        self.assertEqual(H._protocol_of(s, ref, rows[0]["logic_hash"])["protocol_id"], sid_p)
        self.assertEqual(H._protocol_of(s, ref, None)["protocol_id"], pid)
        five = next(r for r in rows if r["timeframe"] == "5m")
        s.backtest_strategy(five["strategy_id"], self.d5, False, (per["start"], per["end"]))
        self.assertEqual(s.store.list_trial_events(sid_p)[-1]["strategy_id"], five["strategy_id"])
        ema = s.save_strategy((REPO / "strategies" / "fixtures" / "ema_crossover.yaml").read_text())["strategy_id"]
        with self.assertRaises(ProtocolRefusal) as cm:
            s.run_search({"strategies": {"ids": [five["strategy_id"], ema]}, "datasets": [self.d5],
                          "period": {"start": per["start"], "end": per["end"]}})
        self.assertEqual(cm.exception.code, "PROTOCOL_MISMATCH")

        s.select_shortlist(res["search_id"], [five["strategy_id"]])
        with self.assertRaises(ProtocolRefusal):
            s.evaluate_holdout(pid, res["search_id"], five["strategy_id"])
        hv = s.evaluate_holdout(sid_p, res["search_id"], five["strategy_id"])
        self.assertIn(hv["outcome"], ("HOLDOUT_CRITERIA_MET", "HOLDOUT_CRITERIA_NOT_MET"))
        self.assertEqual(hv["multiple_testing"]["family_size"], 12)
        self.assertFalse([a for a in s.store.list_holdout_access(pid) if a["status"] != "refused"])
        self.assertEqual(s.noise_band()["set"]["holdout_looks_used"], 1)

        s.retire_protocol(pid)
        self.assertEqual(s.get_protocol(sid_p)["status"], "RETIRED")
        self.assertEqual(len(ids), 12)

    def test_jobs_and_api(self):
        s = self.svc()
        s.create_protocol(self.src1m, DISC, HOLD, name="nb-api")
        s.store.close()
        from edgelab.web.app import create_app
        c = create_app(self.work).test_client()
        v = c.get("/api/noise-band").get_json()
        self.assertTrue(v["ready"])
        self.assertEqual(c.post("/api/noise-band/jobs", json={}).status_code, 422)          # create first
        j = c.post("/api/noise-band", json={}).get_json()
        import time
        for _ in range(600):
            d = c.get(f"/api/noise-band/jobs/{j['job_id']}").get_json()
            if d["state"] in ("completed", "failed"):
                break
            time.sleep(0.5)
        self.assertEqual(d["state"], "completed", d.get("error"))
        self.assertEqual(d["result"]["count"], 12)
        self.assertEqual(c.post("/api/noise-band", json={}).status_code, 409)               # one set per protocol
        r = c.post("/api/noise-band/jobs", json={})
        self.assertEqual(r.status_code, 202)
        jid = r.get_json()["job_id"]
        self.assertEqual(r.get_json()["kind"], "noise_band")
        for _ in range(1200):
            d = c.get(f"/api/noise-band/jobs/{jid}").get_json()
            if d["state"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.5)
        self.assertEqual(d["state"], "completed", d.get("error"))
        self.assertEqual(c.get("/api/noise-band").get_json()["set"]["trials_used"], 12)
        pools = c.get("/api/results-view/pools").get_json()
        self.assertEqual([p["ref"] for p in pools if p["pool"] == "noise"], ["set:noise"])
        ov = c.get("/api/results-view/overview?campaign_run=set:noise").get_json()
        self.assertEqual((ov["facts"]["strategies"], ov["facts"]["tested"]), (12, 12))


if __name__ == "__main__":
    unittest.main()
