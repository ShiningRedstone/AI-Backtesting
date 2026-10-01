"""ADR-63: whole-contract MNQ execution model. One authoritative contract spec, one contract conversion for every
risk-based sizing mode, never a fractional contract, never rounded up, distinct from the (proxy) data series.
SYNTHETIC data only."""
import copy
import inspect
import math
import unittest
from pathlib import Path

import numpy as np

from edgelab.engine import sizing as SZ
from edgelab.engine.backtester import BacktestError, run_backtest
from edgelab.engine.costs import CostModel
from edgelab.engine.signals import OrderSpec, SignalSet, Strategy
from edgelab.instruments import ExecutionContractError, contract_for, contract_summary, execution_view
from edgelab.strategy import factory as FX
from edgelab.strategy import factory_space as S
from edgelab.strategy.compiler import compile_definition
from edgelab.strategy.dsl import canonical_logic, identity, load_definition, resolve, validate
from tests.helpers import CFG, INSTRUMENTS, NQ, UTC247, ZERO_COSTS, bt_cfg, dataset
from tests.test_capabilities import DSL_BASE

MNQ = INSTRUMENTS["MNQ"]
CFD = INSTRUMENTS["NAS100_CFD"]
DUK = INSTRUMENTS["NQ_DUKASCOPY"]
FLAT = (100, 100, 100, 100)


class One(Strategy):
    """Signals at the given bars: ``stop_pts`` from the signal close, long or short, optional target."""
    family = "contract_seq"

    def __init__(self, bars_dirs, stop_pts=10.0, target_pts=None):
        super().__init__(OrderSpec("market"), sig=str(sorted(bars_dirs.items())), stop=stop_pts, target=target_pts)
        self._d, self._stop, self._tgt = bars_dirs, stop_pts, target_pts

    def generate_signals(self, bars):
        s = SignalSet.empty(len(bars))
        for i, d in self._d.items():
            if i < len(bars):
                s.direction[i] = d
                s.stop_price[i] = bars.close[i] - d * self._stop
                if self._tgt:
                    s.target_price[i] = bars.close[i] + d * self._tgt
        return s


def cfd_ds(rows, ds_id="CT"):
    return dataset(rows, tf=1, cal=UTC247, inst=CFD, ds_id=ds_id)


def run(rows, sizing, direction=1, stop_pts=10.0, costs=ZERO_COSTS, ds=None, contract=MNQ, **kw):
    ds = ds or cfd_ds(rows)
    return run_backtest(ds, One({0: direction}, stop_pts, **kw), costs, bt_cfg(), sizing=sizing, contract=contract)


ROWS = [FLAT] * 6
RISK = lambda usd, **kw: {"mode": "risk", "risk_usd": usd, "contract": "MNQ", **kw}


# ===================================================================================== the specification
class TestSpecification(unittest.TestCase):
    def test_mnq_is_defined_once_in_config_and_is_integer_contracts(self):
        self.assertEqual((MNQ.symbol, MNQ.tick_size, MNQ.tick_value, MNQ.point_value), ("MNQ", 0.25, 0.5, 2.0))
        self.assertEqual((MNQ.min_size, MNQ.size_step, MNQ.exchange), (1.0, 1.0, "CME"))
        text = (Path(__file__).resolve().parents[1] / "configs" / "instruments.yaml").read_text()
        self.assertEqual(text.count("MNQ:"), 1)
        root = Path(__file__).resolve().parents[1] / "edgelab"
        for f in root.rglob("*.py"):
            src = f.read_text()
            for bad in ("point_value = 2", "point_value=2", "tick_value=0.5", "tick_value = 0.5"):
                self.assertNotIn(bad, src, f"{f.name}: the MNQ economics must come from configs/instruments.yaml only")

    def test_factory_declares_only_the_contract_name(self):
        self.assertEqual(S.EXECUTION_CONTRACT, "MNQ")
        for key in S.SIZING:
            blk = FX._sizing(key)
            self.assertEqual(blk["contract"], "MNQ")
            self.assertEqual({"point_value", "tick_size", "tick_value"} & set(blk), set())

    def test_execution_view_keeps_the_data_series_and_takes_contract_economics(self):
        for data in (DUK, CFD, INSTRUMENTS["NAS100_HISTDATA"]):
            v = execution_view(data, MNQ)
            self.assertEqual(v.point_value, 2.0, data.symbol)             # exact
            self.assertEqual((v.min_size, v.size_step, v.symbol), (1.0, 1.0, "MNQ"))
            self.assertEqual((v.tick_size, v.calendar, v.asset_class), (data.tick_size, data.calendar, data.asset_class))
            self.assertEqual(v.extra["data_instrument"], data.symbol)
        info = contract_summary(MNQ, DUK)
        self.assertEqual((info["contract"], info["data_instrument"], info["contract_point_value"]), ("MNQ", "NQ_DUKASCOPY", 2.0))
        self.assertIn("not snapped", info["translation"])
        self.assertNotEqual(DUK.symbol, "MNQ")                              # the dataset is never represented as MNQ
        self.assertEqual(DUK.point_value, 1.0)                              # and its own model is untouched

    def test_mnq_contracts_are_refused_on_a_different_futures_series(self):
        with self.assertRaises(ExecutionContractError):
            execution_view(NQ, MNQ)
        execution_view(MNQ, MNQ)                                            # the MNQ series itself is fine


