"""Phase 2: CFD cost architecture. No broker numbers appear in configs; every number
below is an explicit test assumption."""
import copy
import unittest

import numpy as np
import pandas as pd

from edgelab.data.synthetic import bars_from_ohlc
from edgelab.data.validation import validate_and_freeze
from edgelab.engine.backtester import BacktestError, run_backtest
from edgelab.engine.costs import CostConfigError, CostModel, cost_model_from_config
from edgelab.engine.signals import OrderSpec
from edgelab.engine.sizing import contracts_for_risk
from tests.helpers import CFG, INSTRUMENTS, NQ, UTC247, Scripted, bt_cfg

CFD = INSTRUMENTS["NAS100_CFD"]


def ny(s):
    return pd.Timestamp(s, tz="America/New_York").value


class TestFinancing(unittest.TestCase):
    cm = CostModel(financing_mode="annual_rate", financing_long_rate=0.0365, financing_short_rate=-0.01,
                   financing_day_count=365, triple_rollover_weekday=2)          # Wednesday triple

    def test_rollover_counting(self):
        r = self.cm.rollovers_held
        self.assertEqual(r(ny("2024-06-10 16:00"), ny("2024-06-10 16:30")), 0)   # intraday
        self.assertEqual(r(ny("2024-06-10 16:00"), ny("2024-06-11 10:00")), 1)   # Mon 17:00
        self.assertEqual(r(ny("2024-06-11 16:00"), ny("2024-06-13 10:00")), 4)   # Tue 1 + Wed 3
        self.assertEqual(r(ny("2024-06-14 16:00"), ny("2024-06-17 10:00")), 1)   # Fri; weekend not charged
        fri3 = CostModel(financing_mode="annual_rate", triple_rollover_weekday=4)
        self.assertEqual(fri3.rollovers_held(ny("2024-06-14 16:00"), ny("2024-06-17 10:00")), 3)

    def test_boundaries_and_dst(self):
        r = self.cm.rollovers_held
        self.assertEqual(r(ny("2024-06-10 17:00"), ny("2024-06-10 18:00")), 0)   # opened AT rollover
        self.assertEqual(r(ny("2024-06-10 16:59"), ny("2024-06-10 17:00")), 1)   # held through it
        # Fri Mar 8 17:00 EST (22:00 UTC) and Mon Mar 11 17:00 EDT (21:00 UTC)
        self.assertEqual(r(ny("2024-03-08 16:00"), ny("2024-03-11 17:30")), 2)
        self.assertEqual(CostModel().rollovers_held(ny("2024-06-10 16:00"), ny("2024-06-20 16:00")), 0)

    def test_financing_amount_and_credit(self):
        # notional 18000 x $1/pt x 2 units = 36000; 3.65%/365 = 0.0001 per night -> $3.60 per night
        f = self.cm.financing_usd(1, 18000.0, 2, CFD, ny("2024-06-10 16:00"), ny("2024-06-11 10:00"))
        self.assertAlmostEqual(f, 3.6)
        g = self.cm.financing_usd(-1, 18000.0, 2, CFD, ny("2024-06-10 16:00"), ny("2024-06-11 10:00"))
        self.assertAlmostEqual(g, -36000 * 0.01 / 365)                           # short credit


def spread_ds(spreads, rows=None, inst=CFD, start="2024-01-02 00:00"):
    rows = rows or [(100 + i * 0.1, 100.5 + i * 0.1, 99.5 + i * 0.1, 100 + i * 0.1) for i in range(len(spreads))]
    df = bars_from_ohlc(rows, start=start)
    if spreads is not None:
        df["spread"] = spreads
    return validate_and_freeze(df, inst, UTC247, "1m", 1, "test", "SPR", asset_type="CFD")


