"""ADR-91: research runs no longer run out of memory and do less repeated work, with bit-identical results.

* zero-copy array hashing gives the old digests;
* bar fingerprints are remembered only while the arrays are read-only (and recomputed otherwise);
* the feature memory cache is bounded by bytes as well as by count; the derived-input memo (higher-timeframe bars,
  session membership, trading dates) returns exactly the freshly computed values, read-only;
* the vectorized session-open / session-close lookups equal the old per-bar dictionary lookups;
* the memory plan picks the number of worker processes from the free memory; an out-of-memory failure restarts the
  run with half the cores;
* a research run with tiny caches (constant evictions) gives the same cells, trades hashes, metrics and trial ledger
  as with the default caches."""
import hashlib
import json
import os
import pickle
import shutil
import threading
import unittest
from unittest import mock

import numpy as np
import pandas as pd

from edgelab.core.identity import hash_arrays
from edgelab.core.memo import DerivedMemo
from edgelab.data.schema import BarArrays
from edgelab.features.cache import FeatureCache
from edgelab.research import memory as M


def _old_hash(*arrays):
    h = hashlib.sha256()
    for a in arrays:
        a = np.ascontiguousarray(a)
        h.update(str(a.dtype).encode())
        h.update(str(a.shape).encode())
        h.update(a.tobytes())
    return h.hexdigest()


def _bars(n=5000, seed=0, ask=True):
    rng = np.random.default_rng(seed)
    c = 18000 + np.cumsum(rng.normal(0, 2, n))
    start = pd.Timestamp("2024-03-04 00:00", tz="UTC").value
    ts = start + np.arange(n, dtype=np.int64) * 60_000_000_000
    extra = dict(ask_open=c + 1, ask_high=c + 2, ask_low=c, ask_close=c + 1) if ask else {}
    return BarArrays(ts, c, c + 1.5, c - 1.5, c, np.ones(n), 1, **extra)


class TestHashing(unittest.TestCase):
    def test_zero_copy_hash_equals_the_old_digest(self):
        rng = np.random.default_rng(1)
        cases = [rng.normal(size=999), rng.integers(0, 9, size=(30, 7)), np.arange(30, dtype=np.int64)[::3],
                 np.array([], float), np.array(["a", "bb"]), np.array(["x", 1], dtype=object), np.array(3.5),
                 np.zeros(3, dtype="M8[ns]"), np.float32([1, 2]), rng.normal(size=(5, 4)).T, np.array([True, False]),
                 np.frombuffer(b"ask_ohlc", np.uint8)]
        for c in cases:
            self.assertEqual(hash_arrays(c), _old_hash(c), c.dtype)
        self.assertEqual(hash_arrays(*cases), _old_hash(*cases))

    def test_bar_fingerprint_memo(self):
        b = _bars()
        raw = b._content_hash()
        self.assertEqual(b.content_hash(), raw)
        self.assertEqual(b.__dict__["_hash_memo"], raw)
        for k in (60, 2500, len(b) - 1):                            # causality-check truncations
            self.assertEqual(b.head(k).content_hash(), b.head(k)._content_hash())
            self.assertEqual(b.head(k).content_hash(), b.head(k)._content_hash())   # second time from the parent
        self.assertEqual(set(b.__dict__["_head_hashes"]), {60, 2500, len(b) - 1})
        b2 = pickle.loads(pickle.dumps(b))                          # to a worker: memos dropped, arrays read-only
        self.assertNotIn("_hash_memo", b2.__dict__)
        self.assertTrue(b2._frozen())
        self.assertEqual(b2.content_hash(), raw)
        b.close.flags.writeable = True                              # writeable again: never trusted, recomputed
        b.close[0] += 1.0
        self.assertNotEqual(b.content_hash(), raw)
        self.assertEqual(b.content_hash(), b._content_hash())


