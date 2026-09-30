"""ADR-57: robust acceptance statistics of the research protocol (version 2). Synthetic numbers only:
the bootstrap-t / normal minimum lower bound at the Bonferroni per-test alpha, its determinism and
monotonicity in the trial count, degenerate samples, and the Monte-Carlo p-value control filter."""
import copy
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from statistics import NormalDist

import numpy as np

from edgelab.research import protocol as rp

SPEC = rp.DEFAULT_ACCEPTANCE["oos_confidence"]
MT = rp.DEFAULT_MULTIPLE_TESTING


def pennies(n, seed=3):
    """Left-skewed trade returns: many small wins, occasional large losses (positive mean)."""
    rng = np.random.default_rng(seed)
    return np.where(rng.random(n) < 0.93, 0.3, -2.9)


class TestRobustLowerBound(unittest.TestCase):
    def test_deterministic_for_sample_and_seed(self):                                          # 1, 2
        r = pennies(400)
        a = rp.robust_lower_bound(r, 0.001, SPEC, seed=11, replicates=20_000)
        b = rp.robust_lower_bound(r.copy(), 0.001, SPEC, seed=11, replicates=20_000)
        self.assertEqual(a, b)
        self.assertEqual(rp.derive_seed("RP_X", "abc"), rp.derive_seed("RP_X", "abc"))
        self.assertNotEqual(rp.derive_seed("RP_X", "abc"), rp.derive_seed("RP_Y", "abc"))  # protocol-specific
        self.assertNotEqual(rp.derive_seed("RP_X", "abc"), rp.derive_seed("RP_X", "abd"))  # candidate-specific

    def test_other_seed_is_reproducible_but_may_differ(self):                                  # 3
        r = np.random.default_rng(8).standard_t(3, 400) * 0.8 + 0.05       # continuous, heavy-tailed
        a1, a2 = (rp.robust_lower_bound(r, 0.001, SPEC, seed=1, replicates=20_000) for _ in range(2))
        b = rp.robust_lower_bound(r, 0.001, SPEC, seed=2, replicates=20_000)
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1["q_boot"], b["q_boot"])

    def test_exact_tail_order_statistic_and_definition(self):                                   # 4
        r = pennies(300)
        t = rp.bootstrap_t_stats(r, 10_000, seed=5)
        alpha = rp.bonferroni(MT, 40)["per_test_alpha"]
        self.assertEqual(alpha, 0.05 / 40)
        res = rp.robust_lower_bound(r, alpha, SPEC, seed=5, replicates=10_000, t_stats=t)
        k = math.ceil((1 - alpha) * 10_000)
        self.assertEqual((res["order_statistic"], res["q_boot"]), (k, float(np.sort(t)[k - 1])))
        se = r.std(ddof=1) / math.sqrt(len(r))
        self.assertAlmostEqual(res["z"], NormalDist().inv_cdf(1 - alpha))
        self.assertAlmostEqual(res["lb"], r.mean() - max(res["z"], res["q_boot"]) * se)
        self.assertEqual(res["lb"], min(res["lb_normal"], res["lb_bootstrap_t"]))

    def test_family_size_only_tightens_the_bound(self):                                         # 5, 6
        r = pennies(500)
        t = rp.bootstrap_t_stats(r, 200_000, seed=9)
        lbs, zs = [], []
        for m in (1, 10, 100, 500, 2000):
            a = rp.bonferroni(MT, m)
            self.assertAlmostEqual(a["per_test_alpha"], 0.05 / m)
            res = rp.robust_lower_bound(r, a["per_test_alpha"], SPEC, seed=9, replicates=200_000, t_stats=t)
            lbs.append(res["lb"])
            zs.append(res["z"])
        self.assertEqual(lbs, sorted(lbs, reverse=True))                                         # never easier
        self.assertEqual(zs, sorted(zs))
        self.assertEqual(rp.bonferroni(MT, 0)["family_size"], 1)                                  # minimum family

    def test_degenerate_and_small_samples_are_never_met(self):                                  # 7, 8
        for r, why in ((np.full(200, 0.5), "zero standard error"), (pennies(29), "n < 30"),
                       (np.r_[pennies(100), np.nan], "non-finite")):
            res = rp.robust_lower_bound(r, 0.001, SPEC, seed=1, replicates=1000)
            self.assertFalse(res["available"], why)
            self.assertIsNone(res["lb"])
        t = rp.bootstrap_t_stats(np.array([1.0, 1.0, 1.0, -1.0]), 2000, seed=0)                # zero resample se
        self.assertTrue(np.isinf(t).any() and not np.isnan(t).any())

    def test_heavy_left_tail_bootstrap_binds_and_is_stricter_than_normal(self):                # 9
        r = pennies(600)
        self.assertGreater(r.mean(), 0)
        res = rp.robust_lower_bound(r, 0.05 / 2000, SPEC, seed=4, replicates=400_000)
        self.assertEqual(res["binding"], "bootstrap_t")                                          # skew is priced in
        self.assertGreater(res["q_boot"], res["z"])
        self.assertLess(res["lb"], res["lb_normal"])


