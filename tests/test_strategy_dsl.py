"""Phase 3: DSL parsing, validation (schema / logic / architecture / causality), parameters,
canonicalization and identity."""
import copy
import json
import unittest

import yaml

from edgelab.features.sessions import SessionWindow
from edgelab.strategy.dsl import (StrategyValidationError, canonical_definition, check_parameter, identity,
                                  load_definition, require_valid, validate)
from tests.phase2_helpers import SESSIONS

EMA20 = {"feature": "ema", "params": {"period": 20}, "output": "ema"}


def base_doc(**over):
    d = {"dsl_version": 1, "name": "t", "timeframe": "5m",
         "entry": {"direction": "long", "long": {"left": {"bar": "close"}, "op": ">", "right": copy.deepcopy(EMA20)}},
         "exit": {"stop": {"type": "points", "points": 10.0}}}
    for k, v in over.items():
        d[k] = v
    return d


def errors(doc, sessions=SESSIONS):
    return validate(doc, sessions).errors


def paths(doc):
    return [e.path for e in errors(doc)]


def ident(doc):
    return identity(require_valid(doc, SESSIONS), SESSIONS)


class TestParsing(unittest.TestCase):
    def test_minimal_valid_and_formats(self):
        d = base_doc()
        self.assertTrue(validate(d, SESSIONS).valid)
        self.assertEqual(load_definition(yaml.safe_dump(d)), d)                 # YAML text
        self.assertEqual(ident(json.loads(json.dumps(d))), ident(d))            # JSON round trip

    def test_fixtures_are_valid(self):
        import glob
        for f in glob.glob("strategies/fixtures/*.yaml"):
            if "variations" in f or "proposals" in f:
                continue
            r = validate(f, SESSIONS)
            self.assertTrue(r.valid, f + "\n" + r.report())

    def test_required_blocks(self):
        d = base_doc()
        del d["entry"]
        self.assertIn("entry", paths(d))
        d = base_doc(exit={})
        self.assertIn("exit.stop", paths(d))
        d = base_doc()
        del d["name"]
        self.assertIn("name", paths(d))
        self.assertIn("timeframe", paths(base_doc(timeframe="7x")))
        self.assertIn("dsl_version", paths(base_doc(dsl_version=2)))

    def test_unknown_keys_get_suggestions(self):
        e = errors(base_doc(exitt={}))
        self.assertEqual(e[0].path, "exitt")
        self.assertIn("exit", e[0].hint)

    def test_invalid_feature_output_param(self):
        d = base_doc()
        d["entry"]["long"]["right"] = {"feature": "ema_fastest", "output": "ema"}
        e = errors(d)[0]
        self.assertEqual(e.path, "entry.long.right.feature")
        self.assertIn("ema", e.hint)
        d["entry"]["long"]["right"] = {"feature": "ema", "output": "value"}
        self.assertEqual(paths(d), ["entry.long.right.output"])
        d["entry"]["long"]["right"] = {"feature": "fvg"}                          # several outputs
        self.assertEqual(paths(d), ["entry.long.right.output"])
        d["entry"]["long"]["right"] = {"feature": "ema", "params": {"period": 0}, "output": "ema"}
        self.assertTrue(errors(d))

    def test_invalid_operator(self):
        d = base_doc()
        d["entry"]["long"]["op"] = "=>"
        self.assertEqual(paths(d), ["entry.long.op"])


