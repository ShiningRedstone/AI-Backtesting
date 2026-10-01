"""Matched random-entry control for a DSL candidate (a CONTROL, not a strategy).

Question it addresses: are the candidate's results materially different from entering at random
moments that the candidate itself could have traded, with everything else held equal?

What is held fixed (the candidate's own compiled definition supplies all of it): dataset and time
period, timeframe, entry session / weekday rules, order type and entry reference, stop and target
construction (e.g. 1.5 x ATR(14), 2R), cooldown, signal exits, time stops, sizing, the cost profile,
and the engine (fills, one position at a time, session flattening, causality check).

What is randomized: only WHETHER a bar is an entry and, when the candidate trades both ways, the
direction. Eligible bars are those the candidate could have entered at its bar close: inside its
entry session/weekdays, with an executable reference, stop and target for the drawn direction
(``DSLStrategy._entry_allowed`` / ``_orders``, the candidate's own code, all causal at bar t).

Signal pipeline, the same stages on both sides (compiler helpers):
  candidate: _entry_directions (trigger) -> _orders (valid levels) -> _apply_cooldown -> signals
  control:   eligible bar + random fire   -> _orders (valid levels) -> _apply_cooldown -> signals
Calibration is at the PRE-COOLDOWN stage on both sides (pre-entry quantities only, never outcomes):
  * signal rate: p = S_pre / E, S_pre = the candidate's valid entries BEFORE cooldown on the same
    data, E = the realization's eligible-bar count. Each eligible bar fires independently with
    probability p, so the expected number of pre-cooldown control fires equals S_pre.
  * direction mix: P(long) = the long share of those pre-cooldown candidate entries.
  The SAME cooldown is then applied to the control's fires, and the engine applies the SAME
  busy/one-position rules; post-cooldown signal counts and trade counts are reported, not forced.
  They can differ from the candidate's because cooldown removes more of a clustered candidate's
  signals than of uniformly spread random ones - that difference is a property of the candidate's
  timing, which is part of what the control compares.
  S_pre, E and the long share are fixed per realization before the backtest (design constants
  computed from the whole period, carrying no outcome information), so a bar's decision uses only
  its own uniform draw and its causal eligibility: the per-bar stream
  ``default_rng(seed).random((n, 2))`` is prefix-stable, so the engine's truncation (causality)
  check passes. Results are a CONDITIONAL null: conditioned on the candidate's pre-cooldown entry
  frequency and direction mix, not on trade outcomes.

It does NOT test: the exit rules (identical on both sides), the choice of stop/target/sizing,
cooldown or costs. Random fires are spread uniformly over eligible bars, so the candidate's
clustering in time is part of what differs, not something the control reproduces. The
"fraction of controls exceeding the candidate" is a descriptive rank within this conditional null,
not a p-value and not a probability of future profitability.
"""
from __future__ import annotations

from typing import Mapping

import numpy as np

from edgelab.core.identity import hash_obj
from edgelab.data.schema import BarArrays
from edgelab.engine.signals import SignalSet
from edgelab.features.engine import FeatureFrame
from edgelab.strategy.compiler import DSLStrategy, _Eval

CONTROL_METHOD = "random_entry_conditional_v2"      # v2: calibrated at the pre-cooldown stage


def candidate_opportunities(bound: DSLStrategy, bars: BarArrays) -> tuple[int, int]:
    """(valid entries before cooldown, of which long) for a bound candidate, via its own
    trigger and order-validation stages."""
    f = bound._context.engine_for(bars).frame(bound.feature_specs())
    ev = _Eval(bars, f)
    d = bound._entry_directions(bars, f, ev, {})
    ok = bound._orders(bars, ev, d, {})[3]
    return int(ok.sum()), int((ok & (d == 1)).sum())


