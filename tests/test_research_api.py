"""Phase 4 step 8: the /api/research HTTP routes (Flask test client) over the research services.

Routes are thin: they parse JSON, call Services and map errors (422 invalid search spec or
ranking request, 409 job conflict, 404 unknown job/search, 400 malformed input). A background job
is held mid-cell by a gate so status and cancel are exercised while it is running."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from edgelab.web.app import create_app
from tests.test_batch_runner import build_workspace
from tests.test_jobs import T, Gate, in_thread


def strict(obj):
    return json.dumps(obj, allow_nan=False)


class TestResearchApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        _, cls.ids = build_workspace(cls.root)
        cls.app = create_app(cls.root)
        cls.c = cls.app.test_client()
        cls.svc = cls.app.config["EDGELAB"]["services"]
        cls.seed = 0

    @classmethod
    def tearDownClass(cls):
        cls.svc.jobs.join(T)
        shutil.rmtree(cls.root, ignore_errors=True)

    def call(self, method, url, body=None, status=200):
        r = self.c.open(url, method=method, json=body)
        self.assertEqual(r.status_code, status, r.get_data(as_text=True)[:600])
        strict(r.get_json())
        return r.get_json()

    def spec(self, **kw):
        type(self).seed += 1
        i = self.ids
        return {"strategies": {"ids": [i["ema"], i["ema3"], i["never"], i["ema15"]]},
                "datasets": [i["fut"], i["cfd"]], "seed": self.seed, **kw}

    def run_job(self, spec):
        with Gate(self.svc) as g:
            g.permits.release(10)
            job = self.call("POST", "/api/research/jobs", {"spec": spec}, 202)
            self.assertTrue(self.svc.jobs.join(T))
        return self.call("GET", f"/api/research/jobs/{job['job_id']}")

    # ------------------------------------------------------------------ validate / plan
    def test_validate(self):
        ok = self.call("POST", "/api/research/validate", {"spec": self.spec()})
        self.assertTrue(ok["valid"])
        self.assertEqual(len(ok["search_hash"]), 64)
        bad = self.call("POST", "/api/research/validate",
                        {"spec": {**self.spec(), "mystery": 1, "ranking": {"metric": "win_rate"}}})
        self.assertFalse(bad["valid"])
        self.assertEqual({e["path"] for e in bad["errors"]}, {"mystery", "ranking.metric"})
        for body in ({"spec": "strategies: {}"}, {"spec": ["x"]}, {}):   # data-only JSON objects
            self.assertEqual(self.call("POST", "/api/research/validate", body, 400)["error"]["kind"], "bad_request")

    def test_plan(self):
        p = self.call("POST", "/api/research/plan", {"spec": self.spec()})
        self.assertTrue(p["search_id"].startswith("SRCH_"))
        self.assertEqual((p["counts"]["planned"], p["counts"]["eligible"], p["counts"]["ineligible"]), (8, 3, 5))
        self.assertEqual([c["plan_index"] for c in p["cells"]], list(range(8)))
        self.assertIn("nothing has been executed", p["note"])

    def test_invalid_search_spec_is_422(self):
        n_jobs = len(self.svc.jobs.list())
        for spec in ({**self.spec(), "mystery": 1},
                     {"strategies": {"ids": ["STR_000000000000"]}, "datasets": [self.ids["fut"]]},
                     {**self.spec(), "max_cells": 1}):
            with self.subTest(spec=spec):
                for url in ("/api/research/plan", "/api/research/jobs"):
                    err = self.call("POST", url, {"spec": spec}, 422)["error"]
                    self.assertEqual(err["kind"], "search_spec")
                    self.assertTrue(err["issues"])
        self.assertEqual(len(self.svc.jobs.list()), n_jobs)             # refused before any job exists

    # ------------------------------------------------------------------ jobs
    def test_job_lifecycle_status_cancel_and_conflict_while_running(self):
        spec = self.spec()
        with Gate(self.svc) as g:
            job = self.call("POST", "/api/research/jobs", {"spec": spec}, 202)
            self.assertEqual(job["state"], "queued")
            jid = job["job_id"]
            g.entered.get(timeout=T)                                   # a cell is executing now
            st = in_thread(self.call, "GET", f"/api/research/jobs/{jid}")      # responsive mid-cell
            self.assertEqual((st["state"], st["progress"]["eligible"], st["progress"]["evaluated"]), ("running", 3, 0))
            err = in_thread(self.call, "POST", "/api/research/jobs", {"spec": self.spec()}, 409)["error"]
            self.assertEqual(err["kind"], "job_conflict")
            self.assertIn(jid, err["reason"])
            c = in_thread(self.call, "POST", f"/api/research/jobs/{jid}/cancel")
            self.assertEqual((c["state"], c["cancel_requested"]), ("running", True))
            g.permits.release()                                        # the running cell finishes
            self.assertTrue(self.svc.jobs.join(T))
            self.assertEqual(g.calls, 1)
            self.assertEqual(g.lock_free_at_entry, [True])             # lock not held into the cell
        final = self.call("GET", f"/api/research/jobs/{jid}")
        self.assertEqual(final["history"], ["queued", "running", "cancelled"])
        self.assertEqual((final["progress"]["cancelled"], final["progress"]["evaluated"]), (2, 1))
        done = self.run_job(self.spec())                               # free again: a new job completes
        self.assertEqual(done["state"], "completed")

    def test_unknown_or_malformed_job_ids(self):
        self.assertEqual(self.call("GET", "/api/research/jobs/JOB_000000000000", status=404)["error"]["kind"],
                         "not_found")
        self.call("POST", "/api/research/jobs/JOB_000000000000/cancel", status=404)
        self.call("GET", "/api/research/jobs/not-a-job", status=400)

    # ------------------------------------------------------------------ searches / ranking / shortlist
    def test_search_listing_detail_ranking_and_shortlist(self):
        i = self.ids
        done = self.run_job(self.spec())
        sid = done["search_id"]
        rows = self.call("GET", "/api/research/searches")
        row = next(r for r in rows if r["search_id"] == sid)
        self.assertEqual((row["status"], row["n_evaluated"], row["n_trials"]), ("completed", 3, 3))
        self.assertIsInstance(row["spec"], dict)
        self.assertNotIn("spec_json", row)
        d = self.call("GET", f"/api/research/searches/{sid}")
        self.assertEqual(len(d["cells"]), 8)
        self.assertEqual(d["historical_cells"], [])
        self.assertEqual(d["cumulative"]["trials"], 3)

        r = self.call("GET", f"/api/research/searches/{sid}/ranking")
        self.assertEqual((r["metric"], r["min_sample_label"], r["validated"], r["in_sample"]),
                         ("expectancy_r", "MODERATE SAMPLE", False, True))
        self.assertEqual(r["n_trials"], 3)
        low = self.call("GET", f"/api/research/searches/{sid}/ranking?metric=net_r&min_sample_label=LOW%20SAMPLE%20SIZE")
        self.assertEqual({x["strategy_id"] for x in low["ranked"]}, {i["ema"], i["ema3"]})
        vals = [x["value"] for x in low["ranked"]]
        self.assertEqual(vals, sorted(vals, reverse=True))
        err = self.call("GET", f"/api/research/searches/{sid}/ranking?metric=win_rate", status=422)["error"]
        self.assertEqual(err["kind"], "ranking")
        self.assertIn("never a ranking metric", err["reason"])

        runs = self.call("GET", "/api/results")
        sl = self.call("POST", f"/api/research/searches/{sid}/shortlist",
                       {"strategy_ids": [i["ema3"], i["ema"], i["ema3"]]})
        self.assertEqual((sl["strategy_ids"], sl["duplicates_removed"], sl["validated"]), ([i["ema3"], i["ema"]], 1, False))
        self.assertEqual(self.call("GET", f"/api/research/searches/{sid}")["shortlist"]["strategy_ids"], [i["ema3"], i["ema"]])
        self.assertEqual([(x["run_id"], x["status"]) for x in self.call("GET", "/api/results")],
                         [(x["run_id"], x["status"]) for x in runs])             # no run status changed
        self.assertEqual(self.call("POST", f"/api/research/searches/{sid}/shortlist",
                                   {"strategy_ids": ["STR_000000000000"]}, 422)["error"]["kind"], "ranking")
        self.call("POST", f"/api/research/searches/{sid}/shortlist", {"strategy_ids": i["ema"]}, 400)

    def test_unknown_or_malformed_search_ids(self):
        for url in ("/api/research/searches/SRCH_000000000000", "/api/research/searches/SRCH_000000000000/ranking"):
            self.assertEqual(self.call("GET", url, status=404)["error"]["kind"], "not_found")
        self.call("POST", "/api/research/searches/SRCH_000000000000/shortlist", {"strategy_ids": []}, 404)
        self.call("GET", "/api/research/searches/bad-id", status=400)
        self.call("GET", "/api/research/nope", status=404)

    def test_restart_reconciliation_runs_at_app_start(self):
        done = self.run_job(self.spec())
        self.svc.store.update_search_batch(done["search_id"], status="running")   # as if the process died
        app2 = create_app(self.root)                                   # a new web process
        try:
            row = next(r for r in app2.test_client().get("/api/research/searches").get_json()
                       if r["search_id"] == done["search_id"])
            self.assertEqual(row["status"], "interrupted")
        finally:
            app2.config["EDGELAB"]["services"].store.close()


if __name__ == "__main__":
    unittest.main()
