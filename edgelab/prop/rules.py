"""Versioned, machine-readable prop-account rule sets (Phase 6 prop simulation layer).

A rule set is DATA (YAML/JSON), validated strictly: unknown keys are errors, and rules that the
stored trade records cannot model honestly are refused by name instead of approximated (see
``REFUSED``). No firm's rules are built in; ``configs/prop/`` ships only synthetic, test-only
examples. See PROP_SIMULATION.md for every field and its exact semantics.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from edgelab.core.identity import hash_obj

KIND = "edgelab.prop_rules"
SCHEMA_VERSION = 1
ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

DETECTION = ("end_of_trade", "intratrade_bound")
DRAWDOWN_MODES = ("static", "trailing")
TRAILING_REFERENCES = ("closed_balance", "end_of_day_balance")
DAILY_LOSS_ACTIONS = ("terminate", "pause_day")
VIOLATION_ACTIONS = ("terminate", "record")

# Rules the trade records cannot support, refused by name (never approximated).
REFUSED = {
    "intratrade_equity_high": "trailing drawdown from intra-trade (unrealized) equity highs needs the order "
                              "of MFE and MAE inside each trade; trade records hold only bar-resolution "
                              "MFE/MAE magnitudes, not their timing",
    "unrealized_equity_high": "same as intratrade_equity_high",
}
REFUSED_KEYS = {
    "payouts": "payout withdrawals/splits are not simulated (only payout eligibility is reported)",
    "news_restrictions": "no economic-event calendar exists in the project data",
    "weekend_holding": "positions are flattened by the backtest (hold_overnight: false); not modelled",
}

_TOP = {"kind", "schema_version", "id", "name", "description", "synthetic_test_only", "account",
        "trading_day", "target", "drawdown", "daily_loss", "detection", "min_trading_days",
        "max_calendar_days", "position", "consistency", "payout_eligibility", "session"}


class PropConfigError(ValueError):
    """A prop rule set is invalid or asks for a rule that cannot be modelled honestly."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class PropRules:
    """A validated rule set. ``doc`` is the normalized document; ``config_hash`` identifies it."""
    doc: dict = field(repr=False)
    config_hash: str = ""

    def __getitem__(self, k: str) -> Any:
        return self.doc[k]

    @property
    def id(self) -> str:
        return self.doc["id"]

    def to_dict(self) -> dict:
        return copy.deepcopy(self.doc)


def _num(v: Any, path: str, errs: list, *, positive: bool = False, allow_none: bool = False,
         integer: bool = False) -> Any:
    if v is None:
        if not allow_none:
            errs.append(f"{path} is required")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        errs.append(f"{path} must be a finite number")
        return None
    if integer and int(v) != v:
        errs.append(f"{path} must be a whole number")
        return None
    if positive and not v > 0:
        errs.append(f"{path} must be > 0")
        return None
    if not positive and v < 0:
        errs.append(f"{path} must be >= 0")
        return None
    return int(v) if integer else float(v)


def _keys(d: Any, allowed: set, path: str, errs: list) -> dict:
    if d is None:
        return {}
    if not isinstance(d, Mapping):
        errs.append(f"{path} must be a mapping")
        return {}
    for k in d:
        if k in REFUSED_KEYS:
            errs.append(f"{path}.{k} is not supported: {REFUSED_KEYS[k]}")
        elif k not in allowed:
            errs.append(f"{path}.{k} is not a known field (allowed: {sorted(allowed)})")
    return dict(d)


def _tz(v: Any, path: str, errs: list) -> str | None:
    if not isinstance(v, str) or not v:
        errs.append(f"{path} must be an IANA timezone name (e.g. America/New_York)")
        return None
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError):
        errs.append(f"{path}: unknown timezone {v!r}")
        return None
    return v


def _time(v: Any, path: str, errs: list) -> str | None:
    if not isinstance(v, str) or not TIME_RE.match(v):
        errs.append(f"{path} must be HH:MM (24h)")
        return None
    return v


def _choice(v: Any, options: tuple, path: str, errs: list, default: str | None = None) -> str | None:
    if v is None and default is not None:
        return default
    if v not in options:
        errs.append(f"{path} must be one of {list(options)}")
        return None
    return v


