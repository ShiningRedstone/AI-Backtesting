"""Frontend bundle integrity: recompute the source hash exactly as web/build.mjs does, so a test
(or the dashboard) can tell whether edgelab/web/static was built from the current sources."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from edgelab.runtime import is_frozen, resource_dir, static_dir

REPO = resource_dir()
WEB = REPO / "web"                       # frontend sources: present in a checkout, absent when packaged
STATIC = static_dir()


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
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:  # e.g. merge-conflict markers left in the file
        return {"built": False, "up_to_date": False,
                "error": f"edgelab/web/static/build-info.json is unreadable ({exc}); restore it with "
                         "`git restore --source=HEAD -- edgelab/web/static` or run `npm run build` in web/"}
    current = source_hash() if (WEB / "src").is_dir() and not is_frozen() else None
    import edgelab
    version_ok = info.get("app_version") == edgelab.__version__
    return {"built": True, **info, "current_source_sha256": current, "packaged": is_frozen(),
            "backend_version": edgelab.__version__, "version_matches": version_ok,
            "up_to_date": (current is None or current == info.get("source_sha256")) and version_ok}
