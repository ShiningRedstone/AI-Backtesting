"""Process-wide, size-bounded memo of DERIVED inputs (ADR-91).

Research runs compute the same derived inputs over and over: every strategy on a dataset re-resamples its bars to the
same higher timeframes, re-derives the same session membership and trading dates, and the causality check does it
again for each of its ~21 truncated histories, which are identical for every strategy on that dataset (the cuts are
seeded). This memo keeps such results keyed by the EXACT content hash of the bars they were computed from (plus the
calendar / session definition), so a value is only ever reused for byte-identical input: results cannot change.

Stored arrays are made read-only, so a caller can never alter a shared value. Bounded by bytes (env
``EDGELAB_DERIVED_CACHE_MB``, default 512 MB; research workers get their share from the memory plan); least recently
used entries are dropped first. A miss simply recomputes.
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from typing import Any, Callable, Hashable

import numpy as np

DEFAULT_MB = 512


def _budget() -> int:
    try:
        return max(0, int(os.environ.get("EDGELAB_DERIVED_CACHE_MB", DEFAULT_MB))) * 1024 * 1024
    except ValueError:
        return DEFAULT_MB * 1024 * 1024


def _freeze(v: Any) -> tuple[Any, int]:
    """Make arrays (also inside tuples / dicts / BarArrays) read-only; return (value, bytes)."""
    if isinstance(v, np.ndarray):
        if v.flags.writeable:
            v.flags.writeable = False
        return v, int(v.nbytes)
    if isinstance(v, tuple):
        parts = [_freeze(x) for x in v]
        return tuple(p[0] for p in parts), sum(p[1] for p in parts)
    if isinstance(v, dict):
        size = 0
        for k, x in v.items():
            v[k], s = _freeze(x)
            size += s
        return v, size
    arrays = getattr(v, "__dict__", None)
    if arrays is not None and hasattr(v, "content_hash"):      # BarArrays: already read-only by construction
        return v, int(sum(a.nbytes for a in arrays.values() if isinstance(a, np.ndarray)))
    return v, 64


class DerivedMemo:
    def __init__(self, max_bytes: int | None = None):
        self.max_bytes = _budget() if max_bytes is None else int(max_bytes)
        self._d: OrderedDict[Hashable, tuple[Any, int]] = OrderedDict()
        self.bytes = 0
        self.hits = self.misses = 0
        self._lock = threading.Lock()

    def get_or_compute(self, key: Hashable, fn: Callable[[], Any]) -> Any:
        if not self.max_bytes:
            return fn()
        with self._lock:
            hit = self._d.get(key)
            if hit is not None:
                self._d.move_to_end(key)
                self.hits += 1
                return hit[0]
        value, size = _freeze(fn())
        with self._lock:
            self.misses += 1
            if size <= self.max_bytes and key not in self._d:
                self._d[key] = (value, size)
                self.bytes += size
                while self.bytes > self.max_bytes and self._d:
                    _, (_, s) = self._d.popitem(last=False)
                    self.bytes -= s
        return value

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self.bytes = 0


_MEMO: list[DerivedMemo] = []
_MEMO_LOCK = threading.Lock()


def derived() -> DerivedMemo:
    with _MEMO_LOCK:
        if not _MEMO:
            _MEMO.append(DerivedMemo())
        return _MEMO[0]
