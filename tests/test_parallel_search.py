"""Phase 4 step 7: parallel search (workers > 1) in worker processes.

Guarantees: workers only compute (they have no ResultStore), the parent writes everything in plan
order, and a parallel run reproduces the sequential run exactly (cell ids, statuses, trades
hashes, failure texts); the worker count never changes the search identity; the shared on-disk
feature cache stays uncorrupted with several processes writing and reading it."""
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from edgelab.features.cache import FeatureCache
from edgelab.research import batch
from tests.test_batch_runner import build_workspace

REPO = Path(__file__).resolve().parents[1]
WEEKEND = {"start": "2024-03-09T00:00:00Z", "end": "2024-03-09T23:00:00Z"}   # inside the range, no bars


def key(summary):
    return [(c["cell_id"], c["plan_index"], c["status"], c["trades_hash"], c["error"]) for c in summary["cells"]]


def _cache_stress(root: str, worker: int) -> dict:
    """One process writing and re-reading the same 30 keys as its siblings (spawned)."""
    c = FeatureCache(root, memory_entries=1)
    bad = 0
    for rnd in range(3):
        for i in range(30):
            rng = np.random.default_rng(i)
            arrays = {"value": rng.normal(size=3000), "ts": np.arange(3000, dtype=np.int64) + i}
            c.put("DS", f"k{i:02d}", arrays, {"feature": "stress"})
            c.clear_memory()
            got, _ = c.get("DS", f"k{(i + worker + rnd) % 30:02d}")
            if got is not None:
                want = np.random.default_rng((i + worker + rnd) % 30).normal(size=3000)
                bad += int(got["value"].tobytes() != want.tobytes())
    return {"corrupt": c.stats["corrupt"], "mismatches": bad}


class TestParallelSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seq_root, cls.par_root = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        cls.seq, cls.ids = build_workspace(cls.seq_root)
        cls.par, ids2 = build_workspace(cls.par_root)
        assert ids2 == cls.ids                                          # content-addressed: same ids
        i = cls.ids
        cls.spec = {"strategies": {"ids": [i["ema"], i["ema3"], i["never"], i["ema15"], i["ema"]],
                                   "variation_batches": [i["vb"]]},
                    "datasets": [i["fut"], i["cfd"]]}
        cls.s = cls.seq.run_search(cls.spec)                            # workers=1 (spec default)
        cls.p = cls.par.run_search(cls.spec, workers=3)                 # cold, shared feature cache

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.seq_root, ignore_errors=True)
        shutil.rmtree(cls.par_root, ignore_errors=True)

    # ------------------------------------------------------------------ paths and identity
    def test_sequential_and_parallel_paths(self):
        self.assertEqual(self.s["execution"], {"workers": 1, "mode": "sequential", "worker_processes": 0})
        self.assertEqual(self.p["execution"]["mode"], "processes")
        self.assertGreaterEqual(self.p["execution"]["worker_processes"], 1)
        self.assertEqual(self.p["search_id"], self.s["search_id"])      # worker count is not identity
        self.assertEqual(self.p["search_hash"], self.s["search_hash"])
        self.assertEqual(self.par.run_search({**self.spec, "workers": 2})["search_id"], self.p["search_id"])

    def test_parallel_reproduces_sequential(self):
        self.assertEqual(key(self.p), key(self.s))                      # ids, order, status, trades hash
        self.assertEqual(self.p["n_evaluated"], 5)                      # ema, ema3, never, 2 VB children
        for k in ("n_planned", "n_eligible", "n_ineligible", "n_evaluated", "n_failed", "n_trials"):
            self.assertEqual(self.p[k], self.s[k], k)
        self.assertEqual(self.p["cumulative"], self.s["cumulative"])
        ids = [c["cell_id"] for c in self.p["cells"]]
        self.assertEqual(len(ids), len(set(ids)))                        # no duplicate cells
        self.assertEqual(len({c["strategy_id"] for c in self.p["cells"]}), 6)   # duplicate id collapsed

    def test_plan_order_persistence_and_unique_run_ids(self):
        cells = self.par.store.list_search_cells(self.p["search_id"])
        self.assertEqual([c["plan_index"] for c in cells], list(range(len(cells))))
        run_ids = [c["run_id"] for c in cells if c["run_id"]]
        self.assertEqual(run_ids, sorted(run_ids))                       # recorded in plan order
        self.assertEqual(len(set(run_ids)), len(run_ids))
        mine = [r for r in self.par.list_runs() if f"search {self.p['search_id']} " in (r.get("notes") or "")]
        self.assertEqual(sorted(r["run_id"] for r in mine), run_ids)      # exactly one run per completed cell
        for c in cells:
            if c["run_id"]:
                rec, _ = self.par.store.load_run(c["run_id"])
                self.assertEqual((rec["status"], rec["trades_hash"]), ("IN_SAMPLE", c["trades_hash"]))
                self.assertIn(f"search {self.p['search_id']} cell {c['cell_id']}", rec["notes"])
        vb_children = set(self.par.library.load_batch(self.ids["vb"])["children"])
        for c in cells:                                                  # lineage recorded by the parent
            if c["strategy_id"] in vb_children and c["run_id"]:
                rec, _ = self.par.store.load_run(c["run_id"])
                self.assertEqual(rec["strategy"]["parent_strategy_id"], self.ids["ema"])
                self.assertTrue(rec["strategy"]["mutation"].startswith("rr: 2.0 -> "))

    def test_workers_only_compute_and_never_write(self):
        ctx_args = (self.par.cfg, self.par._config_hash(), {self.ids["fut"]: self.par.load_dataset(self.ids["fut"])},
                    {"root": None, "verify": True, "memory_entries": 8})
        batch._worker_init(*ctx_args)                                    # run the worker code in-process
        try:
            self.assertFalse(hasattr(batch._WORKER["ctx"], "store"))
            runs = len(self.par.store.list_runs())
            out = batch._worker_cell(self.par.library.load(self.ids["ema"])["definition"], self.ids["fut"], None)
            self.assertNotIn("run_id", out)
            self.assertEqual(len(self.par.store.list_runs()), runs)
            single = self.par.backtest_strategy(self.ids["ema"], self.ids["fut"])
            self.assertEqual(out["result"].trades_hash, single["trades_hash"])   # same _run_cell pipeline
        finally:
            batch._WORKER.clear()

    # ------------------------------------------------------------------ failures / resume / cancel
    def test_failed_cells_match_sequential_semantics(self):
        spec = {**self.spec, "period": WEEKEND}
        a, b = self.seq.run_search(spec), self.par.run_search(spec, workers=2)
        self.assertEqual(key(b), key(a))
        failed = [c for c in b["cells"] if c["status"] == "failed"]
        self.assertEqual(len(failed), 5)
        self.assertTrue(all("no bars" in c["error"] and c["run_id"] is None for c in failed))
        self.assertEqual((b["n_evaluated"], b["n_failed"], b["n_trials"]), (5, 5, 5))

    def test_resume_after_parallel_run(self):
        runs = len(self.par.store.list_runs())
        again = self.par.run_search(self.spec, workers=3)
        self.assertEqual((again["n_evaluated"], again["n_skipped_resume"], again["n_trials"]), (0, 5, 0))
        self.assertEqual(len(self.par.store.list_runs()), runs)
        self.assertEqual(key(again), key(self.p))
        seq_again = self.par.run_search(self.spec)                       # and sequentially
        self.assertEqual(seq_again["n_skipped_resume"], 5)

    def test_cancellation_between_cells(self):
        spec = {**self.spec, "seed": 99}                                 # a fresh search
        calls = []

        def cancel():                                                    # True from the 3rd check on
            calls.append(1)
            return len(calls) > 2
        out = batch.run_search(self.par, spec, 2, cancel=cancel)
        by = [c["status"] for c in out["cells"] if c["status"] != "ineligible"]
        self.assertEqual(by, ["completed", "completed", "cancelled", "cancelled", "cancelled"])
        self.assertEqual((out["status"], out["n_cancelled"], out["n_evaluated"]), ("cancelled", 3, 2))
        resumed = self.par.run_search(spec, workers=2)
        self.assertEqual((resumed["n_skipped_resume"], resumed["n_evaluated"]), (2, 3))
        self.assertEqual([c["trades_hash"] for c in resumed["cells"]],
                         [c["trades_hash"] for c in self.seq.run_search(spec)["cells"]])

    # ------------------------------------------------------------------ feature cache across processes
    def test_shared_feature_cache_is_not_corrupted(self):
        root = self.par.cache.root
        self.assertTrue(any(root.rglob("*.npz")))                        # workers wrote the cold cache
        self.assertEqual(list(root.rglob("*.tmp*")), [])
        fresh = FeatureCache(root)                                       # verify every stored entry
        entries = fresh.entries()
        self.assertGreater(len(entries), 0)
        for e in entries:
            got, src = fresh.get(e["dataset_id"], e["key"])
            self.assertEqual(src, "disk", e["key"])
        self.assertEqual(fresh.stats["corrupt"], 0)

    def test_concurrent_processes_writing_the_same_keys(self):
        root = tempfile.mkdtemp()
        try:
            with ProcessPoolExecutor(4, mp_context=multiprocessing.get_context("spawn")) as ex:
                results = list(ex.map(_cache_stress, [root] * 4, range(4)))
            self.assertEqual(results, [{"corrupt": 0, "mismatches": 0}] * 4)
            self.assertEqual(list(Path(root).rglob("*.tmp*")), [])
            fresh = FeatureCache(root)
            for i in range(30):
                got, src = fresh.get("DS", f"k{i:02d}")
                self.assertEqual(src, "disk")
                self.assertEqual(got["value"].tobytes(), np.random.default_rng(i).normal(size=3000).tobytes())
            self.assertEqual(fresh.stats["corrupt"], 0)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    # ------------------------------------------------------------------ benchmark
    def test_benchmark_script_runs(self):
        r = subprocess.run([sys.executable, str(REPO / "scripts" / "benchmark_search.py"), "--quick", "--workers", "2",
                            "--json"], cwd=REPO, capture_output=True, text=True, timeout=600,
                           env={**os.environ, "PYTHONPATH": str(REPO)})
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        import json
        rep = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(rep["sequential_parallel_match"])
        self.assertEqual([x["workers"] for x in rep["runs"]], [1, 2])
        self.assertEqual([x["mode"] for x in rep["runs"]], ["sequential", "processes"])
        self.assertTrue(all(x["cells_evaluated"] == 2 and x["elapsed_s"] > 0 for x in rep["runs"]))


if __name__ == "__main__":
    unittest.main()
