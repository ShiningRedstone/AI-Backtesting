"""Frontend bundle integrity: recompute the source hash exactly as web/build.mjs does, so a test
(or the dashboard) can tell whether edgelab/web/static was built from the current sources."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WEB = REPO / "web"
STATIC = Path(__file__).parent / "static"


def source_hash(web: Path = WEB) -> str:
    h = hashlib.sha256()

    def walk(d: Path) -> None:
        for p in sorted(d.iterdir(), key=lambda x: x.name):
            if p.is_dir():
                walk(p)
            else:
                h.update(p.relative_to(web).as_posix().encode())
                h.update(p.read_bytes())

    walk(web / "src")
    for f in ("index.html", "build.mjs", "package.json", "tsconfig.json"):
        h.update(f.encode())
        h.update((web / f).read_bytes())
    return h.hexdigest()


def bundle_status() -> dict:
    info_path = STATIC / "build-info.json"
    if not info_path.exists():
        return {"built": False, "up_to_date": False}
    info = json.loads(info_path.read_text())
    current = source_hash() if (WEB / "src").is_dir() else None
    return {"built": True, **info, "current_source_sha256": current,
            "up_to_date": current is None or current == info.get("source_sha256")}
