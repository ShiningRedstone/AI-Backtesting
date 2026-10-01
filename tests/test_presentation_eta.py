"""ADR-70 pure checks: deterministic display names / explanations that never touch identity, and the data-based ETA."""
import copy
import json
import re
import unittest

from edgelab.core.identity import hash_obj
from edgelab.research import campaign as C
from edgelab.strategy import factory as FX
from edgelab.strategy import factory_space as S
from edgelab.strategy.dsl import identity as dsl_identity
from edgelab.strategy.presentation import display_name, explanation, humanize, key_parameters, present

PERF = re.compile(r"\b(profitable|winning|best|strong|weak|promising|recommended|robust)\b", re.I)


class TestPresentation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = FX.generate(seed=4, quotas={f.fid: 2 for f in S.FAMILIES}).strategies

    def test_names_are_clean_deterministic_and_identity_free(self):
        for r in self.rows:
            a, b = display_name(r), display_name(json.loads(json.dumps(r)))
            self.assertEqual(a, b)                                                   # deterministic
            self.assertNotIn("_", a)
            self.assertNotIn("{", a)
            self.assertTrue(len(a) < 200 and a.strip() == a)
            fam = [f for f in S.FAMILIES if f.fid == r["family_id"]][0]
            self.assertTrue(a.startswith(fam.name))                                  # family name from the catalog
            self.assertIn(r["definition"]["timeframe"], a)
        before = copy.deepcopy(self.rows)
        for r in self.rows:
            present(r)
        self.assertEqual(self.rows, before)                                          # presentation mutates nothing
        for r in self.rows[:10]:
            ident = dsl_identity(r["definition"], {})
            self.assertEqual((ident.strategy_id, ident.logic_hash), (r["strategy_id"], r["logic_hash"]))

    def test_parameters_that_distinguish_strategies_stay_visible(self):
        a, b = self.rows[0], self.rows[1]                                            # same family, different variation
        self.assertNotEqual(display_name(a) + explanation(a), display_name(b) + explanation(b))
        fp = a["variation"]["family_params"]
        for k, v in list(fp.items())[:2]:
            self.assertIn(humanize(k), display_name(a) + " " + " ".join(humanize(x) for x in key_parameters(a)))
            self.assertIn(str(v) if not isinstance(v, float) else f"{v:g}", json.dumps(key_parameters(a)))

    def test_explanations_are_mechanical_deterministic_and_tied_to_the_definition(self):
        for r in self.rows:
            e = explanation(r)
            self.assertEqual(e, explanation(copy.deepcopy(r)))
            self.assertLessEqual(e.count(". "), 3)
            self.assertNotIn("_", e)
            self.assertIsNone(PERF.search(e), e)
            v = r["variation"]
            tf = {"1m": "1-minute", "5m": "5-minute", "15m": "15-minute", "30m": "30-minute", "60m": "1-hour"}[v["timeframe"]]
            self.assertIn(tf, e)
            self.assertIn({"long": "long only", "short": "short only", "both": "long and short"}[v["direction"]], e)
            if v["mtf"]:
                self.assertIn(v["mtf"]["htf"], e)                                    # the MTF dependency is named
            if v["stop"]["type"] == "atr":
                self.assertIn("ATR-based stop", e)
            self.assertIn(f"maximum hold of {r['definition']['exit']['max_hold_bars']} bars", e)
        # a changed definition detail changes the explanation (tied to the definition, not to the id)
        r = copy.deepcopy(self.rows[0])
        r["definition"]["exit"]["max_hold_bars"] = 999
        self.assertNotEqual(explanation(r), explanation(self.rows[0]))

    def test_present_block_keeps_the_machine_identity(self):
        p = present(self.rows[3])
        self.assertEqual((p["strategy_id"], p["logic_hash"], p["definition_hash"], p["machine_name"]),
                         (self.rows[3]["strategy_id"], self.rows[3]["logic_hash"], self.rows[3]["definition_hash"],
                          self.rows[3]["definition"]["name"]))
        self.assertEqual(humanize("rsi_mean_reversion"), "RSI Mean Reversion")


def obs(fam, tf, d, ok=True):
    return {"family_id": fam, "timeframe": tf, "duration_s": d, "success": ok}


class TestEta(unittest.TestCase):
    def test_insufficient_observations_estimate_only(self):
        rem = [{"family_id": "a", "timeframe": "5m"}] * 10
        e = C.estimate_remaining([obs("a", "5m", 2.0), obs("a", "5m", 3.0)], rem)
        self.assertEqual((e["state"], e["remaining_seconds"], e["n_observations"]), ("estimating", None, 2))
        e2 = C.estimate_remaining([obs("a", "5m", 2.0, ok=False)] * 5, rem)          # failures are not timing evidence
        self.assertEqual(e2["state"], "estimating")
        self.assertEqual(C.estimate_remaining([], [])["remaining_seconds"], 0.0)

    def test_medians_by_group_with_fallbacks_and_sequential_sum(self):
        observations = ([obs("a", "5m", d) for d in (1.0, 2.0, 100.0)]                # median 2 (robust to the outlier)
                        + [obs("b", "1m", d) for d in (10.0, 12.0, 14.0)]            # median 12
                        + [obs("c", "1m", 30.0)])                                    # too few for (c, 1m)
        remaining = [{"family_id": "a", "timeframe": "5m"}] * 4 \
            + [{"family_id": "c", "timeframe": "1m"}] * 2 \
            + [{"family_id": "z", "timeframe": "60m"}] * 1
        e = C.estimate_remaining(observations, remaining)
        self.assertEqual(e["state"], "estimate")
        # a: 4 x 2 = 8; c -> timeframe 1m median of (10,12,14,30) = 13 -> 26; z -> overall median of 7 obs = 12 -> 12
        self.assertAlmostEqual(e["remaining_seconds"], 8 + 26 + 12)
        self.assertEqual(e["basis"]["groups_used"], {"family_timeframe": 4, "timeframe": 2, "all": 1})
        self.assertEqual(e["workers"], 1)
        self.assertIn("estimate", e["note"])
        e2 = C.estimate_remaining(observations + [obs("a", "5m", 2.0)] * 3, remaining)
        self.assertNotEqual(e2["remaining_seconds"], e["remaining_seconds"])          # recalculated as data accumulates

    def test_deterministic(self):
        o = [obs("a", "5m", d) for d in (3.0, 1.0, 2.0, 2.5)]
        rem = [{"family_id": "a", "timeframe": "5m"}] * 3
        self.assertEqual(json.dumps(C.estimate_remaining(o, rem)), json.dumps(C.estimate_remaining(list(reversed(o)), rem)))
        self.assertEqual(hash_obj(C.estimate_remaining(o, rem)), hash_obj(C.estimate_remaining(o, rem)))


if __name__ == "__main__":
    unittest.main()
