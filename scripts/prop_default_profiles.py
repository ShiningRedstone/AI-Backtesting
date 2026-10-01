"""Writes the schema-3 DEFAULT prop rule profiles (ADR-66) and registers them (append-only; an existing id+version is never
rewritten). Every rule has a value, a status and a basis:

  VERIFIED         supplied explicitly by the user as a canonical default (task of 2026-09-30)
  ASSUMED_DEFAULT  EdgeLab fallback assumption (still an ACTIVE rule the simulator checks)
  CUSTOM           a user change for a future run (see edgelab.prop.profiles.customize)

Run once per new version:

    python scripts/prop_default_profiles.py      (Windows: .\\.venv\\Scripts\\python.exe scripts\\prop_default_profiles.py)

To change a rule: edit it below (mark it CUSTOM), bump VERSION, run again. The simulator code never changes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from edgelab.prop import profiles as P  # noqa: E402

VERSION = 4          # v4 (ADR-67): metadata-only correction of v3 (VERIFIED vs ASSUMED_DEFAULT); same values
PROVIDED_ON = "2026-09-30"
CANON = "Canonical task default supplied by the user (2026-09-30)"
SOURCE = {"title": "Canonical default prop rule profiles supplied by the user in the EdgeLab tasks of 2026-09-30",
          "provided_on": PROVIDED_ON,
          "note": "VERIFIED = explicitly supplied by the user; ASSUMED_DEFAULT = EdgeLab assumption, simulated but not a "
                  "confirmed provider rule. Neither was checked against provider documentation by EdgeLab."}


def V(value, basis=CANON):
    return {"value": value, "status": P.RULE_VERIFIED, "basis": basis}


def A(value, basis):
    return {"value": value, "status": P.RULE_ASSUMED, "basis": basis}


BOUNDARY = "EdgeLab default day boundary 18:00 America/New_York (assumed; configurable per stage)"
DD_MEAS = ("EdgeLab execution-model assumption: worst executable price reached during the trade (bar/quote model, MAE) "
           "compared with the floor; not a provider-certified intrabar rule")
DLL_MEAS = "EdgeLab default: DLL measured on closed trades only (assumed; unrealized/intraday measurement configurable)"
LOCK = "EdgeLab default lock: trigger starting balance + 2,100, locked floor starting balance + 100 (assumed; configurable)"
BREACH = "EdgeLab default: touching the floor counts as a breach (assumed)"
FUNDED_STARTS = ("EdgeLab default: the funded account is a separate state that starts after the evaluation pass day ends "
                 "(next trading day) (assumed)")
SELECT_DD_MODE = ("'EOD' supplied; trailing from the highest end-of-day balance is the EdgeLab interpretation (assumed)")
INACTIVE = "inactive while the parent rule is disabled; value kept so the profile stays runnable"


def stage(s, *, start, dd_verified, lock_basis=LOCK, dll=None, cons=None, scaling=None, max_micros=40, extra=None,
          scaling_off_supplied=False, max_micros_rule=None, dd_mode_rule=None):
    """dll: (enabled, amount, behavior_rule); cons: (enabled, percent, window_rule, cushion_rules | None);
    scaling: None (disabled) or dict of VERIFIED/ASSUMED rules."""
    r = {f"{s}.day_boundary.timezone": A("America/New_York", BOUNDARY),
         f"{s}.day_boundary.reset_time": A("18:00", BOUNDARY),
         f"{s}.starting_balance": V(start), f"{s}.max_micros": max_micros_rule or V(max_micros),
         f"{s}.drawdown.max_loss": V(2000), f"{s}.drawdown.mode": dd_mode_rule or V("eod_trailing"),
         f"{s}.drawdown.update_frequency": V("end_of_day"),
         f"{s}.drawdown.lock_enabled": dd_verified("lock_enabled", True, lock_basis),
         f"{s}.drawdown.lock_trigger_offset": dd_verified("lock_trigger_offset", 2100, lock_basis),
         f"{s}.drawdown.locked_floor_offset": dd_verified("locked_floor_offset", 100, lock_basis),
         f"{s}.drawdown.breach_comparison": A("at_or_below", BREACH),
         f"{s}.drawdown.measurement": A("intratrade_worst_price", DD_MEAS)}
    en, amt, beh = dll
    r[f"{s}.dll.enabled"] = V(en)
    r[f"{s}.dll.amount"] = V(amt) if en or amt == 0 else A(amt, INACTIVE)
    r[f"{s}.dll.behavior"] = beh
    r[f"{s}.dll.measurement"] = A("closed_trades_only", DLL_MEAS)
    cen, pct, window, cushion = cons
    r[f"{s}.consistency.enabled"] = V(cen)
    r[f"{s}.consistency.percent"] = V(pct) if cen else A(pct, INACTIVE)
    r[f"{s}.consistency.window"] = window
    c_en, c_amt, c_pp = cushion or (A(False, "no consistency cushion configured"), A(0, INACTIVE), A(0, INACTIVE))
    r.update({f"{s}.consistency.cushion_enabled": c_en, f"{s}.consistency.cushion_amount_usd": c_amt,
              f"{s}.consistency.cushion_percent_points": c_pp})
    if scaling is None:
        r.update({f"{s}.scaling.enabled": V(False) if scaling_off_supplied else A(False, f"no {s} scaling supplied: disabled (assumed)"),
                  f"{s}.scaling.basis": A("balance_above_stage_start", INACTIVE),
                  f"{s}.scaling.update": A("end_of_session", INACTIVE), f"{s}.scaling.effective": A("next_session", INACTIVE),
                  f"{s}.scaling.persist": A(False, INACTIVE), f"{s}.scaling.start_micros": A(max_micros, INACTIVE),
                  f"{s}.scaling.tiers": A([], INACTIVE)})
    else:
        r.update({f"{s}.scaling.{k}": v for k, v in scaling.items()})
    r.update(extra or {})
    return r


def scaling(t30, t40):
    return {"enabled": V(True), "start_micros": V(20),
            "tiers": V([{"min_profit": t30, "micros": 30}, {"min_profit": t40, "micros": 40}]),
            "update": V("end_of_session"), "effective": V("next_session"),
            "basis": A("balance_above_stage_start", "EdgeLab default: tier from EOD balance above the funded start, so a "
                                                    "payout can move the tier down (assumed)"),
            "persist": A(False, "EdgeLab default: tiers are recalculated each session end and can move down (assumed)")}


def all_assumed_lock(key, value, basis):
    return A(value, basis)


NO_CONS_EVAL = (False, 0, A("account", INACTIVE), None)


def payout(**kw):
    base = {"payout.enabled": V(True), "payout.cycle_reset": V("after_payout"),
            "payout.after_payout_drawdown": A("unchanged", "EdgeLab default: a payout does not move the drawdown floor (assumed)"),
            "payout.request": A("max_allowed", "EdgeLab default: each payout requests the maximum permitted amount (assumed)")}
    base.update(kw)
    return base


CONVENTIONS = {"on_oversize": "fail_incompatible", "pass_checked": "end_of_trading_day", "payout_checked": "end_of_trading_day"}


def doc(pid, provider, product, path, desc, rules):
    return {"kind": P.KIND, "schema_version": 3, "profile_id": pid, "version": VERSION, "provider": provider,
            "product": product, "path": path, "description": desc, "rules_source": dict(SOURCE),
            "conventions": dict(CONVENTIONS), "rules": dict(sorted(rules.items()))}


RESEARCH_DATE = A("2026-10-01", "research scenario purchase date (assumed); no purchase-date dependent rule in this profile")

# ------------------------------------------------------------------------------------------- LucidFlex 50K
lucid_rules = {
    "account.size": V(50000), "account.quantity_unit": V("MNQ"), "account.purchase_date": RESEARCH_DATE,
    **stage("evaluation", start=50000, dd_verified=all_assumed_lock,
            dll=(False, 0, A("soft", INACTIVE)),
            cons=(True, 50, V("account", "Canonical task default: largest single-day profit / account profit"),
                  (A(True, "LucidFlex consistency cushion: enabled as a configurable field (assumed)"),
                   A(0, "cushion amount 0 by default: the ordinary 50% rule applies exactly (assumed)"),
                   A(0, "cushion percent points 0 by default (assumed)"))),
            extra={"evaluation.profit_target": V(3000), "evaluation.minimum_trading_days": V(0)}, scaling_off_supplied=True),
    **stage("funded", start=50000, dd_verified=all_assumed_lock, dll=(False, 0, A("soft", INACTIVE)),
            cons=(False, 0, A("account", INACTIVE), None), scaling=scaling(1000, 2000),
            extra={"funded.carry_evaluation_profit": A(False, "EdgeLab default: the funded account starts at its own "
                                                              "starting balance (assumed)"),
                   "funded.starts": A("next_trading_day", FUNDED_STARTS)}),
    **payout(**{"payout.frequency_mode": V("winning_days"), "payout.winning_days_required": V(5),
                "payout.winning_day_threshold": V(150), "payout.positive_cycle_profit_required": V(True),
                "payout.balance_requirement_enabled": A(False, "no LucidFlex payout balance requirement supplied (assumed)"),
                "payout.balance_requirement": A(0, INACTIVE),
                "payout.buffer_enabled": V(True), "payout.buffer_offset": V(0),
                "payout.formula_mode": V("share_of_profit"), "payout.formula_factor": V(0.5),
                "payout.formula_profit_basis": A("balance_above_stage_start", "EdgeLab default: 'eligible profit' = balance "
                                                                               "above the funded start (assumed)"),
                "payout.minimum": V(500), "payout.cap_mode": V("fixed"), "payout.cap_schedule": V([2000]),
                "payout.cap_table": A([], INACTIVE), "payout.split_trader": V(0.9),
                "payout.count_limit_enabled": V(True), "payout.count_limit": V(5)}),
    "live_transition.rule": V("after_payout_count", "Canonical task default: eligible after the final payout, not guaranteed"),
    "live_transition.payout_count": V(5),
}
lucid = doc("LUCID_LUCIDFLEX_50K", "Lucid Trading", "LucidFlex",
            "LucidFlex 50K evaluation -> LucidFlex 50K funded -> LucidFlex payouts -> live-transition eligibility",
            "LucidFlex only (NOT LucidPro, NOT LucidDaily).", lucid_rules)

# ------------------------------------------------------------------------------------------- Tradeify Growth 50K
growth_dll = (True, 1250, V("soft"))
growth_rules = {
    "account.size": V(50000), "account.quantity_unit": V("MNQ"), "account.purchase_date": RESEARCH_DATE,
    **stage("evaluation", start=50000, dd_verified=all_assumed_lock, dll=growth_dll, cons=NO_CONS_EVAL,
            extra={"evaluation.profit_target": V(3000), "evaluation.minimum_trading_days": V(1)}),
    **stage("funded", start=50000, dd_verified=all_assumed_lock, dll=growth_dll,
            max_micros_rule=A(40, "no Growth funded contract limit supplied: the 40-micro research maximum (assumed)"),
            cons=(True, 35, A("payout_cycle", "EdgeLab default: 35% = best winning day / profit since the previous payout "
                                              "(assumed)"), None),
            scaling=None,
            extra={"funded.carry_evaluation_profit": A(False, "EdgeLab default (assumed)"), "funded.starts": A("next_trading_day", FUNDED_STARTS)}),
    **payout(**{"payout.frequency_mode": V("winning_days"), "payout.winning_days_required": V(5),
                "payout.winning_day_threshold": V(150),
                "payout.positive_cycle_profit_required": A(False, "Growth: not required unless otherwise established (assumed)"),
                "payout.balance_requirement_enabled": V(True), "payout.balance_requirement": V(53000),
                "payout.buffer_enabled": A(True, "Growth payout formula: only profit above the starting balance (assumed)"),
                "payout.buffer_offset": A(0, "Growth payout formula: baseline = starting balance (assumed)"),
                "payout.formula_mode": A("share_of_profit", "Growth payout formula: eligible profit above the baseline, "
                                                            "subject to the cap (assumed)"),
                "payout.formula_factor": A(1.0, "Growth payout formula: 100% of eligible profit before the cap (assumed)"),
                "payout.formula_profit_basis": A("balance_above_stage_start", "Growth payout formula (assumed)"),
                "payout.minimum": V(500), "payout.cap_mode": V("by_payout_number"),
                "payout.cap_schedule": V([1500, 2000, 2500, 3000]), "payout.cap_table": A([], INACTIVE),
                "payout.split_trader": V(0.9),
                "payout.count_limit_enabled": A(False, "Growth payout count: unlimited (assumed)"),
                "payout.count_limit": A(0, INACTIVE)}),
    "live_transition.rule": A("none", "no live-transition rule configured for Growth (assumed)"),
    "live_transition.payout_count": A(0, INACTIVE),
}
growth = doc("TRADEIFY_GROWTH_50K", "Tradeify", "Growth", "Tradeify Growth 50K evaluation -> Growth 50K funded -> Growth payouts",
             "Tradeify Growth 50K.", growth_rules)


# ------------------------------------------------------------------------------------------- Tradeify Select 50K (shared eval)
def select_eval():
    return stage("evaluation", start=50000, dd_verified=all_assumed_lock, dll=(False, 0, A("soft", INACTIVE)),
                 dd_mode_rule=A("eod_trailing", SELECT_DD_MODE),
                 cons=(True, 40, V("account", "Canonical task default: largest single day / total account profit"), None),
                 extra={"evaluation.profit_target": V(3000), "evaluation.minimum_trading_days": V(3)})


def select_common(pid, product, path, funded_dll, pay):
    rules = {"account.size": V(50000), "account.quantity_unit": V("MNQ"), "account.purchase_date": V("2026-10-01"),
             **select_eval(),
             **stage("funded", start=50000, dd_verified=all_assumed_lock, dll=funded_dll,
                     dd_mode_rule=A("eod_trailing", SELECT_DD_MODE),
                     cons=(False, 0, A("account", INACTIVE), None), scaling=scaling(1500, 2000),
                     extra={"funded.carry_evaluation_profit": A(False, "EdgeLab default (assumed)"),
                            "funded.starts": A("next_trading_day", FUNDED_STARTS)}),
             **payout(**pay),
             "live_transition.rule": A("none", "no live-transition rule configured for Select (assumed)"),
             "live_transition.payout_count": A(0, INACTIVE)}
    return doc(pid, "Tradeify", product, path, f"Select evaluation, {product} funded branch.", rules)


flex = select_common(
    "TRADEIFY_SELECT_FLEX_50K", "Select Flex", "Tradeify Select 50K evaluation -> Select Flex 50K funded -> Select Flex payouts",
    (False, 0, A("soft", INACTIVE)),
    {"payout.frequency_mode": V("winning_days"), "payout.winning_days_required": V(5), "payout.winning_day_threshold": V(150),
     "payout.positive_cycle_profit_required": V(True),
     "payout.balance_requirement_enabled": V(False, "Canonical task default: Select Flex minimum balance = none"),
     "payout.balance_requirement": A(0, INACTIVE),
     "payout.buffer_enabled": A(False, "Select Flex: no payout buffer (assumed default)"), "payout.buffer_offset": A(0, INACTIVE),
     "payout.formula_mode": V("share_of_profit"), "payout.formula_factor": V(0.5),
     "payout.formula_profit_basis": A("balance_above_stage_start", "EdgeLab default: 'total eligible profit' = balance above "
                                                                   "the funded start (assumed)"),
     "payout.minimum": V(250), "payout.cap_mode": V("by_purchase_date"), "payout.cap_schedule": A([], INACTIVE),
     "payout.cap_table": V([{"purchased_on_or_after": "2026-09-01", "amount": 2500}],
                           "Canonical task default: $2,500 cap for the current research scenario (purchase 2026-10-01)"),
     "payout.split_trader": V(0.9), "payout.count_limit_enabled": A(False, "no payout count limit supplied (assumed)"),
     "payout.count_limit": A(0, INACTIVE),
     "payout.cycle_reset": A("after_payout", "Select Flex: payout cycle resets after payout (assumed default)")})

daily = select_common(
    "TRADEIFY_SELECT_DAILY_50K", "Select Daily",
    "Tradeify Select 50K evaluation -> Select Daily 50K funded -> Select Daily payouts",
    (True, 1000, A("soft", "Select Daily DLL behaviour: soft (stop for the day) is an EdgeLab default assumption; "
                           "set hard (CUSTOM) to fail the account instead")),
    {"payout.frequency_mode": A("daily", "Select Daily: a payout can be requested at any day's end (assumed)"),
     "payout.winning_days_required": A(1, INACTIVE),
     "payout.winning_day_threshold": A(150, "inactive for daily payouts (no winning-day condition); value kept for reporting"),
     "payout.positive_cycle_profit_required": V(True),
     "payout.balance_requirement_enabled": A(False, "no Select Daily balance requirement supplied beyond the buffer (assumed)"),
     "payout.balance_requirement": A(0, INACTIVE),
     "payout.buffer_enabled": V(True), "payout.buffer_offset": V(2100),
     "payout.formula_mode": V("cycle_profit_multiple"), "payout.formula_factor": V(2),
     "payout.formula_profit_basis": V("cycle_net_profit"), "payout.minimum": V(250),
     "payout.cap_mode": V("by_purchase_date"), "payout.cap_schedule": A([], INACTIVE),
     "payout.cap_table": V([{"purchased_on_or_after": "2026-09-01", "amount": 1250}],
                           "Canonical task default: $1,250 cap for the current research scenario (purchase 2026-10-01)"),
     "payout.split_trader": V(0.9), "payout.count_limit_enabled": A(False, "no payout count limit supplied (assumed)"),
     "payout.count_limit": A(0, INACTIVE)})


def main() -> None:
    d = P.profiles_dir(ROOT)
    for p in (lucid, growth, flex, daily):
        P.validate_profile(p)
        f = d / f"{p['profile_id']}.v{p['version']}.yaml"
        if f.exists():
            print(f"{f.name}: exists (never rewritten)")
            continue
        head = ("# EdgeLab prop rule profile, schema 3 (ADR-66, ADR-67). Every rule: value + status (VERIFIED / ASSUMED_DEFAULT / CUSTOM)\n"
                "# + basis. Never edit a registered file: bump the version (scripts/prop_default_profiles.py).\n")
        f.write_text(head + yaml.safe_dump(p, sort_keys=False, width=120))
        print(P.register_profile(d, f))
    print("registry problems:", P.check_registry(d))


if __name__ == "__main__":
    main()
