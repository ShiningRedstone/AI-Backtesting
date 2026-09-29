"""Versioned discovery-request and proposal schemas (data only; no code is ever accepted)."""
from __future__ import annotations

import re
from typing import Any, Mapping

from edgelab.strategy import dsl
from edgelab.strategy.proposals import claim_phrases

REQUEST_VERSION = 1
PROPOSAL_SCHEMA_VERSION = 1
MAX_PROPOSALS = 10
MODES = ("hypothesis", "template", "modify")
TEMPLATES = {
    "ema_trend": "Trend continuation: a fast moving average crossing a slow one",
    "rsi_reversion": "Mean reversion after an RSI extreme",
    "range_breakout": "Volatility expansion: a close outside an ATR band around the mean",
}
REQUEST_KEYS = {"request_version", "mode", "hypothesis", "template", "base_strategy_id", "n_proposals",
                "scope", "constraints", "provider"}
SCOPE_KEYS = {"dataset_id", "instrument", "timeframe", "session", "direction", "date_scope"}
CONSTRAINT_KEYS = {"features", "entry_orders", "stop_types", "target_types", "sizing_modes", "exit_kinds",
                   "max_cooldown_bars", "max_conditions", "max_parameters", "parameter_limits"}
EXIT_KINDS = ("stop", "target", "time_stop_bars", "max_hold_bars", "signal")

# What a provider returns per proposal (the "content"); EdgeLab wraps it in the envelope below.
CONTENT_KEYS = {"hypothesis", "strategy_name", "family", "definition", "parameters", "timeframe",
                "instrument", "session", "assumptions", "rationale", "constraints", "changes"}
CONTENT_REQUIRED = ("hypothesis", "strategy_name", "family", "definition", "timeframe", "rationale")
FAMILY_KEYS = {"id", "name", "category"}
ENVELOPE_KEYS = ("proposal_id", "schema_version", "request_id", "generation_id", "index", "provider",
                 "generation", "content")

CODE_PATTERNS = [r"\bdef\s+\w+\s*\(", r"\bimport\s+[\w.]+", r"\bfrom\s+[\w.]+\s+import\b", r"\blambda\b[^:]*:",
                 r"__\w+__", r"\b(exec|eval|compile|open|getattr|setattr)\s*\(", r"```", r"<script",
                 r"\bos\.system\b", r"\bsubprocess\b"]


class DiscoveryRequestError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def code_phrases(text: str) -> list[str]:
    return [m.group(0) for p in CODE_PATTERNS for m in re.finditer(p, text or "", re.IGNORECASE)]


def _strings(node: Any, path: str = ""):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, Mapping):
        for k, v in node.items():
            yield from _strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")


def _str_list(v: Any, allowed: tuple | None, name: str, errors: list) -> list | None:
    if v is None:
        return None
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v) or not v:
        errors.append(f"constraints.{name} must be a non-empty list of names")
        return None
    if allowed is not None:
        bad = [x for x in v if x not in allowed]
        if bad:
            errors.append(f"constraints.{name}: unsupported {bad} (supported: {list(allowed)})")
    return sorted(set(v))


def _int(v: Any, lo: int, hi: int, name: str, errors: list) -> int | None:
    if v is None:
        return None
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        errors.append(f"{name} must be an integer in {lo}..{hi}")
        return None
    return v