class TestCaches(unittest.TestCase):
    def test_feature_cache_bounded_by_bytes_and_count(self):
        arr = lambda v: {"x": np.full(1000, float(v))}                 # 8,000 bytes  # noqa: E731
        c = FeatureCache(None, memory_entries=512, max_bytes=20_000)
        for i in range(5):
            c.put("D", f"k{i}", arr(i), {})
        self.assertLessEqual(c.bytes, 20_000)
        self.assertEqual(list(c._mem), ["k3", "k4"])                   # least recently used dropped first
        hit, src = c.get("D", "k4")
        self.assertEqual((src, float(hit["x"][0])), ("memory", 4.0))
        self.assertIsNone(c.get("D", "k0")[0])                         # evicted: a miss, never a wrong value
        c2 = FeatureCache(None, memory_entries=2, max_bytes=10 ** 9)
        for i in range(4):
            c2.put("D", f"k{i}", arr(i), {})
        self.assertEqual(list(c2._mem), ["k2", "k3"])                  # the count bound (configs) still applies
        with mock.patch.dict(os.environ, {"EDGELAB_FEATURE_CACHE_MB": "3"}):
            self.assertEqual(FeatureCache(None).max_bytes, 3 * 1024 * 1024)

    def test_derived_memo(self):
        m = DerivedMemo(max_bytes=20_000)
        calls = []
        val = m.get_or_compute("a", lambda: calls.append(1) or (np.ones(1000), np.zeros(1000)))
        again = m.get_or_compute("a", lambda: calls.append(1) or None)
        self.assertIs(val, again)
        self.assertEqual(calls, [1])
        self.assertFalse(val[0].flags.writeable)                       # shared values are read-only
        m.get_or_compute("b", lambda: np.ones(2000))                   # 16,000 bytes: "a" is evicted
        self.assertNotIn("a", m._d)
        self.assertLessEqual(m.bytes, 20_000)
        off = DerivedMemo(max_bytes=0)
        self.assertEqual(off.get_or_compute("x", lambda: 5), 5)
        self.assertFalse(off._d)

    def test_memoized_derivations_equal_fresh_ones(self):
        from edgelab.core.memo import derived
        from edgelab.data.calendar import load_calendars
        from edgelab.features.mtf import _build_htf, build_htf
        from edgelab.features.sessions import load_sessions
        from tests.helpers import CFG
        cal = load_calendars(CFG)["CME_EQUITY"]
        b = _bars(20000, ask=False)
        b = BarArrays(b.ts_ns, b.open, b.high, b.low, b.close, b.volume, 1)
        derived().clear()
        for tf in (5, 15, 60):
            fresh, known = _build_htf(b, cal, tf)
            for _ in range(2):
                memo, known2 = build_htf(b, cal, tf)
                self.assertEqual(memo.content_hash(), fresh.content_hash())
                np.testing.assert_array_equal(known2, known)
        np.testing.assert_array_equal(cal.trading_dates_of(b), cal.trading_dates(b.ts))
        for w in load_sessions(CFG).values():
            for got, want in zip(w.membership_of(b), w.membership(b.ts_ns)):
                np.testing.assert_array_equal(got, want)
        rng = np.random.default_rng(5)                                  # truncations: bar-by-bar, so a prefix of the full
        for k in sorted(set(rng.integers(50, len(b), 25).tolist())) + [len(b) - 1]:
            h = b.head(k)
            np.testing.assert_array_equal(cal.trading_dates_of(h), cal.trading_dates(h.ts))
            for w in load_sessions(CFG).values():
                for got, want in zip(w.membership_of(h), w.membership(h.ts_ns)):
                    np.testing.assert_array_equal(got, want)
        for d in pd.date_range("2024-03-01", "2024-03-20").date:
            self.assertEqual(cal.session_bounds(d), cal._session_bounds(d))

    def test_vectorized_session_lookup_equals_the_old_dict_lookup(self):
        from edgelab.data.calendar import load_calendars
        from tests.helpers import CFG
        cal = load_calendars(CFG)["CME_EQUITY"]
        b = _bars(30000, ask=False)
        ts_idx = pd.DatetimeIndex(b.ts)
        td = cal.trading_dates(ts_idx)
        uniq = np.unique(td)
        old = {d: cal.session_bounds(pd.Timestamp(d).date())[0].tz_convert("UTC").as_unit("ns").value for d in uniq}
        want = np.array([old[d] for d in td], dtype=np.int64)
        arr = np.array([old[d] for d in uniq], dtype=np.int64)
        np.testing.assert_array_equal(arr[np.searchsorted(uniq, td)], want)


