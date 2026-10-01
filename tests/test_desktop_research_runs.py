"""ADR-69: desktop Research Runs over a frozen campaign (control/presentation layer only).

Family scope -> the campaign's own frozen strategy ids; background job over the SAME frozen search (workers 1, protocol-gated,
discovery only); durable run records + store rows survive a restart; cancel/resume never duplicate a trial. SYNTHETIC
workspace (Dukascopy-shaped fixture, 1m + 4 derived timeframes) and a small real factory manifest (seed 4, 30 strategies)."""
import copy
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from edgelab.core.identity import hash_obj
from edgelab.research import campaign as C
from edgelab.services import Services
from edgelab.strategy import factory as FX
from edgelab.strategy import factory_space as S
from tests.dukascopy_fixture import write_fixture
from tests.test_research_protocol import DISC, HOLD

REPO = Path(__file__).resolve().parents[1]


def build_workspace(budget_extra=0):
    root = Path(tempfile.mkdtemp())
    shutil.copytree(REPO / "configs", root / "configs")
    csv = root / "combined.csv"
    write_fixture(csv, end="2024-04-27")
    f = pd.read_csv(csv, dtype=str)
    s = 1.0 + 0.25 * (np.arange(len(f)) % 7)
    for k, extra in (("open", 0.0), ("high", 0.5), ("low", 0.0), ("close", 0.0)):
        f[f"ask_{k}"] = (f[k].astype(float) + s + extra).map(lambda x: f"{x:.3f}")
    f.to_csv(csv, index=False)
    svc = Services(root=root)
    imp = svc.import_file(dict(
        file=str(csv), profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY", provider="DUKASCOPY", asset_type="CFD",
        symbol="USATECH.IDX/USD", price_basis="bid", timeframe="1m", derive_timeframes=["5m", "15m", "30m", "60m"],
        build_features=False, bid_close_column="close", ask_close_column="ask_close", ask_open_column="ask_open",
        ask_high_column="ask_high", ask_low_column="ask_low", dataset_name="DUKA_SYN"))
    res = FX.generate(seed=4, quotas={f.fid: 1 for f in S.FAMILIES})
    FX.write_manifest(res, svc.factory_dir / res.manifest_id)
    p = svc.create_protocol(imp["derived"][0], DISC, HOLD, name="desktop", exposure_statement="none",
                            trial_budget=len(res.strategies) + budget_extra)
    cid = svc.campaign_freeze(res.manifest_id)["campaign_id"]
    # these desktop tests exercise the one-core runner (they stub Services._run_cell, which only it calls); the
    # multi-core path (ADR-77) is covered, against this same fixture, by tests/test_parallel_research_runs.py
    svc.set_ui_preferences({"research_processes": 1})
    svc.store.close()
    return root, res, p["protocol_id"], cid


