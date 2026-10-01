"""ADR-76: single backtests as background jobs. Same path and same result as `backtest_strategy`; the service lock is
never held while the engine computes (other pages keep answering); bad input is refused."""
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]


def _wait(svc, job_id, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = svc.backtest_job(job_id)
        if j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("backtest job did not finish")


class TestBacktestJobs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.services import Services
        from edgelab.web.demo import create_demo_workspace
        cls.tmp = Path(tempfile.mkdtemp())
        create_demo_workspace(cls.tmp / "demo", REPO)
        cls.svc = Services(root=cls.tmp / "demo")
        cls.sid = cls.svc.library.list()[0]["strategy_id"]
        cls.ds = next(d["dataset_id"] for d in cls.svc.backtest_readiness(cls.sid)["datasets"] if d["runnable"])

    @classmethod
    def tearDownClass(cls):
        cls.svc.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_job_result_equals_the_synchronous_backtest(self):
        sync = self.svc.backtest_strategy(self.sid, self.ds, record=True)
        job = _wait(self.svc, self.svc.start_backtest_job(self.sid, self.ds)["job_id"])
        self.assertEqual(job["state"], "completed")
        res = job["result"]
        self.assertEqual(res["trades_hash"], sync["trades_hash"])
        self.assertEqual(res["metrics"], sync["metrics"])
        self.assertNotEqual(res["run_id"], sync["run_id"])                      # recorded as its own run
        rec, _ = self.svc.store.load_run(res["run_id"])
        self.assertEqual(rec["status"], "IN_SAMPLE")

    def test_lock_is_free_while_the_engine_computes(self):
        from edgelab.engine import backtester
        started, release = threading.Event(), threading.Event()
        real = backtester.run_backtest

        def slow(*a, **kw):
            started.set()
            release.wait(30)
            return real(*a, **kw)
        with mock.patch.object(backtester, "run_backtest", slow):
            jid = self.svc.start_backtest_job(self.sid, self.ds)["job_id"]
            self.assertTrue(started.wait(30))
            got = self.svc.lock.acquire(timeout=2)                           # another page's request would get it
            self.assertTrue(got)
            self.svc.lock.release()
            self.assertEqual(self.svc.backtest_job(jid)["state"], "running")
            release.set()
            self.assertEqual(_wait(self.svc, jid)["state"], "completed")

    def test_failures_are_reported_and_http_routes(self):
        job = _wait(self.svc, self.svc.start_backtest_job(self.sid, "NO_SUCH_DATASET")["job_id"])
        self.assertEqual(job["state"], "failed")
        self.assertTrue(job["error"]["message"])
        from edgelab.web.app import create_app
        c = create_app(self.svc.root, demo=True).test_client()
        try:
            r = c.post("/api/backtests/jobs", json={"strategy": self.sid, "dataset_id": self.ds})
            self.assertEqual(r.status_code, 200)
            jid = r.get_json()["job_id"]
            for _ in range(600):
                st = c.get(f"/api/backtests/jobs/{jid}").get_json()
                if st["state"] != "running":
                    break
                time.sleep(0.05)
            self.assertEqual(st["state"], "completed")
            self.assertEqual(c.get("/api/backtests/jobs/bad").status_code, 400)
            self.assertEqual(c.get("/api/backtests/jobs/BTJ_000000000000").status_code, 404)
            self.assertEqual(c.post("/api/backtests/jobs", json={"strategy": "x", "dataset_id": self.ds}).status_code, 400)
        finally:
            c.application.config["EDGELAB"]["services"].store.close()


if __name__ == "__main__":
    unittest.main()
