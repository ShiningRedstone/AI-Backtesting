"""ADR-67: protocol version 3 supports exactly the declared 10,000-unique-trial research universe.

The declared budget is enforced by the SAME ledger checks as before (per evaluation and per search plan), the Bonferroni
family is the declared budget (not the growing count), holdout looks stay independently limited, and an unused protocol is
superseded by a NEW frozen record, never edited. Everything runs on a temporary SYNTHETIC workspace: the filler ledger rows
below are test fixtures in that throwaway store, not research trials; no real protocol, trial or holdout look is touched."""
import unittest
from datetime import datetime, timezone

from edgelab.research import protocol as rp
from edgelab.research.protocol import ProtocolRefusal
from tests.test_research_protocol import DISC, HOLD, ProtocolBase


def filler_trials(s, pid, n, config_hash):
    """n counted synthetic ledger rows (distinct keys) in the throwaway test store."""
    now = datetime.now(timezone.utc).isoformat()
    for i in range(n):
        key = rp.hash_obj({"filler": i})
        s.store.add_trial_event({"protocol_id": pid, "trial_id": "TR_FILL", "trial_key": key, "status": "completed",
                                 "entry_point": "backtest_strategy", "strategy_id": f"STR_FILL{i}", "logic_hash": key,
                                 "definition_hash": key, "family": "filler", "dataset_id": "D", "source_dataset_id": "D",
                                 "evaluated_content_hash": "x", "window_start": "a", "window_end": "b",
                                 "config_hash": config_hash, "cost_scenario": None, "proposal_id": None,
                                 "search_id": None, "run_id": None, "error": None, "created_at": now})


class TestDeclaredFamily(unittest.TestCase):
    def test_defaults_record_10000_trials_independent_looks_and_the_declared_family(self):
        self.assertEqual((rp.PROTOCOL_VERSION, rp.DEFAULT_TRIAL_BUDGET, rp.DEFAULT_HOLDOUT_LOOKS), (3, 10_000, 10))
        mt = rp.DEFAULT_MULTIPLE_TESTING
        self.assertEqual((mt["method"], mt["familywise_alpha"], mt["family_size_rule"]),
                         ("Bonferroni-familywise-alpha", 0.05, "declared_max_unique_trials"))
        b = rp.bootstrap_replicates_for(10_000)
        self.assertEqual(b, 5_000_000)                                           # 25 / (0.05 / 10,000)
        self.assertGreaterEqual(b * 0.05 / 10_000, 25)
        self.assertEqual(rp.bootstrap_replicates_for(2000), rp.BOOTSTRAP_REPLICATES)   # version-2 value unchanged

    def test_family_is_the_declared_budget_not_the_count(self):
        mat = {"multiple_testing": dict(rp.DEFAULT_MULTIPLE_TESTING), "trial_budget": {"max_unique_trials": 10_000}}
        for counted in (0, 1, 37, 9_999, 10_000):
            self.assertEqual(rp.family_size(mat, counted), 10_000)
        adj = rp.bonferroni(mat["multiple_testing"], rp.family_size(mat, 3))
        self.assertAlmostEqual(adj["per_test_alpha"], 0.05 / 10_000)
        v2 = {"multiple_testing": {k: v for k, v in rp.DEFAULT_MULTIPLE_TESTING.items() if k != "family_size_rule"},
              "trial_budget": {"max_unique_trials": 2000}}
        self.assertEqual(rp.family_size(v2, 37), 37)                               # version-2 records keep their rule

    def test_holdout_assessment_uses_the_declared_family(self):
        import copy
        import numpy as np
        ac = copy.deepcopy(rp.DEFAULT_ACCEPTANCE)
        ac["oos_confidence"]["bootstrap"]["replicates"] = 20_000                   # test speed only
        mat = {"acceptance_criteria": ac, "multiple_testing": dict(rp.DEFAULT_MULTIPLE_TESTING),
               "trial_budget": {"max_unique_trials": 10_000}}
        r = np.tile([0.3, -0.1, 0.2, -0.05], 150)
        out = rp.assess_holdout(mat, {"trade_count": 600, "sample_label": "ADEQUATE SAMPLE", "expectancy_r": float(r.mean()),
                                      "expectancy_se": 0.01, "profit_factor": 2.0, "net_r": 1.0},
                                [{"cost_multiplier": 1.5, "net_r": 1}, {"cost_multiplier": 2.0, "net_r": 1}],
                                list(np.linspace(-0.2, 0, 100)), 3, trade_r=r, seed=1)
        self.assertEqual(out["multiple_testing"]["family_size"], 10_000)          # 3 counted, 10,000 declared
        self.assertAlmostEqual(out["criteria"]["adjusted_confidence"]["per_test_alpha"], 5e-6)