# ===================================================================================== conversion properties
class TestConversion(unittest.TestCase):
    def test_never_fractional_never_up_and_maximal(self):
        rng = np.random.default_rng(11)
        checked = 0
        for _ in range(4000):
            stop = float(rng.choice([0.25, 0.5, 1, 2.75, 5, 7.3, 10, 12.5, 33.25, 80.125]))
            budget = float(rng.choice([rng.uniform(1, 3000), round(rng.uniform(0.05, 2.0), 2) / 100.0 * rng.uniform(1e4, 2e5)]))
            per = stop * MNQ.point_value
            d = SZ.contracts_for_risk(budget, stop, MNQ)
            n = d.contracts
            self.assertIsInstance(n, int)
            self.assertGreaterEqual(n, 0)
            self.assertLessEqual(n * per, budget + 1e-9, (stop, budget, n))              # never above the budget
            if n:
                self.assertGreater((n + 1) * per, budget - 1e-9, (stop, budget, n))        # and the largest whole count
            else:
                self.assertGreater(per, budget - 1e-9)
            checked += 1
        self.assertEqual(checked, 4000)

    def test_user_example_between_contract_sizes(self):
        d = SZ.contracts_for_risk(119.0, 10.0, MNQ)          # 10-point stop x $2 = $20/contract; 119/20 = 5.95
        self.assertEqual((d.contracts, d.risk_per_contract_usd, d.total_risk_usd), (5, 20.0, 100.0))
        self.assertLessEqual(d.total_risk_usd, 119.0)
        for budget in (119.99, 100.0, 100.000000001, 99.9999999):
            n = SZ.contracts_for_risk(budget, 10.0, MNQ).contracts
            self.assertEqual(n, 5 if budget >= 100.0 else 4, budget)

    def test_float_noise_does_not_round_a_contract_away_or_up(self):
        b = 0.001 * 100_000 * 5                               # noisy products of a percentage and an equity
        self.assertEqual(SZ.contracts_for_risk(0.005 * 100_000, 10.0, MNQ).contracts, 25)       # 500 / 20
        self.assertEqual(SZ.contracts_for_risk(499.99999999999994, 10.0, MNQ).contracts, 25)    # noise only
        self.assertEqual(SZ.contracts_for_risk(499.999, 10.0, MNQ).contracts, 24)               # a real shortfall
        self.assertIsInstance(SZ.contracts_for_risk(b, 10.0, MNQ).contracts, int)

    def test_zero_when_the_budget_cannot_buy_one_contract_and_invalid_inputs(self):
        self.assertEqual(SZ.contracts_for_risk(19.99, 10.0, MNQ).contracts, 0)
        for bad in ((0.0, 10.0), (-5.0, 10.0), (100.0, 0.0), (100.0, -1.0), (math.nan, 10.0), (100.0, math.inf)):
            self.assertEqual(SZ.contracts_for_risk(*bad, MNQ).contracts, 0, bad)

    def test_cap_is_a_hard_deterministic_limit(self):
        d = SZ.contracts_for_risk(10_000.0, 10.0, MNQ, max_contracts=7)
        self.assertEqual((d.contracts, d.total_risk_usd), (7, 140.0))
        self.assertIn("capped", d.reason)
        self.assertEqual(SZ.contracts_for_risk(10_000.0, 10.0, MNQ, max_contracts=7).contracts, 7)   # deterministic
        self.assertEqual(SZ.contracts_for_risk(60.0, 10.0, MNQ, max_contracts=7).contracts, 3)       # cap not binding

    def test_quantity_guard(self):
        self.assertEqual(SZ.check_quantity(3.0, MNQ), 3)
        for bad in (2.5, 0.5, 0.0, -1.0, 3.0000001):
            with self.assertRaises(ValueError, msg=bad):
                SZ.check_quantity(bad, MNQ)
        self.assertEqual(SZ.check_quantity(0.5, CFD), 0.5)                   # CFD units keep their own step (legacy)


