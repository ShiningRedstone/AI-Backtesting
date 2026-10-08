"""Saved chart state (ADR-111): drawings per symbol and the chart layout, as JSON under <data>/charts/. Data only: the
browser sends plain JSON, it is size-checked and stored as is (never executed, never research data)."""
from __future__ import annotations

import json
from pathlib import Path

from edgelab.charts.feed import SYMBOLS, ChartError

MAX_BYTES = 4_000_000


def _dir(data_root) -> Path:
    p = Path(data_root) / "charts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(p: Path, obj) -> None:
    from edgelab.core.fsutil import atomic_write_text
    text = json.dumps(obj, separators=(",", ":"))
    if len(text) > MAX_BYTES:
        raise ChartError("TOO_LARGE", "Too much chart data to save (over 4 MB).")
    atomic_write_text(p, text)


def drawings(data_root, symbol: str) -> list:
    if symbol not in SYMBOLS:
        raise ChartError("BAD_SYMBOL", f"Unknown symbol {symbol!r}.")
    return _read(_dir(data_root) / f"drawings_{symbol}.json", [])


def save_drawings(data_root, symbol: str, items) -> dict:
    if symbol not in SYMBOLS:
        raise ChartError("BAD_SYMBOL", f"Unknown symbol {symbol!r}.")
    if not isinstance(items, list) or not all(isinstance(x, dict) and isinstance(x.get("type"), str) for x in items):
        raise ChartError("BAD_DRAWINGS", "Drawings must be a list of objects with a type.")
    _write(_dir(data_root) / f"drawings_{symbol}.json", items)
    return {"saved": len(items)}


def layout(data_root) -> dict:
    return _read(_dir(data_root) / "layout.json", {})


def save_layout(data_root, obj) -> dict:
    if not isinstance(obj, dict):
        raise ChartError("BAD_LAYOUT", "The chart layout must be an object.")
    _write(_dir(data_root) / "layout.json", obj)
    return {"saved": True}
