"""ADR-90 round: display preferences (USD/CHF, chart grouping, Live 50K limit, fee discounts) stay outside the research
config hash; the "Live 50K OK" check (known answers, the SQL year grouping against pandas); positive / negative net-R
breakdown counts and the drawn total; the drawdown histogram cut at the 99th percentile; the strategy-pool filter; paper
fee discounts frozen at account start; automatic restarts of a failing research run (backoff, problems flag, cancel)."""
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

REPO = Path(__file__).resolve().parents[1]


class DemoBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.services import Services
        from edgelab.web.demo import create_demo_workspace
        cls.tmp = Path(tempfile.mkdtemp())
        create_demo_workspace(cls.tmp / "demo", REPO)
        cls.svc = Services(root=cls.tmp / "demo")
        for s in cls.svc.library.list()[:3]:
            d = next(x["dataset_id"] for x in cls.svc.backtest_readiness(s["strategy_id"])["datasets"] if x["runnable"])
            cls.svc.backtest_strategy(s["strategy_id"], d, record=True)

    @classmethod
    def tearDownClass(cls):
        cls.svc.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestPreferences(DemoBase):
    def test_new_prefs_validated_and_outside_the_config_hash(self):
        svc = self.svc
        h = svc._config_hash()
        p = svc.ui_preferences()
        self.assertEqual((p["currency"], p["chf_per_usd"], p["chart_cluster"], p["chart_cluster_distance"], p["live_dd_limit_usd"]),
                         ("USD", None, True, 1.0, 5000.0))
        self.assertEqual(p["prop_discount"], {"enabled": False, "pct": {}})
        out = svc.set_ui_preferences({"currency": "CHF", "chf_per_usd": 0.88, "chart_cluster": False,
                                      "chart_cluster_distance": 2.5, "live_dd_limit_usd": 3000,
                                      "prop_discount": {"enabled": True, "pct": {"TRADEIFY_GROWTH_50K": 20}}})
        self.assertEqual((out["currency"], out["chf_per_usd"], out["chart_cluster"], out["chart_cluster_distance"],
                          out["live_dd_limit_usd"]), ("CHF", 0.88, False, 2.5, 3000.0))
        self.assertEqual(out["prop_discount"], {"enabled": True, "pct": {"TRADEIFY_GROWTH_50K": 20.0}})
        for bad in ({"currency": "EUR"}, {"chf_per_usd": 0}, {"chf_per_usd": "0.9"}, {"chart_cluster": 1},
                    {"chart_cluster_distance": 5}, {"live_dd_limit_usd": -1}, {"live_dd_limit_usd": True},
                    {"prop_discount": {"enabled": True, "pct": {"NOPE": 10}}},
                    {"prop_discount": {"enabled": True, "pct": {"TRADEIFY_GROWTH_50K": 120}}},
                    {"prop_discount": {"on": True}}):
            with self.assertRaises(ValueError, msg=bad):
                svc.set_ui_preferences(bad)
        self.assertEqual(svc._config_hash(), h)                                   # never part of the research config
        svc.set_ui_preferences({"currency": "USD", "chf_per_usd": None, "chart_cluster": True, "chart_cluster_distance": 1.0,
                                "live_dd_limit_usd": 5000, "prop_discount": {"enabled": False, "pct": {}}})

    def test_discounted_fees_eval_and_reset_only(self):
        svc = self.svc
        fees = {"eval_price": 100.0, "reset_fee": 50.0, "activation_fee": 30.0}
        self.assertEqual(svc.discounted_fees("TRADEIFY_GROWTH_50K", fees), (fees, None))          # switch off
        svc.set_ui_preferences({"prop_discount": {"enabled": True, "pct": {"TRADEIFY_GROWTH_50K": 25}}})
        try:
            got, rec = svc.discounted_fees("TRADEIFY_GROWTH_50K", fees)
            self.assertEqual(got, {"eval_price": 75.0, "reset_fee": 37.5, "activation_fee": 30.0})
            self.assertEqual((rec["pct"], rec["before"]), (25.0, fees))
            self.assertEqual(svc.discounted_fees("LUCID_LUCIDFLEX_50K", fees), (fees, None))      # no % for this account
        finally:
            svc.set_ui_preferences({"prop_discount": {"enabled": False, "pct": {}}})