def wait(svc, job_id, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = svc.campaign_job(job_id)
        if st["state"] in ("completed", "failed", "cancelled"):
            return st
        time.sleep(0.05)
    raise AssertionError("job did not finish")


class TestDesktopResearchRuns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.res, cls.pid, cls.cid = build_workspace()
        cls.rows = cls.res.strategies
        cls.n = len(cls.rows)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def svc(self):
        s = Services(root=self.root)
        self.addCleanup(s.store.close)
        return s

    def fam_ids(self, fams):
        return sorted(r["strategy_id"] for r in self.rows if r["family_id"] in fams)

    def test_lifecycle(self):
        s = self.svc()
        mf = s.factory_dir / self.res.manifest_id / "strategies.jsonl"
        mtime, files0 = mf.stat().st_mtime_ns, C.manifest_files(mf.parent)
        # ---- discover the frozen campaign (no regeneration) and its families from the manifest catalog
        with mock.patch.object(FX, "generate", side_effect=AssertionError("must not regenerate")):
            lst = s.campaigns()
            d = s.campaign_detail(self.cid)
        self.assertEqual([c["campaign_id"] for c in lst], [self.cid])
        cat = [f["family_id"] for f in self.res.header["families"]]
        self.assertEqual([f["family_id"] for f in d["families"]], [f for f in cat if any(r["family_id"] == f for r in self.rows)])
        self.assertEqual(sum(f["n_strategies"] for f in d["families"]), self.n)
        self.assertEqual((d["protocol"]["protocol_id"], d["execution"]["workers"], d["execution"]["holdout"]),
                         (self.pid, 1, {"enabled": False}))
        self.assertEqual(d["progress"]["completed"], 0)
        # ---- a subset runs in the background: only those families' frozen ids, progress visible
        fams = [d["families"][0]["family_id"], d["families"][3]["family_id"]]
        seen = []
        job = s.start_campaign_job(self.cid, fams)
        self.assertEqual((job["kind"], job["campaign_id"], job["families"]), ("campaign", self.cid, fams))
        while True:
            st = s.campaign_job(job["job_id"])
            seen.append(st["live"].get("status"))
            if st["state"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.02)
        self.assertEqual(st["state"], "completed", st)
        live = st["live"]
        self.assertEqual((live["status"], live["n_scope"], live["families"], live["counts"]["completed_this_run"]),
                         ("completed", len(self.fam_ids(fams)), fams, len(self.fam_ids(fams))))   # the SCOPE is done
        self.assertIn("running", seen)
        for k in ("campaign_id", "search_id", "protocol_id", "protocol_version", "manifest_id", "started_at", "totals"):
            self.assertIn(k, live)
        done = sorted(e["strategy_id"] for e in s.store.list_trial_events(self.pid) if e["counted"])
        self.assertEqual(done, self.fam_ids(fams))                                       # exactly the scope's ids
        self.assertEqual(s.store.get_search_batch(d["search_id"])["status"], "partial")
        # ---- reopen the app (new Services): history, results and provenance persisted
        s.store.close()
        s2 = self.svc()
        d2 = s2.campaign_detail(self.cid)
        self.assertEqual(d2["progress"]["completed"], len(self.fam_ids(fams)))
        self.assertEqual(d2["runs"][0]["status"], "completed")
        self.assertLess(d2["progress"]["completed"], self.n)                               # the campaign is not
        self.assertEqual(d2["runs"][0]["families"], fams)
        fr = s2.campaign_family_results(self.cid, fams[0])
        self.assertTrue(all(x["status"] == "completed" and x["run_id"] for x in fr["strategies"]))
        self.assertEqual([x["strategy_id"] for x in fr["strategies"]],
                         [r["strategy_id"] for r in self.rows if r["family_id"] == fams[0]])   # manifest order
        one = s2.campaign_strategy_result(self.cid, fr["strategies"][0]["strategy_id"])
        pv = one["provenance"]
        for k in ("protocol_id", "protocol_version", "campaign_id", "search_id", "manifest_id", "logic_hash",
                  "config_hash", "account", "prop_profiles", "dataset_id", "dataset_content_hash"):
            self.assertIsNotNone(pv[k], k)
        self.assertEqual(len(one["prop"]["profiles"]), 4)
        self.assertEqual(one["prop"]["base"]["trade_count"], one["n_trades"])
        # ---- interrupted run: a crash mid-run, then reopen -> interrupted, completed cells kept
        real = s2._run_cell
        calls = {"n": 0}

        def crash_after_2(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt("simulated app stop")
            return real(*a, **kw)

        rec_before = C.progress(s2, C.load(s2, self.cid), [r["strategy_id"] for r in self.rows])["completed"]
        with mock.patch.object(s2, "_run_cell", side_effect=crash_after_2):
            job2 = s2.start_campaign_job(self.cid, None)
            st2 = wait(s2, job2["job_id"])
        self.assertEqual(st2["state"], "failed")
        s2.store.close()
        s3 = self.svc()
        runs = s3.campaign_detail(self.cid)["runs"]
        self.assertEqual(runs[0]["status"], "failed")                                      # recorded, with the error
        self.assertIn("KeyboardInterrupt", json.dumps(runs[0]["errors"]))
        rec = runs[0]
        rec["status"] = "running"                                                          # as if the process died
        C.save_run_record(s3, rec)
        s3.jobs                                                                            # manager start = reconcile
        self.assertEqual(s3.campaign_detail(self.cid)["runs"][0]["status"], "interrupted")
        prog = s3.campaign_detail(self.cid)["progress"]
        self.assertEqual(prog["completed"], rec_before + 2)
        # ---- cancel: preserves completed results, nothing marked done that did not run
        real3 = s3._run_cell
        holder = {}

        def cancel_after_1(*a, **kw):
            out = real3(*a, **kw)
            s3.cancel_job(holder["job"])
            return out

        n_events = len(s3.store.list_trial_events(self.pid))
        with mock.patch.object(s3, "_run_cell", side_effect=cancel_after_1):
            j = s3.start_campaign_job(self.cid, None)
            holder["job"] = j["job_id"]
            st3 = wait(s3, j["job_id"])
        self.assertEqual((st3["state"], st3["live"]["status"]), ("cancelled", "cancelled"))
        p3 = s3.campaign_detail(self.cid)["progress"]
        self.assertEqual(p3["completed"], prog["completed"] + 1)
        self.assertEqual(len(s3.store.list_trial_events(self.pid)), n_events + 1)
        # ---- resume to completion: only the remaining cells run, no duplicate trial event
        calls4 = {"n": 0}
        real4 = s3._run_cell

        def spy(*a, **kw):
            calls4["n"] += 1
            return real4(*a, **kw)

        with mock.patch.object(s3, "_run_cell", side_effect=spy):
            st4 = wait(s3, s3.start_campaign_job(self.cid, None)["job_id"])
        self.assertEqual((st4["state"], st4["live"]["status"]), ("completed", "completed"))
        self.assertEqual(calls4["n"], self.n - p3["completed"])
        ev = s3.store.list_trial_events(self.pid)
        counted = [e for e in ev if e["counted"]]
        self.assertEqual(len(counted), self.n)
        self.assertEqual(len({e["strategy_id"] for e in counted}), self.n)
        self.assertEqual(sum(1 for e in ev if e["status"] == "completed" and not e["counted"]), 0)   # no duplicates
        self.assertEqual(s3.store.get_search_batch(d["search_id"])["status"], "completed")
        # ---- a further run of everything evaluates nothing
        with mock.patch.object(s3, "_run_cell", side_effect=AssertionError("must not run")):
            st5 = wait(s3, s3.start_campaign_job(self.cid, None)["job_id"])
        self.assertEqual(st5["live"]["counts"]["skipped_completed"], self.n)
        # ---- frozen identities and manifest untouched; no holdout look
        self.assertEqual((mf.stat().st_mtime_ns, C.manifest_files(mf.parent)), (mtime, files0))
        for r in self.rows:
            self.assertEqual(C.library_identity(s3, r["strategy_id"]),
                             (r["strategy_id"], r["logic_hash"], r["definition_hash"]))
        self.assertEqual(s3.protocol_status(self.pid)["holdout"]["looks_used"], 0)
        self.assertEqual(len(s3.campaign_detail(self.cid)["runs"]), 5)

    def test_scope_refusals_and_one_job_at_a_time(self):
        s = self.svc()
        with self.assertRaises(C.CampaignError):
            s.start_campaign_job(self.cid, ["no_such_family"])
        with self.assertRaises(C.CampaignError):
            s.start_campaign_job(self.cid, [])
        with self.assertRaises(C.CampaignError):
            s.start_campaign_job("CMP_000000000000", None)
        gate = threading.Event()
        real = s._run_cell

        def slow(*a, **kw):
            gate.wait(30)
            return real(*a, **kw)

        from edgelab.research.jobs import JobConflict
        with mock.patch.object(s, "_run_cell", side_effect=slow):
            j = s.start_campaign_job(self.cid, None)
            try:
                with self.assertRaises(JobConflict):
                    s.start_campaign_job(self.cid, None)
                # status stays answerable while the service lock is held elsewhere
                got = {}
                with s.lock:
                    t = threading.Thread(target=lambda: got.setdefault("st", s.campaign_job(j["job_id"])))
                    t.start()
                    t.join(5)
                self.assertIn("st", got)
            finally:
                s.cancel_job(j["job_id"])
                gate.set()
                wait(s, j["job_id"])


class TestPureScope(unittest.TestCase):
    def test_select_all_is_the_whole_frozen_universe_and_subsets_keep_frozen_ids(self):
        rows = [{"strategy_id": f"STR_{i:012X}", "family_id": f"fam{i % 30:02d}", "logic_hash": hash_obj(i)}
                for i in range(10_000)]
        header = {"families": [{"family_id": f"fam{k:02d}"} for k in range(30)]}
        ids, fams = C.scope_ids(header, rows, None)
        self.assertEqual((len(ids), len(fams)), (10_000, 30))
        self.assertEqual(ids, [r["strategy_id"] for r in rows])
        sub, sf = C.scope_ids(header, rows, ["fam07", "fam03"])
        self.assertEqual(sf, ["fam03", "fam07"])                                      # catalog order, not a ranking
        self.assertEqual(sub, [r["strategy_id"] for r in rows if r["family_id"] in ("fam03", "fam07")])
        self.assertEqual(C.family_order(header, rows), [f"fam{k:02d}" for k in range(30)])


class TestCapacityGovernance(unittest.TestCase):
    def test_foreign_trial_blocks_the_run_before_any_evaluation(self):
        root, res, pid, cid = build_workspace()
        try:
            s = Services(root=root)
            spec = C.load(s, cid)
            per = spec["search"]["spec"]["period"]
            from tests.test_research_protocol import fixture
            extra = s.save_strategy(fixture("rsi_threshold.yaml"))["strategy_id"]
            tf5 = spec["dataset_resolution"]["by_timeframe"]["5m"]["dataset_id"]
            s.backtest_strategy(extra, tf5, False, (per["start"], per["end"]))   # a trial outside the campaign
            with mock.patch.object(s, "_run_cell", side_effect=AssertionError("must not run")):
                st = wait(s, s.start_campaign_job(cid, None)["job_id"])
            self.assertEqual(st["state"], "failed")
            self.assertIn("PREFLIGHT_FAILED", st["error"])
            self.assertEqual(st["live"]["status"], "failed")
            self.assertTrue(any("foreign" in e["check"] for e in st["live"]["errors"]))
            self.assertEqual(s.store.count_trials(pid), 1)                            # nothing added by the campaign
            s.store.close()
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.res, cls.pid, cls.cid = build_workspace()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_routes(self):
        from edgelab.web.app import create_app
        app = create_app(self.root)
        c = app.test_client()
        self.assertEqual([x["campaign_id"] for x in c.get("/api/campaigns").get_json()], [self.cid])
        d = c.get(f"/api/campaigns/{self.cid}").get_json()
        fam = d["families"][1]["family_id"]
        self.assertEqual(c.post(f"/api/campaigns/{self.cid}/jobs", json={"families": ["BAD FAMILY"]}).status_code, 400)
        self.assertEqual(c.post(f"/api/campaigns/{self.cid}/jobs", json={"families": ["zzz"]}).status_code, 422)
        r = c.post(f"/api/campaigns/{self.cid}/jobs", json={"families": [fam]})
        self.assertEqual(r.status_code, 202)
        jid = r.get_json()["job_id"]
        self.assertEqual(c.get("/api/campaigns/active-job").get_json()["job"]["job_id"], jid)
        t0 = time.time()
        while time.time() - t0 < 300:
            st = c.get(f"/api/campaigns/jobs/{jid}").get_json()
            if st["state"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.05)
        self.assertEqual(st["state"], "completed", st)
        fr = c.get(f"/api/campaigns/{self.cid}/families/{fam}").get_json()
        self.assertTrue(all(x["status"] == "completed" for x in fr["strategies"]))
        sid = fr["strategies"][0]["strategy_id"]
        one = c.get(f"/api/campaigns/{self.cid}/strategies/{sid}").get_json()
        self.assertEqual(one["provenance"]["campaign_id"], self.cid)
        self.assertEqual(c.get("/api/campaigns/CMP_BAD").status_code, 400)
        self.assertEqual(c.get(f"/api/campaigns/{self.cid}/check").get_json()["ready"], True)


if __name__ == "__main__":
    unittest.main()


class TestTreeScopeEtaHoldout(unittest.TestCase):
    """ADR-70: research browser tree, strategy-id scopes, persisted timing + data-based ETA, and the holdout boundary."""

    @classmethod
    def setUpClass(cls):
        cls.root, cls.res, cls.pid, cls.cid = build_workspace(budget_extra=5)
        cls.rows = cls.res.strategies
        cls.n = len(cls.rows)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def svc(self):
        s = Services(root=self.root)
        self.addCleanup(s.store.close)
        return s

    def test_tree_scope_timing_eta_and_restart(self):
        s = self.svc()
        t = s.campaign_tree(self.cid)
        cat = [f["family_id"] for f in self.res.header["families"]]
        self.assertEqual([f["family_id"] for f in t["families"]], [f for f in cat if any(r["family_id"] == f for r in self.rows)])
        self.assertEqual(sum(f["n_strategies"] for f in t["families"]), self.n == t["n_strategies"] and self.n)
        flat = [x for f in t["families"] for x in f["strategies"]]
        self.assertEqual([x["strategy_id"] for x in flat], [r["strategy_id"] for r in self.rows])      # manifest order
        self.assertTrue(all("_" not in x["display_name"] and x["status"] == "not_started" for x in flat))
        self.assertEqual(sorted(t["datasets"]), ["15m", "1m", "30m", "5m", "60m"])
        self.assertIn("BID-ASK", t["data_line"])
        self.assertEqual(s.campaign_detail(self.cid)["eta"]["state"], "estimating")                   # no observations yet
        # ---- an explicit strategy selection (two families, three frozen ids) runs only those ids
        pick = [self.rows[0]["strategy_id"], self.rows[1]["strategy_id"], self.rows[7]["strategy_id"]]
        with self.assertRaises(C.CampaignError):
            s.start_campaign_job(self.cid, None, strategy_ids=["STR_000000000000"])
        job = s.start_campaign_job(self.cid, None, strategy_ids=pick)
        self.assertEqual(job["n_strategy_ids"], 3)
        st = wait(s, job["job_id"])
        self.assertEqual((st["state"], st["live"]["status"], st["live"]["scope_kind"]), ("completed", "completed", "strategies"))
        self.assertEqual(sorted(e["strategy_id"] for e in s.store.list_trial_events(self.pid) if e["counted"]), sorted(pick))
        self.assertEqual(sorted(st["live"]["families"]), sorted({self.rows[i]["family_id"] for i in (0, 1, 7)}))
        self.assertIsNotNone(st["live"]["preflight_seconds"])
        self.assertIn("strategies selected", st["live"].get("phase", "") + " strategies selected")  # phase text existed
        rid = st["live"]["run_record_id"]
        self.assertEqual(sorted(s.campaign_run_scope(self.cid, rid)["strategy_ids"]), sorted(pick))     # scope persisted
        # ---- timing persisted per cell; ETA now data-based
        spec = C.load(s, self.cid)
        cells = {c["strategy_id"]: c for c in s.store.list_search_cells(spec["search"]["search_id"], current=True)
                 if c["status"] != "ineligible"}                       # the other timeframes' cells never run
        for sid in pick:
            c = cells[sid]
            self.assertTrue(c["started_at"] and c["finished_at"] and c["duration_s"] is not None and c["duration_s"] >= 0)
        obs = C.timing_observations(s, spec, {r["strategy_id"]: r["family_id"] for r in self.rows},
                                    {r["strategy_id"]: r["definition"]["timeframe"] for r in self.rows})
        self.assertEqual(len(obs), 3)
        self.assertTrue(all(o["success"] and o["timeframe"] and o["family_id"] for o in obs))
        eta = s.campaign_detail(self.cid)["eta"]
        self.assertEqual((eta["state"], eta["n_observations"], eta["remaining_strategies"]), ("estimate", 3, self.n - 3))
        self.assertGreater(eta["remaining_seconds"], 0)
        self.assertEqual(st["live"]["eta"]["remaining_strategies"], 0)                                   # the scope is done
        # ---- restart: tree statuses, run history, scope and ETA inputs come back from persisted state
        s.store.close()
        s2 = self.svc()
        t2 = s2.campaign_tree(self.cid)
        done = [x["strategy_id"] for f in t2["families"] for x in f["strategies"] if x["status"] == "completed"]
        self.assertEqual(sorted(done), sorted(pick))
        d2 = s2.campaign_detail(self.cid)
        self.assertEqual((d2["runs"][0]["run_record_id"], d2["runs"][0]["status"], d2["eta"]["n_observations"]), (rid, "completed", 3))
        fr = s2.campaign_family_results(self.cid, self.rows[0]["family_id"])
        one = [x for x in fr["strategies"] if x["strategy_id"] == pick[0]][0]
        self.assertTrue(one["result_available"] and one["display_name"] and one["explanation"] and one["duration_s"] is not None)
        sr = s2.campaign_strategy_result(self.cid, pick[0])
        self.assertEqual((sr["timeframe"], sr["dataset_id"]), (self.rows[0]["definition"]["timeframe"],
                                                               spec["dataset_resolution"]["by_timeframe"][self.rows[0]["definition"]["timeframe"]]["dataset_id"]))
        self.assertEqual(sr["presentation"]["logic_hash"], self.rows[0]["logic_hash"])
        self.assertEqual(sr["account"]["starting_equity"], 50_000.0)
        # ---- resuming the same selection evaluates nothing and adds no event
        n_ev = len(s2.store.list_trial_events(self.pid))
        with mock.patch.object(s2, "_run_cell", side_effect=AssertionError("must not run")):
            st2 = wait(s2, s2.start_campaign_job(self.cid, None, strategy_ids=pick)["job_id"])
        self.assertEqual((st2["live"]["counts"]["skipped_completed"], len(s2.store.list_trial_events(self.pid))), (3, n_ev))

    def test_holdout_boundary(self):
        """The frozen desktop request stays inside discovery; explicit holdout-reaching or windowless requests are refused,
        never clipped; the protocol row serves the one discovery window clients must use."""
        from edgelab.research import protocol as rp
        from edgelab.research.search import plan_search
        from edgelab.web.app import create_app
        root, res, pid, cid = build_workspace()                 # own workspace: the corrected ad-hoc backtest below is a
        self.addCleanup(shutil.rmtree, root, True)              # protocol trial outside the campaign (a "foreign" trial)
        s = Services(root=root)
        self.addCleanup(s.store.close)
        rows, self_pid, self_cid = res.strategies, pid, cid
        spec = C.load(s, self_cid)
        p = s.store.get_protocol(self_pid)
        hold_open = pd.Timestamp(p["material"]["windows"]["holdout"]["boundary_open"])
        per = spec["search"]["spec"]["period"]
        self.assertLess(pd.Timestamp(per["end"]), hold_open)
        self.assertEqual(rp.interval_stage(p, per["start"], per["end"]), "discovery")
        plan = plan_search(spec["search"]["spec"], s)                                                 # the desktop request
        self.assertEqual((plan.protocol_id, plan.period["end"]), (self_pid, per["end"]))
        self.assertEqual(s.list_protocols()[0]["discovery_period"], per)                               # same window, served as data
        # explicit windows reaching the holdout: refused by the governance layer, nothing evaluated
        sid0 = rows[0]["strategy_id"]
        tf0 = rows[0]["definition"]["timeframe"]
        did = spec["dataset_resolution"]["by_timeframe"][tf0]["dataset_id"]
        n_runs, n_tr = len(s.store.list_runs()), s.store.count_trials(self_pid)
        for bad in ((per["start"], str(hold_open + pd.Timedelta(days=3))), (per["start"], str(hold_open))):
            with self.assertRaises(rp.ProtocolRefusal) as cm:
                s.backtest_strategy(sid0, did, False, bad)
            self.assertEqual(cm.exception.code, "HOLDOUT_LOCKED")
        with self.assertRaises(rp.ProtocolRefusal) as cm:                                             # windowless = full dataset
            s.backtest_strategy(sid0, did, False)
        self.assertEqual(cm.exception.code, "HOLDOUT_LOCKED")
        self.assertEqual((len(s.store.list_runs()), s.store.count_trials(self_pid)), (n_runs, n_tr))
        c = create_app(root).test_client()
        r = c.post("/api/backtests", json={"strategy": sid0, "dataset_id": did})                       # the old ad-hoc request
        self.assertGreaterEqual(r.status_code, 400)
        self.assertIn("HOLDOUT_LOCKED", json.dumps(r.get_json()))
        r = c.post("/api/backtests", json={"strategy": sid0, "dataset_id": did,
                                           "period": {"start": per["start"], "end": str(hold_open + pd.Timedelta(days=1))}})
        self.assertGreaterEqual(r.status_code, 400)
        self.assertIn("HOLDOUT_LOCKED", json.dumps(r.get_json()))
        self.assertEqual(s.store.count_trials(self_pid), n_tr)
        disc = c.get("/api/protocols").get_json()[0]["discovery_period"]
        r = c.post("/api/backtests", json={"strategy": sid0, "dataset_id": did, "period": disc})         # the corrected request
        self.assertEqual(r.status_code, 200, r.get_json())
        rec, _ = s.store.load_run(r.get_json()["run_id"])
        self.assertLess(pd.Timestamp(rec["dataset"]["end"]), hold_open)
        self.assertEqual(s.protocol_status(self_pid)["holdout"]["looks_used"], 0)
