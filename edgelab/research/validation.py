"""Validation primitives for FIXED strategies: temporal holdout (OOS), walk-forward windows,
frozen definitions, and trade-resampling Monte Carlo. Nothing here fits or optimises anything.

Windows. Bounds are UTC instants; a window selects bars whose OPEN is in [start, end] (the
``restrict_to_period`` convention). Consecutive windows are separated by 1 ns, so no bar can fall
in two of them. Each window is backtested on its own re-validated restricted dataset (lineage:
``parent_dataset_id``), so indicators start fresh at the window start (causal; the first bars of a
window may produce no signals while features warm up) and no position carries across a boundary.

Frozen definition. The strategy document is resolved once, deep-copied per window and hashed
before and after; every window must compile to the same ``strategy_id``. With a fixed strategy the
"train" window was not used to choose anything, so a split measures stability across time; it
becomes a true out-of-sample test only when a selection step uses the train window alone.

Monte Carlo (``monte_carlo``). Resamples the OBSERVED per-trade R values; it is not a market
simulation and does not re-run the engine.
  * ``bootstrap``: n trades drawn with replacement per simulation. Treats trades as i.i.d. draws
    (not established: trades can be serially dependent and regimes change). Gives distributions of
    total R and maximum drawdown under that assumption.
  * ``shuffle``: a random permutation of the same trades. Total R is identical in every
    simulation; only the ORDER changes, so it tests how much the observed drawdown depends on
    trade sequence.
Seeded with ``numpy.random.default_rng(seed)``; identical inputs + seed give identical output.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from edgelab.analytics.metrics import max_drawdown
from edgelab.core.identity import hash_obj

ONE_NS = pd.Timedelta(1, "ns")
MC_METHODS = {
    "bootstrap": "trades resampled with replacement (i.i.d. assumption, not established); "
                 "distribution of total R and max drawdown",
    "shuffle": "same trades in random order; total R unchanged, tests sequence dependence of drawdown",
}
PERCENTILES = (5, 25, 50, 75, 95)


class ValidationError(ValueError):
    """A validation request is malformed (bad windows, split outside the data, ...)."""


def _utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


@dataclass(frozen=True)
class Window:
    role: str
    start: pd.Timestamp
    end: pd.Timestamp                    # inclusive (bar opens in [start, end])

    def to_dict(self) -> dict:
        return {"role": self.role, "start": self.start.isoformat(), "end": self.end.isoformat()}


def oos_windows(start, end, split_at) -> tuple[Window, Window]:
    """[start, split) is the train window, [split, end] the out-of-sample window."""
    s, e, k = _utc(start), _utc(end), _utc(split_at)
    if not s < k <= e:
        raise ValidationError(f"split_at {k} must be after the data start {s} and not after its end {e}")
    return Window("train", s, k - ONE_NS), Window("oos", k, e)


def walk_forward_windows(start, end, train_months: int, test_months: int,
                         anchored: bool = False) -> list[dict]:
    """Consecutive, non-overlapping test windows of ``test_months`` after an initial
    ``train_months``; each test window is preceded by its train window (rolling: the previous
    ``train_months``; anchored: everything since ``start``). The last test window may be shorter
    (``partial``: true); a test window must contain at least one instant of the data."""
    if not (isinstance(train_months, int) and isinstance(test_months, int)) or train_months < 1 or test_months < 1:
        raise ValidationError("train_months and test_months must be integers >= 1")
    s, e = _utc(start), _utc(end)
    out, test_start = [], s + pd.DateOffset(months=train_months)
    while test_start <= e:
        nominal_end = test_start + pd.DateOffset(months=test_months) - ONE_NS
        train_start = s if anchored else test_start - pd.DateOffset(months=train_months)
        out.append({"index": len(out),
                    "train": Window("train", max(train_start, s), test_start - ONE_NS),
                    "test": Window("test", test_start, min(nominal_end, e)),
                    "partial": nominal_end > e})
        test_start = test_start + pd.DateOffset(months=test_months)
    if not out:
        raise ValidationError(f"no test window fits: the data span {s} .. {e} is shorter than "
                              f"train_months={train_months}")
    return out


def freeze_definition(definition: Mapping) -> tuple[dict, str]:
    """A deep, JSON-canonical copy of the strategy document and its hash."""
    frozen = json.loads(json.dumps(definition, sort_keys=True, default=str))
    return frozen, hash_obj(frozen)


def copy_frozen(frozen: Mapping) -> dict:
    return copy.deepcopy(dict(frozen))


def _max_dd_rows(samples: np.ndarray) -> np.ndarray:
    eq = np.concatenate([np.zeros((samples.shape[0], 1)), np.cumsum(samples, axis=1)], axis=1)
    return np.max(np.maximum.accumulate(eq, axis=1) - eq, axis=1)


def monte_carlo(r: Sequence[float] | np.ndarray, n_sims: int = 1000, seed: int = 0,
                method: str = "bootstrap", percentiles: Sequence[int] = PERCENTILES) -> dict[str, Any]:
    """Seeded resampling of observed per-trade R (see module docstring for what each method tests)."""
    if method not in MC_METHODS:
        raise ValidationError(f"method must be one of {sorted(MC_METHODS)}")
    if not (isinstance(n_sims, int) and n_sims >= 1):
        raise ValidationError("n_sims must be an integer >= 1")
    r = np.asarray(r, dtype=float)
    n = len(r)
    out: dict[str, Any] = {"method": method, "tests": MC_METHODS[method], "seed": int(seed),
                           "n_sims": n_sims, "n_trades": n,
                           "note": "resampling of observed trades, not a market simulation"}
    if n == 0:
        return out
    rng = np.random.default_rng(seed)
    chunk = max(1, 2_000_000 // n)
    totals, dds = [], []
    for k0 in range(0, n_sims, chunk):
        k = min(chunk, n_sims - k0)
        if method == "bootstrap":
            samples = r[rng.integers(0, n, size=(k, n))]
        else:
            samples = rng.permuted(np.tile(r, (k, 1)), axis=1)
        totals.append(samples.sum(axis=1))
        dds.append(_max_dd_rows(samples))
    tot, dd = np.concatenate(totals), np.concatenate(dds)
    obs_total, obs_dd = float(r.sum()), max_drawdown(r)
    pct = [int(p) for p in percentiles]
    out.update({
        "observed_total_r": obs_total, "observed_max_drawdown_r": obs_dd,
        "total_r_percentiles": dict(zip(pct, np.percentile(tot, pct).tolist())),
        "max_drawdown_r_percentiles": dict(zip(pct, np.percentile(dd, pct).tolist())),
        "p_total_r_negative": float((tot < 0).mean()),
        "observed_max_drawdown_rank": float((dd <= obs_dd).mean()),   # share of sims with DD <= observed
    })
    return out


def validation_id(kind: str, definition_hash: str, dataset_id: str, windows: Sequence[Mapping]) -> str:
    return "VAL_" + hash_obj({"kind": kind, "definition": definition_hash, "dataset": dataset_id,
                              "windows": list(windows)})[:12].upper()
