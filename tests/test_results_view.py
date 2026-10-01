"""Backtest results read models, bootstrapped prop evaluations, stored random controls and the risk-per-trade display
preference (ADR-73): known answers and guarantees (read-only views; controls never become runs or trials; the
preference never touches the research config hash)."""
import json
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

    def test_calendar_years_known_answers(self):
        """Years and months by the New York date of each trade's EXIT; all twelve months listed; $ = R x risk."""
        ex = ["2021-12-31 22:00", "2022-01-01 03:00", "2022-03-15 10:00", "2022-03-16 10:00", "2024-07-01 12:00"]   # UTC
        t = pd.DataFrame({"exit_ts": pd.to_datetime(ex, utc=True), "net_r": [1.0, 2.0, -1.0, 3.0, -0.5]})
        ys = rv.calendar_years(t, 500.0)
        self.assertEqual([y["year"] for y in ys], [2021, 2022, 2024])                  # 2023 had no trades: not listed
        y21, y22, y24 = ys
        self.assertEqual((y21["trades"], y21["net_r"]), (2, 3.0))     # 2022-01-01 03:00 UTC is 31 Dec 22:00 in New York
        self.assertEqual((y22["trades"], y22["net_r"], y22["win_rate"]), (2, 2.0, 0.5))
        self.assertAlmostEqual(y22["expectancy_r"], 1.0)
        self.assertEqual(y22["net_usd_at_risk"], 1000.0)
        self.assertEqual([m["month"] for m in y22["months"]][:3], ["Jan", "Feb", "Mar"])
        self.assertEqual(len(y22["months"]), 12)
        mar = y22["months"][2]
        self.assertEqual((mar["trades"], mar["net_r"], mar["net_usd_at_risk"]), (2, 2.0, 1000.0))
        jan = y22["months"][0]
        self.assertEqual((jan["trades"], jan["net_r"], jan["expectancy_r"], jan["win_rate"]), (0, None, None, None))
        self.assertEqual((y21["months"][11]["trades"], y24["months"][6]["net_r"]), (2, -0.5))
        self.assertEqual(rv.calendar_years(t.iloc[0:0], 500.0), [])


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

    def test_panel_years_and_curve_match_the_stored_trades(self):
        p = self.svc.strategy_panel(self.sid, {})
        _, t = self.svc.store.load_run(self.run_id)
        self.assertNotIn("last_12_months", p)
        self.assertIsNone(p["holdout_period"])                 # no research protocol in the demo workspace: nothing locked
        self.assertEqual(sum(y["trades"] for y in p["years"]), len(t))
        self.assertAlmostEqual(sum(y["net_r"] for y in p["years"]), float(t["net_r"].sum()), places=9)
        for y in p["years"]:
            self.assertEqual(sum(m["trades"] for m in y["months"]), y["trades"])
            self.assertAlmostEqual(sum(m["net_r"] or 0.0 for m in y["months"]), y["net_r"], places=9)
        self.assertEqual(p["curve"]["n_trades"], len(t))
        self.assertAlmostEqual(p["curve"]["final_net_r"], p["kpis"]["net_r"], places=9)

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
            self.assertEqual(c.get("/api/explorer/strategies?prop=payout&favorites_only=1").status_code, 200)
            self.assertFalse(c.get("/api/preferences/ui").get_json()["show_ids"])
            self.assertEqual(c.post("/api/preferences/ui", json={"show_ids": "x"}).status_code, 400)
            self.assertEqual(c.get("/api/results-view/runs").status_code, 200)
            self.assertEqual(c.get("/api/favorites").status_code, 200)
            self.assertEqual(c.post("/api/workspace/reset", json={"confirm": "no"}).status_code, 400)
            self.assertEqual(c.post("/api/campaigns/bad/runs/bad/name", json={"name": "x"}).status_code, 400)
        finally:
            c.application.config["EDGELAB"]["services"].store.close()