class TestMemoryPlan(unittest.TestCase):
    class D:
        def __init__(self, n):
            self.bars = _bars(n)

    def test_cores_fit_the_free_memory(self):
        GB = 1024 ** 3
        ds = [self.D(200_000)]
        p = M.plan(15, ds, available=60 * GB, total=64 * GB)
        self.assertEqual((p.processes, p.limited_by_memory), (15, False))
        small = M.plan(15, ds, available=6 * GB, total=32 * GB)
        self.assertLess(small.processes, 15)
        self.assertTrue(small.limited_by_memory)
        self.assertIn("limited by memory", small.note)
        need = small.processes * (small.worker_fixed_bytes + (small.feature_cache_mb + small.truncation_cache_mb
                                                              + small.derived_cache_mb) * M.MB)
        self.assertLessEqual(need, M.USABLE_SHARE * 6 * GB - M.PARENT_RESERVE_MB * M.MB + 1)
        self.assertEqual(M.plan(15, ds, available=1 * GB, total=32 * GB).processes, 1)   # never fewer than one
        self.assertEqual(M.plan(4, ds, available=None, total=None).processes in (1, 2, 3, 4), True)
        self.assertGreater(M.dataset_bytes(ds[0]), 200_000 * 8 * 9)

    def test_memory_failures_are_recognised(self):
        for t in ("MemoryError: Unable to allocate 8.51 MiB", "BrokenProcessPool: a child process terminated",
                  "numpy._core._exceptions._ArrayMemoryError: Unable to allocate 1.70 MiB"):
            self.assertTrue(M.is_memory_failure(t))
        self.assertFalse(M.is_memory_failure("ValueError: boom"))
        self.assertFalse(M.is_memory_failure(None))


class TestRestartWithFewerCores(unittest.TestCase):
    def test_out_of_memory_halves_the_cores(self):
        from edgelab.research import jobs as J
        svc = mock.MagicMock()
        svc.store.list_search_batches.return_value = []
        svc.store.get_search_batch.return_value = None
        with mock.patch("edgelab.research.campaign.reconcile_run_records", return_value=[]):
            m = J.JobManager(svc, threading.RLock())
        m.RESTART_FIRST = m.RESTART_MAX = 0.01
        seen = []

        def runner(*a, **kw):
            seen.append(kw["processes"])
            if len(seen) < 4:
                return {"run_status": "stopped_on_failure", "n_failed_cells": 2,
                        "failed_cells": [{"error": "ValueError: x"}, {"error": "MemoryError: Unable to allocate 8.51 MiB"}]}
            return {"run_status": "completed", "n_failed_cells": 0}

        job = J.CampaignJob("JOB_M", "CMP_X", "SRCH_X", None)
        job.processes = 15
        with mock.patch("edgelab.research.campaign.run_scope", side_effect=runner):
            m._work_campaign(job, 0)
        self.assertEqual(seen, [15, 7, 3, 1])
        self.assertEqual(job.state, "completed")
        self.assertIn("out of memory: restarting with 1 CPU core", job.last_error)


class TestTinyCachesSameResults(unittest.TestCase):
    """Constant cache evictions change nothing: identical cells, trades, metrics, prop audits and trial ledger."""

    def run_scope(self, ws):
        from edgelab.services import Services
        from tests.test_desktop_research_runs import wait
        root, res, pid, cid = ws
        s = Services(root=root)
        self.addCleanup(s.store.close)
        d = s.campaign_detail(cid)
        fams = [f["family_id"] for f in d["families"][:6]]
        st = wait(s, s.start_campaign_job(cid, fams, processes=1)["job_id"])
        self.assertEqual(st["state"], "completed", st)
        cells = {c["strategy_id"]: (c["trades_hash"], c["headline_json"])
                 for c in s.store.list_search_cells(d["search_id"]) if c["status"] == "completed"}
        runs = s.store._query("SELECT strategy_id, trades_hash, record_json FROM runs ORDER BY run_id")
        return {"cells": cells, "runs": [(r[0], r[1]) for r in runs],
                "props": {r[0]: json.loads(r[2]).get("prop") for r in runs},
                "ledger": [(e["strategy_id"], e["counted"], e["status"]) for e in s.store.list_trial_events(pid)]}

    def test_tiny_caches_equal_default(self):
        from edgelab.core import memo
        from edgelab.features import strategy_api
        from tests.test_desktop_research_runs import build_workspace
        a, b = build_workspace(), build_workspace()
        self.addCleanup(shutil.rmtree, a[0], True)
        self.addCleanup(shutil.rmtree, b[0], True)
        base = self.run_scope(a)
        tiny = {"EDGELAB_FEATURE_CACHE_MB": "1", "EDGELAB_DERIVED_CACHE_MB": "1", "EDGELAB_CAUSALITY_CACHE_MB": "1"}
        with mock.patch.dict(os.environ, tiny):
            memo._MEMO.clear()
            strategy_api._TRUNC_CACHE.clear()
            try:
                small = self.run_scope(b)
            finally:
                memo._MEMO.clear()
                strategy_api._TRUNC_CACHE.clear()
        self.assertTrue(base["cells"])
        self.assertEqual(base, small)


if __name__ == "__main__":
    unittest.main()