class TestControlFilterAndProtocolVersions(unittest.TestCase):
    def test_monte_carlo_p_value_semantics(self):                                               # 10
        ctrl = list(np.linspace(-0.5, 0.0, 100))
        self.assertAlmostEqual(rp.control_p_value(0.1, ctrl), 1 / 101)                          # beats all
        self.assertAlmostEqual(rp.control_p_value(0.0, ctrl), 2 / 101)                          # tie counts against
        c4 = ctrl[:96] + [0.2] * 4
        c5 = ctrl[:95] + [0.2] * 5
        self.assertAlmostEqual(rp.control_p_value(0.1, c4), 5 / 101)                            # 0.0495 -> passes
        self.assertAlmostEqual(rp.control_p_value(0.1, c5), 6 / 101)                            # 0.0594 -> fails
        self.assertAlmostEqual(rp.control_p_value(0.1, ctrl[:99] + [float("nan")]), 2 / 101)    # non-finite counts
        self.assertIsNone(rp.control_p_value(None, ctrl))
        ac = rp.DEFAULT_ACCEPTANCE["random_control"]
        self.assertEqual((ac["n_controls"], ac["max_p_value"]), (100, 0.05))
        self.assertIn("NOT a familywise significance test", ac["role"])

    def assess(self, material, e, controls):
        metrics = {"trade_count": 600, "sample_label": "ADEQUATE SAMPLE", "expectancy_r": e, "expectancy_se": 0.01,
                   "profit_factor": 1.5, "loss_rate": 0.4, "net_r": 10.0}
        costs = [{"cost_multiplier": 1.5, "net_r": 1.0}, {"cost_multiplier": 2.0, "net_r": 0.5}]
        return rp.assess_holdout(material, metrics, costs, controls, 2000, trade_r=pennies(600), seed=7)

    def test_v2_assessment_uses_the_robust_rules_and_v1_keeps_its_own(self):                   # 10, 11
        mat_v2 = {"acceptance_criteria": copy.deepcopy(rp.DEFAULT_ACCEPTANCE), "multiple_testing": dict(MT)}
        mat_v2["acceptance_criteria"]["oos_confidence"]["bootstrap"]["replicates"] = 50_000        # test speed only
        ctrl = list(np.linspace(-0.2, 0.0, 100))
        out = self.assess(mat_v2, 0.05, ctrl)
        ci = out["criteria"]["adjusted_confidence"]
        self.assertEqual((ci["method_id"], ci["family_size"]), ("min_normal_bootstrap_t_v1", 2000))
        self.assertEqual(out["criteria"]["random_control"]["rule_id"], "monte_carlo_pvalue_v1")
        self.assertFalse(out["accepted"])
        v1 = {"acceptance_criteria": {**copy.deepcopy(rp.DEFAULT_ACCEPTANCE),
                                      "oos_confidence": {"statistic": "x", "method": "normal", "must_exceed": 0.0},
                                      "random_control": {"method": "x", "n_controls": 100, "seed": 0,
                                                         "statistic": "expectancy_r", "percentile": 95, "rule": "x"}},
              "multiple_testing": dict(MT)}
        out1 = rp.assess_holdout(v1, {"trade_count": 600, "sample_label": "ADEQUATE SAMPLE", "expectancy_r": 0.05,
                                      "expectancy_se": 0.01, "profit_factor": 1.5, "net_r": 1},
                                 [{"cost_multiplier": 1.5, "net_r": 1}, {"cost_multiplier": 2.0, "net_r": 1}], ctrl, 2000)
        self.assertEqual(out1["criteria"]["adjusted_confidence"]["method_id"], "normal_v1")
        self.assertIn("threshold", out1["criteria"]["random_control"])
        with self.assertRaises(ValueError):                                                      # v2 needs the trades
            rp.assess_holdout(mat_v2, {"trade_count": 600, "sample_label": "ADEQUATE SAMPLE", "expectancy_r": 0.05,
                                       "expectancy_se": 0.01}, [], ctrl, 2000)