class TestPreferencesAndReset(unittest.TestCase):
    """ADR-74: favorites, the criteria prop account, research-run names / selection and the workspace reset. Display
    metadata only: backtests, the config hash and the price data are never changed by them."""

    def setUp(self):
        from edgelab.services import Services
        from edgelab.web.demo import create_demo_workspace
        self.tmp = Path(tempfile.mkdtemp())
        create_demo_workspace(self.tmp / "demo", REPO)
        self.svc = Services(root=self.tmp / "demo")
        lib = self.svc.library.list()
        self.sid, self.other = lib[0]["strategy_id"], lib[1]["strategy_id"]
        self.ds = next(d["dataset_id"] for d in self.svc.backtest_readiness(self.sid)["datasets"] if d["runnable"])
        self.run_id = self.svc.backtest_strategy(self.sid, self.ds, record=True)["run_id"]

    def tearDown(self):
        self.svc.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_criteria_profile_decides_pass_flags_and_survivor(self):
        def rec(lucid, growth):
            def prof(pid, ok):
                return {"profile": {"profile_id": pid}, "status": "PASS" if ok else "FAIL",
                        "evaluation": {"status": "PASS" if ok else "FAIL"}, "totals": {"n_payouts": 1 if ok else 0}}
            return {"headline_metrics": {"trade_count": 10, "expectancy_r": 0.2},
                    "prop": {"profiles": [prof("LUCID_LUCIDFLEX_50K", lucid), prof("TRADEIFY_GROWTH_50K", growth)]}}
        r = ov.run_row("RUN_2026_00001", rec(False, True))
        self.assertTrue(r["survivor"])                                         # any account (no criteria)
        lucid = ov.apply_criteria(r, "LUCID_LUCIDFLEX_50K")
        self.assertEqual((lucid["prop_pass_eval"], lucid["prop_pass_payout"], lucid["survivor"]), (False, False, False))
        growth = ov.apply_criteria(r, "TRADEIFY_GROWTH_50K")
        self.assertEqual((growth["prop_pass_eval"], growth["prop_pass_payout"], growth["survivor"]), (True, True, True))
        self.assertIsNone(ov.apply_criteria(r, "TRADEIFY_SELECT_DAILY_50K")["prop_pass_eval"])   # not audited != fail
        self.assertEqual(self.svc.ui_preferences()["prop_criteria_profile"], "LUCID_LUCIDFLEX_50K")   # default
        h = config_hash(load_config(self.svc.root / "configs"))
        self.svc.set_ui_preferences({"prop_criteria_profile": "TRADEIFY_GROWTH_50K", "show_ids": True})
        self.assertEqual(ov.criteria_profile(self.svc), "TRADEIFY_GROWTH_50K")
        self.assertEqual(config_hash(load_config(self.svc.root / "configs")), h)
        with self.assertRaises(ValueError):
            self.svc.set_ui_preferences({"prop_criteria_profile": "NOPE"})
        with self.assertRaises(ValueError):
            self.svc.set_ui_preferences({"show_ids": "yes"})

    def test_favorites_only_for_tested_strategies_and_explorer_filters(self):
        with self.assertRaises(ValueError):
            self.svc.set_favorite(self.other, True)                            # never backtested
        self.svc.set_favorite(self.sid, True)
        self.assertEqual(self.svc.favorites_info()["favorites"], [self.sid])
        e = self.svc.explore_strategies({"favorites_only": "1"})
        self.assertEqual([r["strategy_id"] for r in e["rows"]], [self.sid])
        self.assertTrue(e["rows"][0]["favorite"])
        self.assertTrue(self.svc.strategy_panel(self.sid, {})["favorite"])
        ev = self.svc.explore_strategies({"prop": "eval"})["rows"]
        self.assertTrue(all(r["prop_pass_eval"] for r in ev))
        self.svc.set_favorite(self.sid, False)
        self.assertEqual(self.svc.favorites_info()["favorites"], [])

    def test_research_run_names_and_selection_filter(self):
        from edgelab.research import campaign as C
        cid, rid = "CMP_0123456789AB", "CR_20261001_120000_ABCDEF"
        d = C.runs_dir(self.svc, cid)
        d.mkdir(parents=True)
        (d / f"{rid}.json").write_text(json.dumps({"run_record_id": rid, "campaign_id": cid, "created_at": "2026-10-01"}))
        C.save_scope(self.svc, cid, rid, [self.sid])
        self.assertEqual(self.svc.rename_campaign_run(cid, rid, "  First pass  ")["name"], "First pass")
        self.assertEqual(json.loads((d / f"{rid}.json").read_text())["run_record_id"], rid)    # record untouched
        self.assertEqual(self.svc.campaign_run_names(cid), {rid: "First pass"})
        with self.assertRaises(KeyError):
            self.svc.rename_campaign_run(cid, "CR_20261001_120000_000000", "x")
        o = self.svc.results_overview({"campaign_run": f"{cid}/{rid}"})
        self.assertEqual(o["facts"]["strategies"], 1)
        e = self.svc.explore_strategies({"campaign_run": f"{cid}/{rid}"})
        self.assertEqual([r["strategy_id"] for r in e["rows"]], [self.sid])
        with self.assertRaises(ValueError):
            self.svc.results_overview({"campaign_run": "bad"})

    def test_reset_keeps_price_data_and_deletes_research(self):
        datasets = self.svc.store._query("SELECT COUNT(*) FROM datasets")[0][0]
        bars = self.svc.store._query("SELECT COUNT(*) FROM bars")[0][0]
        h = config_hash(load_config(self.svc.root / "configs"))
        self.svc.set_favorite(self.sid, True)
        self.svc.random_entry_control(self.sid, self.ds, n_controls=2, seed=1)
        with self.assertRaises(ValueError):
            self.svc.reset_workspace("yes")
        out = self.svc.reset_workspace("DELETE")
        self.assertGreaterEqual(out["deleted_rows"]["runs"], 1)
        self.assertEqual(self.svc.store._query("SELECT COUNT(*) FROM runs")[0][0], 0)
        self.assertEqual(self.svc.store._query("SELECT COUNT(*) FROM trades")[0][0], 0)
        self.assertEqual(self.svc.library.list(), [])
        self.assertFalse((self.svc.data_root / "controls").exists())
        self.assertEqual(self.svc.store._query("SELECT COUNT(*) FROM datasets")[0][0], datasets)   # price data kept
        self.assertEqual(self.svc.store._query("SELECT COUNT(*) FROM bars")[0][0], bars)
        self.svc.load_dataset(self.ds)                                          # still loads and re-validates
        self.assertEqual(self.svc.ui_preferences()["favorites"], [])
        self.assertEqual(config_hash(load_config(self.svc.root / "configs")), h)
        self.assertTrue((self.svc.data_root / "workspace_reset_log.jsonl").is_file())
        self.assertEqual(self.svc.results_overview({})["facts"]["strategies"], 0)


