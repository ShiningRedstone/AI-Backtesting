"""Research settings vs the protocol's settings (ADR-89).

A research protocol is bound to the hash of the workspace's merged config (``core.config.config_hash``). When the
workspace's ``configs/`` change after the protocol was created, the campaign preflight refuses ("research config =
protocol config"). This module explains WHY and can put the protocol's exact settings back:

* every stored run record keeps the full settings tree it ran with (``research/runs.py`` ``"config"``), so the
  protocol's settings are recovered from a run made under the protocol's config hash (verified by re-hashing);
* ``mismatch_detail`` lists the differing settings (dotted paths) for the preflight;
* ``restore`` writes the protocol's tree back as the config files: built in a temporary folder first, accepted only if
  it loads to EXACTLY the protocol's hash, then the current ``configs/`` is backed up and replaced. Nothing changes
  otherwise. It never edits the protocol, a run, or a result; it only makes the workspace's settings what the
  protocol already records.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from edgelab.core.config import CONFIG_FILES, ENV_PREFIX, config_hash, load_config

MAX_LISTED = 200
JSON_TYPES = (str, int, float, bool, type(None))


class ConfigRestoreError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self)}


def protocol_settings(svc, cfg_hash: str) -> dict | None:
    """The full settings tree recorded under ``cfg_hash`` (from a stored run), or None. Trusted only if it re-hashes."""
    rec = svc.store.run_record_for_config(cfg_hash)
    tree = (rec or {}).get("config")
    if not isinstance(tree, dict) or config_hash(tree) != cfg_hash:
        return None
    return tree


def _short(v: Any, n: int = 120) -> Any:
    if isinstance(v, JSON_TYPES):
        return v if not isinstance(v, str) or len(v) <= n else v[:n] + "…"
    s = json.dumps(v, sort_keys=True, default=str)
    return s if len(s) <= n else s[:n] + "…"


def diff(protocol: Any, current: Any, path: str = "") -> list[dict]:
    """Dotted-path differences: ``added`` = only in the current settings, ``removed`` = only in the protocol's."""
    if isinstance(protocol, Mapping) and isinstance(current, Mapping):
        out = []
        for k in sorted(set(protocol) | set(current), key=str):
            p = f"{path}.{k}" if path else str(k)
            if k not in current:
                out.append({"path": p, "change": "removed", "protocol": _short(protocol[k]), "current": None})
            elif k not in protocol:
                out.append({"path": p, "change": "added", "protocol": None, "current": _short(current[k])})
            else:
                out += diff(protocol[k], current[k], p)
        return out
    same = json.dumps(protocol, sort_keys=True, default=str) == json.dumps(current, sort_keys=True, default=str)
    return [] if same else [{"path": path, "change": "changed", "protocol": _short(protocol), "current": _short(current)}]


def env_overrides(environ: Mapping[str, str] | None = None) -> list[str]:
    return sorted(k for k in (os.environ if environ is None else environ) if k.startswith(ENV_PREFIX))


def mismatch_detail(svc, protocol_hash: str) -> dict:
    """What the preflight shows when the live settings hash differs from the protocol's."""
    live = svc._config_hash()
    out: dict[str, Any] = {"current_hash": live, "protocol_hash": protocol_hash,
                           "config_folder": str(Path(svc.root) / "configs"), "env_overrides": env_overrides()}
    tree = protocol_settings(svc, protocol_hash)
    if tree is None:
        out.update(differences=[], n_differences=None, restorable=False,
                   reason="no stored run carries the protocol's settings, so the difference cannot be shown")
        return out
    d = diff(tree, svc.cfg)
    out.update(differences=d[:MAX_LISTED], n_differences=len(d), restorable=True, reason=None)
    if out["env_overrides"]:
        out.update(restorable=False, reason="EDGELAB__ environment variables change the settings; remove them first")
    return out


def _file_keys(folder: Path) -> dict[str, str]:
    """top-level key -> config file that defines it (for the files present in ``folder``)."""
    out = {}
    for name in CONFIG_FILES:
        p = folder / name
        if p.is_file():
            for k in (yaml.safe_load(p.read_text(encoding="utf-8")) or {}):
                out.setdefault(k, name)
    return out


def plan_files(tree: Mapping, current: Path, defaults: Path) -> dict[str, dict]:
    """Split the protocol's merged tree back into the config files (current placement first, then the defaults')."""
    where = {**_file_keys(defaults), **_file_keys(current)}
    files: dict[str, dict] = {name: {} for name in CONFIG_FILES}
    for k, v in tree.items():
        if k not in where:
            raise ConfigRestoreError("UNPLACEABLE_KEY", f"cannot tell which config file holds the setting {k!r}")
        files[where[k]][k] = v
    return files


def _check_json_native(o: Any, path: str = "") -> None:
    if isinstance(o, Mapping):
        for k, v in o.items():
            _check_json_native(v, f"{path}.{k}" if path else str(k))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            _check_json_native(v, f"{path}[{i}]")
    elif not isinstance(o, JSON_TYPES):
        raise ConfigRestoreError("NOT_RESTORABLE", f"the current setting {path} is not plain data; restore by hand")


def restore(svc, protocol_hash: str, *, defaults: Path | None = None) -> dict:
    """Write the protocol's settings into ``<root>/configs`` (verified first; backup kept). Returns what changed."""
    from edgelab.runtime import default_config_dir
    root = Path(svc.root)
    cur = root / "configs"
    if svc._config_hash() == protocol_hash:
        return {"restored": False, "already_identical": True, "config_hash": protocol_hash}
    if env_overrides():
        raise ConfigRestoreError("ENV_OVERRIDES", "EDGELAB__ environment variables change the settings; remove them first")
    tree = protocol_settings(svc, protocol_hash)
    if tree is None:
        raise ConfigRestoreError("NO_RECORDED_SETTINGS", "no stored run carries the protocol's settings")
    _check_json_native(svc.cfg)
    changed_before = diff(tree, svc.cfg)
    files = plan_files(tree, cur, Path(defaults) if defaults else default_config_dir())
    tmp = Path(tempfile.mkdtemp(prefix="edgelab-config-restore-"))
    try:
        stage = tmp / "configs"
        shutil.copytree(cur, stage)                                  # extra files (web.yaml, search example) kept
        written = []
        for name, part in files.items():
            p = stage / name
            old = yaml.safe_load(p.read_text(encoding="utf-8")) if p.is_file() else None
            if (old or {}) == part:
                continue                                             # untouched files keep their comments
            p.write_text("# Restored to the research protocol's recorded settings (ADR-89).\n"
                         + yaml.safe_dump(part, sort_keys=False, allow_unicode=True, default_flow_style=False),
                         encoding="utf-8")
            written.append(name)
        got = config_hash(load_config(stage))
        if got != protocol_hash:
            raise ConfigRestoreError("HASH_MISMATCH", f"the rebuilt settings hash to {got[:12]}, not the protocol's "
                                     f"{protocol_hash[:12]}; nothing was changed")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = root / f"configs.backup-{stamp}"
        shutil.copytree(cur, backup)
        for name in written:
            shutil.copy2(stage / name, cur / name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"restored": True, "files": written, "backup": str(backup), "config_hash": protocol_hash,
            "differences_fixed": len(changed_before)}
