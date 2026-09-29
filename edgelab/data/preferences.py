"""Workspace-level research preferences (Phase 9): currently the Preferred Research Dataset.

Stored as ``<data root>/workspace_preferences.json``, next to the workspace's store, so it travels
with the workspace and is never part of a run record, a strategy, a dataset or the research config
hash. It is only a DEFAULT for new research: pages preselect it; nothing already stored (runs,
searches, validations, strategies, datasets) is read from it or rewritten when it changes.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

PREFS_FILE = "workspace_preferences.json"
PREFS_VERSION = 1
HISTORY_LIMIT = 50


def prefs_path(data_root: str | Path) -> Path:
    return Path(data_root) / PREFS_FILE


def load_prefs(data_root: str | Path) -> dict:
    p = prefs_path(data_root)
    if not p.exists():
        return {"preferences_version": PREFS_VERSION}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"workspace preferences at {p} are unreadable ({exc}); fix or delete the file") from exc
    if not isinstance(d, dict) or d.get("preferences_version") != PREFS_VERSION:
        raise ValueError(f"workspace preferences at {p} have an unsupported format")
    return d


def save_prefs(data_root: str | Path, prefs: dict) -> None:
    """Atomic replace: a crash never leaves a half-written preferences file."""
    p = prefs_path(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({**prefs, "preferences_version": PREFS_VERSION}, indent=1, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".prefs-", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
