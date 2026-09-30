"""Dukascopy-style cost plumbing (after Phase 9): notional commission and explicit 'financing not
modelled'. Both opt-in; the default per-unit / none behaviour is unchanged. No real cost numbers here:
the rates below are test inputs only."""
import copy
import unittest

from edgelab.engine.costs import CostConfigError, CostModel, cost_model_from_config
from edgelab.instruments import Instrument

INST = Instrument("T", tick_size=0.001, tick_value=0.001, calendar="X", min_size=0.01, size_step=0.01)  # point value 1


class TestNotionalCommission(unittest.TestCase):
    def test_per_million_of_traded_notional_per_side(self):
        cm = CostModel(commission_mode="notional", commission_per_million=10.0)
        b = cm.round_trip_base("market", "market", 2.0, INST, entry_price=15000.0, exit_price=15100.0)
        self.assertAlmostEqual(b["commission_usd"], 10.0 / 1e6 * (15000 + 15100) * 1 * 2)
        # scales with the price level (the reason a fixed per-unit amount cannot represent it)
        hi = cm.round_trip_base("market", "market", 2.0, INST, entry_price=21000.0, exit_price=21000.0)
        self.assertAlmostEqual(hi["commission_usd"] / b["commission_usd"], 42000 / 30100)

    def test_needs_prices_and_default_is_unchanged(self):
        with self.assertRaises(ValueError):
            CostModel(commission_mode="notional", commission_per_million=10.0).round_trip_base(
                "market", "market", 1.0, INST)
        per_unit = CostModel(commission_per_side=0.5)
        self.assertEqual(per_unit.round_trip_base("market", "market", 2.0, INST)["commission_usd"], 2.0)
        self.assertEqual(per_unit.round_trip_base("market", "market", 2.0, INST, entry_price=1.0, exit_price=2.0)
                         ["commission_usd"], 2.0)                           # prices ignored in per_unit mode
        with self.assertRaises(ValueError):
            CostModel(commission_mode="bps")
        with self.assertRaises(ValueError):
            CostModel(commission_mode="notional", commission_per_million=-1.0)


class TestFinancingNotModeled(unittest.TestCase):
    def test_charges_nothing_and_is_distinct_from_none(self):
        day, week = 86_400 * 10**9, 7 * 86_400 * 10**9
        t0 = 1_710_000_000 * 10**9
        nm = CostModel(financing_mode="not_modeled", financing_long_rate=0.05)
        self.assertEqual(nm.rollovers_held(t0, t0 + week), 0)
        self.assertEqual(nm.financing_usd(1, 15000.0, 1.0, INST, t0, t0 + day), 0.0)
        self.assertEqual(nm.to_dict()["financing_mode"], "not_modeled")          # disclosed on every run
        self.assertGreater(CostModel(financing_mode="annual_rate", financing_long_rate=0.05)
                           .financing_usd(1, 15000.0, 1.0, INST, t0, t0 + 3 * day), 0.0)