class TestCapacity(ProtocolBase):
    def test_material_is_frozen_with_10000_and_independent_holdout_looks(self):
        s = self.svc()
        p = self.fresh(s, "cap_mat")
        mat = p["material"]
        self.assertEqual((mat["protocol_version"], mat["trial_budget"]["max_unique_trials"],
                          mat["holdout_budget"]["max_unique_candidate_evaluations"]), (3, 10_000, 10))
        self.assertEqual(mat["multiple_testing"]["family_size_rule"], "declared_max_unique_trials")
        self.assertEqual(mat["acceptance_criteria"]["oos_confidence"]["bootstrap"]["replicates"], 5_000_000)
        st = s.protocol_status(p["protocol_id"])
        self.assertEqual((st["multiple_testing"]["family_size"], st["trials"]["unique_numerical_trials"]), (10_000, 0))
        self.assertIn("DECLARED trial budget", st["multiple_testing"]["effect"])
        q = self.fresh(s, "cap_mat", holdout_looks=3)                              # looks configured independently
        self.assertEqual((q["material"]["trial_budget"]["max_unique_trials"],
                          q["material"]["holdout_budget"]["max_unique_candidate_evaluations"]), (10_000, 3))
        self.assertNotEqual(p["protocol_id"], q["protocol_id"])
        rec = s.store.get_protocol(p["protocol_id"])
        forged = {**rec, "material": {**rec["material"], "trial_budget": {**rec["material"]["trial_budget"],
                                                                          "max_unique_trials": 10_001}}}
        with self.assertRaises(ProtocolRefusal) as cm:                             # a frozen record cannot be edited
            rp.verify_record(forged)
        self.assertEqual(cm.exception.code, "PROTOCOL_TAMPERED")

    def test_the_10000th_trial_is_permitted_and_the_10001st_refused(self):
        s = self.svc()
        p = self.fresh(s, "cap_gate")
        pid = p["protocol_id"]
        filler_trials(s, pid, 9_999, p["material"]["config_hash"])
        per = self.disc_period(p)
        s.backtest_strategy(self.ema, self.did, False, per)                         # the 10,000th unique trial
        self.assertEqual(s.store.count_trials(pid), 10_000)
        s.backtest_strategy(self.ema, self.did, False, per)                         # its duplicate is not a new trial
        with self.assertRaises(ProtocolRefusal) as cm:                              # the 10,001st
            s.backtest_strategy(self.rsi, self.did, False, per)
        self.assertEqual(cm.exception.code, "PROTOCOL_TRIAL_BUDGET_EXHAUSTED")
        self.assertEqual(s.store.count_trials(pid), 10_000)
        st = s.protocol_status(pid)
        self.assertEqual((st["trials"]["remaining"], st["multiple_testing"]["family_size"]), (0, 10_000))

    def test_a_search_plan_of_10000_fits_and_10001_does_not(self):
        s = self.svc()
        p = self.fresh(s, "cap_plan")
        s._protocol_budget_check(p["protocol_id"], list(range(10_000)))              # permitted
        with self.assertRaises(ProtocolRefusal) as cm:
            s._protocol_budget_check(p["protocol_id"], list(range(10_001)))
        self.assertEqual(cm.exception.code, "PROTOCOL_TRIAL_BUDGET_EXHAUSTED")

    def test_holdout_look_limit_still_enforced(self):
        s = self.svc()
        p = self.fresh(s, "cap_looks", holdout_looks=2)
        pid = p["protocol_id"]
        sid = self.search(s, p, [self.ema])["search_id"]
        s.select_shortlist(sid, [self.ema])
        now = datetime.now(timezone.utc).isoformat()
        for i in range(2):                                                          # two looks already spent
            s.store.add_holdout_access({"access_id": f"HA_FILL{i}", "protocol_id": pid, "strategy_id": f"STR_X{i}",
                                        "logic_hash": f"x{i}", "definition_hash": "x", "frozen_hash": "x",
                                        "search_id": sid, "created_at": now, "status": "completed"})
        with self.assertRaises(ProtocolRefusal) as cm:
            s.evaluate_holdout(pid, sid, self.ema)
        self.assertEqual(cm.exception.code, "HOLDOUT_BUDGET_EXHAUSTED")
        self.assertEqual(s.protocol_status(pid)["holdout"]["looks_used"], 2)

    def test_supersede_only_an_unused_protocol_with_a_new_frozen_record(self):
        s = self.svc()
        old = self.fresh(s, "cap_old", trial_budget=2000)
        oid = old["protocol_id"]
        before = s.store.get_protocol(oid)["material"]
        new = s.create_protocol(self.did, DISC, HOLD, name="cap_new", pre_protocol_exposure=[self.pre_run],
                                exposure_statement="fixed EMA measured on the full synthetic period before activation",
                                trial_budget=10_000, supersedes=oid)
        self.assertNotEqual(new["protocol_id"], oid)
        self.assertEqual((new["status"], new["material"]["supersedes"]), ("ACTIVE", oid))
        self.assertEqual(new["material"]["trial_budget"]["max_unique_trials"], 10_000)
        self.assertEqual(new["material"]["windows"], before["windows"])
        o = s.get_protocol(oid)
        self.assertEqual((o["status"], o["material"]), ("RETIRED", before))          # retired, never edited
        s.backtest_strategy(self.ema, self.did, False, self.disc_period(new))        # now it has a trial event
        with self.assertRaises(ProtocolRefusal) as cm:
            s.create_protocol(self.did, DISC, HOLD, name="cap_newer", pre_protocol_exposure=[self.pre_run],
                              exposure_statement="x", supersedes=new["protocol_id"])
        self.assertEqual(cm.exception.code, "PROTOCOL_IN_USE")
        self.assertEqual(s.get_protocol(new["protocol_id"])["status"], "ACTIVE")    # nothing retired on refusal
        with self.assertRaises(ProtocolRefusal) as cm:
            s.create_protocol(self.did, DISC, HOLD, name="cap_x", pre_protocol_exposure=[self.pre_run],
                              exposure_statement="x", supersedes=oid)             # not the ACTIVE one
        self.assertEqual(cm.exception.code, "PROTOCOL_SUPERSEDE_INVALID")

    def test_supersede_script_check_is_read_only_and_apply_preserves_the_setup(self):
        from scripts import protocol_supersede as ps
        s = self.svc()
        old = self.fresh(s, "cap_script", trial_budget=2000)
        s.store.close()
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ps.main(["--root", str(self.root)])
        s = self.svc()
        self.assertEqual(s.get_protocol(old["protocol_id"])["status"], "ACTIVE")   # check only: nothing changed
        self.assertIn('"max_unique_trials": 2000', buf.getvalue())
        s.store.close()
        with contextlib.redirect_stdout(io.StringIO()):
            ps.main(["--root", str(self.root), "--apply"])
        s = self.svc()
        act = s.store.list_protocols(status="ACTIVE")
        self.assertEqual(len(act), 1)
        m = act[0]["material"]
        self.assertEqual((m["supersedes"], m["trial_budget"]["max_unique_trials"], m["protocol_version"]),
                         (old["protocol_id"], 10_000, 3))
        self.assertEqual(m["pre_protocol_exposure"]["runs"][0]["run_id"], self.pre_run)
        st = s.protocol_status(act[0]["protocol_id"])
        self.assertEqual((st["trials"]["unique_numerical_trials"], st["holdout"]["looks_used"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
