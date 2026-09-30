"""Locked research protocol, holdout ledger and program-level trial ledger (ADR-56).

A research protocol is fixed BEFORE discovery starts and governs every numerical evaluation of its
instrument/provider through ``Services`` (the only interface the CLI and web API use):

  * windows by TRADING DATE on the dataset's session calendar: a discovery window and a locked
    holdout, stored both as trading-date bounds and as exact resolved bar timestamps;
  * the canonical execution (cost model incl. scenario/spread_source/quote model, backtest config
    and the research config hash) - an evaluation under a different config is refused;
  * a trial budget (unique numerical trials), a holdout-look budget, acceptance criteria and the
    multiple-testing rule, all fixed in advance;
  * ``pre_protocol_exposure``: evaluations made before activation (identities only, no metrics).

Identity. ``protocol_id = RP_ + hash(material)``: changing ANY material field (windows, dataset,
costs, execution, budgets, criteria, exposure) is a different protocol. Records are insert-only;
only the lifecycle status may move ACTIVE -> RETIRED. At most one ACTIVE protocol per
instrument/provider.

Stages. An evaluation window is classified by the bars it actually uses:
  discovery  - every bar inside the discovery window        -> allowed, attributed to the trial ledger
  holdout    - exactly the resolved holdout bars             -> only through a granted holdout access
  overlap    - anything else touching the holdout / outside  -> refused (HOLDOUT_LOCKED)

Trials. One unique numerical trial = (protocol, logic_hash, content hash of the EVALUATED bars
[the restricted slice encodes dataset and window], config hash). Every evaluation event is stored;
only the first completed event of a key is ``counted``. Proposal attempts (AI generations, Mode B
batches) are a separate ledger: a duplicate definition is a new attempt but never a new trial.

Refusals are ``ProtocolRefusal`` (a ValueError) with a machine-readable ``code``.
Nothing here reads results into AI context; the AI layer only sees the protocol id and the
discovery window (edgelab/ai/context.py FORBIDDEN_KEYS still applies).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from statistics import NormalDist
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from edgelab.core.identity import hash_obj

PROTOCOL_VERSION = 1
STATUSES = ("ACTIVE", "RETIRED")
ENTRY_POINTS = ("backtest_strategy", "search_cell", "internal_validation", "random_control_candidate",
                "holdout_evaluation")
SAMPLE_ORDER = ("LOW SAMPLE SIZE", "MODERATE SAMPLE", "ADEQUATE SAMPLE")

DEFAULT_TRIAL_BUDGET = 2000
DEFAULT_HOLDOUT_LOOKS = 10
DEFAULT_ACCEPTANCE = {
    "min_oos_sample_label": "ADEQUATE SAMPLE",
    "min_oos_expectancy_r_exclusive": 0.0,
    "oos_confidence": {
        "statistic": "one-sided lower confidence bound of OOS expectancy_r",
        "method": "normal approximation mean - z * se (the metrics layer's expectancy_se), z = "
                  "inverse normal of (1 - per-test alpha) from the multiple_testing rule",
        "must_exceed": 0.0},
    "min_profit_factor_exclusive": 1.0,
    "random_control": {
        "method": "matched random-entry control (research/controls.py) on the holdout window",
        "n_controls": 100, "seed": 0, "statistic": "expectancy_r", "percentile": 95,
        "rule": "candidate OOS expectancy_r must exceed the 95th percentile of the control realizations "
                "(unadjusted for multiple testing; the trial-count adjustment is carried by oos_confidence)"},
    "cost_stress": {"multipliers": [1.5, 2.0], "rule": "OOS net_r >= 0 at each multiplier of the modelled "
                                                       "transaction costs (analytics.metrics.cost_sensitivity)"},
    "in_sample_rank_sufficient": False,
    "single_oos_pass_sufficient": False,
    "outcome_if_all_met": "HOLDOUT_CRITERIA_MET - eligible for further validation (paper trading, not yet "
                          "implemented); NOT an accepted strategy",
}
DEFAULT_MULTIPLE_TESTING = {
    "method": "Bonferroni-familywise-alpha",
    "familywise_alpha": 0.05,
    "family_size": "counted unique numerical trials of this protocol at holdout-evaluation time (minimum 1)",
    "per_test_alpha": "familywise_alpha / family_size",
    "applies_to": "acceptance_criteria.oos_confidence",
}


class ProtocolRefusal(ValueError):
    """A machine-readable refusal of the research protocol (``code`` + details)."""

    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(f"[{code}] {message}")
        self.code, self.message, self.detail = code, message, detail

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.detail}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(ns: int) -> str:
    return pd.Timestamp(int(ns), tz="UTC").isoformat()


def _date(x: Any) -> pd.Timestamp:
    return pd.Timestamp(str(x)[:10])


# ============================================================ windows
def resolve_windows(ds, discovery: Sequence, holdout: Sequence) -> dict:
    """Trading-date windows -> exact bars of ``ds`` (a ValidatedDataset). Refuses gaps, overlaps,
    windows without bars and a holdout that does not run to the dataset's last trading date."""
    cal, ts = ds.calendar, ds.bars.ts
    td = pd.DatetimeIndex(cal.trading_dates(ts))
    (d0, d1), (h0, h1) = (_date(discovery[0]), _date(discovery[1])), (_date(holdout[0]), _date(holdout[1]))
    if not (d0 <= d1 < h0 <= h1):
        raise ProtocolRefusal("PROTOCOL_WINDOWS_INVALID", "need discovery start <= end < holdout start <= end",
                              discovery=[str(d0.date()), str(d1.date())], holdout=[str(h0.date()), str(h1.date())])
    out = {}
    for name, (a, b) in (("discovery", (d0, d1)), ("holdout", (h0, h1))):
        idx = np.flatnonzero((td >= a) & (td <= b))
        if not len(idx):
            raise ProtocolRefusal("PROTOCOL_WINDOWS_INVALID", f"{name} window has no bars in {ds.manifest.dataset_id}")
        first_open = cal.session_bounds(a.date())[0].tz_convert("UTC").value
        out[name] = {"trading_dates": [str(a.date()), str(b.date())],
                     "first_bar": _iso(ts.asi8[idx[0]]), "last_bar": _iso(ts.asi8[idx[-1]]),
                     "first_bar_ns": int(ts.asi8[idx[0]]), "last_bar_ns": int(ts.asi8[idx[-1]]),
                     "n_bars": int(len(idx)), "boundary_open": _iso(first_open), "boundary_open_ns": int(first_open)}
    between = np.flatnonzero((td > d1) & (td < h0))
    if len(between):
        raise ProtocolRefusal("PROTOCOL_WINDOWS_INVALID", "bars fall between the discovery and holdout windows",
                              n_bars=int(len(between)))
    if out["holdout"]["last_bar_ns"] != int(ts.asi8[-1]):
        raise ProtocolRefusal("PROTOCOL_WINDOWS_INVALID", "the holdout must run to the dataset's last bar",
                              holdout_last_bar=out["holdout"]["last_bar"], dataset_last_bar=_iso(ts.asi8[-1]))
    before = np.flatnonzero(td < d0)
    out["bars_before_discovery"] = int(len(before))
    return out


