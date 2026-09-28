"""Phase 4 step 5: strategy-library index and Mode B proposal-batch records.

The directory of JSON files stays authoritative; the index is derived, verified against the
directory scan and rebuilt whenever it is missing, stale, corrupted or of another version."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from edgelab.research.search import plan_search
from edgelab.services import Services
from edgelab.strategy.lineage import StrategyLibrary
from tests.phase2_helpers import synthetic_canonical, write_generic_utc

REPO = Path(__file__).resolve().parents[1]
FX = REPO / "strategies" / "fixtures"


def legacy_list(lib: StrategyLibrary, family_id=None, include_archived=False):
    """The pre-index StrategyLibrary.list (directory scan), kept as the reference."""
    dirs = [("instances", False)] + ([("archived", True)] if include_archived else [])
    out = []
    for sub, archived in dirs:
        for p in sorted((lib.root / sub).glob("*.json")):
            d = json.loads(p.read_text())
            if family_id is None or d.get("family_id") == family_id:
                out.append(lib._row(d, archived))
    return out


def legacy_variation_batches(lib: StrategyLibrary):
    """The pre-step-5 list_batches row, over variation batch records (the only kind it knew)."""
    out = []
    for p in sorted((lib.root / "batches").glob("*.json")):
        b = json.loads(p.read_text())
        if "kind" in b:
            continue
        out.append({"batch_id": b["batch_id"], "created_at": b.get("created_at"),
                    "base_strategy_id": b["base"]["strategy_id"], "base_name": b["base"]["definition"].get("name"),
                    "spec_name": b["spec"].get("name"), "mode": b["spec"].get("mode"),
                    "combinations": b.get("combinations"), "generated": b.get("generated"),
                    "duplicates": len(b.get("duplicates", [])), "same_as_base": len(b.get("same_as_base", []))})
    return out


class TestLibraryIndex(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", self.root / "configs")
        self.svc = Services(root=self.root)
        self.lib = self.svc.library
        self.ema = self.svc.save_strategy(str(FX / "ema_crossover.yaml"))["strategy_id"]
        self.rsi = self.svc.save_strategy(str(FX / "rsi_threshold.yaml"))["strategy_id"]
        self.vb = self.svc.generate_variations(str(FX / "opening_range_breakout.yaml"), str(FX / "orb_variations.yaml"))
        self.pb = self.svc.ingest_proposals(str(FX / "proposals_example.yaml"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def assert_matches_scan(self):
        self.assertEqual(self.lib.verify_index(), {"consistent": True, "differences": []})
        fams = {r["family_id"] for r in legacy_list(self.lib, include_archived=True)} | {None, "nope"}
        for fam in fams:
            for arch in (False, True):
                self.assertEqual(self.lib.list(fam, arch), legacy_list(self.lib, fam, arch), (fam, arch))
        self.assertEqual(self.lib.list_batches(), legacy_variation_batches(self.lib))

    # ------------------------------------------------------------------ index
    def test_index_builds_and_matches_the_directory_scan(self):
        self.assertFalse(self.lib.index_path.exists() and self.lib.last_index_event == "used")
        self.assert_matches_scan()
        self.assertTrue(self.lib.index_path.exists())
        self.lib.list()
        self.assertEqual(self.lib.last_index_event, "used")                 # fresh index is reused
        self.assertEqual(len(self.lib.list()), 2 + 36 + self.pb["n_accepted"])

    def test_missing_index_rebuilds(self):
        self.lib.list()
        self.lib.index_path.unlink()
        rows = self.lib.list()
        self.assertEqual(self.lib.last_index_event, "rebuilt:missing")
        self.assertEqual(rows, legacy_list(self.lib))
        self.assertTrue(self.lib.index_path.exists())

    def test_stale_index_rebuilds_after_every_kind_of_change(self):
        self.lib.list()
        changes = [lambda: self.svc.save_strategy(str(FX / "atr_breakout.yaml")),
                   lambda: self.lib.archive(self.rsi),
                   lambda: self.lib.restore(self.rsi),
                   lambda: self.svc.edit_strategy(self.rsi, {**yaml.safe_load((FX / "rsi_threshold.yaml").read_text()),
                                                             "exit": {"stop": {"type": "atr", "multiple": 3.0},
                                                                      "target": {"type": "none"}, "time_stop_bars": 12}}),
                   lambda: self.svc.save_strategy(str(FX / "ema_crossover.yaml"), "manual_edit", self.rsi)]  # lineage append
        for change in changes:
            change()
            self.lib.list()
            self.assertEqual(self.lib.last_index_event, "rebuilt:stale")
            self.assert_matches_scan()

    def test_corrupted_or_foreign_index_is_never_trusted(self):
        self.lib.list()
        doc = json.loads(self.lib.index_path.read_text())
        doc["payload"]["instances"] = doc["payload"]["instances"][:1]     # tampered, checksum now wrong
        self.lib.index_path.write_text(json.dumps(doc))
        self.assertEqual(self.lib.list(), legacy_list(self.lib))
        self.assertEqual(self.lib.last_index_event, "rebuilt:corrupt")
        for bad in ("{not json", "[]", json.dumps({"index_version": 999, "payload": {}})):
            self.lib.index_path.write_text(bad)
            self.assertEqual(self.lib.list(), legacy_list(self.lib))
            self.assertTrue(self.lib.last_index_event.startswith("rebuilt:"), self.lib.last_index_event)
        self.assert_matches_scan()

    def test_identity_and_lineage_are_untouched(self):
        before = {p.name: p.read_bytes() for sub in ("instances", "archived", "batches")
                  for p in (self.lib.root / sub).glob("*.json")}
        self.lib.index_path.unlink(missing_ok=True)
        self.lib.list()
        self.lib.verify_index()
        after = {p.name: p.read_bytes() for sub in ("instances", "archived", "batches")
                 for p in (self.lib.root / sub).glob("*.json")}
        self.assertEqual(after, before)                                     # only index.json is written
        doc = self.lib.load(self.ema)
        self.assertEqual(self.svc.validate_strategy(self.ema)["identity"],
                         {k: doc[k] for k in ("strategy_id", "logic_hash", "definition_hash")})

    # ------------------------------------------------------------------ proposal batches
    def test_proposal_batch_record_is_saved_and_discoverable(self):
        rec = self.lib.load_batch(self.pb["batch_id"])
        accepted = [a["strategy_id"] for a in self.pb["accepted"]]
        self.assertEqual(rec["kind"], "proposal")
        self.assertEqual(rec["children"], accepted)
        self.assertEqual((rec["n_accepted"], rec["n_rejected"]), (3, 5))
        self.assertEqual(len(rec["rejected"]), 5)                          # negative results kept
        self.assertEqual(rec["source"]["kind"], yaml.safe_load((FX / "proposals_example.yaml").read_text())["source"]["kind"])
        self.assertEqual(rec["config_hash"], self.svc._config_hash())
        for sid in accepted:
            lin = self.lib.load(sid)["lineage"]
            self.assertIn(self.pb["batch_id"], {r.get("generation_batch_id") for r in lin})
        self.assertEqual(self.lib.batch_members(self.pb["batch_id"]), sorted(accepted))
        (row,) = self.svc.list_variation_batches(kind="proposal")
        self.assertEqual(row, {"batch_id": self.pb["batch_id"], "kind": "proposal", "created_at": rec["created_at"],
                               "source_kind": rec["source"]["kind"], "n_accepted": 3, "n_rejected": 5, "children": 3})
        detail = self.svc.get_variation_batch(self.pb["batch_id"])
        self.assertEqual([c["strategy_id"] for c in detail["children_detail"]], accepted)

    def test_save_false_stores_no_batch_record(self):
        before = sorted(p.name for p in (self.lib.root / "batches").glob("*.json"))
        batch = yaml.safe_load((FX / "proposals_example.yaml").read_text())
        batch["request"] = {**(batch.get("request") or {}), "n_families": 99}    # a different batch id
        out = self.svc.ingest_proposals(batch, save=False)
        self.assertNotIn(f"{out['batch_id']}.json", before)
        self.assertEqual(sorted(p.name for p in (self.lib.root / "batches").glob("*.json")), before)

    def test_list_variation_batches_default_is_backward_compatible(self):
        default = self.svc.list_variation_batches()
        self.assertEqual(default, legacy_variation_batches(self.lib))
        self.assertEqual([b["batch_id"] for b in default], [self.vb["batch_id"]])
        self.assertTrue(all("kind" not in b for b in default))              # row format unchanged
        self.assertEqual(self.svc.list_variation_batches(kind="variation"), default)
        self.assertEqual([b["batch_id"] for b in self.svc.list_variation_batches(kind="proposal")], [self.pb["batch_id"]])
        self.assertEqual(self.svc.system_status()["variation_batches"], 1)
        with self.assertRaises(ValueError):
            self.svc.list_variation_batches(kind="other")

    def test_mode_a_variation_behaviour_is_unchanged(self):
        rec = self.lib.load_batch(self.vb["batch_id"])
        self.assertNotIn("kind", rec)                                       # variation records unchanged
        self.assertEqual(self.vb["generated"], 35)
        self.assertEqual(rec["children"], self.svc.get_variation_batch(self.vb["batch_id"])["children"])
        self.assertEqual(len(self.svc.list_strategies("ny_opening_range_breakout")), 36)
        self.assertEqual(sorted(self.lib.batch_members(self.vb["batch_id"])), sorted(rec["children"]))

    # ------------------------------------------------------------------ search integration
    def test_search_resolves_proposal_batches_from_the_record(self):
        df = synthetic_canonical("2024-03-04", "2024-03-08", tf=5, seed=9)
        write_generic_utc(df, self.root / "fut.csv")
        fut = self.svc.import_file(dict(file=str(self.root / "fut.csv"), instrument="NQ", provider="SYNTHF",
                                        asset_type="FUTURE", timeframe="5m", source_timezone="UTC"))["dataset_id"]
        spec = {"strategies": {"proposal_batches": [self.pb["batch_id"]]}, "datasets": [fut]}
        expected = sorted(a["strategy_id"] for a in self.pb["accepted"])
        lib, real_list, real_members = self.lib, self.lib.list, self.lib.batch_members

        def refuse(*a, **k):
            raise AssertionError("proposal batch resolution must not scan the library")
        lib.list, lib.batch_members = refuse, refuse
        try:
            plan = plan_search(spec, self.svc)
        finally:
            lib.list, lib.batch_members = real_list, real_members
        self.assertEqual([s["strategy_id"] for s in plan.strategies], expected)
        self.assertTrue(all(s["sources"] == [self.pb["batch_id"]] for s in plan.strategies))
        os.remove(self.lib.root / "batches" / f"{self.pb['batch_id']}.json")   # a batch ingested before step 5
        legacy = plan_search(spec, self.svc)
        self.assertEqual([s["strategy_id"] for s in legacy.strategies], expected)
        self.assertEqual(legacy.search_hash, plan.search_hash)
        self.assertEqual([c["cell_id"] for c in legacy.cells], [c["cell_id"] for c in plan.cells])


if __name__ == "__main__":
    unittest.main()
