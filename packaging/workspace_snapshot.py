"""READ-ONLY before/after check of a research workspace (e.g. around switching to it in EdgeLab).

    python packaging\\workspace_snapshot.py C:\\Users\\you\\Documents\\AI-Backtesting --out before.json
    ... use EdgeLab.exe: select the workspace, browse datasets / Strategy Lab / Results / Prop ...
    python packaging\\workspace_snapshot.py C:\\Users\\you\\Documents\\AI-Backtesting --compare before.json

Records the SQLite store's dataset/run counts (opened read-only) and a SHA-256 of every file under
data\\ (store, datasets, run records, strategy instances, prop simulations). Compare reports any
difference. Expected after only browsing: none, except the derived strategy-library index
(data\\strategy_library\\index.json), which the library rebuilds when strategies are listed, and
feature-cache files if a feature was computed. New runs/simulations appear only if you created them.
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path


def snapshot(root: Path) -> dict:
    db = root / "data" / "edgelab.sqlite"
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("datasets", "runs")}
        manifests = dict(con.execute("SELECT dataset_id, content_hash FROM datasets").fetchall())
        runs = dict(con.execute("SELECT run_id, trades_hash FROM runs").fetchall())
    finally:
        con.close()
    files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted((root / "data").rglob("*")) if p.is_file()}
    return {"root": str(root.resolve()), "counts": counts, "dataset_content_hashes": manifests,
            "run_trades_hashes": runs, "files": files}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--out")
    g.add_argument("--compare")
    a = ap.parse_args()
    now = snapshot(Path(a.root))
    print(f"workspace {now['root']}: datasets {now['counts']['datasets']}, runs {now['counts']['runs']}, "
          f"{len(now['files'])} files under data\\")
    if a.out:
        Path(a.out).write_text(json.dumps(now, indent=1))
        print(f"saved {a.out}")
        return 0
    before = json.loads(Path(a.compare).read_text())
    ok = True
    for k in ("counts", "dataset_content_hashes", "run_trades_hashes"):
        same = before[k] == now[k]
        ok &= same
        print(("  OK   " if same else "  DIFF ") + f"{k} unchanged")
    changed = sorted(f for f in set(before["files"]) | set(now["files"]) if before["files"].get(f) != now["files"].get(f))
    derived = [f for f in changed if f.endswith("strategy_library/index.json") or "/feature_cache/" in f]
    other = [f for f in changed if f not in derived]
    print(f"  files changed: {len(changed)} (derived index/cache: {len(derived)}; other: {len(other)})")
    for f in other:
        print(f"    {f}")
    ok &= not other
    print("==== WORKSPACE UNCHANGED" if ok else "==== WORKSPACE CHANGED (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
