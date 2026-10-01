"""ADR-64..66: versioned schema-3 prop rulebooks (every rule = value + VERIFIED / ASSUMED_DEFAULT / CUSTOM + basis) and the
chronological evaluation -> funded -> payout lifecycle.

Known answers against the SHIPPED default profiles: every expected figure is hand-computed from those defaults.
SYNTHETIC trade streams only."""
import copy
import json
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from edgelab.engine.sizing import DEFAULT_RESEARCH_ACCOUNT, contracts_for_risk
from edgelab.prop import lifecycle as L
from edgelab.prop import profiles as P
from tests.helpers import INSTRUMENTS

REPO = Path(__file__).resolve().parents[1]
DAY0 = date(2024, 3, 4)            # a Monday
DEF = {p["profile_id"]: p for p in P.load_default_profiles(REPO)}
LUCID, GROWTH = DEF["LUCID_LUCIDFLEX_50K"], DEF["TRADEIFY_GROWTH_50K"]
FLEX, DAILY = DEF["TRADEIFY_SELECT_FLEX_50K"], DEF["TRADEIFY_SELECT_DAILY_50K"]


def day(d: int) -> str:
    return str(DAY0 + timedelta(days=d + 2 * (d // 5)))                     # skip weekends


def mk(rows):
    """rows: (day_index, net_usd, micros=20, mae_usd=0); several rows on one day are placed hourly from 09:00 NY."""
    out, per_day = [], {}
    for r in rows:
        d, pnl = r[0], r[1]
        q = r[2] if len(r) > 2 else 20
        mae = r[3] if len(r) > 3 else 0.0
        k = per_day.get(d, 0)
        per_day[d] = k + 1
        ent = pd.Timestamp(f"{day(d)} {9 + k:02d}:00", tz="America/New_York").tz_convert("UTC")
        out.append({"trade_no": len(out) + 1, "entry_ts": ent, "exit_ts": ent + pd.Timedelta(minutes=30), "contracts": q,
                    "net_usd": float(pnl), "mae_points": float(mae), "risk_points": 1.0, "risk_usd": 1.0})
    return pd.DataFrame(out)


def mod(profile, changes):
    """A CUSTOM copy (new version) with the given rule values changed."""
    return P.customize(profile, changes, "test change")


def V(p, key):
    return p["rules"][key]["value"]


def S(p, key):
    return p["rules"][key]["status"]


def sim(rows, prof):
    return L.simulate_lifecycle(mk(rows), prof)


def events(r, typ):
    return [e for e in r["events"] if e["type"] == typ]


LUCID_PASS = [(0, 1500), (1, 1500)]
GROWTH_PASS = [(0, 3000)]
SEL_PASS = [(0, 1000), (1, 1000), (2, 1000)]


class TestAccount(unittest.TestCase):
    def test_default_account_and_whole_mnq_contracts(self):
        self.assertEqual(DEFAULT_RESEARCH_ACCOUNT["starting_equity"], 50_000.0)
        mnq = INSTRUMENTS["MNQ"]
        self.assertEqual((mnq.point_value, mnq.tick_size, mnq.tick_value, mnq.min_size, mnq.size_step), (2.0, 0.25, 0.5, 1.0, 1.0))
        d = contracts_for_risk(250, 7.0, mnq)                 # 250 / 14 = 17.86 -> 17, never up
        self.assertEqual(d.contracts, 17)
        self.assertLessEqual(d.contracts * 7.0 * 2.0, 250)
        self.assertEqual(contracts_for_risk(10, 7.0, mnq).contracts, 0)            # zero contracts rejects the trade
        self.assertEqual(contracts_for_risk(1000, 1.0, mnq, max_contracts=40).contracts, 40)
        for p in DEF.values():
            self.assertEqual((V(p, "account.size"), V(p, "account.quantity_unit")), (50000, "MNQ"))


class TestRulebook(unittest.TestCase):
    def test_every_rule_has_value_status_and_basis_and_none_is_null(self):
        for p in DEF.values():
            self.assertEqual((p["schema_version"], p["version"]), (3, 4))
            self.assertEqual(set(p["rules"]), set(P.RULE_SPEC))
            for k, r in p["rules"].items():
                self.assertIsNotNone(r["value"], k)
                self.assertIn(r["status"], P.RULE_STATUSES)
                self.assertTrue(r["basis"].strip(), k)
        bad = copy.deepcopy(LUCID)
        bad["rules"]["payout.minimum"]["value"] = None
        with self.assertRaises(P.ProfileError):
            P.validate_profile(bad)
        bad = copy.deepcopy(LUCID)
        del bad["rules"]["payout.minimum"]
        with self.assertRaises(P.ProfileError):
            P.validate_profile(bad)

    def test_status_metadata(self):
        self.assertEqual(S(LUCID, "evaluation.profit_target"), P.RULE_VERIFIED)
        for p in DEF.values():                                                    # section 10 A-F: assumed defaults
            for k in ("evaluation.drawdown.lock_enabled", "funded.drawdown.lock_trigger_offset",
                      "funded.drawdown.locked_floor_offset", "evaluation.day_boundary.reset_time",
                      "funded.day_boundary.reset_time", "funded.drawdown.measurement", "evaluation.dll.measurement"):
                self.assertEqual(S(p, k), P.RULE_ASSUMED, (p["profile_id"], k))
        self.assertEqual((V(DAILY, "funded.dll.behavior"), S(DAILY, "funded.dll.behavior")), ("soft", P.RULE_ASSUMED))
        self.assertEqual((V(LUCID, "evaluation.consistency.cushion_enabled"), V(LUCID, "evaluation.consistency.cushion_amount_usd"),
                          S(LUCID, "evaluation.consistency.cushion_amount_usd")), (True, 0, P.RULE_ASSUMED))
        for k in ("payout.formula_factor", "payout.count_limit_enabled", "funded.scaling.enabled"):
            self.assertEqual(S(GROWTH, k), P.RULE_ASSUMED, k)
        c = mod(DAILY, {"funded.dll.behavior": "hard"})
        self.assertEqual((S(c, "funded.dll.behavior"), c["version"]), (P.RULE_CUSTOM, 5))
        rb = P.rule_basis(c)
        self.assertEqual((rb["custom_rule_count"], list(rb["custom_rules"])), (1, ["funded.dll.behavior"]))
        self.assertEqual(S(DAILY, "funded.dll.behavior"), P.RULE_ASSUMED)          # the original is untouched

    def test_basis_counts_are_reported_on_every_result(self):
        r = sim(LUCID_PASS, LUCID)
        rb = P.rule_basis(LUCID)
        self.assertEqual(r["rule_basis_state"], "RULE_ASSUMED")
        s = r["summary"]
        self.assertEqual((s["verified_rule_count"], s["assumed_rule_count"], s["custom_rule_count"]),
                         (rb["verified_rule_count"], rb["assumed_rule_count"], 0))
        self.assertGreater(s["assumed_rule_count"], 0)
        self.assertIn("UNDER DEFAULT ASSUMED RULES", r["final_status"])
        self.assertIn("RULE BASIS", " ".join(r["headline"]))
        self.assertIn("Simulation used EdgeLab default assumptions", " ".join(r["headline"]))
        self.assertNotIn("PROVIDER PASS", json.dumps(r, default=str))

    def test_registry_append_only_edits_detected_superseded_versions_claim_nothing(self):
        d = P.profiles_dir(REPO)
        self.assertEqual(P.check_registry(d), [])
        vers = {(e["profile_id"], e["version"]) for e in P.read_registry(d)["profiles"]}
        for pid in DEF:
            self.assertTrue({(pid, 1), (pid, 2), (pid, 3), (pid, 4)} <= vers)
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(d, tmp / "p")
            f = tmp / "p" / "LUCID_LUCIDFLEX_50K.v3.yaml"
            with self.assertRaises(P.ProfileError):
                P.register_profile(tmp / "p", f)
            f.write_text(f.read_text().replace("value: 3000", "value: 3100", 1))
            self.assertTrue(any("content changed" in x for x in P.check_registry(tmp / "p")))
        finally:
            shutil.rmtree(tmp)
        for v in (1, 2):
            old = P.load_profile(d / f"LUCID_LUCIDFLEX_50K.v{v}.yaml")
            r = L.simulate_lifecycle(mk(LUCID_PASS), old)
            self.assertEqual(r["status"], "NOT_APPLICABLE")
            self.assertNotIn("evaluation", r)

    def test_rules_are_variables_not_code(self):
        import inspect
        src = inspect.getsource(L)
        for n in ("3000", "2000", "1250", "53000", "2100", "52100", "50100", "150", "0.9", "0.35", "0.4", "18:00"):
            self.assertNotIn(n, src, f"provider number {n} hard-coded in the simulator")
        self.assertEqual(sim([(0, 2600)], mod(GROWTH, {"evaluation.profit_target": 2500}))["evaluation"]["status"], "PASS")
        self.assertEqual(sim([(0, 2600)], GROWTH)["evaluation"]["status"], "FAIL")

    def test_purchase_date_without_a_cap_is_refused_at_load(self):
        with self.assertRaises(P.ProfileError):
            mod(FLEX, {"account.purchase_date": "2026-08-01"})
        with self.assertRaises(P.ProfileError):
            mod(LUCID, {"conventions.on_oversize": "clip"})


class TestLucidFlex(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual((LUCID["provider"], LUCID["product"]), ("Lucid Trading", "LucidFlex"))
        exp = {"evaluation.profit_target": 3000, "evaluation.drawdown.max_loss": 2000, "evaluation.drawdown.mode": "eod_trailing",
               "evaluation.consistency.enabled": True, "evaluation.consistency.percent": 50, "evaluation.max_micros": 40,
               "evaluation.scaling.enabled": False, "evaluation.dll.enabled": False, "evaluation.dll.amount": 0,
               "evaluation.minimum_trading_days": 0, "evaluation.drawdown.lock_enabled": True,
               "evaluation.drawdown.lock_trigger_offset": 2100, "evaluation.drawdown.locked_floor_offset": 100,
               "funded.starting_balance": 50000, "funded.drawdown.max_loss": 2000, "funded.consistency.enabled": False,
               "funded.max_micros": 40, "funded.dll.enabled": False, "funded.scaling.start_micros": 20,
               "funded.scaling.tiers": [{"min_profit": 1000, "micros": 30}, {"min_profit": 2000, "micros": 40}],
               "funded.scaling.update": "end_of_session", "funded.scaling.effective": "next_session",
               "payout.buffer_enabled": True, "payout.buffer_offset": 0, "payout.split_trader": 0.9,
               "payout.winning_day_threshold": 150, "payout.winning_days_required": 5,
               "payout.positive_cycle_profit_required": True, "payout.minimum": 500, "payout.formula_factor": 0.5,
               "payout.cap_mode": "fixed", "payout.cap_schedule": [2000], "payout.count_limit_enabled": True,
               "payout.count_limit": 5, "payout.cycle_reset": "after_payout", "live_transition.rule": "after_payout_count",
               "live_transition.payout_count": 5}
        for k, v in exp.items():
            self.assertEqual(V(LUCID, k), v, k)

    def test_pass_requires_consistency_not_just_the_target(self):
        r = sim([(0, 1600), (1, 1400), (2, 200)], LUCID)
        tr = events(r, "TARGET_REACHED_NOT_PASSED")
        self.assertEqual((tr[0]["date"], tr[0]["unmet"]), (day(1), ["CONSISTENCY"]))
        self.assertEqual(r["evaluation"]["pass"]["date"], day(2))                 # 1600 <= 50% x 3200
        r = sim([(0, 2900), (1, 200)], LUCID)                                    # hit target, fail consistency
        s = r["summary"]
        self.assertEqual((s["evaluation_status"], s["evaluation_failure_reason"]), ("FAIL", "NOT_PASSED_BY_END_OF_DATA"))
        self.assertTrue(r["evaluation"]["target_reached_but_not_passed"])
        self.assertIn("FAIL UNDER DEFAULT ASSUMED RULES", r["final_status"])

    def test_consistency_cushion_is_configurable_and_reported_separately(self):
        self.assertEqual(sim([(0, 1600), (1, 1400)], LUCID)["evaluation"]["status"], "FAIL")
        c = sim([(0, 1600), (1, 1400)], mod(LUCID, {"evaluation.consistency.cushion_amount_usd": 100}))
        self.assertEqual(c["evaluation"]["status"], "PASS")
        cons = c["evaluation"]["pass"]["consistency"]
        self.assertEqual((cons["normal_rule_met"], cons["met_with_cushion"], cons["cushion_allowance"]), (False, True, 100.0))
        self.assertIn("UNDER DEFAULT ASSUMED RULES", c["final_status"])          # still also has assumed rules

    def test_40_micro_evaluation_max_incompatible_never_clipped(self):
        self.assertNotEqual(sim([(0, 100, 40)], LUCID)["evaluation"]["status"], "INCOMPATIBLE")
        r = sim([(0, 100, 41)], LUCID)
        b = r["evaluation"]["breach"]
        self.assertEqual((b["requested_quantity"], b["permitted_quantity"], b["rule"]), (41.0, 40.0, "evaluation.max_micros"))
        self.assertEqual((r["evaluation"]["status"], r["status"]), ("INCOMPATIBLE", "INCOMPATIBLE"))
        self.assertTrue(r["final_status"].startswith("EVAL INCOMPATIBLE"))

    def test_pass_enters_funded_and_scaling_violation_is_incompatible(self):
        r = sim(LUCID_PASS + [(2, 100, 25)], LUCID)
        s = r["summary"]
        self.assertEqual((s["evaluation_status"], s["funded_status"], s["funded_failure_reason"]),
                         ("PASS", "INCOMPATIBLE", "SCALING_LIMIT_VIOLATION"))
        self.assertEqual(s["funded_start_timestamp"],
                         pd.Timestamp(f"{day(1)} 18:00", tz="America/New_York").isoformat())
        b = r["funded"]["breach"]
        self.assertEqual((b["requested_quantity"], b["permitted_quantity"]), (25.0, 20.0))
        r = sim(LUCID_PASS + [(2, 1000, 20), (2, 100, 30)], LUCID)                # +1000 intraday: still 20 today
        self.assertEqual(r["funded"]["breach"]["permitted_quantity"], 20.0)
        r = sim(LUCID_PASS + [(2, 1000, 20), (3, 1000, 30), (4, 100, 40)], LUCID)
        self.assertEqual((r["funded"]["status"], r["funded"]["outcome"]), ("PASS", "ACTIVE_AT_END_OF_DATA"))
        self.assertEqual([d["permitted_micros"] for d in r["funded"]["days"]], [20.0, 30.0, 40.0])

    def test_35_micros_against_a_20_micro_funded_limit(self):
        rows = LUCID_PASS + [(2, 100, 35)]
        t = mk(rows)
        keep = t.copy()
        r = L.simulate_lifecycle(t, LUCID)
        self.assertEqual(r["summary"]["funded_status"], "INCOMPATIBLE")
        self.assertIn("requested 35 micros, permitted 20 micros", r["summary"]["funded_failure_detail"])
        pd.testing.assert_frame_equal(t, keep)                                    # the strategy trade still says 35
        self.assertEqual(r["quantity"]["max"], 35.0)

    def test_assumed_lock_is_active_and_changing_it_changes_the_result(self):
        r = sim(LUCID_PASS + [(2, 3000), (3, -2800)], LUCID)
        f = r["funded"]
        self.assertEqual((f["status"], f["floor"], f["floor_locked"], f["balance"]), ("PASS", 50100.0, True, 50200.0))
        r2 = sim(LUCID_PASS + [(2, 3000), (3, -2800)], mod(LUCID, {"funded.drawdown.lock_enabled": False}))
        self.assertEqual((r2["funded"]["status"], r2["funded"]["breach"]["reason"]), ("FAIL", "MAX_LOSS_LIMIT"))

    def test_payout_formula_split_and_scaling_moves_down_after_payout(self):
        r = sim(LUCID_PASS + [(d, 500) for d in range(2, 7)], LUCID)
        (p,) = r["payouts"]
        self.assertEqual((p["date"], p["cycle_profit"], p["formula_amount"], p["cap"], p["gross"]),
                         (day(6), 2500.0, 1250.0, 2000.0, 1250.0))
        self.assertEqual((p["trader_share"], p["firm_share"], p["balance_before"], p["balance_after"]),
                         (1125.0, 125.0, 52500.0, 51250.0))
        self.assertEqual(r["funded"]["days"][-2]["next_session_permitted_micros"], 40.0)
        self.assertEqual(r["funded"]["next_session_permitted_micros"], 30.0)
        s = r["summary"]
        self.assertEqual((s["payout_count"], s["payout_dates"], s["payout_gross_amounts"], s["payout_trader_amounts"],
                          s["total_trader_payout"]), (1, [day(6)], [1250.0], [1125.0], 1125.0))

    def test_profitable_days(self):
        r = sim(LUCID_PASS + [(d, 500) for d in range(2, 6)] + [(6, 149), (7, 150)], LUCID)
        (p,) = r["payouts"]
        self.assertEqual((p["date"], len(p["winning_days"])), (day(7), 5))
        r = sim(LUCID_PASS + [(d, 500) for d in range(2, 6)] + [(6, 149)], LUCID)
        self.assertEqual(r["payouts"], [])

    def test_minimum_and_cap(self):
        r = sim(LUCID_PASS + [(d, 150) for d in range(2, 7)], LUCID)
        self.assertEqual(r["payouts"], [])
        self.assertIn("BELOW_MINIMUM_PAYOUT", events(r, "PAYOUT_NOT_ELIGIBLE")[0]["unmet"])
        r = sim(LUCID_PASS + [(d, 1000) for d in range(2, 7)], LUCID)
        self.assertEqual((r["payouts"][0]["gross"], r["payouts"][0]["trader_share"]), (2000.0, 1800.0))

    def test_insufficient_cycle_profit(self):
        r = sim(LUCID_PASS + [(2, -1500)] + [(d, 200) for d in range(3, 8)], LUCID)
        self.assertEqual(r["payouts"], [])
        self.assertIn("POSITIVE_CYCLE_PROFIT", events(r, "PAYOUT_NOT_ELIGIBLE")[0]["unmet"])

    def test_five_payouts_then_live_transition_eligibility_only(self):
        r = sim(LUCID_PASS + [(d, 500) for d in range(2, 27)] + [(27, -5000)], LUCID)
        self.assertEqual(len(r["payouts"]), 5)
        self.assertEqual((r["funded"]["status"], r["funded"]["outcome"]), ("PASS", "LIVE_TRANSITION_ELIGIBLE"))
        self.assertIn("not guaranteed", " ".join(r["headline"]))
        for x in r["payouts"]:
            self.assertAlmostEqual(x["trader_share"], 0.9 * x["gross"])
            self.assertLessEqual(x["gross"], 2000.0)

    def test_payout_followed_by_later_breach(self):
        r = sim(LUCID_PASS + [(d, 500) for d in range(2, 7)] + [(7, -1200)], LUCID)
        s = r["summary"]
        self.assertEqual((s["payout_count"], s["funded_status"], s["funded_failure_reason"], s["final_balance"]),
                         (1, "FAIL", "MAX_LOSS_LIMIT", 50050.0))
        self.assertEqual(r["status"], "FAIL")

    def test_dll_off_by_default(self):
        self.assertEqual(sim([(0, -1900), (0, 1000)], LUCID)["evaluation"]["days"][0]["n_trades"], 2)


class TestTradeifyGrowth(unittest.TestCase):
    def test_defaults(self):
        exp = {"evaluation.profit_target": 3000, "evaluation.drawdown.max_loss": 2000, "evaluation.minimum_trading_days": 1,
               "evaluation.max_micros": 40, "evaluation.dll.enabled": True, "evaluation.dll.amount": 1250,
               "evaluation.dll.behavior": "soft", "evaluation.consistency.enabled": False, "funded.dll.amount": 1250,
               "funded.consistency.enabled": True, "funded.consistency.percent": 35, "payout.winning_day_threshold": 150,
               "payout.winning_days_required": 5, "payout.balance_requirement": 53000, "payout.minimum": 500,
               "payout.split_trader": 0.9, "payout.cap_schedule": [1500, 2000, 2500, 3000], "funded.scaling.enabled": False,
               "payout.positive_cycle_profit_required": False, "payout.count_limit_enabled": False,
               "funded.drawdown.lock_trigger_offset": 2100, "funded.drawdown.locked_floor_offset": 100}
        for k, v in exp.items():
            self.assertEqual(V(GROWTH, k), v, k)
        self.assertEqual([L.payout_cap(GROWTH, n) for n in range(1, 7)], [1500, 2000, 2500, 3000, 3000, 3000])

    def test_soft_dll_stops_the_day_and_continues_next_day(self):
        r = sim([(0, -800), (0, -500), (0, 2000), (1, 1500), (2, 1500), (3, 1300)], GROWTH)
        ev = r["evaluation"]
        self.assertEqual((ev["days"][0]["n_trades"], ev["days"][0]["n_skipped_dll"], ev["dll_soft_breaches"]), (2, 1, 1))
        self.assertEqual((ev["status"], ev["pass"]["date"]), ("PASS", day(3)))
        r = sim([(0, -800), (0, -500), (1, 5000)], mod(GROWTH, {"evaluation.dll.behavior": "hard"}))
        self.assertEqual((r["evaluation"]["status"], r["evaluation"]["failure_reason"]), ("FAIL", "DAILY_LOSS_LIMIT"))

    def test_dll_measurement_is_configurable(self):
        rows = [(0, -600, 20, 1300), (0, 3000)]                                   # closed -600, worst inside -1300
        self.assertEqual(sim(rows, GROWTH)["evaluation"]["days"][0]["n_trades"], 2)
        r = sim(rows, mod(GROWTH, {"evaluation.dll.measurement": "intraday_worst_price"}))
        self.assertEqual(r["evaluation"]["days"][0]["n_skipped_dll"], 1)

    def test_target_after_an_earlier_drawdown_breach_is_a_fail(self):
        r = sim([(0, 1500), (1, -2100), (2, 5000)], GROWTH)
        s = r["summary"]
        self.assertEqual((s["evaluation_status"], s["evaluation_failure_reason"]), ("FAIL", "MAX_LOSS_LIMIT"))
        self.assertEqual((s["state_at_evaluation_breach"]["balance"], s["state_at_evaluation_breach"]["floor"]),
                         (49400.0, 49500.0))
        self.assertEqual(s["evaluation_failure_timestamp"], mk([(1, 0)])["exit_ts"][0].isoformat())

    def test_drawdown_measured_inside_the_trade_by_default(self):
        self.assertEqual(sim([(0, 100, 20, 2100)], GROWTH)["evaluation"]["failure_reason"], "MAX_LOSS_LIMIT")
        closed = mod(GROWTH, {"evaluation.drawdown.measurement": "closed_balance"})
        self.assertEqual(sim([(0, 100, 20, 2100)], closed)["evaluation"]["failure_reason"], "NOT_PASSED_BY_END_OF_DATA")

    def test_no_evaluation_consistency(self):
        self.assertEqual(sim(GROWTH_PASS, GROWTH)["evaluation"]["pass"]["date"], day(0))

    def test_funded_35pct_consistency_per_payout_request(self):
        r = sim(GROWTH_PASS + [(1, 2500)] + [(d, 300) for d in range(2, 6)], GROWTH)
        self.assertEqual(r["payouts"], [])
        self.assertEqual(r["funded"]["payout_checks"][0]["unmet"], ["CONSISTENCY"])

    def test_53000_balance_requirement_cap_and_split(self):
        r = sim(GROWTH_PASS + [(d, 500) for d in range(1, 6)] + [(6, 600)], GROWTH)
        self.assertEqual(r["funded"]["payout_checks"][0]["unmet"], ["BALANCE_REQUIREMENT"])
        (p,) = r["payouts"]
        self.assertEqual((p["date"], p["gross"], p["trader_share"], p["firm_share"], p["balance_after"]),
                         (day(6), 1500.0, 1350.0, 150.0, 51600.0))

    def test_multiple_payouts_reset_the_winning_day_count(self):
        r = sim(GROWTH_PASS + [(d, 700) for d in range(1, 6)] + [(d, 400) for d in range(6, 11)], GROWTH)
        self.assertEqual([(x["date"], x["gross"]) for x in r["payouts"]], [(day(5), 1500.0), (day(10), 2000.0)])
        self.assertEqual(len(r["payouts"][1]["winning_days"]), 5)


class TestTradeifySelect(unittest.TestCase):
    def test_defaults_and_shared_evaluation(self):
        ev = lambda p: {k: v for k, v in p["rules"].items() if k.startswith("evaluation.")}
        self.assertEqual(ev(FLEX), ev(DAILY))                                     # one Select evaluation, two branches
        exp = {"evaluation.profit_target": 3000, "evaluation.drawdown.max_loss": 2000, "evaluation.dll.enabled": False,
               "evaluation.consistency.percent": 40, "evaluation.minimum_trading_days": 3, "evaluation.max_micros": 40}
        for k, v in exp.items():
            self.assertEqual(V(FLEX, k), v, k)
        for p in (FLEX, DAILY):
            self.assertEqual((V(p, "funded.scaling.start_micros"), V(p, "funded.max_micros"), V(p, "funded.scaling.tiers")),
                             (20, 40, [{"min_profit": 1500, "micros": 30}, {"min_profit": 2000, "micros": 40}]))
            self.assertFalse(V(p, "funded.consistency.enabled"))
            self.assertEqual((V(p, "account.purchase_date"), V(p, "payout.minimum"), V(p, "payout.split_trader")),
                             ("2026-10-01", 250, 0.9))
        self.assertFalse(V(FLEX, "funded.dll.enabled"))
        self.assertFalse(V(FLEX, "payout.buffer_enabled"))
        self.assertEqual((V(DAILY, "funded.dll.enabled"), V(DAILY, "funded.dll.amount")), (True, 1000))
        self.assertEqual((V(DAILY, "payout.buffer_enabled"), V(DAILY, "payout.buffer_offset")), (True, 2100))
        self.assertEqual((L.payout_cap(FLEX, 1), L.payout_cap(DAILY, 1)), (2500.0, 1250.0))
        self.assertEqual((V(FLEX, "payout.formula_factor"), V(DAILY, "payout.formula_factor")), (0.5, 2))

    def test_no_evaluation_dll(self):
        r = sim([(0, -1300), (0, 100)], FLEX)
        self.assertEqual((r["evaluation"]["days"][0]["n_trades"], r["evaluation"]["days"][0]["n_skipped_dll"]), (2, 0))

    def test_40pct_consistency(self):
        r = sim([(0, 1300), (1, 1000), (2, 700), (3, 300)], FLEX)
        self.assertEqual(events(r, "TARGET_REACHED_NOT_PASSED")[0]["unmet"], ["CONSISTENCY"])
        self.assertEqual(r["evaluation"]["pass"]["date"], day(3))

    def test_minimum_three_trading_days(self):
        r = sim([(0, 1500), (1, 1500), (2, 10)], mod(FLEX, {"evaluation.consistency.enabled": False}))
        self.assertEqual(events(r, "TARGET_REACHED_NOT_PASSED")[0]["unmet"], ["MINIMUM_TRADING_DAYS"])
        self.assertEqual((r["evaluation"]["pass"]["date"], r["evaluation"]["pass"]["trading_days"]), (day(2), 3))

    def test_both_branches_share_the_evaluation_outcome(self):
        rows = SEL_PASS + [(3, 200)]
        self.assertEqual(sim(rows, FLEX)["evaluation"]["pass"], sim(rows, DAILY)["evaluation"]["pass"])

    def test_funded_scaling(self):
        r = sim(SEL_PASS + [(3, 1400), (4, 100, 30)], FLEX)
        self.assertEqual((r["funded"]["status"], r["funded"]["breach"]["reason"]), ("INCOMPATIBLE", "SCALING_LIMIT_VIOLATION"))
        r = sim(SEL_PASS + [(3, 1500), (4, 100, 30), (5, 400, 30), (6, 100, 40)], FLEX)
        self.assertEqual([d["permitted_micros"] for d in r["funded"]["days"]], [20.0, 30.0, 30.0, 40.0])
        self.assertEqual(r["funded"]["status"], "PASS")

    def test_flex_no_dll_and_daily_assumed_soft_dll_is_active(self):
        r = sim(SEL_PASS + [(3, -1100), (3, 100)], FLEX)
        self.assertEqual(r["funded"]["days"][0]["n_trades"], 2)
        rows = SEL_PASS + [(3, -1000), (3, 500), (4, 100)]
        r = sim(rows, DAILY)                                                      # ASSUMED_DEFAULT soft: stop for the day
        f = r["funded"]
        self.assertEqual((f["days"][0]["n_trades"], f["days"][0]["n_skipped_dll"], f["status"], f["trading_days"]),
                         (1, 1, "PASS", 2))
        self.assertIn("funded.dll.behavior", r["rule_basis"]["assumed_rules"])
        r = sim(rows, mod(DAILY, {"funded.dll.behavior": "hard"}))                 # CUSTOM hard: the account fails
        self.assertEqual((r["funded"]["status"], r["funded"]["breach"]["reason"]), ("FAIL", "DAILY_LOSS_LIMIT"))
        self.assertIn("funded.dll.behavior", r["rule_basis"]["custom_rules"])

    def test_flex_5_day_payouts_minimum_and_reset(self):
        r = sim(SEL_PASS + [(d, 1200) for d in range(3, 8)], FLEX)
        self.assertEqual((r["payouts"][0]["gross"], r["payouts"][0]["trader_share"]), (2500.0, 2250.0))
        r = sim(SEL_PASS + [(d, 150) for d in range(3, 13)], FLEX)
        self.assertEqual([(x["date"], x["gross"]) for x in r["payouts"]],
                         [(day(7), 375.0), (day(12), 562.5)])                     # 50% x (51,125 - 50,000)
        self.assertEqual(sim(SEL_PASS + [(d, 150) for d in range(3, 8)], mod(FLEX, {"payout.minimum": 500}))["payouts"], [])

    def test_daily_2x_cycle_profit_cap_and_buffer(self):
        r = sim(SEL_PASS + [(3, 5000), (4, 150), (5, 100), (6, -50)], DAILY)
        got = [(x["date"], x["formula_amount"], x["withdrawable"], x["gross"]) for x in r["payouts"]]
        self.assertEqual(got, [(day(3), 10000.0, 2900.0, 1250.0), (day(4), 300.0, 1800.0, 300.0)])
        self.assertEqual([e["unmet"] for e in events(r, "PAYOUT_NOT_ELIGIBLE")],
                         [["BELOW_MINIMUM_PAYOUT"], ["BELOW_MINIMUM_PAYOUT"]])
        r = sim(SEL_PASS + [(3, 2500)], DAILY)
        self.assertEqual((r["payouts"][0]["max_permitted"], r["payouts"][0]["balance_after"]), (400.0, 52100.0))
        r = sim(SEL_PASS + [(3, -100)], DAILY)
        self.assertEqual(events(r, "PAYOUT_NOT_ELIGIBLE")[0]["unmet"], ["POSITIVE_CYCLE_PROFIT"])

    def test_day_boundaries_are_per_stage(self):
        late = [(0, 1000), (1, 1000)]
        t = mk(late + [(2, 1000)])
        t.loc[2, ["entry_ts", "exit_ts"]] = [pd.Timestamp(f"{day(1)} 18:30", tz="America/New_York").tz_convert("UTC"),
                                             pd.Timestamp(f"{day(1)} 19:00", tz="America/New_York").tz_convert("UTC")]
        a = L.simulate_lifecycle(t, FLEX)                                         # 18:30 belongs to the next trading day
        self.assertEqual(a["evaluation"]["pass"]["date"], day(2))
        b = L.simulate_lifecycle(t, mod(FLEX, {"evaluation.day_boundary.reset_time": "23:00"}))
        self.assertEqual(b["evaluation"]["status"], "FAIL")                       # only 2 trading days


class TestDeterminismAndCausality(unittest.TestCase):
    ROWS = LUCID_PASS + [(d, 500 if d % 3 else -300) for d in range(2, 30)]

    def test_deterministic_rerun_and_input_untouched(self):
        t = mk(self.ROWS)
        keep = t.copy()
        for p in DEF.values():
            self.assertEqual(json.dumps(L.simulate_lifecycle(t, p), default=str),
                             json.dumps(L.simulate_lifecycle(t.copy(), p), default=str))
        pd.testing.assert_frame_equal(t, keep)

    def test_no_future_leakage_prefix_property(self):
        full = mk(self.ROWS)
        for p in DEF.values():
            a = L.simulate_lifecycle(full, p)
            for k in (3, 9, 17):
                cut = full[full["exit_ts"] < pd.Timestamp(f"{day(k)} 00:00", tz="America/New_York")]
                b = L.simulate_lifecycle(cut, p)
                for stage in ("evaluation", "funded"):
                    da = [d for d in (a[stage].get("days") or []) if d["date"] < day(k)]
                    self.assertEqual(da, [d for d in (b[stage].get("days") or []) if d["date"] < day(k)])
                self.assertEqual([e for e in a["events"] if e["date"] < day(k)],
                                 [e for e in b["events"] if e["date"] < day(k)])

    def test_result_states_and_required_fields_no_score(self):
        c = L.compact(L.simulate_lifecycle(mk(self.ROWS), LUCID))
        self.assertIn(c["status"], L.STATES)
        for k in ("evaluation_status", "evaluation_pass_timestamp", "evaluation_failure_timestamp", "evaluation_failure_reason",
                  "funded_status", "funded_start_timestamp", "funded_failure_timestamp", "funded_failure_reason",
                  "payout_count", "payout_dates", "payout_gross_amounts", "payout_trader_amounts", "total_trader_payout",
                  "final_status", "rule_profile_id", "rule_profile_version", "account_size", "contract", "purchase_date",
                  "verified_rule_count", "assumed_rule_count", "custom_rule_count"):
            self.assertIn(k, c["summary"])
        self.assertTrue(c["profile"]["profile_hash"])
        blob = json.dumps(c, default=str)
        for bad in ('"score"', '"rank"', '"grade"', "IN_PROGRESS"):
            self.assertNotIn(bad, blob)


if __name__ == "__main__":
    unittest.main()
