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

Acceptance statistics (protocol_version 2, ADR-57). The OOS confidence criterion uses
``LB = min(normal bound, bootstrap-t bound) = mean - max(z, q*) * se`` at the Bonferroni per-test alpha,
with a pre-registered replicate count and a seed derived from (protocol, candidate logic) - see
``robust_lower_bound``. The random-entry control is an exact Monte-Carlo p-value robustness filter,
NOT a familywise test. Version-1 records keep their own (normal / percentile) rules when assessed.

Declared multiplicity family (protocol_version 3, ADR-67). The Bonferroni family is the DECLARED
``trial_budget.max_unique_trials`` (fixed before discovery, e.g. the 10,000-strategy universe), not the number of trials counted
so far: per-test alpha = familywise_alpha / declared budget at every holdout look, so an early look is never easier than a
late one. The bootstrap-t replicate count is derived from that family (>= 25 replicates beyond the tail). Version-2 records
keep their counted-family rule. A protocol that has not been used (zero counted trials, zero holdout looks) can be SUPERSEDED
by a new protocol (new identity, ``supersedes`` recorded; the old record is retired, never edited).

Flip companion (ADR-88). One ACTIVE research protocol may get ONE flip companion (role ``flip_companion``, stored under
the scope key ``<instrument>@<provider>#flip``, so the one-ACTIVE-per-scope rule above is unchanged): the parent's windows,
data, execution and config, a pre-registered ``mirror_set`` of fully mirrored strategies (its trial budget), its own
holdout looks, and a Bonferroni family of the parent's declared budget plus the flips. A registered flip is governed by
the companion wherever it is evaluated, never by the parent (``Services._governing_protocol(..., logic_hash=)``).

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

PROTOCOL_VERSION = 3
STATUSES = ("ACTIVE", "RETIRED")
ENTRY_POINTS = ("backtest_strategy", "search_cell", "internal_validation", "random_control_candidate",
                "holdout_evaluation")
SAMPLE_ORDER = ("LOW SAMPLE SIZE", "MODERATE SAMPLE", "ADEQUATE SAMPLE")

DEFAULT_TRIAL_BUDGET = 10_000       # the declared 10,000-strategy research universe (ADR-60, ADR-67)
DEFAULT_HOLDOUT_LOOKS = 10          # independent of the trial budget
FAMILYWISE_ALPHA = 0.05
MIN_TAIL_REPLICATES = 25
BOOTSTRAP_REPLICATES = 1_000_000     # 25 / (0.05 / 2000): the version-2 default (2,000-trial family)
BOOTSTRAP_MIN_TRADES = 30
BOOTSTRAP_CHUNK = 2000
DEFAULT_ACCEPTANCE = {
    "min_oos_sample_label": "ADEQUATE SAMPLE",
    "min_oos_expectancy_r_exclusive": 0.0,
    "oos_confidence": {
        "method_id": "min_normal_bootstrap_t_v1",
        "statistic": "one-sided lower confidence bound of OOS expectancy_r (per-trade net R)",
        "definition": "LB = min(mean - z*se, mean - q_boot*se) = mean - max(z, q_boot)*se, se = std(ddof=1)/sqrt(n); "
                      "z = inverse standard normal of (1 - per_test_alpha); q_boot = the ceil((1 - per_test_alpha)*B)-th "
                      "smallest of t*_b = (mean*_b - mean) / (std*_b(ddof=1)/sqrt(n)), b = 1..B",
        "bootstrap": {"method": "studentized bootstrap (bootstrap-t), trades resampled with replacement",
                      "resampling_unit": "one trade (i.i.d. assumption; serial dependence is NOT modelled)",
                      "replicates": BOOTSTRAP_REPLICATES, "chunk": BOOTSTRAP_CHUNK,
                      "rng": "numpy.random.default_rng(seed) (PCG64), integers(0, n, (chunk, n)) per chunk",
                      "seed": "int(first 16 hex of hash_obj({protocol_id, logic_hash, purpose: "
                              "'oos_confidence_bootstrap_t_v1'}), 16) - derived, never chosen",
                      "zero_resample_se": "t* = +inf if mean* > mean, -inf if mean* < mean, 0 if equal (conservative)",
                      "min_trades": BOOTSTRAP_MIN_TRADES},
        "degenerate": "n < min_trades, se == 0 or any non-finite value -> unavailable -> criterion NOT met",
        "combination": "the minimum of both bounds: valid whenever either approximation is valid; never easier than "
                       "the normal bound alone",
        "must_exceed": 0.0},
    "min_profit_factor_exclusive": 1.0,
    "random_control": {
        "rule_id": "monte_carlo_pvalue_v1",
        "method": "matched random-entry control (research/controls.py) on the holdout window",
        "n_controls": 100, "seed": 0, "statistic": "expectancy_r",
        "p_value": "(1 + #{controls with expectancy_r >= candidate or non-finite}) / (n_controls + 1)",
        "max_p_value": 0.05,
        "role": "robustness filter against a conditional null (entry timing/direction randomized among the "
                "candidate's own eligible bars; same exits, costs, sizing). NOT a familywise significance test and "
                "NOT a multiple-testing correction: the smallest attainable p is 1/101; the trial-count adjustment "
                "is carried by oos_confidence only"},
    "cost_stress": {"multipliers": [1.5, 2.0], "rule": "OOS net_r >= 0 at each multiplier of the modelled "
                                                       "transaction costs (analytics.metrics.cost_sensitivity)"},
    "in_sample_rank_sufficient": False,
    "single_oos_pass_sufficient": False,
    "outcome_if_all_met": "HOLDOUT_CRITERIA_MET - eligible for further validation (paper trading, not yet "
                          "implemented); NOT an accepted strategy",
}
DEFAULT_MULTIPLE_TESTING = {
    "method": "Bonferroni-familywise-alpha",
    "familywise_alpha": FAMILYWISE_ALPHA,
    "family_size_rule": "declared_max_unique_trials",
    "family_size": "the DECLARED trial_budget.max_unique_trials of this protocol (fixed before discovery; never smaller "
                   "than the counted unique trials, which the budget caps)",
    "per_test_alpha": "familywise_alpha / family_size",
    "applies_to": "acceptance_criteria.oos_confidence",
}


def bootstrap_replicates_for(trial_budget: int, familywise_alpha: float = FAMILYWISE_ALPHA) -> int:
    """Replicates so that >= MIN_TAIL_REPLICATES bootstrap statistics lie beyond the per-test alpha of the declared family."""
    return max(BOOTSTRAP_REPLICATES, math.ceil(MIN_TAIL_REPLICATES * int(trial_budget) / float(familywise_alpha)))


def family_size(material: Mapping, counted_trials: int) -> int:
    """The Bonferroni family of a protocol: the declared budget (version 3) or the counted trials (version <= 2). A flip
    protocol (ADR-88) declares its parent's budget PLUS its own: its strategies were chosen from the parent's results."""
    mt = material["multiple_testing"]
    if mt.get("family_size_rule") == "declared_max_unique_trials":
        return max(int(material["trial_budget"]["max_unique_trials"]), int(counted_trials), 1)
    if mt.get("family_size_rule") == FLIP_FAMILY_RULE:
        return max(int(material["parent"]["trial_budget"]) + int(material["trial_budget"]["max_unique_trials"]),
                   int(counted_trials), 1)
    return max(1, int(counted_trials))


# ============================================================ flip (mirror) companion protocol (ADR-88)
FLIP_ROLE = "flip_companion"
FLIP_SCOPE_SUFFIX = "#flip"
FLIP_FAMILY_RULE = "declared_parent_plus_own"


def is_flip(rec: Mapping) -> bool:
    """True for a flip companion protocol record (or its material)."""
    mat = rec.get("material", rec)
    return mat.get("role") == FLIP_ROLE


# ADR-93: the separate protocol of the hand-built "My strategy" (its own trials and holdout look; same data, windows,
# execution and config as its parent). Like a flip protocol it is a COMPANION: never the governing research protocol.
MY_STRATEGY_ROLE = "my_strategy"
MY_STRATEGY_SCOPE_SUFFIX = "#my_strategy"
# ADR-97 / ADR-100: the first Strategy autotuner (10,000 fixed combinations) and its flipped reruns. Their code was removed
# in ADR-101; the roles stay so their recorded protocols and tries remain companions (never a governing protocol).
AUTOTUNE_ROLE = "my_autotune"
AUTOTUNE_SCOPE_SUFFIX = "#my_autotune"
AUTOTUNE_FLIP_ROLE = "my_autotune_flip"
AUTOTUNE_FLIP_SCOPE_SUFFIX = "#my_autotune_flip"
OPTIMIZER_ROLE = "my_optimizer"               # ADR-101: the Strategy autotuner's step-by-step optimiser (own budget)
OPTIMIZER_SCOPE_SUFFIX = "#my_optimizer"
MY_HOLDOUT_ROLE = "my_holdout"                # ADR-102: My strategy holdout allowance (1 automatic + 1 manual look)
MY_HOLDOUT_SCOPE_SUFFIX = "#my_holdout"
MARKET_SIM_ROLE = "market_sim"                # ADR-107: the Market simulator's ONE holdout prediction look (no trials)
MARKET_SIM_SCOPE_SUFFIX = "#market_sim"
MARKET_DIRECTION_ROLE = "market_sim_direction"   # ADR-109: the second Market simulator look (direction calls only)
MARKET_DIRECTION_SCOPE_SUFFIX = "#market_sim_direction"


def is_companion(rec: Mapping) -> bool:
    """True for a companion protocol (flip, My strategy, autotuner): it never governs library strategies or campaigns."""
    mat = rec.get("material", rec)
    return mat.get("role") in (FLIP_ROLE, MY_STRATEGY_ROLE, AUTOTUNE_ROLE, AUTOTUNE_FLIP_ROLE, OPTIMIZER_ROLE,
                               MY_HOLDOUT_ROLE, MARKET_SIM_ROLE, MARKET_DIRECTION_ROLE)


def build_flip_material(parent: Mapping, *, mirror_set: Sequence[Mapping], selection: Mapping,
                        datasets_by_timeframe: Mapping, discovery_period: Mapping, parent_looks_used: int,
                        holdout_looks: int = DEFAULT_HOLDOUT_LOOKS, name: str = "") -> dict:
    """The material of the ONE flip companion of an ACTIVE parent protocol: the parent's data, windows, execution and
    config, its own pre-registered set of flipped strategies (the trial budget is exactly that set), its own holdout
    looks, and a Bonferroni family of the parent's declared budget plus the flips (the flips were selected from the
    parent's discovery results, so the honest family includes them)."""
    import copy as _copy
    pm = parent["material"]
    if not isinstance(holdout_looks, int) or isinstance(holdout_looks, bool) or holdout_looks < 1:
        raise ProtocolRefusal("PROTOCOL_INVALID", "holdout_looks must be a positive integer", value=holdout_looks)
    if not mirror_set:
        raise ProtocolRefusal("FLIP_EMPTY", "no flipped strategy to register")
    own, parent_budget = len(mirror_set), int(pm["trial_budget"]["max_unique_trials"])
    family = parent_budget + own
    mt = {**DEFAULT_MULTIPLE_TESTING, "familywise_alpha": float(pm["multiple_testing"]["familywise_alpha"]),
          "family_size_rule": FLIP_FAMILY_RULE,
          "family_size": f"the parent protocol's DECLARED trial budget ({parent_budget}) plus this protocol's registered "
                         f"flipped strategies ({own}) = {family}: the flips were chosen from the parent's discovery "
                         "results, so every parent trial counts in their family; each protocol controls its own "
                         "familywise error"}
    acceptance = _copy.deepcopy(pm["acceptance_criteria"])
    oc = acceptance.get("oos_confidence") or {}
    if oc.get("method_id") == "min_normal_bootstrap_t_v1":
        oc["bootstrap"]["replicates"] = bootstrap_replicates_for(family, mt["familywise_alpha"])
    return {
        "protocol_version": PROTOCOL_VERSION,
        "role": FLIP_ROLE,
        "name": name,
        "parent": {"protocol_id": parent["protocol_id"], "material_hash": parent["material_hash"],
                   "trial_budget": parent_budget, "holdout_looks_used_at_creation": int(parent_looks_used)},
        "scope": dict(pm["scope"]), "source_dataset": dict(pm["source_dataset"]), "windows": dict(pm["windows"]),
        "execution": dict(pm["execution"]), "config_hash": pm["config_hash"],
        "search_constraints": {
            "strategies": "only the registered flipped strategies (mirror_set); every other strategy stays governed by "
                          "the parent protocol, and a registered flip is never governed by the parent",
            "evaluation_windows": "every discovery evaluation must lie entirely inside the discovery window",
            "stages": {"discovery": "the flip scan's search (one trial per flipped strategy)",
                       "holdout": "only Services.evaluate_holdout for a flipped survivor, within this protocol's looks"}},
        "trial_budget": {"max_unique_trials": own,
                         "unit": "unique numerical trial = (protocol, logic_hash, content hash of the evaluated bars "
                                 "[dataset + window], config hash); the budget is exactly the registered set"},
        "holdout_budget": {"max_unique_candidate_evaluations": holdout_looks, "per_candidate": 1},
        "acceptance_criteria": acceptance,
        "multiple_testing": mt,
        "pre_protocol_exposure": {
            "statement": (f"Flipped strategies were selected from the parent protocol's discovery results (before-cost R "
                          f"per trade; see selection). The parent had used {int(parent_looks_used)} holdout look(s) when "
                          "this protocol was created; flips of holdout-tested strategies are excluded."),
            "runs": []},
        "mirror_set": [dict(r) for r in mirror_set],
        "selection": dict(selection),
        "search": {"datasets_by_timeframe": dict(datasets_by_timeframe), "period": dict(discovery_period)},
        "supersedes": None,
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


def stage_of(protocol: Mapping, first_ns: int, last_ns: int, tf_ns: int | None = None) -> str:
    """discovery | holdout | overlap for an evaluation whose bars run from first_ns to last_ns.

    Holdout = exactly the holdout window: the first and last resolved source bars. ADR-85: a dataset DERIVED from the
    protocol source on a coarser timeframe (``tf_ns`` = its bar length) covers the same window when its first bar opens
    at or after the holdout session open and contains the first source bar, and its last bar contains the last source
    bar. On the source timeframe (1-minute grid) this is the same exact-equality test as before."""
    w = protocol["material"]["windows"]
    d, h = w["discovery"], w["holdout"]
    if first_ns >= d["boundary_open_ns"] and last_ns < h["boundary_open_ns"]:
        return "discovery"
    if first_ns == h["first_bar_ns"] and last_ns == h["last_bar_ns"]:
        return "holdout"
    if tf_ns and tf_ns > 0:
        f, l = h["first_bar_ns"], h["last_bar_ns"]
        if first_ns >= h["boundary_open_ns"] and first_ns <= f < first_ns + tf_ns and last_ns <= l < last_ns + tf_ns:
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
                   multiple_testing: Mapping | None = None, search_constraints: Mapping | None = None,
                   supersedes: str | None = None) -> dict:
    m = dataset_manifest
    for k, v in (("trial_budget", trial_budget), ("holdout_looks", holdout_looks)):
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ProtocolRefusal("PROTOCOL_INVALID", f"{k} must be a positive integer", value=v)
    mt = dict(multiple_testing or DEFAULT_MULTIPLE_TESTING)
    if acceptance is None:
        import copy as _copy
        acceptance = _copy.deepcopy(DEFAULT_ACCEPTANCE)
        acceptance["oos_confidence"]["bootstrap"]["replicates"] = bootstrap_replicates_for(
            trial_budget, float(mt["familywise_alpha"]))
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
        "multiple_testing": mt,
        "pre_protocol_exposure": {"statement": exposure_statement, "runs": list(pre_protocol_exposure)},
        "supersedes": supersedes,
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


def holdout_exposed(rec: Mapping) -> set[str]:
    """Strategy ids whose holdout was already looked at under an EARLIER protocol (ADR-87: listed in this protocol's
    pre-protocol exposure by the strategy-pool switch); they are never tested on the holdout again."""
    runs = ((rec.get("material") or {}).get("pre_protocol_exposure") or {}).get("runs") or []
    return {r["strategy_id"] for r in runs if r.get("strategy_id") and str(r.get("note", "")).startswith("holdout look under")}


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


_family = family_size


def derive_seed(protocol_id: str, logic_hash: str, purpose: str = "oos_confidence_bootstrap_t_v1") -> int:
    """The pre-registered bootstrap seed of one candidate under one protocol (never user-chosen)."""
    return int(hash_obj({"protocol_id": protocol_id, "logic_hash": logic_hash, "purpose": purpose})[:16], 16)


def bootstrap_t_stats(r: np.ndarray, replicates: int, seed: int, chunk: int = BOOTSTRAP_CHUNK) -> np.ndarray:
    """B studentized bootstrap statistics t*_b = (mean*_b - mean) / (std*_b / sqrt(n)), trades resampled
    with replacement. Deterministic for (r, replicates, seed, chunk); zero resample se -> signed inf / 0."""
    r = np.asarray(r, float)
    n, m0 = len(r), float(r.mean())
    rng = np.random.default_rng(seed)
    out = np.empty(int(replicates))
    for i in range(0, int(replicates), chunk):
        k = min(chunk, int(replicates) - i)
        x = r[rng.integers(0, n, size=(k, n))]
        d = x.mean(axis=1) - m0
        se = x.std(axis=1, ddof=1) / math.sqrt(n)
        with np.errstate(divide="ignore", invalid="ignore"):
            t = d / se
        zero = se == 0
        t[zero] = np.where(d[zero] > 0, np.inf, np.where(d[zero] < 0, -np.inf, 0.0))
        out[i:i + k] = t
    return out


def robust_lower_bound(r: Sequence[float], per_test_alpha: float, spec: Mapping, seed: int,
                       replicates: int | None = None, t_stats: np.ndarray | None = None) -> dict:
    """``oos_confidence`` method ``min_normal_bootstrap_t_v1``: LB = mean - max(z, q_boot) * se at the
    one-sided ``per_test_alpha``. Unavailable (never met) for degenerate / too small / non-finite samples."""
    b = spec["bootstrap"]
    x = np.asarray(r, float)
    n = int(len(x))
    base = {"method_id": spec["method_id"], "n": n, "per_test_alpha": per_test_alpha, "seed": int(seed),
            "replicates": int(replicates or b["replicates"]), "numpy": np.__version__}
    if n < b["min_trades"] or not np.isfinite(x).all():
        return {**base, "available": False, "reason": f"n < {b['min_trades']} or non-finite trade returns", "lb": None}
    mean, sd = float(x.mean()), float(x.std(ddof=1))
    se = sd / math.sqrt(n)
    if not se > 0:
        return {**base, "available": False, "reason": "zero standard error (degenerate sample)", "lb": None}
    if not 0 < per_test_alpha < 1:
        raise ValueError("per_test_alpha must be in (0, 1)")
    z = NormalDist().inv_cdf(1.0 - per_test_alpha)
    t = t_stats if t_stats is not None else bootstrap_t_stats(x, base["replicates"], seed, b.get("chunk", BOOTSTRAP_CHUNK))
    k = math.ceil((1.0 - per_test_alpha) * len(t))                 # order statistic (1-based), conservative
    q = float(np.sort(t)[min(k, len(t)) - 1])
    lb_n, lb_b = mean - z * se, (mean - q * se if math.isfinite(q) else -math.inf)
    return {**base, "available": True, "mean": mean, "se": se, "z": z, "q_boot": q, "order_statistic": k,
            "lb_normal": lb_n, "lb_bootstrap_t": lb_b, "lb": min(lb_n, lb_b),
            "binding": "normal" if lb_n <= lb_b else "bootstrap_t"}


def control_p_value(candidate: float | None, controls: Sequence[float]) -> float | None:
    """Exact Monte-Carlo p-value; ties and non-finite control statistics count against the candidate."""
    if candidate is None or not np.isfinite(candidate) or not len(controls):
        return None
    c = np.array([np.nan if v is None else v for v in controls], float)
    worse = int(np.sum(~np.isfinite(c) | (c >= candidate)))
    return (1 + worse) / (len(c) + 1)


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if not math.isnan(v) else None


def assess_holdout(material: Mapping, metrics: Mapping, cost_rows: Sequence[Mapping],
                   control_expectancies: Sequence[float], family_size: int, trade_r: Sequence[float] | None = None,
                   seed: int | None = None) -> dict:
    """Apply the protocol's pre-registered criteria to one holdout evaluation. Every criterion is
    reported (pass / fail / unavailable); unavailable counts as not met. Never 'accepted'."""
    ac, mt = material["acceptance_criteria"], material["multiple_testing"]
    adj = bonferroni(mt, _family(material, family_size))
    crit = {}
    lab = metrics.get("sample_label")
    need = ac["min_oos_sample_label"]
    crit["sample"] = {"value": lab, "required": need,
                      "met": lab in SAMPLE_ORDER and SAMPLE_ORDER.index(lab) >= SAMPLE_ORDER.index(need)}
    e, se, n = _num(metrics.get("expectancy_r")), _num(metrics.get("expectancy_se")), metrics.get("trade_count") or 0
    crit["expectancy"] = {"value": e, "required": f"> {ac['min_oos_expectancy_r_exclusive']}",
                          "met": e is not None and e > ac["min_oos_expectancy_r_exclusive"]}
    oc = ac["oos_confidence"]
    if oc.get("method_id") == "min_normal_bootstrap_t_v1":
        if trade_r is None or seed is None:
            raise ValueError("the robust lower bound needs the OOS per-trade net R and the derived seed")
        rb = robust_lower_bound(trade_r, adj["per_test_alpha"], oc, seed)
        lb = rb["lb"]
        crit["adjusted_confidence"] = {**rb, "lower_bound": lb, "family_size": adj["family_size"],
                                       "required": f"> {oc['must_exceed']}",
                                       "met": bool(rb["available"] and lb is not None and lb > oc["must_exceed"])}
    else:                                    # protocol_version 1 records: normal approximation only
        lb = e - adj["z_one_sided"] * se if (e is not None and se is not None and n > 1) else None
        crit["adjusted_confidence"] = {"method_id": "normal_v1", "lower_bound": lb, "z": adj["z_one_sided"],
                                       "per_test_alpha": adj["per_test_alpha"], "family_size": adj["family_size"],
                                       "required": f"> {oc['must_exceed']}",
                                       "met": lb is not None and lb > oc["must_exceed"]}
    pf = metrics.get("profit_factor")
    pf_v = math.inf if (pf is None and (metrics.get("loss_rate") == 0 and (metrics.get("net_r") or 0) > 0)) else _num(pf)
    crit["profit_factor"] = {"value": pf_v, "required": f"> {ac['min_profit_factor_exclusive']}",
                             "met": pf_v is not None and pf_v > ac["min_profit_factor_exclusive"]}
    rc = ac["random_control"]
    if rc.get("rule_id") == "monte_carlo_pvalue_v1":
        pv = control_p_value(e, list(control_expectancies))
        crit["random_control"] = {"rule_id": rc["rule_id"], "p_value": pv, "max_p_value": rc["max_p_value"],
                                  "n_controls": len(control_expectancies), "candidate": e,
                                  "role": "robustness filter, not a familywise test",
                                  "met": pv is not None and len(control_expectancies) == rc["n_controls"]
                                  and pv <= rc["max_p_value"]}
    else:                                    # protocol_version 1 records: linear-interpolated percentile
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
