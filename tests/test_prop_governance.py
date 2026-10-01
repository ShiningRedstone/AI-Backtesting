"""ADR-67 pre-campaign validation of the prop layer: rule metadata (VERIFIED vs ASSUMED_DEFAULT), one hand-computed SUCCESSFUL
end-to-end lifecycle per profile (through the service path too), profile independence, the $50K default and the MNQ
binding of the generated universe. SYNTHETIC hand-built trades only; nothing here is a research trial."""
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from edgelab.engine.sizing import DEFAULT_RESEARCH_ACCOUNT
from edgelab.prop import lifecycle as L
from edgelab.prop import profiles as P
from edgelab.prop import service as PS
from tests.test_prop_lifecycle import DEF, DAILY, FLEX, GROWTH, LUCID, REPO, day, mk

MNQ_RUN = {"execution_contract": {"contract": "MNQ"}}
V3 = {pid: P.load_profile(P.profiles_dir(REPO) / f"{pid}.v3.yaml") for pid in DEF}

# ------------------------------------------------------------------ the four hand-computed success fixtures
LUCID_OK = [(0, 1500), (1, 1500)] + [(d, 500) for d in range(2, 12)] + [(12, -1800)]
GROWTH_OK = [(0, 3000)] + [(d, 700) for d in range(1, 6)]
FLEX_OK = [(0, 1000), (1, 1000), (2, 1000), (3, 1000, 20), (4, 600, 20), (5, 400, 30), (6, 300, 40), (7, 200, 40)]
DAILY_OK = [(0, 1000), (1, 1000), (2, 1000), (3, -1000, 20), (3, 500, 20), (4, 1600, 20), (5, 2000, 20), (6, 100, 40)]


def sim(rows, prof):
    return L.simulate_lifecycle(mk(rows), prof)


