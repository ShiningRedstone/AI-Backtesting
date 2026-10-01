"""Strategy families, instances and lineage.

Family   = the conceptual hypothesis ("NY opening-range breakout").
Instance = one concrete, canonical DSL definition, identified by its logic hash (strategy_id).
Lineage  = how an instance came to exist: from a user, from a Mode A variation of a parent,
           or from a Mode B proposal - with the exact parameter changes.

The library is a directory of JSON files (one per instance), easy to inspect, diff and back up:
    <root>/instances/<STRATEGY_ID>.json   canonical definition + identity + lineage records
    <root>/batches/<BATCH_ID>.json        generation batch records (reproducibility)
    <root>/archived/<STRATEGY_ID>.json    archived instances (reversible; still loadable, so
                                          lineage that points at them never breaks)
An instance file is written once; later lineage records for the same logic are appended
(the same logic can be reached from two parents - both paths are kept).

    <root>/index.json                     a derived listing index (never authoritative)
The index holds the listing rows, batch summaries and batch membership so listings do not parse
every file. It carries a fingerprint of the library files (name, size, mtime, inode per file) and
a checksum of its own payload; a missing, stale, corrupted or foreign-version index is rebuilt
from the directory scan, which stays the source of truth (`verify_index` compares the two).
Batch records: Mode A variation batches (no `kind` key, historically) and Mode B proposal
batches (`kind: proposal`).
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

GENERATION_METHODS = ("user", "manual_edit", "duplicate", "mode_a_variation", "mode_b_proposal",
                      "mode_b_modification",          # AI Discovery: a new version of an existing strategy
                      "factory_variant")              # strategy factory manifest (ADR-58)
INDEX_VERSION = 1
BATCH_KINDS = ("variation", "proposal")


def batch_kind(record: Mapping) -> str:
    return record.get("kind", "variation")


@dataclass(frozen=True)
class StrategyFamily:
    family_id: str
    name: str = ""
    hypothesis: str = ""
    category: str = ""

    @classmethod
    def from_definition(cls, raw: Mapping) -> "StrategyFamily":
        f = raw.get("family") or {}
        return cls(f.get("id", "unassigned"), f.get("name", ""), f.get("hypothesis", ""), f.get("category", ""))


@dataclass(frozen=True)
class Change:
    parameter: str
    old: Any
    new: Any
    category: str = "parameter"


@dataclass
class LineageRecord:
    strategy_id: str
    logic_hash: str
    definition_hash: str
    family_id: str
    generation_method: str
    parent_strategy_id: str | None = None
    changes: list = field(default_factory=list)            # list[Change]
    generation_parameters: dict = field(default_factory=dict)
    generation_batch_id: str | None = None
    generation_timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    versions: dict = field(default_factory=dict)            # dsl / compiler / feature / config

    def __post_init__(self):
        if self.generation_method not in GENERATION_METHODS:
            raise ValueError(f"generation_method must be one of {GENERATION_METHODS}")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["changes"] = [asdict(c) if isinstance(c, Change) else dict(c) for c in self.changes]
        return d


def _atomic_write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True, default=str)
    os.replace(tmp, path)


class StrategyLibrary:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.last_index_event: str | None = None     # "used" or "rebuilt:<reason>" (diagnostics)
        (self.root / "instances").mkdir(parents=True, exist_ok=True)
        (self.root / "batches").mkdir(parents=True, exist_ok=True)
        (self.root / "archived").mkdir(parents=True, exist_ok=True)

    def _path(self, strategy_id: str) -> Path:
        return self.root / "instances" / f"{strategy_id}.json"

    def _archived_path(self, strategy_id: str) -> Path:
        return self.root / "archived" / f"{strategy_id}.json"

    def save(self, definition: Mapping, identity: Mapping, lineage: LineageRecord) -> bool:
        """Store an instance (canonical definition). Returns False if the logic already existed;
        the new lineage record is still appended (and the first definition is kept)."""
        p = self._path(identity["strategy_id"])
        rec = lineage.to_dict()
        if self._archived_path(identity["strategy_id"]).exists():
            raise ValueError(f"{identity['strategy_id']} is archived; restore it before saving it again")
        if p.exists():
            doc = json.loads(p.read_text())
            if doc["logic_hash"] != identity["logic_hash"]:
                raise ValueError(f"id collision for {identity['strategy_id']}")
            key = lambda r: (r.get("parent_strategy_id"), r.get("generation_batch_id"),   # noqa: E731
                             r.get("generation_method"), json.dumps(r.get("changes"), sort_keys=True, default=str))
            if key(rec) not in {key(r) for r in doc["lineage"]}:
                doc["lineage"].append(rec)
                _atomic_write(p, doc)
            return False
        _atomic_write(p, {**dict(identity), "definition": dict(definition), "lineage": [rec],
                          "family_id": lineage.family_id, "name": definition.get("name")})
        return True

    def load(self, strategy_id: str) -> dict:
        """Active or archived instance (archived ones carry ``archived: true``)."""
        p = self._path(strategy_id)
        if p.exists():
            return json.loads(p.read_text())
        a = self._archived_path(strategy_id)
        if a.exists():
            return {**json.loads(a.read_text()), "archived": True}
        raise KeyError(strategy_id)

    def exists(self, strategy_id: str) -> bool:
        return self._path(strategy_id).exists() or self._archived_path(strategy_id).exists()

    @staticmethod
    def _row(d: dict, archived: bool) -> dict:
        first = d["lineage"][0]
        definition = d.get("definition") or {}
        return {"strategy_id": d["strategy_id"], "name": d.get("name"), "family_id": d.get("family_id"),
                "generation_method": first["generation_method"],
                "parent_strategy_id": first.get("parent_strategy_id"),
                "generation_batch_id": first.get("generation_batch_id"),
                "created_at": first.get("generation_timestamp"),
                "timeframe": definition.get("timeframe"),
                "n_parameters": len(definition.get("parameters") or {}),
                "n_lineage_records": len(d["lineage"]), "archived": archived}

    def list(self, family_id: str | None = None, include_archived: bool = False) -> list[dict]:
        idx = self._index()
        rows = idx["instances"] + (idx["archived"] if include_archived else [])
        return [dict(r) for r in rows if family_id is None or r.get("family_id") == family_id]

    # ------------------------------------------------------------------ index
    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def _fingerprint(self) -> str:
        from edgelab.core.identity import hash_obj
        entries = []
        for sub in ("instances", "archived", "batches"):
            for p in sorted((self.root / sub).glob("*.json")):
                st = p.stat()
                entries.append([sub, p.name, st.st_size, st.st_mtime_ns, st.st_ino])
        return hash_obj(entries)

    @staticmethod
    def _batch_row(b: dict) -> dict:
        if batch_kind(b) == "variation":        # the pre-index listing row, unchanged
            return {"batch_id": b["batch_id"], "created_at": b.get("created_at"),
                    "base_strategy_id": b["base"]["strategy_id"], "base_name": b["base"]["definition"].get("name"),
                    "spec_name": b["spec"].get("name"), "mode": b["spec"].get("mode"),
                    "combinations": b.get("combinations"), "generated": b.get("generated"),
                    "duplicates": len(b.get("duplicates", [])), "same_as_base": len(b.get("same_as_base", []))}
        return {"batch_id": b["batch_id"], "kind": batch_kind(b), "created_at": b.get("created_at"),
                "source_kind": (b.get("source") or {}).get("kind"), "n_accepted": b.get("n_accepted"),
                "n_rejected": b.get("n_rejected"), "children": len(b.get("children") or [])}

    def _scan(self) -> dict:
        """The authoritative directory scan, in the shape the index stores."""
        out: dict = {"instances": [], "archived": [], "batches": [], "batch_members": {}}
        for sub, archived in (("instances", False), ("archived", True)):
            for p in sorted((self.root / sub).glob("*.json")):
                d = json.loads(p.read_text())
                out[sub].append(self._row(d, archived))
                for r in d["lineage"]:
                    b = r.get("generation_batch_id")
                    if b and d["strategy_id"] not in out["batch_members"].setdefault(b, []):
                        out["batch_members"][b].append(d["strategy_id"])
        for p in sorted((self.root / "batches").glob("*.json")):
            b = json.loads(p.read_text())
            out["batches"].append({"kind": batch_kind(b), "row": self._batch_row(b)})
        return out

    def _index(self) -> dict:
        """The index payload; rebuilt from `_scan` when missing, stale, corrupted or of another version."""
        from edgelab.core.identity import hash_obj
        fp = self._fingerprint()
        try:
            doc = json.loads(self.index_path.read_text())
            if doc.get("index_version") != INDEX_VERSION:
                reason = "version"
            elif doc.get("checksum") != hash_obj(doc["payload"]):
                reason = "corrupt"
            elif doc.get("fingerprint") != fp:
                reason = "stale"
            else:
                self.last_index_event = "used"
                return doc["payload"]
        except FileNotFoundError:
            reason = "missing"
        except (ValueError, KeyError, TypeError, AttributeError):
            reason = "corrupt"
        payload = self._scan()
        try:
            _atomic_write(self.index_path, {"index_version": INDEX_VERSION, "fingerprint": fp,
                                            "checksum": hash_obj(payload), "payload": payload})
        except OSError:
            pass                                # an unwritable index only costs speed
        self.last_index_event = f"rebuilt:{reason}"
        return payload

    def verify_index(self) -> dict:
        """Compare the index with the authoritative directory scan."""
        idx, scan = self._index(), self._scan()
        diff = sorted(k for k in scan if idx.get(k) != scan[k])
        return {"consistent": not diff, "differences": diff}

    def batch_members(self, batch_id: str) -> list[str]:
        """Strategies (active or archived) with a lineage record from this generation batch."""
        return list(self._index()["batch_members"].get(batch_id, []))

    def archive(self, strategy_id: str) -> None:
        """Reversible removal from the active library (never deletes files)."""
        p = self._path(strategy_id)
        if not p.exists():
            raise KeyError(strategy_id)
        os.replace(p, self._archived_path(strategy_id))

    def restore(self, strategy_id: str) -> None:
        a = self._archived_path(strategy_id)
        if not a.exists():
            raise KeyError(strategy_id)
        os.replace(a, self._path(strategy_id))

    def list_batches(self, kind: str | None = None) -> list[dict]:
        """Batch listing rows. Default (None) = variation batches only, exactly as before proposal
        batches were stored; `kind="proposal"` lists Mode B proposal batches."""
        if kind is not None and kind not in BATCH_KINDS:
            raise ValueError(f"kind must be one of {BATCH_KINDS} (or omitted)")
        want = kind or "variation"
        return [dict(b["row"]) for b in self._index()["batches"] if b["kind"] == want]

    def families(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in self.list():
            counts[s["family_id"]] = counts.get(s["family_id"], 0) + 1
        return dict(sorted(counts.items()))

    def children(self, strategy_id: str) -> list[str]:
        return sorted(s["strategy_id"] for s in self._all_lineage() if s["parent"] == strategy_id)

    def ancestry(self, strategy_id: str) -> list[dict]:
        """Chain from this instance back to its root (first lineage record at each step)."""
        chain, seen, cur = [], set(), strategy_id
        while cur and cur not in seen and self.exists(cur):
            seen.add(cur)
            rec = self.load(cur)["lineage"][0]
            chain.append({"strategy_id": cur, "generation_method": rec["generation_method"],
                          "parent_strategy_id": rec.get("parent_strategy_id"), "changes": rec.get("changes", [])})
            cur = rec.get("parent_strategy_id")
        return chain

    def _all_lineage(self) -> list[dict]:
        out = []
        for p in list((self.root / "instances").glob("*.json")) + list((self.root / "archived").glob("*.json")):
            d = json.loads(p.read_text())
            for r in d["lineage"]:
                out.append({"strategy_id": d["strategy_id"], "parent": r.get("parent_strategy_id")})
        return out

    def save_batch(self, batch: Mapping) -> None:
        _atomic_write(self.root / "batches" / f"{batch['batch_id']}.json", dict(batch))

    def load_batch(self, batch_id: str) -> dict:
        return json.loads((self.root / "batches" / f"{batch_id}.json").read_text())
