"""Phase 3: Mode A controlled variations, lineage library, Mode B proposal ingestion."""
import copy
import tempfile
import unittest

from edgelab.strategy.dsl import StrategyValidationError, identity, load_definition, require_valid
from edgelab.strategy.lineage import Change, LineageRecord, StrategyLibrary
from edgelab.strategy.proposals import ProposalRequest, StaticProposer, claim_phrases, ingest_proposals
from edgelab.strategy.variations import VariationError, combination_count, generate_variations, validate_spec
from tests.phase2_helpers import SESSIONS

ORB = "strategies/fixtures/opening_range_breakout.yaml"
ORB_SPEC = "strategies/fixtures/orb_variations.yaml"
PROPOSALS = "strategies/fixtures/proposals_example.yaml"


def spec(*dims, **kw):
    return {"variation_spec_version": 1, "name": "t", "mode": "grid", "dimensions": list(dims), **kw}


class TestVariations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.batch = generate_variations(ORB, ORB_SPEC, SESSIONS)
        cls.base = load_definition(ORB)

    def test_counts_and_dedupe(self):
        b = self.batch
        self.assertEqual(b.record["combinations"], 48)
        self.assertEqual((len(b.variants), len(b.duplicates), len(b.same_as_base)), (35, 11, 2))
        ids = [v.strategy_id for v in b.variants]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn(b.base_strategy_id, ids)
        # every duplicate is a volume threshold varied while the filter is off
        self.assertTrue(all(d["combination"]["use_volume_filter"] is False for d in b.duplicates))

    def test_children_are_valid_and_recorded(self):
        for v in self.batch.variants:
            self.assertTrue(require_valid(v.definition, SESSIONS))
            self.assertEqual(identity(v.definition, SESSIONS).logic_hash, v.identity["logic_hash"])
            rec = v.lineage
            self.assertEqual(rec.generation_method, "mode_a_variation")
            self.assertEqual(rec.parent_strategy_id, self.batch.base_strategy_id)
            self.assertEqual(rec.generation_batch_id, self.batch.batch_id)
            self.assertEqual(rec.family_id, "ny_opening_range_breakout")
            self.assertTrue(rec.changes)
            for c in rec.changes:
                self.assertIsInstance(c, Change)
                self.assertEqual(self.base["parameters"][c.parameter]["value"], c.old)
                self.assertEqual(v.definition["parameters"][c.parameter]["value"], c.new)

    def test_reproducible_from_the_batch_record(self):
        rec = self.batch.record
        again = generate_variations(rec["base"]["definition"], rec["spec"], SESSIONS)
        self.assertEqual(again.batch_id, self.batch.batch_id)
        self.assertEqual([v.strategy_id for v in again.variants], rec["children"])

    def test_max_variants_is_enforced_loudly(self):
        s = load_definition(ORB_SPEC)
        s["max_variants"] = 47
        with self.assertRaises(VariationError) as cm:
            generate_variations(ORB, s, SESSIONS)
        self.assertIn("48", str(cm.exception))

    def test_spec_validation(self):
        base = load_definition(ORB)
        cases = {
            "undeclared parameter": spec({"parameter": "atr_mult", "values": [1, 2]}),
            "outside declared domain": spec({"parameter": "rr", "values": [2.0, 5.0]}),
            "off the declared grid": spec({"parameter": "rr", "values": [1.25]}),
            "unknown choice": spec({"parameter": "or_window", "values": ["OR_0930_1100"]}),
            "range on a choice": spec({"parameter": "or_window", "range": {"min": 1, "max": 2, "step": 1}}),
            "zero step": spec({"parameter": "rr", "range": {"min": 1.0, "max": 3.0, "step": 0}}),
            "step not dividing": spec({"parameter": "rr", "range": {"min": 1.0, "max": 3.0, "step": 0.75}}),
            "values and range": spec({"parameter": "rr", "values": [2.0], "range": {"min": 1, "max": 2, "step": 1}}),
            "duplicate values": spec({"parameter": "rr", "values": [2.0, 2.0]}),
            "duplicate dimension": spec({"parameter": "rr", "values": [2.0]}, {"parameter": "rr", "values": [1.5]}),
            "unknown key": spec({"parameter": "rr", "values": [2.0]}, limit=5),
            "random without seed": spec({"parameter": "rr", "values": [2.0]}, mode="random_sample", sample_size=1),
            "bad mode": spec({"parameter": "rr", "values": [2.0]}, mode="genetic"),
            "no dimensions": spec(),
        }
        for label, s in cases.items():
            self.assertFalse(validate_spec(s, base).valid, label)
            with self.assertRaises((StrategyValidationError, VariationError)):
                generate_variations(base, s, SESSIONS)

    def test_one_at_a_time_and_random_sample(self):
        s = spec({"parameter": "rr", "values": [1.5, 2.0, 2.5]}, {"parameter": "use_volume_filter", "values": [False, True]},
                 mode="one_at_a_time")
        self.assertEqual(combination_count(s), 5)
        b = generate_variations(ORB, s, SESSIONS)
        self.assertEqual(len(b.variants), 3)                                   # base values skipped
        self.assertTrue(all(len(v.lineage.changes) == 1 for v in b.variants))
        r = spec({"parameter": "rr", "range": {"min": 1.0, "max": 3.0, "step": 0.5}},
                 {"parameter": "or_window", "values": ["OR_0930_0945", "OR_0930_1000", "OR_0930_1030"]},
                 mode="random_sample", seed=11, sample_size=6)
        a1, a2 = generate_variations(ORB, r, SESSIONS), generate_variations(ORB, r, SESSIONS)
        self.assertEqual([v.strategy_id for v in a1.variants], [v.strategy_id for v in a2.variants])
        r2 = copy.deepcopy(r)
        r2["seed"] = 12
        self.assertNotEqual(a1.batch_id, generate_variations(ORB, r2, SESSIONS).batch_id)

    def test_invalid_child_fails_the_whole_batch(self):
        base = load_definition(ORB)
        base["parameters"]["min_rvol"]["min"] = 0.0
        base["parameters"]["min_rvol"]["max"] = 2.0
        base["parameters"]["min_rvol"]["step"] = 0.1
        base["entry"]["long"]["all"][2] = {"left": 0.0, "op": "<", "right": "$min_rvol", "enabled": "$use_volume_filter"}
        s = spec({"parameter": "use_volume_filter", "values": [True]})
        with self.assertRaises(VariationError) as cm:                            # const-vs-const when enabled
            generate_variations(base, s, SESSIONS)
        self.assertIn("batch rejected", str(cm.exception))


