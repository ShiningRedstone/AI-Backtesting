"""ADR-77: research runs on several CPU cores give exactly the results of one core.

Two identical synthetic workspaces (Dukascopy-shaped fixture, small real factory manifest) run the same family scope of
their frozen campaign, one with processes=1 and one with processes=2: same cells, same trades hashes and metrics, same
prop audits, the same run order and the same protocol trial ledger (order, counted, status). The frozen spec, the search
id and the preflight are untouched by the setting (it is never part of an identity)."""
import json
import os
import shutil
import unittest

from edgelab.services import Services
from tests.test_desktop_research_runs import build_workspace, wait


@unittest.skipIf((os.cpu_count() or 1) < 2, "needs at least 2 CPU cores")
class TestParallelResearchRuns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = build_workspace()
        cls.b = build_workspace()

    @classmethod
    def tearDownClass(cls):
        for ws in (cls.a, cls.b):
            shutil.rmtree(ws[0], ignore_errors=True)

    def run_scope(self, ws, processes):
        root, res, pid, cid = ws
        s = Services(root=root)
        self.addCleanup(s.store.close)
        d = s.campaign_detail(cid)
        fams = [f["family_id"] for f in d["families"][:8]]
        job = s.start_campaign_job(cid, fams, processes=processes)
        self.assertEqual(job["processes"], processes)
        st = wait(s, job["job_id"])
        self.assertEqual(st["state"], "completed", st)
        self.assertEqual(st["live"]["processes"], processes)
        sid = d["search_id"]
        cells = {c["strategy_id"]: c for c in s.store.list_search_cells(sid) if c["status"] == "completed"}
        runs = s.store._query("SELECT run_id, strategy_id, trades_hash, record_json FROM runs ORDER BY run_id")
        props = {r[1]: json.loads(r[3]).get("prop") for r in runs}
        ledger = [(e["strategy_id"], e["counted"], e["status"]) for e in s.store.list_trial_events(pid)]
        durations = [c["duration_s"] for c in cells.values()]
        return {"search_id": sid, "spec": s.campaign_detail(cid)["execution"], "fams": fams,
                "cells": {k: (c["trades_hash"], c["headline_json"]) for k, c in cells.items()},
                "run_order": [r[1] for r in runs], "trades": {r[1]: r[2] for r in runs}, "props": props,
                "ledger": ledger, "durations": durations, "batch": s.store.get_search_batch(sid)["status"]}

    def test_two_cores_equal_one_core(self):
        one = self.run_scope(self.a, 1)
        two = self.run_scope(self.b, 2)
        self.assertTrue(one["cells"])
        self.assertEqual(one["fams"], two["fams"])
        self.assertEqual(one["cells"], two["cells"])                      # trades hash + headline metrics per cell
        self.assertEqual(one["trades"], two["trades"])
        self.assertEqual(one["props"], two["props"])                      # prop audit computed in the worker = parent
        self.assertEqual(one["run_order"], two["run_order"])              # written in plan order
        self.assertEqual(one["ledger"], two["ledger"])
        self.assertEqual((one["batch"], two["batch"]), ("partial", "partial"))
        self.assertEqual(one["spec"]["workers"], 1)                       # the frozen spec is untouched
        self.assertEqual(two["spec"]["workers"], 1)
        self.assertTrue(all(isinstance(x, float) for x in two["durations"]))   # per-cell timing kept in parallel

    def test_cancel_then_resume_on_two_cores_counts_every_trial_once(self):
        import time
        root, res, pid, cid = build_workspace()
        self.addCleanup(shutil.rmtree, root, True)
        s = Services(root=root)
        self.addCleanup(s.store.close)
        job = s.start_campaign_job(cid, None, processes=2)
        while True:                                                      # cancel once some cells are recorded
            st = s.campaign_job(job["job_id"])
            if st["state"] in ("completed", "failed", "cancelled") or \
                    (st["live"].get("counts") or {}).get("completed_this_run", 0) >= 2:
                break
            time.sleep(0.02)
        s.cancel_job(job["job_id"])
        st = wait(s, job["job_id"])
        self.assertIn(st["state"], ("cancelled", "completed"))
        mid = [e for e in s.store.list_trial_events(pid) if e["counted"]]
        self.assertEqual(len(mid), len({e["strategy_id"] for e in mid}))   # no duplicates after the stop
        st2 = wait(s, s.start_campaign_job(cid, None, processes=2)["job_id"])
        self.assertEqual((st2["state"], st2["live"]["status"]), ("completed", "completed"))
        counted = [e for e in s.store.list_trial_events(pid) if e["counted"]]
        self.assertEqual(len(counted), len(res.strategies))
        self.assertEqual(len({e["strategy_id"] for e in counted}), len(res.strategies))
        self.assertEqual(sum(1 for e in s.store.list_trial_events(pid) if e["status"] == "completed" and not e["counted"]), 0)

    def test_processes_are_validated(self):
        from edgelab.research import campaign as C
        root, res, pid, cid = self.a
        s = Services(root=root)
        self.addCleanup(s.store.close)
        for bad in (0, -1, C.max_processes() + 1, True, "2"):
            with self.assertRaises(ValueError):
                s.start_campaign_job(cid, None, processes=bad)
        info = s.research_processes()
        self.assertEqual((info["default"], info["max"]), (max(1, C.max_processes() - 1), C.max_processes()))
        s.set_ui_preferences({"research_processes": 1})
        self.assertEqual(s.research_processes()["processes"], 1)
        with self.assertRaises(ValueError):
            s.set_ui_preferences({"research_processes": 0})
        s.set_ui_preferences({"research_processes": None})
        self.assertEqual(s.research_processes()["processes"], info["default"])


if __name__ == "__main__":
    unittest.main()
