"""Phase 5 research analytics: pooled vs per-dataset metrics, stability counts, session and hour
buckets, exact cost sensitivity, break-even multiple, and the caveat labels reports must carry.
Deterministic synthetic trades only; no real data, no strategy search."""
from __future__ import annotations

import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.analytics import research as ra
from edgelab.analytics.metrics import breakeven_cost_multiplier, compute_metrics, cost_sensitivity
from edgelab.engine.backtester import BacktestResult
from edgelab.features.sessions import load_sessions
from edgelab.research.runs import record_run
from edgelab.services import Services
from tests.helpers import CFG
from tests.phase2_helpers import TEST_FEED, TEST_PROXY, TEST_PROXY_PROFILE, add_test_proxy_feed, with_test_proxy

SESSIONS = load_sessions(CFG)


def trades(entries_ny, gross, cost):
    """Minimal engine-shaped trade rows. entries_ny: New York wall-clock strings; R columns given."""
    ts = pd.DatetimeIndex([pd.Timestamp(e, tz="America/New_York") for e in entries_ny]).tz_convert("UTC")
    g, c = np.asarray(gross, float), np.asarray(cost, float)
    n = len(ts)
    return pd.DataFrame({
        "entry_bar": np.arange(n), "exit_bar": np.arange(n) + 1, "direction": np.ones(n),
        "contracts": np.full(n, 2.0), "entry_price_theo": np.full(n, 100.0), "exit_price_theo": 100.0 + g,
        "entry_ts": ts, "exit_ts": ts + pd.Timedelta(minutes=5), "gross_r": g, "cost_r": c, "cost_r_base": c,
        "net_r": g - c, "net_usd": (g - c) * 20.0, "holding_minutes": np.full(n, 5.0),
        "exit_reason": np.where(g > 0, "TARGET", "STOP"), "conflict_resolution": np.full(n, ""),
    })


# year A: 3 trades, strong; year B: 1 trade, loss -> pooled expectancy != mean of yearly expectancies
A = trades(["2019-01-07 10:00", "2019-01-08 10:30", "2019-01-09 13:00"], [2.0, 2.0, -1.0], [0.2, 0.2, 0.2])
B = trades(["2020-01-06 03:00"], [-1.0], [0.2])


class TestSampleScopeLabels(unittest.TestCase):
    """The first caveat names the sample the runs were measured on (run status)."""
    BASE = {"assumptions": {"cost_status": "assumed", "costs": {"profile": "P"}}, "dataset": {}}
    IN_SAMPLE_TEXT = ("Historical, in-sample, descriptive results under the stated assumptions; "
                      "not a forecast and not by itself evidence of a trading edge.")

    def first(self, *statuses):
        return ra.research_labels([{**self.BASE, "status": s} for s in statuses])[0]

    def test_oos_first_label_does_not_say_in_sample(self):
        for st in (("OUT_OF_SAMPLE",), ("WALK_FORWARD",), ("OUT_OF_SAMPLE", "WALK_FORWARD")):
            label = self.first(*st)
            self.assertNotIn("in-sample", label, st)
            self.assertTrue(label.startswith("Historical, out-of-sample, descriptive results"), st)
            self.assertIn("not by itself evidence of a trading edge", label)

    def test_in_sample_and_default_wording_unchanged(self):
        self.assertEqual(self.first("IN_SAMPLE"), self.IN_SAMPLE_TEXT)
        self.assertEqual(ra.research_labels([self.BASE])[0], self.IN_SAMPLE_TEXT)     # no status recorded
        self.assertIn("in-sample and out-of-sample", self.first("IN_SAMPLE", "OUT_OF_SAMPLE"))