def stage_of(protocol: Mapping, first_ns: int, last_ns: int) -> str:
    """discovery | holdout | overlap for an evaluation whose bars run from first_ns to last_ns."""
    w = protocol["material"]["windows"]
    d, h = w["discovery"], w["holdout"]
    if first_ns >= d["boundary_open_ns"] and last_ns < h["boundary_open_ns"]:
        return "discovery"
    if first_ns == h["first_bar_ns"] and last_ns == h["last_bar_ns"]:
        return "holdout"
    return "overlap"


def interval_stage(protocol: Mapping, start: Any, end: Any) -> str:
    """Stage of a requested [start, end] (bar-open instants) before any bar is loaded: discovery if
    it ends before the holdout boundary, else overlap (the exact holdout is only reachable through
    a granted access, never through a plan)."""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    s = s.tz_localize("UTC") if s.tz is None else s.tz_convert("UTC")
    e = e.tz_localize("UTC") if e.tz is None else e.tz_convert("UTC")
    w = protocol["material"]["windows"]
    return "discovery" if e.value < w["holdout"]["boundary_open_ns"] else "overlap"


# ============================================================ protocol record
def build_material(*, dataset_manifest, windows: Mapping, cost_model: Mapping, backtest_config_hash: str,
                   config_hash: str, pre_protocol_exposure: Sequence[Mapping], exposure_statement: str,
                   name: str = "", trial_budget: int = DEFAULT_TRIAL_BUDGET,
                   holdout_looks: int = DEFAULT_HOLDOUT_LOOKS, acceptance: Mapping | None = None,
                   multiple_testing: Mapping | None = None, search_constraints: Mapping | None = None) -> dict:
    m = dataset_manifest
    for k, v in (("trial_budget", trial_budget), ("holdout_looks", holdout_looks)):
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ProtocolRefusal("PROTOCOL_INVALID", f"{k} must be a positive integer", value=v)
    return {
        "protocol_version": PROTOCOL_VERSION,
        "name": name,
        "scope": {"instrument": m.instrument, "provider": m.provider, "timeframe": m.timeframe},
        "source_dataset": {"dataset_id": m.dataset_id, "content_hash": m.content_hash, "calendar": m.calendar,
                           "calendar_fingerprint": m.calendar_fingerprint, "has_ask_ohlc": bool(m.has_ask_ohlc),
                           "quality_status": m.quality_status},
        "windows": dict(windows),
        "execution": {"cost_model": dict(cost_model), "cost_scenario": cost_model.get("scenario"),
                      "spread_source": cost_model.get("spread_source"),
                      "quote_model": "directional_bid_ask" if cost_model.get("spread_source") == "quotes"
                      else "single_series",
                      "backtest_config_hash": backtest_config_hash},
        "config_hash": config_hash,
        "search_constraints": dict(search_constraints or {
            "evaluation_windows": "every discovery evaluation must lie entirely inside the discovery window",
            "datasets": "any dataset of this instrument/provider is governed; HistData and other sources are "
                        "separate studies and are never pooled with this protocol",
            "stages": {"discovery": "exploration and internal validation (evaluate_oos / walk_forward bounded "
                                    "inside the discovery window)",
                       "holdout": "only Services.evaluate_holdout for a frozen, shortlisted candidate"}}),
        "trial_budget": {"max_unique_trials": trial_budget,
                         "unit": "unique numerical trial = (protocol, logic_hash, content hash of the evaluated "
                                 "bars [dataset + window], config hash); duplicates and failures are recorded "
                                 "but not counted"},
        "holdout_budget": {"max_unique_candidate_evaluations": holdout_looks, "per_candidate": 1},
        "acceptance_criteria": dict(acceptance or DEFAULT_ACCEPTANCE),
        "multiple_testing": dict(multiple_testing or DEFAULT_MULTIPLE_TESTING),
        "pre_protocol_exposure": {"statement": exposure_statement, "runs": list(pre_protocol_exposure)},
    }