class TestProtocolMigration(unittest.TestCase):
    """A version-1 protocol retired before any trial; version 2 created with a new identity."""

    def test_v1_retired_v2_new_identity_zero_trials(self):                                      # 11, 12
        from tests.test_research_protocol import ProtocolBase
        base = ProtocolBase
        base.setUpClass()
        self.addCleanup(base.tearDownClass)
        from edgelab.services import Services
        s = Services(root=base.root)
        self.addCleanup(s.store.close)
        for p in s.store.list_protocols(status="ACTIVE"):
            s.retire_protocol(p["protocol_id"])
        v2 = s.create_protocol(base.did, ("2024-03-04", "2024-04-05"), ("2024-04-08", "2024-04-26"), name="mig",
                               pre_protocol_exposure=[base.pre_run], exposure_statement="x")
        rec = s.store.get_protocol(v2["protocol_id"])
        v1_mat = copy.deepcopy(rec["material"])                                                  # the same protocol,
        v1_mat["protocol_version"] = 1                                                           # as version 1 wrote it
        v1_mat["acceptance_criteria"]["oos_confidence"] = {"statistic": "one-sided lower confidence bound",
                                                           "method": "normal approximation", "must_exceed": 0.0}
        s.retire_protocol(v2["protocol_id"])
        v1 = rp.make_record(v1_mat, {"code_version": "test"})
        s.store.save_protocol(v1, s._scope_key("NQ_DUKASCOPY", "DUKASCOPY"))
        self.assertNotEqual(v1["protocol_id"], v2["protocol_id"])                                # material change
        self.assertEqual(v2["material"]["protocol_version"], 2)
        self.assertEqual(v2["material"]["acceptance_criteria"]["oos_confidence"]["method_id"], "min_normal_bootstrap_t_v1")
        s.retire_protocol(v1["protocol_id"])                                                      # zero trials: retire
        for forged in ({**v1, "material": {**v1["material"], "name": "edited"}},           # stale hashes
                       {**v1, "material": {**v1["material"], "name": "edited"},
                        "material_hash": rp.hash_obj({**v1["material"], "name": "edited"})}):  # id no longer matches
            with self.assertRaises(rp.ProtocolRefusal) as cm:
                s.store.save_protocol(forged, "NQ_DUKASCOPY@DUKASCOPY")
            self.assertEqual(cm.exception.code, "PROTOCOL_TAMPERED")
        self.assertEqual(s.get_protocol(v1["protocol_id"])["status"], "RETIRED")
        self.assertEqual(s.get_protocol(v1["protocol_id"])["material"], v1_mat)                   # kept as evidence
        v2b = s.create_protocol(base.did, ("2024-03-04", "2024-04-05"), ("2024-04-08", "2024-04-26"), name="mig-v2",
                                pre_protocol_exposure=[base.pre_run], exposure_statement="v1 retired before any trial")
        st = s.protocol_status(v2b["protocol_id"])
        self.assertEqual((st["status"], st["trials"]["unique_numerical_trials"], st["holdout"]["looks_used"]),
                         ("ACTIVE", 0, 0))
        self.assertEqual(s.protocol_status(v1["protocol_id"])["trials"]["evaluation_events"], 0)


if __name__ == "__main__":
    unittest.main()
