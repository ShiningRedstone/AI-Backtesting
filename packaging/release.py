"""Prepare the update artifacts of a GitHub Release from a finished build (does NOT publish).

    python packaging/release.py [--dist dist/EdgeLab] [--out dist/release] [--notes NOTES.md] [--allow-dirty]

Produces in --out:
  EdgeLab-<version>-<platform>.zip   the packaged application folder (top-level folder "EdgeLab/")
  edgelab-release.json               the machine-readable release manifest the updater reads
  SHA256SUMS.txt                     human-checkable checksums
and prints the `gh release create` command that would publish them. Refuses when the versions of
the backend (edgelab.__version__), the packaged build manifest and the packaged frontend bundle
disagree, or (without --allow-dirty) when the build came from uncommitted tracked changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def _find(folder: Path, *rel: str) -> Path | None:
    for base in (folder, folder / "_internal"):
        p = base.joinpath(*rel)
        if p.is_file():
            return p
    return None


def build_zip(app: Path, out: Path) -> None:
    """Deterministic member order; every member under the top-level folder name of ``app``."""
    files = sorted(p for p in app.rglob("*") if p.is_file())
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in files:
            if p.is_symlink():
                raise SystemExit(f"refusing to package a symbolic link: {p}")
            z.write(p, (Path(app.name) / p.relative_to(app)).as_posix())


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None) -> int:
    import edgelab
    from edgelab.updater.apply import exe_name
    from edgelab.updater.core import MANIFEST_NAME, MANIFEST_SCHEMA, current_platform, validate_manifest
    from edgelab.updater.source import DirectoryReleaseSource
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=str(REPO / "dist" / "EdgeLab"))
    ap.add_argument("--out", default=str(REPO / "dist" / "release"))
    ap.add_argument("--notes", help="release notes file (Markdown/text)")
    ap.add_argument("--platform", default=current_platform(), help="artifact platform tag (default: this machine)")
    ap.add_argument("--allow-dirty", action="store_true", help="accept a build made from uncommitted changes")
    a = ap.parse_args(argv)
    app, out = Path(a.dist).resolve(), Path(a.out).resolve()
    version = edgelab.__version__
    if not (app / exe_name()).is_file():
        sys.exit(f"{app / exe_name()} not found: build first (python packaging/build.py)")
    mf = _find(app, "edgelab_build.json")
    if mf is None:
        sys.exit(f"{app} has no edgelab_build.json")
    build = json.loads(mf.read_text())
    info = _find(app, "edgelab", "web", "static", "build-info.json")
    ui_version = json.loads(info.read_text()).get("app_version") if info else None
    problems = []
    if build.get("app_version") != version:
        problems.append(f"packaged build manifest says {build.get('app_version')}, edgelab.__version__ is {version}")
    if ui_version != version:
        problems.append(f"packaged frontend bundle says {ui_version}, edgelab.__version__ is {version}")
    if build.get("git_tracked_changes") and not a.allow_dirty:
        problems.append("the build was made from uncommitted tracked changes (git_tracked_changes=true); "
                        "commit first or pass --allow-dirty")
    if problems:
        sys.exit("refusing to prepare a release:\n  - " + "\n  - ".join(problems))
    notes = Path(a.notes).read_text(encoding="utf-8") if a.notes else f"EdgeLab {version}"
    out.mkdir(parents=True, exist_ok=True)
    name = f"EdgeLab-{version}-{a.platform}.zip"
    zpath = out / name
    print(f"packaging {app} -> {zpath}")
    build_zip(app, zpath)
    digest, size = sha256(zpath), zpath.stat().st_size
    manifest = {"schema": MANIFEST_SCHEMA, "app": "EdgeLab", "version": version, "tag": f"v{version}",
                "platform": a.platform, "published_at": datetime.now(timezone.utc).isoformat(),
                "notes": notes, "artifact": {"name": name, "size": size, "sha256": digest, "app_dir": app.name},
                "build": {k: build.get(k) for k in ("build_id", "git_commit", "source_sha256", "built_at")}}
    validate_manifest(manifest)
    (out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    (out / "SHA256SUMS.txt").write_text(f"{digest}  {name}\n", encoding="utf-8")
    rel = DirectoryReleaseSource(out).latest()                  # the updater's own reader accepts it
    assert rel.version == version
    print(f"\nversion   {version} (tag v{version})\nartifact  {name}\nsize      {size:,} bytes\nsha256    {digest}\n"
          f"build     {build.get('build_id')} commit {build.get('git_commit')}")
    notes_arg = f'--notes-file "{a.notes}"' if a.notes else f'--notes "EdgeLab {version}"'
    print("\nNOT published. To publish (maintainers only), from the repository root:\n"
          f'  gh release create v{version} "{zpath}" "{out / MANIFEST_NAME}" "{out / "SHA256SUMS.txt"}" '
          f'--repo ShiningRedstone/AI-Backtesting --title "EdgeLab {version}" {notes_arg}')
    print(f"\nLocal test of the update flow without GitHub: set EDGELAB_UPDATE_SOURCE={out} and start an OLDER build.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
