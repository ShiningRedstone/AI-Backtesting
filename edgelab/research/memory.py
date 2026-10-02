"""How many CPU cores a research run can use without running out of memory (ADR-91).

Every worker process holds its own copy of the datasets it needs plus two in-memory caches (the feature cache and the
causality check's truncated-history cache). Before a multi-core run the parent measures what one worker needs and the
memory that is free, and uses only as many workers as fit. The number of workers never changes a result (ADR-77): each
strategy is computed the same way on any core and written by the parent in plan order.
"""
from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping

import numpy as np

MB = 1024 * 1024
USABLE_SHARE = 0.70              # of the memory that is available when the run starts
PARENT_RESERVE_MB = 1024         # the app itself (web server, read caches, the parent's dataset copies)
WORKER_OVERHEAD_MB = 1200        # measured (ADR-91): Python + libraries + transient feature computation on 1m data
MIN_FEATURE_CACHE_MB = 256
MAX_FEATURE_CACHE_MB = 2048
MIN_TRUNCATION_CACHE_MB = 128
MAX_TRUNCATION_CACHE_MB = 1024
MIN_DERIVED_CACHE_MB = 128       # higher-timeframe bars, session membership, trading dates (core/memo.py)
MAX_DERIVED_CACHE_MB = 768


def system_memory() -> tuple[int | None, int | None]:
    """(total, available) bytes of physical memory, or (None, None) when unknown. No extra dependency."""
    try:
        if sys.platform == "win32":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullTotalPhys), int(st.ullAvailPhys)
            return None, None
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        return info.get("MemTotal"), info.get("MemAvailable", info.get("MemFree"))
    except Exception:                                        # noqa: BLE001 - unknown: the caller keeps its choice
        try:
            page, pages = os.sysconf("SC_PAGE_SIZE"), os.sysconf("SC_PHYS_PAGES")
            avail = os.sysconf("SC_AVPHYS_PAGES")
            return int(page * pages), int(page * avail)
        except (ValueError, OSError, AttributeError):
            return None, None


def dataset_bytes(ds) -> int:
    """Memory one copy of a validated dataset's bar arrays takes (measured from the arrays, not guessed)."""
    bars = getattr(ds, "bars", None)
    if bars is None:
        return 0
    fields = vars(bars) if hasattr(bars, "__dict__") else {k: getattr(bars, k) for k in getattr(bars, "__slots__", ())}
    return int(sum(v.nbytes for v in fields.values() if isinstance(v, np.ndarray)))


@dataclass(frozen=True)
class MemoryPlan:
    requested: int                 # cores the user's setting asks for
    processes: int                 # cores the run will use
    limited_by_memory: bool
    available_bytes: int | None
    total_bytes: int | None
    worker_fixed_bytes: int        # datasets + process overhead, per worker
    feature_cache_mb: int          # per worker
    truncation_cache_mb: int       # per worker
    derived_cache_mb: int = MIN_DERIVED_CACHE_MB    # per worker

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def note(self) -> str:
        free = f"{self.available_bytes / 1024 ** 3:.1f} GB free" if self.available_bytes else "free memory unknown"
        if self.limited_by_memory:
            return f"{self.processes} of {self.requested} CPU cores: limited by memory ({free})"
        return f"{self.processes} CPU core{'s' if self.processes != 1 else ''} ({free})"


def plan(requested: int, datasets: Iterable, available: int | None = None, total: int | None = None) -> MemoryPlan:
    """How many of ``requested`` worker processes fit in memory, and each one's cache budgets."""
    requested = max(1, int(requested))
    if available is None and total is None:
        total, available = system_memory()
    # a worker keeps each dataset AND its period-restricted copy for the run's window (Services._cell_dataset): x2
    fixed = 2 * sum(dataset_bytes(d) for d in datasets) + WORKER_OVERHEAD_MB * MB
    if not available:
        return MemoryPlan(requested, requested, False, available, total, fixed, MIN_FEATURE_CACHE_MB * 2,
                          MIN_TRUNCATION_CACHE_MB * 2, MIN_DERIVED_CACHE_MB * 2)
    usable = max(0.0, USABLE_SHARE * available - PARENT_RESERVE_MB * MB)
    per_min = fixed + (MIN_FEATURE_CACHE_MB + MIN_TRUNCATION_CACHE_MB + MIN_DERIVED_CACHE_MB) * MB
    fit = max(1, int(usable // per_min)) if usable > 0 else 1
    n = min(requested, fit)
    spare = max(0.0, usable - n * fixed) / n / MB if n else 0.0     # cache memory each worker may use
    feat = int(min(MAX_FEATURE_CACHE_MB, max(MIN_FEATURE_CACHE_MB, spare * 0.45)))
    trunc = int(min(MAX_TRUNCATION_CACHE_MB, max(MIN_TRUNCATION_CACHE_MB, spare * 0.30)))
    der = int(min(MAX_DERIVED_CACHE_MB, max(MIN_DERIVED_CACHE_MB, spare * 0.25)))
    return MemoryPlan(requested, n, n < requested, int(available), total and int(total), int(fixed), feat, trunc, der)


def is_memory_failure(text: str | None) -> bool:
    """A failure caused by running out of memory (or a worker the system killed for it)."""
    t = text or ""
    return "MemoryError" in t or "BrokenProcessPool" in t or "Unable to allocate" in t


def failed_texts(out: Mapping) -> list[str]:
    return [str(c.get("error") or "") for c in out.get("failed_cells") or ()]
