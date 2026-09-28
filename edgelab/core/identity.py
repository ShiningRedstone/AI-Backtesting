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
        h.update(a.tobytes())
    return h.hexdigest()


def source_hash() -> str:
    """Hash of every .py file in the package: detects uncommitted code changes."""
    h = hashlib.sha256()
    for p in sorted(PACKAGE_DIR.rglob("*.py")):
        h.update(str(p.relative_to(PACKAGE_DIR)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PACKAGE_DIR.parent,
                             capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return None
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def code_version() -> dict:
    return {"git_commit": git_commit(), "source_sha256": source_hash()}


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
