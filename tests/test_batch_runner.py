"""Phase 4 step 3: durable SQLite search storage and the synchronous `run_search` runner.

Guarantees: every planned cell is stored (completed / failed / ineligible), completed cells
come from the same pipeline as `backtest_strategy` and link to a normal IN_SAMPLE run,
resume never re-runs or double-counts a completed cell, and DuckDB refuses search storage."""
import copy
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import yaml

from edgelab.data.store import DuckDBStore, SearchStorageUnsupported, SQLiteStore
from edgelab.research.search import SearchSpecError
from edgelab.services import Services
from tests.phase2_helpers import synthetic_canonical, write_generic_utc

REPO = Path(__file__).resolve().parents[1]
FX = REPO / "strategies" / "fixtures"


def fixture(name: str) -> dict:
    return yaml.safe_load((FX / name).read_text())


def build_workspace(root: Path):
    """Two identical-content imports (NQ future, unconfigured CFD) + strategies (one per case)."""
    shutil.copytree(REPO / "configs", root / "configs")
    df = synthetic_canonical("2024-03-04", "2024-03-22", tf=5, seed=9)
    write_generic_utc(df, root / "fut.csv")
    write_generic_utc(df, root / "cfd.csv")
    s = Services(root=root)
    ids = {"fut": s.import_file(dict(file=str(root / "fut.csv"), instrument="NQ", provider="SYNTHF",
                                     asset_type="FUTURE", timeframe="5m", source_timezone="UTC"))["dataset_id"],
           "cfd": s.import_file(dict(file=str(root / "cfd.csv"), instrument="NAS100_CFD", provider="SYNTHC",
                                     asset_type="CFD", timeframe="5m", source_timezone="UTC"))["dataset_id"]}
    ema = fixture("ema_crossover.yaml")
    ids["ema"] = s.save_strategy(ema)["strategy_id"]
    ema3 = copy.deepcopy(ema)
    ema3["sizing"] = {"mode": "fixed", "quantity": 3}
    ids["ema3"] = s.save_strategy(ema3)["strategy_id"]
    never = fixture("rsi_threshold.yaml")
    never["entry"]["long"] = {"left": {"bar": "close"}, "op": "<", "right": 0}          # price is never < 0
    ids["never"] = s.save_strategy(never)["strategy_id"]
    ema15 = copy.deepcopy(ema)
    ema15["timeframe"] = "15m"
    ids["ema15"] = s.save_strategy(ema15)["strategy_id"]
    vb = s.generate_variations(ema, {"mode": "grid", "dimensions": [{"parameter": "rr", "values": [1.5, 3.0]}]})
    ids["vb"] = vb["batch_id"]
    return s, ids


