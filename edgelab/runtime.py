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


UPDATE_CACHE_ENV = "EDGELAB_UPDATE_CACHE"


def update_cache_dir(environ: Mapping[str, str] | None = None, system: str | None = None) -> Path:
    """Where verified update downloads are staged: its own folder, never inside the installation, a
    workspace or the default data root. Windows: %LOCALAPPDATA%\\EdgeLab-Updater. ``EDGELAB_UPDATE_CACHE``
    overrides it (tests)."""
    env = os.environ if environ is None else environ
    if env.get(UPDATE_CACHE_ENV):
        return Path(env[UPDATE_CACHE_ENV]).expanduser()
    return user_data_root({k: v for k, v in env.items() if k != DATA_ROOT_ENV}, system).with_name(
        f"{APP_NAME}-Updater")


def install_dir() -> Path | None:
    """The folder holding the running EdgeLab executable (packaged only)."""
    return Path(sys.executable).resolve().parent if is_frozen() else None


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


# ------------------------------------------------------------------------------------ research workspace
# A research workspace is a folder with configs/ + data/ (the store, datasets, runs, strategy
# library, feature cache, prop simulations). The app REMEMBERS which one is selected in its own
# settings file, which lives OUTSIDE every workspace (never inside a research store). Selecting a
# workspace is a pointer change: nothing is copied, migrated, re-imported or deleted.
SETTINGS_ENV = "EDGELAB_SETTINGS"
SETTINGS_SCHEMA = 1
STORE_TABLES = ("datasets", "runs")


def settings_path(environ: Mapping[str, str] | None = None, system: str | None = None) -> Path:
    """The app settings file. Windows: %APPDATA%\\EdgeLab\\settings.json; macOS:
    ~/Library/Preferences/EdgeLab/settings.json; other: $XDG_CONFIG_HOME/edgelab/settings.json.
    ``EDGELAB_SETTINGS`` overrides it (tests, portable setups)."""
    env = os.environ if environ is None else environ
    if env.get(SETTINGS_ENV):
        return Path(env[SETTINGS_ENV]).expanduser()
    system = system or platform.system()
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    if system == "Windows":
        base = Path(env["APPDATA"]) if env.get("APPDATA") else home / "AppData" / "Roaming"
        return base / APP_NAME / "settings.json"
    if system == "Darwin":
        return home / "Library" / "Preferences" / APP_NAME / "settings.json"
    base = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config"
    return base / "edgelab" / "settings.json"


def load_settings(path: Path | None = None) -> dict:
    p = path or settings_path()
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(updates: Mapping, path: Path | None = None) -> dict:
    """Merge ``updates`` into the settings file (written atomically)."""
    p = path or settings_path()
    data = {**load_settings(p), **dict(updates), "schema": SETTINGS_SCHEMA}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    os.replace(tmp, p)
    return data


def resolve_workspace(data_root: str | Path | None = None, environ: Mapping[str, str] | None = None) -> tuple[Path | None, str]:
    """(workspace, source) by precedence: --data-root, EDGELAB_DATA_ROOT, the saved selection;
    (None, "none") when nothing was chosen (first run). The default location is used only once it
    has been selected: it is never picked silently."""
    env = os.environ if environ is None else environ
    if data_root:
        return Path(data_root).expanduser().resolve(), "--data-root"
    if env.get(DATA_ROOT_ENV):
        return Path(env[DATA_ROOT_ENV]).expanduser().resolve(), DATA_ROOT_ENV
    saved = load_settings(settings_path(env)).get("workspace")
    if saved:
        return Path(saved).expanduser().resolve(), "saved selection"
    return None, "none"


def _count_files(d: Path, pattern: str) -> int:
    return sum(1 for _ in d.glob(pattern)) if d.is_dir() else 0


def inspect_workspace(path: str | Path) -> dict:
    """READ-ONLY check of a candidate workspace: nothing is created, opened for writing or
    migrated. The SQLite store is opened with ``mode=ro``. Returns counts and every problem."""
    import sqlite3
    root = Path(path).expanduser()
    out: dict = {"path": str(root), "exists": root.is_dir(), "valid": False, "has_store": False,
                 "store_backend": None, "store_path": None, "writable": False, "demo": False,
                 "datasets": 0, "runs": 0, "strategies": 0, "prop_simulations": 0, "has_feature_cache": False,
                 "problems": []}
    if not root.is_dir():
        out["problems"].append("the folder does not exist")
        return out
    root = root.resolve()
    out["path"] = str(root)
    if is_frozen() and _inside(root, resource_dir()):
        out["problems"].append("the folder is inside the application bundle")
        return out
    out["demo"] = (root / "DEMO_WORKSPACE").exists()
    if out["demo"]:
        out["problems"].append("this is a synthetic demo workspace (start it with --demo)")
    if not (root / "configs").is_dir():
        out["problems"].append("no configs/ folder: this is not an EdgeLab research workspace")
        return out
    try:
        from edgelab.core.config import load_config
        cfg = load_config(root / "configs")
    except Exception as exc:                                   # noqa: BLE001 - reported to the user
        out["problems"].append(f"configs/ cannot be loaded: {exc}")
        return out
    st = cfg["storage"]
    data_root = root / st.get("root", "data")
    backend = st.get("backend", "auto")
    if backend == "duckdb":
        out["problems"].append(f"storage backend {backend!r} would use DuckDB; the desktop app uses the SQLite store "
                               "(set storage.backend: sqlite)")
    out["store_backend"] = "sqlite"
    db = data_root / st["sqlite_path"]
    out["store_path"] = str(db)
    out["data_root"] = str(data_root)
    out["writable"] = os.access(root, os.W_OK) and (not data_root.exists() or os.access(data_root, os.W_OK))
    if not out["writable"]:
        out["problems"].append("the workspace is not writable by this user")
    if db.is_file():
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            try:
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                missing = [t for t in STORE_TABLES if t not in tables]
                if missing:
                    out["problems"].append(f"the SQLite file has no EdgeLab tables {missing}")
                else:
                    out["has_store"] = True
                    out["datasets"] = con.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
                    out["runs"] = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            finally:
                con.close()
        except sqlite3.Error as exc:
            out["problems"].append(f"the SQLite store cannot be opened: {exc}")
    lib = data_root / "strategy_library"                        # the path Services uses
    out["strategies"] = _count_files(lib / "instances", "*.json")
    out["prop_simulations"] = _count_files(data_root / "prop_simulations", "PROP_*.json")
    out["has_feature_cache"] = (data_root / cfg.get("features", {}).get("cache_dir", "feature_cache")).is_dir()
    out["valid"] = not out["problems"]
    out["empty"] = out["valid"] and not out["has_store"] and out["strategies"] == 0
    return out


def create_workspace(path: str | Path) -> dict:
    """Create a NEW workspace in an empty or missing folder (bundled default configs copied).
    Refuses a folder that already has content, so an existing workspace is never re-initialised."""
    root = Path(path).expanduser()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError(f"{root} already exists and is not empty; choose an empty folder, or open it as an "
                         "existing workspace")
    init_workspace(root)
    return inspect_workspace(root)


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
    if not m:
        return "development"
    if m.get("build_number"):
        return f"packaged {m['app_version']} build {m['build_number']} of {m.get('channel')} ({m['build_id']})"
    return f"packaged {m['app_version']} build {m['build_id']}"


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