class TestSpreadAndRefusals(unittest.TestCase):
    order = OrderSpec("market", stop_points=5.0, time_exit_bars=3)

    def run1(self, ds, costs, sizing=None):
        return run_backtest(ds, Scripted(self.order, {2: 1}), costs, bt_cfg(), sizing=sizing)

    def test_dataset_spread_charged_as_entry_exit_average(self):
        sp = [1.0, 1.0, 2.0, 9.0, 9.0, 4.0, 1.0, 1.0, 1.0, 1.0]
        res = self.run1(spread_ds(sp), CostModel(spread_source="dataset"), {"mode": "fixed", "contracts": 1.5})
        t = res.trades.iloc[0]
        entry_bar = int(t["entry_bar"])
        exit_bar = int(t["exit_bar"])
        expect = 0.5 * (sp[entry_bar] + sp[exit_bar]) * 1.0 * 1.5
        self.assertAlmostEqual(t["spread_usd"], expect)
        self.assertEqual(t["contracts"], 1.5)                                   # fractional CFD units

    def test_refuses_dataset_spread_when_dataset_has_none(self):
        with self.assertRaises(BacktestError):
            self.run1(spread_ds(None, rows=[(100, 101, 99, 100)] * 10), CostModel(spread_source="dataset"))

    def test_refuses_trade_on_bar_without_spread(self):
        sp = [1.0, 1.0, 1.0, np.nan, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]   # bar 3 = entry (next open)
        with self.assertRaises(BacktestError):
            self.run1(spread_ds(sp), CostModel(spread_source="dataset"))

    def test_refuses_unconfigured_model(self):
        with self.assertRaises(BacktestError):
            self.run1(spread_ds([1.0] * 10), CostModel(status="unconfigured"))

    def test_point_slippage(self):
        cm = CostModel(slippage_unit="points", slippage_ticks_market=0.5)
        self.assertAlmostEqual(cm.round_trip_base("market", "market", 2, CFD)["slippage_usd"], 2.0)
        self.assertAlmostEqual(cm.round_trip_base("market", "market", 2, CFD)["slippage_ticks"], 100)
        res = self.run1(spread_ds([1.0] * 10), cm)
        t = res.trades.iloc[0]
        self.assertAlmostEqual(t["entry_price_eff"] - t["entry_price_theo"], 0.5)

    def test_cost_sensitivity_stays_exact_with_financing_and_spread(self):
        rows = [(100 + i * 0.01, 100.5 + i * 0.01, 99.5 + i * 0.01, 100 + i * 0.01) for i in range(3000)]
        ds = spread_ds([1.2] * 3000, rows=rows, start="2024-06-10 20:00")
        order = OrderSpec("market", stop_points=50.0, time_exit_bars=1500)          # held overnight
        base = CostModel(spread_source="dataset", commission_per_side=0.3, slippage_unit="points",
                         slippage_ticks_market=0.2, financing_mode="annual_rate", financing_long_rate=0.05,
                         rollover_timezone="UTC", rollover_time="21:00")
        r1 = run_backtest(ds, Scripted(order, {5: 1}), base, bt_cfg())
        t1 = r1.trades.iloc[0]
        self.assertGreater(t1["financing_usd"], 0)
        import dataclasses
        r2 = run_backtest(ds, Scripted(order, {5: 1}), dataclasses.replace(base, multiplier=2.0), bt_cfg())
        t2 = r2.trades.iloc[0]
        self.assertAlmostEqual(t2["net_r"], t1["gross_r"] - 2 * t1["cost_r_base"], places=10)
        self.assertEqual(r1.assumptions["cost_status"], "assumed")


class TestConfigResolution(unittest.TestCase):
    def test_cfd_profiles_ship_unconfigured(self):
        for sym in ("NAS100_CFD", "US100_CFD", "NQ_CFD"):
            with self.assertRaises(CostConfigError) as cm:
                cost_model_from_config(CFG, sym)
            self.assertIn("unconfigured", str(cm.exception))
        z = cost_model_from_config(CFG, "NAS100_CFD", allow_unconfigured=True)
        self.assertEqual(z.status, "zero_for_testing")
        self.assertEqual(z.round_trip_base("market", "market", 1, CFD)["spread_usd"], 0)

    def test_provider_override(self):
        cfg = copy.deepcopy(CFG)
        cfg["costs"]["symbols"]["NAS100_CFD"] = dict(cfg["costs"]["symbols"]["NAS100_CFD"])
        cfg["costs"]["symbols"]["NAS100_CFD"]["providers"] = {"MYBROKER": {
            "status": "broker_verified", "commission_per_side": 0.0, "slippage_unit": "points",
            "slippage_ticks_market": 0.3, "slippage_ticks_stop": 0.5, "spread_source": "dataset",
            "financing_mode": "annual_rate", "financing_long_rate": 0.07, "triple_rollover_weekday": 4}}
        cm = cost_model_from_config(cfg, "NAS100_CFD", provider="MYBROKER")
        self.assertEqual((cm.status, cm.profile, cm.spread_source, cm.triple_rollover_weekday),
                         ("broker_verified", "NAS100_CFD@MYBROKER", "dataset", 4))
        with self.assertRaises(CostConfigError):
            cost_model_from_config(cfg, "NAS100_CFD", provider="OTHERBROKER")   # other feed stays unconfigured

    def test_fixed_spread_requires_a_number(self):
        cfg = copy.deepcopy(CFG)
        cfg["costs"]["symbols"]["NAS100_CFD"] = {"status": "assumed", "commission_per_side": 0,
                                                 "slippage_ticks_market": 0, "slippage_ticks_stop": 0,
                                                 "spread_points": None}
        with self.assertRaises(CostConfigError):
            cost_model_from_config(cfg, "NAS100_CFD")

    def test_futures_config_unchanged(self):
        cm = cost_model_from_config(CFG, "NQ")
        self.assertEqual(cm.status, "assumed")
        self.assertEqual(cm.slippage_unit, "ticks")


