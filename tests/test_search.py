"""Phase 4 step 2: search spec validation, canonical search_hash, and the deterministic
strategy x dataset planner. Planning never executes anything."""
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from edgelab.research.search import (SearchSpecError, canonical_search_spec, cell_id, plan_search,
                                     search_hash, validate_search_spec)
from edgelab.services import Services
from tests.phase2_helpers import synthetic_canonical, write_generic_utc

REPO = Path(__file__).resolve().parents[1]
FX = REPO / "strategies" / "fixtures"


def fixture(name: str) -> dict:
    return yaml.safe_load((FX / name).read_text())


class TestSearchPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.root / "configs")
        a = synthetic_canonical("2024-03-04", "2024-03-22", tf=5, seed=9)
        b = synthetic_canonical("2024-03-11", "2024-04-05", tf=5, seed=10)
        write_generic_utc(a, cls.root / "fut.csv")
        write_generic_utc(a, cls.root / "cfd.csv")
        write_generic_utc(b, cls.root / "fut_late.csv")
        cls.svc = s = Services(root=cls.root)

        def imp(f, inst, prov, at):
            return s.import_file(dict(file=str(cls.root / f), instrument=inst, provider=prov, asset_type=at,
                                      timeframe="5m", source_timezone="UTC"))["dataset_id"]
        cls.fut = imp("fut.csv", "NQ", "SYNTHF", "FUTURE")
        cls.cfd = imp("cfd.csv", "NAS100_CFD", "SYNTHC", "CFD")
        cls.late = imp("fut_late.csv", "NQ", "SYNTHF", "FUTURE")
        cls.ema = s.save_strategy(fixture("ema_crossover.yaml"))["strategy_id"]
        cls.rsi = s.save_strategy(fixture("rsi_threshold.yaml"))["strategy_id"]
        ema15 = fixture("ema_crossover.yaml")
        ema15["timeframe"] = "15m"
        cls.ema15 = s.save_strategy(ema15)["strategy_id"]
        cls.vb = s.generate_variations(fixture("opening_range_breakout.yaml"),
                                       yaml.safe_load((FX / "orb_variations.yaml").read_text()))
        cls.pb = s.ingest_proposals(str(FX / "proposals_example.yaml"))
        cls.ema_family = s.library.load(cls.ema)["family_id"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def spec(self, **kw):
        base = {"strategies": {"ids": [self.ema, self.rsi]}, "datasets": [self.fut, self.cfd]}
        base.update(kw)
        return base

    # ------------------------------------------------------------------ strict validation
    def test_strict_validation_rejects_unknown_keys_and_malformed_values(self):
        bad = {
            "strategies": {"ids": [self.ema], "idz": ["STR_X"]},
            "datasets": [self.fut, self.fut],
            "period": {"start": "2024-03-05", "end": "2024-03-01T00:00:00Z", "extra": 1},
            "ranking": {"metric": "win_rate", "min_sample_label": "BIG", "order": "desc"},
            "max_cells": True, "seed": "7", "workers": 0, "mystery": 1,
        }
        res = validate_search_spec(bad)
        self.assertFalse(res.valid)
        paths = {i.path for i in res.errors}
        for p in ("mystery", "strategies.idz", "datasets", "period.extra", "period.start", "ranking.metric",
                  "ranking.min_sample_label", "ranking.order", "max_cells", "seed", "workers"):
            self.assertIn(p, paths)
        self.assertIn("never a ranking metric", next(i.message for i in res.errors if i.path == "ranking.metric"))
        self.assertIn("never guessed", next(i.message for i in res.errors if i.path == "period.start"))
        for spec in ({"strategies": {"ids": ["VB_123"]}, "datasets": [self.fut]},           # wrong prefix
                     {"strategies": {"variation_batches": ["STR_1"]}, "datasets": [self.fut]},
                     {"strategies": {}, "datasets": [self.fut]},
                     {"strategies": {"ids": []}, "datasets": [self.fut]},
                     {"strategies": {"ids": [self.ema]}, "datasets": []},
                     {"strategies": {"ids": [self.ema]}, "datasets": [self.fut], "period": "latest"},
                     {"strategies": {"ids": [self.ema]}, "datasets": [self.fut], "search_spec_version": 2},
                     ["not", "a", "mapping"]):
            with self.subTest(spec=spec):
                self.assertFalse(validate_search_spec(spec).valid)
                with self.assertRaises(SearchSpecError):
                    plan_search(spec, self.svc)

    def test_invalid_references_are_refused_with_clear_errors(self):
        spec = {"strategies": {"ids": [self.ema, "STR_DOES_NOT_EXIST"], "variation_batches": ["VB_NOPE"],
                               "proposal_batches": ["PB_NOPE"], "families": ["no_such_family"]},
                "datasets": [self.fut, "NO_SUCH_DATASET"]}
        with self.assertRaises(SearchSpecError) as cm:
            plan_search(spec, self.svc)
        msgs = {i.path: i.message for i in cm.exception.issues}
        self.assertEqual(msgs["strategies.ids"], "strategy STR_DOES_NOT_EXIST is not in the strategy library")
        self.assertEqual(msgs["strategies.variation_batches"], "variation batch VB_NOPE not found")
        self.assertIn("no stored strategies belong to proposal batch PB_NOPE", msgs["strategies.proposal_batches"])
        self.assertIn("no strategies in family 'no_such_family'", msgs["strategies.families"])
        self.assertEqual(msgs["datasets"], "dataset NO_SUCH_DATASET is not in the store")
        self.assertEqual(len(cm.exception.issues), 5)                    # every problem reported at once

    # ------------------------------------------------------------------ identity
    def test_search_hash_is_canonical(self):
        h = search_hash(self.spec())
        same = {"datasets": [self.cfd, self.fut], "strategies": {"ids": [self.rsi, self.ema, self.rsi]},
                "workers": 4, "ranking": {"metric": "expectancy_r"}}
        self.assertEqual(search_hash(same), h)                              # order, duplicates, workers, defaults
        self.assertEqual(canonical_search_spec(same)["strategies"]["ids"], sorted([self.ema, self.rsi]))
        for change in ({"ranking": {"metric": "profit_factor"}},            # downstream analysis of results
                       {"ranking": {"min_sample_label": "ADEQUATE SAMPLE"}},
                       {"ranking": {"metric": "net_r", "min_sample_label": "LOW SAMPLE SIZE"}},
                       {"max_cells": 5}, {"max_cells": 99999},              # a safety cap, not an input
                       {"workers": 1}, {"workers": 16}):                    # never changes results
            with self.subTest(same=change):
                self.assertEqual(search_hash(self.spec(**change)), h)
                self.assertEqual(plan_search(self.spec(**change), self.svc).search_id,
                                 plan_search(self.spec(), self.svc).search_id)
        for change in ({"datasets": [self.fut]}, {"seed": 1}, {"period": "common"},
                       {"strategies": {"ids": [self.ema]}}, {"strategies": {"families": [self.ema_family]}},
                       {"period": {"start": "2024-03-11T00:00:00Z", "end": "2024-03-15T00:00:00Z"}}):
            with self.subTest(different=change):
                self.assertNotEqual(search_hash(self.spec(**change)), h)
        p1 = {"start": "2024-03-11T00:00:00Z", "end": "2024-03-15T00:00:00Z"}
        p2 = {"start": "2024-03-10T19:00:00-05:00", "end": "2024-03-15T01:00:00+01:00"}
        self.assertEqual(search_hash(self.spec(period=p1)), search_hash(self.spec(period=p2)))

    def test_cells_are_deterministic_and_stable(self):
        a = plan_search(self.spec(), self.svc)
        b = plan_search({"datasets": [self.cfd, self.fut], "strategies": {"ids": [self.rsi, self.ema]},
                         "workers": 3}, self.svc)
        self.assertEqual(a.search_id, b.search_id)
        self.assertEqual(a.plan_hash, b.plan_hash)
        self.assertEqual(a.cells, b.cells)
        expected = [(s, d) for s in sorted([self.ema, self.rsi]) for d in sorted([self.fut, self.cfd])]
        self.assertEqual([(c["strategy_id"], c["dataset_id"]) for c in a.cells], expected)
        self.assertEqual([c["plan_index"] for c in a.cells], list(range(len(expected))))
        c0 = a.cells[0]
        self.assertEqual(c0["cell_id"], cell_id(c0["strategy_id"], c0["dataset_id"], c0["dataset_content_hash"],
                                                self.svc._config_hash(), a.search_hash))
        by = {(c["strategy_id"], c["dataset_id"]): c for c in a.cells}   # fut and cfd hold IDENTICAL bars
        self.assertEqual(by[(self.ema, self.fut)]["dataset_content_hash"], by[(self.ema, self.cfd)]["dataset_content_hash"])
        self.assertNotEqual(by[(self.ema, self.fut)]["cell_id"], by[(self.ema, self.cfd)]["cell_id"])
        other = plan_search(self.spec(seed=5), self.svc)                  # another search: other cell ids
        self.assertNotEqual({c["cell_id"] for c in other.cells}, {c["cell_id"] for c in a.cells})
        ranked = plan_search(self.spec(ranking={"metric": "net_r"}, max_cells=50, workers=8), self.svc)
        self.assertEqual(ranked.cells, a.cells)                           # ranking/cap/workers: same cells
        self.assertEqual(ranked.plan_hash, a.plan_hash)
        self.assertNotEqual(cell_id(self.ema, self.fut, "h", "c", "s"), cell_id(self.ema, self.cfd, "h", "c", "s"))
        self.assertEqual(len({c["cell_id"] for c in a.cells}), len(a.cells))
        self.assertTrue(a.search_id.startswith("SRCH_"))

    # ------------------------------------------------------------------ expansion / dedupe / sources
    def test_duplicate_strategies_collapse_across_sources(self):
        p = plan_search({"strategies": {"ids": [self.ema, self.ema], "families": [self.ema_family]},
                         "datasets": [self.fut]}, self.svc)
        family = [r["strategy_id"] for r in self.svc.library.list(self.ema_family)]
        self.assertIn(self.ema, family)
        self.assertEqual(p.counts["strategies"], len(set(family)))
        self.assertEqual(p.counts["strategy_references"], 1 + len(family))  # canonical ids are de-duplicated
        self.assertEqual(p.counts["duplicate_references_collapsed"], 1)
        ema_row = next(r for r in p.strategies if r["strategy_id"] == self.ema)
        self.assertEqual(ema_row["sources"], sorted(["ids", f"family:{self.ema_family}"]))
        self.assertEqual(len(p.cells), len(set(family)))

    def test_variation_and_proposal_batches_expand(self):
        pb_ids = [a["strategy_id"] for a in self.pb["accepted"]]
        p = plan_search({"strategies": {"variation_batches": [self.vb["batch_id"]],
                                        "proposal_batches": [self.pb["batch_id"]]},
                         "datasets": [self.fut, self.cfd, self.late]}, self.svc)
        children = self.svc.library.load_batch(self.vb["batch_id"])["children"]
        ids = {r["strategy_id"] for r in p.strategies}
        self.assertEqual(ids, set(children) | set(pb_ids))
        self.assertEqual(p.counts["planned"], len(ids) * 3)
        self.assertEqual(len(p.cells), p.counts["planned"])

    def test_archived_members_excluded_and_explicit_archived_refused(self):
        sid = self.svc.library.load_batch(self.vb["batch_id"])["children"][0]
        self.svc.archive_strategy(sid)
        try:
            p = plan_search({"strategies": {"variation_batches": [self.vb["batch_id"]]},
                             "datasets": [self.fut]}, self.svc)
            self.assertNotIn(sid, {r["strategy_id"] for r in p.strategies})
            self.assertEqual(p.excluded, [{"strategy_id": sid, "reason": "archived",
                                           "sources": [self.vb["batch_id"]]}])
            self.assertEqual(p.counts["excluded_archived"], 1)
            with self.assertRaises(SearchSpecError) as cm:
                plan_search({"strategies": {"ids": [sid]}, "datasets": [self.fut]}, self.svc)
            self.assertIn("archived", str(cm.exception))
        finally:
            self.svc.restore_strategy(sid)

    # ------------------------------------------------------------------ eligibility
    def test_ineligible_cells_carry_readiness_reasons(self):
        p = plan_search({"strategies": {"ids": [self.ema, self.ema15]}, "datasets": [self.fut, self.cfd]}, self.svc)
        by = {(c["strategy_id"], c["dataset_id"]): c for c in p.cells}
        ok = by[(self.ema, self.fut)]
        self.assertTrue(ok["eligible"])
        self.assertEqual(ok["reasons"], [])
        cfd = by[(self.ema, self.cfd)]                                   # CFD costs are never invented
        self.assertFalse(cfd["eligible"])
        self.assertEqual(cfd["cost_status"], "unconfigured")
        rd = {d["dataset_id"]: d for d in self.svc.backtest_readiness(self.ema)["datasets"]}
        self.assertEqual(cfd["reasons"], rd[self.cfd]["reasons"])
        self.assertEqual(ok["reasons"], rd[self.fut]["reasons"])
        rd15 = {d["dataset_id"]: d for d in self.svc.backtest_readiness(self.ema15)["datasets"]}
        tf = by[(self.ema15, self.fut)]
        self.assertFalse(tf["eligible"])
        self.assertEqual(tf["reasons"], rd15[self.fut]["reasons"])
        self.assertEqual(tf["reasons"], ["timeframe 5m does not match the strategy timeframe 15m"])
        self.assertEqual(p.counts, {"strategy_references": 2, "strategies": 2, "duplicate_references_collapsed": 0,
                                    "excluded_archived": 0, "datasets": 2, "planned": 4, "eligible": 1,
                                    "ineligible": 3})

    # ------------------------------------------------------------------ max_cells
    def test_max_cells_refuses_before_execution_and_never_truncates(self):
        runs_before = len(self.svc.store.list_runs())
        spec = {"strategies": {"ids": [self.ema, self.rsi, self.ema15]}, "datasets": [self.fut, self.cfd, self.late]}
        full = plan_search(spec, self.svc)
        self.assertEqual(full.counts["eligible"], 4)                     # ema, rsi on fut and late
        self.assertEqual(full.counts["planned"], 9)
        exact = plan_search({**spec, "max_cells": 4}, self.svc)          # ineligible cells are not capped
        self.assertEqual(len(exact.cells), 9)
        with self.assertRaises(SearchSpecError) as cm:
            plan_search({**spec, "max_cells": 3}, self.svc)
        self.assertIn("never truncated", str(cm.exception))
        self.assertIn("4 eligible cells exceed max_cells 3", str(cm.exception))
        self.assertEqual(len(self.svc.store.list_runs()), runs_before)   # planning executes nothing

    # ------------------------------------------------------------------ periods / warnings
    def test_period_restriction_and_warnings(self):
        spec = {"strategies": {"ids": [self.ema]}, "datasets": [self.fut, self.cfd, self.late]}
        plain = plan_search(spec, self.svc)
        self.assertIsNone(plain.period)
        self.assertTrue(any("IDENTICAL bar content" in w for w in plain.warnings))   # fut and cfd share bars
        self.assertTrue(any("different periods" in w for w in plain.warnings))
        common = plan_search({**spec, "period": "common"}, self.svc)
        self.assertEqual(common.period["mode"], "common")
        self.assertEqual(common.period["start"][:10], "2024-03-11")
        self.assertEqual(common.period["end"][:10], "2024-03-22")
        self.assertFalse(any("different periods" in w for w in common.warnings))
        self.assertNotEqual(common.search_id, plain.search_id)
        early = plan_search({**spec, "period": {"start": "2024-03-04T00:00:00Z", "end": "2024-03-08T00:00:00Z"}},
                            self.svc)
        by = {c["dataset_id"]: c for c in early.cells}
        self.assertTrue(by[self.fut]["eligible"])
        self.assertFalse(by[self.late]["eligible"])
        self.assertIn("outside the period", by[self.late]["reasons"][0])
        none = plan_search({**spec, "period": {"start": "2024-01-01T00:00:00Z", "end": "2024-01-31T00:00:00Z"}},
                           self.svc)                                     # every cell ineligible, reported
        self.assertEqual((none.counts["eligible"], none.counts["ineligible"]), (0, 3))
        self.assertTrue(any("no eligible cells" in w for w in none.warnings))

    def test_plan_is_plain_data(self):
        import json
        json.dumps(plan_search(self.spec(period="common"), self.svc).to_dict(), allow_nan=False)


if __name__ == "__main__":
    unittest.main()
