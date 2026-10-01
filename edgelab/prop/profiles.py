"""Versioned prop-firm RULE PROFILES (ADR-64): provider / product / account size / stage rules, with evidence.

A profile is plain data (YAML) describing one program's path (evaluation -> funded -> payouts). It carries:

* ``evaluation`` / ``funded`` / ``payout`` / ``live_transition`` / ``trading_day``: the PROVIDER rules;
* ``conventions``: how EdgeLab simulates what the provider rules leave open (never presented as provider rules);
* ``verification``: sources (official title, URL, access date) and, for EVERY provider-rule value, an evidence entry
  {source, quote, value}. The effective status is DERIVED from that evidence, never taken from a flag:

    verified      every rule value has evidence from a listed source and the evidence value equals the profile value
    unverified    anything else (fail closed: the lifecycle simulator produces no outcome, see lifecycle.py)
    test_fixture  a ``TEST_`` profile used only by tests (needs an explicit ``allow_test_fixture``)

Versioning: a profile file is ``<PROFILE_ID>.v<N>.yaml`` and is registered in ``REGISTRY.json`` with its content hash.
Files are never edited in place: a changed rule or new evidence is a NEW version (new file, new hash); the registry check
fails if a registered file changed or a file is unregistered. Results record profile id, version and hash.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

import yaml

from edgelab.core.identity import hash_obj

SCHEMA_VERSION = 1
KIND = "edgelab.prop_profile"
STATUS_VERIFIED, STATUS_UNVERIFIED, STATUS_TEST = "verified", "unverified", "test_fixture"
# schema 2: rules supplied by the user as the canonical defaults (simulated; labelled "user_specified", never "verified")
# or verified against official documents with per-rule evidence (declared "official_verified", derived "verified")
STATUS_USER, STATUS_OFFICIAL = "user_specified", "official_verified"
SIMULATABLE = (STATUS_VERIFIED, STATUS_USER)
STATUS_RULEBOOK = "rulebook"          # schema 3: per-rule VERIFIED / ASSUMED_DEFAULT / CUSTOM; always runnable
RULE_SECTIONS = ("trading_day", "evaluation", "funded", "payout", "live_transition")
MLL_TYPES = ("static", "eod_trailing")
DETECTIONS = ("closed_balance", "intratrade_bound")
AFTER_PAYOUT_FLOORS = ("unchanged", "reset_to_balance_minus_mll", "floor_at_starting_balance")
PAYOUT_POLICIES = ("winning_days_cycle",)
PROFIT_BASES = ("cycle_net_profit", "balance_above_funded_start")
SCALING_BASES = ("balance_above_funded_start", "cumulative_profit_incl_payouts")
REQUEST_CONVENTIONS = ("max_allowed", "min_allowed")


class ProfileError(ValueError):
    pass


# ------------------------------------------------------------------------------------------ helpers
def leaf_paths(obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """(dotted path, value) for every leaf; a list is one leaf."""
    if isinstance(obj, Mapping):
        out: list = []
        for k in sorted(obj):
            out += leaf_paths(obj[k], f"{prefix}.{k}" if prefix else str(k))
        return out
    return [(prefix, obj)]


def _get(doc: Mapping, path: str) -> Any:
    cur: Any = doc
    for part in path.split("."):
        cur = cur[part]
    return cur


def profile_hash(doc: Mapping) -> str:
    """Content hash of the whole profile (rules, conventions and verification evidence)."""
    return hash_obj(dict(doc))


# ------------------------------------------------------------------------------------------ validation
def _req(ok: bool, msg: str, errs: list) -> None:
    if not ok:
        errs.append(msg)


def _num(v: Any, path: str, errs: list, *, null_ok: bool = True, positive: bool = False) -> None:
    if v is None:
        _req(null_ok, f"{path}: required", errs)
        return
    _req(isinstance(v, (int, float)) and not isinstance(v, bool), f"{path}: must be a number or null", errs)
    if positive and isinstance(v, (int, float)) and not isinstance(v, bool):
        _req(v > 0, f"{path}: must be > 0", errs)


def _mll(m: Any, path: str, errs: list) -> None:
    if not isinstance(m, Mapping):
        errs.append(f"{path}: must be a mapping")
        return
    extra = set(m) - {"amount", "type", "lock_at_offset", "breach_comparison"}
    _req(not extra, f"{path}: unknown keys {sorted(extra)}", errs)
    _num(m.get("amount"), f"{path}.amount", errs, positive=True)
    _req(m.get("type") in MLL_TYPES + (None,), f"{path}.type must be one of {MLL_TYPES} or null", errs)
    _num(m.get("lock_at_offset"), f"{path}.lock_at_offset", errs)
    _req(m.get("breach_comparison") in ("at_or_below", "below", None), f"{path}.breach_comparison", errs)


def _stage_common(s: Any, path: str, errs: list, allowed: set) -> None:
    if not isinstance(s, Mapping):
        errs.append(f"{path}: must be a mapping")
        return
    extra = set(s) - allowed
    _req(not extra, f"{path}: unknown keys {sorted(extra)}", errs)
    _num(s.get("starting_balance"), f"{path}.starting_balance", errs, positive=True)
    _mll(s.get("mll"), f"{path}.mll", errs)
    _req(s.get("breach_detection") in DETECTIONS + (None,), f"{path}.breach_detection must be one of {DETECTIONS} or null", errs)
    dl = s.get("daily_loss")
    if not isinstance(dl, Mapping) or set(dl) - {"amount", "action"}:
        errs.append(f"{path}.daily_loss must be {{amount, action}}")
    else:
        _num(dl.get("amount"), f"{path}.daily_loss.amount", errs, positive=True)
        _req(dl.get("action") in ("breach", "pause_day", None), f"{path}.daily_loss.action", errs)
    _num(s.get("max_contracts"), f"{path}.max_contracts", errs, positive=True)


def validate_profile(raw: Any) -> dict:
    """Strict structural validation (schema 1 = superseded ADR-64 drafts, kept loadable for history; schema 2 = the
    configurable rulebook). Returns the (unchanged) document or raises ProfileError listing every problem."""
    if not isinstance(raw, Mapping):
        raise ProfileError("a profile must be a mapping")
    if raw.get("schema_version") == 3:
        return _validate_v3(raw)
    if raw.get("schema_version") == 2:
        return _validate_v2(raw)
    return _validate_v1(raw)


def _validate_v1(raw: Mapping) -> dict:
    errs: list[str] = []
    top = {"kind", "schema_version", "profile_id", "version", "provider", "product", "account_size", "currency", "stages",
           "effective_date", "description", "trading_day", "evaluation", "funded", "payout", "live_transition",
           "conventions", "verification"}
    extra = set(raw) - top
    _req(not extra, f"unknown top-level keys {sorted(extra)}", errs)
    _req(raw.get("kind") == KIND, f"kind must be {KIND}", errs)
    _req(raw.get("schema_version") == SCHEMA_VERSION, f"schema_version must be {SCHEMA_VERSION}", errs)
    pid = raw.get("profile_id")
    _req(isinstance(pid, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", pid or ""), "profile_id must match [A-Z][A-Z0-9_]*", errs)
    _req(isinstance(raw.get("version"), int) and raw.get("version", 0) >= 1, "version must be an integer >= 1", errs)
    for k in ("provider", "product"):
        _req(isinstance(raw.get(k), str) and raw[k].strip() != "", f"{k} is required", errs)
    _num(raw.get("account_size"), "account_size", errs, null_ok=False, positive=True)
    td = raw.get("trading_day")
    if not isinstance(td, Mapping) or set(td) != {"timezone", "reset_time"}:
        errs.append("trading_day must be {timezone, reset_time}")
    ev, fu, po, lt = (raw.get(k) for k in ("evaluation", "funded", "payout", "live_transition"))
    _stage_common(ev, "evaluation", errs, {"starting_balance", "target_profit", "mll", "breach_detection", "daily_loss",
                                           "consistency", "min_trading_days", "max_contracts"})
    if isinstance(ev, Mapping):
        _num(ev.get("target_profit"), "evaluation.target_profit", errs, positive=True)
        c = ev.get("consistency")
        _req(c is None or (isinstance(c, Mapping) and set(c) == {"max_best_day_share"}), "evaluation.consistency", errs)
        if isinstance(c, Mapping):
            _num(c.get("max_best_day_share"), "evaluation.consistency.max_best_day_share", errs, null_ok=False, positive=True)
        _num(ev.get("min_trading_days"), "evaluation.min_trading_days", errs)
    _stage_common(fu, "funded", errs, {"starting_balance", "carry_evaluation_profit", "mll", "breach_detection",
                                       "daily_loss", "consistency", "max_contracts", "scaling"})
    if isinstance(fu, Mapping):
        _req(fu.get("carry_evaluation_profit") in (True, False, None), "funded.carry_evaluation_profit", errs)
        _req(fu.get("consistency") is None, "funded.consistency: a funded consistency rule is not modelled (must be null)", errs)
        sc = fu.get("scaling")
        if sc is not None:
            ok = isinstance(sc, Mapping) and set(sc) == {"update", "basis", "tiers"} and sc.get("update") == "end_of_session" \
                and sc.get("basis") in SCALING_BASES and isinstance(sc.get("tiers"), list) and sc["tiers"]
            _req(bool(ok), "funded.scaling must be {update: end_of_session, basis, tiers: [{min_profit, max_contracts}]}", errs)
            if ok:
                prev = None
                for i, t in enumerate(sc["tiers"]):
                    _req(isinstance(t, Mapping) and set(t) == {"min_profit", "max_contracts"}, f"funded.scaling.tiers[{i}]", errs)
                    if isinstance(t, Mapping) and "min_profit" in t:
                        _req(prev is None or t["min_profit"] > prev, "funded.scaling.tiers must have increasing min_profit", errs)
                        prev = t["min_profit"]
    if not isinstance(po, Mapping):
        errs.append("payout: must be a mapping")
    else:
        extra = set(po) - {"policy", "profit_split_trader", "min_winning_days", "min_daily_profit",
                           "require_positive_cycle_profit", "min_payout", "max_payout", "max_payouts", "after_payout"}
        _req(not extra, f"payout: unknown keys {sorted(extra)}", errs)
        _req(po.get("policy") in PAYOUT_POLICIES + (None,), f"payout.policy must be one of {PAYOUT_POLICIES} or null (other "
             "payout formulas are not implemented: they need a verified formula)", errs)
        for k in ("profit_split_trader", "min_winning_days", "min_daily_profit", "min_payout", "max_payouts"):
            _num(po.get(k), f"payout.{k}", errs)
        _req(po.get("require_positive_cycle_profit") in (True, False, None), "payout.require_positive_cycle_profit", errs)
        mp = po.get("max_payout")
        _req(isinstance(mp, Mapping) and set(mp) == {"share_of_profit", "cap", "profit_basis"}, "payout.max_payout", errs)
        if isinstance(mp, Mapping):
            _req(mp.get("profit_basis") in PROFIT_BASES + (None,), f"payout.max_payout.profit_basis in {PROFIT_BASES}", errs)
        ap = po.get("after_payout")
        _req(isinstance(ap, Mapping) and set(ap) == {"floor"} and ap.get("floor") in AFTER_PAYOUT_FLOORS + (None,),
             f"payout.after_payout must be {{floor: one of {AFTER_PAYOUT_FLOORS} or null}}", errs)
    _req(isinstance(lt, Mapping) and set(lt) == {"after_payouts"}, "live_transition must be {after_payouts}", errs)
    cv = raw.get("conventions")
    if not isinstance(cv, Mapping) or set(cv) != {"on_oversize", "payout_request", "funded_starts", "pass_checked"}:
        errs.append("conventions must be {on_oversize, payout_request, funded_starts, pass_checked}")
    else:
        _req(cv["on_oversize"] == "terminate_violation", "conventions.on_oversize must be terminate_violation (sizes are never clipped)", errs)
        _req(cv["payout_request"] in REQUEST_CONVENTIONS, f"conventions.payout_request in {REQUEST_CONVENTIONS}", errs)
        _req(cv["funded_starts"] == "next_trading_day", "conventions.funded_starts must be next_trading_day", errs)
        _req(cv["pass_checked"] == "end_of_trading_day", "conventions.pass_checked must be end_of_trading_day", errs)
    v = raw.get("verification")
    if not isinstance(v, Mapping):
        errs.append("verification: required")
    else:
        extra = set(v) - {"status", "verified_on", "sources", "evidence", "missing_note", "unofficial_corroboration"}
        _req(not extra, f"verification: unknown keys {sorted(extra)}", errs)
        _req(v.get("status") in (STATUS_VERIFIED, STATUS_UNVERIFIED, STATUS_TEST), "verification.status", errs)
        _req(isinstance(v.get("sources"), list), "verification.sources must be a list", errs)
        _req(isinstance(v.get("evidence"), Mapping), "verification.evidence must be a mapping", errs)
    if errs:
        raise ProfileError("invalid prop profile:\n  " + "\n  ".join(errs))
    return dict(raw)


# ------------------------------------------------------------------------------------------ schema 2 (configurable rulebook)
DD_MODES = ("eod_trailing", "static")
ENFORCEMENTS = ("closed_balance", "intratrade_bound")
DLL_BEHAVIORS = ("soft_breach", "hard_breach")
STAGE_SCALING_BASES = ("balance_above_stage_start", "cumulative_profit_incl_payouts")
FREQ_MODES = ("winning_days", "daily")
FORMULA_MODES = ("share_of_profit", "cycle_profit_multiple")
FORMULA_BASES = ("balance_above_stage_start", "cycle_net_profit")
CAP_MODES = ("fixed", "by_payout_number", "by_purchase_date", "none")
AFTER_PAYOUT_DD = ("unchanged", "reset_to_balance_minus_max_loss", "floor_at_starting_balance")
LIVE_RULES = ("none", "after_payout_count")
CONS_WINDOWS = ("account", "payout_cycle")


def _exact(obj: Any, path: str, keys: set, errs: list) -> bool:
    if not isinstance(obj, Mapping):
        errs.append(f"{path}: must be a mapping with keys {sorted(keys)}")
        return False
    if set(obj) != keys:
        errs.append(f"{path}: keys must be exactly {sorted(keys)} (missing {sorted(keys - set(obj))}, "
                    f"unknown {sorted(set(obj) - keys)})")
        return False
    return True


def _bool(v: Any, path: str, errs: list) -> None:
    _req(isinstance(v, bool), f"{path}: must be true/false", errs)


def _v2_drawdown(d: Any, path: str, errs: list) -> None:
    if not _exact(d, path, {"max_loss", "mode", "update_frequency", "lock_trigger_offset", "locked_floor_offset",
                            "breach_comparison", "enforcement"}, errs):
        return
    _num(d["max_loss"], f"{path}.max_loss", errs, null_ok=False, positive=True)
    _req(d["mode"] in DD_MODES, f"{path}.mode in {DD_MODES}", errs)
    _req(d["update_frequency"] == "end_of_day", f"{path}.update_frequency must be end_of_day", errs)
    _num(d["lock_trigger_offset"], f"{path}.lock_trigger_offset", errs)
    _num(d["locked_floor_offset"], f"{path}.locked_floor_offset", errs)
    _req((d["lock_trigger_offset"] is None) == (d["locked_floor_offset"] is None),
         f"{path}: lock_trigger_offset and locked_floor_offset are both set or both null", errs)
    _req(d["breach_comparison"] in ("at_or_below", "below"), f"{path}.breach_comparison", errs)
    _req(d["enforcement"] in ENFORCEMENTS, f"{path}.enforcement in {ENFORCEMENTS}", errs)


def _v2_dll(d: Any, path: str, errs: list) -> None:
    if not _exact(d, path, {"enabled", "amount", "behavior", "detection"}, errs):
        return
    _bool(d["enabled"], f"{path}.enabled", errs)
    if d["enabled"]:
        _num(d["amount"], f"{path}.amount", errs, null_ok=False, positive=True)
        _req(d["behavior"] in DLL_BEHAVIORS, f"{path}.behavior in {DLL_BEHAVIORS}", errs)
        _req(d["detection"] == "closed_trade", f"{path}.detection must be closed_trade", errs)


def _v2_consistency(c: Any, path: str, errs: list, applies_to: str) -> None:
    if not _exact(c, path, {"enabled", "percent", "applies_to", "window", "cushion"}, errs):
        return
    _bool(c["enabled"], f"{path}.enabled", errs)
    if c["enabled"]:
        _num(c["percent"], f"{path}.percent", errs, null_ok=False, positive=True)
        _req(isinstance(c["percent"], (int, float)) and c["percent"] <= 100, f"{path}.percent must be <= 100", errs)
        _req(c["applies_to"] == applies_to, f"{path}.applies_to must be {applies_to}", errs)
        _req(c["window"] in CONS_WINDOWS, f"{path}.window in {CONS_WINDOWS}", errs)
        if _exact(c["cushion"], f"{path}.cushion", {"amount_usd", "percent_points", "note"}, errs):
            _num(c["cushion"]["amount_usd"], f"{path}.cushion.amount_usd", errs, null_ok=False)
            _num(c["cushion"]["percent_points"], f"{path}.cushion.percent_points", errs, null_ok=False)


def _v2_scaling(sc: Any, path: str, errs: list) -> None:
    if not _exact(sc, path, {"enabled", "basis", "update", "persist", "start_micros", "tiers"}, errs):
        return
    _bool(sc["enabled"], f"{path}.enabled", errs)
    if not sc["enabled"]:
        return
    _req(sc["basis"] in STAGE_SCALING_BASES, f"{path}.basis in {STAGE_SCALING_BASES}", errs)
    _req(sc["update"] == "end_of_session", f"{path}.update must be end_of_session (never intraday)", errs)
    _bool(sc["persist"], f"{path}.persist", errs)
    _num(sc["start_micros"], f"{path}.start_micros", errs, null_ok=False, positive=True)
    prev = None
    for i, t in enumerate(sc["tiers"] if isinstance(sc["tiers"], list) else [None]):
        if _exact(t, f"{path}.tiers[{i}]", {"min_profit", "micros"}, errs):
            _num(t["min_profit"], f"{path}.tiers[{i}].min_profit", errs, null_ok=False)
            _num(t["micros"], f"{path}.tiers[{i}].micros", errs, null_ok=False, positive=True)
            _req(prev is None or t["min_profit"] > prev, f"{path}.tiers: min_profit must increase", errs)
            prev = t["min_profit"]


def _v2_stage(s: Any, path: str, errs: list, extra: set) -> None:
    keys = {"starting_balance", "max_micros", "drawdown", "dll", "consistency", "scaling"} | extra
    if not _exact(s, path, keys, errs):
        return
    _num(s["starting_balance"], f"{path}.starting_balance", errs, null_ok=False, positive=True)
    _num(s["max_micros"], f"{path}.max_micros", errs, null_ok=False, positive=True)
    _v2_drawdown(s["drawdown"], f"{path}.drawdown", errs)
    _v2_dll(s["dll"], f"{path}.dll", errs)
    _v2_consistency(s["consistency"], f"{path}.consistency", errs,
                    "evaluation_pass" if path == "evaluation" else "payout_request")
    _v2_scaling(s["scaling"], f"{path}.scaling", errs)


def _v2_payout(po: Any, errs: list) -> None:
    keys = {"enabled", "frequency", "winning_day_threshold", "require_positive_cycle_profit", "min_balance_to_request",
            "buffer_offset", "formula", "minimum", "cap", "split_trader", "count_limit", "cycle_reset",
            "after_payout_drawdown", "request"}
    if not _exact(po, "payout", keys, errs):
        return
    _bool(po["enabled"], "payout.enabled", errs)
    if not po["enabled"]:
        return
    if _exact(po["frequency"], "payout.frequency", {"mode", "winning_days"}, errs):
        _req(po["frequency"]["mode"] in FREQ_MODES, f"payout.frequency.mode in {FREQ_MODES}", errs)
        if po["frequency"]["mode"] == "winning_days":
            _num(po["frequency"]["winning_days"], "payout.frequency.winning_days", errs, null_ok=False, positive=True)
    _num(po["winning_day_threshold"], "payout.winning_day_threshold", errs)
    _bool(po["require_positive_cycle_profit"], "payout.require_positive_cycle_profit", errs)
    _num(po["min_balance_to_request"], "payout.min_balance_to_request", errs)
    _num(po["buffer_offset"], "payout.buffer_offset", errs)
    if _exact(po["formula"], "payout.formula", {"mode", "share", "multiple", "profit_basis"}, errs):
        f = po["formula"]
        _req(f["mode"] in FORMULA_MODES, f"payout.formula.mode in {FORMULA_MODES}", errs)
        _req(f["profit_basis"] in FORMULA_BASES, f"payout.formula.profit_basis in {FORMULA_BASES}", errs)
        if f["mode"] == "share_of_profit":
            _num(f["share"], "payout.formula.share", errs, null_ok=False, positive=True)
        if f["mode"] == "cycle_profit_multiple":
            _num(f["multiple"], "payout.formula.multiple", errs, null_ok=False, positive=True)
    _num(po["minimum"], "payout.minimum", errs, null_ok=False)
    if _exact(po["cap"], "payout.cap", {"mode", "amount", "schedule", "table"}, errs):
        c = po["cap"]
        _req(c["mode"] in CAP_MODES, f"payout.cap.mode in {CAP_MODES}", errs)
        if c["mode"] == "fixed":
            _num(c["amount"], "payout.cap.amount", errs, null_ok=False, positive=True)
        if c["mode"] == "by_payout_number":
            _req(isinstance(c["schedule"], list) and c["schedule"] and all(isinstance(x, (int, float)) and x > 0
                                                                           for x in c["schedule"]),
                 "payout.cap.schedule must be a non-empty list of amounts (the last one repeats)", errs)
        if c["mode"] == "by_purchase_date":
            ok = isinstance(c["table"], list) and c["table"] and all(
                isinstance(r, Mapping) and set(r) == {"purchased_on_or_after", "amount"} for r in c["table"])
            _req(bool(ok), "payout.cap.table must be [{purchased_on_or_after: YYYY-MM-DD, amount}]", errs)
    _num(po["split_trader"], "payout.split_trader", errs, null_ok=False, positive=True)
    _req(isinstance(po["split_trader"], (int, float)) and po["split_trader"] <= 1, "payout.split_trader must be <= 1", errs)
    _num(po["count_limit"], "payout.count_limit", errs)
    _req(po["cycle_reset"] == "after_payout", "payout.cycle_reset must be after_payout", errs)
    _req(po["after_payout_drawdown"] in AFTER_PAYOUT_DD, f"payout.after_payout_drawdown in {AFTER_PAYOUT_DD}", errs)
    _req(po["request"] in REQUEST_CONVENTIONS, f"payout.request in {REQUEST_CONVENTIONS}", errs)


def _validate_v2(raw: Mapping) -> dict:
    errs: list[str] = []
    top = {"kind", "schema_version", "profile_id", "version", "provider", "product", "path", "account_size", "currency",
           "quantity_unit", "purchase_date", "effective_date", "description", "trading_day", "evaluation", "funded",
           "payout", "live_transition", "conventions", "basis"}
    _exact(raw, "profile", top, errs)
    _req(raw.get("kind") == KIND, f"kind must be {KIND}", errs)
    pid = raw.get("profile_id")
    _req(isinstance(pid, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", pid or ""), "profile_id must match [A-Z][A-Z0-9_]*", errs)
    _req(isinstance(raw.get("version"), int) and raw.get("version", 0) >= 1, "version must be an integer >= 1", errs)
    for k in ("provider", "product", "path", "quantity_unit"):
        _req(isinstance(raw.get(k), str) and raw[k].strip() != "", f"{k} is required", errs)
    _num(raw.get("account_size"), "account_size", errs, null_ok=False, positive=True)
    pd_ = raw.get("purchase_date")
    _req(pd_ is None or (isinstance(pd_, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", pd_)), "purchase_date: YYYY-MM-DD or null", errs)
    if _exact(raw.get("trading_day"), "trading_day", {"timezone", "reset_time"}, errs):
        _req(isinstance(raw["trading_day"]["reset_time"], str), "trading_day.reset_time: HH:MM", errs)
    _v2_stage(raw.get("evaluation"), "evaluation", errs, {"profit_target", "minimum_trading_days"})
    if isinstance(raw.get("evaluation"), Mapping):
        _num(raw["evaluation"].get("profit_target"), "evaluation.profit_target", errs, null_ok=False, positive=True)
        _num(raw["evaluation"].get("minimum_trading_days"), "evaluation.minimum_trading_days", errs)
    _v2_stage(raw.get("funded"), "funded", errs, {"carry_evaluation_profit"})
    if isinstance(raw.get("funded"), Mapping):
        _bool(raw["funded"].get("carry_evaluation_profit"), "funded.carry_evaluation_profit", errs)
    _v2_payout(raw.get("payout"), errs)
    if _exact(raw.get("live_transition"), "live_transition", {"rule", "payout_count", "note"}, errs):
        lt = raw["live_transition"]
        _req(lt["rule"] in LIVE_RULES, f"live_transition.rule in {LIVE_RULES}", errs)
        if lt["rule"] == "after_payout_count":
            _num(lt["payout_count"], "live_transition.payout_count", errs, null_ok=False, positive=True)
    if _exact(raw.get("conventions"), "conventions", {"on_oversize", "funded_starts", "pass_checked", "payout_checked"}, errs):
        cv = raw["conventions"]
        _req(cv["on_oversize"] == "fail_incompatible", "conventions.on_oversize must be fail_incompatible (never clipped)", errs)
        _req(cv["funded_starts"] == "next_trading_day", "conventions.funded_starts must be next_trading_day", errs)
        _req(cv["pass_checked"] == "end_of_trading_day", "conventions.pass_checked must be end_of_trading_day", errs)
        _req(cv["payout_checked"] == "end_of_trading_day", "conventions.payout_checked must be end_of_trading_day", errs)
    if _exact(raw.get("basis"), "basis", {"status", "specified_on", "source", "modelling_choices", "sources", "evidence"}, errs):
        b = raw["basis"]
        _req(b["status"] in (STATUS_USER, STATUS_OFFICIAL, STATUS_TEST, STATUS_UNVERIFIED), "basis.status", errs)
        _req(isinstance(b["sources"], list) and isinstance(b["evidence"], Mapping), "basis.sources / basis.evidence", errs)
        _req(isinstance(b["modelling_choices"], list) and all(
            isinstance(c, Mapping) and set(c) == {"path", "value", "note"} for c in b["modelling_choices"]),
            "basis.modelling_choices must be [{path, value, note}]", errs)
    if errs:
        raise ProfileError("invalid prop profile:\n  " + "\n  ".join(errs))
    return dict(raw)


# ------------------------------------------------------------------------------------------ verification
def required_rule_paths(profile: Mapping) -> list[str]:
    if profile.get("schema_version") == 3:
        return sorted(profile["rules"])
    secs = RULE_SECTIONS + (("purchase_date",) if profile.get("schema_version") == 2 else ())
    return [p for sec in secs for p, _ in leaf_paths(profile[sec], sec)]


def _meta(profile: Mapping) -> Mapping:
    """The verification/basis block (schema 1: ``verification``; schema 2: ``basis``)."""
    return profile["basis"] if profile.get("schema_version") == 2 else profile["verification"]


def verification_report(profile: Mapping) -> dict:
    """Derived status + exactly what is missing. Never trusts the declared flag alone."""
    if profile.get("schema_version") == 3:
        rb = rule_basis(profile)
        return {"status": STATUS_RULEBOOK, "missing": [], "value_unknown": [], "reasons": [], "declared": None,
                "n_required": len(profile["rules"]), "n_missing": 0, "rule_basis": rb}
    v = _meta(profile)
    declared = v.get("status")
    pid = profile["profile_id"]
    if declared == STATUS_USER:
        src = v.get("source") or {}
        reasons = [r for r, bad in (("basis.source.title is not set", not src.get("title")),
                                    ("basis.source.provided_on is not set", not src.get("provided_on")),
                                    ("basis.specified_on is not set", not v.get("specified_on"))) if bad]
        return {"status": STATUS_USER if not reasons else STATUS_UNVERIFIED, "missing": [], "value_unknown": [],
                "reasons": reasons, "declared": declared, "n_required": len(required_rule_paths(profile)), "n_missing": 0}
    if declared == STATUS_TEST:
        if not pid.startswith("TEST_"):
            return {"status": STATUS_UNVERIFIED, "missing": ["profile_id must start with TEST_ for a test fixture"],
                    "value_unknown": [], "declared": declared}
        return {"status": STATUS_TEST, "missing": [], "value_unknown": [], "declared": declared}
    sources = {s.get("id"): s for s in v.get("sources", []) if isinstance(s, Mapping)}
    bad_src = [i for i, s in sources.items() if not (s.get("title") and str(s.get("url", "")).startswith("https://")
                                                     and s.get("accessed_on"))]
    missing, unknown = [], []
    for path in required_rule_paths(profile):
        val = _get(profile, path)
        ev = v.get("evidence", {}).get(path)
        ok = (isinstance(ev, Mapping) and ev.get("source") in sources and ev["source"] not in bad_src
              and str(ev.get("quote", "")).strip() != "" and "value" in ev and ev["value"] == val)
        if not ok:
            missing.append(path)
            if val is None:
                unknown.append(path)
    reasons = []
    if declared not in (STATUS_VERIFIED, STATUS_OFFICIAL):
        reasons.append("verification.status is not 'verified'")
    if not v.get("verified_on"):
        reasons.append("verification.verified_on is not set")
    if not sources:
        reasons.append("no official source is recorded (verification.sources)")
    if bad_src:
        reasons.append(f"sources lacking title / https URL / accessed_on: {sorted(map(str, bad_src))}")
    status = STATUS_VERIFIED if not missing and not reasons else STATUS_UNVERIFIED
    return {"status": status, "missing": missing, "value_unknown": unknown, "reasons": reasons, "declared": declared,
            "n_required": len(required_rule_paths(profile)), "n_missing": len(missing)}


def profile_identity(profile: Mapping) -> dict:
    rep = verification_report(profile)
    if profile.get("schema_version") == 3:
        rb = rep["rule_basis"]
        r = profile["rules"]
        return {"profile_id": profile["profile_id"], "version": profile["version"], "profile_hash": profile_hash(profile),
                "schema_version": 3, "provider": profile["provider"], "product": profile["product"], "path": profile["path"],
                "account_size": r["account.size"]["value"], "quantity_unit": r["account.quantity_unit"]["value"],
                "purchase_date": r["account.purchase_date"]["value"], "verification_status": STATUS_RULEBOOK,
                "rules_source": dict(profile["rules_source"]),
                **{k: rb[k] for k in ("verified_rule_count", "assumed_rule_count", "custom_rule_count",
                                      "inactive_rule_count", "basis_state")}}
    m = _meta(profile)
    out = {"profile_id": profile["profile_id"], "version": profile["version"], "profile_hash": profile_hash(profile),
           "schema_version": profile.get("schema_version"), "provider": profile["provider"], "product": profile["product"],
           "account_size": profile["account_size"], "purchase_date": profile.get("purchase_date"),
           "verification_status": rep["status"], "verified_on": m.get("verified_on"),
           "sources": [{k: s.get(k) for k in ("id", "title", "url", "accessed_on")} for s in m.get("sources", [])]}
    if profile.get("schema_version") == 2:
        out.update(path=profile["path"], rules_basis=rep["status"], specified_on=m.get("specified_on"),
                   rules_source=dict(m.get("source") or {}),
                   modelling_choices=[c["path"] for c in m.get("modelling_choices", [])])
    return out


# ------------------------------------------------------------------------------------------ files / registry
def profiles_dir(root: Path | str) -> Path:
    return Path(root) / "configs" / "prop" / "profiles"


def load_profile(path: Path | str) -> dict:
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    doc = validate_profile(raw)
    name = Path(path).name
    want = f"{doc['profile_id']}.v{doc['version']}.yaml"
    if name != want:
        raise ProfileError(f"file name {name!r} must be {want!r}")
    return doc


def _registry_path(d: Path) -> Path:
    return d / "REGISTRY.json"


def read_registry(d: Path) -> dict:
    p = _registry_path(d)
    return json.loads(p.read_text()) if p.exists() else {"schema_version": 1, "profiles": []}


def register_profile(d: Path, path: Path) -> dict:
    """Append a profile version to the registry (append-only; an existing id+version can never be re-registered)."""
    doc = load_profile(path)
    reg = read_registry(d)
    for e in reg["profiles"]:
        if (e["profile_id"], e["version"]) == (doc["profile_id"], doc["version"]):
            raise ProfileError(f"{doc['profile_id']} v{doc['version']} is already registered; create a new version instead")
    entry = {"profile_id": doc["profile_id"], "version": doc["version"], "file": Path(path).name,
             "profile_hash": profile_hash(doc)}
    reg["profiles"].append(entry)
    _registry_path(d).write_text(json.dumps(reg, indent=1, sort_keys=True) + "\n")
    return entry


def check_registry(d: Path) -> list[str]:
    """Problems (empty = consistent): a registered file changed or vanished, or a profile file is unregistered."""
    reg = read_registry(d)
    problems, seen = [], set()
    for e in reg["profiles"]:
        f = d / e["file"]
        seen.add(e["file"])
        if not f.exists():
            problems.append(f"{e['file']}: registered but missing")
            continue
        if profile_hash(load_profile(f)) != e["profile_hash"]:
            problems.append(f"{e['file']}: content changed since registration (create a new version, never edit in place)")
    for f in sorted(d.glob("*.yaml")):
        if f.name not in seen:
            problems.append(f"{f.name}: not registered")
    return problems


def load_default_profiles(root: Path | str) -> list[dict]:
    """The registered profiles, latest version of each profile id, in a fixed order."""
    d = profiles_dir(root)
    if not d.is_dir():
        return []
    latest: dict[str, dict] = {}
    for e in read_registry(d)["profiles"]:
        doc = load_profile(d / e["file"])
        if e["profile_id"] not in latest or doc["version"] > latest[e["profile_id"]]["version"]:
            latest[e["profile_id"]] = doc
    return [latest[k] for k in sorted(latest)]


# ================================================================================ schema 3: every rule = {value, status, basis}
# A schema-3 profile carries a flat ``rules`` map: dotted rule key -> {value, status, basis}. EVERY rule has a non-null value
# (the profile is always runnable). ``status`` is VERIFIED (supplied by the user / authoritative project evidence),
# ASSUMED_DEFAULT (an EdgeLab fallback assumption; still an ACTIVE rule the simulator checks) or CUSTOM (intentionally changed
# by the user). ``resolve`` turns the rules into the simulator's internal structure; results report the basis counts.
RULE_VERIFIED, RULE_ASSUMED, RULE_CUSTOM = "VERIFIED", "ASSUMED_DEFAULT", "CUSTOM"
RULE_STATUSES = (RULE_VERIFIED, RULE_ASSUMED, RULE_CUSTOM)
DD_MEASUREMENTS = ("closed_balance", "intratrade_worst_price")
DLL_MEASUREMENTS = ("closed_trades_only", "unrealized_worst_price", "intraday_worst_price")
DLL_BEHAVIORS3 = ("soft", "hard")
STAGES = ("evaluation", "funded")


def _is_num(v, lo=None, strict=False):
    ok = isinstance(v, (int, float)) and not isinstance(v, bool)
    if ok and lo is not None:
        ok = v > lo if strict else v >= lo
    return ok


def _chk(pred, msg):
    return lambda v: None if pred(v) else msg


NUM0 = _chk(lambda v: _is_num(v, 0), "a number >= 0")
POS = _chk(lambda v: _is_num(v, 0, strict=True), "a number > 0")
BOOL = _chk(lambda v: isinstance(v, bool), "true/false")
DATE = _chk(lambda v: isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) is not None, "YYYY-MM-DD")
HHMM = _chk(lambda v: isinstance(v, str) and re.fullmatch(r"\d{2}:\d{2}", v) is not None, "HH:MM")
STR = _chk(lambda v: isinstance(v, str) and v.strip() != "", "a non-empty string")


def ONE(*opts):
    return _chk(lambda v: v in opts, f"one of {list(opts)}")


def _tiers(v):
    if not isinstance(v, list):
        return "a list of {min_profit, micros}"
    prev = None
    for t in v:
        if not (isinstance(t, Mapping) and set(t) == {"min_profit", "micros"} and _is_num(t["min_profit"], 0)
                and _is_num(t["micros"], 0, strict=True)):
            return "a list of {min_profit, micros}"
        if prev is not None and t["min_profit"] <= prev:
            return "tiers with increasing min_profit"
        prev = t["min_profit"]
    return None


def _amounts(v):
    return None if isinstance(v, list) and all(_is_num(x, 0, strict=True) for x in v) else "a list of amounts > 0"


def _table(v):
    ok = isinstance(v, list) and all(isinstance(r, Mapping) and set(r) == {"purchased_on_or_after", "amount"}
                                     and DATE(r["purchased_on_or_after"]) is None and _is_num(r["amount"], 0, strict=True)
                                     for r in v)
    return None if ok else "a list of {purchased_on_or_after: YYYY-MM-DD, amount}"


def _stage_spec(s: str) -> dict:
    spec = {
        f"{s}.day_boundary.timezone": STR, f"{s}.day_boundary.reset_time": HHMM,
        f"{s}.starting_balance": POS, f"{s}.max_micros": POS,
        f"{s}.drawdown.max_loss": POS, f"{s}.drawdown.mode": ONE(*DD_MODES),
        f"{s}.drawdown.update_frequency": ONE("end_of_day"),
        f"{s}.drawdown.lock_enabled": BOOL, f"{s}.drawdown.lock_trigger_offset": NUM0,
        f"{s}.drawdown.locked_floor_offset": _chk(lambda v: _is_num(v), "a number"),
        f"{s}.drawdown.breach_comparison": ONE("at_or_below", "below"),
        f"{s}.drawdown.measurement": ONE(*DD_MEASUREMENTS),
        f"{s}.dll.enabled": BOOL, f"{s}.dll.amount": NUM0, f"{s}.dll.behavior": ONE(*DLL_BEHAVIORS3),
        f"{s}.dll.measurement": ONE(*DLL_MEASUREMENTS),
        f"{s}.consistency.enabled": BOOL,
        f"{s}.consistency.percent": _chk(lambda v: _is_num(v, 0) and v <= 100, "a percent 0..100"),
        f"{s}.consistency.window": ONE(*CONS_WINDOWS),
        f"{s}.consistency.cushion_enabled": BOOL, f"{s}.consistency.cushion_amount_usd": NUM0,
        f"{s}.consistency.cushion_percent_points": NUM0,
        f"{s}.scaling.enabled": BOOL, f"{s}.scaling.basis": ONE(*STAGE_SCALING_BASES),
        f"{s}.scaling.update": ONE("end_of_session"), f"{s}.scaling.effective": ONE("next_session"),
        f"{s}.scaling.persist": BOOL, f"{s}.scaling.start_micros": POS, f"{s}.scaling.tiers": _tiers}
    if s == "evaluation":
        spec.update({"evaluation.profit_target": POS, "evaluation.minimum_trading_days": NUM0})
    else:
        spec.update({"funded.carry_evaluation_profit": BOOL, "funded.starts": ONE("next_trading_day")})
    return spec


RULE_SPEC: dict = {
    "account.size": POS, "account.quantity_unit": STR, "account.purchase_date": DATE,
    **_stage_spec("evaluation"), **_stage_spec("funded"),
    "payout.enabled": BOOL, "payout.frequency_mode": ONE(*FREQ_MODES), "payout.winning_days_required": POS,
    "payout.winning_day_threshold": NUM0, "payout.positive_cycle_profit_required": BOOL,
    "payout.balance_requirement_enabled": BOOL, "payout.balance_requirement": NUM0,
    "payout.buffer_enabled": BOOL, "payout.buffer_offset": NUM0,
    "payout.formula_mode": ONE(*FORMULA_MODES), "payout.formula_factor": POS,
    "payout.formula_profit_basis": ONE(*FORMULA_BASES), "payout.minimum": NUM0,
    "payout.cap_mode": ONE(*CAP_MODES), "payout.cap_schedule": _amounts, "payout.cap_table": _table,
    "payout.split_trader": _chk(lambda v: _is_num(v, 0, strict=True) and v <= 1, "a fraction in (0, 1]"),
    "payout.count_limit_enabled": BOOL, "payout.count_limit": NUM0, "payout.cycle_reset": ONE("after_payout"),
    "payout.after_payout_drawdown": ONE(*AFTER_PAYOUT_DD), "payout.request": ONE(*REQUEST_CONVENTIONS),
    "live_transition.rule": ONE(*LIVE_RULES), "live_transition.payout_count": NUM0,
}


def _active(rules: Mapping, key: str) -> bool:
    """False when the rule is switched off by its gate (reported as inactive, excluded from the basis counts)."""
    v = lambda k: rules[k]["value"]
    parts = key.split(".")
    if parts[0] in STAGES:
        s, grp = parts[0], parts[1]
        if grp == "dll" and parts[2] != "enabled":
            return v(f"{s}.dll.enabled")
        if grp == "consistency" and parts[2] != "enabled":
            if not v(f"{s}.consistency.enabled"):
                return False
            if parts[2] in ("cushion_amount_usd", "cushion_percent_points"):
                return v(f"{s}.consistency.cushion_enabled")
        if grp == "scaling" and parts[2] != "enabled":
            return v(f"{s}.scaling.enabled")
        if grp == "drawdown" and parts[2] in ("lock_trigger_offset", "locked_floor_offset"):
            return v(f"{s}.drawdown.lock_enabled")
        return True
    if parts[0] == "payout":
        if key != "payout.enabled" and not v("payout.enabled"):
            return False
        gates = {"payout.winning_days_required": v("payout.frequency_mode") == "winning_days",
                 "payout.winning_day_threshold": v("payout.frequency_mode") == "winning_days",
                 "payout.balance_requirement": v("payout.balance_requirement_enabled"),
                 "payout.buffer_offset": v("payout.buffer_enabled"),
                 "payout.count_limit": v("payout.count_limit_enabled"),
                 "payout.cap_schedule": v("payout.cap_mode") in ("fixed", "by_payout_number"),
                 "payout.cap_table": v("payout.cap_mode") == "by_purchase_date"}
        return gates.get(key, True)
    if key == "live_transition.payout_count":
        return v("live_transition.rule") == "after_payout_count"
    if key == "account.purchase_date":
        return v("payout.enabled") and v("payout.cap_mode") == "by_purchase_date"
    return True


def _validate_v3(raw: Mapping) -> dict:
    errs: list[str] = []
    _exact(raw, "profile", {"kind", "schema_version", "profile_id", "version", "provider", "product", "path", "description",
                            "rules_source", "conventions", "rules"}, errs)
    _req(raw.get("kind") == KIND, f"kind must be {KIND}", errs)
    pid = raw.get("profile_id")
    _req(isinstance(pid, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", pid or ""), "profile_id must match [A-Z][A-Z0-9_]*", errs)
    _req(isinstance(raw.get("version"), int) and raw.get("version", 0) >= 1, "version must be an integer >= 1", errs)
    for k in ("provider", "product", "path"):
        _req(isinstance(raw.get(k), str) and raw[k].strip() != "", f"{k} is required", errs)
    src = raw.get("rules_source")
    _req(isinstance(src, Mapping) and bool(src.get("title")) and bool(src.get("provided_on")),
         "rules_source must be {title, provided_on, ...}", errs)
    if _exact(raw.get("conventions"), "conventions", {"on_oversize", "pass_checked", "payout_checked"}, errs):
        cv = raw["conventions"]
        _req(cv["on_oversize"] == "fail_incompatible", "conventions.on_oversize must be fail_incompatible (never clipped)", errs)
        _req(cv["pass_checked"] == "end_of_trading_day", "conventions.pass_checked must be end_of_trading_day", errs)
        _req(cv["payout_checked"] == "end_of_trading_day", "conventions.payout_checked must be end_of_trading_day", errs)
    rules = raw.get("rules")
    if not isinstance(rules, Mapping):
        errs.append("rules: must be a mapping of rule key -> {value, status, basis}")
    else:
        miss, extra = set(RULE_SPEC) - set(rules), set(rules) - set(RULE_SPEC)
        _req(not miss, f"rules missing {sorted(miss)}", errs)
        _req(not extra, f"unknown rules {sorted(extra)}", errs)
        for k in sorted(set(rules) & set(RULE_SPEC)):
            r = rules[k]
            if not (isinstance(r, Mapping) and set(r) == {"value", "status", "basis"}):
                errs.append(f"rules.{k}: must be exactly {{value, status, basis}}")
                continue
            if r["value"] is None:
                errs.append(f"rules.{k}: value is null (every rule needs a value; mark an assumption ASSUMED_DEFAULT)")
                continue
            e = RULE_SPEC[k](r["value"])
            _req(e is None, f"rules.{k}: must be {e}", errs)
            _req(r["status"] in RULE_STATUSES, f"rules.{k}.status must be one of {RULE_STATUSES}", errs)
            _req(isinstance(r["basis"], str) and r["basis"].strip() != "", f"rules.{k}.basis must be a non-empty string", errs)
        if not miss and not errs:
            v = lambda k: rules[k]["value"]
            for s in STAGES:
                if v(f"{s}.scaling.enabled"):
                    _req(v(f"{s}.scaling.start_micros") <= v(f"{s}.max_micros"), f"{s}.scaling.start_micros > max_micros", errs)
            if v("payout.enabled"):
                _req(v("payout.cap_mode") not in ("fixed", "by_payout_number") or len(v("payout.cap_schedule")) > 0,
                     "payout.cap_schedule must not be empty for this cap_mode", errs)
                _req(v("payout.cap_mode") != "by_purchase_date" or any(
                    str(r["purchased_on_or_after"]) <= v("account.purchase_date") for r in v("payout.cap_table")),
                    "payout.cap_table has no cap for account.purchase_date (add the cap for that purchase date)", errs)
    if errs:
        raise ProfileError("invalid prop profile:\n  " + "\n  ".join(errs))
    return dict(raw)


def rule_basis(profile: Mapping) -> dict:
    """Counts and lists of VERIFIED / ASSUMED_DEFAULT / CUSTOM over the ACTIVE rules (+ the inactive ones)."""
    rules = profile["rules"]
    out = {"verified_rule_count": 0, "assumed_rule_count": 0, "custom_rule_count": 0, "inactive_rule_count": 0,
           "assumed_rules": {}, "custom_rules": {}}
    for k in sorted(rules):
        r = rules[k]
        if not _active(rules, k):
            out["inactive_rule_count"] += 1
            continue
        if r["status"] == RULE_VERIFIED:
            out["verified_rule_count"] += 1
        elif r["status"] == RULE_ASSUMED:
            out["assumed_rule_count"] += 1
            out["assumed_rules"][k] = {"value": r["value"], "basis": r["basis"]}
        else:
            out["custom_rule_count"] += 1
            out["custom_rules"][k] = {"value": r["value"], "basis": r["basis"]}
    out["basis_state"] = ("RULE_ASSUMED" if out["assumed_rule_count"] else
                          "CUSTOM_RULES" if out["custom_rule_count"] else "VERIFIED_RULES")
    out["label"] = {"RULE_ASSUMED": "UNDER DEFAULT ASSUMED RULES", "CUSTOM_RULES": "UNDER CUSTOM RULES",
                    "VERIFIED_RULES": "UNDER VERIFIED RULES"}[out["basis_state"]]
    return out


def resolve(profile: Mapping) -> dict:
    """The simulator's internal rule structure from a schema-3 profile (values only; disabled options become None)."""
    v = lambda k: profile["rules"][k]["value"]

    def stage(s):
        lock = v(f"{s}.drawdown.lock_enabled")
        out = {"day_boundary": {"timezone": v(f"{s}.day_boundary.timezone"), "reset_time": v(f"{s}.day_boundary.reset_time")},
               "starting_balance": v(f"{s}.starting_balance"), "max_micros": v(f"{s}.max_micros"),
               "drawdown": {"max_loss": v(f"{s}.drawdown.max_loss"), "mode": v(f"{s}.drawdown.mode"),
                            "lock_trigger_offset": v(f"{s}.drawdown.lock_trigger_offset") if lock else None,
                            "locked_floor_offset": v(f"{s}.drawdown.locked_floor_offset") if lock else None,
                            "breach_comparison": v(f"{s}.drawdown.breach_comparison"),
                            "measurement": v(f"{s}.drawdown.measurement")},
               "dll": {"enabled": v(f"{s}.dll.enabled"), "amount": v(f"{s}.dll.amount"), "behavior": v(f"{s}.dll.behavior"),
                       "measurement": v(f"{s}.dll.measurement")},
               "consistency": {"enabled": v(f"{s}.consistency.enabled"), "percent": v(f"{s}.consistency.percent"),
                               "window": v(f"{s}.consistency.window"),
                               "cushion": {"amount_usd": v(f"{s}.consistency.cushion_amount_usd")
                                           if v(f"{s}.consistency.cushion_enabled") else 0,
                                           "percent_points": v(f"{s}.consistency.cushion_percent_points")
                                           if v(f"{s}.consistency.cushion_enabled") else 0}},
               "scaling": {"enabled": v(f"{s}.scaling.enabled"), "basis": v(f"{s}.scaling.basis"),
                           "persist": v(f"{s}.scaling.persist"), "start_micros": v(f"{s}.scaling.start_micros"),
                           "tiers": list(v(f"{s}.scaling.tiers"))}}
        if s == "evaluation":
            out.update(profit_target=v("evaluation.profit_target"), minimum_trading_days=v("evaluation.minimum_trading_days"))
        else:
            out["carry_evaluation_profit"] = v("funded.carry_evaluation_profit")
        return out

    return {"profile_id": profile["profile_id"], "version": profile["version"], "provider": profile["provider"],
            "product": profile["product"], "account_size": v("account.size"), "quantity_unit": v("account.quantity_unit"),
            "purchase_date": v("account.purchase_date"), "conventions": dict(profile["conventions"]),
            "evaluation": stage("evaluation"), "funded": stage("funded"),
            "payout": {"enabled": v("payout.enabled"), "frequency_mode": v("payout.frequency_mode"),
                       "winning_days_required": v("payout.winning_days_required"),
                       "winning_day_threshold": v("payout.winning_day_threshold"),
                       "positive_cycle_profit_required": v("payout.positive_cycle_profit_required"),
                       "balance_requirement": v("payout.balance_requirement") if v("payout.balance_requirement_enabled") else None,
                       "buffer_offset": v("payout.buffer_offset") if v("payout.buffer_enabled") else None,
                       "formula_mode": v("payout.formula_mode"), "formula_factor": v("payout.formula_factor"),
                       "formula_profit_basis": v("payout.formula_profit_basis"), "minimum": v("payout.minimum"),
                       "cap_mode": v("payout.cap_mode"), "cap_schedule": list(v("payout.cap_schedule")),
                       "cap_table": list(v("payout.cap_table")), "split_trader": v("payout.split_trader"),
                       "count_limit": v("payout.count_limit") if v("payout.count_limit_enabled") else None,
                       "after_payout_drawdown": v("payout.after_payout_drawdown"), "request": v("payout.request")},
            "live_transition": {"rule": v("live_transition.rule"), "payout_count": v("live_transition.payout_count")}}


def customize(profile: Mapping, changes: Mapping[str, Any], basis: str, *, version: int | None = None) -> dict:
    """A NEW version of a schema-3 profile with the given rules changed and marked CUSTOM (the original is untouched)."""
    doc = json.loads(json.dumps(profile))
    for k, val in changes.items():
        if k not in doc["rules"]:
            raise ProfileError(f"unknown rule {k!r}")
        doc["rules"][k] = {"value": val, "status": RULE_CUSTOM, "basis": basis}
    doc["version"] = version if version is not None else int(profile["version"]) + 1
    return validate_profile(doc)
