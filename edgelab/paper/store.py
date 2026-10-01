"""Paper account records (ADR-81): JSON files under ``<data>/paper/`` (never research runs, never protocol trials).

    accounts/<PA_id>/account.json   inputs: frozen strategy definition + identity, rule profile id/version, fees
                                    snapshot, start date, running/stopped
    accounts/<PA_id>/state.json     the last full recompute (paper.engine.simulate_account) on the feed
    batches/<PB_id>.json            one record per "Start N accounts" action
"""
from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from edgelab.core.fsutil import atomic_write_text


def paper_dir(data_root: Path | str) -> Path:
    return Path(data_root) / "paper"


def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(obj, indent=1, sort_keys=True, default=str))   # pages read it meanwhile (ADR-80)


def _read(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12].upper()}"


def save_account(data_root, acct: dict) -> None:
    _write(paper_dir(data_root) / "accounts" / acct["account_id"] / "account.json", acct)


def load_account(data_root, account_id: str) -> dict:
    a = _read(paper_dir(data_root) / "accounts" / account_id / "account.json")
    if a is None:
        raise KeyError(f"paper account {account_id} not found")
    return a


def save_state(data_root, account_id: str, state: dict) -> None:
    _write(paper_dir(data_root) / "accounts" / account_id / "state.json", {**state, "computed_at": now_iso()})


def load_state(data_root, account_id: str) -> dict | None:
    return _read(paper_dir(data_root) / "accounts" / account_id / "state.json")


def list_accounts(data_root) -> list[dict]:
    d = paper_dir(data_root) / "accounts"
    out = []
    for p in sorted(d.glob("PA_*/account.json")) if d.is_dir() else []:
        a = _read(p)
        if a:
            out.append(a)
    return sorted(out, key=lambda a: a.get("created_at") or "")


def delete_account(data_root, account_id: str) -> None:
    load_account(data_root, account_id)
    shutil.rmtree(paper_dir(data_root) / "accounts" / account_id)


def save_batch(data_root, batch: dict) -> None:
    _write(paper_dir(data_root) / "batches" / f"{batch['batch_id']}.json", batch)


def delete_records(data_root) -> int:
    """Workspace reset: remove every paper account and batch (the downloaded feed is price data and is kept)."""
    n = len(list_accounts(data_root))
    for sub in ("accounts", "batches"):
        shutil.rmtree(paper_dir(data_root) / sub, ignore_errors=True)
    return n