class TestPooledAndGroups(unittest.TestCase):
    def test_pooled_is_one_measurement_not_an_average(self):
        p = ra.pooled_summary({"A": A, "B": B})
        direct = compute_metrics(pd.concat([A, B]).sort_values("entry_ts"))
        for k in ("trade_count", "gross_r", "net_r", "cost_r", "expectancy_r", "profit_factor", "win_rate",
                  "max_drawdown_r", "max_win_streak", "max_loss_streak", "best_trade_r", "worst_trade_r"):
            self.assertEqual(p[k], direct[k], k)
        self.assertAlmostEqual(p["expectancy_r"], (1.8 + 1.8 - 1.2 - 1.2) / 4)          # 0.3
        mean_of_groups = (compute_metrics(A)["expectancy_r"] + compute_metrics(B)["expectancy_r"]) / 2
        self.assertNotAlmostEqual(p["expectancy_r"], mean_of_groups)                  # 0.3 vs -0.2
        self.assertIn("pooled", p["method"])

    def test_pooled_drawdown_and_streaks_cross_dataset_boundaries(self):
        a = trades(["2019-12-30 10:00", "2019-12-31 10:00"], [1.0, -1.0], [0.0, 0.0])
        b = trades(["2020-01-02 10:00", "2020-01-03 10:00"], [-1.0, 1.0], [0.0, 0.0])
        p = ra.pooled_summary({"b": b, "a": a})                         # input order must not matter
        self.assertEqual((p["max_drawdown_r"], p["max_loss_streak"]), (2.0, 2))
        rows = {r["group"]: r for r in ra.group_table({"a": a, "b": b})}
        self.assertEqual((rows["a"]["max_drawdown_r"], rows["b"]["max_drawdown_r"]), (1.0, 1.0))

    def test_group_table_and_stability(self):
        rows = ra.group_table({"2019": A, "2020": B})
        self.assertEqual([r["group"] for r in rows], ["2019", "2020"])
        self.assertEqual((rows[0]["trade_count"], rows[1]["trade_count"]), (3, 1))
        self.assertAlmostEqual(rows[0]["net_r"], 2.4)
        self.assertAlmostEqual(rows[0]["breakeven_cost_multiplier"], 3.0 / 0.6)
        self.assertAlmostEqual(rows[1]["breakeven_cost_multiplier"], -1.0 / 0.2)
        s = ra.stability_summary(rows)
        self.assertEqual((s["groups"], s["groups_net_positive"], s["groups_gross_positive"],
                          s["groups_profit_factor_above_1"]), (2, 1, 1, 1))
        self.assertAlmostEqual(s["expectancy_r_min"], -1.2)
        self.assertAlmostEqual(s["expectancy_r_max"], 0.8)
        self.assertAlmostEqual(s["expectancy_r_std"], float(np.std([0.8, -1.2], ddof=1)))


class TestSessionsAndHours(unittest.TestCase):
    def test_canonical_sessions_by_entry_time(self):
        rows = {r["session"]: r for r in ra.session_breakdown(ra.pooled_trades({"A": A, "B": B}), SESSIONS)["rows"]}
        self.assertEqual(rows["NY_RTH"]["trade_count"], 3)              # 10:00, 10:30, 13:00 NY
        self.assertEqual(rows["NY_AM"]["trade_count"], 2)
        self.assertEqual(rows["NY_PM"]["trade_count"], 1)
        self.assertEqual(rows["LONDON"]["trade_count"], 3)              # 08:00-16:30 London = 03:00-11:30 NY (Jan)
        self.assertEqual(rows["(none of the listed windows)"]["trade_count"], 0)
        self.assertAlmostEqual(rows["NY_AM"]["net_r"], 3.6)
        self.assertEqual(rows["NY_AM"]["profit_factor"], math.inf)
        self.assertEqual(list(rows)[:-1], list(SESSIONS))               # configured windows, config order

    def test_trades_outside_every_window(self):
        t = trades(["2019-01-05 12:00"], [1.0], [0.1])                   # Saturday noon NY
        rows = {r["session"]: r for r in ra.session_breakdown(t, SESSIONS)["rows"]}
        self.assertEqual(rows["(none of the listed windows)"]["trade_count"], 1)

    def test_hour_buckets(self):
        h = ra.hour_breakdown(ra.pooled_trades({"A": A, "B": B}), "America/New_York")
        rows = {r["bucket"]: r for r in h["rows"]}
        self.assertEqual({k: v["trade_count"] for k, v in rows.items()},
                         {"03:00": 1, "10:00": 2, "13:00": 1})
        self.assertAlmostEqual(rows["10:00"]["expectancy_r"], 1.8)


class TestCostSensitivity(unittest.TestCase):
    def test_gross_constant_cost_scales_and_matches_existing_function(self):
        pooled = ra.pooled_trades({"A": A, "B": B})
        mults = CFG["backtest"]["cost_sensitivity_multipliers"]
        t = ra.cost_sensitivity_table(pooled, mults)
        base = cost_sensitivity(pooled, tuple(mults))
        self.assertEqual([r["cost_multiplier"] for r in t["rows"]], [float(m) for m in mults])
        for row, ref in zip(t["rows"], base.itertuples()):
            self.assertAlmostEqual(row["gross_r"], 2.0)                  # unchanged by construction
            self.assertAlmostEqual(row["cost_r"], 0.8 * row["cost_multiplier"])
            self.assertAlmostEqual(row["net_r"], 2.0 - 0.8 * row["cost_multiplier"])
            self.assertAlmostEqual(row["net_r"], ref.net_r)
        self.assertAlmostEqual(t["breakeven_cost_multiplier"], breakeven_cost_multiplier(pooled))
        self.assertAlmostEqual(t["breakeven_cost_multiplier"], 2.0 / 0.8)


