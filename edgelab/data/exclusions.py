"""Explicit, audited source-quality exclusion windows (ADR-44).

A named set in ``configs/data.yaml`` (``source_exclusions.<NAME>``) lists half-open windows
``[start, end)`` of bar-open times that a specific source is known to deliver OUTSIDE its expected
session (e.g. HistData NSXUSD's extra 17:00-17:59 New York bars in DST-mismatch weeks). An import
opts in by naming the set; nothing is ever excluded implicitly.

This is NOT a filter. A set is refused unless every window is well formed (quoted timestamps with
an explicit UTC offset, start < end, a non-empty reason, no overlaps), every window matches at least
one bar, and every matched bar is outside the import calendar's session. So it can only remove bars
the ``bars_outside_session`` check would flag anyway, and only where someone wrote down why.
Thresholds and calendars are untouched; the retained bars are validated normally, and the full
record (counts, windows, reasons, set hash, hash of the removed rows) goes into the manifest.
"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping

import numpy as np
import pandas as pd

from edgelab.core.identity import hash_obj
from edgelab.data.calendar import SessionCalendar


class ExclusionError(ValueError):
    """An exclusion set is malformed or unsafe for this data; nothing is excluded."""


def _instant(value: Any, where: str) -> pd.Timestamp:
    if not isinstance(value, str):
        raise ExclusionError(f"{where}: timestamps must be quoted strings with an explicit UTC offset "
                             f"(got {type(value).__name__}; unquoted YAML timestamps lose their offset)")
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError) as exc:
        raise ExclusionError(f"{where}: unparseable timestamp {value!r}") from exc
    if ts.tz is None:
        raise ExclusionError(f"{where}: {value!r} has no UTC offset; refusing to guess its timezone")
    return ts.tz_convert("UTC")


def parse_exclusion_set(name: str, spec: Mapping) -> list[dict]:
    """Validate a set definition; return its windows sorted by start (UTC)."""
    windows = spec.get("windows") if isinstance(spec, Mapping) else None
    if not isinstance(windows, list) or not windows:
        raise ExclusionError(f"exclusion set {name!r}: 'windows' must be a non-empty list")
    out = []
    for i, w in enumerate(windows):
        where = f"exclusion set {name!r} window {i}"
        if not isinstance(w, Mapping) or set(w) != {"start", "end", "reason"}:
            raise ExclusionError(f"{where}: needs exactly the keys start, end, reason")
        if not isinstance(w["reason"], str) or not w["reason"].strip():
            raise ExclusionError(f"{where}: reason must be a non-empty string")
        s, e = _instant(w["start"], where), _instant(w["end"], where)
        if not s < e:
            raise ExclusionError(f"{where}: start {w['start']} is not before end {w['end']}")
        out.append({"start": w["start"], "end": w["end"], "reason": w["reason"].strip(),
                    "start_utc": s, "end_utc": e})
    out.sort(key=lambda w: w["start_utc"])
    for a, b in zip(out, out[1:]):
        if b["start_utc"] < a["end_utc"]:
            raise ExclusionError(f"exclusion set {name!r}: windows [{a['start']}, {a['end']}) and "
                                 f"[{b['start']}, {b['end']}) overlap; they are not merged implicitly")
    return out


def apply_exclusions(df: pd.DataFrame, calendar: SessionCalendar, name: str,
                     spec: Mapping) -> tuple[pd.DataFrame, dict]:
    """Remove the rows inside the set's windows. Returns (retained rows, audit record).

    Raises ExclusionError if the set is malformed, a window matches no bar, or any matched bar is
    inside the calendar's session. ``df`` must hold normalized bars (tz-aware UTC ``ts``)."""
    windows = parse_exclusion_set(name, spec)
    ts = pd.DatetimeIndex(df["ts"])
    ns = ts.asi8
    drop = np.zeros(len(df), dtype=bool)
    record_windows = []
    for w in windows:
        m = (ns >= w["start_utc"].value) & (ns < w["end_utc"].value)
        n = int(m.sum())
        if n == 0:
            raise ExclusionError(f"exclusion set {name!r}: window [{w['start']}, {w['end']}) matches no "
                                 f"bar; an audited window must describe bars that exist")
        inside = m & calendar.in_session(ts)
        if inside.any():
            raise ExclusionError(
                f"exclusion set {name!r}: window [{w['start']}, {w['end']}) contains {int(inside.sum())} "
                f"bar(s) inside calendar {calendar.name!r} sessions, e.g. {[str(t) for t in ts[inside][:3]]}; "
                f"exclusions may only remove out-of-session source anomalies")
        drop |= m
        record_windows.append({"start": w["start"], "end": w["end"], "reason": w["reason"],
                               "start_utc": w["start_utc"].isoformat(), "end_utc": w["end_utc"].isoformat(),
                               "rows_excluded": n})
    removed = df.loc[drop]
    record = {
        "set": name,
        "set_hash": hash_obj({"name": name, "windows": [{k: w[k] for k in ("start", "end", "reason")}
                                                         for w in windows]}),
        "description": str(spec.get("description", "")),
        "calendar": calendar.name,
        "rows_before": int(len(df)),
        "rows_excluded": int(drop.sum()),
        "rows_after": int(len(df) - drop.sum()),
        "excluded_rows_sha256": hashlib.sha256(removed.to_csv(index=False).encode()).hexdigest(),
        "windows": record_windows,
    }
    return df.loc[~drop].reset_index(drop=True), record


def exclusion_set_from_config(cfg: Mapping, name: str) -> Mapping:
    sets = cfg.get("source_exclusions") or {}
    if name not in sets:
        raise ExclusionError(f"unknown exclusion set {name!r}; define it under source_exclusions in "
                             f"configs/data.yaml")
    return sets[name]