class TestOneConversionForEveryRiskMode(unittest.TestCase):
    def test_risk_and_equity_risk_share_the_single_contract_conversion(self):
        calls = []
        real = SZ.contracts_for_risk

        def spy(*a, **k):
            calls.append(a[0])
            return real(*a, **k)
        SZ.contracts_for_risk = spy
        try:
            a = SZ.size_trade({"mode": "risk", "risk_usd": 500.0, "contract": "MNQ"}, 10.0, MNQ)
            b = SZ.size_trade({"mode": "equity_risk", "risk_pct": 0.5, "contract": "MNQ"}, 10.0, MNQ,
                              equity=100_000.0)
        finally:
            SZ.contracts_for_risk = real
        self.assertEqual(calls, [500.0, 500.0])
        self.assertEqual((a.contracts, b.contracts), (25, 25))
        self.assertEqual(a, b)
        src = inspect.getsource(SZ)
        self.assertEqual(src.count("ROUND_FLOOR"), 2)        # the import and the ONE floor, inside contracts_for_risk
        self.assertIn("ROUND_FLOOR", inspect.getsource(real))

    def test_equity_mode_rejects_non_positive_equity_and_needs_the_equity_argument(self):
        self.assertEqual(SZ.size_trade({"mode": "equity_risk", "risk_pct": 1.0}, 10.0, MNQ, equity=0.0).contracts, 0)
        self.assertEqual(SZ.size_trade({"mode": "equity_risk", "risk_pct": 1.0}, 10.0, MNQ, equity=-5.0).contracts, 0)
        with self.assertRaises(ValueError):
            SZ.size_trade({"mode": "equity_risk", "risk_pct": 1.0}, 10.0, MNQ)


