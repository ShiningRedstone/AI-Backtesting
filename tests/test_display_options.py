"""ADR-86 display options: the theme preference (dark / light / system; display only, outside the research config hash)
and Home's total backtested trades (= the sum over every stored run, recomputed from the run records)."""
import shutil
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class TestDisplayOptions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.services import Services
        from edgelab.web.demo import create_demo_workspace
        cls.tmp = Path(tempfile.mkdtemp())
        create_demo_workspace(cls.tmp / "demo", REPO)
        cls.svc = Services(root=cls.tmp / "demo")
        for s in cls.svc.library.list()[:2]:
            d = next(x["dataset_id"] for x in cls.svc.backtest_readiness(s["strategy_id"])["datasets"] if x["runnable"])
            cls.svc.backtest_strategy(s["strategy_id"], d, record=True)

    @classmethod
    def tearDownClass(cls):
        cls.svc.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_theme_preference(self):
        svc = self.svc
        h = svc._config_hash()
        self.assertEqual(svc.ui_preferences()["theme"], "dark")                       # default unchanged
        for t in ("light", "system", "dark"):
            self.assertEqual(svc.set_ui_preferences({"theme": t})["theme"], t)
        with self.assertRaises(ValueError):
            svc.set_ui_preferences({"theme": "pink"})
        self.assertEqual(svc._config_hash(), h)                                         # never part of the research config

    def test_home_total_backtested_trades(self):
        svc = self.svc
        from edgelab.research import overview as ov
        facts = svc.research_overview()["facts"]
        direct = sum(len(svc.store.load_run(r)[1]) for r in svc.store.list_runs()["run_id"])   # stored trades, counted
        self.assertEqual(facts["trades_total"], direct)
        self.assertGreater(facts["trades_total"], 0)
        self.assertEqual(facts["runs"], len(ov.run_records(svc)))


if __name__ == "__main__":
    unittest.main()