def make_record(material: Mapping, created_with: Mapping) -> dict:
    return {"object": "edgelab.research_protocol", "protocol_id": protocol_id(material),
            "material_hash": hash_obj(material), "material": dict(material), "created_at": _now(),
            "created_with": dict(created_with), "status": "ACTIVE"}


def protocol_id(material: Mapping) -> str:
    return "RP_" + hash_obj(material, 12).upper()


def verify_record(rec: Mapping) -> None:
    if protocol_id(rec["material"]) != rec["protocol_id"] or hash_obj(rec["material"]) != rec["material_hash"]:
        raise ProtocolRefusal("PROTOCOL_TAMPERED", "stored protocol material does not match its identity",
                              protocol_id=rec.get("protocol_id"))


# ============================================================ trials
def trial_key(pid: str, logic_hash: str, eval_content_hash: str, config_hash: str) -> str:
    return hash_obj({"protocol_id": pid, "logic_hash": logic_hash, "evaluated_content_hash": eval_content_hash,
                     "config_hash": config_hash})


# ============================================================ multiple testing + holdout assessment
def bonferroni(multiple_testing: Mapping, family_size: int) -> dict:
    """Per-test alpha and the one-sided z for the protocol's Bonferroni rule and a family size."""
    if multiple_testing.get("method") != "Bonferroni-familywise-alpha":
        raise ProtocolRefusal("PROTOCOL_MT_UNSUPPORTED", "unsupported multiple-testing method",
                              method=multiple_testing.get("method"))
    m = max(1, int(family_size))
    alpha = float(multiple_testing["familywise_alpha"]) / m
    return {"method": "Bonferroni-familywise-alpha", "familywise_alpha": float(multiple_testing["familywise_alpha"]),
            "family_size": m, "per_test_alpha": alpha, "z_one_sided": NormalDist().inv_cdf(1.0 - alpha)}


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if not math.isnan(v) else None


