"""Runtime locations and build identity: the ONE place that knows whether EdgeLab runs from a
repository checkout (development) or from a packaged (PyInstaller) build.

Three kinds of location, never mixed:

* bundled application resources (read-only): the ``edgelab`` package, the built frontend
  (``edgelab/web/static``), default ``configs/`` and ``strategies/fixtures/``. Development: the
  repository. Packaged: the bundle directory (``sys._MEIPASS``).
* user configuration + user data (persistent, writable): one workspace root holding ``configs/``
  (copied once from the bundled defaults, never overwritten), ``data/`` (store, datasets, runs,
  strategy library, feature cache, prop simulations, ``data/import``), ``reports/`` and ``logs/``.
  Packaged default: ``%LOCALAPPDATA%\\EdgeLab``. Development keeps using the repository as its root.
* build identity: in a packaged build, source files are not on disk, so every code hash the
  research lineage records (package source hash, compiler source hash, feature implementation
  hashes, git commit) is read from ``edgelab_build.json``, generated from the real sources at build
  time by :func:`generate_build_manifest`. A packaged build without that manifest refuses to run
  research (reproducibility is never silently degraded; nothing is faked).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Mapping

APP_NAME = "EdgeLab"
BUILD_MANIFEST = "edgelab_build.json"
WORKSPACE_MARKER = "logs/edgelab_workspace.json"   # under logs/ (git-ignored in a checkout)
DATA_ROOT_ENV = "EDGELAB_DATA_ROOT"
MANIFEST_SCHEMA = 1


class BuildManifestError(RuntimeError):
    """A packaged build lacks (or has an invalid) build manifest: code identity is unknown."""


# ------------------------------------------------------------------------------------ mode
def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def package_dir() -> Path:
    """The ``edgelab`` package directory (packaged: inside the bundle)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS")) / "edgelab"
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Bundled read-only resources: repository root (dev) or bundle directory (packaged)."""
    return Path(getattr(sys, "_MEIPASS")) if is_frozen() else package_dir().parent


def static_dir() -> Path:
    return package_dir() / "web" / "static"


def default_config_dir() -> Path:
    return resource_dir() / "configs"


def fixtures_dir() -> Path:
    return resource_dir() / "strategies" / "fixtures"


# ------------------------------------------------------------------------------------ user data
def user_data_root(environ: Mapping[str, str] | None = None, system: str | None = None) -> Path:
    """Persistent user workspace for the packaged app. ``EDGELAB_DATA_ROOT`` overrides it.
    Windows: %LOCALAPPDATA%\\EdgeLab; macOS: ~/Library/Application Support/EdgeLab;
    other: $XDG_DATA_HOME/EdgeLab (default ~/.local/share/EdgeLab)."""
    env = os.environ if environ is None else environ
    if env.get(DATA_ROOT_ENV):
        return Path(env[DATA_ROOT_ENV]).expanduser()
    system = system or platform.system()
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    if system == "Windows":
        base = Path(env["LOCALAPPDATA"]) if env.get("LOCALAPPDATA") else home / "AppData" / "Local"
    elif system == "Darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else home / ".local" / "share"
    return base / APP_NAME


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def init_workspace(root: str | Path, defaults: str | Path | None = None) -> dict:
    """Create the persistent workspace if needed. Bundled default configs are COPIED once into
    ``root/configs``; an existing ``configs/`` is never touched. Only configs are copied (no data,
    no repository files). Refuses a root inside the read-only bundle."""
    root = Path(root).expanduser().resolve()
    defaults = Path(defaults) if defaults else default_config_dir()
    if is_frozen() and _inside(root, resource_dir()):
        raise ValueError(f"the data root {root} is inside the application bundle; user data must live outside it")
    root.mkdir(parents=True, exist_ok=True)
    created_configs = False
    if not (root / "configs").exists():
        if not defaults.is_dir():
            raise FileNotFoundError(f"bundled default configs not found at {defaults}")
        shutil.copytree(defaults, root / "configs")
        created_configs = True
    for d in ("data/import", "reports", "logs"):
        (root / d).mkdir(parents=True, exist_ok=True)
    marker = root / WORKSPACE_MARKER
    if not marker.exists():
        marker.write_text(json.dumps({"app": APP_NAME, "created_at": datetime.now(timezone.utc).isoformat(),
                                      "created_by": build_label()}, indent=1) + "\n")
    return {"root": str(root), "configs_created": created_configs,
            "config_differences": config_differences(root / "configs", defaults)}


def config_differences(user_configs: Path, defaults: Path) -> list[str]:
    """Bundled default config files whose content differs from (or is absent in) the user's copy.
    Reported, never auto-applied: user configuration is the user's."""
    if not defaults.is_dir() or not user_configs.is_dir():
        return []
    out = []
    for p in sorted(defaults.rglob("*")):
        if p.is_file():
            rel = p.relative_to(defaults)
            q = user_configs / rel
            if not q.exists() or q.read_bytes() != p.read_bytes():
                out.append(rel.as_posix())
    return out


