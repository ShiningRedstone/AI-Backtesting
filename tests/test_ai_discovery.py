"""Phase 9 Part B: AI-assisted discovery. Request/proposal schemas, the deterministic mock provider, the
strict gate (valid / malformed / causality-invalid / parameter-invalid, request constraints), identity,
blind context, human review, saving with provenance, modification lineage, provider configuration.
No network: the external provider is exercised only with a fake opener."""
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from edgelab.ai import providers as P
from edgelab.ai.context import FORBIDDEN_KEYS, _keys
from edgelab.ai.schema import DiscoveryRequestError
from edgelab.services import Services
from edgelab.strategy.compiler import compile_definition
from tests.phase2_helpers import TEST_FEED
from tests.test_workspace import make_workspace

REQ = {"mode": "hypothesis", "hypothesis": "After an oversold stretch price tends to revert toward its mean.",
       "scope": {"session": "NY_RTH"}}


class AiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        cls.made = make_workspace(cls.root)                   # synthetic runnable dataset (test-local proxy feed) + a run
        cls.did = cls.made["dataset_id"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def svc(self):
        s = Services(root=self.root)
        self.addCleanup(s.store.close)
        return s

    def req(self, **kw):
        r = json.loads(json.dumps(REQ))
        r["scope"]["dataset_id"] = self.did
        for k, v in kw.items():
            if k in ("session", "direction", "dataset_id", "date_scope", "timeframe", "instrument"):
                r["scope"][k] = v
            else:
                r[k] = v
        return r


class TestRequestSchema(AiBase):
    def bad(self, req, needle):
        with self.assertRaises(DiscoveryRequestError) as cm:
            self.svc().ai_generate(req)
        self.assertTrue(any(needle in e for e in cm.exception.errors), cm.exception.errors)

    def test_refusals_are_exact(self):
        self.bad({**self.req(), "expected_return": 3}, "unknown request key 'expected_return'")
        self.bad(self.req(hypothesis="A profitable strategy with a high win rate"), "performance-claim language")
        self.bad(self.req(hypothesis="import os; os.system('x')"), "contains code")
        self.bad(self.req(n_proposals=0), "n_proposals must be an integer in 1..10")
        self.bad(self.req(n_proposals=11), "n_proposals must be an integer in 1..10")
        self.bad(self.req(mode="modify"), "needs base_strategy_id")
        self.bad(self.req(mode="template", template="magic"), "needs template one of")
        self.bad(self.req(session="MOON"), "not a configured session")
        self.bad({**self.req(), "constraints": {"features": ["telepathy"]}}, "unsupported ['telepathy']")
        self.bad({**self.req(), "constraints": {"stop_types": ["trailing"]}}, "unsupported ['trailing']")
        self.bad({**self.req(), "constraints": {"parameter_limits": {"fast": {"min": 9, "max": 3}}}}, "min > max")
        self.bad(self.req(mode="hypothesis", hypothesis=""), "needs a plain-language hypothesis")

    def test_scope_must_agree_with_the_dataset(self):
        from edgelab.ai.context import DiscoveryScopeError
        s = self.svc()
        with self.assertRaises(DiscoveryScopeError):
            s.ai_generate(self.req(timeframe="15m"))
        with self.assertRaises(DiscoveryScopeError):
            s.ai_generate(self.req(date_scope={"start": "1999-01-01", "end": "2000-01-01"}))
        with self.assertRaises(DiscoveryScopeError):          # no dataset and no preferred dataset: never guessed
            r = self.req()
            r["scope"]["dataset_id"] = None
            s.ai_generate(r)


class TestMockAndGate(AiBase):
    def test_mock_is_deterministic_and_covers_the_four_outcomes(self):
        s = self.svc()
        g1, g2 = s.ai_generate(self.req()), s.ai_generate(self.req())
        self.assertEqual(g1["generation_id"], g2["generation_id"])
        self.assertEqual([p["proposal_id"] for p in g1["proposals"]], [p["proposal_id"] for p in g2["proposals"]])
        self.assertEqual([p["content"] for p in g1["proposals"]], [p["content"] for p in g2["proposals"]])
        valid, malformed, causal, param = g1["proposals"]
        failed = lambda p: [x["stage"] for x in p["gate"]["stages"] if x["status"] == "failed"]
        self.assertEqual(valid["gate"]["status"], "valid")
        self.assertTrue(all(x["status"] == "passed" for x in valid["gate"]["stages"]))
        self.assertEqual(failed(malformed), ["schema"])
        reasons = " | ".join(malformed["gate"]["rejection_reasons"])
        self.assertIn("unknown key 'expected_profit_factor'", reasons)
        self.assertIn("contains code", reasons)
        self.assertEqual(failed(causal), ["causality"])
        self.assertIn("looks into the future (non-causal)", causal["gate"]["rejection_reasons"][0])
        self.assertEqual(failed(param), ["parameter_domain"])
        self.assertRegex(param["gate"]["rejection_reasons"][0], r"parameter_domain: parameters\.\w+\.value: value .* outside \[")
        for p in (malformed, causal, param):
            self.assertIsNone(p["gate"]["identity"])
            later = [x["status"] for x in p["gate"]["stages"]][-2:]
            self.assertEqual(later, ["not_run", "not_run"])                   # compile/identity never run on a reject
        self.assertEqual(g1["scope"]["dataset_id"], self.did)
        self.assertEqual(g1["provider"]["kind"], "mock")
        # the mock never mentions performance; there is no ranking or score anywhere in the record
        text = json.dumps(g1).lower()
        for word in ("score", "rank", "best", "win_rate"):
            self.assertNotIn(f'"{word}', text)

    def test_identity_is_the_compiler_identity(self):
        s = self.svc()
        v = s.ai_generate(self.req())["proposals"][0]
        cc = compile_definition(v["content"]["definition"], s.sessions, s._config_hash())
        self.assertEqual(v["gate"]["identity"], cc.identity.to_dict())
        self.assertTrue(v["gate"]["identity"]["strategy_id"].startswith("STR_"))

    def test_request_constraints_reject_with_reasons(self):
        s = self.svc()
        for cons, needle in (({"features": ["ema"]}, "outside the requested set"),
                             ({"max_conditions": 0}, None),
                             ({"stop_types": ["points"]}, "stop type 'atr' is outside"),
                             ({"parameter_limits": {"rsi_len": {"max": 10}}}, "above the requested limit 10")):
            if needle is None:
                with self.assertRaises(DiscoveryRequestError):
                    s.ai_generate({**self.req(n_proposals=1), "constraints": cons})
                continue
            p = s.ai_generate({**self.req(n_proposals=1), "constraints": cons})["proposals"][0]
            self.assertEqual(p["gate"]["status"], "rejected")
            self.assertTrue(any(needle in r for r in p["gate"]["rejection_reasons"]), p["gate"]["rejection_reasons"])
        p = s.ai_generate(self.req(n_proposals=1, direction="long", session="NY_0930_1030"))["proposals"][0]
        self.assertEqual(p["gate"]["status"], "valid")
        self.assertEqual((p["gate"]["summary"]["direction"], p["gate"]["summary"]["session"]), ("long", "NY_0930_1030"))

    def test_gate_refuses_disagreeing_or_repaired_content(self):
        from edgelab.ai.gate import gate_proposal
        s = self.svc()
        g = s.ai_generate(self.req(n_proposals=1))
        content = json.loads(json.dumps(g["proposals"][0]["content"]))
        req = g["request"]
        k = sorted(content["parameters"])[0]
        content["parameters"][k] = content["parameters"][k] + 1               # claims a value the definition does not have
        r = gate_proposal(content, req, g["scope"], s.sessions, s._config_hash())
        self.assertEqual(r["status"], "rejected")
        self.assertIn("disagrees with the definition value", r["rejection_reasons"][0])
        c2 = json.loads(json.dumps(g["proposals"][0]["content"]))
        c2["rationale"] = "This is a proven, profitable edge."
        r = gate_proposal(c2, req, g["scope"], s.sessions, s._config_hash())
        self.assertIn("performance claim language", " ".join(r["rejection_reasons"]))
        self.assertEqual(gate_proposal("def f(): pass", req, g["scope"], s.sessions, None)["status"], "rejected")


class TestBlindContext(AiBase):
    def test_context_carries_no_results_and_does_not_change_with_runs(self):
        s = self.svc()
        ctx1 = s.ai_context(self.req())
        self.assertFalse(_keys(ctx1) & FORBIDDEN_KEYS)
        self.assertNotIn(self.made["run_id"], json.dumps(ctx1))
        s.backtest_strategy(s.library.load(self.made["strategy_id"])["definition"], self.did, record=True)
        s.random_entry_control(self.made["strategy_id"], self.did, n_controls=2, seed=1)
        ctx2 = s.ai_context(self.req())
        self.assertEqual(ctx1["context_hash"], ctx2["context_hash"])        # results never reach the context
        self.assertEqual(ctx1["context_version"], 1)
        self.assertTrue(ctx1["dsl"]["features"])
        self.assertIn("NY_RTH", ctx1["dsl"]["sessions"])

    def test_generation_uses_exactly_that_context(self):
        s = self.svc()
        g = s.ai_generate(self.req())
        self.assertEqual(g["context_hash"], s.ai_context(self.req())["context_hash"])


class TestReviewSaveAndLineage(AiBase):
    def test_accept_save_backtest_and_lineage(self):
        s = self.svc()
        dup = s.ai_generate(self.req(template="ema_trend", mode="template", hypothesis="", n_proposals=1))["proposals"][0]
        self.assertTrue(any("already in the library" in w for w in dup["gate"]["warnings"]))  # same logic as the EMA fixture
        g = s.ai_generate(self.req(template="range_breakout", mode="template", hypothesis=""))
        valid, malformed = g["proposals"][0], g["proposals"][1]
        with self.assertRaises(ValueError):
            s.ai_save(valid["proposal_id"])                                  # must be accepted first
        with self.assertRaises(ValueError):
            s.ai_decide(malformed["proposal_id"], "accepted")               # a gate reject cannot be accepted
        s.ai_decide(malformed["proposal_id"], "rejected", "malformed")
        s.ai_decide(valid["proposal_id"], "accepted", "reviewed")
        out = s.ai_save(valid["proposal_id"])
        sid = out["strategy_id"]
        self.assertTrue(out["created"])
        self.assertEqual(sid, valid["gate"]["identity"]["strategy_id"])
        doc = s.library.load(sid)
        rec = doc["lineage"][0]
        self.assertEqual(rec["generation_method"], "mode_b_proposal")
        gp = rec["generation_parameters"]
        self.assertEqual((gp["proposal_id"], gp["request_id"], gp["generation_id"]),
                         (valid["proposal_id"], valid["request_id"], valid["generation_id"]))
        self.assertEqual(gp["context_hash"], g["context_hash"])
        self.assertEqual(gp["provider"]["kind"], "mock")
        self.assertEqual(doc["definition_hash"], valid["gate"]["identity"]["definition_hash"])
        self.assertNotIn("api_key", json.dumps(doc).lower())
        gen = s.ai_generation(g["generation_id"])
        self.assertEqual(gen["proposals"][0]["decision"]["saved_strategy_id"], sid)
        self.assertEqual(gen["proposals"][1]["decision"]["decision"], "rejected")
        # hand-off to the EXISTING engine, then lineage AI request -> proposal -> strategy -> run
        bt = s.backtest_strategy(sid, self.did, record=True)
        lin = s.ai_lineage(valid["proposal_id"])
        self.assertEqual(lin["request"]["request_id"], valid["request_id"])
        self.assertEqual(lin["strategy"]["strategy_id"], sid)
        run = next(r for r in lin["runs"] if r["run_id"] == bt["run_id"])
        self.assertEqual((run["dataset_id"], run["provider"], run["cost_status"]), (self.did, TEST_FEED, "assumed"))
        self.assertEqual(run["definition_hash"], doc["definition_hash"])
        self.assertEqual(run["status"], "IN_SAMPLE")
        with self.assertRaises(ValueError):
            s.ai_decide(valid["proposal_id"], "rejected")                  # saved: archive the strategy instead

    def test_modification_is_a_new_version_with_parent(self):
        s = self.svc()
        base = self.made["strategy_id"]
        parent_file = next((self.root / "data" / "strategy_library" / "instances").glob(f"{base}*.json"))
        parent_bytes = parent_file.read_bytes()
        g = s.ai_generate({"mode": "modify", "base_strategy_id": base, "n_proposals": 1,
                           "scope": {"dataset_id": self.did, "session": "NY_0930_1030"}})
        p = g["proposals"][0]
        self.assertEqual(p["gate"]["status"], "valid", p["gate"]["rejection_reasons"])
        self.assertEqual(p["parent"]["strategy_id"], base)
        self.assertIn("entry.session: 'NY_RTH' -> 'NY_0930_1030'", p["gate"]["summary"]["computed_changes"])
        s.ai_decide(p["proposal_id"], "accepted")
        out = s.ai_save(p["proposal_id"])
        self.assertNotEqual(out["strategy_id"], base)
        rec = s.library.load(out["strategy_id"])["lineage"][0]
        self.assertEqual((rec["generation_method"], rec["parent_strategy_id"]), ("mode_b_modification", base))
        self.assertEqual(rec["generation_parameters"]["parent_definition_hash"], s.library.load(base)["definition_hash"])
        cats = {c["category"] for c in rec["changes"]}
        self.assertEqual(cats, {"ai_stated_change", "computed_change"})
        self.assertEqual(parent_file.read_bytes(), parent_bytes)            # the original is never changed
        self.assertIn(base, [a["strategy_id"] for a in s.library.ancestry(out["strategy_id"])])

    def test_identical_to_parent_is_refused(self):
        from edgelab.ai.gate import gate_proposal
        s = self.svc()
        base = self.made["strategy_id"]
        doc = s.library.load(base)
        g = s.ai_generate({"mode": "modify", "base_strategy_id": base, "n_proposals": 1,
                           "scope": {"dataset_id": self.did, "session": "NY_RTH"}})
        req, scope = g["request"], g["scope"]
        d = json.loads(json.dumps(doc["definition"]))
        fam = {k: d["family"][k] for k in ("id", "name", "category") if k in d["family"]}
        content = {"hypothesis": "same", "strategy_name": d["name"], "family": fam, "definition": d,
                   "parameters": {k: v["value"] for k, v in d["parameters"].items()}, "timeframe": d["timeframe"],
                   "session": d["entry"].get("session"), "rationale": "unchanged", "changes": ["none"]}
        parent = {"strategy_id": base, "logic_hash": doc["logic_hash"], "definition_hash": doc["definition_hash"],
                  "definition": doc["definition"]}
        r = gate_proposal(content, req, scope, s.sessions, s._config_hash(), parent)
        self.assertEqual(r["status"], "rejected")
        self.assertIn("identical logic to the base strategy", r["rejection_reasons"][0])


class TestProviderConfig(unittest.TestCase):
    def test_offline_by_default_and_key_never_exposed(self):
        st = P.provider_status(environ={})
        self.assertEqual((st["configured_provider"], st["external_configured"], st["available"]), ("none", False, ["mock"]))
        self.assertIn("offline", st["offline_notice"])
        env = {P.PROVIDER_ENV: "anthropic", P.KEY_ENV: "sk-secret-value"}
        st = P.provider_status(environ=env)
        self.assertIn(P.MODEL_ENV, st["problem"])                            # no model is assumed
        self.assertNotIn("sk-secret-value", json.dumps(st))
        env[P.MODEL_ENV] = "some-model"
        st = P.provider_status(environ=env)
        self.assertTrue(st["external_configured"])
        self.assertEqual(st["available"], ["mock", "anthropic"])
        prov = P.get_provider("anthropic", environ=env)
        self.assertNotIn("sk-secret-value", json.dumps(prov.describe()))
        with self.assertRaises(P.ProviderError):
            P.get_provider("anthropic", environ={})
        self.assertIsInstance(P.get_provider(None, environ={}), P.MockProvider)

    def test_external_provider_output_goes_through_the_gate_unrepaired(self):
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout):
            seen["headers"] = dict(req.headers)
            seen["body"] = json.loads(req.data)
            return Resp(json.dumps({"content": [{"type": "text", "text": "Here you go: not json at all"}]}).encode())

        prov = P.AnthropicProvider("some-model", "sk-secret", opener=opener)
        out = prov.generate_proposals({"mode": "hypothesis", "hypothesis": "h", "template": None, "n_proposals": 2,
                                       "constraints": {}}, {"scope": {}, "context_hash": "x"})
        self.assertEqual(out.proposals, [{"unparsed_output": "Here you go: not json at all"}])
        self.assertEqual(seen["headers"]["X-api-key"], "sk-secret")          # sent only to the provider
        self.assertNotIn("sk-secret", json.dumps(seen["body"]))
        self.assertEqual(seen["body"]["model"], "some-model")

        def failing(req, timeout):
            raise OSError("network down sk-secret")
        with self.assertRaises(P.ProviderError) as cm:
            P.AnthropicProvider("m", "sk-secret", opener=failing).generate_proposals(
                {"mode": "hypothesis", "hypothesis": "h", "template": None, "n_proposals": 1, "constraints": {}}, {})
        self.assertNotIn("sk-secret", str(cm.exception))