class TestDukascopyTemplate(unittest.TestCase):
    """The shipped Dukascopy profile has the right shape but NO numbers: it still refuses."""

    def setUp(self):
        from edgelab.core.config import load_config
        self.cfg = load_config("configs")

    def test_template_shape_and_refusal(self):
        prov = self.cfg["costs"]["symbols"]["NQ_DUKASCOPY"]["providers"]["DUKASCOPY"]
        self.assertEqual((prov["commission_mode"], prov["spread_source"], prov["financing_mode"], prov["slippage_unit"]),
                         ("notional", "dataset", "not_modeled", "points"))
        self.assertIsNone(prov["commission_per_million"])
        with self.assertRaises(CostConfigError):
            cost_model_from_config(self.cfg, "NQ_DUKASCOPY", provider="DUKASCOPY")

    def test_agreed_slippage_scenario_is_set_and_commission_is_the_only_gap(self):
        prov = self.cfg["costs"]["symbols"]["NQ_DUKASCOPY"]["providers"]["DUKASCOPY"]
        self.assertEqual(prov["scenario"], "tradovate_average_slippage_proxy_v1")
        self.assertEqual(prov["status"], "assumed")
        self.assertEqual(prov["basis"].strip(),                                     # YAML '>' folds lines with spaces
                         "Central public-evidence assumption of 2 CME NQ ticks (0.50 index points) per market/stop "
                         "execution. Based on Tradovate's statement that slippage is normal and public Tradovate-user "
                         "reports describing roughly 1–2 ticks / a couple ticks. This is not a statistically measured "
                         "Tradovate-wide average and is not broker-verified for Dukascopy USATECH.IDX/USD.")
        self.assertEqual((prov["slippage_ticks_market"], prov["slippage_ticks_stop"]), (0.50, 0.50))
        self.assertIsNone(prov["commission_per_million"])                           # commission still unset
        with self.assertRaises(CostConfigError) as cm:
            cost_model_from_config(self.cfg, "NQ_DUKASCOPY", provider="DUKASCOPY")
        msg = str(cm.exception)
        from edgelab.engine.costs import CostScenarioIncomplete
        self.assertIsInstance(cm.exception, CostScenarioIncomplete)               # still a CostConfigError
        self.assertEqual((cm.exception.scenario, cm.exception.status, cm.exception.missing),
                         ("tradovate_average_slippage_proxy_v1", "assumed", ["commission_per_million"]))
        self.assertIn("set: commission_per_million:", msg)                        # the ONLY missing item
        for other in ("scenario:", "basis:", "slippage_ticks_market:", "slippage_ticks_stop:", "status:",
                      "slippage_unit:", "commission_mode:"):
            self.assertNotIn(other, msg)
        # HistData's profile is untouched
        self.assertEqual(cost_model_from_config(self.cfg, "NAS100_HISTDATA", provider="HISTDATA").status, "assumed")

    def test_notional_profile_requires_the_per_million_rate(self):
        cfg = copy.deepcopy(self.cfg)
        prov = cfg["costs"]["symbols"]["NQ_DUKASCOPY"]["providers"]["DUKASCOPY"]
        prov.update(status="assumed", slippage_ticks_market=0.0, slippage_ticks_stop=0.0)   # test inputs only
        with self.assertRaises(CostConfigError) as cm:
            cost_model_from_config(cfg, "NQ_DUKASCOPY", provider="DUKASCOPY")
        self.assertIn("commission_per_million", str(cm.exception))
        prov["commission_per_million"] = 1.0                                       # test input only
        m = cost_model_from_config(cfg, "NQ_DUKASCOPY", provider="DUKASCOPY")
        self.assertEqual((m.commission_mode, m.spread_source, m.financing_mode, m.commission_per_side),
                         ("notional", "dataset", "not_modeled", 0.0))


