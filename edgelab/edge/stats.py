"""Small, deterministic statistics for the edge lab (fixed seeds: the same inputs always give the same numbers)."""
from __future__ import annotations

import math

import numpy as np

PERMUTATIONS = 10_000
BOOTSTRAPS = 10_000


def bootstrap_ci(x: np.ndarray, level: float, seed: int, reps: int = BOOTSTRAPS) -> tuple[float, float] | None:
    """Percentile bootstrap interval of the mean (observations resampled with replacement)."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 2:
        return None
    rng = np.random.default_rng(seed)
    means = np.empty(reps)
    step = max(1, 2_000_000 // n)
    for a in range(0, reps, step):
        b = min(reps, a + step)
        idx = rng.integers(0, n, size=(b - a, n))
        means[a:b] = x[idx].mean(axis=1)
    lo, hi = np.quantile(means, [(1 - level) / 2, 1 - (1 - level) / 2])
    return float(lo), float(hi)


def shuffle_test(direction: np.ndarray, move: np.ndarray, seed: int, reps: int = PERMUTATIONS) -> dict:
    """Is the signal's direction related to the move that followed? The directions are shuffled across the same days
    (same number of longs and shorts, same moves, so drift is kept) ``reps`` times. Two-sided p-value of the mean of
    direction x move, and the null distribution's summary (for the chart)."""
    d = np.asarray(direction, dtype=float)
    m = np.asarray(move, dtype=float)
    obs = float(np.mean(d * m))
    rng = np.random.default_rng(seed)
    null = np.empty(reps)
    for k in range(reps):
        null[k] = float(np.mean(rng.permutation(d) * m))
    centre = float(null.mean())
    p = (1 + int(np.sum(np.abs(null - centre) >= abs(obs - centre) - 1e-15))) / (reps + 1)
    return {"observed": obs, "p": float(p), "null_mean": centre, "null_sd": float(null.std(ddof=1)),
            "null_hist": histogram(null)}


def histogram(x: np.ndarray, bins: int = 40) -> dict:
    x = np.asarray(x, dtype=float)
    if not len(x):
        return {"edges": [], "counts": [], "n": 0}
    counts, edges = np.histogram(x, bins=bins)
    return {"edges": [float(e) for e in edges], "counts": [int(c) for c in counts], "n": int(len(x))}


def summary(x: np.ndarray) -> dict:
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n == 0:
        return {"n": 0, "mean": None, "sd": None, "se": None, "t": None}
    sd = float(np.std(x, ddof=1)) if n > 1 else None
    se = sd / math.sqrt(n) if sd is not None else None
    return {"n": int(n), "mean": float(np.mean(x)), "sd": sd, "se": se,
            "t": float(np.mean(x) / se) if se else None}


def z_two_sided(alpha: float) -> float:
    """z with P(|Z| > z) = alpha (standard normal; no scipy needed)."""
    lo, hi = 0.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if math.erfc(mid / math.sqrt(2)) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def detectable_mean(se: float | None, alpha: float, power: float = 0.80) -> float | None:
    """The smallest true mean the test would find with ``power`` at level ``alpha`` (normal approximation)."""
    if not se:
        return None
    z_power = z_two_sided(2 * (1 - power))     # one-sided quantile of the power
    return (z_two_sided(alpha) + z_power) * se
