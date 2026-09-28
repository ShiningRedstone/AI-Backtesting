"""Structured logging.

Every record is emitted as one JSON line with a fixed field set:
timestamp, component, severity, event, strategy, signal, account, order, error,
run_id, plus any extra structured fields. Missing fields are written as null so
downstream analysis can rely on the columns existing.

Usage:
    log = get_logger("backtester")
    log.event("trade_closed", strategy="BRK_01", run_id=rid, net_r=1.2)
    log.event("fill_rejected", severity="WARNING", error="stop beyond entry")
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STANDARD_FIELDS = ("component", "event", "strategy", "signal", "account", "order",
                   "error", "run_id")
_ROOT_NAME = "edgelab"
_configured = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc)
                                 .isoformat(timespec="milliseconds"),
            "severity": record.levelname,
        }
        fields = getattr(record, "fields", {}) or {}
        for f in STANDARD_FIELDS:
            payload[f] = fields.get(f)
        payload["component"] = payload["component"] or record.name.removeprefix(_ROOT_NAME + ".")
        payload["event"] = payload["event"] or record.getMessage()
        for k, v in fields.items():
            if k not in payload:
                payload[k] = v
        return json.dumps(payload, default=str)


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {}) or {}
        extras = " ".join(f"{k}={v}" for k, v in fields.items()
                          if k not in ("component", "event") and v is not None)
        comp = fields.get("component") or record.name.removeprefix(_ROOT_NAME + ".")
        return f"{record.levelname:<7} [{comp}] {fields.get('event') or record.getMessage()} {extras}".rstrip()


class EventLogger:
    """Thin wrapper that forces structured, named events."""

    def __init__(self, component: str):
        self.component = component
        self._log = logging.getLogger(f"{_ROOT_NAME}.{component}")

    def event(self, event: str, severity: str = "INFO", **fields: Any) -> None:
        level = logging.getLevelName(severity.upper())
        if not isinstance(level, int):
            level = logging.INFO
        fields = {"component": self.component, "event": event, **fields}
        self._log.log(level, event, extra={"fields": fields})


def configure_logging(level: str = "INFO", json_file: str | Path | None = None,
                      console: bool = True, force: bool = False) -> None:
    global _configured
    if _configured and not force:
        return
    root = logging.getLogger(_ROOT_NAME)
    root.handlers.clear()
    root.setLevel(level.upper())
    root.propagate = False
    if console:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(HumanFormatter())
        root.addHandler(h)
    if json_file:
        Path(json_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(json_file)
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)
    _configured = True


def get_logger(component: str) -> EventLogger:
    global _configured
    if not logging.getLogger(_ROOT_NAME).handlers:
        # Safe default: warnings and above to stderr until configured explicitly.
        configure_logging(level="WARNING", force=True)
        _configured = False  # an explicit configure_logging() call still takes effect
    return EventLogger(component)
