"""Research-terminal read models (research/overview.py) and their HTTP routes: read-only by construction,
holdout runs never presented as ordinary OOS, pipeline states from stored facts, server-side explorer
filtering/sorting/paging, and no HTTP route that can change a protocol. Synthetic data only."""
import unittest

from edgelab.research import protocol as rp
from edgelab.web.app import create_app
from tests.test_research_protocol import DISC, HOLD, ProtocolBase


class TestResearchTerminal(ProtocolBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from edgelab.services import Services
        s = Services(root=cls.root)
        p = s.create_protocol(cls.did, DISC, HOLD, name="overview", pre_protocol_exposure=[cls.pre_run], exposure_statement="x")
        cls.pid = p["protocol_id"]
        w = p["material"]["windows"]["discovery"]
        cls.search = s.run_search({"strategies": {"ids": [cls.ema, cls.rsi, cls.slow]}, "datasets": [cls.did],
                                   "period": {"start": w["first_bar"], "end": w["last_bar"]}})["search_id"]
        s.select_shortlist(cls.search, [cls.ema])
        cls.holdout_run = s.evaluate_holdout(cls.pid, cls.search, cls.ema)["run_id"]
        s.store.close()

    def client(self):
        app = create_app(self.root)
        self.addCleanup(app.config["EDGELAB"]["services"].store.close)
        return app.test_client(), app.config["EDGELAB"]["services"]

    def state(self, svc):
        rec = svc.store.get_protocol(self.pid)
        return (svc.store.count_trials(self.pid), len(svc.store.list_trial_events(self.pid)),
                len(svc.store.list_proposal_attempts(self.pid)), len(svc.store.list_holdout_access(self.pid)),
                len(svc.store.list_runs()), rec["material_hash"], rec["status"])

    def test_every_read_model_is_read_only(self):
        c, svc = self.client()
        before = self.state(svc)
        runs = [r["run_id"] for r in svc.list_runs()]
        for url in ["/api/overview", "/api/explorer/strategies?scope=any", "/api/research/dashboard?include_synthetic=1",
                    f"/api/results/{runs[0]}/analytics", f"/api/results/{self.holdout_run}/analytics",
                    f"/api/strategies/{self.ema}/pipeline", "/api/pipeline", "/api/protocols", f"/api/protocols/{self.pid}"]:
            r = c.get(url)
            self.assertEqual(r.status_code, 200, (url, r.get_json()))
        self.assertEqual(self.state(svc), before)                                     # no trial, look, run or protocol change
        rp.verify_record(svc.store.get_protocol(self.pid))

    def test_no_http_route_changes_a_protocol(self):
        c, _ = self.client()
        for method, url in (("post", "/api/protocols"), ("post", f"/api/protocols/{self.pid}"),
                            ("post", f"/api/protocols/{self.pid}/retire"), ("put", f"/api/protocols/{self.pid}"),
                            ("delete", f"/api/protocols/{self.pid}")):
            self.assertIn(getattr(c, method)(url, json={}).status_code, (404, 405), url)
        self.assertEqual(c.get("/api/protocols/RP_bad").status_code, 400)

    def test_holdout_run_is_labelled_and_never_counted_as_oos(self):
        c, svc = self.client()
        ov = c.get("/api/overview").get_json()
        row = [x for x in ov["recent_runs"] if x["run_id"] == self.holdout_run][0]
        self.assertTrue(row["holdout"])
        self.assertEqual(row["status"], "OUT_OF_SAMPLE")                               # stored status unchanged
        ex = c.get("/api/explorer/strategies?scope=any&page_size=200").get_json()
        ema = [x for x in ex["rows"] if x["strategy_id"] == self.ema][0]
        self.assertIsNone(ema["oos_run_id"])                                           # the holdout look is not "OOS"
        import json
        led = [h for h in svc.store.list_holdout_access(self.pid) if h["status"] == "completed"][-1]
        self.assertEqual(ema["holdout_outcome"], json.loads(led["result_json"])["outcome"])
        self.assertIn(ema["state"], ("holdout_criteria_met", "holdout_criteria_not_met"))
        oos = c.get("/api/explorer/strategies?scope=oos").get_json()
        self.assertNotIn(self.holdout_run, [x["ref_run"]["run_id"] for x in oos["rows"] if x["ref_run"]])
        dash = c.get("/api/research/dashboard?scope=oos&include_synthetic=1").get_json()
        self.assertEqual((dash["n_runs"], dash["holdout_runs_excluded"]), (0, 1))
        an = c.get(f"/api/results/{self.holdout_run}/analytics").get_json()
        self.assertTrue(an["run"]["holdout"])

    def test_pipeline_states_come_from_stored_facts(self):
        c, _ = self.client()
        st = {s["id"]: s for s in c.get(f"/api/strategies/{self.ema}/pipeline").get_json()["stages"]}
        self.assertEqual(st["shortlist"]["state"], "done")
        self.assertIn("not acceptance", st["shortlist"]["evidence"])
        self.assertEqual(st["holdout_authorization"]["state"], "done")
        self.assertIn(st["holdout_result"]["state"], ("done", "failed"))
        self.assertIn("never 'accepted'", st["holdout_result"]["evidence"])
        self.assertEqual(st["controls"]["state"], "done")                               # stored with the holdout look
        self.assertEqual(st["oos"]["state"], "pending")                                # the holdout run is not OOS
        for k in ("paper_evaluation", "human_review"):
            self.assertEqual(st[k]["state"], "not_available")
        rsi = {s["id"]: s for s in c.get(f"/api/strategies/{self.rsi}/pipeline").get_json()["stages"]}
        self.assertEqual((rsi["shortlist"]["state"], rsi["holdout_authorization"]["state"]), ("pending", "pending"))
        self.assertEqual(rsi["controls"]["state"], "not_recorded")

    def test_explorer_filters_sorts_and_pages_on_the_server(self):
        c, _ = self.client()
        allr = c.get("/api/explorer/strategies?scope=in_sample&sort=expectancy_r&order=desc").get_json()
        vals = [x["expectancy_r"] for x in allr["rows"] if x["expectancy_r"] is not None]
        self.assertEqual(vals, sorted(vals, reverse=True))
        self.assertEqual(allr["library_total"], 3)
        p1 = c.get("/api/explorer/strategies?scope=any&page_size=2&page=1&sort=strategy_id").get_json()
        p2 = c.get("/api/explorer/strategies?scope=any&page_size=2&page=2&sort=strategy_id").get_json()
        self.assertEqual((p1["total"], p1["pages"], len(p1["rows"]), len(p2["rows"])), (3, 2, 2, 1))
        self.assertFalse({x["strategy_id"] for x in p1["rows"]} & {x["strategy_id"] for x in p2["rows"]})
        q = c.get("/api/explorer/strategies?q=rsi_threshold").get_json()          # searches name, id, family, hypothesis
        self.assertEqual([x["strategy_id"] for x in q["rows"]], [self.rsi])
        self.assertEqual(c.get("/api/explorer/strategies?family_id=ema_crossover").get_json()["total"], 2)
        self.assertEqual(c.get(f"/api/explorer/strategies?protocol={self.pid}").get_json()["total"], 1)
        big = c.get("/api/explorer/strategies?min_trades=100000").get_json()
        self.assertEqual(big["total"], 0)
        for bad in ("sort=win_ratez", "scope=holdout", "page=x", "min_trades=abc", "page_size=-1"):
            self.assertEqual(c.get(f"/api/explorer/strategies?{bad}").status_code, 400, bad)

    def test_home_overview_and_execution_identity(self):
        c, _ = self.client()
        ov = c.get("/api/overview").get_json()
        self.assertEqual(ov["protocols"][0]["protocol_id"], self.pid)
        self.assertEqual(ov["dataset_source"], "active protocol")
        ex = ov["execution"]
        self.assertEqual((ex["quote_model"], ex["spread_source"]), ("directional_bid_ask", "quotes"))
        self.assertEqual(ex["quote_sides"], {"long_entry": "ASK", "long_exit": "BID", "short_entry": "BID", "short_exit": "ASK"})
        self.assertIn("Nothing here labels a strategy profitable", ov["note"])
        protos = c.get("/api/protocols").get_json()
        self.assertEqual(protos[0]["protocol_version"], 3)

    def test_run_analytics_breakdowns_are_consistent(self):
        c, svc = self.client()
        a = c.get(f"/api/results/{self.pre_run}/analytics").get_json()
        n = a["n_trades"]
        self.assertGreater(n, 0)
        for key in ("direction", "year", "weekday", "month"):
            self.assertEqual(sum(b["trade_count"] for b in a[key]), n, key)            # every trade in exactly one bucket
        self.assertEqual(sum(a["r_histogram"]["net"]["counts"]), n)
        self.assertEqual(sum(a["win_loss"].values()), n)
        self.assertAlmostEqual(a["metrics"]["net"]["net_r"], svc.store.load_run(self.pre_run)[1]["net_r"].sum(), places=9)
        self.assertEqual(len(a["monte_carlo_paths"]["paths"]), 40)
        self.assertIn("not a market simulation", a["monte_carlo_paths"]["note"])

    def test_holdout_control_p_value_comes_from_the_ledger(self):
        """The exact Monte-Carlo p-value shown in the UI is the one the protocol stored for the holdout look
        (ad-hoc controls stay descriptive: no p-value, see test_random_control)."""
        c, svc = self.client()
        pl = c.get(f"/api/strategies/{self.ema}/pipeline").get_json()
        rc = [h["random_control"] for h in pl["holdout"] if h["random_control"]][0]
        import json
        stored = json.loads([h for h in svc.store.list_holdout_access(self.pid) if h["status"] == "completed"][-1]["result_json"])
        self.assertEqual(rc, stored["criteria"]["random_control"])
        self.assertEqual(rc["rule_id"], "monte_carlo_pvalue_v1")


if __name__ == "__main__":
    unittest.main()