class TestRuleMetadata(unittest.TestCase):
    ASSUMED_ALL = ["evaluation.day_boundary.timezone", "evaluation.day_boundary.reset_time", "funded.day_boundary.timezone",
                   "funded.day_boundary.reset_time", "evaluation.drawdown.lock_enabled", "evaluation.drawdown.lock_trigger_offset",
                   "evaluation.drawdown.locked_floor_offset", "funded.drawdown.lock_enabled",
                   "funded.drawdown.lock_trigger_offset", "funded.drawdown.locked_floor_offset",
                   "evaluation.drawdown.measurement", "funded.drawdown.measurement", "evaluation.drawdown.breach_comparison",
                   "funded.drawdown.breach_comparison", "funded.carry_evaluation_profit", "funded.starts",
                   "payout.request", "payout.after_payout_drawdown"]
    ASSUMED = {
        "LUCID_LUCIDFLEX_50K": ["evaluation.consistency.cushion_enabled", "evaluation.consistency.cushion_amount_usd",
                                "evaluation.consistency.cushion_percent_points", "funded.scaling.persist",
                                "funded.scaling.basis", "payout.formula_profit_basis", "payout.balance_requirement_enabled"],
        "TRADEIFY_GROWTH_50K": ["evaluation.dll.measurement", "funded.dll.measurement", "payout.formula_mode",
                                "payout.formula_factor", "payout.formula_profit_basis", "payout.count_limit_enabled",
                                "funded.scaling.enabled", "funded.max_micros", "payout.positive_cycle_profit_required",
                                "evaluation.scaling.enabled", "live_transition.rule"],
        "TRADEIFY_SELECT_FLEX_50K": ["evaluation.drawdown.mode", "funded.drawdown.mode", "payout.buffer_enabled",
                                     "payout.cycle_reset", "payout.count_limit_enabled", "evaluation.scaling.enabled"],
        "TRADEIFY_SELECT_DAILY_50K": ["evaluation.drawdown.mode", "funded.drawdown.mode", "funded.dll.behavior",
                                      "funded.dll.measurement", "payout.frequency_mode",
                                      "payout.balance_requirement_enabled", "payout.count_limit_enabled"],
    }
    VERIFIED = {
        "LUCID_LUCIDFLEX_50K": ["account.size", "account.quantity_unit", "evaluation.profit_target", "evaluation.drawdown.max_loss",
                                "evaluation.drawdown.mode", "evaluation.consistency.percent", "evaluation.consistency.window",
                                "evaluation.max_micros", "evaluation.scaling.enabled", "evaluation.dll.enabled",
                                "evaluation.minimum_trading_days", "funded.starting_balance", "funded.max_micros",
                                "funded.scaling.tiers", "funded.scaling.start_micros", "funded.scaling.update",
                                "payout.split_trader", "payout.winning_day_threshold", "payout.winning_days_required",
                                "payout.minimum", "payout.formula_factor", "payout.cap_schedule", "payout.count_limit",
                                "payout.buffer_offset", "payout.cycle_reset", "live_transition.rule"],
        "TRADEIFY_GROWTH_50K": ["evaluation.profit_target", "evaluation.dll.amount", "evaluation.dll.behavior",
                                "evaluation.minimum_trading_days", "evaluation.max_micros", "evaluation.drawdown.mode",
                                "funded.consistency.percent", "funded.dll.amount", "payout.balance_requirement",
                                "payout.cap_schedule", "payout.minimum", "payout.split_trader", "payout.winning_day_threshold"],
        "TRADEIFY_SELECT_FLEX_50K": ["evaluation.consistency.percent", "evaluation.minimum_trading_days", "evaluation.dll.enabled",
                                     "funded.dll.enabled", "funded.consistency.enabled", "funded.scaling.tiers",
                                     "payout.formula_factor", "payout.cap_table", "payout.minimum", "account.purchase_date",
                                     "payout.balance_requirement_enabled"],
        "TRADEIFY_SELECT_DAILY_50K": ["funded.dll.enabled", "funded.dll.amount", "payout.buffer_offset", "payout.formula_factor",
                                      "payout.formula_profit_basis", "payout.cap_table", "payout.minimum",
                                      "payout.positive_cycle_profit_required", "payout.cycle_reset"],
    }

    def test_assumptions_are_never_labelled_verified(self):
        for pid, p in DEF.items():
            for k in self.ASSUMED_ALL + self.ASSUMED[pid]:
                self.assertEqual(p["rules"][k]["status"], P.RULE_ASSUMED, (pid, k))
                self.assertIn(p["rules"][k]["basis"].split()[0], ("EdgeLab", "no", "'EOD'", "Growth", "Select", "Growth:",
                                                                  "LucidFlex", "cushion", "Select", "tiers"), (pid, k))
            for k in self.VERIFIED[pid]:
                self.assertEqual(p["rules"][k]["status"], P.RULE_VERIFIED, (pid, k))
            for k, r in p["rules"].items():
                self.assertIsNotNone(r["value"], (pid, k))
                self.assertTrue(r["basis"].strip())
                if r["status"] == P.RULE_VERIFIED:
                    self.assertNotIn("assumed", r["basis"].lower(), (pid, k))

    def test_metadata_correction_changes_no_value_and_no_simulation_result(self):
        for pid in DEF:
            self.assertEqual({k: r["value"] for k, r in DEF[pid]["rules"].items()},
                             {k: r["value"] for k, r in V3[pid]["rules"].items()}, pid)
        drop = ("verified_rule_count", "assumed_rule_count", "custom_rule_count", "rule_profile_version", "final_status")
        for rows in (LUCID_OK, GROWTH_OK, FLEX_OK, DAILY_OK, [(0, 1500), (1, -2100)]):
            for pid in DEF:
                a, b = sim(rows, DEF[pid]), sim(rows, V3[pid])
                for k in ("evaluation", "funded", "payouts", "totals", "events", "status"):
                    self.assertEqual(a[k], b[k], (pid, k))
                self.assertEqual({k: v for k, v in a["summary"].items() if k not in drop},
                                 {k: v for k, v in b["summary"].items() if k not in drop})