class TestLibrary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = StrategyLibrary(self.tmp.name)
        self.batch = generate_variations(ORB, ORB_SPEC, SESSIONS)

    def tearDown(self):
        self.tmp.cleanup()

    def _save_all(self):
        raw = load_definition(ORB)
        from edgelab.strategy.compiler import compile_definition
        c = compile_definition(raw, SESSIONS)
        rec = LineageRecord(c.identity.strategy_id, c.identity.logic_hash, c.identity.definition_hash,
                            c.family_id, "user")
        self.assertTrue(self.lib.save(raw, c.identity.to_dict(), rec))
        for v in self.batch.variants:
            self.lib.save(v.definition, v.identity, v.lineage)
        self.lib.save_batch(self.batch.record)
        return c.identity.strategy_id

    def test_save_load_list_families(self):
        base_id = self._save_all()
        self.assertEqual(len(self.lib.list()), 36)
        self.assertEqual(self.lib.families(), {"ny_opening_range_breakout": 36})
        child = self.batch.variants[0]
        doc = self.lib.load(child.strategy_id)
        self.assertEqual(doc["definition"], child.definition)
        self.assertEqual(self.lib.load_batch(self.batch.batch_id)["children"], self.batch.record["children"])
        with self.assertRaises(KeyError):
            self.lib.load("STR_DOESNOTEXIST")

    def test_lineage_graph(self):
        base_id = self._save_all()
        self.assertEqual(len(self.lib.children(base_id)), 35)
        chain = self.lib.ancestry(self.batch.variants[5].strategy_id)
        self.assertEqual([c["generation_method"] for c in chain], ["mode_a_variation", "user"])
        self.assertEqual(chain[-1]["strategy_id"], base_id)

    def test_save_is_idempotent_and_keeps_all_parents(self):
        self._save_all()
        v = self.batch.variants[0]
        self.assertFalse(self.lib.save(v.definition, v.identity, v.lineage))       # same record again
        self.assertEqual(len(self.lib.load(v.strategy_id)["lineage"]), 1)
        other = LineageRecord(v.strategy_id, v.identity["logic_hash"], v.identity["definition_hash"],
                              v.lineage.family_id, "manual_edit", parent_strategy_id="STR_OTHERPARENT")
        self.lib.save(v.definition, v.identity, other)
        self.assertEqual(len(self.lib.load(v.strategy_id)["lineage"]), 2)
        with self.assertRaises(ValueError):
            LineageRecord("x", "y", "z", "f", "ai_magic")


