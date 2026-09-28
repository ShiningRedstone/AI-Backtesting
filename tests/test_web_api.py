"""Phase 3.5: HTTP API over the Phase 3 services (Flask test client, no browser)."""
import copy
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from edgelab.web.app import create_app
from edgelab.web.config import WebConfigError, load_web_config
from edgelab.web.demo import create_demo_workspace

REPO = Path(__file__).resolve().parents[1]
FX = REPO / "strategies" / "fixtures"
MIN = {"dsl_version": 1, "name": "api_min", "timeframe": "5m",
       "family": {"id": "api_family", "hypothesis": "test fixture"},
       "parameters": {"n": {"type": "integer", "value": 20, "min": 10, "max": 30, "step": 10},
                      "flag": {"type": "boolean", "value": False}},
       "entry": {"direction": "long",
                 "long": {"all": [{"left": {"bar": "close"}, "op": ">",
                                   "right": {"feature": "ema", "params": {"period": "$n"}, "output": "ema"}},
                                  {"enabled": "$flag", "left": {"bar": "close"}, "op": ">",
                                   "right": {"feature": "ema", "params": {"period": 50}, "output": "ema", "timeframe": "1h"}}]}},
       "exit": {"stop": {"type": "points", "points": 10}, "target": {"type": "risk_reward", "multiple": 2}}}


def strict(obj):
    return json.dumps(obj, allow_nan=False)


class TestWebApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        create_demo_workspace(cls.tmp / "demo", REPO)
        cls.app = create_app(cls.tmp / "demo", demo=True)
        cls.c = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, url, status=200):
        r = self.c.get(url)
        self.assertEqual(r.status_code, status, r.get_data(as_text=True)[:400])
        strict(r.get_json())
        return r.get_json()

    def post(self, url, body, status=200):
        r = self.c.post(url, json=body)
        self.assertEqual(r.status_code, status, r.get_data(as_text=True)[:600])
        strict(r.get_json())
        return r.get_json()

    def save(self, d, **kw):
        return self.post("/api/strategies/save", {"definition": d, **kw})

    # ------------------------------------------------------------------ system / options
    def test_status_and_options_come_from_the_backend(self):
        s = self.get("/api/status")
        self.assertTrue(s["demo"])
        self.assertEqual(s["datasets"], 2)
        self.assertGreaterEqual(s["strategies"], 8)
        self.assertTrue(s["frontend"]["built"])
        o = self.get("/api/options")
        from edgelab.features.spec import all_defs
        from edgelab.strategy import dsl
        self.assertEqual({f["id"] for f in o["features"]}, {d.feature_id for d in all_defs() if not d.feature_id.startswith("_")})
        self.assertEqual(o["comparison_operators"], list(dsl.OPERATORS))
        self.assertEqual(o["parameter_types"], list(dsl.PARAM_TYPES))
        self.assertIn("60m", o["htf_options"]["5m"])
        self.assertNotIn("1m", o["htf_options"]["5m"])                      # lower TF never offered
        self.assertTrue(all(int(h[:-1]) % 15 == 0 for h in o["htf_options"]["15m"]))
        self.assertEqual(next(f for f in o["features"] if f["id"] == "session")["session_params"], ["session"])
        self.assertIn("trailing_stop", o["unsupported"])
        cfg = self.get("/api/config")
        self.assertEqual(cfg["cost_profiles"]["NAS100_CFD"]["status"], "unconfigured")
        self.assertIn("generic_csv", cfg["import_profiles"])

    # ------------------------------------------------------------------ builder contracts
    def test_render_never_crashes_on_drafts(self):
        for draft in ({}, {"entry": 5}, {"name": "x", "entry": {"direction": "long", "long": {"any": [{"not": {}}]}}}):
            r = self.post("/api/strategies/render", {"definition": draft})
            self.assertFalse(r["valid"])
            self.assertTrue(r["errors"])
            self.assertIn("yaml", r)
        r = self.post("/api/strategies/render", {"definition": MIN})
        self.assertTrue(r["valid"])
        self.assertIn("period: $n", r["yaml"])
        self.assertIn("version: 1", r["canonical_yaml"])                    # canonical form pins versions
        self.assertTrue(r["identity"]["strategy_id"].startswith("STR_"))

    def test_validate_reports_backend_paths(self):
        bad = copy.deepcopy(MIN)
        bad["entry"]["long"]["all"][0]["right"]["feature"] = "ema_fast"
        bad["exit"]["stop"]["points"] = -1
        v = self.post("/api/strategies/validate", {"definition": bad})
        paths = {e["path"] for e in v["errors"]}
        self.assertEqual(paths, {"entry.long.all[0].right.feature", "exit.stop.points"})
        self.assertIn("ema", next(e for e in v["errors"] if "feature" in e["path"])["hint"])

    def test_save_reload_identity_and_lineage_methods(self):
        d = copy.deepcopy(MIN)
        d["name"] = "api_identity"
        r = self.save(d)
        sid = r["strategy_id"]
        stored = self.get(f"/api/strategies/{sid}")
        again = self.post("/api/strategies/validate", {"definition": stored["definition"]})
        self.assertEqual(again["identity"]["strategy_id"], sid)                   # reload -> same identity
        self.assertFalse(self.save(stored["definition"])["created"])              # idempotent library
        noop = self.save(stored["definition"], parent_strategy_id=sid, method="manual_edit")
        self.assertFalse(noop["created"])
        self.assertIn("No logic change", noop["note"])
        self.assertEqual(len(self.get(f"/api/strategies/{sid}")["lineage"]), 1)   # no self-referencing record
        edited = copy.deepcopy(d)
        edited["exit"]["stop"]["points"] = 12
        e = self.save(edited, parent_strategy_id=sid, method="manual_edit")
        lin = self.get(f"/api/strategies/{e['strategy_id']}/lineage")
        self.assertEqual((lin["records"][0]["generation_method"], lin["records"][0]["parent_strategy_id"]), ("manual_edit", sid))
        draft = self.post(f"/api/strategies/{sid}/duplicate", {"name": "api_copy"})["draft"]
        self.assertEqual(draft["name"], "api_copy")
        draft["exit"]["stop"]["points"] = 13
        dup = self.save(draft, parent_strategy_id=sid, method="duplicate")
        self.assertEqual(self.get(f"/api/strategies/{dup['strategy_id']}")["lineage"][0]["generation_method"], "duplicate")
        self.post("/api/strategies/save", {"definition": draft, "parent_strategy_id": sid, "method": "ai_magic"}, 400)
        self.post("/api/strategies/save", {"definition": draft, "parent_strategy_id": "STR_000000000000"}, 404)
        err = self.post("/api/strategies/save", {"definition": {"name": "x"}}, 422)["error"]
        self.assertEqual(err["kind"], "validation")
        self.assertTrue(err["issues"])

    def test_archive_is_reversible_and_keeps_lineage(self):
        d = copy.deepcopy(MIN)
        d["name"] = "api_archive"
        d["exit"]["stop"]["points"] = 17
        sid = self.save(d)["strategy_id"]
        self.post(f"/api/strategies/{sid}/archive", {})
        self.assertNotIn(sid, [r["strategy_id"] for r in self.get("/api/strategies")])
        self.assertIn(sid, [r["strategy_id"] for r in self.get("/api/strategies?archived=1")])
        self.assertTrue(self.get(f"/api/strategies/{sid}")["archived"])            # still loadable
        self.assertEqual(self.post("/api/strategies/save", {"definition": d}, 400)["error"]["kind"], "invalid_request")
        self.post(f"/api/strategies/{sid}/restore", {})
        self.assertIn(sid, [r["strategy_id"] for r in self.get("/api/strategies")])

    # ------------------------------------------------------------------ variations / lineage
    def test_variation_preview_generate_and_batches(self):
        d = copy.deepcopy(MIN)
        d["name"] = "api_var_base"
        d["exit"]["stop"]["points"] = 21          # unique logic: names alone never make a new instance
        sid = self.save(d)["strategy_id"]
        spec = {"variation_spec_version": 1, "name": "api_grid", "mode": "grid", "max_variants": 10,
                "dimensions": [{"parameter": "n", "range": {"min": 10, "max": 30, "step": 10}},
                               {"parameter": "flag", "values": [False, True]}]}
        p = self.post("/api/variations/preview", {"base": sid, "spec": spec})
        self.assertEqual((p["ok"], p["combinations"]), (True, 6))
        tight = {**spec, "max_variants": 5}
        p = self.post("/api/variations/preview", {"base": sid, "spec": tight})
        self.assertFalse(p["ok"])
        self.assertIn("exceed max_variants", p["errors"][-1]["message"])
        refused = self.post("/api/variations", {"base": sid, "spec": tight}, 422)["error"]
        self.assertEqual(refused["kind"], "variation")
        bad = {**spec, "dimensions": [{"parameter": "n", "values": [99]}]}
        self.assertFalse(self.post("/api/variations/preview", {"base": sid, "spec": bad})["ok"])   # outside domain
        g = self.post("/api/variations", {"base": sid, "spec": spec})
        self.assertEqual((g["combinations"], g["generated"], g["same_as_base"]), (6, 5, 1))
        self.assertEqual(set(g["variants"][0]["overrides"]), {"n", "flag"})
        batch = self.get(f"/api/variation-batches/{g['batch_id']}")
        self.assertEqual([c["strategy_id"] for c in batch["children_detail"]], [v["strategy_id"] for v in g["variants"]])
        self.assertIn(g["batch_id"], [b["batch_id"] for b in self.get("/api/variation-batches")])
        fam = self.get("/api/families/api_family")
        kids = [n for n in fam["instances"] if sid in n["parents"]]
        self.assertEqual(len(kids), 5)
        self.assertIn(sid, fam["roots"])

    # ------------------------------------------------------------------ datasets / backtests
    def test_datasets_readiness_backtest_and_cfd(self):
        ds = self.get("/api/datasets")
        fut = next(d for d in ds if d["asset_type"] == "FUTURE")
        cfd = next(d for d in ds if d["asset_type"] == "CFD")
        self.assertTrue(fut["synthetic"] and cfd["synthetic"])
        self.assertTrue(fut["runnable"])
        self.assertFalse(cfd["runnable"])
        self.assertIn("unconfigured", " ".join(cfd["reasons"]))
        self.assertTrue(self.get(f"/api/datasets/{fut['dataset_id']}")["limitations"])
        tf15 = {**MIN, "timeframe": "15m"}
        rd = self.post("/api/backtests/readiness", {"strategy": tf15})
        self.assertTrue(all("does not match the strategy timeframe" in " ".join(d["reasons"]) for d in rd["datasets"]))
        bt = self.post("/api/backtests", {"strategy": MIN, "dataset_id": fut["dataset_id"]})
        self.assertTrue(bt["synthetic"])
        self.assertTrue(bt["run_id"].startswith("RUN_"))
        self.assertIn("sample_label", bt["metrics"])
        run = self.get(f"/api/results/{bt['run_id']}")
        self.assertEqual(run["record"]["status"], "IN_SAMPLE")
        self.assertTrue(run["synthetic"])
        self.assertIn(bt["run_id"], [r["run_id"] for r in self.get("/api/results")])
        err = self.post("/api/backtests", {"strategy": MIN, "dataset_id": cfd["dataset_id"]}, 409)["error"]
        self.assertEqual(err["kind"], "cost_unconfigured")
        self.assertIn("unconfigured", err["reason"])
        err = self.post("/api/backtests", {"strategy": tf15, "dataset_id": fut["dataset_id"]}, 422)["error"]
        self.assertEqual(err["kind"], "compile")

    # ------------------------------------------------------------------ security / errors
    def test_inputs_are_data_only(self):
        for src in ("/etc/passwd", "name: x\n", str(FX / "ema_crossover.yaml"), "STR_nothex", 5):
            e = self.post("/api/strategies/validate", {"definition": src}, 400)["error"]
            self.assertEqual(e["kind"], "bad_request")
        self.assertEqual(self.c.post("/api/strategies/validate", data="x").status_code, 400)       # not JSON
        e = self.post("/api/import/inspect", {"path": "../../etc/passwd", "options": {}}, 403)["error"]
        self.assertEqual(e["kind"], "forbidden")
        f = self.get("/api/import/files")["files"][0]["path"]
        self.post("/api/import/inspect", {"path": f, "options": {"evil": 1}}, 400)
        e = self.post("/api/dsl/parse", {"text": "!!python/object/apply:os.system ['echo hi']"}, 422)["error"]
        self.assertEqual(e["kind"], "parse")
        self.assertEqual(self.post("/api/dsl/parse", {"text": "name: x\ntimeframe: 5m"})["definition"]["name"], "x")
        self.assertEqual(self.get("/api/nope", 404)["error"]["kind"], "not_found")
        self.assertEqual(self.get("/api/strategies/STR_000000000000", 404)["error"]["kind"], "not_found")
        self.assertEqual(self.get("/api/results/../../x", 404)["error"]["kind"], "not_found")

    def test_internal_errors_keep_details_out_of_the_message(self):
        svc = self.app.config["EDGELAB"]["services"]
        orig = svc.strategy_families
        svc.strategy_families = lambda: 1 / 0
        try:
            e = self.get("/api/families", 500)["error"]
        finally:
            svc.strategy_families = orig
        self.assertEqual(e["message"], "Unexpected server error.")
        self.assertIn("ZeroDivisionError", e["details"])

    def test_spa_and_static(self):
        self.assertIn(b'id="root"', self.c.get("/").data)
        self.assertIn(b'id="root"', self.c.get("/strategies/anything").data)          # SPA fallback
        self.assertEqual(self.c.get("/app.js").status_code, 200)


