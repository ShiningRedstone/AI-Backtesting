"""ADR-89: trades per week = trades / weeks of the TESTED data window (not first entry -> last exit, which made one
1-minute trade read as 10,080 per week). A derived number only: trades, P&L and trades_hash are unchanged; stored runs
are corrected when read (nothing re-run), so the explorer, strategy panel and holdout ranking all see the same value."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from edgelab.analytics.metrics import compute_metrics, recorded_trades_per_week, trades_per_week

REPO = Path(__file__).resolve().parents[1]


class TestFormula(unittest.TestCase):
    def test_known_answers(self):
        self.assertEqual(trades_per_week(1, "2024-01-01", "2024-01-29"), 0.25)          # 1 trade in 4 weeks
        self.assertEqual(trades_per_week(14, "2024-01-01T00:00Z", "2024-01-15T00:00Z"), 7.0)
        self.assertEqual(trades_per_week(3, "2024-01-01 09:30", "2024-01-01 09:31"), 21.0)  # windows under a day count as a day
        self.assertIsNone(trades_per_week(1, "2024-01-02", "2024-01-01"))
        self.assertIsNone(trades_per_week(1, None, "2024-01-01"))
        rec = {"dataset": {"start": "2024-01-01 00:00:00+00:00", "end": "2024-01-29 00:00:00+00:00"},
               "headline_metrics": {"trade_count": 1, "trades_per_week": 10080.0}}       # the reported bug
        self.assertEqual(recorded_trades_per_week(rec), 0.25)
        self.assertEqual(recorded_trades_per_week({"headline_metrics": {"trades_per_week": 3.0}}), 3.0)   # no window


class TestStoredRuns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.services import Services
        from edgelab.web.demo import create_demo_workspace
        cls.tmp = Path(tempfile.mkdtemp())
        create_demo_workspace(cls.tmp / "demo", REPO)
        cls.svc = Services(root=cls.tmp / "demo")
        s = cls.svc.library.list()[0]["strategy_id"]
        d = next(x["dataset_id"] for x in cls.svc.backtest_readiness(s)["datasets"] if x["runnable"])
        cls.out = cls.svc.backtest_strategy(s, d, record=True)
        cls.run_id = cls.out["run_id"]

    @classmethod
    def tearDownClass(cls):
        cls.svc.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_new_run_and_read_paths_agree(self):
        from edgelab.research import overview as ov
        svc = self.svc
        rec, trades = svc.store.load_run(self.run_id)
        self.assertGreater(len(trades), 0)
        want = len(trades) / ((pd.Timestamp(rec["dataset"]["end"]) - pd.Timestamp(rec["dataset"]["start"])).total_seconds()
                              / 86400) * 7
        self.assertAlmostEqual(rec["headline_metrics"]["trades_per_week"], want)          # stored on new runs
        self.assertAlmostEqual(ov.run_row(self.run_id, rec)["trades_per_week"], want)     # explorer / panel / holdout
        self.assertAlmostEqual(svc.get_run(self.run_id)["record"]["headline_metrics"]["trades_per_week"], want)
        old = compute_metrics(trades, sample_thresholds=svc.cfg.get("sample_size"))         # historical formula kept
        new = compute_metrics(trades, sample_thresholds=svc.cfg.get("sample_size"),
                              span=(rec["dataset"]["start"], rec["dataset"]["end"]))
        self.assertEqual({k: v for k, v in old.items() if k != "trades_per_week"},
                         {k: v for k, v in new.items() if k != "trades_per_week"}, )       # nothing else moves
        self.assertEqual(rec["trades_hash"], self.out["trades_hash"])

    def test_old_stored_value_is_corrected_on_read(self):
        from edgelab.research import overview as ov
        svc = self.svc
        rec, _ = svc.store.load_run(self.run_id)
        bad = json.loads(json.dumps(rec))
        bad["headline_metrics"]["trades_per_week"] = 10080.0                              # what old runs stored
        self.assertAlmostEqual(ov.run_row(self.run_id, bad)["trades_per_week"], rec["headline_metrics"]["trades_per_week"])


if __name__ == "__main__":
    unittest.main()