class TestLogicalRules(unittest.TestCase):
    def test_contradictions(self):
        d = base_doc()
        d["entry"]["short"] = copy.deepcopy(d["entry"]["long"])
        self.assertIn("entry.short", paths(d))                                   # short cond, long only
        d = base_doc()
        d["entry"]["long"] = {"left": 1, "op": ">", "right": 2}
        self.assertIn("constants", errors(d)[0].message)
        d["entry"]["long"] = {"left": EMA20, "op": ">", "right": copy.deepcopy(EMA20)}
        self.assertIn("itself", errors(d)[0].message)
        d = base_doc()
        d["exit"]["signal"] = {"short": d["entry"]["long"]}
        self.assertIn("exit.signal.short", paths(d))

    def test_empty_conditions(self):
        d = base_doc()
        d["entry"]["long"] = {"any": []}
        self.assertTrue(errors(d))
        d = base_doc(parameters={"on": {"type": "boolean", "value": False}})
        d["entry"]["long"] = {"all": [{**d["entry"]["long"], "enabled": "$on"}]}
        self.assertIn("every bar", errors(d)[0].message)

    def test_entry_orders(self):
        d = base_doc()
        d["entry"]["order"] = {"type": "stop"}
        self.assertIn("entry.order.long_price", paths(d))
        d["entry"]["order"] = {"type": "limit", "long_price": {"bar": "low"}, "expiry_bars": 0}
        self.assertIn("entry.order.expiry_bars", paths(d))
        d["entry"]["order"] = {"type": "market", "long_price": {"bar": "low"}}
        self.assertIn("entry.order.long_price", paths(d))
        d["entry"]["order"] = {"type": "iceberg"}
        self.assertIn("entry.order.type", paths(d))

    def test_stops_targets_sizing(self):
        self.assertIn("exit.stop.points", paths(base_doc(exit={"stop": {"type": "points", "points": -1}})))
        self.assertIn("exit.stop.long", paths(base_doc(exit={"stop": {"type": "price"}})))
        self.assertIn("exit.target.multiple", paths(base_doc(exit={"stop": {"type": "points", "points": 5},
                                                                   "target": {"type": "risk_reward"}})))
        self.assertIn("exit.time_stop_bars", paths(base_doc(exit={"stop": {"type": "points", "points": 5},
                                                                  "time_stop_bars": 0})))
        self.assertIn("sizing.mode", paths(base_doc(sizing={"mode": "kelly"})))
        self.assertIn("sizing.quantity", paths(base_doc(sizing={"mode": "fixed", "quantity": 0})))
        self.assertIn("sizing.risk_usd", paths(base_doc(sizing={"mode": "risk"})))

    def test_sessions(self):
        d = base_doc()
        d["entry"]["session"] = "NY_LUNCH"
        self.assertIn("entry.session", paths(d))
        d["sessions"] = {"NY_LUNCH": {"timezone": "America/New_York", "start": "12:00", "end": "13:00"}}
        self.assertTrue(validate(d, SESSIONS).valid)                              # strategy-local window
        d["sessions"] = {"NY_RTH": {"timezone": "America/New_York", "start": "09:00", "end": "16:00"}}
        d["entry"]["session"] = "NY_RTH"
        self.assertIn("sessions.NY_RTH", paths(d))                               # conflicts with config


class TestArchitectureAndCausalityRules(unittest.TestCase):
    def test_unsupported_engine_features_are_named(self):
        for block, key in (("exit", "trailing_stop"), ("exit", "partial_exits"), ("entry", "pyramiding"),
                           ("entry", "cooldown_after_exit")):
            d = base_doc()
            d[block][key] = {"x": 1}
            e = errors(d)
            self.assertEqual(e[0].path, f"{block}.{key}")
            self.assertIn("not supported", e[0].message)

    def test_future_references_rejected(self):
        d = base_doc()
        d["entry"]["long"]["left"] = {"bar": "close", "lag": -1}
        e = errors(d)[0]
        self.assertEqual(e.path, "entry.long.left.lag")
        self.assertIn("non-causal", e.message)
        d["entry"]["long"]["left"] = {"bar": "close", "lead": 1}
        self.assertIn("future", errors(d)[0].message)

    def test_lower_or_non_multiple_timeframes_rejected(self):
        d = base_doc()
        d["entry"]["long"]["right"] = {**EMA20, "timeframe": "1m"}
        self.assertIn("entry.long.right.timeframe", paths(d))
        d["entry"]["long"]["right"] = {**EMA20, "timeframe": "7m"}
        self.assertIn("entry.long.right.timeframe", paths(d))

    def test_non_causal_feature_rejected(self):
        from edgelab.features.spec import REGISTRY, FeatureDef, register
        register(FeatureDef("zz_lookahead_test", 1, "test", (), (("x", "leaky"),), lambda i, p: {},
                            "t", "t", "t", "0", causal=False))
        try:
            d = base_doc()
            d["entry"]["long"]["right"] = {"feature": "zz_lookahead_test", "output": "x"}
            e = errors(d)
            self.assertEqual(len(e), 1)
            self.assertIn("is not causal", e[0].message)          # rejected for the RIGHT reason
        finally:
            REGISTRY.pop("zz_lookahead_test", None)


