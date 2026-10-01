"""Storage backends, run reproducibility, configuration and structured logging."""
import json
import logging
import tempfile
import unittest
from pathlib import Path

import numpy as np

from edgelab.analytics.metrics import compute_metrics
from edgelab.core.config import ConfigError, config_hash, get_secret, load_config
from edgelab.core.logging import JsonFormatter, STANDARD_FIELDS, get_logger
from edgelab.data.store import DuckDBStore, SQLiteStore
from edgelab.data.synthetic import generate_bars
from edgelab.data.validation import validate_and_freeze
from edgelab.engine.backtester import run_backtest
from edgelab.engine.signals import OrderSpec
from edgelab.research.runs import build_run_record, record_run
from edgelab.strategies.examples import Breakout, RandomEntry
from tests.helpers import CFG, CME, NQ, NQ_COSTS, bt_cfg

try:
    import duckdb  # noqa: F401
    HAS_DUCKDB = True
except ImportError:
    HAS_DUCKDB = False


def small_ds(seed=1):
    df, truth = generate_bars(CME, "2024-01-08", "2024-01-13", tf_minutes=5, seed=seed)
    return validate_and_freeze(df, NQ, CME, "5m", 5, "synthetic", f"SYN_{seed}", source_detail=truth)


class StoreContract:
    """Behaviour every backend must satisfy. Subclasses provide make_store()."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = self.make_store(Path(self.tmp.name))
        self.ds = small_ds()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_dataset_round_trip_verifies_hash(self):
        self.store.save_dataset(self.ds.manifest, self.ds.bars)
        m, bars = self.store.load_dataset(self.ds.manifest.dataset_id, 5)
        self.assertEqual(bars.content_hash(), self.ds.manifest.content_hash)
        self.assertEqual(m.n_bars, len(self.ds.bars))
        self.store.save_dataset(self.ds.manifest, self.ds.bars)   # idempotent

    def test_same_id_different_content_rejected(self):
        self.store.save_dataset(self.ds.manifest, self.ds.bars)
        other = small_ds(seed=2)
        other.manifest.dataset_id = self.ds.manifest.dataset_id
        with self.assertRaises(ValueError):
            self.store.save_dataset(other.manifest, other.bars)

    def test_run_round_trip_and_ids(self):
        strat = Breakout(OrderSpec("market", stop_points=10, target_points=20), lookback=10)
        res = run_backtest(self.ds, strat, NQ_COSTS, bt_cfg())
        m = compute_metrics(res.trades)
        r1 = record_run(self.store, CFG, res, m, seed=0)
        r2 = record_run(self.store, CFG, res, m, seed=0)
        self.assertRegex(r1, r"^RUN_\d{4}_00001$")
        self.assertTrue(r2.endswith("00002"))
        rec, trades = self.store.load_run(r1)
        self.assertEqual(rec["trades_hash"], res.trades_hash)
        self.assertEqual(len(trades), len(res.trades))
        np.testing.assert_allclose(trades.net_r, res.trades.net_r)
        self.assertEqual(str(trades.entry_ts.dt.tz), "UTC")
        self.assertEqual(list(self.store.list_runs().run_id), [r1, r2])


class TestSQLiteStore(StoreContract, unittest.TestCase):
    def make_store(self, root):
        return SQLiteStore(root / "t.sqlite")

    def test_tampered_bars_detected(self):
        self.store.save_dataset(self.ds.manifest, self.ds.bars)
        self.store._exec("UPDATE bars SET close = close + 0.25 WHERE rowid = 10")
        with self.assertRaises(ValueError):
            self.store.load_dataset(self.ds.manifest.dataset_id, 5)


@unittest.skipUnless(HAS_DUCKDB, "duckdb not installed (pip install duckdb) - backend untested here")
class TestDuckDBStore(StoreContract, unittest.TestCase):
    def make_store(self, root):
        return DuckDBStore(root / "t.duckdb", root / "pq")


class TestOpenStoreBackend(unittest.TestCase):
    """ADR-71: `auto` is the SQLite store even when duckdb is installed (searches are SQLite-only)."""
    def test_auto_is_sqlite_even_with_duckdb_installed(self):
        import sys
        import types
        from unittest import mock
        from edgelab.data.store import open_store
        root = Path(tempfile.mkdtemp())
        cfg = {"storage": {"backend": "auto", "duckdb_path": "x.duckdb", "parquet_dir": "pq", "sqlite_path": "x.sqlite"}}
        with mock.patch.dict(sys.modules, {"duckdb": types.ModuleType("duckdb")}):    # "installed"
            store = open_store(cfg, root)
        self.assertEqual(store.backend, "sqlite")
        self.assertFalse((root / "x.duckdb").exists())
        store.close()


class TestReproducibility(unittest.TestCase):
    def test_identical_inputs_identical_trades(self):
        ds = small_ds()
        strat = RandomEntry(OrderSpec("market", stop_points=10, target_points=15), p=0.1, seed=7)
        a = run_backtest(ds, strat, NQ_COSTS, CFG["backtest"])
        b = run_backtest(small_ds(), RandomEntry(OrderSpec("market", stop_points=10, target_points=15),
                                                 p=0.1, seed=7), NQ_COSTS, CFG["backtest"])
        self.assertEqual(a.trades_hash, b.trades_hash)
        self.assertEqual(a.strategy_id, b.strategy_id)
        c = run_backtest(ds, RandomEntry(OrderSpec("market", stop_points=10, target_points=15),
                                         p=0.1, seed=8), NQ_COSTS, CFG["backtest"])
        self.assertNotEqual(a.trades_hash, c.trades_hash)
        self.assertNotEqual(a.strategy_id, c.strategy_id)   # ID encodes the configuration

    def test_run_record_contents(self):
        ds = small_ds()
        res = run_backtest(ds, Breakout(OrderSpec("market", stop_points=10, target_points=20)),
                           NQ_COSTS, CFG["backtest"])
        rec = build_run_record(CFG, res, compute_metrics(res.trades), seed=42,
                               parent_strategy_id="BREAKOUT_x", mutation="lookback 20")
        for key in ("strategy", "dataset", "config", "config_hash", "code_version", "environment",
                    "seed", "assumptions", "trades_hash", "causality_check", "disclaimer"):
            self.assertIn(key, rec)
        self.assertEqual(rec["dataset"]["content_hash"], ds.manifest.content_hash)
        self.assertEqual(rec["dataset"]["provider"], "synthetic")
        self.assertEqual(rec["status"], "IN_SAMPLE")
        self.assertEqual(rec["assumptions"]["costs"]["commission_per_side"], 1.5)
        self.assertTrue(rec["causality_check"]["passed"])
        self.assertEqual(rec["strategy"]["parent_strategy_id"], "BREAKOUT_x")
        self.assertIn("source_sha256", rec["code_version"])
        json.dumps(rec, default=str)   # serialisable
        with self.assertRaises(ValueError):
            build_run_record(CFG, res, {}, status="VALIDATED")   # not a legal status


class TestConfig(unittest.TestCase):
    def test_hash_stable_and_env_override(self):
        a, b = load_config(environ={}), load_config(environ={})
        self.assertEqual(config_hash(a), config_hash(b))
        c = load_config(environ={"EDGELAB__BACKTEST__SAME_BAR_POLICY": "optimistic",
                                 "EDGELAB__BACKTEST__COST_MULTIPLIER": "2"})
        self.assertEqual(c["backtest"]["same_bar_policy"], "optimistic")
        self.assertEqual(c["backtest"]["cost_multiplier"], 2)
        self.assertNotEqual(config_hash(a), config_hash(c))

    def test_invalid_values_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(environ={"EDGELAB__BACKTEST__SAME_BAR_POLICY": "whatever"})
        with self.assertRaises(ConfigError):
            load_config(environ={"EDGELAB__BACKTEST__COST_MULTIPLIER": "-1"})
        with self.assertRaises(ConfigError):
            load_config(config_dir="/nonexistent")

    def test_secrets_only_from_env(self):
        with self.assertRaises(ConfigError):
            get_secret("EDGELAB_TEST_SECRET_THAT_DOES_NOT_EXIST")


class TestLogging(unittest.TestCase):
    def test_json_record_has_standard_fields(self):
        rec = logging.LogRecord("edgelab.backtester", logging.WARNING, __file__, 1, "x", None, None)
        rec.fields = {"component": "backtester", "event": "fill_rejected", "strategy": "S1",
                      "error": "stop beyond fill", "contracts": 2}
        out = json.loads(JsonFormatter().format(rec))
        for f in ("timestamp", "severity") + STANDARD_FIELDS:
            self.assertIn(f, out)
        self.assertEqual((out["severity"], out["event"], out["contracts"]), ("WARNING", "fill_rejected", 2))
        self.assertIsNone(out["account"])

    def test_event_logger(self):
        with self.assertLogs("edgelab.test", level="INFO") as cm:
            get_logger("test").event("hello", run_id="RUN_1")
        self.assertEqual(cm.records[0].fields["run_id"], "RUN_1")


if __name__ == "__main__":
    unittest.main()
