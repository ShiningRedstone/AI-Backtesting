"""Matched random-entry control (research/controls.py): sessions, causality, matching rule,
seeded reproducibility, identical costs/exits/sizing, frozen candidate, provenance, and that a
control can never be stored as a candidate run. Synthetic data only."""
import contextlib
import copy
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from edgelab.engine.backtester import EXIT_ORDER_TYPE, run_backtest
from edgelab.features.spec import FeatureSpec
from edgelab.features.strategy_api import FeatureContext
from edgelab.research.compare import restrict_to_period
from edgelab.research.controls import RandomEntryControl, realization_seeds, summarize
from edgelab.research.validation import freeze_definition
from edgelab.services import Services
from edgelab.strategy.compiler import compile_strategy
from tests.phase2_helpers import synthetic_canonical, write_generic_utc

REPO = Path(__file__).resolve().parents[1]
EMA = yaml.safe_load((REPO / "strategies" / "fixtures" / "ema_crossover.yaml").read_text())


class TestRandomEntryControl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.root / "configs")
        write_generic_utc(synthetic_canonical("2024-01-02", "2024-03-29", tf=5, seed=5), cls.root / "h.csv")
        cls.svc = Services(root=cls.root)
        cls.did = cls.svc.import_file(dict(file=str(cls.root / "h.csv"), instrument="NAS100_HISTDATA",
                                           provider="HISTDATA", asset_type="CFD", timeframe="5m",
                                           source_timezone="UTC", calendar="CME_EQUITY", price_basis="bid",
                                           build_features=False))["dataset_id"]
        cls.ema_before = copy.deepcopy(EMA)
        cls.r1 = cls.svc.random_entry_control(EMA, cls.did, n_controls=6, seed=1)
        cls.r2 = cls.svc.random_entry_control(EMA, cls.did, n_controls=6, seed=1)
        cls.r3 = cls.svc.random_entry_control(EMA, cls.did, n_controls=6, seed=2)
        cls.ds = cls.svc.load_dataset(cls.did)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def control(self, ds, seed=11, rate=None, p_long=None):
        m = self.r1["control_config"]["matching"]
        ctrl = RandomEntryControl(compile_strategy(EMA, self.svc.sessions).compiled, seed)
        ctrl = ctrl.bind(FeatureContext(ds, self.svc.sessions, self.svc.cache))
        return ctrl.set_design(rate if rate is not None else self.r1["realizations"][0]["signal_rate"],
                               p_long if p_long is not None else m["candidate_long_share"])

    def test_entries_stay_in_the_candidate_session(self):
        ctrl = self.control(self.ds)
        fired = np.flatnonzero(ctrl.generate_signals(self.ds.bars).direction != 0)
        self.assertGreater(len(fired), 50)
        inside = self.svc.sessions["NY_RTH"].membership(self.ds.bars.ts_ns)[0]
        self.assertTrue(inside[fired].all())

    def test_no_future_information(self):
        cut = restrict_to_period(self.ds, self.ds.manifest.start, "2024-02-15")
        full = self.control(self.ds).generate_signals(self.ds.bars)
        part = self.control(cut).generate_signals(cut.bars)
        n = len(cut.bars)
        for k in ("direction", "stop_price", "target_price"):
            self.assertTrue(np.array_equal(np.nan_to_num(getattr(full, k)[:n], nan=-1),
                                           np.nan_to_num(getattr(part, k), nan=-1)), k)
        self.assertTrue(all(r["causality_passed"] for r in self.r1["realizations"]))   # engine truncation check

    def test_matching_rule(self):
        m = self.r1["control_config"]["matching"]
        s = self.r1["candidate"]["pre_cooldown_signals"]                    # ema: cooldown 0 -> same as signals
        self.assertEqual((m["candidate_pre_cooldown_signals"], m["candidate_signals"]), (s, self.r1["candidate"]["signals"]))
        for r in self.r1["realizations"]:
            self.assertAlmostEqual(r["signal_rate"], s / r["eligible_bars"])
        realized = np.mean([r["signals"] for r in self.r1["realizations"]])
        self.assertLess(abs(realized - s), 3 * np.sqrt(s))                  # Bernoulli(p) per eligible bar
        long_share = m["candidate_long_share"]
        sig = self.control(self.ds, rate=1.0).generate_signals(self.ds.bars)
        self.assertAlmostEqual((sig.direction == 1).sum() / (sig.direction != 0).sum(), long_share, delta=0.05)

    def test_seeded_reproducibility(self):
        h1 = [r["trades_hash"] for r in self.r1["realizations"]]
        self.assertEqual(h1, [r["trades_hash"] for r in self.r2["realizations"]])
        self.assertEqual(self.r1["comparison"], self.r2["comparison"])
        self.assertEqual(self.r1["control_config"]["realization_seeds"], realization_seeds(1, 6))
        self.assertNotEqual(h1, [r["trades_hash"] for r in self.r3["realizations"]])
        self.assertNotEqual(self.r1["validation_id"], self.r3["validation_id"])

    def test_costs_exits_and_sizing_identical_to_candidate(self):
        cell = self.svc._run_cell(copy.deepcopy(EMA), self.did, False)
        ctrl = self.control(self.ds)
        res = run_backtest(self.ds, ctrl, cell["costs"], self.svc.cfg["backtest"], sizing=cell["strategy"].sizing)
        t = res.trades
        self.assertGreater(len(t), 20)
        self.assertEqual(res.assumptions["costs"], cell["result"].assumptions["costs"])
        self.assertEqual(res.assumptions["cost_status"], "assumed")
        for row in t.itertuples():                                            # same cost function, same profile
            base = cell["costs"].round_trip_base(row.entry_type, EXIT_ORDER_TYPE[row.exit_reason], row.contracts,
                                                 self.ds.instrument)
            total = base["commission_usd"] + base["fees_usd"] + base["slippage_usd"] + base["spread_usd"]
            self.assertAlmostEqual(row.cost_usd_base, total + row.financing_usd)
        self.assertEqual(set(t["contracts"]), set(cell["result"].trades["contracts"]))   # fixed sizing, 1 unit
        sig = ctrl.generate_signals(self.ds.bars)
        on = sig.direction != 0
        close = np.asarray(self.ds.bars.close, float)
        atr = ctrl._context.engine_for(self.ds.bars).frame(
            [FeatureSpec.make("atr", {"period": 14})]).get_output(FeatureSpec.make("atr", {"period": 14}), "atr")
        d = sig.direction[on].astype(float)
        np.testing.assert_allclose(sig.stop_price[on], close[on] - d * 1.5 * atr[on])            # 1.5 x ATR(14)
        np.testing.assert_allclose(sig.target_price[on], close[on] + d * 2.0 * 1.5 * atr[on])    # 2R

    def test_candidate_frozen_and_provenance(self):
        self.assertEqual(EMA, self.ema_before)
        self.assertEqual(self.r1["candidate"]["definition_hash"], freeze_definition(EMA)[1])
        self.assertEqual(self.r1["candidate"]["strategy_id"], compile_strategy(EMA, self.svc.sessions).strategy_id)
        d = self.r1["dataset"]
        self.assertEqual((d["dataset_id"], d["provider"], d["instrument"]), (self.did, "HISTDATA", "NAS100_HISTDATA"))
        self.assertEqual((self.r1["cost_profile"], self.r1["cost_status"]), ("NAS100_HISTDATA@HISTDATA", "assumed"))
        cfg = self.r1["control_config"]
        self.assertEqual((cfg["method"], cfg["n_controls"], cfg["base_seed"]), ("random_entry_conditional_v2", 6, 1))
        text = " | ".join(self.r1["labels"])
        for phrase in ("HistData NSXUSD CFD BID research proxy", "MNQ-equivalent assumed costs",
                       "not broker-verified", "Random-entry control: a conditional null"):
            self.assertIn(phrase, text)
        self.assertNotIn("p_value", json.dumps(self.r1))
        sub = self.svc.random_entry_control(EMA, self.did, n_controls=2, seed=1,
                                            period=("2024-02-01", "2024-03-29"))
        self.assertEqual(sub["dataset"]["parent_dataset_id"], self.did)       # period lineage

    def test_oos_control_report_is_not_labelled_in_sample(self):
        oos = self.svc.random_entry_control(EMA, self.did, n_controls=2, seed=1, period=("2024-02-15", "2024-03-29"),
                                            sample_status="OUT_OF_SAMPLE")
        self.assertEqual(oos["sample_status"], "OUT_OF_SAMPLE")
        self.assertNotIn("in-sample", oos["labels"][0])
        self.assertTrue(oos["labels"][0].startswith("Historical, out-of-sample, descriptive results"))
        self.assertIn("in-sample", self.r1["labels"][0])                        # default stays IN_SAMPLE
        with self.assertRaises(ValueError):
            self.svc.random_entry_control(EMA, self.did, n_controls=1, sample_status="VALIDATED")

    def test_control_is_never_a_candidate_run(self):
        self.assertEqual(len(self.svc.store.list_runs()), 0)                  # nothing was recorded
        self.assertFalse(self.r1["stored_as_runs"])
        ids = {r["control_strategy_id"] for r in self.r1["realizations"]}
        self.assertTrue(all(i.startswith("CTRL_") for i in ids))
        self.assertNotIn(self.r1["candidate"]["strategy_id"], ids)
        self.assertEqual(len(ids), 6)

    def test_summary_and_refusals(self):
        s = summarize({"net_r": 0.0, "trade_count": 3},
                      [{"net_r": x, "trade_count": 3} for x in (-2.0, -1.0, 1.0, 2.0)])
        self.assertEqual(s["net_r"]["fraction_of_controls_exceeding_candidate"], 0.5)
        self.assertEqual(s["net_r"]["median"], 0.0)
        with self.assertRaises(ValueError):
            self.svc.random_entry_control(EMA, self.did, n_controls=0)
        with self.assertRaises(ValueError):
            RandomEntryControl(compile_strategy(EMA, self.svc.sessions).compiled, 1).set_design(1.5, 0.5)

    def test_cli(self):
        from edgelab.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["--root", str(self.root), "validate", "control", str(REPO / "strategies/fixtures/ema_crossover.yaml"),
                         self.did, "--controls", "6", "--seed", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue())["comparison"], self.r1["comparison"])


