"""Backtest results read models, bootstrapped prop evaluations, stored random controls and the risk-per-trade display
preference (ADR-73): known answers and guarantees (read-only views; controls never become runs or trials; the
preference never touches the research config hash)."""
import shutil
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from edgelab.core.config import config_hash, load_config
from edgelab.prop import bootstrap as bs
from edgelab.research import overview as ov
from edgelab.research import results_view as rv

REPO = Path(__file__).resolve().parents[1]


def _profile(pid="TRADEIFY_GROWTH_50K"):
    from edgelab.prop.service import default_profiles
    return next(p for p in default_profiles(REPO) if p["profile_id"] == pid)


def _trades(days: int, net_usd: float, start="2024-03-04") -> pd.DataFrame:
    """One trade per New York business day 10:00-11:00 with the same net P&L (a hand-checkable sequence)."""
    dates = pd.bdate_range(start, periods=days)
    rows = []
    for i, d in enumerate(dates):
        e = pd.Timestamp(f"{d.date()} 10:00", tz="America/New_York").tz_convert("UTC")
        rows.append({"trade_no": i + 1, "entry_ts": e, "exit_ts": e + pd.Timedelta(hours=1), "contracts": 1,
                     "net_usd": net_usd, "net_r": net_usd / 100.0, "gross_r": net_usd / 100.0, "mae_points": 0.0,
                     "risk_points": 10.0, "risk_usd": 20.0})
    return pd.DataFrame(rows)


class TestSurvivorAndCurves(unittest.TestCase):
    def test_survivor_needs_positive_net_and_an_eval_pass_with_payout(self):
        def rec(exp, ev, payouts):
            return {"headline_metrics": {"trade_count": 10, "expectancy_r": exp, "avg_winner_r": 2.0, "avg_loser_r": -1.0},
                    "prop": {"profiles": [{"profile": {"profile_id": "P"}, "status": ev, "evaluation": {"status": ev},
                                           "totals": {"n_payouts": payouts}}]}}
        self.assertTrue(ov.run_row("RUN_2026_00001", rec(0.1, "PASS", 1))["survivor"])
        self.assertFalse(ov.run_row("RUN_2026_00001", rec(-0.1, "PASS", 1))["survivor"])     # net negative
        self.assertFalse(ov.run_row("RUN_2026_00001", rec(0.1, "PASS", 0))["survivor"])      # passed, never paid
        self.assertFalse(ov.run_row("RUN_2026_00001", rec(0.1, "FAIL", 0))["survivor"])
        r = ov.run_row("RUN_2026_00001", rec(0.1, "PASS", 1))
        self.assertAlmostEqual(r["avg_rr"], 2.0)

    def test_breakeven_curve_values(self):
        c = rv.breakeven_curves(0.1)
        zero = {p["win_rate"]: p["avg_rr"] for p in c["zero"]["points"]}
        cost = {p["win_rate"]: p["avg_rr"] for p in c["after_cost"]["points"]}
        self.assertAlmostEqual(zero[0.5], 1.0)                        # 50% x 1R - 50% x 1R = 0
        self.assertAlmostEqual(cost[0.5], 1.2)                        # 0.5 x 1.2 - 0.5 - 0.1 = 0
        self.assertNotIn("after_cost", rv.breakeven_curves(None))


class TestBootstrap(unittest.TestCase):
    def test_identical_winning_days_always_pass_on_the_audited_day(self):
        from edgelab.prop.lifecycle import simulate_lifecycle
        prof, t = _profile(), _trades(40, 400.0)
        audited = simulate_lifecycle(t, prof)["evaluation"]
        self.assertEqual(audited["status"], "PASS")
        r = bs.run_bootstrap(t, prof, bs.params_of(n=60, seed=3))
        self.assertEqual((r["valid_replays"], r["p_pass"]), (60, 1.0))      # every replay is the same sequence
        self.assertEqual(r["median_days_to_pass"], float(audited["pass"]["trading_days"]))

    def test_losing_days_always_breach_and_results_are_seeded(self):
        prof, t = _profile(), _trades(30, -300.0)
        r = bs.run_bootstrap(t, prof, bs.params_of(n=50))
        self.assertEqual((r["p_pass"], r["p_evaluation_breach"]), (0.0, 1.0))
        mixed = pd.concat([_trades(20, 450.0), _trades(20, -380.0, start="2024-04-01")], ignore_index=True)
        mixed["trade_no"] = range(1, len(mixed) + 1)
        a = bs.run_bootstrap(mixed, prof, bs.params_of(n=50, seed=7))
        b = bs.run_bootstrap(mixed, prof, bs.params_of(n=50, seed=7))
        self.assertEqual((a["p_pass"], a["failure_reasons"]), (b["p_pass"], b["failure_reasons"]))

    def test_parameters_and_unsupported_modes(self):
        with self.assertRaises(bs.BootstrapError):
            bs.params_of(mode="intraday_trailing")                    # refused by name, never approximated
        with self.assertRaises(bs.BootstrapError):
            bs.params_of(n=10)
        v = bs.variant_profile(_profile(), "static")
        self.assertEqual((v["rules"]["evaluation.drawdown.mode"]["value"], v["rules"]["evaluation.drawdown.mode"]["status"]),
                         ("static", "CUSTOM"))
        self.assertEqual(_profile()["rules"]["evaluation.drawdown.mode"]["value"], "eod_trailing")   # original untouched