class TestProposals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rep = ingest_proposals(PROPOSALS, SESSIONS)

    def test_accepted_and_rejected_with_reasons(self):
        r = self.rep
        self.assertEqual([a["family_id"] for a in r.accepted], ["gap_fade", "london_range_break", "gap_fade_bigger"])
        reasons = {x["family_id"]: " ".join(x["errors"]) for x in r.rejected}
        self.assertIn("claim language", reasons["claims_perf"])
        self.assertIn("expected_win_rate", reasons["with_numbers"])
        self.assertIn("unknown feature 'supertrend'", reasons["bad_feature"])
        self.assertIn("trailing stops are not supported", reasons["trailing_idea"])
        self.assertIn("identical logic", reasons["gap_fade_copy"])
        self.assertTrue(any("same structure" in w for w in r.warnings))
        self.assertTrue(any("8 families requested, 3 accepted" in w for w in r.warnings))

    def test_accepted_have_lineage_and_valid_definitions(self):
        for rec in self.rep.lineage:
            self.assertEqual(rec.generation_method, "mode_b_proposal")
            self.assertEqual(rec.generation_parameters["source"]["kind"], "ai")
            self.assertEqual(rec.generation_batch_id, self.rep.batch_id)
            self.assertTrue(require_valid(self.rep.definitions[rec.strategy_id], SESSIONS))

    def test_batch_level_rules(self):
        b = load_definition(PROPOSALS)
        del b["source"]
        with self.assertRaises(StrategyValidationError):
            ingest_proposals(b, SESSIONS)
        b = load_definition(PROPOSALS)
        b["performance_summary"] = {"sharpe": 2}
        with self.assertRaises(StrategyValidationError):
            ingest_proposals(b, SESSIONS)
        b = load_definition(PROPOSALS)
        b["proposals"][0]["strategy"]["family"] = {"id": "something_else"}
        rep = ingest_proposals(b, SESSIONS)
        self.assertIn("!= proposal family id", rep.rejected[0]["errors"][0])

    def test_claim_detection(self):
        for text in ("This is highly profitable", "win-rate above 60", "guaranteed returns", "Sharpe of 3",
                     "a 70% win", "strong edge here", "proven setup"):
            self.assertTrue(claim_phrases(text), text)
        for text in ("Opening gaps tend to partially retrace", "Tests the edge of the range",
                     "Measures whether breakouts continue"):
            self.assertFalse(claim_phrases(text), text)

    def test_menu_and_proposer_interface(self):
        req = ProposalRequest(20, "focus on the NY session")
        menu = req.capability_menu(SESSIONS)
        self.assertEqual(menu["requested_families"], 20)
        ids = {f["id"] for f in menu["features"]}
        self.assertTrue({"ema", "vwap", "fvg", "swings", "time_of_day"} <= ids)
        self.assertIn("trailing_stop", menu["unsupported"])
        self.assertTrue(any("do not state or estimate performance" in r for r in menu["rules"]))
        batch = StaticProposer(PROPOSALS).propose(req, menu)
        self.assertEqual(ingest_proposals(batch, SESSIONS).batch_id, self.rep.batch_id)


if __name__ == "__main__":
    unittest.main()
