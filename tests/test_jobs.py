"""Phase 4 step 6: background search jobs (one worker thread, one active job), progress,
cooperative cancellation between cells, failure handling and restart reconciliation.

Cells are held at a gate (events/queues, no sleeps) so each test controls exactly when a cell
starts and finishes. The service lock must be free while a cell runs."""
import queue
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from edgelab.research.jobs import JobConflict, JobManager
from tests.test_batch_runner import build_workspace

T = 30          # generous timeout (seconds) for every wait; tests never sleep


class Gate:
    """Replaces services._run_cell: each call announces itself and waits for a permit."""

    def __init__(self, svc, fail_on: int | None = None, exc: BaseException | None = None):
        self.svc, self.real = svc, svc._run_cell
        self.entered: queue.Queue = queue.Queue()
        self.permits = threading.Semaphore(0)
        self.calls, self.fail_on, self.exc = 0, fail_on, exc
        self.lock_free_at_entry: list[bool] = []

    def __call__(self, *a, **kw):
        self.calls += 1
        n = self.calls
        self.lock_free_at_entry.append(lock_is_free(self.svc.lock))
        self.entered.put(n)
        assert self.permits.acquire(timeout=T), "gate never opened"
        if self.fail_on == n:
            raise self.exc
        return self.real(*a, **kw)

    def __enter__(self):
        self.svc._run_cell = self
        return self

    def __exit__(self, *exc):
        del self.svc._run_cell
        self.permits.release(100)


def lock_is_free(lock) -> bool:
    """True if ANOTHER thread can take the lock right now (an RLock owner could re-enter)."""
    got = []

    def probe():                         # an RLock must be released by the thread that took it
        ok = lock.acquire(timeout=2)
        if ok:
            lock.release()
        got.append(ok)
    t = threading.Thread(target=probe)
    t.start()
    t.join()
    return got[0]


def in_thread(fn, *a):
    """Run fn in another thread and fail (instead of hanging) if it blocks."""
    out = []
    t = threading.Thread(target=lambda: out.append(fn(*a)), daemon=True)
    t.start()
    t.join(T)
    assert out, f"{fn.__name__} blocked"
    return out[0]