# ===================================================================================== through the engine
class TestEngine(unittest.TestCase):
    def test_example_five_point_nine_contracts_executes_exactly_five_long_and_short(self):
        for direction in (1, -1):
            res = run(ROWS, RISK(119.0), direction=direction)
            t = res.trades.iloc[0]
            self.assertEqual(t.contracts, 5, direction)
            self.assertIsInstance(int(t.contracts), int)
            self.assertEqual(float(t.contracts) % 1, 0.0)
            self.assertEqual(t.risk_points, 10.0)
            self.assertEqual(t.risk_usd, 100.0)                      # 5 contracts x 10 points x $2
            self.assertLessEqual(t.risk_usd, 119.0)
            self.assertEqual(t.planned_risk_usd, 100.0)
            self.assertEqual(t.direction, direction)

    def test_fixed_contracts_are_whole_and_pnl_uses_the_contract_point_value(self):
        for direction in (1, -1):
            rows = [FLAT, FLAT, (100, 100 + 10 * direction + (1 if direction > 0 else -1), 100, 100 + 10 * direction), FLAT]
            rows[2] = (100, 111, 99, 110) if direction > 0 else (100, 101, 89, 90)
            res = run(rows, {"mode": "fixed", "contracts": 3, "contract": "MNQ"}, direction=direction, stop_pts=10.0,
                      target_pts=10.0)
            t = res.trades.iloc[0]
            self.assertEqual((t.contracts, t.exit_reason), (3, "TARGET"))
            self.assertAlmostEqual(t.gross_usd, 10.0 * 2.0 * 3)       # 10 points x $2 x 3 contracts
            self.assertAlmostEqual(t.gross_r, 1.0)

    def test_fractional_fixed_quantity_never_reaches_execution(self):
        with self.assertRaises(BacktestError):
            run(ROWS, {"mode": "fixed", "contracts": 2.5, "contract": "MNQ"})
        with self.assertRaises(BacktestError):
            run(ROWS, {"mode": "fixed", "contracts": 0.5, "contract": "MNQ"})
        res = run(ROWS, {"mode": "fixed", "contracts": 2.0, "contract": "MNQ"})
        self.assertEqual(res.trades.iloc[0].contracts, 2)

    def test_zero_contracts_rejects_the_trade(self):
        res = run(ROWS, RISK(19.0))
        self.assertTrue(res.trades.empty)
        self.assertEqual(res.skipped.get("SIZE_ZERO"), 1)
        self.assertEqual(len(run(ROWS, RISK(20.0)).trades), 1)            # exactly one contract's risk is enough

    def test_cap_in_the_engine(self):
        res = run(ROWS, RISK(10_000.0, max_contracts=7))
        self.assertEqual(res.trades.iloc[0].contracts, 7)

    def test_equity_risk_uses_the_same_conversion_with_realised_equity_only(self):
        eq = {"mode": "equity_risk", "risk_pct": 0.5, "contract": "MNQ"}
        res = run(ROWS, eq)                                                      # the default $50,000 research account
        t = res.trades.iloc[0]
        self.assertEqual((t.contracts, t.equity_before, t.risk_usd), (12, 50_000.0, 240.0))      # 250 / ($2 x 10) = 12.5 -> 12
        self.assertEqual(res.trades.iloc[0].contracts, run(ROWS, RISK(250.0)).trades.iloc[0].contracts)
        two = [FLAT, FLAT, (100, 111, 99, 110), FLAT, FLAT, FLAT, FLAT]

        class Two(One):
            pass
        ds = cfd_ds(two + [FLAT] * 3, "CT2")
        r2 = run_backtest(ds, One({0: 1, 4: 1}, 10.0, 10.0), ZERO_COSTS, bt_cfg(), sizing=eq, contract=MNQ)
        self.assertEqual(list(r2.trades.contracts), [12, 12])           # +$240 win: 50,240 x 0.5% = 251.2 -> 12.56 -> 12
        self.assertEqual(r2.trades.equity_before.iloc[1], 50_000.0 + r2.trades.net_usd.iloc[0])
        self.assertTrue(all(float(c) % 1 == 0 for c in r2.trades.contracts))

    def test_refusals_never_fall_back_to_the_data_series(self):
        with self.assertRaises(BacktestError):
            run(ROWS, RISK(500.0), contract=None)                          # names MNQ, spec not supplied
        with self.assertRaises(BacktestError):
            run(ROWS, {"mode": "risk", "risk_usd": 500.0, "contract": "ES"}, contract=MNQ)      # different contract
        with self.assertRaises(BacktestError):
            run(ROWS, RISK(500.0), ds=dataset(ROWS, tf=1, inst=NQ, ds_id="CTN"))               # NQ futures series
        legacy = run(ROWS, {"mode": "risk", "risk_usd": 500.0}, contract=None)                # no contract: legacy CFD units
        self.assertEqual(legacy.trades.iloc[0].contracts, 50.0)                               # 500 / ($1 x 10): CFD unit model
        self.assertNotIn("execution_contract", legacy.assumptions)

    def test_assumptions_disclose_the_translation(self):
        a = run(ROWS, RISK(119.0)).assumptions["execution_contract"]
        self.assertEqual((a["contract"], a["contract_point_value"], a["data_instrument"]), ("MNQ", 2.0, "NAS100_CFD"))
        self.assertIn("configs/instruments.yaml", a["source"])
        self.assertIn("planned", a["sizing_reference"])


# ===================================================================================== quote-aware + R invariance
def quote_ds(ds_id="CQ"):
    from edgelab.data.synthetic import bars_from_ohlc
    from edgelab.data.validation import validate_and_freeze
    bid = [FLAT, (100, 101, 99, 100.5), (100.5, 102, 99.5, 101), (101, 101.5, 100, 100.2), (100.2, 100.8, 99.2, 100.0),
           (100.0, 100.4, 99.0, 99.5), (99.5, 99.9, 98.8, 99.1)]
    df = bars_from_ohlc(bid)
    s = 1.0
    df["spread"] = s
    df["ask_open"], df["ask_high"], df["ask_low"], df["ask_close"] = (df["open"] + s, df["high"] + s + 0.25,
                                                                     df["low"] + s, df["close"] + s)
    return validate_and_freeze(df, CFD, UTC247, "1m", 1, "test", ds_id, asset_type="CFD")