class RandomEntryControl(DSLStrategy):
    """One seeded realization of the matched random-entry control for a compiled candidate."""

    def __init__(self, compiled, seed: int, signal_rate: float | None = None, p_long: float = 0.5):
        super().__init__(compiled)
        self.family = "random_entry_control"
        self.control = {"method": CONTROL_METHOD, "candidate_strategy_id": compiled.identity.strategy_id,
                        "seed": int(seed), "signal_rate": signal_rate, "p_long": float(p_long)}

    @property
    def strategy_id(self) -> str:
        """Never the candidate's id: a control result cannot be mistaken for the candidate."""
        return "CTRL_" + hash_obj(self.control)[:12].upper()

    @property
    def spec(self) -> dict:
        s = super().spec
        s["control"] = dict(self.control)
        return s

    def set_design(self, signal_rate: float, p_long: float) -> "RandomEntryControl":
        if not 0.0 <= signal_rate <= 1.0:
            raise ValueError(f"signal rate {signal_rate} outside [0, 1]")
        self.control = {**self.control, "signal_rate": float(signal_rate), "p_long": float(p_long)}
        return self

    def _draws(self, n: int) -> tuple[np.ndarray, np.ndarray]:
        u = np.random.default_rng(self.control["seed"]).random((n, 2))      # prefix-stable per bar
        d = np.where(u[:, 1] < self.control["p_long"], 1, -1).astype(np.int8)
        return u[:, 0], d

    def eligible(self, bars: BarArrays, f: FeatureFrame) -> np.ndarray:
        """Bars at which this realization's drawn direction is an executable candidate entry."""
        n = len(bars)
        _, d = self._draws(n)
        allowed = self._entry_allowed(f, n)
        return self._orders(bars, _Eval(bars, f), np.where(allowed, d, 0).astype(np.int8), {})[3]

    def eligible_count(self, bars: BarArrays) -> int:
        frame = self._context.engine_for(bars).frame(self.feature_specs())
        return int(self.eligible(bars, frame).sum())

    def _fires(self, bars: BarArrays, f: FeatureFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if self.control["signal_rate"] is None:
            raise RuntimeError("random-entry control used before set_design()")
        u, d = self._draws(len(bars))
        eligible = self.eligible(bars, f)
        return eligible & (u < self.control["signal_rate"]), d, eligible

    def pre_cooldown_fires(self, bars: BarArrays) -> int:
        """Random fires before cooldown (the stage calibrated against the candidate)."""
        return int(self._fires(bars, self._context.engine_for(bars).frame(self.feature_specs()))[0].sum())

    def signals_from_features(self, bars: BarArrays, f: FeatureFrame) -> SignalSet:
        fire, d, eligible = self._fires(bars, f)
        diag = {"bars": len(bars), "control": dict(self.control), "eligible": int(eligible.sum()),
                "pre_cooldown_fires": int(fire.sum())}
        return self._emit(bars, _Eval(bars, f), np.where(fire, d, 0).astype(np.int8), diag)


def realization_seeds(seed: int, n: int) -> list[int]:
    """Deterministic, well-separated per-realization seeds from one base seed."""
    return [int(s.generate_state(1)[0]) for s in np.random.SeedSequence(int(seed)).spawn(n)]


def summarize(candidate: Mapping, controls: list[Mapping], percentiles=(5, 25, 50, 75, 95)) -> dict:
    """Candidate metrics vs the distribution of control-realization metrics (descriptive only)."""
    def arr(k):
        return np.array([c.get(k, np.nan) if c.get(k) is not None else np.nan for c in controls], float)

    out: dict = {"realizations": len(controls)}
    for k in ("trade_count", "net_r", "gross_r", "expectancy_r", "profit_factor", "max_drawdown_r"):
        x = arr(k)
        fin = x[np.isfinite(x)]
        cand = candidate.get(k)
        row = {"candidate": cand, "finite_realizations": int(len(fin)),
               "mean": float(fin.mean()) if len(fin) else None,
               "median": float(np.median(fin)) if len(fin) else None,
               "percentiles": dict(zip(percentiles, np.percentile(fin, percentiles).tolist())) if len(fin) else {}}
        if cand is not None and np.isfinite(cand) and len(fin):
            row["fraction_of_controls_exceeding_candidate"] = float((fin > cand).mean())
        out[k] = row
    out["note"] = ("fraction_of_controls_exceeding_candidate = share of finite control realizations "
                   "with a strictly larger value; a descriptive rank within the conditional null, "
                   "not a p-value. For max_drawdown_r larger is worse.")
    return out


# ------------------------------------------------------------------------------ stored control records (ADR-73)
CONTROLS_DIR = "controls"


def controls_dir(data_root) -> "Path":
    from pathlib import Path
    return Path(data_root) / CONTROLS_DIR


def save_control_record(data_root, result: Mapping) -> str:
    """Keep a finished random-entry control as a CONTROL record (``<data>/controls/<validation id>.json``) so it can be
    shown next to strategies. It is never a run record, a strategy or a trial, and nothing reads it for research
    decisions; re-running the same control (same validation id) replaces the file with identical content."""
    import json
    from datetime import datetime, timezone
    vid = str(result["validation_id"])
    d = controls_dir(data_root)
    d.mkdir(parents=True, exist_ok=True)
    body = {"record": "random_entry_control", "validation_id": vid,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            **{k: result.get(k) for k in ("candidate", "dataset", "cost_profile", "cost_status", "sample_status",
                                          "control_config", "labels", "realizations")},
            "note": "control results: entry timing randomized; not strategies, not trials, never ranked as strategies"}
    tmp = d / f".{vid}.tmp"
    tmp.write_text(json.dumps(body, default=str), encoding="utf-8")
    tmp.replace(d / f"{vid}.json")
    return f"{vid}.json"


def load_control_records(data_root) -> list[dict]:
    import json
    d = controls_dir(data_root)
    out = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if rec.get("record") == "random_entry_control":
                out.append(rec)
    return out
