"""Mathematically known answers on synthetic data (mandatory before trusting real data).

For a driftless random walk with fills exactly at stop/target:
  P(target before stop) = stop / (stop + target)        (gambler's ruin)
  E[gross R]            = 0                             (optional stopping, martingale)
Any bias beyond sampling error means the engine is leaking or distorting fills.
"""
import unittest

import numpy as np
import pandas as pd

from edgelab.analytics.metrics import compute_metrics
from edgelab.data.resample import resample_bars
from edgelab.data.synthetic import generate_bars
from edgelab.data.validation import validate_and_freeze
from edgelab.engine.backtester import run_backtest
from edgelab.engine.signals import OrderSpec
from edgelab.strategies.examples import RandomEntry
from tests.helpers import CME, NQ, NQ_COSTS, ZERO_COSTS, bt_cfg

Z = 4.0   # tolerance in standard errors (keeps false-alarm rate negligible)


class TestRandomWalk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        df, cls.truth = generate_bars(CME, "2024-01-01", "2024-05-01", tf_minutes=1,
                                      sigma_per_bar=3.0, seed=2024)
        cls.ds = validate_and_freeze(df, NQ, CME, "1m", 1, "synthetic", "RW")
        cls.order = OrderSpec("market", stop_points=20, target_points=40)
        cls.strat = RandomEntry(cls.order, p=0.2, seed=99)
        cls.res = run_backtest(cls.ds, cls.strat, ZERO_COSTS, bt_cfg())
        cls.tr = cls.res.trades

    def test_enough_trades(self):
        self.assertGreater(len(self.tr), 1000)

    def test_gamblers_ruin_hit_probability(self):
        bracket = self.tr[self.tr.exit_reason.isin(["STOP", "TARGET"])]
        p_hat = (bracket.exit_reason == "TARGET").mean()
        p = 20 / (20 + 40)
        se = np.sqrt(p * (1 - p) / len(bracket))
        self.assertLess(abs(p_hat - p), Z * se, f"p_hat={p_hat:.4f} expected {p:.4f} (se {se:.4f})")

    def test_zero_gross_expectancy(self):
        r = self.tr.gross_r.to_numpy()
        se = r.std(ddof=1) / np.sqrt(len(r))
        self.assertLess(abs(r.mean()), Z * se, f"mean={r.mean():+.4f}R se={se:.4f}")

    def test_bracket_exits_are_exactly_minus1_or_plus2(self):
        stops = self.tr[self.tr.exit_reason == "STOP"].gross_r
        tgts = self.tr[self.tr.exit_reason == "TARGET"].gross_r
        np.testing.assert_allclose(stops, -1.0)
        np.testing.assert_allclose(tgts, 2.0)

    def test_costs_make_expectancy_negative_by_exactly_cost_r(self):
        res = run_backtest(self.ds, self.strat, NQ_COSTS, bt_cfg())
        np.testing.assert_allclose(res.trades.net_r, res.trades.gross_r - res.trades.cost_r)
        np.testing.assert_allclose(res.trades.gross_r, self.tr.gross_r)   # costs never move fills
        m = compute_metrics(res.trades)
        self.assertLess(m["expectancy_r"], compute_metrics(self.tr)["expectancy_r"])
        self.assertAlmostEqual(m["cost_r"] / m["trade_count"], res.trades.cost_r.mean())

    def test_high_win_rate_is_not_edge(self):
        """Tight target / wide stop: ~80% win rate on a random walk, yet zero gross edge."""
        order = OrderSpec("market", stop_points=40, target_points=10)
        res = run_backtest(self.ds, RandomEntry(order, p=0.2, seed=5), NQ_COSTS, bt_cfg())
        m = compute_metrics(res.trades, r_col="gross_r")
        self.assertGreater(m["win_rate"], 0.7)
        self.assertLess(abs(m["expectancy_r"]), Z * m["expectancy_se"])   # no edge despite 80% wins
        self.assertLess(res.trades.net_r.mean(), m["expectancy_r"])         # and costs push it below


class TestTrend(unittest.TestCase):
    def test_known_drift_rewards_longs_punishes_shorts(self):
        df, _ = generate_bars(CME, "2024-01-08", "2024-01-20", tf_minutes=1, sigma_per_bar=0.5,
                              drift_per_bar=0.25, seed=1)
        ds = validate_and_freeze(df, NQ, CME, "1m", 1, "synthetic", "TREND")
        order = OrderSpec("market", stop_points=10, target_points=10)
        longs = run_backtest(ds, RandomEntry(order, p=0.05, seed=1, side="long"), ZERO_COSTS, bt_cfg())
        shorts = run_backtest(ds, RandomEntry(order, p=0.05, seed=1, side="short"), ZERO_COSTS, bt_cfg())
        self.assertGreater((longs.trades.gross_r > 0).mean(), 0.95)
        self.assertLess((shorts.trades.gross_r > 0).mean(), 0.05)


class TestConflictPolicyOrdering(unittest.TestCase):
    def test_conservative_le_intrabar_le_optimistic(self):
        """Same trades under every policy; only conflict-bar exits differ, in a known order."""
        df1, _ = generate_bars(CME, "2024-01-08", "2024-02-03", tf_minutes=1, sigma_per_bar=3.0, seed=8)
        ltf = validate_and_freeze(df1, NQ, CME, "1m", 1, "synthetic", "L")
        htf = validate_and_freeze(resample_bars(df1, CME, 30), NQ, CME, "30m", 30, "synthetic", "H")
        strat = RandomEntry(OrderSpec("market", stop_points=8, target_points=8), p=0.2, seed=4)
        out = {}
        for pol in ("conservative", "intrabar", "optimistic"):
            out[pol] = run_backtest(htf, strat, ZERO_COSTS, bt_cfg(same_bar_policy=pol), ltf=ltf).trades
        a, b, c = out["conservative"], out["intrabar"], out["optimistic"]
        np.testing.assert_array_equal(a.entry_bar, b.entry_bar)
        np.testing.assert_array_equal(a.entry_bar, c.entry_bar)
        self.assertTrue((a.gross_r.to_numpy() <= b.gross_r.to_numpy() + 1e-12).all())
        self.assertTrue((b.gross_r.to_numpy() <= c.gross_r.to_numpy() + 1e-12).all())
        n_conf = (a.conflict_resolution != "").sum()
        self.assertGreater(n_conf, 10, "test needs real conflicts to be meaningful")
        resolved = (b.conflict_resolution == "INTRABAR").sum()
        self.assertGreater(resolved / n_conf, 0.8)
        # conservative understates, optimistic overstates the intrabar truth
        self.assertLess(a.gross_r.sum(), b.gross_r.sum())
        self.assertLess(b.gross_r.sum(), c.gross_r.sum())


if __name__ == "__main__":
    unittest.main()