def assess_holdout(material: Mapping, metrics: Mapping, cost_rows: Sequence[Mapping],
                   control_expectancies: Sequence[float], family_size: int) -> dict:
    """Apply the protocol's pre-registered criteria to one holdout evaluation. Every criterion is
    reported (pass / fail / unavailable); unavailable counts as not met. Never 'accepted'."""
    ac, mt = material["acceptance_criteria"], material["multiple_testing"]
    adj = bonferroni(mt, family_size)
    crit = {}
    lab = metrics.get("sample_label")
    need = ac["min_oos_sample_label"]
    crit["sample"] = {"value": lab, "required": need,
                      "met": lab in SAMPLE_ORDER and SAMPLE_ORDER.index(lab) >= SAMPLE_ORDER.index(need)}
    e, se, n = _num(metrics.get("expectancy_r")), _num(metrics.get("expectancy_se")), metrics.get("trade_count") or 0
    crit["expectancy"] = {"value": e, "required": f"> {ac['min_oos_expectancy_r_exclusive']}",
                          "met": e is not None and e > ac["min_oos_expectancy_r_exclusive"]}
    lb = e - adj["z_one_sided"] * se if (e is not None and se is not None and n > 1) else None
    crit["adjusted_confidence"] = {"lower_bound": lb, "z": adj["z_one_sided"], "per_test_alpha": adj["per_test_alpha"],
                                   "family_size": adj["family_size"],
                                   "required": f"> {ac['oos_confidence']['must_exceed']}",
                                   "met": lb is not None and lb > ac["oos_confidence"]["must_exceed"]}
    pf = metrics.get("profit_factor")
    pf_v = math.inf if (pf is None and (metrics.get("loss_rate") == 0 and (metrics.get("net_r") or 0) > 0)) else _num(pf)
    crit["profit_factor"] = {"value": pf_v, "required": f"> {ac['min_profit_factor_exclusive']}",
                             "met": pf_v is not None and pf_v > ac["min_profit_factor_exclusive"]}
    rc = ac["random_control"]
    fin = np.array([x for x in control_expectancies if x is not None and np.isfinite(x)], float)
    thr = float(np.percentile(fin, rc["percentile"])) if len(fin) else None
    crit["random_control"] = {"threshold": thr, "percentile": rc["percentile"], "n_finite_controls": int(len(fin)),
                              "candidate": e, "met": thr is not None and e is not None and e > thr}
    stress = {float(r["cost_multiplier"]): _num(r["net_r"]) for r in cost_rows}
    need_m = [float(x) for x in ac["cost_stress"]["multipliers"]]
    crit["cost_stress"] = {"net_r": {str(k): stress.get(k) for k in need_m},
                           "met": all(stress.get(k) is not None and stress[k] >= 0 for k in need_m)}
    all_met = all(c["met"] for c in crit.values())
    return {"criteria": crit, "multiple_testing": adj,
            "outcome": "HOLDOUT_CRITERIA_MET" if all_met else "HOLDOUT_CRITERIA_NOT_MET",
            "accepted": False,
            "note": ("a single out-of-sample pass is never acceptance: " + ac["outcome_if_all_met"]) if all_met else
                    "at least one pre-registered criterion is not met (unavailable counts as not met)"}
