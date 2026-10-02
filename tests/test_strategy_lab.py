"""Phase 8 Strategy Lab: the GUI research workflow through the HTTP API on a synthetic store.

duplicate -> edit parameters -> validate -> save new version (parent unchanged, hashes) ->
eligible/ineligible dataset -> backtest -> variations (exact combinations, dedupe, seed) -> batch
search job -> comparison (no ranking) -> OOS / walk-forward / random control (whole dataset and OOS
window) -> prop simulation -> machine-readable provenance; Mode B proposals through the gate."""
import copy
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from edgelab.web.app import create_app
from tests.phase2_helpers import TEST_FEED, TEST_PROXY, TEST_PROXY_PROFILE, add_test_proxy_feed, synthetic_canonical, write_generic_utc

REPO = Path(__file__).resolve().parents[1]
EMA = yaml.safe_load((REPO / "strategies" / "fixtures" / "ema_crossover.yaml").read_text())


class TestStrategyLabApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.root / "configs")
        add_test_proxy_feed(cls.root / "configs")
        write_generic_utc(synthetic_canonical("2024-01-02", "2024-06-28", tf=5, seed=5), cls.root / "h.csv")
        write_generic_utc(synthetic_canonical("2024-01-02", "2024-02-28", tf=5, seed=6), cls.root / "cfd.csv")
        cls.app = create_app(cls.root)
        cls.c = cls.app.test_client()
        svc = cls.app.config["EDGELAB"]["services"]
        cls.svc = svc
        cls.did = svc.import_file(dict(file=str(cls.root / "h.csv"), instrument=TEST_PROXY, provider=TEST_FEED,
                                       asset_type="CFD", timeframe="5m", source_timezone="UTC", calendar="CME_EQUITY",
                                       price_basis="bid", build_features=False))["dataset_id"]
        cls.cfd = svc.import_file(dict(file=str(cls.root / "cfd.csv"), instrument="NAS100_CFD", provider="SOMEBROKER",
                                       asset_type="CFD", timeframe="5m", source_timezone="UTC", calendar="CME_EQUITY",
                                       price_basis="bid", build_features=False))["dataset_id"]
        cls.base = cls.post("/api/strategies/save", {"definition": EMA})["strategy_id"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def post(cls, url, body, status=200):
        r = cls.c.post(url, json=body)
        assert r.status_code == status, (url, r.status_code, r.get_data(as_text=True)[:800])
        json.dumps(r.get_json(), allow_nan=False)
        return r.get_json()

    @classmethod
    def get(cls, url, status=200):
        r = cls.c.get(url)
        assert r.status_code == status, (url, r.status_code, r.get_data(as_text=True)[:800])
        json.dumps(r.get_json(), allow_nan=False)
        return r.get_json()

    def new_version(self, fast=8, slow=20):
        """The GUI workflow: Duplicate ema_crossover, fast 9 -> 8, slow 21 -> 20, validate, save.
        ema_crossover declares slow on the grid 15 + k*3, so 20 is refused until the user also
        changes the parameter's step (a real, hashed rule change) - never silently snapped."""
        draft = self.post(f"/api/strategies/{self.base}/duplicate", {"name": "ema_lab_copy"})["draft"]
        draft["parameters"]["fast"]["value"] = fast
        draft["parameters"]["slow"]["value"] = slow
        v = self.post("/api/strategies/validate", {"definition": draft})
        self.assertFalse(v["valid"])
        self.assertEqual(v["errors"][0]["path"], "parameters.slow.value")
        draft["parameters"]["slow"]["step"] = 1
        v = self.post("/api/strategies/validate", {"definition": draft})
        self.assertTrue(v["valid"], v["errors"])
        saved = self.post("/api/strategies/save", {"definition": draft, "parent_strategy_id": self.base,
                                                   "method": "duplicate"})
        self.assertEqual(saved["strategy_id"], v["identity"]["strategy_id"])
        return draft, saved

    # ------------------------------------------------------------------ strategy versioning
    def test_versioning_identity_and_parent_immutability(self):
        before = self.get(f"/api/strategies/{self.base}")
        draft, saved = self.new_version(fast=10, slow=20)
        self.assertTrue(saved["created"])
        self.assertNotEqual(saved["strategy_id"], self.base)
        self.assertNotEqual(saved["definition_hash"], before["definition_hash"])
        self.assertEqual(self.get(f"/api/strategies/{self.base}"), {**before, "lineage": before["lineage"]})
        again = self.post("/api/strategies/save", {"definition": copy.deepcopy(draft)})
        self.assertFalse(again["created"])                                  # identical canonical definition
        self.assertEqual((again["strategy_id"], again["definition_hash"]), (saved["strategy_id"], saved["definition_hash"]))
        cosmetic = copy.deepcopy(draft)
        cosmetic["description"] = "only words changed"
        self.assertEqual(self.post("/api/strategies/validate", {"definition": cosmetic})["identity"]["strategy_id"],
                         saved["strategy_id"])                              # cosmetic change: same strategy id
        prov = self.get(f"/api/strategies/{saved['strategy_id']}/research")
        self.assertEqual(prov["object"], "edgelab.strategy_research")
        self.assertEqual(prov["lineage"]["parent_strategy_id"], self.base)
        self.assertEqual(prov["lineage"]["parent_definition_hash"], before["definition_hash"])
        self.assertEqual(prov["lineage"]["generation_method"], "duplicate")
        self.assertEqual((prov["strategy"]["parameters"]["fast"], prov["strategy"]["parameters"]["slow"]), (10, 20))
        self.assertFalse(prov["validation_state"]["validated"])

    def test_new_strategy_from_template_validates_and_is_canonical(self):
        new = {"dsl_version": 1, "name": "lab_new_template", "timeframe": "5m",
               "family": {"id": "lab_template", "hypothesis": "test fixture"},
               "parameters": {"n": {"type": "integer", "value": 20, "min": 10, "max": 30, "step": 10}},
               "entry": {"direction": "long", "long": {"left": {"bar": "close"}, "op": ">",
                                                        "right": {"feature": "ema", "params": {"period": "$n"}, "output": "ema"}}},
               "exit": {"stop": {"type": "points", "points": 10}, "target": {"type": "risk_reward", "multiple": 2}}}
        v = self.post("/api/strategies/validate", {"definition": new})
        self.assertTrue(v["valid"], v["errors"])
        s1 = self.post("/api/strategies/save", {"definition": new})
        s2 = self.post("/api/strategies/validate", {"definition": json.loads(json.dumps(new))})
        self.assertEqual(s1["strategy_id"], s2["identity"]["strategy_id"])
        bad = copy.deepcopy(new)
        bad["exit"]["trailing_stop"] = {"points": 5}                        # unsupported: refused by name
        self.assertFalse(self.post("/api/strategies/validate", {"definition": bad})["valid"])

    # ------------------------------------------------------------------ datasets
    def test_dataset_eligibility_and_refusal(self):
        rows = {d["dataset_id"]: d for d in self.post("/api/backtests/readiness", {"strategy": self.base})["datasets"]}
        self.assertTrue(rows[self.did]["runnable"])
        self.assertFalse(rows[self.cfd]["runnable"])
        self.assertTrue(any("unconfigured" in r for r in rows[self.cfd]["reasons"]))
        self.assertTrue(rows[self.did]["limitations"] is not None)
        err = self.post("/api/backtests", {"strategy": self.base, "dataset_id": self.cfd}, 409)["error"]
        self.assertEqual(err["kind"], "cost_unconfigured")

    # ------------------------------------------------------------------ research workflow
    def test_backtest_variations_batch_compare_validate_prop(self):
        _, saved = self.new_version()
        sid = saved["strategy_id"]
        bt = self.post("/api/backtests", {"strategy": sid, "dataset_id": self.did})
        run = self.get(f"/api/results/{bt['run_id']}")
        self.assertEqual(run["record"]["strategy"]["strategy_id"], sid)
        self.assertEqual(run["record"]["strategy"]["dsl"]["definition_hash"], saved["definition_hash"])
        curve = self.get(f"/api/results/{bt['run_id']}/curve")
        self.assertEqual(curve["n_trades"], bt["metrics"]["trade_count"])
        self.assertAlmostEqual(curve["final_net_r"], bt["metrics"]["net_r"], places=6)
        self.assertAlmostEqual(curve["max_drawdown_r"], bt["metrics"]["max_drawdown_r"], places=6)

        # bounded grid around the chosen parameters: exact combinations shown before generation
        spec = {"variation_spec_version": 1, "name": "lab_grid", "mode": "grid", "max_variants": 20,
                "dimensions": [{"parameter": "fast", "values": [7, 8, 9]}, {"parameter": "slow", "values": [20, 21]}]}
        prev = self.post("/api/variations/preview", {"base": sid, "spec": spec})
        self.assertTrue(prev["ok"])
        self.assertEqual(prev["combinations_list"], [{"fast": f, "slow": s} for f in (7, 8, 9) for s in (20, 21)])
        gen = self.post("/api/variations", {"base": sid, "spec": spec, "save": True})
        self.assertEqual(gen["combinations"], 6)
        self.assertEqual(len(gen["same_as_base_combinations"]), 1)          # (8, 20) is the base itself
        self.assertEqual(gen["generated"] + len(gen["duplicates"]) + 1, 6)
        child = gen["variants"][0]["strategy_id"]
        cprov = self.get(f"/api/strategies/{child}/research")
        self.assertEqual(cprov["lineage"]["parent_strategy_id"], sid)
        self.assertEqual(cprov["lineage"]["parent_definition_hash"], saved["definition_hash"])
        self.assertEqual(cprov["lineage"]["generation_method"], "mode_a_variation")
        self.assertEqual(cprov["lineage"]["generation_batch"]["batch_id"], gen["batch_id"])
        self.assertEqual(cprov["lineage"]["generation_batch"]["spec"]["mode"], "grid")
        seeded = {**spec, "mode": "random_sample", "seed": 3, "sample_size": 3}
        a = self.post("/api/variations/preview", {"base": sid, "spec": seeded})["combinations_list"]
        self.assertEqual(a, self.post("/api/variations/preview", {"base": sid, "spec": seeded})["combinations_list"])
        self.assertEqual(len(a), 3)

        # run the batch on the dataset through the existing background job
        job = self.post("/api/research/jobs", {"spec": {"search_spec_version": 1,
                                                        "strategies": {"ids": [sid], "variation_batches": [gen["batch_id"]]},
                                                        "datasets": [self.did], "max_cells": 50, "seed": 1}}, 202)
        for _ in range(600):
            st = self.get(f"/api/research/jobs/{job['job_id']}")
            if st["state"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.1)
        self.assertEqual(st["state"], "completed", st)
        cmp_ = self.get(f"/api/compare?source=search&id={job['search_id']}")
        self.assertEqual(cmp_["n_runs"], gen["generated"] + 1)
        for row in cmp_["rows"]:
            self.assertEqual(row["status"], "IN_SAMPLE")
            self.assertEqual(row["scope"], "In-sample (exploratory)")
            self.assertFalse(row["validated"])
            for k in ("trade_count", "gross_r", "net_r", "cost_r", "expectancy_r", "profit_factor", "max_drawdown_r"):
                self.assertIn(k, row["metrics"])
            self.assertIn("breakeven_cost_multiplier", row)
            self.assertEqual((row["dataset_id"], row["provider"], row["cost_profile"], row["cost_status"]),
                             (self.did, TEST_FEED, TEST_PROXY_PROFILE, "assumed"))
            self.assertNotIn("rank", row)
            self.assertNotIn("score", row)
        self.assertTrue(any("not ranked or scored" in x for x in cmp_["labels"]))
        lin = self.get(f"/api/compare?source=lineage&id={sid}")
        self.assertGreaterEqual(lin["n_runs"], cmp_["n_runs"] + 1)           # + the single backtest
        self.assertEqual(self.get(f"/api/compare?source=batch&id={gen['batch_id']}")["source"]["kind"], "variation_batch")
        self.get("/api/compare?source=nope&id=x", 400)

        # OOS, walk-forward, random control (whole dataset and OOS window) for the selected candidate
        oos = self.post("/api/validation/oos", {"strategy": sid, "dataset_id": self.did, "split_at": "2024-04-01",
                                                "mc_sims": 100})
        self.assertEqual([w["status"] for w in oos["windows"]], ["IN_SAMPLE", "OUT_OF_SAMPLE"])
        oos_run = oos["windows"][1]["run_id"]
        self.assertEqual(self.get(f"/api/results/{oos_run}")["record"]["status"], "OUT_OF_SAMPLE")
        wf = self.post("/api/validation/walkforward", {"strategy": sid, "dataset_id": self.did, "train_months": 2,
                                                       "test_months": 1, "mc_sims": 100})
        self.assertTrue(any(w["status"] == "WALK_FORWARD" for w in wf["windows"]))
        ctrl = self.post("/api/validation/control", {"strategy": sid, "dataset_id": self.did, "n_controls": 3, "seed": 7})
        self.assertEqual((ctrl["sample_status"], ctrl["stored_as_runs"], len(ctrl["realizations"])), ("IN_SAMPLE", False, 3))
        octrl = self.post("/api/validation/control", {"strategy": sid, "dataset_id": self.did, "n_controls": 3, "seed": 7,
                                                      "split_at": "2024-04-01"})
        self.assertEqual(octrl["sample_status"], "OUT_OF_SAMPLE")
        self.assertTrue(octrl["control_config"]["period"][0].startswith("2024-04-01"))
        self.assertTrue(octrl["labels"][0].startswith("Historical, out-of-sample,"))
        direct = self.svc.random_entry_control(sid, self.did, 3, 7, period=tuple(octrl["control_config"]["period"]),
                                               sample_status="OUT_OF_SAMPLE")
        self.assertEqual(direct["comparison"], octrl["comparison"])         # the unchanged control method
        self.post("/api/validation/control", {"strategy": sid, "dataset_id": self.did, "n_controls": 0}, 400)
        self.post("/api/validation/oos", {"strategy": "rm -rf", "dataset_id": self.did, "split_at": "2024-04-01"}, 400)

        prop = self.post("/api/prop/simulate", {"run_id": oos_run, "accounts": [{"config": "SYNTH_STATIC_EVAL"}]})
        self.assertEqual(prop["lineage"]["source_run_status"], "OUT_OF_SAMPLE")
        prov = self.get(f"/api/strategies/{sid}/research")
        statuses = set(prov["validation_state"]["run_statuses"])
        self.assertTrue({"IN_SAMPLE", "OUT_OF_SAMPLE", "WALK_FORWARD"} <= statuses)
        self.assertTrue(prov["validation_state"]["has_out_of_sample_runs"])
        self.assertFalse(prov["validation_state"]["validated"])
        by_run = {r["run_id"]: r for r in prov["runs"]}
        self.assertEqual(by_run[oos_run]["prop_simulations"], 1)
        self.assertEqual(by_run[oos_run]["scope"], "Out-of-sample")
        self.assertIn(gen["batch_id"], prov["variation_batches_from_this_strategy"])

    # ------------------------------------------------------------------ Mode B
    def test_proposals_enter_through_the_gate(self):
        text = (REPO / "strategies" / "fixtures" / "proposals_example.yaml").read_text()
        menu = self.get("/api/proposals/menu?n=5")
        self.assertTrue(menu)
        dry = self.post("/api/proposals/ingest", {"batch": text, "save": False})
        self.assertGreater(dry["n_accepted"], 0)
        self.assertGreater(dry["n_rejected"], 0)                            # the fixture is mixed on purpose
        self.assertFalse(dry["saved"])
        acc = dry["accepted"][0]["strategy_id"]
        self.get(f"/api/strategies/{acc}", 404)                             # nothing saved on a dry run
        saved = self.post("/api/proposals/ingest", {"batch": text, "save": True})
        prov = self.get(f"/api/strategies/{acc}/research")
        self.assertEqual(prov["lineage"]["generation_method"], "mode_b_proposal")
        self.assertEqual(prov["lineage"]["generation_batch"]["batch_id"], saved["batch_id"])
        self.assertEqual(prov["runs"], [])                                  # a proposal never runs itself
        self.post("/api/proposals/ingest", {"batch": ": not yaml : ["}, 422)
        self.post("/api/proposals/ingest", {"batch": [1, 2]}, 400)


if __name__ == "__main__":
    unittest.main()