class TestWorkspaceViews(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.services import Services
        from edgelab.web.demo import create_demo_workspace
        cls.tmp = Path(tempfile.mkdtemp())
        create_demo_workspace(cls.tmp / "demo", REPO)
        cls.svc = Services(root=cls.tmp / "demo")
        sid = cls.svc.library.list()[0]["strategy_id"]
        cls.sid = sid
        cls.ds = next(d["dataset_id"] for d in cls.svc.backtest_readiness(sid)["datasets"] if d["runnable"])
        cls.run_id = cls.svc.backtest_strategy(sid, cls.ds, record=True)["run_id"]

    @classmethod
    def tearDownClass(cls):
        cls.svc.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_overview_and_panel_are_labelled_reads(self):
        before = self.svc.store._query("SELECT COUNT(*) FROM runs")[0][0]
        o = self.svc.results_overview({})
        self.assertEqual(o["scope"], "in_sample")
        self.assertGreaterEqual(o["facts"]["tested"], 1)
        self.assertTrue(all(p["synthetic"] for p in o["points"]))      # the demo is synthetic and says so
        self.assertIn("target_type", o["breakdowns"])
        p = self.svc.strategy_panel(self.sid, {})
        self.assertTrue(p["tested"])
        self.assertEqual(p["run_id"], self.run_id)
        self.assertEqual(p["technical"]["strategy_id"], self.sid)
        self.assertTrue(any(r["rule"] == "Stop" for r in p["rules"]))
        self.assertNotIn("_", p["display_name"])
        self.assertTrue(p["prop"] and all(x["profile_name"] and "_" not in x["profile_name"] for x in p["prop"]))
        self.assertEqual(self.svc.store._query("SELECT COUNT(*) FROM runs")[0][0], before)   # nothing recorded

    def test_controls_are_stored_as_controls_never_as_runs(self):
        runs_before = self.svc.store._query("SELECT COUNT(*) FROM runs")[0][0]
        res = self.svc.random_entry_control(self.sid, self.ds, n_controls=2, seed=1)
        self.assertFalse(res["stored_as_runs"])
        self.assertTrue((self.svc.data_root / "controls" / res["stored_as_control_record"]).is_file())
        self.assertEqual(self.svc.store._query("SELECT COUNT(*) FROM runs")[0][0], runs_before)
        o = self.svc.results_overview({})
        cid = o["controls"][0]["control_id"]
        self.assertTrue(cid.startswith("CTRL_"))
        self.assertEqual(self.svc.control_panel(cid)["kind"], "Random control")
        self.assertEqual(self.svc.results_overview({"controls": "0"})["controls"], [])

    def test_risk_per_trade_is_display_only(self):
        h = config_hash(load_config(self.svc.root / "configs"))
        self.assertEqual(self.svc.risk_per_trade()["risk_per_trade_usd"], 250.0)
        self.svc.set_risk_per_trade(500)
        p = self.svc.strategy_panel(self.sid, {})
        self.assertAlmostEqual(p["kpis"]["net_usd_at_risk"], p["kpis"]["net_r"] * 500)
        self.assertEqual(config_hash(load_config(self.svc.root / "configs")), h)
        with self.assertRaises(ValueError):
            self.svc.set_risk_per_trade(-1)
        self.svc.set_risk_per_trade(250)

    def test_http_routes(self):
        from edgelab.web.app import create_app
        c = create_app(self.svc.root, demo=True).test_client()
        try:
            self.assertEqual(c.get("/api/results-view/overview?basis=gross").status_code, 200)
            self.assertEqual(c.get(f"/api/results-view/strategies/{self.sid}").get_json()["strategy_id"], self.sid)
            self.assertEqual(c.get("/api/results-view/strategies/bad").status_code, 400)
            r = c.post("/api/prop/bootstrap", json={"run_id": self.run_id, "profile_id": "TRADEIFY_GROWTH_50K", "n": 50})
            self.assertIn(r.get_json()["state"], ("running", "done"))
            self.assertEqual(c.post("/api/prop/bootstrap", json={"run_id": self.run_id, "profile_id": "TRADEIFY_GROWTH_50K",
                                                                  "mode": "intraday_trailing"}).status_code, 400)
            self.assertEqual(c.post("/api/preferences/risk-per-trade", json={"risk_per_trade_usd": "x"}).status_code, 400)
            self.assertEqual(c.get("/api/explorer/strategies?survivors_only=1").status_code, 200)
        finally:
            c.application.config["EDGELAB"]["services"].store.close()


if __name__ == "__main__":
    unittest.main()
