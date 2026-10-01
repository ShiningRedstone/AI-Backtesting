"""Feature cache.

Key = sha256 of everything the arrays depend on (see FeatureEngine.cache_key):
dataset id + content hash, base and computation timeframe, calendar fingerprint,
volume type, the canonical feature spec (id, version, params), the implementation hash
of the feature code, resolved session-window definitions, and the keys of all
dependencies (so a change in ATR invalidates every feature built on ATR).

Storage: <root>/<dataset_id>/<key[:2]>/<key>.npz (+ .json metadata), written atomically
(temp file + rename). Each entry records a sha256 of its arrays; loads re-verify it
(configurable), and a corrupted entry is discarded and recomputed, never trusted.
Layer 1 is an in-memory LRU; layer 2 is disk. Entries are immutable: a new definition
always produces a new key.

Thread safety (a background search job computes features while a web request may too): the
in-memory LRU and the stats are guarded by a private lock, taken only for those dictionary
operations (never for disk I/O or feature computation). On disk every file is written to a
unique temp file and moved into place with os.replace, and the metadata is written BEFORE the
arrays, so a reader that finds an .npz always finds its metadata. Two threads missing the same
key may both compute it; the values are deterministic, so the second write is identical.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from edgelab.core.identity import hash_arrays
from edgelab.core.logging import get_logger

log = get_logger("feature_cache")
CACHE_SCHEMA_VERSION = 1


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)


def _arrays_hash(arrays: dict) -> str:
    names = sorted(arrays)
    return hash_arrays(np.array(names, dtype=object).astype(str), *(arrays[k] for k in names))


class FeatureCache:
    def __init__(self, root: str | Path | None, verify: bool = True, memory_entries: int = 512):
        self.root = Path(root) if root else None
        self.verify = verify
        self.memory_entries = memory_entries
        self._mem: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.RLock()       # in-memory LRU + stats only
        self.stats = {"memory_hits": 0, "disk_hits": 0, "misses": 0, "writes": 0, "corrupt": 0}
        if self.root:
            self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, dataset_id: str, key: str) -> Path:
        return self.root / _safe(dataset_id) / key[:2] / f"{key}.npz"

    def _count(self, name: str) -> None:
        with self._lock:
            self.stats[name] += 1

    def get(self, dataset_id: str, key: str) -> tuple[dict | None, str | None]:
        with self._lock:                                      # lookup + LRU touch in one step
            hit = self._mem.get(key)
            if hit is not None:
                self._mem.move_to_end(key)
                self.stats["memory_hits"] += 1
                return hit, "memory"
        if self.root:
            p = self._path(dataset_id, key)
            if p.exists():
                try:
                    with np.load(p, allow_pickle=False) as z:
                        arrays = {k: z[k] for k in z.files}
                    meta = json.loads(p.with_suffix(".json").read_text())
                    if meta.get("key") != key or (self.verify and _arrays_hash(arrays) != meta["arrays_sha256"]):
                        raise ValueError("checksum/key mismatch")
                except Exception as exc:              # corrupted or partial entry: never trust it
                    self._count("corrupt")
                    log.event("cache_entry_discarded", severity="WARNING", error=str(exc), key=key)
                    p.unlink(missing_ok=True)
                    p.with_suffix(".json").unlink(missing_ok=True)
                else:
                    for a in arrays.values():
                        a.flags.writeable = False
                    self._remember(key, arrays)
                    self._count("disk_hits")
                    return arrays, "disk"
        self._count("misses")
        return None, None

    def put(self, dataset_id: str, key: str, arrays: dict, meta: dict) -> None:
        # feature outputs are float64; integer arrays (e.g. ns timestamps) keep int64 - never
        # squeezed through float64, which cannot represent all ns epoch values exactly
        arrays = {k: np.ascontiguousarray(v, dtype=np.int64 if np.asarray(v).dtype.kind in "iu" else np.float64)
                  for k, v in arrays.items()}
        for a in arrays.values():
            a.flags.writeable = False
        self._remember(key, arrays)
        if not self.root:
            return
        p = self._path(dataset_id, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        full_meta = {**meta, "key": key, "arrays_sha256": _arrays_hash(arrays),
                     "created_at": datetime.now(timezone.utc).isoformat(),
                     "n": int(len(next(iter(arrays.values())))) if arrays else 0,
                     "cache_schema": CACHE_SCHEMA_VERSION, "dataset_id": dataset_id}
        # metadata first, then arrays; each via its own unique temp file + atomic os.replace
        fd, tmp_meta = tempfile.mkstemp(dir=p.parent, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(full_meta, default=str, sort_keys=True))
            os.replace(tmp_meta, p.with_suffix(".json"))
        finally:
            if os.path.exists(tmp_meta):
                os.remove(tmp_meta)
        fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp.npz")
        os.close(fd)
        try:
            np.savez(tmp, **arrays)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        self._count("writes")

    def _remember(self, key: str, arrays: dict) -> None:
        with self._lock:
            self._mem[key] = arrays
            self._mem.move_to_end(key)
            while len(self._mem) > self.memory_entries:
                self._mem.popitem(last=False)

    def clear_memory(self) -> None:
        with self._lock:
            self._mem.clear()

    def entries(self, dataset_id: str | None = None) -> list[dict]:
        """Metadata of stored entries (Feature Lab 'cache status')."""
        if not self.root:
            return []
        base = self.root / _safe(dataset_id) if dataset_id else self.root
        out = []
        for m in sorted(base.rglob("*.json")):
            try:
                d = json.loads(m.read_text())
                d["bytes"] = m.with_suffix(".npz").stat().st_size if m.with_suffix(".npz").exists() else 0
                out.append(d)
            except (OSError, ValueError):
                continue
        return out

    def clear(self, dataset_id: str | None = None) -> None:
        self.clear_memory()
        if self.root:
            target = self.root / _safe(dataset_id) if dataset_id else self.root
            if target.exists():
                shutil.rmtree(target)
            self.root.mkdir(parents=True, exist_ok=True)


class MemoryFeatureCache(FeatureCache):
    """In-memory only, bounded by BYTES (ADR-77): holds the features of the causality check's truncated histories.
    Keys are the ordinary feature cache keys, which include the content hash of the (truncated) bars they were
    computed from, so an entry is only ever reused for byte-identical input."""

    def __init__(self, max_bytes: int):
        super().__init__(None, verify=False, memory_entries=1 << 30)
        self.max_bytes = int(max_bytes)
        self._sizes: dict[str, int] = {}
        self.bytes = 0

    def _remember(self, key: str, arrays: dict) -> None:
        size = int(sum(np.asarray(a).nbytes for a in arrays.values()))
        with self._lock:
            if key in self._mem:
                self.bytes -= self._sizes.pop(key, 0)
            if size > self.max_bytes:
                self._mem.pop(key, None)
                return
            self._mem[key] = arrays
            self._mem.move_to_end(key)
            self._sizes[key] = size
            self.bytes += size
            while self.bytes > self.max_bytes and self._mem:
                old, _ = self._mem.popitem(last=False)
                self.bytes -= self._sizes.pop(old, 0)