class TestSuccessfulLifecycles(unittest.TestCase):
    def test_A_lucidflex_pass_funded_two_payouts_then_breach(self):
        r = sim(LUCID_OK, LUCID)
        s = r["summary"]
        self.assertEqual((s["evaluation_status"], r["evaluation"]["pass"]["date"]), ("PASS", day(1)))
        self.assertEqual(s["funded_start_timestamp"], f"{day(1)}T18:00:00-05:00")          # EST (before DST)
        p1, p2 = r["payouts"]
        self.assertEqual((p1["date"], p1["gross"], p1["trader_share"], p1["firm_share"], p1["balance_after"]),
                         (day(6), 1250.0, 1125.0, 125.0, 51250.0))                 # 50% x 2,500
        self.assertEqual((p2["date"], p2["gross"], p2["trader_share"], p2["balance_after"]),
                         (day(11), 1875.0, 1687.5, 51875.0))                       # 50% x 3,750
        self.assertEqual(len(p2["winning_days"]), 5)                              # the cycle reset after payout 1
        self.assertEqual((s["funded_status"], s["funded_failure_reason"], s["final_balance"]), ("FAIL", "MAX_LOSS_LIMIT", 50075.0))
        self.assertEqual(r["funded"]["breach"]["state_at_breach"]["floor"], 50100.0)   # locked floor
        self.assertEqual(s["total_trader_payout"], 2812.5)
        self.assertIn("EVAL PASS UNDER DEFAULT ASSUMED RULES", r["headline"])

    def test_B_growth_pass_funded_payout(self):
        r = sim(GROWTH_OK, GROWTH)
        s = r["summary"]
        self.assertEqual((s["evaluation_status"], s["funded_status"], s["funded_outcome"]),
                         ("PASS", "PASS", "ACTIVE_AT_END_OF_DATA"))
        (p,) = r["payouts"]
        self.assertEqual((p["date"], p["cap"], p["gross"], p["trader_share"], p["balance_after"]),
                         (day(5), 1500.0, 1500.0, 1350.0, 52000.0))
        self.assertTrue(p["consistency"]["met"])                                   # 700 / 3,500 = 20% <= 35%
        self.assertEqual(r["status"], "PASS")

    def test_C_select_flex_pass_funded_scaling_payout(self):
        r = sim(FLEX_OK, FLEX)
        self.assertEqual(r["evaluation"]["pass"]["date"], day(2))
        self.assertEqual([d["permitted_micros"] for d in r["funded"]["days"]], [20.0, 20.0, 30.0, 40.0, 40.0])
        (p,) = r["payouts"]
        self.assertEqual((p["date"], p["gross"], p["trader_share"], p["balance_after"]), (day(7), 1250.0, 1125.0, 51250.0))
        self.assertEqual(r["funded"]["next_session_permitted_micros"], 20.0)       # payout moved the tier down
        self.assertEqual(r["summary"]["funded_status"], "PASS")

    def test_D_select_daily_pass_funded_dll_scaling_payout(self):
        r = sim(DAILY_OK, DAILY)
        f = r["funded"]
        self.assertEqual((f["days"][0]["n_trades"], f["days"][0]["n_skipped_dll"], f["dll_soft_breaches"]), (1, 1, 1))
        self.assertEqual([d["permitted_micros"] for d in f["days"]], [20.0, 20.0, 20.0, 40.0])
        (p,) = r["payouts"]
        self.assertEqual((p["date"], p["formula_amount"], p["cap"], p["withdrawable"], p["gross"], p["trader_share"]),
                         (day(5), 5200.0, 1250.0, 500.0, 500.0, 450.0))           # the $2,100 buffer binds
        unmet = {c["date"]: c["unmet"] for c in f["payout_checks"]}
        self.assertEqual((unmet[day(3)], unmet[day(4)], unmet[day(6)]),
                         (["POSITIVE_CYCLE_PROFIT"], ["BELOW_MINIMUM_PAYOUT"], ["BELOW_MINIMUM_PAYOUT"]))
        self.assertEqual((f["status"], r["summary"]["total_trader_payout"]), ("PASS", 450.0))

    def test_the_service_path_carries_the_same_results_for_every_profile(self):
        for rows, pid in ((LUCID_OK, "LUCID_LUCIDFLEX_50K"), (GROWTH_OK, "TRADEIFY_GROWTH_50K"),
                          (FLEX_OK, "TRADEIFY_SELECT_FLEX_50K"), (DAILY_OK, "TRADEIFY_SELECT_DAILY_50K")):
            out = PS.outcomes(REPO, mk(rows), assumptions=MNQ_RUN)
            got = {p["profile"]["profile_id"]: p for p in out["profiles"]}
            self.assertEqual(set(got), set(DEF))
            self.assertEqual(got[pid]["summary"], sim(rows, DEF[pid])["summary"])
            self.assertEqual(got[pid]["summary"]["evaluation_status"], "PASS")
            self.assertGreater(got[pid]["summary"]["payout_count"], 0)
            self.assertEqual(out["base"]["trade_count"], len(rows))