class TestJobs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        cls.svc, cls.ids = build_workspace(cls.root)
        cls.seed = 0

    @classmethod
    def tearDownClass(cls):
        cls.svc.jobs.join(T)
        shutil.rmtree(cls.root, ignore_errors=True)

    def fresh_spec(self):
        """A spec with its own search id (seed is part of the identity): 3 eligible cells."""
        type(self).seed += 1
        i = self.ids
        return {"strategies": {"ids": [i["ema"], i["ema3"], i["never"], i["ema15"]]},
                "datasets": [i["fut"], i["cfd"]], "seed": self.seed}

    def finish(self, job_id):
        self.assertTrue(self.svc.jobs.join(T))
        return self.svc.job_status(job_id)

    # ------------------------------------------------------------------ lifecycle / conflict
    def test_lifecycle_queued_running_completed(self):
        spec = self.fresh_spec()
        with Gate(self.svc) as g:
            started = self.svc.start_search_job(spec)
            self.assertEqual(started["state"], "queued")
            g.entered.get(timeout=T)
            self.assertEqual(self.svc.job_status(started["job_id"])["state"], "running")
            g.permits.release(3)
            st = self.finish(started["job_id"])
        self.assertEqual(st["history"], ["queued", "running", "completed"])
        self.assertIsNone(st["error"])
        self.assertEqual(st["progress"]["batch_status"], "completed")
        self.assertEqual((st["progress"]["evaluated"], st["progress"]["fraction_done"]), (3, 1.0))
        self.assertEqual(self.svc.store.get_search_batch(st["search_id"])["status"], "completed")

    def test_only_one_active_job(self):
        spec, other = self.fresh_spec(), self.fresh_spec()
        with Gate(self.svc) as g:
            first = self.svc.start_search_job(spec)
            g.entered.get(timeout=T)
            for s in (other, spec):                                          # any spec, deterministically
                with self.assertRaises(JobConflict) as cm:
                    self.svc.start_search_job(s)
                self.assertIn(first["job_id"], str(cm.exception))
            g.permits.release(3)
            self.finish(first["job_id"])
        with Gate(self.svc) as g:                                            # free again once finished
            g.permits.release(3)
            second = self.svc.start_search_job(other)
            self.assertEqual(self.finish(second["job_id"])["state"], "completed")

    def test_invalid_spec_is_refused_before_any_job_starts(self):
        from edgelab.research.search import SearchSpecError
        n = len(self.svc.jobs.list())
        for bad in ({"strategies": {"ids": ["STR_NOPE"]}, "datasets": [self.ids["fut"]]},
                    {**self.fresh_spec(), "workers": 2}, {**self.fresh_spec(), "mystery": 1}):
            with self.assertRaises(SearchSpecError):
                self.svc.start_search_job(bad)
        self.assertEqual(len(self.svc.jobs.list()), n)

    # ------------------------------------------------------------------ progress / responsiveness / lock
    def test_progress_and_status_while_running_and_lock_is_free(self):
        spec = self.fresh_spec()
        with Gate(self.svc) as g:
            job = self.svc.start_search_job(spec)["job_id"]
            g.entered.get(timeout=T)
            p0 = in_thread(self.svc.job_status, job)["progress"]              # readable mid-cell
            self.assertEqual((p0["eligible"], p0["evaluated"], p0["pending"], p0["ineligible"]), (3, 0, 3, 5))
            g.permits.release()
            g.entered.get(timeout=T)                                          # cell 1 stored, cell 2 started
            p1 = in_thread(self.svc.job_status, job)["progress"]
            self.assertEqual((p1["evaluated"], p1["pending"], p1["cell_status"].get("completed")), (1, 2, 1))
            self.assertAlmostEqual(p1["fraction_done"], 1 / 3, places=5)
            g.permits.release(2)
            st = self.finish(job)
        self.assertEqual(st["progress"]["evaluated"], 3)
        self.assertEqual(g.lock_free_at_entry, [True, True, True])            # lock released before each cell

    def test_lock_is_not_held_during_the_backtest(self):
        import edgelab.engine.backtester as bt
        real, seen = bt.run_backtest, []

        def spy(*a, **kw):
            seen.append(lock_is_free(self.svc.lock))
            return real(*a, **kw)
        with mock.patch.object(bt, "run_backtest", spy):
            job = self.svc.start_search_job(self.fresh_spec())["job_id"]
            st = self.finish(job)
        self.assertEqual(st["state"], "completed")
        self.assertEqual(seen, [True, True, True])

    # ------------------------------------------------------------------ cancellation
    def test_cooperative_cancellation_between_cells(self):
        spec = self.fresh_spec()
        runs_before = len(self.svc.store.list_runs())
        with Gate(self.svc) as g:
            job = self.svc.start_search_job(spec)["job_id"]
            g.entered.get(timeout=T)
            st = in_thread(self.svc.cancel_job, job)                          # callable mid-cell
            self.assertEqual((st["state"], st["cancel_requested"]), ("running", True))
            g.permits.release()                                               # the running cell finishes
            st = self.finish(job)
            self.assertEqual(g.calls, 1)                                      # no new cell started
        self.assertEqual(st["history"], ["queued", "running", "cancelled"])
        cells = self.svc.store.list_search_cells(st["search_id"], current=True)
        by = {}
        for c in cells:
            by.setdefault(c["status"], []).append(c)
        self.assertEqual({k: len(v) for k, v in by.items()}, {"completed": 1, "cancelled": 2, "ineligible": 5})
        self.assertTrue(self.svc.store.has_run(by["completed"][0]["run_id"]))  # durable
        self.assertEqual(len(self.svc.store.list_runs()), runs_before + 1)
        b = self.svc.store.get_search_batch(st["search_id"])
        self.assertEqual((b["status"], b["n_cancelled"], b["n_evaluated"], b["n_trials"]), ("cancelled", 2, 1, 1))
        again = self.svc.run_search(spec)                                     # explicit resume later
        self.assertEqual((again["status"], again["n_skipped_resume"], again["n_evaluated"]), ("completed", 1, 2))

    def test_cancel_of_finished_or_unknown_job(self):
        with Gate(self.svc) as g:
            g.permits.release(3)
            job = self.svc.start_search_job(self.fresh_spec())["job_id"]
            self.finish(job)
        st = self.svc.cancel_job(job)
        self.assertEqual((st["state"], st["cancel_requested"]), ("completed", False))
        with self.assertRaises(KeyError):
            self.svc.cancel_job("JOB_NOPE")
        with self.assertRaises(KeyError):
            self.svc.job_status("JOB_NOPE")

    # ------------------------------------------------------------------ failures
    def test_worker_exception_is_durable_failed(self):
        class Boom(BaseException):          # escapes run_search's own `except Exception`
            pass
        for exc in (Boom("worker died"), RuntimeError("store exploded")):
            with self.subTest(exc=type(exc).__name__):
                spec = self.fresh_spec()
                with Gate(self.svc, fail_on=2, exc=exc) as g:
                    target = g if isinstance(exc, Boom) else None
                    if target is None:                                        # fail outside the cell path
                        import edgelab.research.batch as batch
                        real_lineage = batch._lineage
                        calls = []

                        def lineage(*a):
                            calls.append(1)
                            if len(calls) == 2:
                                raise exc
                            return real_lineage(*a)
                        patcher = mock.patch.object(batch, "_lineage", lineage)
                        patcher.start()
                    try:
                        g.permits.release(3)
                        job = self.svc.start_search_job(spec)["job_id"]
                        st = self.finish(job)
                    finally:
                        if target is None:
                            patcher.stop()
                self.assertEqual(st["state"], "failed")
                self.assertIn(type(exc).__name__, st["error"])
                b = self.svc.store.get_search_batch(st["search_id"])
                self.assertEqual(b["status"], "failed")                       # never left `running`
                self.assertIsNotNone(b["finished_at"])
                done = [c for c in self.svc.store.list_search_cells(st["search_id"]) if c["status"] == "completed"]
                self.assertEqual(len(done), 1)                                # first cell preserved
                self.assertTrue(self.svc.store.has_run(done[0]["run_id"]))

    # ------------------------------------------------------------------ restart
    def test_restart_marks_running_batches_interrupted_only(self):
        store = self.svc.store
        specs = [self.fresh_spec() for _ in range(4)]
        ids = [self.svc.run_search(s)["search_id"] for s in specs]
        running, cancelled, failed, completed = ids
        store.update_search_batch(running, status="running", finished_at=None)   # a process died mid-run
        store.update_search_batch(cancelled, status="cancelled")
        store.update_search_batch(failed, status="failed")
        cells_before = store.list_search_cells(running)
        jm = JobManager(self.svc, self.svc.lock)                              # a new process starts
        self.assertEqual(jm.interrupted, [running])
        status = {s: store.get_search_batch(s)["status"] for s in ids}
        self.assertEqual(status, {running: "interrupted", cancelled: "cancelled", failed: "failed",
                                  completed: "completed"})
        self.assertEqual(store.list_search_cells(running), cells_before)     # cells untouched, not resumed
        self.assertEqual(JobManager(self.svc, self.svc.lock).interrupted, [])  # idempotent
        again = self.svc.run_search(specs[0])                                 # explicit resume works
        self.assertEqual((again["status"], again["n_skipped_resume"], again["n_evaluated"]), ("completed", 3, 0))

    # ------------------------------------------------------------------ synchronous path unchanged
    def test_synchronous_run_search_is_unchanged(self):
        s = self.svc.run_search(self.fresh_spec())
        self.assertEqual((s["status"], s["n_evaluated"], s["n_trials"], s["n_cancelled"]), ("completed", 3, 3, 0))
        self.assertEqual(s["cumulative"]["completed"], 3)
        self.assertIs(self.svc.lock, self.svc.jobs.lock)                     # one lock, shared


if __name__ == "__main__":
    unittest.main()
