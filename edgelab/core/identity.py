"""Identity & reproducibility helpers: stable hashes, code version, environment."""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

PACKAGE_DIR = Path(__file__).resolve().parents[1]


def stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def hash_obj(obj: Any, length: int | None = None) -> str:
    h = hashlib.sha256(stable_json(obj).encode()).hexdigest()
    return h[:length] if length else h


def hash_arrays(*arrays: np.ndarray) -> str:
    """Hash the exact bytes (dtype + shape + values) of numpy arrays."""
    h = hashlib.sha256()
    for a in arrays:
        a = np.ascontiguousarray(a)
        h.update(str(a.dtype).encode())
        h.update(str(a.shape).encode())
        if a.dtype.hasobject or not a.size:
            h.update(a.tobytes())
        else:                                  # ADR-91: the same bytes without a full copy (memoryview of the buffer)
            h.update(memoryview(a.reshape(-1).view(np.uint8)))
    return h.hexdigest()


def source_hash() -> str:
    """Hash of every .py file in the package: detects uncommitted code changes. A packaged build
    has no source files on disk: it reports the hash computed from the sources at build time
    (edgelab_build.json) and refuses to run without it (see edgelab.runtime)."""
    from edgelab import runtime
    m = runtime.build_manifest()
    if m is not None:
        return m["source_sha256"]
    h = hashlib.sha256()
    for p in sorted(PACKAGE_DIR.rglob("*.py")):
        h.update(str(p.relative_to(PACKAGE_DIR)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def git_commit() -> str | None:
    from edgelab import runtime
    m = runtime.build_manifest()
    if m is not None:
        return m["git_commit"]
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PACKAGE_DIR.parent,
                             capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return None
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def code_version() -> dict:
    """Development: git commit + source hash (unchanged). Packaged: the same two values from the
    build manifest, plus the build id and app version that distinguish application builds."""
    from edgelab import runtime
    m = runtime.build_manifest()
    if m is None:
        return {"git_commit": git_commit(), "source_sha256": source_hash()}
    return {"git_commit": m["git_commit"], "source_sha256": m["source_sha256"], "packaged": True,
            "app_version": m["app_version"], "build_id": m["build_id"],
            "git_tracked_changes": m.get("git_tracked_changes")}


def environment_fingerprint() -> dict:
    import pandas as pd
    fp = {"python": platform.python_version(), "platform": platform.platform(),
          "numpy": np.__version__, "pandas": pd.__version__}
    for mod in ("duckdb", "numba", "polars", "pyarrow"):
        try:
            fp[mod] = __import__(mod).__version__
        except ImportError:
            fp[mod] = None
    return fp