class TestParameters(unittest.TestCase):
    def check(self, decl):
        return check_parameter("p", decl, "parameters.p")

    def test_valid_declarations(self):
        for decl in ({"type": "integer", "value": 20, "min": 10, "max": 50, "step": 5},
                     {"type": "float", "value": 1.0, "min": 0.5, "max": 2.0, "step": 0.25},
                     {"type": "boolean", "value": True},
                     {"type": "choice", "value": "a", "choices": ["a", "b"]},
                     {"type": "timeframe", "value": "15m", "choices": ["5m", "15m"]}):
            self.assertEqual(self.check(decl), [], decl)

    def test_invalid_declarations(self):
        bad = {"bad type": {"type": "number", "value": 1},
               "min > max": {"type": "float", "value": 1.0, "min": 3, "max": 2},
               "out of range": {"type": "integer", "value": 60, "min": 10, "max": 50},
               "step <= 0": {"type": "float", "value": 1.0, "min": 0.5, "max": 2.0, "step": 0},
               "step does not divide": {"type": "float", "value": 1.0, "min": 0.5, "max": 2.0, "step": 0.4},
               "value off grid": {"type": "float", "value": 1.1, "min": 0.5, "max": 2.0, "step": 0.25},
               "step without bounds": {"type": "integer", "value": 5, "step": 1},
               "float step on integer": {"type": "integer", "value": 10, "min": 10, "max": 20, "step": 2.5},
               "float for integer": {"type": "integer", "value": 2.5},
               "boolean with min": {"type": "boolean", "value": True, "min": 0},
               "choice without choices": {"type": "choice", "value": "a"},
               "value not in choices": {"type": "choice", "value": "c", "choices": ["a", "b"]},
               "bad timeframe": {"type": "timeframe", "value": "7x"},
               "missing value": {"type": "float"},
               "unknown key": {"type": "float", "value": 1.0, "maximum": 3}}
        for label, decl in bad.items():
            self.assertTrue(self.check(decl), label)

    def test_references(self):
        d = base_doc()
        d["entry"]["long"]["right"] = {"feature": "ema", "params": {"period": "$n"}, "output": "ema"}
        self.assertIn("entry.long.right.params.period", paths(d))                # undeclared $n
        d["parameters"] = {"n": {"type": "integer", "value": 20}}
        self.assertTrue(validate(d, SESSIONS).valid)
        d["parameters"]["unused"] = {"type": "float", "value": 1.0}
        self.assertEqual([w.path for w in validate(d, SESSIONS).warnings], ["parameters.unused"])
        d["parameters"]["n"] = {"type": "float", "value": 20.5}                  # wrong type for the slot
        self.assertFalse(validate(d, SESSIONS).valid)
        self.assertTrue(errors(base_doc(parameters={"Bad-Name": {"type": "float", "value": 1.0}})))


