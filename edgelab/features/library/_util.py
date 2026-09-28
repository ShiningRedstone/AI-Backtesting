"""Deterministic, causal numerical building blocks (all prefix-invariant: appending bars
never changes earlier outputs, bit for bit)."""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.signal import lfilter

NAN = np.nan


def recursive_smooth(x: np.ndarray, alpha: float, seed_index: int, seed_value: float) -> np.ndarray:
    """y[seed_index] = seed_value; y[t] = y[t-1] + alpha * (x[t] - y[t-1]) for t > seed_index.
    NaN before seed_index. Implemented with scipy's sequential IIR filter (exact recurrence)."""
    n = len(x)
    out = np.full(n, NAN)
    if seed_index >= n or not np.isfinite(seed_value):
        return out
    out[seed_index] = seed_value
    rest = x[seed_index + 1:]
    if len(rest):
        zi = np.array([(1.0 - alpha) * seed_value])
        out[seed_index + 1:], _ = lfilter([alpha], [1.0, -(1.0 - alpha)], rest, zi=zi)
    return out


def rolling_window(x: np.ndarray, w: int) -> np.ndarray:
    """(n-w+1, w) read-only view; row j covers x[j : j+w]."""
    return np.lib.stride_tricks.sliding_window_view(x, w)


def trailing(x: np.ndarray, w: int, fn) -> np.ndarray:
    """fn over the w bars ending at t (inclusive). NaN for t < w-1."""
    out = np.full(len(x), NAN)
    if len(x) >= w:
        out[w - 1:] = fn(rolling_window(x, w), axis=1)
    return out


def prior(x: np.ndarray, w: int, fn) -> np.ndarray:
    """fn over the w bars BEFORE t (t-w .. t-1), excluding t. NaN for t < w."""
    out = np.full(len(x), NAN)
    if len(x) > w:
        out[w:] = fn(rolling_window(x, w)[:-1], axis=1)
    return out


def ffill(x: np.ndarray) -> np.ndarray:
    """Forward-fill NaNs with the last finite value (causal)."""
    x = np.asarray(x, dtype=float)
    if len(x) == 0:
        return x.copy()
    mask = np.isfinite(x)
    idx = np.where(mask, np.arange(len(x)), 0)
    np.maximum.accumulate(idx, out=idx)
    out = x[idx]
    out[~np.maximum.accumulate(mask)] = NAN
    return out


def shift(x: np.ndarray, k: int = 1) -> np.ndarray:
    out = np.full(len(x), NAN)
    if k < len(x):
        out[k:] = x[:-k] if k else x
    return out


def group_cum(values: np.ndarray, groups: np.ndarray, how: str) -> np.ndarray:
    """Cumulative op within consecutive groups (group labels must be sorted in time)."""
    s = pd.Series(values)
    g = s.groupby(groups, sort=False)
    return getattr(g, how)().to_numpy(float)


def safe_div(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        out = a / b
    out[~np.isfinite(out)] = NAN
    return out


def last_completed(end_ns: np.ndarray, ts_close_ns: np.ndarray) -> np.ndarray:
    """Index of the latest item with end <= each bar's close (-1 if none). end_ns sorted."""
    return np.searchsorted(end_ns, ts_close_ns, side="right") - 1


def take_or_nan(values: np.ndarray, idx: np.ndarray) -> np.ndarray:
    out = np.full(len(idx), NAN)
    ok = idx >= 0
    out[ok] = values[idx[ok]]
    return out