class TestViewCaches(unittest.TestCase):
    """Speed caches of the read-only views (ADR-75) never serve stale data: a new strategy, a new backtest, a renamed
    library and a restart are all seen; display names never carry the id-like hash."""

    def setUp(self):
        import os
        from edgelab.services import Services
        from edgelab.web.demo import create_demo_workspace
        self.tmp = Path(tempfile.mkdtemp())
        self._env = os.environ.get("EDGELAB_VIEW_CACHE")
        os.environ["EDGELAB_VIEW_CACHE"] = str(self.tmp / "cache")
        create_demo_workspace(self.tmp / "demo", REPO)
        self.svc = Services(root=self.tmp / "demo")

    def tearDown(self):
        import os
        self.svc.store.close()
        if self._env is None:
            os.environ.pop("EDGELAB_VIEW_CACHE", None)
        else:
            os.environ["EDGELAB_VIEW_CACHE"] = self._env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_backtest_and_new_strategy_are_seen_immediately(self):
        sid = self.svc.library.list()[0]["strategy_id"]
        before = self.svc.explore_strategies({"tested_only": "1"})["total"]
        ds = next(d["dataset_id"] for d in self.svc.backtest_readiness(sid)["datasets"] if d["runnable"])
        self.svc.backtest_strategy(sid, ds, record=True)
        self.assertEqual(self.svc.explore_strategies({"tested_only": "1"})["total"], before + 1)   # db token moved
        n = len(self.svc.library.list())
        doc = self.svc.library.load(sid)
        d = dict(doc["definition"]); d["name"] = "cache_probe_strategy"
        d.setdefault("parameters", {})
        res = self.svc.save_strategy(d) if hasattr(self.svc, "save_strategy") else None
        if res is not None:
            self.assertEqual(len(self.svc.library.list()), n + (1 if res.get("created") else 0))
            self.assertEqual(len(ov.library_facets(self.svc)), len(self.svc.library.list()))

    def test_fingerprint_fast_path_matches_full_scan(self):
        lib = self.svc.library
        self.assertEqual(lib._fingerprint(), lib._fingerprint_scan())
        sid = lib.list()[0]["strategy_id"]
        lib.archive(sid)
        self.assertEqual(lib._fingerprint(), lib._fingerprint_scan())          # invalidated by the write
        self.assertNotIn(sid, {r["strategy_id"] for r in ov.library_facets(self.svc)})
        lib.restore(sid)
        self.assertIn(sid, {r["strategy_id"] for r in ov.library_facets(self.svc)})

    def test_facets_disk_cache_survives_restart_and_rebuilds_on_change(self):
        from edgelab.services import Services
        rows = ov.library_facets(self.svc)
        self.assertTrue(ov.facets_cache_path(self.svc).is_file())
        self.assertFalse((self.svc.data_root / "view_cache").exists())               # never inside the workspace
        ov._FACET_CACHE.clear()
        svc2 = Services(root=self.svc.root)
        try:
            self.assertEqual(ov.library_facets(svc2), rows)                     # read from disk, identical
        finally:
            svc2.store.close()

    def test_display_names_never_carry_the_hash(self):
        doc = {"strategy_id": "STR_0123456789AB", "name": "breakout_retest_02306faef8", "family_id": "x",
               "definition": {"name": "breakout_retest_02306faef8"}, "lineage": [{}]}
        n = ov.display_names(doc)
        self.assertNotIn("02306faef8", n["display_name"] + n["short_name"])
        for f in ov.library_facets(self.svc):
            self.assertNotRegex(f["display_name"], r"[0-9a-f]{8,}")