class TestQuotesAndR(unittest.TestCase):
    COSTS = CostModel(spread_source="quotes", slippage_unit="points", slippage_ticks_market=0.5, slippage_ticks_stop=0.5,
                      commission_mode="notional", commission_per_million=30.15, status="assumed")

    def test_directional_execution_is_unchanged_and_r_does_not_depend_on_the_contract(self):
        for direction in (1, -1):
            ds = quote_ds()
            legacy = run_backtest(ds, One({0: direction}, 2.0, 3.0), self.COSTS, bt_cfg(),
                                  sizing={"mode": "fixed", "contracts": 1})
            mnq = run_backtest(ds, One({0: direction}, 2.0, 3.0), self.COSTS, bt_cfg(),
                               sizing={"mode": "fixed", "contracts": 1, "contract": "MNQ"}, contract=MNQ)
            a, b = legacy.trades.iloc[0], mnq.trades.iloc[0]
            for col in ("entry_quote_side", "exit_quote_side", "entry_price_theo", "exit_price_theo", "exit_reason"):
                self.assertEqual(a[col], b[col], (direction, col))
            for col in ("gross_r", "cost_r", "net_r", "risk_points", "mfe_r", "mae_r"):
                self.assertAlmostEqual(a[col], b[col], places=9, msg=(direction, col))
            for col in ("gross_usd", "cost_usd", "risk_usd", "slippage_usd"):
                self.assertAlmostEqual(b[col], 2.0 * a[col], places=9, msg=(direction, col))      # $2 vs $1 per point
            self.assertEqual(b.spread_usd, 0.0)                     # the spread is inside the quote-side fill prices
            self.assertEqual(mnq.assumptions["quote_model"], "directional_bid_ask")
            self.assertEqual(mnq.trades_hash == legacy.trades_hash, False)       # USD differ, prices identical


# ===================================================================================== DSL / identity / factory
class TestDslAndFactory(unittest.TestCase):
    def test_contract_key_validation_and_identity(self):
        base = load_definition(DSL_BASE)
        d = copy.deepcopy(base)
        d["sizing"] = {"mode": "risk", "risk_usd": 500, "max_quantity": 20, "contract": "MNQ"}
        self.assertTrue(validate(d, {}).valid)
        self.assertEqual(compile_definition(d, {}).sizing,
                         {"mode": "risk", "risk_usd": 500.0, "max_contracts": 20, "contract": "MNQ"})
        for bad, path in (({"mode": "fixed", "quantity": 2.5, "contract": "MNQ"}, "sizing.quantity"),
                          ({"mode": "fixed", "quantity": 1, "contract": "mnq"}, "sizing.contract"),
                          ({"mode": "risk", "risk_usd": 500, "max_quantity": 2.5, "contract": "MNQ"}, "sizing.max_quantity")):
            d["sizing"] = bad
            self.assertIn(path, {i.path for i in validate(d, {}).errors}, bad)
        self.assertNotIn("contract", canonical_logic(resolve(base), {})["sizing"])            # absent stays absent
        with_c = copy.deepcopy(base)
        with_c["sizing"] = {"mode": "fixed", "quantity": 1, "contract": "MNQ"}
        self.assertNotEqual(identity(base, {}).logic_hash, identity(with_c, {}).logic_hash)
        self.assertNotIn("contract", compile_definition(base, {}).sizing)

    def test_every_generated_strategy_trades_whole_mnq_contracts(self):
        res = FX.generate(seed=53, quotas={f.fid: 5 for f in S.FAMILIES})
        for r in res.strategies:
            sz = r["definition"]["sizing"]
            self.assertEqual(sz["contract"], "MNQ", r["strategy_id"])
            if sz["mode"] == "fixed":
                self.assertEqual(float(sz["quantity"]) % 1, 0.0)
            if "max_quantity" in sz:
                self.assertIsInstance(sz["max_quantity"], int)
            self.assertEqual(r["tags"]["execution_contract"], "MNQ")
            self.assertEqual(compile_definition(r["definition"], {}).sizing["contract"], "MNQ")

    def test_services_resolve_the_contract_from_the_single_config(self):
        import shutil
        import tempfile
        from edgelab.services import Services
        root = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(Path(__file__).resolve().parents[1] / "configs", root / "configs")
            svc = Services(root=root)
            self.assertEqual(contract_for(svc.cfg, {"mode": "fixed", "contract": "MNQ"}).point_value, 2.0)
            self.assertIsNone(contract_for(svc.cfg, {"mode": "fixed"}))
            with self.assertRaises(ValueError):                      # ExecutionContractError is an InstrumentError(ValueError)
                contract_for(svc.cfg, {"mode": "fixed", "contract": "XYZ"})
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