MTF = yaml.safe_load((REPO / "strategies" / "fixtures" / "mtf_trend_filter.yaml").read_text())


class TestCooldownCalibration(unittest.TestCase):
    """Regression (audit finding): with a cooldown, p was calibrated to the candidate's signals AFTER
    cooldown while the control applies cooldown AFTER firing, so controls under-fired (~11% on
    mtf_trend_filter). Calibration is now at the pre-cooldown stage on both sides."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.root / "configs")
        write_generic_utc(synthetic_canonical("2024-01-02", "2024-03-29", tf=5, seed=5), cls.root / "h.csv")
        cls.svc = Services(root=cls.root)
        cls.did = cls.svc.import_file(dict(file=str(cls.root / "h.csv"), instrument="NAS100_HISTDATA",
                                           provider="HISTDATA", asset_type="CFD", timeframe="5m",
                                           source_timezone="UTC", calendar="CME_EQUITY", price_basis="bid",
                                           build_features=False))["dataset_id"]
        cls.ds = cls.svc.load_dataset(cls.did)
        cand = compile_strategy(MTF, cls.svc.sessions).bind(FeatureContext(cls.ds, cls.svc.sessions, cls.svc.cache))
        cls.post = int((cand.generate_signals(cls.ds.bars).direction != 0).sum())
        cls.pre = cls.post + cand.last_diagnostics.get("cooldown_suppressed", 0)   # independent path: diagnostics
        cls.rep = cls.svc.random_entry_control(MTF, cls.did, n_controls=20, seed=0)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_candidate_pre_cooldown_count_is_the_calibration_target(self):
        self.assertEqual(MTF["entry"]["cooldown_bars"], 6)
        self.assertGreater(self.pre, self.post)                               # cooldown is active here
        m = self.rep["control_config"]["matching"]
        self.assertEqual((m["candidate_pre_cooldown_signals"], m["candidate_signals"]), (self.pre, self.post))
        for r in self.rep["realizations"]:
            self.assertAlmostEqual(r["signal_rate"], self.pre / r["eligible_bars"])
            self.assertNotAlmostEqual(r["signal_rate"], self.post / r["eligible_bars"])   # the old (wrong) target

    def test_pre_cooldown_fires_are_calibrated_not_reduced(self):
        """Each realization's pre-cooldown fires ~ Binomial(E, p) with mean p*E = pre: the mean over
        realizations must sit within 4 standard errors of pre (and nearer pre than the old target)."""
        reals = self.rep["realizations"]
        fires = np.array([r["pre_cooldown_signals"] for r in reals], float)
        var = sum(self.pre * (1 - r["signal_rate"]) for r in reals)          # sum of binomial variances
        se = np.sqrt(var) / len(reals)
        self.assertLessEqual(abs(fires.mean() - self.pre), 4 * se)
        self.assertLess(abs(fires.mean() - self.pre), abs(fires.mean() - self.post))

    def test_control_applies_the_same_cooldown(self):
        r0 = self.rep["realizations"][0]
        ctrl = RandomEntryControl(compile_strategy(MTF, self.svc.sessions).compiled, r0["seed"])
        ctrl = ctrl.bind(FeatureContext(self.ds, self.svc.sessions, self.svc.cache))
        ctrl.set_design(r0["signal_rate"], self.rep["control_config"]["matching"]["candidate_long_share"])
        fired = np.flatnonzero(ctrl.generate_signals(self.ds.bars).direction != 0)
        self.assertGreater(ctrl.last_diagnostics.get("cooldown_suppressed", 0), 0)
        self.assertGreater(np.diff(fired).min(), 6)                           # no two entries within cooldown
        self.assertEqual(ctrl.last_diagnostics["pre_cooldown_fires"], r0["pre_cooldown_signals"])
        self.assertEqual(len(fired), r0["signals"])

    def test_deterministic_and_causal(self):
        again = self.svc.random_entry_control(MTF, self.did, n_controls=20, seed=0)
        self.assertEqual([r["trades_hash"] for r in again["realizations"]],
                         [r["trades_hash"] for r in self.rep["realizations"]])
        self.assertTrue(all(r["causality_passed"] for r in self.rep["realizations"]))


if __name__ == "__main__":
    unittest.main()