if __name__ == "__main__":
    unittest.main()


from tests.test_research_protocol import DISC, HOLD, ProtocolBase   # noqa: E402


class TestPanelHoldoutPeriod(ProtocolBase):
    """ADR-84: the strategy panel shows the protocol's locked holdout after a discovery run (never backtested), or the
    strategy's own holdout evaluation, labelled and kept apart from the discovery years. Reading it changes nothing."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from edgelab.services import Services
        s = Services(root=cls.root)
        p = s.create_protocol(cls.did, DISC, HOLD, name="panel", pre_protocol_exposure=[cls.pre_run], exposure_statement="x")
        cls.pid, w = p["protocol_id"], p["material"]["windows"]
        cls.hold = w["holdout"]
        cls.search = s.run_search({"strategies": {"ids": [cls.ema, cls.rsi]}, "datasets": [cls.did],
                                   "period": {"start": w["discovery"]["first_bar"], "end": w["discovery"]["last_bar"]}})["search_id"]
        s.select_shortlist(cls.search, [cls.ema])
        cls.holdout_run = s.evaluate_holdout(cls.pid, cls.search, cls.ema)["run_id"]
        s.store.close()

    def test_locked_holdout_without_an_evaluation(self):
        s = self.svc()
        before = (s.store.count_trials(self.pid), len(s.store.list_holdout_access(self.pid)), len(s.store.list_runs()))
        p = s.strategy_panel(self.rsi, {})
        h = p["holdout_period"]
        self.assertEqual((h["from"], h["to"], h["trading_dates"]), (self.hold["first_bar"], self.hold["last_bar"], list(HOLD)))
        self.assertFalse(h["evaluated"])
        self.assertEqual(h["years"], [{"year": 2024, "locked": True}])
        self.assertIsNone(h["curve"])
        self.assertTrue(all(pd.Timestamp(x["exit_ts"]) < pd.Timestamp(h["from"]) for x in p["curve"]["points"]))
        self.assertEqual(before, (s.store.count_trials(self.pid), len(s.store.list_holdout_access(self.pid)), len(s.store.list_runs())))

    def test_holdout_evaluation_is_shown_separately(self):
        s = self.svc()
        p = s.strategy_panel(self.ema, {})
        h = p["holdout_period"]
        self.assertTrue(h["evaluated"])
        self.assertEqual(h["run_id"], self.holdout_run)
        _, ht = s.store.load_run(self.holdout_run)
        self.assertEqual(sum(y["trades"] for y in h["years"]), len(ht))
        self.assertAlmostEqual(sum(y["net_r"] or 0.0 for y in h["years"]), float(ht["net_r"].sum()), places=9)
        self.assertEqual(h["curve"]["n_trades"], len(ht))
        _, t = s.store.load_run(p["run_id"])                 # the discovery years are the panel run's trades only
        self.assertNotEqual(p["run_id"], self.holdout_run)
        self.assertEqual(sum(y["trades"] for y in p["years"]), len(t))

    def test_no_holdout_period_when_the_run_reaches_it(self):
        s = self.svc()
        rec, _ = s.store.load_run(self.pre_run)               # pre-protocol run over the whole dataset
        self.assertGreaterEqual(pd.Timestamp(rec["dataset"]["end"]), pd.Timestamp(self.hold["first_bar"]))
        self.assertIsNone(rv.holdout_period(s, rec, [], 250.0))
