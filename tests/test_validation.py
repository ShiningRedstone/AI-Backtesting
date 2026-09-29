"""Validation layer for FIXED strategies: OOS split, walk-forward windows, frozen definitions,
seeded trade-resampling Monte Carlo, lineage and cost-status propagation. Synthetic data only."""
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from edgelab.analytics.metrics import max_drawdown
from edgelab.research import validation as v
from edgelab.services import Services
from tests.phase2_helpers import synthetic_canonical, write_generic_utc

REPO = Path(__file__).resolve().parents[1]
EMA = yaml.safe_load((REPO / "strategies" / "fixtures" / "ema_crossover.yaml").read_text())
T = pd.Timestamp


class TestWindows(unittest.TestCase):
    def test_oos_split_boundaries(self):
        tr, te = v.oos_windows("2024-01-01", "2024-06-30 23:55", "2024-04-01")
        self.assertEqual(tr.start, T("2024-01-01", tz="UTC"))
        self.assertEqual(tr.end, T("2024-04-01", tz="UTC") - pd.Timedelta(1, "ns"))
        self.assertEqual((te.start, te.end), (T("2024-04-01", tz="UTC"), T("2024-06-30 23:55", tz="UTC")))
        self.assertLess(tr.end, te.start)                                   # no bar can be in both
        for bad in ("2024-01-01", "2023-12-01", "2024-07-01"):
            with self.assertRaises(v.ValidationError):
                v.oos_windows("2024-01-01", "2024-06-30 23:55", bad)

    def test_walk_forward_rolling_and_anchored(self):
        segs = v.walk_forward_windows("2024-01-01", "2024-06-20", 2, 1)
        self.assertEqual([s["test"].start.strftime("%m-%d") for s in segs], ["03-01", "04-01", "05-01", "06-01"])
        self.assertEqual([s["partial"] for s in segs], [False, False, False, True])
        self.assertEqual(segs[-1]["test"].end, T("2024-06-20", tz="UTC"))
        for a, b in zip(segs, segs[1:]):                                   # contiguous, non-overlapping OOS
            self.assertEqual(a["test"].end + pd.Timedelta(1, "ns"), b["test"].start)
        for s in segs:
            self.assertEqual(s["train"].end + pd.Timedelta(1, "ns"), s["test"].start)   # train strictly before
            self.assertEqual(s["train"].start, s["test"].start - pd.DateOffset(months=2))
        anchored = v.walk_forward_windows("2024-01-01", "2024-06-20", 2, 1, anchored=True)
        self.assertTrue(all(s["train"].start == T("2024-01-01", tz="UTC") for s in anchored))
        with self.assertRaises(v.ValidationError):
            v.walk_forward_windows("2024-01-01", "2024-02-15", 2, 1)       # no test window fits
        with self.assertRaises(v.ValidationError):
            v.walk_forward_windows("2024-01-01", "2024-06-20", 0, 1)

    def test_freeze_definition(self):
        frozen, h = v.freeze_definition(EMA)
        self.assertEqual(h, v.freeze_definition(yaml.safe_load(yaml.safe_dump(EMA)))[1])
        c = v.copy_frozen(frozen)
        c["parameters"]["fast"]["value"] = 99
        self.assertEqual(v.freeze_definition(frozen)[1], h)                  # copies cannot alter the frozen doc


class TestMonteCarlo(unittest.TestCase):
    R = np.array([1.0, -0.5, 2.0, -1.0, 0.5, -0.7, 1.2, -1.1])

    def test_seeded_and_reproducible(self):
        a = v.monte_carlo(self.R, 500, seed=7)
        self.assertEqual(a, v.monte_carlo(self.R, 500, seed=7))
        self.assertNotEqual(a["total_r_percentiles"], v.monte_carlo(self.R, 500, seed=8)["total_r_percentiles"])
        self.assertEqual((a["seed"], a["n_sims"], a["n_trades"]), (7, 500, 8))
        self.assertIn("not a market simulation", a["note"])

    def test_bootstrap_percentiles_match_independent_computation(self):
        n_sims, seed = 300, 3
        idx = np.random.default_rng(seed).integers(0, len(self.R), size=(n_sims, len(self.R)))
        samples = self.R[idx]
        tot = samples.sum(axis=1)
        dd = np.array([max_drawdown(row) for row in samples])                 # the Phase 1 definition
        out = v.monte_carlo(self.R, n_sims, seed, "bootstrap")
        self.assertEqual(out["total_r_percentiles"], dict(zip(v.PERCENTILES, np.percentile(tot, v.PERCENTILES).tolist())))
        self.assertEqual(out["max_drawdown_r_percentiles"], dict(zip(v.PERCENTILES, np.percentile(dd, v.PERCENTILES).tolist())))
        self.assertAlmostEqual(out["p_total_r_negative"], float((tot < 0).mean()))
        self.assertAlmostEqual(out["observed_max_drawdown_r"], max_drawdown(self.R))
        self.assertAlmostEqual(out["observed_max_drawdown_rank"], float((dd <= max_drawdown(self.R)).mean()))

    def test_shuffle_keeps_total_and_varies_drawdown(self):
        out = v.monte_carlo(self.R, 400, 1, "shuffle")
        self.assertTrue(all(abs(x - self.R.sum()) < 1e-12 for x in out["total_r_percentiles"].values()))
        self.assertEqual(out["p_total_r_negative"], 0.0)
        self.assertLess(out["max_drawdown_r_percentiles"][5], out["max_drawdown_r_percentiles"][95])

    def test_edge_cases_and_refusals(self):
        self.assertEqual(v.monte_carlo([], 10)["n_trades"], 0)
        with self.assertRaises(v.ValidationError):
            v.monte_carlo(self.R, 10, method="gaussian")
        with self.assertRaises(v.ValidationError):
            v.monte_carlo(self.R, 0)


