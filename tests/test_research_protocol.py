"""ADR-56: locked research protocol, holdout ledger and program-level trial ledger. SYNTHETIC data only
(a Dukascopy-shaped BID/ASK fixture under the canonical `quotes` profile); no AI provider is called
except the deterministic mock, no real dataset is touched."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from edgelab.ai.context import FORBIDDEN_KEYS, _keys
from edgelab.core.identity import hash_obj
from edgelab.research import protocol as rp
from edgelab.research.protocol import ProtocolRefusal
from edgelab.services import Services
from tests.dukascopy_fixture import write_fixture

REPO = Path(__file__).resolve().parents[1]
FX = REPO / "strategies" / "fixtures"
DISC, HOLD = ("2024-03-04", "2024-04-05"), ("2024-04-08", "2024-04-26")


def fixture(name):
    return yaml.safe_load((FX / name).read_text())


def fingerprint(svc, run_id):
    rec, tr = svc.store.load_run(run_id)
    return hash_obj({"rec": json.loads(json.dumps(rec, default=str)), "trades": tr.to_json(date_format="iso")})


class ProtocolBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.root / "configs")
        csv = cls.root / "combined.csv"
        write_fixture(csv, end="2024-04-27")
        f = pd.read_csv(csv, dtype=str)
        s = 1.0 + 0.25 * (np.arange(len(f)) % 7)
        for k, extra in (("open", 0.0), ("high", 0.5), ("low", 0.0), ("close", 0.0)):
            f[f"ask_{k}"] = (f[k].astype(float) + s + extra).map(lambda x: f"{x:.3f}")
        f.to_csv(csv, index=False)
        svc = Services(root=cls.root)
        cls.did = svc.import_file(dict(
            file=str(csv), profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY", provider="DUKASCOPY",
            asset_type="CFD", symbol="USATECH.IDX/USD", price_basis="bid", timeframe="1m", derive_timeframes=["5m"],
            build_features=False, bid_close_column="close", ask_close_column="ask_close", ask_open_column="ask_open",
            ask_high_column="ask_high", ask_low_column="ask_low", dataset_name="DUKA_SYN"))["derived"][0]
        cls.ema = svc.save_strategy(fixture("ema_crossover.yaml"))["strategy_id"]
        cls.rsi = svc.save_strategy(fixture("rsi_threshold.yaml"))["strategy_id"]
        slow = fixture("ema_crossover.yaml")
        slow["name"] = "ema_crossover_slow"
        slow["entry"]["long"]["right"]["params"]["period"] = 34
        slow["entry"]["short"]["left"]["params"]["period"] = 34
        cls.slow = svc.save_strategy(slow)["strategy_id"]
        cls.pre_run = svc.backtest_strategy(fixture("ema_crossover.yaml"), cls.did, record=True)["run_id"]   # pre-protocol
        cls.pre_fp = fingerprint(svc, cls.pre_run)
        cls.content_hash = svc.load_dataset(cls.did).manifest.content_hash
        svc.store.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def svc(self, cfg=None):
        s = Services(cfg=cfg, root=self.root)
        self.addCleanup(s.store.close)
        return s

    def fresh(self, s, name, **kw):
        """Retire whatever is ACTIVE for the scope and activate a protocol unique to this test."""
        for p in s.store.list_protocols(status="ACTIVE"):
            s.retire_protocol(p["protocol_id"])
        return s.create_protocol(self.did, DISC, HOLD, name=name, pre_protocol_exposure=[self.pre_run],
                                 exposure_statement="fixed EMA measured on the full synthetic period before activation",
                                 **kw)

    def disc_period(self, p):
        w = p["material"]["windows"]["discovery"]
        return (w["first_bar"], w["last_bar"])

    def refused(self, code, fn, *a, **kw):
        with self.assertRaises(ProtocolRefusal) as cm:
            fn(*a, **kw)
        self.assertEqual(cm.exception.code, code, cm.exception.to_dict())
        return cm.exception

    def search(self, s, p, ids, **kw):
        per = self.disc_period(p)
        return s.run_search({"strategies": {"ids": ids}, "datasets": [self.did],
                             "period": {"start": per[0], "end": per[1]}, **kw})


class TestProtocolRecord(ProtocolBase):
    def test_creation_windows_hashing_and_exposure(self):                                        # 1
        s = self.svc()
        p = self.fresh(s, "t1")
        self.assertTrue(p["protocol_id"].startswith("RP_") and p["status"] == "ACTIVE" and p["created"])
        rp.verify_record(p)
        mat = p["material"]
        w = mat["windows"]
        self.assertEqual((w["discovery"]["trading_dates"], w["holdout"]["trading_dates"]), (list(DISC), list(HOLD)))
        self.assertLess(w["discovery"]["last_bar_ns"], w["holdout"]["boundary_open_ns"])
        self.assertEqual(pd.Timestamp(w["holdout"]["last_bar"]), pd.Timestamp(s.load_dataset(self.did).manifest.end))
        self.assertEqual(mat["source_dataset"]["content_hash"], self.content_hash)
        self.assertEqual((mat["execution"]["spread_source"], mat["execution"]["quote_model"]), ("quotes", "directional_bid_ask"))
        self.assertEqual((mat["trial_budget"]["max_unique_trials"], mat["holdout_budget"]["max_unique_candidate_evaluations"]),
                         (10000, 10))
        ac = mat["acceptance_criteria"]
        self.assertEqual((ac["min_oos_sample_label"], ac["min_oos_expectancy_r_exclusive"], ac["min_profit_factor_exclusive"],
                          ac["cost_stress"]["multipliers"], ac["in_sample_rank_sufficient"], ac["single_oos_pass_sufficient"]),
                         ("ADEQUATE SAMPLE", 0.0, 1.0, [1.5, 2.0], False, False))
        self.assertEqual(mat["multiple_testing"]["method"], "Bonferroni-familywise-alpha")
        exp = mat["pre_protocol_exposure"]["runs"][0]
        self.assertEqual(exp["run_id"], self.pre_run)
        self.assertFalse({"metrics", "headline_metrics", "net_r", "expectancy_r"} & set(exp))    # identities only
        self.assertEqual(s.protocol_status(p["protocol_id"])["trials"]["unique_numerical_trials"], 0)  # not re-attributed

    def test_material_change_is_a_new_identity_and_lifecycle(self):                              # 2
        s = self.svc()
        p = self.fresh(s, "t2")
        again = s.create_protocol(self.did, DISC, HOLD, name="t2", pre_protocol_exposure=[self.pre_run],
                                  exposure_statement="fixed EMA measured on the full synthetic period before activation")
        self.assertEqual((again["protocol_id"], again["created"]), (p["protocol_id"], False))    # idempotent
        e = self.refused("PROTOCOL_ALREADY_ACTIVE", s.create_protocol, self.did, DISC, HOLD, name="t2", holdout_looks=3,
                         pre_protocol_exposure=[self.pre_run],
                         exposure_statement="fixed EMA measured on the full synthetic period before activation")
        self.assertEqual(e.detail["active_protocol_id"], p["protocol_id"])
        s.retire_protocol(p["protocol_id"])
        q = s.create_protocol(self.did, DISC, HOLD, name="t2", holdout_looks=3, pre_protocol_exposure=[self.pre_run],
                              exposure_statement="fixed EMA measured on the full synthetic period before activation")
        self.assertNotEqual(q["protocol_id"], p["protocol_id"])
        s.retire_protocol(q["protocol_id"])
        self.refused("PROTOCOL_RETIRED", s.create_protocol, self.did, DISC, HOLD, name="t2",
                     pre_protocol_exposure=[self.pre_run],
                     exposure_statement="fixed EMA measured on the full synthetic period before activation")
        self.assertEqual(s.get_protocol(p["protocol_id"])["material"], p["material"])            # material kept

    def test_frozen_protocol_cannot_be_edited_in_place(self):                                   # 3
        s = self.svc()
        p = self.fresh(s, "t3")
        rec = s.store.get_protocol(p["protocol_id"])
        forged = copy.deepcopy(rec)
        forged["material"]["holdout_budget"]["max_unique_candidate_evaluations"] = 99
        forged["material_hash"] = hash_obj(forged["material"])
        self.refused("PROTOCOL_TAMPERED", s.store.save_protocol, forged, "NQ_DUKASCOPY@DUKASCOPY")    # id != hash
        s.store._exec("UPDATE research_protocols SET record_json = ? WHERE protocol_id = ?",
                      (json.dumps(forged), p["protocol_id"]))                                   # direct tampering
        self.refused("PROTOCOL_TAMPERED", s.get_protocol, p["protocol_id"])
        self.refused("PROTOCOL_TAMPERED", s.backtest_strategy, self.ema, self.did, False, self.disc_period(p))
        s.store._exec("UPDATE research_protocols SET record_json = ? WHERE protocol_id = ?",
                      (json.dumps(rec), p["protocol_id"]))                                      # restore for other tests
        s.retire_protocol(p["protocol_id"])

    def test_config_change_cannot_silently_alter_costs_or_execution(self):                      # 19
        s = self.svc()
        p = self.fresh(s, "t19")
        cfg = copy.deepcopy(s.cfg)
        cfg["costs"]["symbols"]["NQ_DUKASCOPY"]["providers"]["DUKASCOPY"]["slippage_ticks_market"] = 0.0
        other = self.svc(cfg)
        self.refused("PROTOCOL_CONFIG_CHANGED", other.backtest_strategy, self.ema, self.did, False, self.disc_period(p))
        cfg2 = copy.deepcopy(s.cfg)
        cfg2["costs"]["symbols"]["NQ_DUKASCOPY"]["providers"]["DUKASCOPY"]["spread_source"] = "dataset"
        self.refused("PROTOCOL_CONFIG_CHANGED", self.svc(cfg2).backtest_strategy, self.ema, self.did, False,
                     self.disc_period(p))


class TestHoldoutLock(ProtocolBase):
    def test_discovery_allowed_holdout_refused_everywhere(self):                                # 4, 5
        s = self.svc()
        p = self.fresh(s, "t4")
        n_runs = len(s.store.list_runs())
        ok = s.backtest_strategy(self.ema, self.did, record=True, period=self.disc_period(p))
        self.assertEqual((ok["protocol"]["stage"], ok["protocol"]["counted"]), ("discovery", True))
        e = self.refused("HOLDOUT_LOCKED", s.backtest_strategy, self.ema, self.did)            # full dataset
        self.assertEqual(e.detail["holdout_trading_dates"], list(HOLD))
        self.refused("HOLDOUT_LOCKED", s.backtest_strategy, self.ema, self.did, False,
                     (self.disc_period(p)[0], "2024-04-10 00:00+00:00"))
        self.refused("HOLDOUT_LOCKED", s.evaluate_oos, self.ema, self.did, "2024-03-25", record=True)
        self.refused("HOLDOUT_LOCKED", s.walk_forward, self.ema, self.did, 1, 1, record=True)
        self.refused("HOLDOUT_LOCKED", s.random_entry_control, self.ema, self.did, 2)
        self.refused("HOLDOUT_LOCKED", s.plan_search, {"strategies": {"ids": [self.ema]}, "datasets": [self.did]})
        self.refused("HOLDOUT_LOCKED", s.run_search, {"strategies": {"ids": [self.ema]}, "datasets": [self.did],
                                                      "period": {"start": "2024-03-10T00:00:00+00:00",
                                                                 "end": "2024-04-20T00:00:00+00:00"}})
        self.assertEqual(len(s.store.list_runs()), n_runs + 1)                                  # no partial side effect
        v = s.evaluate_oos(self.ema, self.did, "2024-03-25", bounds=self.disc_period(p))       # internal validation
        self.assertEqual({w["status"] for w in v["windows"]}, {"IN_SAMPLE", "OUT_OF_SAMPLE"})
        st = s.protocol_status(p["protocol_id"])["trials"]
        self.assertEqual(st["by_entry_point"], {"backtest_strategy": 1, "internal_validation": 2})

    def test_ai_scope_cannot_reach_the_holdout_and_context_stays_blind(self):                   # 6, 18
        s = self.svc()
        p = self.fresh(s, "t6")
        req = {"mode": "hypothesis", "hypothesis": "After an oversold stretch price tends to revert toward its mean.",
               "scope": {"session": "NY_RTH", "dataset_id": self.did}}
        self.refused("AI_SCOPE_DATES_REQUIRED", s.ai_generate, req)
        req["scope"]["date_scope"] = {"start": "2024-03-04", "end": "2024-04-15"}
        self.refused("AI_SCOPE_HOLDOUT_OVERLAP", s.ai_context, req)
        req["scope"]["date_scope"] = {"start": "2024-03-04", "end": "2024-04-05"}
        ctx = s.ai_context(req)
        self.assertFalse(_keys(ctx) & FORBIDDEN_KEYS)
        text = json.dumps(ctx)
        self.assertNotIn("holdout", text.lower())
        self.assertNotIn(self.pre_run, text)
        g = s.ai_generate(req)
        self.assertEqual(g["scope"]["protocol_id"], p["protocol_id"])
        st = s.protocol_status(p["protocol_id"])
        self.assertEqual((st["proposal_attempts"]["total"], st["trials"]["unique_numerical_trials"]),
                         (len(g["proposals"]), 0))                                              # attempts are not trials
        s.ai_generate(req)                                                                      # deterministic re-run
        self.assertEqual(s.protocol_status(p["protocol_id"])["proposal_attempts"]["total"], len(g["proposals"]))
        s.ingest_proposals(str(FX / "proposals_example.yaml"))                                  # Mode B batch attempts
        st2 = s.protocol_status(p["protocol_id"])["proposal_attempts"]
        self.assertIn("mode_b_ingest", st2["by_source"])
        self.assertGreater(st2["total"], len(g["proposals"]))

    def test_holdout_only_through_the_permitted_stage(self):                                    # 7, 8, 9, 18
        s = self.svc()
        p = self.fresh(s, "t7")
        pid = p["protocol_id"]
        h = p["material"]["windows"]["holdout"]
        hold = (h["first_bar"], h["last_bar"])
        self.refused("HOLDOUT_LOCKED", s.backtest_strategy, self.ema, self.did, False, hold)
        self.refused("HOLDOUT_ACCESS_INVALID", s._run_cell, self.ema, self.did, False, period=hold,
                     entry_point="holdout_evaluation", holdout_access_id="HA_FAKE")
        srch = self.search(s, p, [self.ema, self.rsi])
        sid = srch["search_id"]
        self.refused("HOLDOUT_NOT_SHORTLISTED", s.evaluate_holdout, pid, sid, self.ema)
        s.select_shortlist(sid, [self.ema])
        n_trials = s.protocol_status(pid)["trials"]["unique_numerical_trials"]
        out = s.evaluate_holdout(pid, sid, self.ema)
        self.assertEqual((out["accepted"], out["run_status"], out["holdout_looks_used"]), (False, "OUT_OF_SAMPLE", 1))
        self.assertIn(out["outcome"], ("HOLDOUT_CRITERIA_MET", "HOLDOUT_CRITERIA_NOT_MET"))
        self.assertEqual(set(out["criteria"]), {"sample", "expectancy", "adjusted_confidence", "profit_factor",
                                                "random_control", "cost_stress"})
        self.assertEqual(out["multiple_testing"]["family_size"], 10_000)                       # declared, not counted
        self.assertLess(n_trials, 10_000)
        rec, _ = s.store.load_run(out["run_id"])
        self.assertEqual((rec["status"], pd.Timestamp(rec["dataset"]["start"]), pd.Timestamp(rec["dataset"]["end"])),
                         ("OUT_OF_SAMPLE", pd.Timestamp(h["first_bar"]), pd.Timestamp(h["last_bar"])))
        self.assertEqual(s.protocol_status(pid)["trials"]["unique_numerical_trials"], n_trials)   # holdout != trial
        self.refused("HOLDOUT_ALREADY_EVALUATED", s.evaluate_holdout, pid, sid, self.ema)
        led = s.store.list_holdout_access(pid)
        self.assertEqual([a["status"] for a in led], ["refused", "completed", "refused"])
        self.assertEqual([a["reason_code"] for a in led if a["status"] == "refused"],
                         ["HOLDOUT_NOT_SHORTLISTED", "HOLDOUT_ALREADY_EVALUATED"])
        req = {"mode": "hypothesis", "hypothesis": "After an oversold stretch price tends to revert toward its mean.",
               "scope": {"session": "NY_RTH", "dataset_id": self.did,
                         "date_scope": {"start": "2024-03-04", "end": "2024-04-05"}}}
        text = json.dumps(s.ai_context(req))                                                    # still blind
        self.assertNotIn(out["run_id"], text)
        self.assertNotIn(out["access_id"], text)

    def test_changed_stored_definition_is_refused_before_a_look_is_spent(self):
        s = self.svc()
        p = self.fresh(s, "t_tamper")
        pid = p["protocol_id"]
        sid = self.search(s, p, [self.ema])["search_id"]
        s.select_shortlist(sid, [self.ema])
        inst = s.library.root / "instances" / f"{self.ema}.json"
        orig = inst.read_text()
        self.addCleanup(inst.write_text, orig)
        doc = json.loads(orig)
        doc["definition"]["entry"]["long"]["right"]["params"]["period"] = 34                    # edited in place
        inst.write_text(json.dumps(doc))
        e = self.refused("HOLDOUT_DEFINITION_CHANGED", s.evaluate_holdout, pid, sid, self.ema)
        self.assertNotEqual(e.to_dict()["compiled_strategy_id"], self.ema)
        self.assertEqual(s.protocol_status(pid)["holdout"]["looks_used"], 0)                    # no look burned
        inst.write_text(orig)
        out = s.evaluate_holdout(pid, sid, self.ema)                                            # the real look
        self.assertEqual(out["holdout_looks_used"], 1)
        self.assertEqual([a["reason_code"] or a["status"] for a in s.store.list_holdout_access(pid)],
                         ["HOLDOUT_DEFINITION_CHANGED", "completed"])

    def test_holdout_look_and_trial_budgets(self):                                              # 10
        s = self.svc()
        p = self.fresh(s, "t10", holdout_looks=1, trial_budget=3)
        pid = p["protocol_id"]
        srch = self.search(s, p, [self.ema, self.rsi])
        s.select_shortlist(srch["search_id"], [self.ema, self.rsi])
        s.evaluate_holdout(pid, srch["search_id"], self.ema)
        self.refused("HOLDOUT_BUDGET_EXHAUSTED", s.evaluate_holdout, pid, srch["search_id"], self.rsi)
        per = self.disc_period(p)
        s.backtest_strategy(self.slow, self.did, False, per)                                    # third unique trial
        s.backtest_strategy(self.slow, self.did, False, per)                                    # duplicate: allowed
        self.refused("PROTOCOL_TRIAL_BUDGET_EXHAUSTED", s.backtest_strategy, self.slow, self.did, False,
                     (per[0], "2024-03-29 20:00+00:00"))
        self.refused("PROTOCOL_TRIAL_BUDGET_EXHAUSTED", s.run_search,
                     {"strategies": {"ids": [self.slow]}, "datasets": [self.did],
                      "period": {"start": per[0], "end": "2024-03-22T20:00:00+00:00"}})


class TestTrialLedger(ProtocolBase):
    def test_every_evaluation_attributed_and_deduplicated(self):                                # 11, 12
        s = self.svc()
        p = self.fresh(s, "t11")
        pid, per = p["protocol_id"], self.disc_period(p)
        a = s.backtest_strategy(self.ema, self.did, False, per)
        cosmetic = fixture("ema_crossover.yaml")
        cosmetic["name"] = "renamed ema"                                                        # same logic
        b = s.backtest_strategy(cosmetic, self.did, False, per)
        c = s.backtest_strategy(self.ema, self.did, False, (per[0], "2024-03-22 20:10+00:00"))  # another window
        self.assertEqual((a["protocol"]["counted"], b["protocol"]["counted"], c["protocol"]["counted"]), (True, False, True))
        self.assertEqual(a["protocol"]["trial_id"], b["protocol"]["trial_id"])
        s.random_entry_control(self.ema, self.did, n_controls=2, period=per)                    # candidate re-run: dup
        ev = s.store.list_trial_events(pid)
        self.assertTrue(all(e["protocol_id"] == pid and e["logic_hash"] and e["config_hash"] == p["material"]["config_hash"]
                            and e["cost_scenario"] == "dukascopy_directional_cost_assumption_v1" for e in ev))
        self.assertEqual([e["entry_point"] for e in ev],
                         ["backtest_strategy", "backtest_strategy", "backtest_strategy", "random_control_candidate"])
        st = s.protocol_status(pid)["trials"]
        self.assertEqual((st["unique_numerical_trials"], st["unique_logic_hashes"], st["duplicate_events"]), (2, 1, 2))
        self.assertEqual(sum(st["by_dataset_window"].values()), 2)

    def test_search_counts_resume_restart_and_parallel(self):                                   # 11, 14, 15
        s = self.svc()
        p = self.fresh(s, "t14")
        pid = p["protocol_id"]
        r1 = self.search(s, p, [self.ema, self.rsi])
        self.assertEqual((r1["protocol_id"], r1["n_trials"]), (pid, 2))
        self.assertEqual(s.protocol_status(pid)["trials"]["unique_numerical_trials"], 2)
        ev = [e for e in s.store.list_trial_events(pid) if e["entry_point"] == "search_cell"]
        self.assertTrue(all(e["search_id"] == r1["search_id"] and e["run_id"] for e in ev))
        s.store.close()
        s2 = self.svc()                                                                         # restart
        self.assertEqual(s2.protocol_status(pid)["trials"]["unique_numerical_trials"], 2)
        r2 = self.search(s2, p, [self.ema, self.rsi])                                           # resume: skipped
        self.assertEqual((r2["search_id"], r2["n_skipped_resume"]), (r1["search_id"], 2))
        r3 = self.search(s2, p, [self.ema, self.rsi], seed=7)                                   # a new search
        self.assertNotEqual(r3["search_id"], r1["search_id"])
        self.assertEqual(r3["n_trials"], 2)                                                     # search-local
        st = s2.protocol_status(pid)["trials"]
        self.assertEqual((st["unique_numerical_trials"], st["duplicate_events"]), (2, 2))       # program: no inflation
        rk = s2.rank_search(r3["search_id"])
        self.assertEqual((rk["program"]["protocol_id"], rk["program"]["program_unique_trials"]), (pid, 2))
        self.assertEqual(s2.select_shortlist(r3["search_id"], [self.ema])["protocol_id"], pid)
        r4 = self.search(s2, p, [self.ema, self.slow], workers=2)                               # parallel
        self.assertEqual(r4["execution"]["mode"], "processes")
        self.assertEqual(s2.protocol_status(pid)["trials"]["unique_numerical_trials"], 3)       # only slow is new

    def test_same_spec_under_another_protocol_is_another_search(self):
        s = self.svc()
        p = self.fresh(s, "t_mm")
        r = self.search(s, p, [self.ema])
        q = self.fresh(s, "t_mm2")
        r2 = self.search(s, q, [self.ema])
        self.assertNotEqual(r["search_id"], r2["search_id"])
        self.assertEqual((r["protocol_id"], r2["protocol_id"]), (p["protocol_id"], q["protocol_id"]))
        self.assertEqual(s.protocol_status(q["protocol_id"])["trials"]["unique_numerical_trials"], 1)


class TestInvariants(ProtocolBase):
    def test_historical_run_and_dataset_unchanged_and_legacy_unaffected(self):                  # 16, 17, 20
        s = self.svc()
        self.fresh(s, "t16")
        for p in s.store.list_protocols(status="ACTIVE"):
            s.retire_protocol(p["protocol_id"])
        legacy = s.backtest_strategy(self.ema, self.did)                                        # no ACTIVE protocol
        self.assertIsNone(legacy["protocol"])
        self.assertEqual(fingerprint(s, self.pre_run), self.pre_fp)
        self.assertEqual(s.load_dataset(self.did).manifest.content_hash, self.content_hash)
        self.assertFalse(any(e["run_id"] == self.pre_run for p in s.store.list_protocols()
                             for e in s.store.list_trial_events(p["protocol_id"])))
        self.assertIsNone(s.plan_search({"strategies": {"ids": [self.ema]}, "datasets": [self.did]})["protocol_id"])


if __name__ == "__main__":
    unittest.main()