class TestUnitSizing(unittest.TestCase):
    def test_fractional_units_floor_to_step(self):
        d = contracts_for_risk(100.0, 30.0, CFD)          # 100 / 30 = 3.333 -> 3.33 units
        self.assertAlmostEqual(d.contracts, 3.33)
        self.assertLessEqual(d.total_risk_usd, 100.0)

    def test_min_size(self):
        d = contracts_for_risk(0.1, 30.0, CFD)            # 0.0033 units < 0.01 minimum
        self.assertEqual(d.contracts, 0)

    def test_futures_stay_integer(self):
        d = contracts_for_risk(500.0, 10.0, NQ)           # $200 per contract -> 2
        self.assertEqual(d.contracts, 2)
        self.assertIsInstance(d.contracts, int)


if __name__ == "__main__":
    unittest.main()


class TestBidSeriesFillsAndSlippageOnce(unittest.TestCase):
    """SINGLE-SERIES mode (quote_model single_series, spread_source dataset): stops/targets trigger on
    the one stored (BID) OHLC for both sides; spread and 0.50-point market/stop slippage are charged
    once each as costs; a TARGET exit is a limit (limit slippage, 0 here). Directional BID/ASK execution
    (spread_source quotes, ADR-55) is tested in tests/test_directional_quotes.py. Test assumptions only."""
    cm = CostModel(spread_source="dataset", slippage_unit="points", slippage_ticks_market=0.5,
                   slippage_ticks_stop=0.5, slippage_ticks_limit=0.0)
    order = OrderSpec("market", stop_points=2.0, target_points=2.0, time_exit_bars=20)

    def short_trade(self, bar5, bar7):
        rows = [(100.0, 100.5, 99.5, 100.0)] * 12
        rows[5], rows[7] = bar5, bar7
        res = run_backtest(spread_ds([3.0] * 12, rows=rows), Scripted(self.order, {2: -1}), self.cm, bt_cfg(),
                           sizing={"mode": "fixed", "contracts": 1})
        self.assertEqual(len(res.trades), 1)
        self.assertEqual((res.assumptions["quote_model"], res.assumptions["spread_treatment"]),
                         ("single_series", "cost_avg_entry_exit"))
        return res.trades.iloc[0]

    def test_short_stop_is_triggered_by_the_stored_bid_high_not_bid_plus_spread(self):
        # entry at bar 3 open 100 -> stop 102, target 98. Bar 5: BID high 101.5 < 102 although
        # BID + spread (the ask a buy-stop executes against) reaches 104.5 -> NOT stopped (known limitation).
        t = self.short_trade(bar5=(100.0, 101.5, 99.5, 100.0), bar7=(100.0, 100.5, 97.9, 98.5))
        self.assertEqual((t["exit_reason"], t["exit_bar"], t["exit_price_theo"]), ("TARGET", 7, 98.0))
        t = self.short_trade(bar5=(100.0, 102.1, 99.5, 100.0), bar7=(100.0, 100.5, 97.9, 98.5))
        self.assertEqual((t["exit_reason"], t["exit_bar"], t["exit_price_theo"]), ("STOP", 5, 102.0))

    def test_slippage_charged_once_per_fill_and_target_is_a_limit(self):
        target = self.short_trade(bar5=(100.0, 101.5, 99.5, 100.0), bar7=(100.0, 100.5, 97.9, 98.5))
        stop = self.short_trade(bar5=(100.0, 102.1, 99.5, 100.0), bar7=(100.0, 100.5, 97.9, 98.5))
        self.assertAlmostEqual(target["slippage_usd"], 0.5)            # market entry 0.50 + limit exit 0
        self.assertAlmostEqual(stop["slippage_usd"], 1.0)              # market entry 0.50 + stop exit 0.50
        for t in (target, stop):
            self.assertAlmostEqual(t["spread_usd"], 3.0)               # (3 + 3) / 2, once per round trip
            # P&L uses theo prices; slippage appears only as a cost (not also in the price)
            self.assertAlmostEqual(t["gross_usd"], -(t["exit_price_theo"] - t["entry_price_theo"]))
            self.assertAlmostEqual(t["gross_usd"] - t["net_usd"], t["slippage_usd"] + t["spread_usd"])
        self.assertAlmostEqual(stop["entry_price_theo"] - stop["entry_price_eff"], 0.5)   # display only
