"""AI provider abstraction: ``AIProvider.generate_proposals(request, context) -> ProviderOutput``.

Providers return proposal CONTENT only (data). EdgeLab assigns ids, wraps each item in the versioned
envelope and sends it through the strict gate; a provider can never bypass it.

Configured by environment (or the desktop app's user settings file, key ``ai``), never inside a
workspace, strategy or run:

    EDGELAB_AI_PROVIDER   none (default) | mock | anthropic
    EDGELAB_AI_MODEL      model name for an external provider (required; no default is assumed)
    ANTHROPIC_API_KEY     read from the environment only when the anthropic provider is used; it is
                          never logged, stored, hashed or returned (status reports only "present")

With no provider configured EdgeLab works offline: the deterministic ``mock`` provider is always
available to exercise the pipeline, and the page says that no AI provider is configured.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from edgelab.core.identity import stable_json

PROVIDER_ENV, MODEL_ENV, KEY_ENV = "EDGELAB_AI_PROVIDER", "EDGELAB_AI_MODEL", "ANTHROPIC_API_KEY"
PROVIDER_KINDS = ("none", "mock", "anthropic")


class ProviderError(RuntimeError):
    pass


@dataclass
class ProviderOutput:
    proposals: list                         # raw content items, exactly as the provider returned them
    provider: dict                          # {kind, name, model, provider_version}
    raw_sha256: str                         # hash of the provider's raw output (audit)
    notes: list = field(default_factory=list)


class AIProvider(Protocol):
    kind: str

    def describe(self) -> dict: ...

    def generate_proposals(self, request: Mapping, context: Mapping) -> ProviderOutput: ...


# ---------------------------------------------------------------------------------------------- mock
def _param(value, lo, hi, step, typ="integer"):
    return {"type": typ, "value": value, "min": lo, "max": hi, "step": step}


def _template(name: str, tf: str, session: str, direction: str) -> tuple[dict, dict]:
    """(definition, family) for a mock template, fitted to the request scope. Standard, well-known
    constructs; no claim is made about any of them."""
    both = direction == "both"
    if name == "rsi_reversion":
        fam = {"id": "ai_rsi_reversion", "name": "RSI extreme reversion", "category": "mean_reversion"}
        rsi = {"feature": "rsi", "params": {"period": "$rsi_len"}, "output": "rsi"}
        entry = {"direction": direction, "session": session}
        if direction in ("long", "both"):
            entry["long"] = {"left": rsi, "op": "<", "right": "$low_level"}
        if direction in ("short", "both"):
            entry["short"] = {"left": rsi, "op": ">", "right": "$high_level"}
        params = {"rsi_len": _param(14, 5, 30, 1), "low_level": _param(25, 10, 40, 5, "float"),
                  "high_level": _param(75, 60, 90, 5, "float"),
                  "stop_atr": _param(2.0, 1.0, 4.0, 0.5, "float")}
        if not both:
            params.pop("high_level" if direction == "long" else "low_level")
        d = {"parameters": params, "entry": entry,
             "exit": {"stop": {"type": "atr", "multiple": "$stop_atr", "period": 14}, "target": {"type": "none"},
                      "time_stop_bars": 24}}
    elif name == "range_breakout":
        fam = {"id": "ai_atr_band_breakout", "name": "ATR band breakout", "category": "volatility"}
        band = lambda op: {"arith": op, "args": [{"feature": "sma", "params": {"period": "$mean_len"}, "output": "sma"},
                                                 {"arith": "mul", "args": ["$band_k", {"feature": "atr", "params": {"period": 14},
                                                                                       "output": "atr"}]}]}
        entry = {"direction": direction, "session": session}
        if direction in ("long", "both"):
            entry["long"] = {"left": {"bar": "close"}, "op": ">", "right": band("add")}
        if direction in ("short", "both"):
            entry["short"] = {"left": {"bar": "close"}, "op": "<", "right": band("sub")}
        d = {"parameters": {"mean_len": _param(20, 10, 50, 5), "band_k": _param(1.5, 0.5, 3.0, 0.5, "float")},
             "entry": entry,
             "exit": {"stop": {"type": "atr", "multiple": 1.0}, "target": {"type": "atr", "multiple": 2.0},
                      "max_hold_bars": 48}}
    else:                                                        # ema_trend
        fam = {"id": "ai_ema_trend", "name": "EMA trend continuation", "category": "trend"}
        fast = {"feature": "ema", "params": {"period": "$fast"}, "output": "ema"}
        slow = {"feature": "ema", "params": {"period": "$slow"}, "output": "ema"}
        entry = {"direction": direction, "session": session}
        if direction in ("long", "both"):
            entry["long"] = {"left": fast, "op": "crosses_above", "right": slow}
        if direction in ("short", "both"):
            entry["short"] = {"left": fast, "op": "crosses_below", "right": slow}
        d = {"parameters": {"fast": _param(9, 5, 20, 1), "slow": _param(21, 15, 60, 3),
                            "stop_atr": _param(1.5, 0.5, 3.0, 0.25, "float"),
                            "rr": _param(2.0, 1.0, 4.0, 0.5, "float")},
             "entry": entry,
             "exit": {"stop": {"type": "atr", "multiple": "$stop_atr", "period": 14},
                      "target": {"type": "risk_reward", "multiple": "$rr"}}}
    definition = {"dsl_version": 1, "name": fam["id"], "family": {**fam}, "timeframe": tf, **d,
                  "sizing": {"mode": "fixed", "quantity": 1}}
    return definition, fam


KEYWORDS = (("rsi_reversion", r"revert|reversion|oversold|overbought|mean|exhaust|stretch"),
            ("range_breakout", r"breakout|break out|volatility|expansion|range|band|squeeze"))


class MockProvider:
    """Deterministic test provider (no model, no network). Returns, in order and cycling:
    one VALID proposal, one MALFORMED (code instead of DSL data, plus a performance field), one
    CAUSALITY-INVALID (reads a future bar) and one PARAMETER-INVALID (value outside its declared
    domain). The template is the request's template, else chosen from hypothesis keywords, else
    ema_trend - keyword routing, not intelligence."""
    kind = "mock"

    def describe(self) -> dict:
        return {"kind": "mock", "name": "Munyun Lab deterministic mock provider", "model": None,
                "provider_version": "mock-1", "configured": True, "external": False}

    def generate_proposals(self, request: Mapping, context: Mapping) -> ProviderOutput:
        scope = context["scope"]
        tf, session = scope["timeframe"], scope.get("session") or "NY_RTH"
        direction = scope.get("direction") or "both"
        template = request.get("template")
        if not template:
            template = next((t for t, rx in KEYWORDS if re.search(rx, request.get("hypothesis", ""), re.I)), "ema_trend")
        base = context.get("base_strategy")
        if request["mode"] == "modify" and base:
            valid = copy.deepcopy(dict(base))
            valid.pop("description", None)
            valid["name"] = f"{valid.get('name', 'strategy')}_ai_v2"
            valid.setdefault("entry", {})["session"] = session
            changes = [f"entry.session -> {session}"]
            if "time_stop_bars" not in valid.get("exit", {}):
                valid["exit"]["time_stop_bars"] = 36
                changes.append("exit.time_stop_bars -> 36 (added)")
            fam = dict(valid.get("family") or {"id": "ai_modified", "name": "AI modification", "category": "other"})
            fam = {k: fam[k] for k in ("id", "name", "category") if k in fam}
            valid["family"] = {**valid.get("family", {}), **fam}
        else:
            valid, fam = _template(template, tf, session, direction)
            changes = None
        hyp = request.get("hypothesis") or TEMPLATES_TEXT.get(template, "")

        def content(defn, name, rationale, **extra):
            c = {"hypothesis": hyp, "strategy_name": name, "family": fam, "definition": defn,
                 "parameters": {k: v["value"] for k, v in (defn.get("parameters") or {}).items()},
                 "timeframe": defn.get("timeframe"), "instrument": scope["instrument"],
                 "session": (defn.get("entry") or {}).get("session"),
                 "assumptions": ["bars are complete when a signal is evaluated (engine rule)",
                                 "costs are whatever the configured cost profile states"],
                 "rationale": rationale, "constraints": ["uses only DSL-supported constructs"], **extra}
            if changes is not None:
                c["changes"] = list(changes)
            return c

        ok = content(valid, valid["name"], "Expresses the stated hypothesis with standard, causal constructs.")
        malformed = {"hypothesis": hyp, "strategy_name": "malformed_example",
                     "definition": "def strategy(bars):\n    return bars.close > bars.open",
                     "expected_profit_factor": 1.8, "timeframe": tf, "rationale": "(mock) malformed output"}
        causal = copy.deepcopy(valid)
        causal["name"] = f"{valid['name']}_lookahead"
        side = "long" if "long" in causal["entry"] else "short"
        causal["entry"][side] = {"all": [causal["entry"][side],
                                         {"left": {"bar": "close", "lag": -1}, "op": ">", "right": {"bar": "close"}}]}
        param_bad = copy.deepcopy(valid)
        param_bad["name"] = f"{valid['name']}_out_of_domain"
        pname = sorted(param_bad.get("parameters") or {})[0] if param_bad.get("parameters") else None
        if pname:
            p = param_bad["parameters"][pname]
            p["value"] = p["max"] + (p.get("step") or 1) * 4             # outside [min, max]
        else:
            param_bad["exit"]["stop"] = {"type": "atr", "multiple": -1.0}
        cycle = [ok, malformed,
                 content(causal, causal["name"], "(mock) compares the current close with the NEXT bar's close."),
                 content(param_bad, param_bad["name"], "(mock) parameter value outside its declared domain.")]
        out = [copy.deepcopy(cycle[i % 4]) for i in range(request["n_proposals"])]
        raw = stable_json(out)
        return ProviderOutput(out, self.describe(), hashlib.sha256(raw.encode()).hexdigest(),
                              notes=["deterministic mock output: exercises the gate; not an AI"])


TEMPLATES_TEXT = {
    "ema_trend": "Short-term momentum shifts marked by a fast/slow moving-average crossing persist for a few bars.",
    "rsi_reversion": "After a short-term RSI extreme, price tends to revert toward its recent mean.",
    "range_breakout": "A close outside an ATR band around the mean marks a volatility expansion that continues.",
}


# ---------------------------------------------------------------------------------------------- external
SYSTEM_PROMPT = (
    "You propose trading-strategy HYPOTHESES as data for a research engine that will test them. "
    "Return ONLY a JSON array of proposal objects with keys: hypothesis, strategy_name, family {id, name, "
    "category}, definition (a strategy in the given DSL, as JSON), parameters (name -> value, equal to the "
    "definition's parameter values), timeframe, instrument, session, assumptions (list of strings), rationale, "
    "constraints (list of strings), and for modifications changes (list of strings). Never include code, "
    "performance estimates, win rates, returns or any claim that a strategy works: the engine measures that. "
    "Use only the features, constructs, sessions and parameter types listed in the context.")


class AnthropicProvider:
    """External provider over HTTPS (stdlib only). Enabled only by EDGELAB_AI_PROVIDER=anthropic with
    EDGELAB_AI_MODEL and ANTHROPIC_API_KEY set in the environment. Sends the request and the BLIND
    context; returns whatever JSON the model produced, unmodified, for the gate to judge."""
    kind = "anthropic"
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, model: str, api_key: str, timeout: float = 120.0, opener=None):
        if not model:
            raise ProviderError(f"set {MODEL_ENV} to the model to use; no default model is assumed")
        if not api_key:
            raise ProviderError(f"set {KEY_ENV} in the environment (never in a workspace, strategy or run)")
        self.model, self._key, self.timeout = model, api_key, timeout
        self._opener = opener

    def describe(self) -> dict:
        return {"kind": "anthropic", "name": "Anthropic Messages API", "model": self.model,
                "provider_version": "anthropic-messages-2023-06-01", "configured": True, "external": True}

    def generate_proposals(self, request: Mapping, context: Mapping) -> ProviderOutput:
        import urllib.request
        user = json.dumps({"request": {k: request[k] for k in ("mode", "hypothesis", "template", "n_proposals",
                                                               "constraints")},
                           "context": context}, sort_keys=True)
        body = json.dumps({"model": self.model, "max_tokens": 8000, "system": SYSTEM_PROMPT,
                           "messages": [{"role": "user", "content": user}]}).encode()
        req = urllib.request.Request(self.URL, data=body, method="POST", headers={
            "content-type": "application/json", "x-api-key": self._key, "anthropic-version": "2023-06-01"})
        opener = self._opener or urllib.request.urlopen
        try:
            with opener(req, timeout=self.timeout) as r:
                payload = json.loads(r.read())
        except Exception as exc:                                # noqa: BLE001 - reported, key never included
            raise ProviderError(f"AI provider request failed: {type(exc).__name__}") from None
        text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
        digest = hashlib.sha256(text.encode()).hexdigest()
        m = re.search(r"\[.*\]", text, re.S)
        try:
            items = json.loads(m.group(0)) if m else None
        except ValueError:
            items = None
        if not isinstance(items, list):
            # Not repaired: the whole output is recorded as one malformed proposal for the gate to reject.
            items = [{"unparsed_output": text[:4000]}]
        return ProviderOutput(items, self.describe(), digest)


# ---------------------------------------------------------------------------------------------- config
def provider_settings(environ: Mapping[str, str] | None = None, user_settings: Mapping | None = None) -> dict:
    env = os.environ if environ is None else environ
    us = dict((user_settings or {}).get("ai") or {})
    kind = (env.get(PROVIDER_ENV) or us.get("provider") or "none").strip().lower()
    model = (env.get(MODEL_ENV) or us.get("model") or "").strip() or None
    return {"kind": kind, "model": model, "api_key_present": bool(env.get(KEY_ENV))}


def provider_status(environ: Mapping[str, str] | None = None, user_settings: Mapping | None = None) -> dict:
    s = provider_settings(environ, user_settings)
    problem = None
    if s["kind"] not in PROVIDER_KINDS:
        problem = f"{PROVIDER_ENV}={s['kind']!r} is not a known provider ({PROVIDER_KINDS})"
    elif s["kind"] == "anthropic" and not s["model"]:
        problem = f"{MODEL_ENV} is not set"
    elif s["kind"] == "anthropic" and not s["api_key_present"]:
        problem = f"{KEY_ENV} is not set in the environment"
    configured = s["kind"] not in ("none", "mock") and problem is None
    return {"configured_provider": s["kind"], "model": s["model"], "external_configured": configured,
            "api_key_present": s["api_key_present"], "problem": problem, "available": ["mock"] +
            (["anthropic"] if configured and s["kind"] == "anthropic" else []),
            "offline_notice": None if configured else
            "No AI provider is configured - Munyun Lab is offline. The deterministic mock provider is available "
            "to exercise the pipeline; it is not an AI. Configure one with environment variables "
            f"({PROVIDER_ENV}, {MODEL_ENV}, {KEY_ENV}); see WEB_UI.md."}


def get_provider(name: str | None, environ: Mapping[str, str] | None = None,
                 user_settings: Mapping | None = None) -> AIProvider:
    s = provider_settings(environ, user_settings)
    kind = name or ("mock" if s["kind"] in ("none", "mock") else s["kind"])
    if kind == "mock":
        return MockProvider()
    if kind == "anthropic":
        env = os.environ if environ is None else environ
        if s["kind"] != "anthropic":
            raise ProviderError(f"the anthropic provider is not configured ({PROVIDER_ENV}=anthropic)")
        return AnthropicProvider(s["model"] or "", env.get(KEY_ENV, ""))
    raise ProviderError(f"unknown AI provider {kind!r} (available: mock"
                        f"{', anthropic' if s['kind'] == 'anthropic' else ''})")
