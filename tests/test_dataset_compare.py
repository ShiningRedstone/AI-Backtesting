"""Phase 2: dataset comparison keeps datasets separate and results fully attributed."""
import unittest

import numpy as np

from edgelab.data.validation import validate_and_freeze
from edgelab.engine.backtester import run_backtest
from edgelab.engine.costs import CostModel, cost_model_from_config
from edgelab.engine.signals import OrderSpec
from edgelab.features.strategy_api import FeatureContext
from edgelab.research.compare import (common_period, compare_feeds, restrict_to_period,
                                      run_across_datasets)
from edgelab.strategies.examples import TrendBreakoutATR
from tests.helpers import CME, INSTRUMENTS, NQ
from tests.phase2_helpers import CFG, SESSIONS, synthetic_canonical

CFD = INSTRUMENTS["NAS100_CFD"]
TEST_COSTS = CostModel(commission_per_side=1.0, slippage_ticks_market=1, slippage_ticks_stop=1,
                       status="assumed", profile="test-only")


def cfd_costs(ds):
    return CostModel(spread_source="dataset" if ds.bars.spread is not None else "fixed",
                     spread_points=0.0 if ds.bars.spread is not None else 1.0, slippage_unit="points",
                     slippage_ticks_market=0.25, slippage_ticks_stop=0.25, status="assumed", profile="test-only")


class TestCompare(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = synthetic_canonical("2024-01-02", "2024-02-01", tf=5, seed=21)
        cls.fut = validate_and_freeze(base, NQ, CME, "5m", 5, "SYNTH_FUT", "FUT", asset_type="FUTURE",
                                      volume_type="synthetic")
        a = base.copy()
        a[["open", "high", "low", "close"]] -= 12.0
        a["spread"] = 1.2
        cls.cfd_a = validate_and_freeze(a, CFD, CME, "5m", 5, "PROVA", "CFD_A", asset_type="CFD",
                                        volume_type="tick", price_basis="bid")
        rng = np.random.default_rng(5)
        b = base.drop(index=rng.choice(len(base), 200, replace=False)).reset_index(drop=True)
        b[["open", "high", "low", "close"]] -= 11.0
        b["volume"] = np.nan
        cls.cfd_b = validate_and_freeze(b, CFD, CME, "5m", 5, "PROVB", "CFD_B", asset_type="CFD",
                                        volume_type="none", price_basis="mid")
        cls.strat = TrendBreakoutATR(OrderSpec("market"), trend_tf="60m")

        def costs_for(ds):
            return TEST_COSTS if ds.manifest.asset_type == "FUTURE" else cfd_costs(ds)
        cls.costs_for = staticmethod(costs_for)
        cls.cmp = run_across_datasets(cls.strat, [cls.fut, cls.cfd_a, cls.cfd_b], costs_for, CFG, SESSIONS)

    def test_one_independent_run_per_dataset(self):
        self.assertEqual([r.provenance["dataset_id"] for r in self.cmp.runs], ["FUT", "CFD_A", "CFD_B"])
        self.assertEqual(len(self.cmp.table()), 3)                     # no blended / averaged row
        for ds, run in zip((self.fut, self.cfd_a, self.cfd_b), self.cmp.runs):
            solo = run_backtest(ds, self.strat.bind(FeatureContext(ds, SESSIONS)), self.costs_for(ds),
                                CFG["backtest"])
            self.assertIsNone(run.error)
            self.assertEqual(len(run.trades), len(solo.trades))
            np.testing.assert_allclose(run.trades["net_r"].to_numpy(), solo.trades["net_r"].to_numpy())

    def test_results_carry_full_provenance(self):
        for run in self.cmp.runs:
            for k in ("dataset_id", "content_hash", "manifest_hash", "provider", "asset_type", "timeframe",
                      "instrument"):
                self.assertTrue(run.provenance.get(k), k)
            self.assertEqual(run.strategy_id, self.strat.strategy_id)
            self.assertIn("features", run.strategy_spec)
            self.assertEqual(run.cost_model["profile"], "test-only")
            self.assertTrue(run.feature_cache_keys)
            self.assertIn("trade_count", run.metrics)
            self.assertIn("sample_label", run.metrics)
        d = self.cmp.to_dict()
        self.assertTrue(d["config_hash"])
        self.assertIn("source_sha256", d["code_version"])
        keys = [tuple(sorted(r.feature_cache_keys.values())) for r in self.cmp.runs]
        self.assertEqual(len(set(keys)), 3)                            # separate feature caches

    def test_same_strategy_logic_everywhere(self):
        self.assertEqual(len({r.strategy_id for r in self.cmp.runs}), 1)
        # identical path with a constant basis -> identical signals, different costs
        self.assertEqual(len(self.cmp.runs[0].trades), len(self.cmp.runs[1].trades))

    def test_identical_content_is_flagged_and_duplicate_ids_refused(self):
        twin = validate_and_freeze(self.fut.bars.to_frame(), NQ, CME, "5m", 5, "OTHER", "FUT_TWIN",
                                   asset_type="FUTURE", volume_type="synthetic")
        c = run_across_datasets(self.strat, [self.fut, twin], lambda d: TEST_COSTS, CFG, SESSIONS)
        self.assertTrue(any("IDENTICAL" in w for w in c.warnings))
        with self.assertRaises(ValueError):
            run_across_datasets(self.strat, [self.fut, self.fut], lambda d: TEST_COSTS, CFG, SESSIONS)

    def test_failure_on_one_dataset_is_recorded_not_hidden(self):
        def real_config(ds):   # CFD profiles ship unconfigured -> refused for CFD, fine for NQ
            return cost_model_from_config(CFG, ds.instrument.symbol)
        c = run_across_datasets(self.strat, [self.fut, self.cfd_a], real_config, CFG, SESSIONS)
        self.assertIsNone(c.runs[0].error)
        self.assertIn("unconfigured", c.runs[1].error)
        self.assertEqual(len(c.table()), 2)

    def test_compare_feeds(self):
        f = compare_feeds(self.fut, self.cfd_b)
        self.assertEqual(f["only_in_a"], 200)
        self.assertEqual(f["only_in_b"], 0)
        self.assertAlmostEqual(f["mean_close_diff_a_minus_b"], 11.0, places=6)
        self.assertGreater(f["bar_return_correlation"], 0.999)
        self.assertTrue(any("asset types" in n for n in f["notes"]))

    def test_restrict_to_common_period(self):
        s, e = common_period([self.fut, self.cfd_b])
        r = restrict_to_period(self.cfd_a, "2024-01-10", "2024-01-20")
        self.assertEqual(r.manifest.parent_dataset_id, "CFD_A")
        self.assertIn("period restriction", r.manifest.derivation)
        self.assertTrue((r.bars.ts >= "2024-01-10").all() and (r.bars.ts <= "2024-01-20").all())
        self.assertEqual(r.manifest.asset_type, "CFD")
        self.assertIsNotNone(r.bars.spread)
        self.assertLess(s, e)


if __name__ == "__main__":
    unittest.main()