class TestCanonicalIdentity(unittest.TestCase):
    def test_key_order_and_formatting_do_not_matter(self):
        d = base_doc(exit={"target": {"multiple": 2, "type": "risk_reward"}, "stop": {"points": 10, "type": "points"}})
        reordered = json.loads(json.dumps(d, sort_keys=True))
        reordered["exit"]["target"]["multiple"] = 2.0
        self.assertEqual(ident(d), ident(reordered))

    def test_semantic_equivalences(self):
        a = base_doc()
        b = base_doc()
        b["entry"]["long"] = {"left": copy.deepcopy(EMA20), "op": "<", "right": {"bar": "close"}}   # flipped
        self.assertEqual(ident(a).logic_hash, ident(b).logic_hash)
        c = base_doc()
        c["entry"]["long"]["right"] = {"feature": "ema", "params": {"period": 20, "slope_bars": 5}, "output": "ema"}
        self.assertEqual(ident(a).logic_hash, ident(c).logic_hash)                # defaults explicit
        x = {"left": {"bar": "close"}, "op": ">", "right": 1.0}
        y = {"left": {"bar": "open", "lag": 0}, "op": ">", "right": 2}
        p = base_doc(); p["entry"]["long"] = {"all": [x, y]}
        q = base_doc(); q["entry"]["long"] = {"all": [y, {**x, "label": "renamed"}]}
        self.assertEqual(ident(p).logic_hash, ident(q).logic_hash)               # order, labels
        up = base_doc(); up["entry"]["long"] = {"left": EMA20, "op": "crosses_above", "right": {"bar": "close"}}
        dn = base_doc(); dn["entry"]["long"] = {"left": {"bar": "close"}, "op": "crosses_below", "right": EMA20}
        self.assertEqual(ident(up).logic_hash, ident(dn).logic_hash)
        h1 = base_doc(); h1["entry"]["long"]["right"] = {**EMA20, "timeframe": "1h"}
        h2 = base_doc(); h2["entry"]["long"]["right"] = {**EMA20, "timeframe": "60m"}
        self.assertEqual(ident(h1).logic_hash, ident(h2).logic_hash)

    def test_parameters_and_metadata_affect_only_the_definition_hash(self):
        lit = base_doc()
        ref = base_doc(parameters={"n": {"type": "integer", "value": 20, "min": 5, "max": 50, "step": 5}})
        ref["entry"]["long"]["right"] = {"feature": "ema", "params": {"period": "$n"}, "output": "ema"}
        a, b = ident(lit), ident(ref)
        self.assertEqual(a.logic_hash, b.logic_hash)
        self.assertNotEqual(a.definition_hash, b.definition_hash)
        renamed = base_doc(name="other", description="changed words")
        self.assertEqual(ident(renamed).logic_hash, a.logic_hash)
        self.assertNotEqual(ident(renamed).definition_hash, a.definition_hash)

    def test_meaningful_changes_change_identity(self):
        base = ident(base_doc()).logic_hash
        variants = {}
        d = base_doc(); d["entry"]["long"]["op"] = ">="; variants["entry operator"] = d
        d = base_doc(); d["entry"]["long"]["right"]["params"]["period"] = 21; variants["feature param"] = d
        d = base_doc(); d["entry"]["long"]["right"]["timeframe"] = "15m"; variants["feature timeframe"] = d
        variants["strategy timeframe"] = base_doc(timeframe="15m")
        d = base_doc(); d["exit"]["stop"]["points"] = 11.0; variants["stop"] = d
        d = base_doc(); d["exit"]["time_stop_bars"] = 5; variants["time stop"] = d
        d = base_doc(); d["entry"]["session"] = "NY_AM"; variants["session"] = d
        variants["sizing"] = base_doc(sizing={"mode": "fixed", "quantity": 2})
        d = base_doc(); d["entry"]["direction"] = "short"; d["entry"]["short"] = d["entry"].pop("long")
        variants["direction"] = d
        hashes = {k: ident(v).logic_hash for k, v in variants.items()}
        for k, h in hashes.items():
            self.assertNotEqual(h, base, k)
        self.assertEqual(len(set(hashes.values())), len(hashes))

    def test_changed_session_definition_changes_identity(self):
        d = base_doc()
        d["entry"]["session"] = "NY_AM"
        other = {**SESSIONS, "NY_AM": SessionWindow("NY_AM", "America/New_York", "09:30", "11:30")}
        a = identity(d, SESSIONS).logic_hash
        b = identity(d, other).logic_hash
        self.assertNotEqual(a, b)

    def test_canonical_definition_is_stable_and_serialisable(self):
        c = canonical_definition(require_valid("strategies/fixtures/opening_range_breakout.yaml", SESSIONS))
        self.assertEqual(json.loads(json.dumps(c)), c)
        self.assertEqual(ident(c).logic_hash, ident("strategies/fixtures/opening_range_breakout.yaml").logic_hash)

    def test_stored_definitions_pin_feature_versions(self):
        c = canonical_definition(require_valid(base_doc(), SESSIONS))
        self.assertEqual(c["entry"]["long"]["right"]["version"], 1)
        c["entry"]["long"]["right"]["version"] = 2                 # a future/changed feature version
        e = errors(c)[0]
        self.assertIn("only v1 is implemented", e.message)

    def test_error_report_format(self):
        d = base_doc()
        d["entry"]["long"]["right"] = {"feature": "ema_fastest", "output": "ema"}
        with self.assertRaises(StrategyValidationError) as cm:
            require_valid(d, SESSIONS)
        text = str(cm.exception)
        self.assertIn("Strategy validation failed", text)
        self.assertIn("entry.long.right.feature:", text)
        self.assertIn("unknown feature 'ema_fastest'", text)
        self.assertIn("did you mean: ema", text)


if __name__ == "__main__":
    unittest.main()
