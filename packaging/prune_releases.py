"""Which GitHub pre-releases CI deletes after publishing a build (ADR-90).

Keeps, per branch that still exists, only its newest branch build ``build-<branch-slug>-<n>``; deletes the older ones and
every branch build whose branch no longer exists. Releases that are not branch builds (``installer-main``, versioned
releases) are never touched. Refuses to delete anything when the branch list looks wrong (empty, or without ``main``),
so a failed ``git ls-remote`` can never wipe the releases.

Usage (CI): python packaging/prune_releases.py --tags tags.txt --branches branches.txt  -> prints the tags to delete.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from edgelab.updater.core import channel_slug  # noqa: E402

BUILD_TAG = re.compile(r"^build-(.+)-(\d+)$")


def to_delete(tags: list[str], branches: list[str]) -> list[str]:
    branches = [b.strip() for b in branches if b.strip()]
    if not branches or "main" not in branches:
        return []                                            # unsure which branches exist: delete nothing
    alive = {channel_slug(b) for b in branches}
    newest: dict[str, tuple[int, str]] = {}
    builds: list[tuple[str, int, str]] = []
    for t in (x.strip() for x in tags):
        m = BUILD_TAG.match(t)
        if not m:
            continue                                         # installer-main, versioned releases: never touched
        slug, n = m.group(1), int(m.group(2))
        builds.append((slug, n, t))
        if slug in alive and n > newest.get(slug, (-1, ""))[0]:
            newest[slug] = (n, t)
    keep = {t for _, t in newest.values()}
    return sorted(t for slug, n, t in builds if t not in keep)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tags", required=True, help="file with one release tag per line")
    ap.add_argument("--branches", required=True, help="file with one existing branch name per line")
    a = ap.parse_args(argv)
    tags = Path(a.tags).read_text(encoding="utf-8-sig").splitlines()
    branches = Path(a.branches).read_text(encoding="utf-8-sig").splitlines()
    for t in to_delete(tags, branches):
        print(t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