class TestProfileIndependence(unittest.TestCase):
    def outcomes(self, profiles, rows):
        with mock.patch("edgelab.prop.service.default_profiles", return_value=profiles):
            return {p["profile"]["profile_id"]: p for p in PS.outcomes(REPO, mk(rows), assumptions=MNQ_RUN)["profiles"]}

    def test_changing_one_profile_changes_only_that_profile(self):
        base = self.outcomes(list(DEF.values()), DAILY_OK)
        for pid, change in (("TRADEIFY_SELECT_DAILY_50K", {"funded.dll.behavior": "hard"}),
                            ("TRADEIFY_SELECT_FLEX_50K", {"payout.minimum": 2000}),
                            ("LUCID_LUCIDFLEX_50K", {"evaluation.day_boundary.reset_time": "23:00"}),
                            ("TRADEIFY_GROWTH_50K", {"funded.drawdown.max_loss": 500})):
            profs = [P.customize(p, change, "independence test") if p["profile_id"] == pid else p for p in DEF.values()]
            new = self.outcomes(profs, DAILY_OK if pid != "TRADEIFY_GROWTH_50K" else DAILY_OK)
            for other in DEF:
                if other == pid:
                    self.assertEqual(new[other]["summary"]["custom_rule_count"], 1)
                else:
                    self.assertEqual(new[other], base[other], (pid, other))

    def test_stage_rules_do_not_leak_between_evaluation_and_funded(self):
        e = P.customize(LUCID, {"evaluation.drawdown.max_loss": 3500}, "t")
        a, b = sim(LUCID_OK, LUCID), sim(LUCID_OK, e)
        self.assertEqual(a["funded"]["breach"]["state_at_breach"]["floor"], b["funded"]["breach"]["state_at_breach"]["floor"])
        self.assertEqual(a["payouts"], b["payouts"])                               # funded rules unchanged
        g = P.customize(GROWTH, {"evaluation.dll.amount": 100}, "t")
        rows = [(0, 3000), (1, -200), (1, 900)]                                    # funded day: -200 then +900
        self.assertEqual(sim(rows, g)["funded"]["days"][0]["n_trades"], 2)          # funded DLL still 1,250
        f = P.customize(LUCID, {"funded.scaling.start_micros": 5}, "t")
        self.assertEqual(sim([(0, 1500, 20), (1, 1500, 20)], f)["evaluation"]["status"], "PASS")   # eval max 40 kept

    def test_branches_do_not_inherit_each_other(self):
        rows = [(0, 1000), (1, 1000), (2, 1000), (3, -1000), (3, 500)]
        self.assertEqual(sim(rows, FLEX)["funded"]["days"][0]["n_trades"], 2)       # Flex: no Daily DLL
        self.assertEqual(sim(rows, DAILY)["funded"]["days"][0]["n_trades"], 1)
        self.assertNotEqual(FLEX["rules"]["payout.formula_mode"]["value"], DAILY["rules"]["payout.formula_mode"]["value"])
        self.assertNotEqual(L.payout_cap(FLEX, 1), L.payout_cap(DAILY, 1))
        for k in ("evaluation.day_boundary.reset_time", "funded.drawdown.max_loss", "payout.formula_mode"):
            for p in DEF.values():
                self.assertIn(k, p["rules"])                                       # every profile carries its own value


class TestResearchEnvironment(unittest.TestCase):
    def test_no_100k_default_in_the_active_research_path(self):
        self.assertEqual(DEFAULT_RESEARCH_ACCOUNT["starting_equity"], 50_000.0)
        files = [*(REPO / "edgelab" / "strategy").glob("*.py"), *(REPO / "edgelab" / "engine").glob("*.py"),
                 *(REPO / "edgelab" / "prop").glob("*.py"), *(REPO / "edgelab" / "research").glob("*.py"),
                 REPO / "edgelab" / "services.py", *(REPO / "configs").glob("*.yaml"),
                 *(REPO / "configs" / "prop" / "profiles").glob("*.yaml")]
        for f in files:
            self.assertIsNone(re.search(r"100[_,]?000(?!\d)", f.read_text()), f.name)

    def test_generated_universe_is_mnq_whole_contracts_and_account_free(self):
        from edgelab.strategy import factory as FX
        from edgelab.strategy import factory_space as S
        res = FX.generate(seed=20260930, quotas={f.fid: 4 for f in S.FAMILIES})
        for r in res.strategies:
            sz = r["definition"]["sizing"]
            self.assertEqual(sz["contract"], "MNQ", r["strategy_id"])
            self.assertIn(sz["mode"], ("fixed", "risk", "equity_risk"))
            if sz["mode"] == "fixed":
                self.assertIsInstance(sz["quantity"], int)
            else:
                self.assertEqual(sz["max_quantity"], 40)
            blob = json.dumps(r["definition"]).lower()
            for bad in ("50000", "starting_equity", "lucid", "tradeify"):
                self.assertNotIn(bad, blob)


if __name__ == "__main__":
    unittest.main()
