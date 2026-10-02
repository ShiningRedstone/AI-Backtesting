"""ADR-85: holdout backtests of chosen survivors. SYNTHETIC data only (the Dukascopy-shaped BID/ASK fixture).

- the gate: a 5m strategy is tested on the 5m dataset derived from a 1-minute protocol source, cut to exactly the holdout
  trading dates; outside a granted access those bars stay locked; a problem found before the grant costs no look;
- the ranking: known answers for the prop-weighted average rank (discovery numbers only);
- the job: survivors only, one test each, within the looks left, one research job at a time; the results view lists the
  holdout runs with the verdict and the discovery figures beside them."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import yaml

from edgelab.research import holdout as H
from edgelab.research import overview as ov
from edgelab.research import protocol as rp
from edgelab.research.protocol import ProtocolRefusal
from edgelab.services import Services
from tests.dukascopy_fixture import write_fixture

REPO = Path(__file__).resolve().parents[1]
FX = REPO / "strategies" / "fixtures"
DISC, HOLD = ("2024-03-04", "2024-04-05"), ("2024-04-08", "2024-04-26")


def fixture(name):
    return yaml.safe_load((FX / name).read_text())


class HoldoutBase(unittest.TestCase):
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
        cls.ema = svc.save_strategy(fixture("ema_crossover.yaml"))["strategy_id"]          # 5m strategies
        cls.rsi = svc.save_strategy(fixture("rsi_threshold.yaml"))["strategy_id"]
        svc.store.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def svc(self):
        s = Services(root=self.root)
        self.addCleanup(s.store.close)
        return s

    def protocol(self, s, name, looks=None):
        """A fresh ACTIVE protocol on the 1-MINUTE source import, and a discovery search of both strategies on 5m."""
        for p in s.store.list_protocols(status="ACTIVE"):
            s.retire_protocol(p["protocol_id"])
        p = s.create_protocol(self.src1m, DISC, HOLD, name=name, holdout_looks=looks)
        from edgelab.research.campaign import discovery_period
        per = discovery_period(p["material"])
        srch = s.run_search({"strategies": {"ids": [self.ema, self.rsi]}, "datasets": [self.d5],
                             "period": {"start": per["start"], "end": per["end"]}})
        return p, srch["search_id"]

    def refused(self, code, fn, *a, **kw):
        with self.assertRaises(ProtocolRefusal) as cm:
            fn(*a, **kw)
        self.assertEqual(cm.exception.code, code, cm.exception.to_dict())
        return cm.exception


class TestGate(HoldoutBase):
    def test_5m_strategy_on_a_1m_protocol_and_locks_unchanged(self):
        s = self.svc()
        p, sid = self.protocol(s, "gate")
        pid, h = p["protocol_id"], p["material"]["windows"]["holdout"]
        hold5 = (h["boundary_open"], h["last_bar"])
        self.refused("HOLDOUT_LOCKED", s.backtest_strategy, self.ema, self.d5, False, hold5)    # no access: locked
        self.refused("HOLDOUT_ACCESS_INVALID", s._run_cell, self.ema, self.d5, False, period=hold5,
                     entry_point="holdout_evaluation", holdout_access_id="HA_FAKE")
        n_trials = s.protocol_status(pid)["trials"]["unique_numerical_trials"]
        s.select_shortlist(sid, [self.ema])
        out = s.evaluate_holdout(pid, sid, self.ema)
        self.assertIn(out["outcome"], ("HOLDOUT_CRITERIA_MET", "HOLDOUT_CRITERIA_NOT_MET"))
        rec, trades = s.store.load_run(out["run_id"])
        d = rec["dataset"]
        self.assertEqual((rec["status"], d["timeframe"]), ("OUT_OF_SAMPLE", "5m"))
        self.assertEqual(pd.Timestamp(d["start"]), pd.Timestamp(h["first_bar"]))                  # first holdout bar
        self.assertLessEqual(pd.Timestamp(d["end"]), pd.Timestamp(h["last_bar"]))
        self.assertGreater(pd.Timestamp(d["end"]) + pd.Timedelta(minutes=5), pd.Timestamp(h["last_bar"]))
        self.assertEqual(s.protocol_status(pid)["trials"]["unique_numerical_trials"], n_trials)   # holdout != trial
        self.assertEqual([a["status"] for a in s.store.list_holdout_access(pid)], ["completed"])

    def test_a_problem_found_before_the_grant_costs_no_look(self):
        from edgelab.engine.costs import CostConfigError
        s = self.svc()
        p, sid = self.protocol(s, "precheck", looks=1)
        pid = p["protocol_id"]
        s.select_shortlist(sid, [self.rsi])
        with mock.patch("edgelab.engine.costs.cost_model_from_config", side_effect=CostConfigError("unconfigured")):
            self.refused("HOLDOUT_DATASET_UNRESOLVED", s.evaluate_holdout, pid, sid, self.rsi)
        led = s.store.list_holdout_access(pid)
        self.assertEqual([a["status"] for a in led], ["refused"])                                 # not granted
        out = s.evaluate_holdout(pid, sid, self.rsi)                                              # the one look
        self.assertEqual(out["holdout_looks_used"], 1)

    def test_stage_of_known_answers(self):
        F, L, B = 1_000 * 60_000_000_000, 2_000 * 60_000_000_000, 999 * 60_000_000_000
        p = {"material": {"windows": {"discovery": {"boundary_open_ns": 0},
                                      "holdout": {"first_bar_ns": F, "last_bar_ns": L, "boundary_open_ns": B}}}}
        m5 = 5 * 60_000_000_000
        self.assertEqual(rp.stage_of(p, F, L), "holdout")                                  # source bars: as before
        self.assertEqual(rp.stage_of(p, F, L - 60_000_000_000), "overlap")                 # 1m: must be exact
        self.assertEqual(rp.stage_of(p, F, L - 4 * 60_000_000_000, m5), "holdout")         # 5m bar contains the last
        self.assertEqual(rp.stage_of(p, F, L - 5 * 60_000_000_000, m5), "overlap")         # a bar short
        self.assertEqual(rp.stage_of(p, B - m5, L - 4 * 60_000_000_000, m5), "overlap")    # starts before the holdout
        self.assertEqual(rp.stage_of(p, B, L - 4 * 60_000_000_000, m5), "holdout")         # opens at the session open
        self.assertEqual(rp.stage_of(p, 10, B - 1), "discovery")


class TestRanking(unittest.TestCase):
    def test_prop_weighted_average_rank(self):
        base = {"trades_per_week": 2.0, "negative_months": 3, "avg_rr": 2.0, "profit_factor": 1.5,
                "max_drawdown_r": 5.0, "expectancy_r": 0.2, "net_r": 20.0, "max_loss_streak": 5}
        a = {"strategy_id": "A", **base}
        b = {"strategy_id": "B", **base, "max_drawdown_r": 4.0}                 # better drawdown (weight 2)
        c = {"strategy_id": "C", **base, "trades_per_week": 3.0}                # better trades/week (weight 1)
        out = H.rank_rows([a, b, c])
        self.assertEqual([r["strategy_id"] for r in out], ["B", "C", "A"])
        # B: dd rank 1 (w2), others tied: tpw (A,B tie 2.5), so score = (2.5 + 2*2 + ... ) / 10 -> check exactly
        total_w = 10.0
        exp_b = (2.5 + 2 * 2 + 2 + 2 + 2 * 1 + 2 + 2 + 2) / total_w
        exp_c = (1 + 2 * 2 + 2 + 2 + 2 * 2.5 + 2 + 2 + 2) / total_w
        self.assertAlmostEqual(out[0]["score"], exp_b)
        self.assertAlmostEqual(out[1]["score"], exp_c)
        self.assertEqual([r["position"] for r in out], [1, 2, 3])
        d = {"strategy_id": "D", **base, "negative_months": None}               # missing ranks last
        self.assertEqual(H.rank_rows([d, dict(a)])[0]["strategy_id"], "A")
        neg = {"strategy_id": "N", **base, "max_drawdown_r": -4.0}              # sign convention does not matter
        self.assertEqual(H.rank_rows([dict(a), neg])[0]["strategy_id"], "N")


class TestJobAndResults(HoldoutBase):
    def test_candidates_job_results_and_refusals(self):
        s = self.svc()
        p, sid = self.protocol(s, "job", looks=1)
        survivors = {self.ema, self.rsi}
        real = ov.apply_criteria

        def as_survivor(row, profile):                     # SYNTHETIC data rarely passes a prop payout: pin the label
            out = real(row, profile)
            out["survivor"] = row.get("strategy_id") in survivors and row.get("status") == "IN_SAMPLE"
            return out
        ov._FACET_CACHE.clear()
        with mock.patch.object(ov, "apply_criteria", as_survivor):
            c = s.holdout_candidates()
            self.assertEqual({r["strategy_id"] for r in c["rows"]}, survivors)
            self.assertTrue(all(r["eligible"] and r["search_id"] == sid for r in c["rows"]))
            self.assertEqual(c["protocol"]["looks_left"], 1)
            self.assertEqual([r["position"] for r in c["rows"]], [1, 2])
            with self.assertRaises(ValueError):
                s.start_holdout_job([self.ema, self.rsi])                     # 2 selected, 1 look left
            with self.assertRaises(ValueError):
                s.start_holdout_job(["STR_000000000000"])                     # not a survivor
            import threading
            from edgelab.research.jobs import JobConflict
            gate, real_run = threading.Event(), H.run_items

            def held(svc, job, lock):                                         # keeps the first job running
                gate.wait(30)
                job.cancel_requested.set()
                real_run(svc, job, lock)
            with mock.patch.object(H, "run_items", held):
                first = s.start_holdout_job([self.ema])
                with self.assertRaises(JobConflict):
                    s.start_holdout_job([self.rsi])                           # one research job at a time
                gate.set()
                self.assertTrue(s.jobs.join(60))
            cancelled = s.holdout_job(first["job_id"])
            self.assertEqual((cancelled["state"], cancelled["items"][0]["state"]), ("cancelled", "cancelled"))
            self.assertEqual(s.holdout_candidates()["protocol"]["looks_left"], 1)      # cancelled before: no look used
            job = s.start_holdout_job([self.ema])
            self.assertTrue(s.jobs.join(600))
            st = s.holdout_job(job["job_id"])
            self.assertEqual((st["state"], st["items"][0]["state"]), ("completed", "completed"))
            self.assertIn(self.ema, json_tag(s, sid))                         # shortlisted automatically
            ov._FACET_CACHE.clear()
            c2 = s.holdout_candidates()
            tested = next(r for r in c2["rows"] if r["strategy_id"] == self.ema)
            self.assertFalse(tested["eligible"])
            self.assertEqual(tested["tested"]["run_id"], st["items"][0]["run_id"])
            self.assertEqual(c2["protocol"]["looks_left"], 0)
            res = s.explore_strategies({"scope": "holdout", "tested_only": "1"})
            self.assertEqual([r["strategy_id"] for r in res["rows"]], [self.ema])
            row = res["rows"][0]
            self.assertEqual(row["ref_run"]["run_id"], st["items"][0]["run_id"])
            self.assertEqual(row["holdout_outcome"], st["items"][0]["outcome"])
            self.assertIsNotNone(row["discovery_expectancy_r"])
            self.assertIn(row["holdout_cost_stress_met"], (True, False))
            panel = s.strategy_panel(self.ema, {"scope": "holdout"})
            self.assertTrue(panel["tested"])
            self.assertEqual((panel["run_id"], panel["is_holdout"]), (st["items"][0]["run_id"], True))   # labelled Holdout
            normal = s.explore_strategies({"scope": "in_sample", "strategy_id": self.ema})["rows"][0]
            self.assertNotEqual(normal["ref_run"]["run_id"], row["ref_run"]["run_id"])   # Strategies stays discovery
            hist = s.holdout_history()
            self.assertEqual([h["status"] for h in hist], ["completed"])


def json_tag(s, sid):
    import json
    b = s.store.get_search_batch(sid)
    return (json.loads(b["shortlist_json"]) if b.get("shortlist_json") else {}).get("strategy_ids") or []


class TestHoldoutHttp(HoldoutBase):
    def test_routes(self):
        from edgelab.web.app import create_app
        app = create_app(self.root)
        s = app.config["EDGELAB"]["services"]
        self.addCleanup(s.store.close)
        self.protocol(s, "http")
        c = app.test_client()
        r = c.get("/api/holdout/candidates")
        self.assertEqual(r.status_code, 200)
        self.assertIn("ranking_rule", r.get_json())
        self.assertEqual(c.get("/api/holdout/history").status_code, 200)
        self.assertEqual(c.post("/api/holdout/jobs", json={"strategy_ids": []}).status_code, 400)
        self.assertEqual(c.post("/api/holdout/jobs", json={"strategy_ids": [self.ema]}).status_code, 400)  # no survivor
        self.assertEqual(c.get("/api/holdout/jobs/JOB_000000000000").status_code, 404)
        self.assertEqual(c.get("/api/explorer/strategies?scope=holdout").status_code, 200)
        self.assertEqual(c.get(f"/api/results-view/strategies/{self.ema}?scope=holdout").status_code, 200)


if __name__ == "__main__":
    unittest.main()