class TestAiWebApi(AiBase):
    def test_routes(self):
        from edgelab.web.app import create_app
        c = create_app(self.root).test_client()
        st = c.get("/api/ai/status").get_json()
        self.assertEqual(st["available"][0], "mock")
        r = c.post("/api/ai/generate", json={"request": self.req()})
        self.assertEqual(r.status_code, 200, r.get_json())
        g = r.get_json()
        pid = g["proposals"][0]["proposal_id"]
        self.assertEqual(c.get("/api/ai/generations").get_json()[0]["generation_id"], g["generation_id"])
        self.assertEqual(c.post(f"/api/ai/proposals/{pid}/save", json={}).status_code, 400)
        self.assertEqual(c.post(f"/api/ai/proposals/{pid}/decision", json={"decision": "accepted"}).status_code, 200)
        sv = c.post(f"/api/ai/proposals/{pid}/save", json={})
        self.assertEqual(sv.status_code, 200, sv.get_json())
        self.assertEqual(c.get(f"/api/ai/proposals/{pid}/lineage").get_json()["strategy"]["strategy_id"], sv.get_json()["strategy_id"])
        bad = c.post("/api/ai/generate", json={"request": {**self.req(), "hypothesis": "guaranteed profitable"}})
        self.assertEqual((bad.status_code, bad.get_json()["error"]["kind"]), (422, "ai_request"))
        self.assertEqual(c.get("/api/ai/generations/nope").status_code, 400)
        ctx = c.post("/api/ai/context", json={"request": self.req()}).get_json()
        self.assertFalse(_keys(ctx) & FORBIDDEN_KEYS)


if __name__ == "__main__":
    unittest.main()