class TestLiveCheck(DemoBase):
    def test_known_answers(self):
        from edgelab.research.overview import live_check
        ref = {"trade_count": 10, "net_usd": 1200.0, "max_drawdown_usd": 900.0}
        ok = live_check(ref, 50.0, 1000.0)
        self.assertTrue(ok["ok"])
        self.assertFalse(live_check(ref, -1.0, 1000.0)["ok"])                       # a losing year
        self.assertFalse(live_check(ref, 50.0, 800.0)["drawdown_within_limit"])
        self.assertFalse(live_check({**ref, "net_usd": -5.0}, 50.0, 1000.0)["net_positive"])
        self.assertIsNone(live_check({"trade_count": 0}, None, 1000.0))
        self.assertTrue(live_check({**ref, "max_drawdown_usd": 1000.0}, 0.0, 1000.0)["ok"])   # limits inclusive

    def test_worst_year_sql_matches_pandas(self):
        from edgelab.research import overview as ov
        years = ov.worst_year_usd(self.svc)
        self.assertTrue(years)
        for rid in self.svc.store.list_runs()["run_id"]:
            _, t = self.svc.store.load_run(rid)
            if not len(t):
                continue
            local = pd.DatetimeIndex(pd.to_datetime(t["exit_ts"], utc=True)).tz_convert("America/New_York")
            want = float(pd.Series(t["net_usd"].to_numpy(float)).groupby(local.year).sum().min())
            self.assertAlmostEqual(years[rid], want, places=6)

    def test_overview_explorer_and_panel(self):
        from edgelab.research import overview as ov
        svc = self.svc
        o = svc.results_overview({})
        live = [p for p in o["points"] if p["live_ok"]]
        self.assertEqual(o["facts"]["live_ok"], len(live))
        self.assertEqual(o["facts"]["live_limit_usd"], 5000.0)
        self.assertEqual(o["facts"]["drawn"], sum(1 for p in o["points"] if p["win_rate"] is not None and p["avg_rr"] is not None))
        for groups in o["breakdowns"].values():                                     # positive / negative / zero add up
            for g in groups:
                self.assertEqual(g["positive"] + g["negative"] + g["zero"], g["strategies"])
        ex = svc.explore_strategies({"tested_only": "1"})
        n_live = sum(1 for r in ex["rows"] if r["live_ok"])
        self.assertEqual(svc.explore_strategies({"tested_only": "1", "live_only": "1"})["total"], n_live)
        sid = o["points"][0]["strategy_id"]
        panel = svc.strategy_panel(sid, {})
        self.assertTrue(panel["tested"])
        self.assertLessEqual({"ok", "net_positive", "drawdown_within_limit", "no_losing_year", "worst_year_usd"}, set(panel["live"]))
        self.assertEqual(panel["live"]["ok"], o["points"][0]["live_ok"])
        with mock.patch.object(ov, "live_limit", return_value=0.01):               # a tiny limit: nothing qualifies
            self.assertEqual(svc.results_overview({})["facts"]["live_ok"], 0)


class TestHistogramAndPools(unittest.TestCase):
    def test_drawdown_axis_cut_at_p99(self):
        from edgelab.research.overview import _hist
        h = _hist([1, 2, 3, 4, 5] * 40 + [40000], 24, 0.0, hi_pct=99)
        self.assertEqual((h["edges"][-1], h["clipped"], sum(h["counts"]), h["n"]), (5.0, 1, 201, 201))
        self.assertAlmostEqual(h["mean"], (15 * 40 + 40000) / 201)                  # mean / median over every run
        self.assertEqual(h["median"], 3.0)

    def test_pool_reference(self):
        from edgelab.research import overview as ov
        with mock.patch("edgelab.research.pool2.pool_ids", return_value={"STR_A", "STR_B"}) as pi:
            self.assertEqual(ov.campaign_run_scope(object(), "pool:2"), {"STR_A", "STR_B"})
            pi.assert_called_once()
        with self.assertRaises(ValueError):
            ov.campaign_run_scope(object(), "pool:x")
        self.assertIsNone(ov.campaign_run_scope(object(), ""))


class TestAutoRestart(unittest.TestCase):
    def _manager(self):
        from edgelab.research import jobs as J
        svc = mock.MagicMock()
        svc.store.list_search_batches.return_value = []
        svc.store.get_search_batch.return_value = None
        with mock.patch("edgelab.research.campaign.reconcile_run_records", return_value=[]):
            m = J.JobManager(svc, threading.RLock())
        m.RESTART_FIRST, m.RESTART_MAX = 0.05, 0.2
        return J, m

    def _run(self, m, J, side_effect):
        job = J.CampaignJob("JOB_T", "CMP_X", "SRCH_X", None)
        patcher = mock.patch("edgelab.research.campaign.run_scope", side_effect=side_effect)
        rs = patcher.start()                                   # stays active until the test ends (the worker thread uses it)
        self.addCleanup(patcher.stop)
        t = threading.Thread(target=m._work_campaign, args=(job, 0), daemon=True)
        t.start()
        return job, rs, t

    def test_restarts_until_success_and_flags_problems(self):
        J, m = self._manager()
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise OSError("disk hiccup")
            return {"run_status": "completed", "n_failed_cells": 0}

        job, rs, t = self._run(m, J, flaky)
        t.join(10)
        self.assertEqual((job.state, job.restarts, calls["n"]), ("completed", 3, 4))
        self.assertTrue(job.having_problems)                                    # 3 errors within 10 minutes
        self.assertIn("disk hiccup", job.last_error)
        self.assertIsNone(job.next_retry_at)

    def test_a_preflight_refusal_is_not_restarted(self):
        from edgelab.research.campaign import CampaignError
        J, m = self._manager()
        job, rs, t = self._run(m, J, CampaignError("PREFLIGHT_FAILED", "preflight failed; nothing was evaluated"))
        t.join(5)
        self.assertEqual((job.state, job.restarts, rs.call_count), ("failed", 0, 1))
        self.assertIn("PREFLIGHT_FAILED", job.error)

    def test_stopped_on_failure_restarts_and_cancel_ends_the_loop(self):
        J, m = self._manager()
        m.RESTART_FIRST = m.RESTART_MAX = 5.0

        def failing(*a, **kw):
            return {"run_status": "stopped_on_failure", "n_failed_cells": 1, "failed_cells": [{"error": "ValueError: boom"}]}

        job, rs, t = self._run(m, J, failing)
        for _ in range(100):
            if job.restarts:
                break
            time.sleep(0.02)
        self.assertEqual(job.restarts, 1)
        self.assertIn("boom", job.last_error)
        self.assertIsNotNone(job.next_retry_at)
        self.assertFalse(job.having_problems)
        job.cancel_requested.set()                                               # Cancel run: no further restart
        t.join(5)
        self.assertEqual(job.state, "cancelled")
        self.assertEqual(rs.call_count, 1)
        snap = job.snapshot()
        self.assertEqual((snap["restarts"], snap["having_problems"]), (1, False))


if __name__ == "__main__":
    unittest.main()
