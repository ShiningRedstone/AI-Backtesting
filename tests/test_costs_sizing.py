"""Transaction costs, R accounting and position sizing."""
import math
import unittest

import numpy as np

from edgelab.analytics.metrics import breakeven_cost_multiplier, cost_sensitivity
from edgelab.data.synthetic import generate_bars
from edgelab.data.validation import validate_and_freeze
from edgelab.engine.backtester import run_backtest
from edgelab.engine.costs import CostModel, cost_model_from_config
from edgelab.engine.signals import OrderSpec
from edgelab.engine.sizing import contracts_for_risk
from edgelab.instruments import Instrument, InstrumentError, instrument_from_config
from edgelab.strategies.examples import RandomEntry
from tests.helpers import (CFG, INSTRUMENTS, NQ, NQ_COSTS, UTC247, Scripted, bt_cfg, dataset,
                           flat_bar)


class TestInstruments(unittest.TestCase):
    def test_point_values(self):
        self.assertEqual(INSTRUMENTS["NQ"].point_value, 20)
        self.assertEqual(INSTRUMENTS["ES"].point_value, 50)
        self.assertEqual(INSTRUMENTS["MNQ"].point_value, 2)
        self.assertEqual(INSTRUMENTS["CL"].point_value, 1000)

    def test_inconsistent_metadata_rejected(self):
        with self.assertRaises(InstrumentError):
            instrument_from_config("BAD", {"tick_size": 0.25, "tick_value": 5, "point_value": 50,
                                           "calendar": "CME_EQUITY"})
        with self.assertRaises(InstrumentError):
            Instrument("X", 0.0, 5.0, "CME_EQUITY")

    def test_round_to_tick(self):
        self.assertEqual(NQ.round_to_tick(18000.13), 18000.25)
        self.assertEqual(NQ.round_to_tick(18000.12), 18000.0)


class TestCosts(unittest.TestCase):
    def test_round_trip_components(self):
        rt = NQ_COSTS.round_trip_base("market", "stop", 1, NQ)
        self.assertAlmostEqual(rt["commission_usd"], 3.0)
        self.assertAlmostEqual(rt["fees_usd"], 1.4)
        self.assertAlmostEqual(rt["slippage_usd"], 10.0)     # 1 tick in + 1 tick out, $5/tick
        rt2 = NQ_COSTS.round_trip_base("limit", "limit", 3, NQ)
        self.assertAlmostEqual(rt2["slippage_usd"], 0.0)
        self.assertAlmostEqual(rt2["commission_usd"], 9.0)

    def test_trade_level_r_accounting(self):
        # long 1 NQ at 100, stop 20 pts -> 1R = $400; stopped out.
        ds = dataset([flat_bar(100), (100, 101, 79, 80)])
        t = run_backtest(ds, Scripted(OrderSpec("market", stop_points=20, target_points=40), {0: 1}),
                         NQ_COSTS, bt_cfg()).trades.iloc[0]
        self.assertAlmostEqual(t.risk_usd, 400.0)
        self.assertAlmostEqual(t.gross_usd, -400.0)
        self.assertAlmostEqual(t.cost_usd, 14.4)
        self.assertAlmostEqual(t.cost_r, 14.4 / 400)
        self.assertAlmostEqual(t.net_r, -1.0 - 0.036)
        self.assertAlmostEqual(t.entry_price_eff, 100.25)    # bought 1 tick worse
        self.assertAlmostEqual(t.exit_price_eff, 79.75)      # sold 1 tick worse

    def test_tight_stops_pay_more_r(self):
        base = NQ_COSTS.round_trip_base("market", "stop", 1, NQ)
        total = base["commission_usd"] + base["fees_usd"] + base["slippage_usd"]
        cost_r = lambda stop_pts: total / (stop_pts * NQ.point_value)
        self.assertAlmostEqual(cost_r(5), 0.144)              # 14.4% of R per trade on a 5-pt stop
        self.assertAlmostEqual(cost_r(50), 0.0144)            # 1.4% on a 50-pt stop
        self.assertAlmostEqual(cost_r(5) / cost_r(50), 10.0)

    def test_cfd_spread(self):
        # Phase 2 change: the spread now comes from an explicit model, because the Phase 1
        # config value (1.0) was an invented broker number. The spread math is unchanged.
        cfd = INSTRUMENTS["NAS100_CFD"]
        cm = CostModel(spread_points=1.0)
        self.assertAlmostEqual(cm.round_trip_base("market", "market", 1, cfd)["spread_usd"], 1.0)
        from edgelab.engine.costs import CostConfigError
        with self.assertRaises(CostConfigError):
            cost_model_from_config(CFG, "NAS100_CFD")   # no broker numbers configured

    def test_negative_cost_rejected(self):
        with self.assertRaises(ValueError):
            CostModel(commission_per_side=-1)

    def test_sensitivity_is_exact(self):
        """net_R at k x costs computed post hoc == a full re-run at multiplier k."""
        df, _ = generate_bars(UTC247, "2024-01-01", "2024-01-06", seed=11)
        ds = validate_and_freeze(df, NQ, UTC247, "1m", 1, "synthetic", "S")
        strat = RandomEntry(OrderSpec("market", stop_points=8, target_points=12), p=0.02, seed=3)
        base = run_backtest(ds, strat, NQ_COSTS, bt_cfg())
        table = cost_sensitivity(base.trades, (0.5, 2.0, 3.0)).set_index("cost_multiplier")
        for k in (0.5, 2.0, 3.0):
            cm = CostModel(**{**NQ_COSTS.to_dict(), "multiplier": k})
            rerun = run_backtest(ds, strat, cm, bt_cfg())
            self.assertEqual(len(rerun.trades), len(base.trades))
            np.testing.assert_allclose(rerun.trades.entry_bar, base.trades.entry_bar)
            self.assertAlmostEqual(rerun.trades.net_r.sum(), table.loc[k, "net_r"], places=9)
        be = breakeven_cost_multiplier(base.trades)
        if math.isfinite(be) and be > 0:
            r_at_be = base.trades.gross_r - be * base.trades.cost_r_base
            self.assertAlmostEqual(r_at_be.sum(), 0.0, places=9)


