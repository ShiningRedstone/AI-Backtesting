"""ADR-78: pages load while a research run holds the service lock.

GET requests read through pooled READ-ONLY SQLite connections without the service lock: with the lock held by another
thread (as a research run does around its store steps) the main pages still answer, with exactly the JSON the locked
path returns; a GET that would write falls back to the lock; the view cache key is the same in every thread; the read
connections cannot write and are closed with the store."""
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
PAGES = ["/api/overview", "/api/explorer/strategies?tested_only=1", "/api/results", "/api/strategies",
         "/api/favorites", "/api/results-view/overview", "/api/research/dashboard?include_synthetic=1",
         "/api/datasets", "/api/protocols", "/api/campaigns", "/api/status", "/api/preferences/ui"]


class TestConcurrentReads(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.web.app import create_app
        from edgelab.web.demo import create_demo_workspace
        cls.tmp = Path(tempfile.mkdtemp())
        create_demo_workspace(cls.tmp / "demo", REPO)
        cls.app = create_app(cls.tmp / "demo", demo=True)
        cls.svc = cls.app.config["EDGELAB"]["services"]
        cls.c = cls.app.test_client()
        sid = cls.svc.library.list()[0]["strategy_id"]
        did = next(d["dataset_id"] for d in cls.svc.backtest_readiness(sid)["datasets"] if d["runnable"])
        cls.svc.backtest_strategy(sid, did, record=True)                     # one stored run to show

    @classmethod
    def tearDownClass(cls):
        cls.svc.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get_with_lock_held(self, url, timeout=20):
        """GET `url` while another thread holds the service lock (like a research run's store step)."""
        held, release = threading.Event(), threading.Event()

        def hold():
            with self.svc.lock:
                held.set()
                release.wait(60)
        t = threading.Thread(target=hold, daemon=True)
        t.start()
        self.assertTrue(held.wait(5))
        out = {}

        def req():
            out["r"] = self.app.test_client().get(url)
        r = threading.Thread(target=req, daemon=True)
        t0 = time.monotonic()
        r.start()
        r.join(timeout)
        elapsed = time.monotonic() - t0
        release.set()
        t.join(5)
        self.assertFalse(r.is_alive(), f"{url} waited for the service lock")
        return out["r"], elapsed

    def test_pages_answer_while_the_lock_is_held_and_match_the_locked_answer(self):
        for url in PAGES:
            free, _ = self.get_with_lock_held(url)
            self.assertEqual(free.status_code, 200, (url, free.get_data(as_text=True)[:300]))
            with mock.patch.object(type(self.svc), "read_context", _no_reader):   # the old, locked path
                locked = self.c.get(url)
            self.assertEqual(locked.status_code, 200, url)
            a, b = free.get_json(), locked.get_json()
            if url == "/api/status":                                              # holds wall-clock fields
                keep = sorted(k for k in a if isinstance(a[k], (list, int)) and not isinstance(a[k], bool))
                a, b = {k: a[k] for k in keep}, {k: b.get(k) for k in keep}
            self.assertEqual(a, b, url)

    def test_read_connections_cannot_write_and_a_writing_get_falls_back(self):
        writer = self.svc.writer_store
        reader = writer.acquire_reader()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                reader._exec("CREATE TABLE nope (x INTEGER)")
        finally:
            writer.release_reader(reader)
        calls = []
        real = self.svc.ui_preferences

        def writes_once():
            calls.append(self.svc.store is self.svc.writer_store)
            if not calls[-1]:
                self.svc.store._exec("CREATE TABLE IF NOT EXISTS t_probe (x INTEGER)")
            return real()
        with mock.patch.object(self.svc, "ui_preferences", writes_once):
            r = self.c.get("/api/preferences/ui")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls, [False, True])                          # read-only attempt, then under the lock

    def test_view_cache_key_is_the_same_in_every_thread_and_sees_writes(self):
        from edgelab.research.campaign import db_token
        w = db_token(self.svc)
        with self.svc.read_context(page=False) as ok:
            self.assertTrue(ok)
            self.assertIsNot(self.svc.store, self.svc.writer_store)
            r = db_token(self.svc)
        self.assertEqual(w, r)
        sid = self.svc.library.list()[0]["strategy_id"]
        did = next(d["dataset_id"] for d in self.svc.backtest_readiness(sid)["datasets"] if d["runnable"])
        self.svc.backtest_strategy(sid, did, record=True)
        self.assertNotEqual(db_token(self.svc), w)

    def test_page_views_refresh_at_most_every_few_seconds_during_a_research_job(self):
        from edgelab.research import campaign as C
        with mock.patch.object(C, "_research_running", lambda svc: True):
            with self.svc.read_context() as ok:
                self.assertTrue(ok)
                t1 = C.db_token(self.svc)
            self.svc.writer_store._exec("CREATE TABLE IF NOT EXISTS t_touch (x INTEGER)")
            with self.svc.read_context() as ok:
                self.assertEqual(C.db_token(self.svc), t1)               # within the refresh interval
            with self.svc.read_context(page=False) as ok:
                self.assertNotEqual(C.db_token(self.svc), t1)            # research code: always exact
        with self.svc.read_context() as ok:
            self.assertNotEqual(C.db_token(self.svc), t1)                # no job running: exact

    def test_read_connections_close_with_the_store(self):
        from edgelab.data.store import SQLiteStore
        st = SQLiteStore(self.tmp / "x.sqlite")
        r = st.acquire_reader()
        st.release_reader(r)
        st.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            r.con.execute("SELECT 1")


from contextlib import contextmanager  # noqa: E402


@contextmanager
def _no_reader(self, page=True):
    yield False


if __name__ == "__main__":
    unittest.main()