def _record(store, cfg, strategy_id, dataset_id, start, end, t, status="assumed",
            profile=TEST_PROXY_PROFILE, notes="single backtest (Strategy Lab)"):
    res = BacktestResult(strategy_id, {"dsl": {"name": "fixture"}}, t, {}, len(t),
                         {"cost_status": status, "costs": {"status": status, "profile": profile}},
                         {"dataset_id": dataset_id, "provider": TEST_FEED, "instrument": TEST_PROXY,
                          "asset_type": "CFD", "price_basis": "bid", "timeframe": "5m", "start": start, "end": end},
                         None)
    return record_run(store, cfg, res, compute_metrics(t), notes=notes)


class TestResearchReportService(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.svc = Services(cfg=with_test_proxy(CFG), root=self.tmp)          # + test-local BID proxy feed
        self.r19 = _record(self.svc.store, self.svc.cfg, "STR_X", "D2019", "2019-01-01", "2019-12-31", A)
        self.r20 = _record(self.svc.store, self.svc.cfg, "STR_X", "D2020", "2020-01-01", "2020-12-31", B)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_report_from_stored_runs(self):
        rep = self.svc.research_report([self.r20, self.r19])            # order-independent
        self.assertEqual([r["dataset_id"] for r in rep["by_dataset"]], ["D2019", "D2020"])
        self.assertEqual(rep["pooled"]["trade_count"], 4)
        self.assertAlmostEqual(rep["pooled"]["expectancy_r"], 0.3)
        self.assertEqual({r["cost_status"] for r in rep["by_dataset"]}, {"assumed"})
        self.assertEqual(rep["cost_status"], ["assumed"])
        self.assertAlmostEqual(rep["cost_sensitivity"]["breakeven_cost_multiplier"], 2.5)
        self.assertEqual(rep["stability"]["groups_net_positive"], 1)
        mc = rep["monte_carlo"]
        self.assertAlmostEqual(mc["shuffle"]["observed_total_r"], rep["pooled"]["net_r"])
        self.assertEqual(mc, self.svc.research_report([self.r19, self.r20])["monte_carlo"])   # seeded
        json.dumps(rep)                                                   # strict-JSON contract

    def test_labels_keep_proxy_and_assumed_cost_caveats(self):
        from unittest import mock
        # standing dataset / cost-profile notes (none ship; test-local entries exercise the mechanism)
        with mock.patch.dict(ra.DATASET_NOTES, {(TEST_FEED, TEST_PROXY): ("test dataset note A", "test dataset note B")}), \
                mock.patch.dict(ra.PROFILE_NOTES, {TEST_PROXY_PROFILE: "test profile note"}):
            text = " | ".join(self.svc.research_report([self.r19, self.r20])["labels"])
        for phrase in ("test dataset note A", "test dataset note B", "test profile note",
                       "not broker-verified", "research proxy, not a tradable", "BID-only prices",
                       "not by itself evidence of a trading edge"):
            self.assertIn(phrase, text)
        from edgelab.instruments import load_instruments
        self.assertTrue(load_instruments(self.svc.cfg)[TEST_PROXY].extra["research_proxy"])

    def test_refusals(self):
        other = _record(self.svc.store, self.svc.cfg, "STR_Y", "D2021", "2021-01-01", "2021-12-31", A)
        with self.assertRaisesRegex(ValueError, "ONE fixed strategy"):
            self.svc.research_report([self.r19, other])
        dup = _record(self.svc.store, self.svc.cfg, "STR_X", "D2019", "2019-01-01", "2019-12-31", A)
        with self.assertRaisesRegex(ValueError, "counted twice"):
            self.svc.research_report([self.r19, dup])
        with self.assertRaises(ValueError):
            self.svc.research_report([])

    def test_http_and_cli(self):
        from edgelab.cli import main
        from edgelab.web.app import create_app
        shutil.copytree(Path(__file__).resolve().parents[1] / "configs", self.tmp / "configs")
        add_test_proxy_feed(self.tmp / "configs")
        c = create_app(self.tmp).test_client()
        r = c.get(f"/api/results/report?run_ids={self.r19},{self.r20}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["pooled"]["trade_count"], 4)
        self.assertEqual(c.get("/api/results/report?run_ids=bad id").status_code, 400)
        self.assertEqual(c.get("/api/results/report").status_code, 400)
        self.assertEqual(main(["--root", str(self.tmp), "report", self.r19, self.r20]), 0)


if __name__ == "__main__":
    unittest.main()
