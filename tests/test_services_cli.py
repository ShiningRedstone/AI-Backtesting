"""Phase 2: GUI-ready service contracts (JSON-clean) and the CLI."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from edgelab.cli import main
from edgelab.features.docs import render_markdown
from edgelab.features.spec import all_defs
from edgelab.services import Services
from tests.phase2_helpers import synthetic_canonical, write_generic_utc, write_mt5_like

REPO = Path(__file__).resolve().parents[1]


def strict_json(obj):
    return json.dumps(obj, allow_nan=False, default=None)   # fails on NaN/inf/numpy leftovers


class TestServices(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.root / "configs")
        df = synthetic_canonical("2024-03-04", "2024-03-08", tf=1, seed=4)
        write_mt5_like(df, cls.root / "a.csv")
        write_generic_utc(df, cls.root / "b.csv", with_volume=False)
        cls.svc = Services(root=cls.root)
        cls.a = cls.svc.import_file(dict(file=str(cls.root / "a.csv"), instrument="NAS100_CFD",
                                         provider="SYNTHA", asset_type="CFD", timeframe="1m",
                                         profile="mt5_export", source_timezone="America/New_York+7h",
                                         spread_multiplier=0.01, price_basis="bid", derive_timeframes=["5m"]))
        cls.b = cls.svc.import_file(dict(file=str(cls.root / "b.csv"), instrument="NAS100_CFD",
                                         provider="SYNTHB", asset_type="CFD", timeframe="1m",
                                         source_timezone="UTC"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_every_contract_is_strict_json(self):
        a, b = self.a["dataset_id"], self.b["dataset_id"]
        for payload in (self.a, self.svc.list_datasets(), self.svc.dataset_detail(a),
                        self.svc.feature_catalog(), self.svc.feature_detail("fvg"),
                        self.svc.feature_cache_status(a), self.svc.research_config_options(),
                        self.svc.compare_feeds(a, b),
                        self.svc.feature_values(a, {"id": "ema", "params": {"period": 20}}, 100, 50),
                        self.svc.build_features(b)):
            strict_json(payload)

    def test_data_center(self):
        rows = {d["dataset_id"]: d for d in self.svc.list_datasets()}
        self.assertEqual(len(rows), 3)                                      # 1m A, derived 5m A, 1m B
        self.assertEqual(rows[self.a["dataset_id"]]["volume_type"], "tick")
        det = self.svc.dataset_detail(self.b["dataset_id"])
        self.assertIn("validation_report", det)
        self.assertTrue(any("no volume" in x for x in det["limitations"]))
        self.assertTrue(any("no spread" in x for x in det["limitations"]))
        self.assertEqual(self.svc.dataset_detail(self.a["dataset_id"])["derived_datasets"], self.a["derived"])

    def test_feature_lab(self):
        ids = {d["id"] for d in self.svc.feature_catalog()}
        self.assertEqual(ids, {d.feature_id for d in all_defs()})
        st = self.svc.feature_cache_status(self.a["dataset_id"])
        self.assertGreater(st["entries"], 10)
        v = self.svc.feature_values(self.b["dataset_id"], {"id": "vwap"})
        self.assertFalse(v["available"])
        self.assertIn("volume", v["reason"])
        v = self.svc.feature_values(self.a["dataset_id"], {"id": "atr", "timeframe": "15m"}, 0, 30)
        self.assertEqual(len(v["values"]["atr"]), 30)
        self.assertIn("completed", v["known_at"])

    def test_research_config_options(self):
        o = self.svc.research_config_options()
        for k in ("instruments", "datasets", "sessions", "features", "calendars"):
            self.assertTrue(o[k], k)
        self.assertIn("NY_0900_1000", o["sessions"])
        self.assertIsInstance(o["strategies"], list)                      # Phase 4: stored strategies, no placeholder
        self.assertNotIn("NOT IMPLEMENTED", json.dumps(o))
        s = o["search"]                                                   # Phase 4 search spec options
        self.assertEqual(s["keys"], ["datasets", "max_cells", "period", "ranking", "search_spec_version", "seed",
                                     "strategies", "workers"])
        self.assertEqual(s["strategy_sources"], {"families": None, "ids": "STR_", "proposal_batches": "PB_",
                                                 "variation_batches": "VB_"})
        self.assertEqual(s["ranking_metrics"], ["expectancy_r", "profit_factor", "net_r"])
        self.assertNotIn("win_rate", s["ranking_metrics"])
        self.assertEqual(s["refused_ranking_metrics"], ["win_rate"])
        self.assertEqual(s["defaults"]["ranking"], {"metric": "expectancy_r", "min_sample_label": "MODERATE SAMPLE"})
        self.assertEqual(s["not_in_search_hash"], ["ranking", "max_cells", "workers"])
        saved = self.svc.save_strategy(str(REPO / "strategies" / "fixtures" / "ema_crossover.yaml"))
        o2 = self.svc.research_config_options()
        self.assertIn(saved["strategy_id"], [r["strategy_id"] for r in o2["strategies"]])
        self.assertIn("ema_crossover", o2["search"]["families"])

    def test_search_example_config_is_valid_and_outside_the_config_hash(self):
        from edgelab.core.config import config_hash, load_config
        from edgelab.research.search import canonical_search_spec, validate_search_spec
        from edgelab.strategy.dsl import load_definition
        example = REPO / "configs" / "search.example.yaml"
        raw = load_definition(str(example))
        self.assertTrue(validate_search_spec(raw).valid)
        canon = canonical_search_spec(raw)
        self.assertEqual(canon["ranking"]["metric"], "expectancy_r")
        for k in ("datasets", "max_cells", "seed", "workers"):
            self.assertIn(k, raw, k)
        self.assertTrue(any(canon["strategies"].values()))
        self.assertEqual(self.svc.validate_search(str(example))["valid"], True)
        without = Path(tempfile.mkdtemp())
        try:                                                                # not read by load_config
            shutil.copytree(REPO / "configs", without / "configs", ignore=shutil.ignore_patterns("search.example.yaml"))
            self.assertEqual(config_hash(load_config(without / "configs")), config_hash(load_config(REPO / "configs")))
        finally:
            shutil.rmtree(without, ignore_errors=True)
        text = example.read_text().lower()
        for word in ("profitable", "validated strategy", "guaranteed"):
            self.assertNotIn(word, text)


class TestCLI(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_commands(self):
        root = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(REPO / "configs", root / "configs")
            write_mt5_like(synthetic_canonical("2024-03-04", "2024-03-06", tf=1), root / "x.csv")
            base = ["--root", str(root)]
            code, out, _ = self.run_cli(*base, "import", str(root / "x.csv"), "--profile", "mt5_export",
                                        "--source-timezone", "America/New_York+7h", "--spread-multiplier",
                                        "0.01", "--instrument", "NAS100_CFD", "--provider", "P",
                                        "--asset-type", "CFD", "--no-features")
            self.assertEqual(code, 0, out)
            self.assertIn("NAS100_CFD_P_1M_", out)
            code, out, _ = self.run_cli(*base, "--json", "datasets")
            self.assertEqual(code, 0)
            self.assertEqual(len(json.loads(out)), 1)
            code, _, err = self.run_cli(*base, "import", str(root / "x.csv"), "--profile", "mt5_export",
                                        "--spread-multiplier", "0.01", "--instrument", "NAS100_CFD",
                                        "--provider", "P", "--asset-type", "CFD")
            self.assertEqual(code, 2)
            self.assertIn("source_timezone", err)
            for cmd in (["sessions"], ["features", "list"]):
                self.assertEqual(self.run_cli(*base, *cmd)[0], 0)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_features_md_is_generated_from_registry(self):
        doc = REPO / "FEATURES.md"
        self.assertTrue(doc.exists(), "run: python -m edgelab.cli features docs > FEATURES.md")
        self.assertEqual(doc.read_text(), render_markdown(),
                         "FEATURES.md is stale: regenerate with python -m edgelab.cli features docs")
        for d in all_defs():
            self.assertIn(f"## `{d.feature_id}`", doc.read_text())


if __name__ == "__main__":
    unittest.main()