# ------------------------------------------------------------------------------------ build manifest
@lru_cache(maxsize=1)
def _load_manifest(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise BuildManifestError(f"packaged build has no {BUILD_MANIFEST}; code identity unknown - rebuild "
                                 "with the build script (research lineage would otherwise be unverifiable)")
    m = json.loads(p.read_text())
    need = {"schema", "app_version", "build_id", "git_commit", "source_sha256", "compiler_source_sha256",
            "feature_impl_hashes"}
    if m.get("schema") != MANIFEST_SCHEMA or not need <= set(m):
        raise BuildManifestError(f"{p} is not a valid EdgeLab build manifest (schema {MANIFEST_SCHEMA})")
    return m


def build_manifest() -> dict | None:
    """The bundled build manifest (packaged), or None in development (sources are on disk)."""
    if not is_frozen():
        return None
    return _load_manifest(str(resource_dir() / BUILD_MANIFEST))


def build_label() -> str:
    m = build_manifest() if is_frozen() else None
    return f"packaged {m['app_version']} build {m['build_id']}" if m else "development"


def generate_build_manifest(repo: str | Path | None = None, extra: Mapping | None = None) -> dict:
    """Run at BUILD time from real sources (development mode only). Every hash is computed by the
    same functions the running code uses in development, so a packaged build records exactly what
    a development run of the same sources would record."""
    if is_frozen():
        raise BuildManifestError("the build manifest is generated from sources, not inside a packaged build")
    import edgelab
    from edgelab.core.identity import git_commit, source_hash
    from edgelab.features.spec import all_defs
    from edgelab.strategy.compiler import compiler_source_hash
    repo = Path(repo) if repo else resource_dir()
    feats = {fd.feature_id: fd.impl_hash for fd in all_defs()}
    info_path = static_dir() / "build-info.json"
    frontend = json.loads(info_path.read_text()) if info_path.exists() else {}
    dirty = None
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo,
                             capture_output=True, text=True, timeout=10)
        dirty = bool(out.stdout.strip()) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        pass
    body = {"schema": MANIFEST_SCHEMA, "app": APP_NAME, "app_version": edgelab.__version__,
            "git_commit": git_commit(), "git_tracked_changes": dirty,
            "source_sha256": source_hash(), "compiler_source_sha256": compiler_source_hash(),
            "feature_impl_hashes": feats, "frontend_source_sha256": frontend.get("source_sha256"),
            "python": platform.python_version(), "build_platform": platform.platform(),
            **(dict(extra) if extra else {})}
    identity_keys = ("app_version", "git_commit", "source_sha256", "compiler_source_sha256",
                     "feature_impl_hashes", "frontend_source_sha256", "python", "pyinstaller")
    body["build_id"] = hashlib.sha256(json.dumps({k: body.get(k) for k in identity_keys},
                                                 sort_keys=True).encode()).hexdigest()[:16].upper()
    body["built_at"] = datetime.now(timezone.utc).isoformat()
    return body


def runtime_info() -> dict:
    """What the app reports about itself (dashboard, /api/status, smoke tests)."""
    m = build_manifest() if is_frozen() else None
    return {"packaged": is_frozen(), "build": build_label(), "build_id": m["build_id"] if m else None,
            "resource_dir": str(resource_dir()), "static_dir": str(static_dir())}