class TestNamedCostScenario(unittest.TestCase):
    """A profile declaring `scenario` is usable only as a complete, named, sourced assumption."""

    def setUp(self):
        from edgelab.core.config import load_config
        self.cfg = copy.deepcopy(load_config("configs"))
        self.prov = self.cfg["costs"]["symbols"]["NQ_DUKASCOPY"]["providers"]["DUKASCOPY"]

    def model(self):
        return cost_model_from_config(self.cfg, "NQ_DUKASCOPY", provider="DUKASCOPY")

    def test_each_required_field_is_named_when_missing(self):
        self.prov.update(status="assumed", scenario=None, basis=None, slippage_ticks_market=None,
                         slippage_ticks_stop=None)
        with self.assertRaises(CostConfigError) as cm:
            self.model()
        msg = str(cm.exception)
        for field in ("scenario:", "basis:", "commission_per_million:", "slippage_ticks_market:", "slippage_ticks_stop:"):
            self.assertIn(field, msg)
        self.prov.update(scenario="s1", basis="   ", commission_per_million=1.0, slippage_ticks_market=0.0,
                         slippage_ticks_stop=0.0)                                   # test inputs only
        with self.assertRaises(CostConfigError) as cm:
            self.model()
        self.assertIn("basis:", str(cm.exception))                                 # blank basis refused
        self.prov.update(basis="stated source", slippage_unit="ticks")
        with self.assertRaises(CostConfigError) as cm:
            self.model()
        self.assertIn("slippage_unit: points", str(cm.exception))
        self.prov.update(slippage_unit="points", scenario="bad name!")
        with self.assertRaises(CostConfigError):
            self.model()

    def test_complete_scenario_keeps_status_and_is_recorded(self):
        self.prov.update(scenario="dukascopy_tier_assumption_v1", basis="test inputs only",
                         commission_per_million=1.0, slippage_ticks_market=0.0, slippage_ticks_stop=0.0)
        for status in ("assumed", "broker_verified"):                              # the distinction is preserved
            self.prov["status"] = status
            m = self.model()
            self.assertEqual((m.status, m.scenario, m.basis), (status, "dukascopy_tier_assumption_v1", "test inputs only"))
            d = m.to_dict()                                                         # what every run record stores
            self.assertEqual((d["scenario"], d["basis"], d["status"]), ("dukascopy_tier_assumption_v1",
                                                                         "test inputs only", status))
        self.prov["status"] = "unconfigured"                                       # unconfigured still refuses
        with self.assertRaises(CostConfigError):
            self.model()

    def test_profiles_without_scenario_are_unaffected(self):
        m = cost_model_from_config(self.cfg, "NAS100_HISTDATA", provider="HISTDATA")
        self.assertEqual((m.status, m.scenario, m.basis), ("assumed", "", ""))

    def test_scenario_is_surfaced_on_stored_runs(self):
        import shutil
        import tempfile
        from pathlib import Path
        from edgelab.research.lab import run_summary
        from edgelab.services import Services
        from tests.test_workspace import EMA, make_workspace
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        made = make_workspace(root, runs=False)
        svc = Services(root=root)
        self.addCleanup(svc.store.close)
        svc.cfg["costs"]["symbols"]["NAS100_HISTDATA"]["providers"]["HISTDATA"].update(
            scenario="test_scn", basis="test inputs only", commission_mode="notional", commission_per_million=2.0)
        out = svc.backtest_strategy(EMA, made["dataset_id"], record=True)
        rec, _ = svc.store.load_run(out["run_id"])
        self.assertEqual((rec["assumptions"]["costs"]["scenario"], rec["assumptions"]["costs"]["basis"]),
                         ("test_scn", "test inputs only"))
        row = run_summary({**rec, "run_id": out["run_id"]})
        self.assertEqual((row["cost_scenario"], row["cost_basis"], row["cost_status"]),
                         ("test_scn", "test inputs only", "assumed"))


class TestBacktestUsesTradePrices(unittest.TestCase):
    def test_engine_charges_notional_commission_from_fill_prices(self):
        import shutil
        import tempfile
        from pathlib import Path
        from edgelab.services import Services
        from tests.test_workspace import EMA, make_workspace
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        made = make_workspace(root, runs=False)
        svc = Services(root=root)
        self.addCleanup(svc.store.close)
        prov = svc.cfg["costs"]["symbols"]["NAS100_HISTDATA"]["providers"]["HISTDATA"]
        prov.update(commission_mode="notional", commission_per_million=20.0)             # test input only
        cell = svc._run_cell(EMA, made["dataset_id"])
        t = cell["result"].trades
        self.assertGreater(len(t), 0)
        want = 20.0 / 1e6 * (t["entry_price_theo"].abs() + t["exit_price_theo"].abs()) * t["contracts"]
        self.assertTrue(((t["commission_usd"] - want).abs() < 1e-9).all())
        self.assertEqual(cell["costs"].commission_mode, "notional")


if __name__ == "__main__":
    unittest.main()