class TestServiceValidation(unittest.TestCase):
    """Fixed ema_crossover on a synthetic 5m NAS100_HISTDATA dataset with provider HISTDATA, so the
    configured assumed MNQ-equivalent profile applies (CME_EQUITY calendar: the synthetic bars follow it)."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.root / "configs")
        write_generic_utc(synthetic_canonical("2024-01-02", "2024-06-28", tf=5, seed=5), cls.root / "h.csv")
        cls.svc = Services(root=cls.root)
        cls.did = cls.svc.import_file(dict(file=str(cls.root / "h.csv"), instrument="NAS100_HISTDATA",
                                           provider="HISTDATA", asset_type="CFD", timeframe="5m",
                                           source_timezone="UTC", calendar="CME_EQUITY", price_basis="bid",
                                           build_features=False))["dataset_id"]
        cls.oos = cls.svc.evaluate_oos(EMA, cls.did, "2024-04-01", record=True, mc_sims=200, mc_seed=1)
        cls.wf = cls.svc.walk_forward(EMA, cls.did, 2, 1, record=True, mc_sims=200, mc_seed=1)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def _stored(self, window):
        rec, trades = self.svc.store.load_run(window["run_id"])
        return rec, trades

    def test_oos_separation_lineage_and_status(self):
        tr, te = self.oos["windows"]
        self.assertEqual((tr["window"]["role"], te["window"]["role"]), ("train", "oos"))
        self.assertLess(T(tr["window"]["end"]), T(te["window"]["start"]))
        split = T("2024-04-01", tz="UTC")
        for w, status in ((tr, "IN_SAMPLE"), (te, "OUT_OF_SAMPLE")):
            rec, trades = self._stored(w)
            self.assertEqual(rec["status"], status)
            self.assertEqual(rec["dataset"]["parent_dataset_id"], self.did)       # lineage to the stored dataset
            self.assertEqual(w["parent_dataset_id"], self.did)
            self.assertIn(self.oos["validation_id"], rec["notes"])
            self.assertTrue(len(trades) > 0)
            if status == "IN_SAMPLE":
                self.assertTrue((trades["entry_ts"] < split).all() and (trades["exit_ts"] <= split).all())
            else:
                self.assertTrue((trades["entry_ts"] >= split).all())
        self.assertNotEqual(tr["dataset_id"], te["dataset_id"])

    def test_parameters_frozen_across_windows(self):
        ids = {w["strategy_id"] for w in self.oos["windows"]} | {w["strategy_id"] for w in self.wf["windows"]}
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.oos["definition_hash"], v.freeze_definition(EMA)[1])
        self.assertEqual(self.wf["definition_hash"], self.oos["definition_hash"])

    def test_cost_status_and_labels_propagate(self):
        for rep in (self.oos, self.wf):
            self.assertEqual(rep["cost_status"], ["assumed"])
            self.assertEqual({w["cost_profile"] for w in rep["windows"]}, {"NAS100_HISTDATA@HISTDATA"})
            text = " | ".join(rep["labels"])
            for phrase in ("MNQ-equivalent assumed costs", "not broker-verified",
                           "HistData NSXUSD CFD BID research proxy", "Fixed strategy"):
                self.assertIn(phrase, text)
            self.assertTrue(rep["labels"][0].startswith("Historical, in-sample and out-of-sample,"))   # train + test

    def test_walk_forward_segments(self):
        tests = [w for w in self.wf["windows"] if w["window"]["role"] == "test"]
        self.assertEqual(len(tests), 4)                                         # Mar, Apr, May, Jun
        starts = [T(w["window"]["start"]) for w in tests]
        ends = [T(w["window"]["end"]) for w in tests]
        for e, s in zip(ends, starts[1:]):
            self.assertEqual(e + pd.Timedelta(1, "ns"), s)
        for w in self.wf["windows"]:
            self.assertEqual(self._stored(w)[0]["status"], "WALK_FORWARD" if w["window"]["role"] == "test"
                             else "IN_SAMPLE")
        self.assertEqual(self.wf["oos_pooled"]["trade_count"], sum(r["trade_count"] for r in self.wf["oos_segments"]))
        self.assertEqual(self.wf["scheme"], "rolling")

    def test_monte_carlo_in_reports_is_reproducible(self):
        again = self.svc.evaluate_oos(EMA, self.did, "2024-04-01", record=False, mc_sims=200, mc_seed=1)
        self.assertEqual(again["monte_carlo_oos"], self.oos["monte_carlo_oos"])
        self.assertEqual([w["trades_hash"] for w in again["windows"]], [w["trades_hash"] for w in self.oos["windows"]])
        mc = self.oos["monte_carlo_oos"]
        self.assertEqual(set(mc), {"bootstrap", "shuffle"})
        self.assertEqual(mc["bootstrap"]["n_trades"], self.oos["windows"][1]["metrics"]["trade_count"])

    def test_cli(self):
        import contextlib
        import io
        import json
        from edgelab.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["--root", str(self.root), "validate", "oos", str(REPO / "strategies/fixtures/ema_crossover.yaml"),
                         self.did, "--split", "2024-04-01", "--sims", "200", "--seed", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue())["monte_carlo_oos"], self.oos["monte_carlo_oos"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--root", str(self.root), "validate", "oos", "x.yaml", self.did]), 2)


if __name__ == "__main__":
    unittest.main()
