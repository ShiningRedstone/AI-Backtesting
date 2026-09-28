"""Phase 4 step 1: `Services._run_cell` (the single-cell pipeline behind `backtest_strategy`
and, later, batch search) and `Services._dataset_eligibility` (behind `backtest_readiness`).

The extraction must not change behaviour: same engine path, the strategy's OWN sizing, the
same refusals, the same run records and the same readiness reasons."""
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from edgelab.analytics.metrics import compute_metrics
from edgelab.engine.backtester import run_backtest
from edgelab.engine.costs import CostConfigError, cost_model_from_config
from edgelab.features.strategy_api import FeatureContext
from edgelab.services import Services
from edgelab.strategy.compiler import StrategyCompileError, compile_strategy
from tests.phase2_helpers import synthetic_canonical, write_generic_utc

REPO = Path(__file__).resolve().parents[1]
FX = REPO / "strategies" / "fixtures"


def fixture(name: str) -> dict:
    return yaml.safe_load((FX / name).read_text())


class TestRunCell(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.root / "configs")
        df = synthetic_canonical("2024-03-04", "2024-03-22", tf=5, seed=9)
        write_generic_utc(df, cls.root / "fut.csv")
        write_generic_utc(df, cls.root / "cfd.csv")
        cls.svc = Services(root=cls.root)
        cls.fut = cls.svc.import_file(dict(file=str(cls.root / "fut.csv"), instrument="NQ", provider="SYNTHF",
                                           asset_type="FUTURE", timeframe="5m", source_timezone="UTC"))["dataset_id"]
        cls.cfd = cls.svc.import_file(dict(file=str(cls.root / "cfd.csv"), instrument="NAS100_CFD", provider="SYNTHC",
                                           asset_type="CFD", timeframe="5m", source_timezone="UTC"))["dataset_id"]
        write_generic_utc(synthetic_canonical("2024-03-04", "2024-03-22", tf=5, seed=11), cls.root / "syn.csv")
        cls.syn = cls.svc.import_file(dict(file=str(cls.root / "syn.csv"), instrument="NQ", provider="synthetic_demo",
                                           asset_type="FUTURE", timeframe="5m", source_timezone="UTC"))["dataset_id"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def reference(self, raw: dict, dataset_id: str):
        """The pre-extraction `backtest_strategy` pipeline, written out step by step."""
        s = self.svc
        ds = s.load_dataset(dataset_id)
        strat = compile_strategy(raw, s.sessions, s._config_hash())
        costs = cost_model_from_config(s.cfg, ds.instrument.symbol, provider=ds.manifest.provider)
        res = run_backtest(ds, strat.bind(FeatureContext(ds, s.sessions, s.cache)), costs, s.cfg["backtest"],
                           sizing=strat.sizing)
        return res, compute_metrics(res.trades, sample_thresholds=s.cfg.get("sample_size"))

    # ------------------------------------------------------------------ same result
    def test_backtest_strategy_matches_the_reference_pipeline(self):
        for name in ("ema_crossover.yaml", "atr_breakout.yaml", "rsi_threshold.yaml"):
            with self.subTest(name):
                ref, met = self.reference(fixture(name), self.fut)
                out = self.svc.backtest_strategy(fixture(name), self.fut)
                self.assertEqual(out["trades_hash"], ref.trades_hash)
                self.assertEqual(out["n_signals"], ref.n_signals)
                self.assertEqual(out["metrics"]["trade_count"], met["trade_count"])
                self.assertEqual(out["metrics"]["sample_label"], met["sample_label"])
                self.assertIsNone(out["run_id"])
                cell = self.svc._run_cell(fixture(name), self.fut)
                self.assertEqual(cell["result"].trades_hash, ref.trades_hash)
                self.assertTrue(cell["result"].causality.passed)
                self.assertFalse(cell["synthetic"])
                self.assertEqual(out["synthetic"], cell["synthetic"])
                self.assertIsNone(cell["run_id"])

    def test_recorded_run_defaults_are_unchanged(self):
        out = self.svc.backtest_strategy(fixture("ema_crossover.yaml"), self.fut, record=True)
        rec, trades = self.svc.store.load_run(out["run_id"])
        self.assertEqual(rec["status"], "IN_SAMPLE")
        self.assertEqual(rec["trades_hash"], out["trades_hash"])
        self.assertEqual(len(trades), out["metrics"]["trade_count"])
        self.assertIsNone(rec["strategy"]["parent_strategy_id"])
        self.assertIsNone(rec["strategy"]["mutation"])
        self.assertEqual(rec["notes"], "single backtest (Strategy Lab)")
        syn = self.svc.backtest_strategy(fixture("ema_crossover.yaml"), self.syn, record=True)
        self.assertTrue(syn["synthetic"])
        self.assertTrue(self.svc.store.load_run(syn["run_id"])[0]["notes"].startswith("SYNTHETIC"))

    def test_run_cell_lineage_kwargs_and_synthetic_label(self):
        cell = self.svc._run_cell(fixture("ema_crossover.yaml"), self.fut, record=True,
                                  parent_strategy_id="STR_PARENT", mutation="rr=3", notes="search cell")
        rec, _ = self.svc.store.load_run(cell["run_id"])
        self.assertEqual(rec["strategy"]["parent_strategy_id"], "STR_PARENT")
        self.assertEqual(rec["strategy"]["mutation"], "rr=3")
        self.assertEqual(rec["notes"], "search cell")
        syn = self.svc._run_cell(fixture("ema_crossover.yaml"), self.syn, record=True, notes="search cell")
        self.assertTrue(self.svc.store.load_run(syn["run_id"])[0]["notes"].startswith("SYNTHETIC"))  # never dropped

    # ------------------------------------------------------------------ sizing
    def test_strategy_own_sizing_is_honoured(self):
        raw = fixture("ema_crossover.yaml")
        raw["sizing"] = {"mode": "fixed", "quantity": 3}
        cell = self.svc._run_cell(raw, self.fut)
        trades = cell["result"].trades
        self.assertGreater(len(trades), 0)
        self.assertEqual(set(trades["contracts"]), {3})
        one = self.svc.backtest_strategy(fixture("ema_crossover.yaml"), self.fut)
        self.assertNotEqual(one["trades_hash"], cell["result"].trades_hash)
        risk = self.svc._run_cell(fixture("atr_breakout.yaml"), self.fut)     # risk mode, max_quantity 5
        self.assertEqual(risk["strategy"].sizing["mode"], "risk")
        self.assertLessEqual(int(risk["result"].trades["contracts"].max()), 5)
        self.assertEqual(risk["result"].trades_hash, self.reference(fixture("atr_breakout.yaml"), self.fut)[0].trades_hash)

    # ------------------------------------------------------------------ refusals
    def test_refusals_are_unchanged_and_record_nothing(self):
        before = len(self.svc.store.list_runs())
        with self.assertRaises(CostConfigError):                     # CFD costs are never invented
            self.svc.backtest_strategy(fixture("ema_crossover.yaml"), self.cfd, record=True)
        with self.assertRaises(CostConfigError):
            self.svc._run_cell(fixture("ema_crossover.yaml"), self.cfd, record=True)
        wrong_tf = fixture("ema_crossover.yaml")
        wrong_tf["timeframe"] = "15m"
        with self.assertRaises(StrategyCompileError):                # 15m strategy on 5m bars
            self.svc.backtest_strategy(wrong_tf, self.fut, record=True)
        with self.assertRaises(KeyError):
            self.svc.backtest_strategy(fixture("ema_crossover.yaml"), "NO_SUCH_DATASET")
        self.assertEqual(len(self.svc.store.list_runs()), before)

    # ------------------------------------------------------------------ eligibility
    def test_eligibility_helper_matches_backtest_readiness(self):
        rows = self.svc.list_datasets()
        for src, tf in ((None, None), (fixture("ema_crossover.yaml"), 5), ({"timeframe": "15m"}, 15),
                        ({"timeframe": "nonsense"}, None)):
            with self.subTest(src=src):
                rd = self.svc.backtest_readiness(src)
                self.assertEqual(rd["datasets"], [self.svc._dataset_eligibility(d, tf) for d in rows])
        by_id = {r["dataset_id"]: r for r in self.svc.backtest_readiness(fixture("ema_crossover.yaml"))["datasets"]}
        self.assertTrue(by_id[self.fut]["runnable"])
        self.assertEqual(by_id[self.fut]["reasons"], [])
        self.assertFalse(by_id[self.cfd]["runnable"])
        self.assertEqual(by_id[self.cfd]["cost"]["status"], "unconfigured")
        self.assertIn("cost profile is unconfigured", by_id[self.cfd]["reasons"][0])
        fut15 = self.svc._dataset_eligibility(next(d for d in rows if d["dataset_id"] == self.fut), 15)
        self.assertFalse(fut15["runnable"])
        self.assertEqual(fut15["reasons"], ["timeframe 5m does not match the strategy timeframe 15m"])


if __name__ == "__main__":
    unittest.main()