def validate_rules(raw: Any) -> PropRules:
    """Validate and normalize a prop rule set; raises PropConfigError listing every problem."""
    errs: list[str] = []
    if not isinstance(raw, Mapping):
        raise PropConfigError(["a prop rule set must be a mapping"])
    top = _keys(raw, _TOP, "rules", errs)
    if top.get("kind") != KIND:
        errs.append(f"kind must be {KIND!r}")
    if top.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version must be {SCHEMA_VERSION}")
    rid = top.get("id")
    if not isinstance(rid, str) or not ID_RE.match(rid):
        errs.append("id must be 1-64 characters of letters, digits, '_', '.', '-'")
    name = top.get("name")
    if not isinstance(name, str) or not name.strip():
        errs.append("name is required")
    synthetic = top.get("synthetic_test_only", False)
    if not isinstance(synthetic, bool):
        errs.append("synthetic_test_only must be true or false")
    desc = top.get("description", "")
    if not isinstance(desc, str):
        errs.append("description must be text")

    acc = _keys(top.get("account"), {"starting_balance", "currency"}, "account", errs)
    start = _num(acc.get("starting_balance"), "account.starting_balance", errs, positive=True)
    currency = acc.get("currency", "USD")
    if currency != "USD":
        errs.append("account.currency must be USD (trade records are in USD)")

    td = _keys(top.get("trading_day"), {"timezone", "reset_time"}, "trading_day", errs)
    if "trading_day" not in top:
        errs.append("trading_day (timezone, reset_time) is required: the daily reset is never guessed")
    day_tz, reset = _tz(td.get("timezone"), "trading_day.timezone", errs), _time(td.get("reset_time"), "trading_day.reset_time", errs)

    tg = _keys(top.get("target"), {"profit", "stop_on_target"}, "target", errs)
    profit = _num(tg.get("profit"), "target.profit", errs, positive=True, allow_none=True)
    stop_on_target = tg.get("stop_on_target", True)
    if not isinstance(stop_on_target, bool):
        errs.append("target.stop_on_target must be true or false")

    dd = _keys(top.get("drawdown"), {"max", "mode", "trailing_reference", "lock_floor_at"}, "drawdown", errs)
    dd_max = _num(dd.get("max"), "drawdown.max", errs, positive=True, allow_none=True)
    mode = _choice(dd.get("mode"), DRAWDOWN_MODES, "drawdown.mode", errs, default="static")
    ref = dd.get("trailing_reference")
    lock = _num(dd.get("lock_floor_at"), "drawdown.lock_floor_at", errs, positive=True, allow_none=True)
    if ref in REFUSED:
        errs.append(f"drawdown.trailing_reference {ref!r} is refused: {REFUSED[ref]}")
        ref = None
    elif mode == "trailing":
        ref = _choice(ref, TRAILING_REFERENCES, "drawdown.trailing_reference", errs)
    elif ref is not None or lock is not None:
        errs.append("drawdown.trailing_reference / lock_floor_at apply only to mode: trailing")
    if dd_max is None and top.get("drawdown") is not None and dd.get("max") is None:
        errs.append("drawdown.max is required when a drawdown rule is given")

    dl = _keys(top.get("daily_loss"), {"max", "action"}, "daily_loss", errs)
    dl_max = _num(dl.get("max"), "daily_loss.max", errs, positive=True, allow_none=True)
    dl_action = _choice(dl.get("action"), DAILY_LOSS_ACTIONS, "daily_loss.action", errs, default="terminate")
    if top.get("daily_loss") is not None and dl.get("max") is None:
        errs.append("daily_loss.max is required when a daily-loss rule is given")

    detection = _choice(top.get("detection"), DETECTION, "detection", errs, default="end_of_trade")
    min_days = _num(top.get("min_trading_days", 0), "min_trading_days", errs, integer=True)
    max_cal = _num(top.get("max_calendar_days"), "max_calendar_days", errs, positive=True, allow_none=True,
                   integer=True)

    pos = _keys(top.get("position"), {"max_units", "action", "scaling"}, "position", errs)
    max_units = _num(pos.get("max_units"), "position.max_units", errs, positive=True, allow_none=True)
    pos_action = _choice(pos.get("action"), VIOLATION_ACTIONS, "position.action", errs, default="terminate")
    scaling = []
    raw_sc = pos.get("scaling") or []
    if not isinstance(raw_sc, list):
        errs.append("position.scaling must be a list of {min_profit, max_units}")
        raw_sc = []
    for i, tier in enumerate(raw_sc):
        t = _keys(tier, {"min_profit", "max_units"}, f"position.scaling[{i}]", errs)
        mp = _num(t.get("min_profit"), f"position.scaling[{i}].min_profit", errs)
        mu = _num(t.get("max_units"), f"position.scaling[{i}].max_units", errs, positive=True)
        if mp is not None and mu is not None:
            scaling.append({"min_profit": mp, "max_units": mu})
    if scaling and max_units is None:
        errs.append("position.scaling needs a base position.max_units")
    if [s["min_profit"] for s in scaling] != sorted({s["min_profit"] for s in scaling}):
        errs.append("position.scaling tiers must have strictly increasing min_profit")

    cons = _keys(top.get("consistency"), {"max_best_day_share"}, "consistency", errs)
    share = None
    if top.get("consistency") is not None:
        share = _num(cons.get("max_best_day_share"), "consistency.max_best_day_share", errs, positive=True)
        if share is not None and share > 1:
            errs.append("consistency.max_best_day_share must be in (0, 1]")

    pe = _keys(top.get("payout_eligibility"), {"min_trading_days", "min_profit"}, "payout_eligibility", errs)
    payout = None
    if top.get("payout_eligibility") is not None:
        payout = {"min_trading_days": _num(pe.get("min_trading_days", 0), "payout_eligibility.min_trading_days",
                                           errs, integer=True),
                  "min_profit": _num(pe.get("min_profit", 0), "payout_eligibility.min_profit", errs)}

    se = _keys(top.get("session"), {"timezone", "entry_start", "entry_end", "flat_by", "action"}, "session", errs)
    session = None
    if top.get("session") is not None:
        session = {"timezone": _tz(se.get("timezone"), "session.timezone", errs),
                   "entry_start": None if se.get("entry_start") is None else _time(se.get("entry_start"), "session.entry_start", errs),
                   "entry_end": None if se.get("entry_end") is None else _time(se.get("entry_end"), "session.entry_end", errs),
                   "flat_by": None if se.get("flat_by") is None else _time(se.get("flat_by"), "session.flat_by", errs),
                   "action": _choice(se.get("action"), VIOLATION_ACTIONS, "session.action", errs, default="terminate")}
        if (session["entry_start"] is None) != (session["entry_end"] is None):
            errs.append("session.entry_start and session.entry_end must be given together")
        if session["entry_start"] is None and session["flat_by"] is None:
            errs.append("session needs an entry window and/or flat_by")

    if profit is None and dd_max is None and dl_max is None and max_units is None and session is None:
        errs.append("a rule set needs at least one rule (target, drawdown, daily_loss, position or session)")
    if errs:
        raise PropConfigError(errs)

    doc = {"kind": KIND, "schema_version": SCHEMA_VERSION, "id": rid, "name": name.strip(),
           "description": desc, "synthetic_test_only": synthetic,
           "account": {"starting_balance": start, "currency": "USD"},
           "trading_day": {"timezone": day_tz, "reset_time": reset},
           "target": None if profit is None else {"profit": profit, "stop_on_target": stop_on_target},
           "drawdown": None if dd_max is None else {"max": dd_max, "mode": mode,
                                                    "trailing_reference": ref if mode == "trailing" else None,
                                                    "lock_floor_at": lock if mode == "trailing" else None},
           "daily_loss": None if dl_max is None else {"max": dl_max, "action": dl_action},
           "detection": detection, "min_trading_days": min_days, "max_calendar_days": max_cal,
           "position": None if max_units is None else {"max_units": max_units, "action": pos_action,
                                                       "scaling": scaling},
           "consistency": None if share is None else {"max_best_day_share": share},
           "payout_eligibility": payout, "session": session}
    # The hash covers the rules, not cosmetic text: renaming/describing a rule set keeps its hash.
    rules_only = {k: v for k, v in doc.items() if k not in ("name", "description")}
    return PropRules(doc, hash_obj(rules_only))