class TestBatchRunner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        cls.svc, cls.ids = build_workspace(cls.root)
        i = cls.ids
        cls.spec = {"strategies": {"ids": [i["ema"], i["ema3"], i["never"], i["ema15"]]},
                    "datasets": [i["fut"], i["cfd"]]}
        cls.runs_before = len(cls.svc.store.list_runs())
        cls.first = cls.svc.run_search(cls.spec)
        cls.runs_after_first = len(cls.svc.store.list_runs())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def cells(self, summary=None):
        return {(c["strategy_id"], c["dataset_id"]): c for c in (summary or self.first)["cells"]}

    # ------------------------------------------------------------------ storage
    def test_sqlite_tables_exist_and_persist(self):
        path = self.svc.store.path
        con = sqlite3.connect(path)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        self.assertTrue({"search_batches", "search_cells", "runs"} <= tables)
        reopened = SQLiteStore(path)                                       # a fresh connection sees it all
        try:
            b = reopened.get_search_batch(self.first["search_id"])
            self.assertEqual(b["status"], "completed")
            self.assertEqual(len(reopened.list_search_cells(self.first["search_id"])), 8)
            self.assertIn(self.first["search_id"], [x["search_id"] for x in reopened.list_search_batches()])
        finally:
            reopened.close()

    def test_duckdb_search_storage_is_refused(self):
        duck = object.__new__(DuckDBStore)                                 # no duckdb needed to hit the guard
        for call in (lambda: duck.upsert_search_batch({}), lambda: duck.list_search_cells("S"),
                     lambda: duck.get_search_batch("S"), lambda: duck.list_search_batches()):
            with self.assertRaises(SearchStorageUnsupported) as cm:
                call()
            self.assertIn("SQLite-backed only", str(cm.exception))
        real = self.svc.store
        self.svc.store = duck
        try:
            with self.assertRaises(SearchStorageUnsupported):
                self.svc.run_search(self.spec)
        finally:
            self.svc.store = real

    # ------------------------------------------------------------------ execution
    def test_eligible_cells_run_in_plan_order_and_link_runs(self):
        i = self.ids
        cells = self.first["cells"]
        self.assertEqual([c["plan_index"] for c in cells], list(range(8)))
        done = [c for c in cells if c["status"] == "completed"]
        self.assertEqual({c["strategy_id"] for c in done}, {i["ema"], i["ema3"], i["never"]})
        self.assertTrue(all(c["dataset_id"] == i["fut"] for c in done))
        run_ids = [c["run_id"] for c in done]
        self.assertEqual(run_ids, sorted(run_ids))                         # recorded in plan order
        self.assertEqual(len(set(run_ids)), len(run_ids))                  # unique
        for c in done:
            rec, trades = self.svc.store.load_run(c["run_id"])
            self.assertEqual(rec["strategy"]["strategy_id"], c["strategy_id"])
            self.assertEqual(rec["dataset"]["dataset_id"], c["dataset_id"])
            self.assertEqual(rec["trades_hash"], c["trades_hash"])
            self.assertEqual(rec["status"], "IN_SAMPLE")
            self.assertIn(f"search {self.first['search_id']} cell {c['cell_id']}", rec["notes"])
            self.assertEqual(len(trades), c["headline"]["trade_count"])
        self.assertEqual(self.runs_after_first - self.runs_before, 3)

    def test_same_pipeline_as_backtest_strategy(self):
        by = self.cells()
        for key in ("ema", "ema3", "never"):
            with self.subTest(key):
                single = self.svc.backtest_strategy(self.ids[key], self.ids["fut"])
                cell = by[(self.ids[key], self.ids["fut"])]
                self.assertEqual(cell["trades_hash"], single["trades_hash"])
                self.assertEqual(cell["headline"]["trade_count"], single["metrics"]["trade_count"])

    def test_strategy_own_sizing_is_honoured(self):
        c = self.cells()[(self.ids["ema3"], self.ids["fut"])]
        _, trades = self.svc.store.load_run(c["run_id"])
        self.assertGreater(len(trades), 0)
        self.assertEqual(set(trades["contracts"]), {3})
        self.assertNotEqual(c["trades_hash"], self.cells()[(self.ids["ema"], self.ids["fut"])]["trades_hash"])

    def test_zero_trade_cell_is_stored(self):
        c = self.cells()[(self.ids["never"], self.ids["fut"])]
        self.assertEqual(c["status"], "completed")
        self.assertIsNotNone(c["run_id"])
        self.assertEqual(c["headline"]["trade_count"], 0)
        self.assertEqual(len(self.svc.store.load_run(c["run_id"])[1]), 0)

    def test_ineligible_and_cfd_cells_never_execute(self):
        i = self.ids
        by = self.cells()
        for sid in (i["ema"], i["ema3"], i["never"], i["ema15"]):
            rd = {d["dataset_id"]: d for d in self.svc.backtest_readiness(sid)["datasets"]}
            c = by[(sid, i["cfd"])]
            self.assertEqual(c["status"], "ineligible")
            self.assertIsNone(c["run_id"])
            self.assertEqual(c["reasons"], rd[i["cfd"]]["reasons"])          # same readiness semantics
            self.assertIn("cost profile is unconfigured", c["reasons"][0])   # CFD costs never invented
        tf = by[(i["ema15"], i["fut"])]
        self.assertEqual((tf["status"], tf["run_id"]), ("ineligible", None))
        self.assertEqual(tf["reasons"], ["timeframe 5m does not match the strategy timeframe 15m"])
        self.assertNotIn(i["cfd"], set(self.svc.store.list_runs()["dataset_id"]))

    def test_accounting(self):
        f = self.first
        self.assertEqual((f["n_planned"], f["n_eligible"], f["n_ineligible"]), (8, 3, 5))
        self.assertEqual((f["n_evaluated"], f["n_trials"], f["n_skipped_resume"], f["n_failed"], f["n_cancelled"]),
                         (3, 3, 0, 0, 0))
        self.assertEqual(f["cumulative"], {"completed": 3, "failed": 0, "pending": 0, "ineligible": 5,
                                           "cancelled": 0, "trials": 3})
        self.assertEqual(f["status"], "completed")
        self.assertIsNotNone(f["finished_at"])
        self.assertEqual(f["spec"]["datasets"], sorted([self.ids["fut"], self.ids["cfd"]]))

    def test_failed_cells_are_recorded_and_retried_not_skipped(self):
        weekend = {"start": "2024-03-09T00:00:00Z", "end": "2024-03-09T23:00:00Z"}   # inside the range, no bars
        spec = {"strategies": {"ids": [self.ids["ema"]]}, "datasets": [self.ids["fut"]], "period": weekend}
        runs = len(self.svc.store.list_runs())
        a = self.svc.run_search(spec)
        (c,) = a["cells"]
        self.assertEqual(c["status"], "failed")
        self.assertIsNone(c["run_id"])
        self.assertIn("no bars", c["error"])
        self.assertEqual((a["n_evaluated"], a["n_failed"], a["n_trials"]), (1, 1, 1))
        b = self.svc.run_search(spec)                                       # failures run again
        self.assertEqual((b["n_evaluated"], b["n_failed"], b["n_skipped_resume"]), (1, 1, 0))
        self.assertEqual(b["cumulative"]["trials"], 1)                      # still one distinct trial
        self.assertEqual(len(self.svc.store.list_runs()), runs)

    def test_lineage_is_recorded_on_cell_runs(self):
        s = self.svc.run_search({"strategies": {"variation_batches": [self.ids["vb"]]}, "datasets": [self.ids["fut"]]})
        muts = set()
        for c in s["cells"]:
            rec, _ = self.svc.store.load_run(c["run_id"])
            self.assertEqual(rec["strategy"]["parent_strategy_id"], self.ids["ema"])
            muts.add(rec["strategy"]["mutation"])
        self.assertEqual(muts, {"rr: 2.0 -> 1.5", "rr: 2.0 -> 3.0"})

    # ------------------------------------------------------------------ resume / reproducibility
    def test_resume_skips_completed_cells(self):
        runs = len(self.svc.store.list_runs())
        again = self.svc.run_search({**self.spec, "ranking": {"metric": "net_r"}, "max_cells": 50})  # same search
        self.assertEqual(again["search_id"], self.first["search_id"])
        self.assertEqual((again["n_evaluated"], again["n_trials"], again["n_skipped_resume"]), (0, 0, 3))
        self.assertEqual(again["cumulative"]["trials"], 3)
        self.assertEqual(len(self.svc.store.list_runs()), runs)              # no duplicate runs
        self.assertEqual({c["cell_id"]: c["run_id"] for c in again["cells"]},
                         {c["cell_id"]: c["run_id"] for c in self.first["cells"]})
        self.assertEqual(again["created_at"], self.first["created_at"])

    def test_resume_reruns_a_completed_cell_whose_run_is_gone(self):
        c = self.cells()[(self.ids["ema"], self.ids["fut"])]
        self.svc.store._exec("DELETE FROM runs WHERE run_id = ?", (c["run_id"],))
        try:
            again = self.svc.run_search(self.spec)
            self.assertEqual((again["n_evaluated"], again["n_skipped_resume"]), (1, 2))
            new = self.cells(again)[(self.ids["ema"], self.ids["fut"])]
            self.assertNotEqual(new["run_id"], c["run_id"])
            self.assertEqual(new["trades_hash"], c["trades_hash"])
        finally:
            type(self).first = self.svc.run_search(self.spec)              # keep class state consistent

    def test_config_change_is_a_new_search_not_a_resume(self):
        other = Services(cfg={**self.svc.cfg, "sample_size": {"min_trades": 7, "preferred_trades": 70}},
                         root=self.root)
        try:
            s = other.run_search(self.spec)
            self.assertNotEqual(s["search_id"], self.first["search_id"])
            self.assertNotEqual(s["config_hash"], self.first["config_hash"])
            self.assertEqual((s["n_evaluated"], s["n_skipped_resume"], s["n_trials"]), (3, 0, 3))
            self.assertTrue({c["cell_id"] for c in s["cells"]}.isdisjoint(c["cell_id"] for c in self.first["cells"]))
            old = other.store.get_search_batch(self.first["search_id"])        # the first search is untouched
            self.assertEqual((old["config_hash"], old["n_planned"]), (self.first["config_hash"], 8))
            again = self.svc.run_search(self.spec)                              # original config still resumes
            self.assertEqual(again["search_id"], self.first["search_id"])
            self.assertEqual((again["n_evaluated"], again["n_skipped_resume"]), (0, 3))
        finally:
            other.store.close()

    def test_current_plan_accounting_excludes_obsolete_cells(self):
        i = self.ids
        fam = self.svc.library.load(i["ema"])["family_id"]
        spec = {"strategies": {"families": [fam]}, "datasets": [i["fut"]]}
        members = {r["strategy_id"] for r in self.svc.library.list(fam)}     # ema, ema3, ema15, 2 VB children
        a = self.svc.run_search(spec)
        self.assertEqual((a["n_planned"], a["n_eligible"]), (len(members), len(members) - 1))   # ema15 is 15m
        self.assertEqual(a["cumulative"]["trials"], len(members) - 1)
        self.svc.archive_strategy(i["ema3"])                                  # family membership changes
        try:
            b = self.svc.run_search(spec)
            self.assertEqual(b["search_id"], a["search_id"])
            self.assertEqual((b["n_planned"], b["n_eligible"], b["n_ineligible"]),
                             (len(members) - 1, len(members) - 2, 1))
            self.assertEqual((b["n_evaluated"], b["n_skipped_resume"], b["n_trials"], b["n_failed"]), (0, len(members) - 2, 0, 0))
            self.assertEqual(b["cumulative"]["trials"], len(members) - 2)     # not inflated by the obsolete cell
            self.assertEqual(b["cumulative"]["completed"], len(members) - 2)
            self.assertNotIn(i["ema3"], {c["strategy_id"] for c in b["cells"]})
            (hist,) = b["historical_cells"]                                    # kept as history, run link intact
            self.assertEqual((hist["strategy_id"], hist["status"], hist["current"]), (i["ema3"], "completed", False))
            self.assertTrue(self.svc.store.has_run(hist["run_id"]))
            self.assertEqual(len(self.svc.store.list_search_cells(a["search_id"])), len(members))
        finally:
            self.svc.restore_strategy(i["ema3"])
        c = self.svc.run_search(spec)                                         # back in the plan: resumable
        self.assertEqual((c["n_evaluated"], c["n_skipped_resume"]), (0, len(members) - 1))
        self.assertEqual(c["historical_cells"], [])
        self.assertEqual({x["cell_id"]: x["run_id"] for x in c["cells"]}, {x["cell_id"]: x["run_id"] for x in a["cells"]})

    def test_parallel_workers_are_refused_for_now(self):
        with self.assertRaises(SearchSpecError):
            self.svc.run_search(self.spec, workers=2)
        with self.assertRaises(SearchSpecError):
            self.svc.run_search({**self.spec, "workers": 4})

    def test_reproducible_in_a_fresh_workspace(self):
        root2 = Path(tempfile.mkdtemp())
        try:
            svc2, ids2 = build_workspace(root2)
            self.assertEqual(ids2, self.ids)                                # content-addressed ids
            second = svc2.run_search(self.spec)
            self.assertEqual(second["search_id"], self.first["search_id"])
            key = lambda s: [(c["cell_id"], c["status"], c["trades_hash"]) for c in s["cells"]]  # noqa: E731
            self.assertEqual(key(second), key(self.first))
            svc2.store.close()
        finally:
            shutil.rmtree(root2, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