class TestWebConfigAndDemo(unittest.TestCase):
    def test_web_config(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / "configs").mkdir()
        self.assertEqual(load_web_config(root).host, "127.0.0.1")                     # defaults without file
        (root / "configs" / "web.yaml").write_text("web: {builder_timeframes: [5m, 7x]}\n")
        with self.assertRaises(WebConfigError):
            load_web_config(root)
        (root / "configs" / "web.yaml").write_text("web: {prot: 1}\n")
        with self.assertRaises(WebConfigError):
            load_web_config(root)
        self.assertTrue(load_web_config(REPO).is_loopback)

    def test_web_config_is_not_research_config(self):
        from edgelab.core.config import CONFIG_FILES
        self.assertNotIn("web.yaml", CONFIG_FILES)

    def test_demo_refuses_real_workspace_and_cli_refuses_remote(self):
        with self.assertRaises(ValueError):
            create_demo_workspace(REPO, REPO)
        from edgelab.web.__main__ import main
        self.assertEqual(main(["--host", "0.0.0.0", "--port", "1"]), 2)

    def test_frontend_bundle_matches_sources(self):
        from edgelab.web.bundle import bundle_status
        s = bundle_status()
        self.assertTrue(s["built"], "run `npm run build` in web/")
        self.assertTrue(s["up_to_date"], "edgelab/web/static is stale: run `npm run build` in web/")

    def test_typescript_typecheck(self):
        tsc = shutil.which("tsc")
        if not tsc:
            try:
                root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, timeout=30).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                root = ""
            cand = Path(root) / "typescript" / "bin" / "tsc"
            tsc = str(cand) if root and cand.exists() else None
        if not tsc:
            self.skipTest("TypeScript not installed")
        r = subprocess.run(["node", tsc, "--noEmit", "-p", "tsconfig.json"] if tsc.endswith("/tsc") and not shutil.which("tsc")
                           else [tsc, "--noEmit", "-p", "tsconfig.json"], cwd=REPO / "web", capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
