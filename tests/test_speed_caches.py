"""ADR-77 speed caches never change a result: every cached path is compared with the fresh computation it replaces.

- causality-check features of truncated histories (in-memory, keyed on the truncated bars' own content hash):
  same causality report, same trades with the cache off, cold and warm; a leaky feature is still caught warm;
- datasets for backtest / research cells validated once per process: same content and identity as a fresh,
  re-validated load; its discovery window equals a fresh restriction; another store never reuses an entry;
- read views: incremental run records = full re-read; run list without trade loads = the old per-run load;
  favorites from the strategy_id column = the JSON query; vectorized DST offsets = the per-bar loop.
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from edgelab.data.synthetic import generate_bars
from edgelab.data.validation import validate_and_freeze
from edgelab.engine.backtester import run_backtest
from edgelab.engine.signals import OrderSpec, SignalSet, check_causality
from edgelab.features import strategy_api
from edgelab.features.cache import MemoryFeatureCache
from edgelab.features.spec import REGISTRY, FeatureDef, FeatureSpec, register
from edgelab.features.strategy_api import FeatureContext, FeatureStrategy
from edgelab.strategies.examples import TrendBreakoutATR
from tests.helpers import CFG, CME, NQ, ZERO_COSTS
from tests.phase2_helpers import SESSIONS

REPO = Path(__file__).resolve().parents[1]


def _ds(seed=5, ds_id="SPEED"):
    df, _ = generate_bars(CME, "2024-03-01", "2024-03-20", tf_minutes=5, seed=seed, session_gap_sigma=4)
    return validate_and_freeze(df, NQ, CME, "5m", 5, "synthetic", ds_id, volume_type="synthetic")


def _leak(inp, p):
    c = inp.bars.close
    return {"x": np.r_[c[1:], np.nan]}                  # next bar's close: a deliberate lookahead


class Leaky(FeatureStrategy):
    family = "speed_leaky_test"

    def feature_specs(self):
        return [FeatureSpec.make("_speed_leak")]

    def signals_from_features(self, bars, f):
        s = SignalSet.empty(len(bars))
        nxt = f.get_output(self.feature_specs()[0], "x")
        up = np.isfinite(nxt) & (nxt > bars.close)
        s.direction[up] = 1
        s.stop_price[up] = bars.close[up] - 10
        return s


class TestCausalityFeatureCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = _ds()
        if "_speed_leak" not in REGISTRY:
            register(FeatureDef("_speed_leak", 1, "test", (), (("x", "leaky"),), _leak, "TEST ONLY - leaky",
                                "leaky", "leaky", "0"))

    @classmethod
    def tearDownClass(cls):
        REGISTRY.pop("_speed_leak", None)

    def run_once(self, cache):
        with mock.patch.object(strategy_api, "truncation_cache", lambda: cache):
            strat = TrendBreakoutATR(OrderSpec("market"), trend_tf="60m")
            bound = strat.bind(FeatureContext(self.ds, SESSIONS))
            rep = check_causality(bound, self.ds.bars, n_cuts=8)
            res = run_backtest(self.ds, bound, ZERO_COSTS, CFG["backtest"])
        return rep, res

    def test_reports_and_trades_identical_with_cache_off_cold_and_warm(self):
        cache = MemoryFeatureCache(256 * 1024 * 1024)
        off_rep, off_res = self.run_once(None)
        cold_rep, cold_res = self.run_once(cache)
        hits_before = cache.stats["memory_hits"]
        warm_rep, warm_res = self.run_once(cache)
        self.assertGreater(cache.stats["memory_hits"], hits_before)          # the warm run really reused entries
        for rep in (cold_rep, warm_rep):
            self.assertEqual(vars(rep), vars(off_rep))
        for res in (cold_res, warm_res):
            self.assertEqual(res.trades_hash, off_res.trades_hash)
            self.assertEqual(vars(res.causality), vars(off_res.causality))
        self.assertTrue(off_rep.passed)

    def test_leaky_feature_still_caught_with_a_warm_cache(self):
        cache = MemoryFeatureCache(256 * 1024 * 1024)
        self.run_once(cache)                                                   # warm with the same data and cuts
        with mock.patch.object(strategy_api, "truncation_cache", lambda: cache):
            for _ in range(2):                                                 # cold, then warm for the leak too
                rep = check_causality(Leaky(OrderSpec("market")).bind(FeatureContext(self.ds, SESSIONS)),
                                      self.ds.bars, n_cuts=8)
                self.assertFalse(rep.passed)

    def test_memory_cache_is_bounded_by_bytes(self):
        c = MemoryFeatureCache(10_000)
        for i in range(10):
            c.put("D", f"k{i}", {"x": np.zeros(500)}, {})                     # 4,000 bytes each
        self.assertLessEqual(c.bytes, 10_000)
        self.assertEqual(c.get("D", "k9")[1], "memory")
        self.assertIsNone(c.get("D", "k0")[0])                                 # oldest evicted
        c.put("D", "big", {"x": np.zeros(5_000)}, {})                         # larger than the bound: not kept
        self.assertIsNone(c.get("D", "big")[0])


class TestValidationDstOffsets(unittest.TestCase):
    def test_vectorized_offsets_equal_the_per_bar_loop(self):
        import pandas as pd
        ds = _ds()                                                             # spans the 10 March 2024 DST switch
        chk = next(c for c in ds.report.checks if c.name == "dst_transitions")
        idx = pd.DatetimeIndex(ds.bars.ts_ns.astype("datetime64[ns]")).tz_localize("UTC")
        loc = idx.tz_convert(CME.timezone)
        offs = np.array([t.utcoffset().total_seconds() for t in loc[:: max(1, len(loc) // 20000)]])
        n = len(np.unique(offs))
        self.assertEqual((chk.status, chk.count), ("INFO" if n > 1 else "PASS", max(n - 1, 0)))
        self.assertIn(str(sorted(float(x) for x in set(offs / 3600))), chk.message)


class TestServiceCaches(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.services import Services
        from edgelab.web.demo import create_demo_workspace
        cls.tmp = Path(tempfile.mkdtemp())
        create_demo_workspace(cls.tmp / "demo", REPO)
        cls.svc = Services(root=cls.tmp / "demo")
        cls.sid = cls.svc.library.list()[0]["strategy_id"]
        cls.did = next(d["dataset_id"] for d in cls.svc.backtest_readiness(cls.sid)["datasets"] if d["runnable"])

    @classmethod
    def tearDownClass(cls):
        cls.svc.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_cell_dataset_equals_a_fresh_validated_load_and_window(self):
        from edgelab.data.importer import load_validated
        from edgelab.research.compare import restrict_to_period
        fresh = load_validated(self.svc.store, self.svc.cfg, self.did)
        a = self.svc._cell_dataset(self.did)
        self.assertIs(self.svc._cell_dataset(self.did), a)                     # reused, not reloaded
        self.assertEqual(a.manifest.content_hash, fresh.manifest.content_hash)
        self.assertEqual(a.bars.content_hash(), fresh.bars.content_hash())
        self.assertEqual(a.manifest.identity(), fresh.manifest.identity())
        self.assertEqual(a.report.to_dict(), fresh.report.to_dict())
        start, end = str(fresh.manifest.start)[:10], str(fresh.manifest.end)[:10]
        period = (f"{start}T00:00:00Z", f"{end}T00:00:00Z")
        w = self.svc._cell_dataset(self.did, period)
        self.assertIs(self.svc._cell_dataset(self.did, period), w)
        ref = restrict_to_period(fresh, period[0], period[1], self.svc.cfg.get("validation"))
        self.assertEqual(w.manifest.identity(), ref.manifest.identity())
        self.assertEqual(w.bars.content_hash(), ref.bars.content_hash())

    def test_backtest_identical_cold_and_warm(self):
        self.svc.__dict__.pop("_cell_ds", None)
        cold = self.svc.backtest_strategy(self.sid, self.did)
        warm = self.svc.backtest_strategy(self.sid, self.did)
        for k in ("trades_hash", "metrics", "causality", "dataset_id"):
            self.assertEqual(cold.get(k), warm.get(k), k)

    def test_another_store_never_reuses_an_entry(self):
        a = self.svc._cell_dataset(self.did)
        with mock.patch.object(self.svc, "store", self.svc.store.__class__(self.svc.store.path)):
            try:
                b = self.svc._cell_dataset(self.did)
            finally:
                self.svc.store.close()
        self.assertIsNot(a, b)
        self.assertEqual(a.manifest.content_hash, b.manifest.content_hash)

    def test_read_views_equal_their_uncached_versions(self):
        import json
        from edgelab.research import overview
        self.svc.backtest_strategy(self.sid, self.did, record=True)
        before = overview.run_records(self.svc)
        self.svc.backtest_strategy(self.sid, self.did, record=True)                 # incremental: one new run
        inc = overview.run_records(self.svc)
        overview._FACET_CACHE.pop("runs", None)
        full = overview.run_records(self.svc)
        self.assertEqual(inc, full)
        self.assertEqual(len(inc), len(before) + 1)
        old = []
        for r in self.svc.store.list_runs().to_dict("records"):                       # the previous list_runs
            rec, _ = self.svc.store.load_run(r["run_id"])
            ds = rec.get("dataset", {})
            old.append({**r, "strategy_name": rec.get("strategy", {}).get("dsl", {}).get("name"),
                        "notes": rec.get("notes"), "synthetic": str(rec.get("notes", "")).startswith("SYNTHETIC"),
                        "instrument": ds.get("instrument"), "timeframe": ds.get("timeframe"),
                        "headline_metrics": rec.get("headline_metrics", {})})
        from edgelab.services import _jsonable
        self.assertEqual(self.svc.list_runs(), _jsonable(old))
        rows = self.svc.store._query("SELECT DISTINCT json_extract(record_json, '$.strategy.strategy_id') FROM runs")
        self.assertEqual(self.svc.favorites_info()["tested"], sorted(r[0] for r in rows if r[0]))
        names = {r[1] for r in self.svc.store._query("PRAGMA index_list(trades)")}
        self.assertIn("ix_trades_run", names)
        json.dumps(inc, default=str)


if __name__ == "__main__":
    unittest.main()
