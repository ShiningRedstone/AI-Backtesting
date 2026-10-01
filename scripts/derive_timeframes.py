"""Add derived timeframes to an existing ROOT dataset by replaying its recorded import (existing import machinery only).

Derived timeframes are created only by the import pipeline (``derive_timeframes``: session-anchored resample of the
import's cleaned bars, BID and observed ASK OHLC first/max/min/last, re-validated, ``parent_dataset_id`` = the root). This
script replays the root's import with the options recorded in its manifest (``source_detail.options``) and the requested
timeframes added. The same source file gives the same bars, the same content hash and therefore the SAME root dataset id
(kept, not re-stored); existing children are recomputed identically and kept; missing children are created.

Safety: read-only unless ``--apply``; refuses if the dataset is not a root, if the recorded source file is missing or its
SHA-256 differs from the manifest, or if the replay would produce a different root id. Features are not built.

    .\\.venv\\Scripts\\python.exe scripts\\derive_timeframes.py --root <workspace> <ROOT_1M_DATASET_ID> 15m 30m 60m
    .\\.venv\\Scripts\\python.exe scripts\\derive_timeframes.py --root <workspace> <ROOT_1M_DATASET_ID> 15m 30m 60m --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edgelab.data.schema import timeframe_minutes  # noqa: E402
from edgelab.services import Services  # noqa: E402


class DeriveRefused(RuntimeError):
    pass


def plan(svc: Services, root_id: str, timeframes: list[str], source_file: str | None = None) -> dict:
    from edgelab.data.importer import file_sha256
    m = svc.store.get_manifest(root_id)
    if m.parent_dataset_id:
        raise DeriveRefused(f"{root_id} is derived from {m.parent_dataset_id}; give the ROOT dataset")
    opts = dict((m.source_detail or {}).get("options") or {})
    if not opts:
        raise DeriveRefused(f"{root_id} has no recorded import options; it cannot be replayed")
    f = Path(source_file or opts["file"])
    if not f.exists():
        raise DeriveRefused(f"recorded source file not found: {f} (pass --source-file to point at the same file)")
    sha = file_sha256(f)
    if sha != m.source_file_sha256:
        raise DeriveRefused(f"{f} SHA-256 {sha[:12]}... differs from the manifest's {m.source_file_sha256[:12]}...; "
                            "a different file would create a different dataset")
    base = timeframe_minutes(m.timeframe)
    for tf in timeframes:
        dm = timeframe_minutes(tf)
        if dm <= base or dm % base:
            raise DeriveRefused(f"{tf} is not a higher multiple of {m.timeframe}")
    old = list(opts.get("derive_timeframes") or [])
    want = sorted({f"{timeframe_minutes(t)}m" for t in old + list(timeframes)}, key=timeframe_minutes)
    children = {d["timeframe"]: d["dataset_id"] for d in svc.list_datasets() if d.get("parent_dataset_id") == root_id}
    return {"root": root_id, "source_file": str(f), "source_sha256": sha, "recorded_derive": old,
            "derive": want, "existing_children": children,
            "missing": [t for t in want if t not in children],
            "options": {**opts, "file": str(f), "derive_timeframes": want, "build_features": False}}


def apply(svc: Services, p: dict) -> dict:
    r = svc.import_file(p["options"])
    if r["dataset_id"] != p["root"]:
        raise DeriveRefused(f"the replay produced {r['dataset_id']}, not {p['root']}; nothing derived belongs to "
                            "the root (inspect the source and options)")
    children = {d["timeframe"]: d["dataset_id"] for d in svc.list_datasets() if d.get("parent_dataset_id") == p["root"]}
    return {"root": p["root"], "root_created": r["created"], "children": children,
            "created": [t for t in p["missing"] if t in children], "warnings": r.get("warnings", [])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="workspace root (configs/ + data)")
    ap.add_argument("dataset_id", help="the ROOT (e.g. 1m) dataset id")
    ap.add_argument("timeframes", nargs="+", help="timeframes to add, e.g. 15m 30m 60m")
    ap.add_argument("--source-file", help="the same source file, if it moved (SHA-256 must match)")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    svc = Services(root=a.root)
    try:
        p = plan(svc, a.dataset_id, a.timeframes, a.source_file)
        print(json.dumps({k: v for k, v in p.items() if k != "options"}, indent=1))
        if a.apply:
            print(json.dumps(apply(svc, p), indent=1))
        else:
            print("check only - rerun with --apply to derive")
    except DeriveRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    finally:
        svc.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