def normalize_request(raw: Any, feature_ids: list[str], session_names: list[str]) -> dict:
    """Validate a discovery request and return its canonical form. Refuses (never repairs):
    unknown keys, claim language, code, unsupported constructs, out-of-range bounds."""
    errors: list[str] = []
    if not isinstance(raw, Mapping):
        raise DiscoveryRequestError(["request must be a JSON object"])
    for k in raw:
        if k not in REQUEST_KEYS:
            errors.append(f"unknown request key {k!r} (allowed: {sorted(REQUEST_KEYS)})")
    if raw.get("request_version", REQUEST_VERSION) != REQUEST_VERSION:
        errors.append(f"unsupported request_version; this build reads {REQUEST_VERSION}")
    mode = raw.get("mode", "hypothesis")
    if mode not in MODES:
        errors.append(f"mode must be one of {MODES}")
    hyp = raw.get("hypothesis") or ""
    if not isinstance(hyp, str) or len(hyp) > 2000:
        errors.append("hypothesis must be text of at most 2000 characters")
        hyp = ""
    hyp = hyp.strip()
    if mode == "hypothesis" and not hyp:
        errors.append("mode 'hypothesis' needs a plain-language hypothesis")
    if hits := claim_phrases(hyp):
        errors.append(f"hypothesis contains performance-claim language {hits}: state a market hypothesis; "
                      "only the numerical engine measures performance")
    if hits := code_phrases(hyp):
        errors.append(f"hypothesis contains code {hits}; requests are plain language and data only")
    template = raw.get("template")
    if mode == "template" and template not in TEMPLATES:
        errors.append(f"mode 'template' needs template one of {list(TEMPLATES)}")
    if mode != "template" and template is not None and template not in TEMPLATES:
        errors.append(f"template must be one of {list(TEMPLATES)}")
    base = raw.get("base_strategy_id")
    if mode == "modify" and not (isinstance(base, str) and re.match(r"^STR_[A-F0-9]{6,}$", base)):
        errors.append("mode 'modify' needs base_strategy_id (a library strategy id STR_...)")
    if mode != "modify" and base is not None:
        errors.append("base_strategy_id is only valid in mode 'modify'")
    n = _int(raw.get("n_proposals", 4), 1, MAX_PROPOSALS, "n_proposals", errors) or 4
    scope_raw = raw.get("scope") or {}
    scope: dict = {}
    if not isinstance(scope_raw, Mapping):
        errors.append("scope must be an object")
        scope_raw = {}
    for k in scope_raw:
        if k not in SCOPE_KEYS:
            errors.append(f"unknown scope key {k!r} (allowed: {sorted(SCOPE_KEYS)})")
    for k in ("dataset_id", "instrument", "timeframe", "session"):
        v = scope_raw.get(k)
        if v is not None and not (isinstance(v, str) and re.match(r"^[A-Za-z0-9_\-.]{1,120}$", v)):
            errors.append(f"scope.{k} must be an identifier")
        scope[k] = v
    if scope.get("session") is not None and scope["session"] not in session_names:
        errors.append(f"scope.session {scope['session']!r} is not a configured session")
    d = scope_raw.get("direction")
    if d is not None and d not in dsl.DIRECTIONS:
        errors.append(f"scope.direction must be one of {dsl.DIRECTIONS}")
    scope["direction"] = d
    ds_ = scope_raw.get("date_scope")
    if ds_ is not None:
        if not (isinstance(ds_, Mapping) and set(ds_) <= {"start", "end"}
                and all(isinstance(ds_.get(x), (str, type(None))) for x in ("start", "end"))):
            errors.append("scope.date_scope must be {start, end} ISO dates (or null)")
            ds_ = None
        else:
            ds_ = {"start": ds_.get("start"), "end": ds_.get("end")}
            for x in ("start", "end"):
                if ds_[x] is not None and not re.match(r"^\d{4}-\d{2}-\d{2}$", ds_[x]):
                    errors.append(f"scope.date_scope.{x} must be YYYY-MM-DD")
            if ds_["start"] and ds_["end"] and ds_["start"] >= ds_["end"]:
                errors.append("scope.date_scope: start must be before end")
    scope["date_scope"] = ds_
    c_raw = raw.get("constraints") or {}
    cons: dict = {}
    if not isinstance(c_raw, Mapping):
        errors.append("constraints must be an object")
        c_raw = {}
    for k in c_raw:
        if k not in CONSTRAINT_KEYS:
            errors.append(f"unknown constraint {k!r} (supported: {sorted(CONSTRAINT_KEYS)})")
    cons["features"] = _str_list(c_raw.get("features"), tuple(feature_ids), "features", errors)
    cons["entry_orders"] = _str_list(c_raw.get("entry_orders"), dsl.ENTRY_ORDER_TYPES, "entry_orders", errors)
    cons["stop_types"] = _str_list(c_raw.get("stop_types"), dsl.STOP_TYPES, "stop_types", errors)
    cons["target_types"] = _str_list(c_raw.get("target_types"), dsl.TARGET_TYPES, "target_types", errors)
    cons["sizing_modes"] = _str_list(c_raw.get("sizing_modes"), dsl.SIZING_MODES, "sizing_modes", errors)
    cons["exit_kinds"] = _str_list(c_raw.get("exit_kinds"), EXIT_KINDS, "exit_kinds", errors)
    cons["max_cooldown_bars"] = _int(c_raw.get("max_cooldown_bars"), 0, 10_000, "constraints.max_cooldown_bars", errors)
    cons["max_conditions"] = _int(c_raw.get("max_conditions"), 1, 20, "constraints.max_conditions", errors)
    cons["max_parameters"] = _int(c_raw.get("max_parameters"), 0, 20, "constraints.max_parameters", errors)
    pl = c_raw.get("parameter_limits")
    if pl is not None:
        ok = isinstance(pl, Mapping) and all(
            isinstance(k, str) and isinstance(v, Mapping) and set(v) <= {"min", "max"} and v
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v.values())
            for k, v in pl.items())
        if not ok:
            errors.append("constraints.parameter_limits must map a parameter name to {min, max} numbers")
            pl = None
        else:
            pl = {k: dict(v) for k, v in sorted(pl.items())}
            for k, v in pl.items():
                if "min" in v and "max" in v and v["min"] > v["max"]:
                    errors.append(f"constraints.parameter_limits.{k}: min > max")
    cons["parameter_limits"] = pl
    prov = raw.get("provider")
    if prov is not None and not (isinstance(prov, str) and re.match(r"^[a-z][a-z0-9_]{0,40}$", prov)):
        errors.append("provider must be a provider name")
    if errors:
        raise DiscoveryRequestError(errors)
    return {"request_version": REQUEST_VERSION, "mode": mode, "hypothesis": hyp,
            "template": template, "base_strategy_id": base, "n_proposals": n, "scope": scope,
            "constraints": cons, "provider": prov}