class TestSizing(unittest.TestCase):
    def test_floor_not_round(self):
        d = contracts_for_risk(1000, 20, NQ)           # $400/contract -> 2.5 -> 2
        self.assertEqual((d.contracts, d.total_risk_usd), (2, 800))

    def test_stop_too_wide_gives_zero(self):
        d = contracts_for_risk(1000, 60, NQ)           # $1,200/contract > budget
        self.assertEqual(d.contracts, 0)
        self.assertIn("budget", d.reason)

    def test_cap_and_micros(self):
        self.assertEqual(contracts_for_risk(1000, 20, INSTRUMENTS["MNQ"]).contracts, 25)
        self.assertEqual(contracts_for_risk(1000, 20, INSTRUMENTS["MNQ"], max_contracts=10).contracts, 10)

    def test_invalid_inputs(self):
        self.assertEqual(contracts_for_risk(1000, 0, NQ).contracts, 0)
        self.assertEqual(contracts_for_risk(0, 20, NQ).contracts, 0)
        self.assertEqual(contracts_for_risk(1000, float("nan"), NQ).contracts, 0)

    def test_engine_uses_risk_sizing_and_skips_zero(self):
        ds = dataset([flat_bar(100), (100, 101, 79, 80)])
        strat = Scripted(OrderSpec("market", stop_points=20, target_points=40), {0: 1})
        t = run_backtest(ds, strat, NQ_COSTS, bt_cfg(), sizing={"mode": "risk", "risk_usd": 1000}).trades.iloc[0]
        self.assertEqual(t.contracts, 2)
        self.assertAlmostEqual(t.gross_usd, -800.0)
        self.assertAlmostEqual(t.gross_r, -1.0)          # R is size-invariant
        res = run_backtest(ds, strat, NQ_COSTS, bt_cfg(), sizing={"mode": "risk", "risk_usd": 300})
        self.assertEqual(res.skipped, {"SIZE_ZERO": 1})


if __name__ == "__main__":
    unittest.main()
