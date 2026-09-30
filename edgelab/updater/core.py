"""Updater core: the one version format, the release-manifest schema and its strict validation.

A release is a deliberate, versioned GitHub Release of ShiningRedstone/AI-Backtesting carrying two
assets: the packaged application as a zip (``EdgeLab-<version>-<platform>.zip``) and a machine-readable
manifest ``edgelab-release.json`` (schema below) that states the version, the artifact name, its
size and its SHA-256. The updater never follows a branch and never runs source code: it downloads the
named artifact, verifies size + SHA-256 against the manifest, and only then stages it.
"""
from __future__ import annotations

import platform
import re
import sys
from typing import Any, Mapping

MANIFEST_NAME = "edgelab-release.json"
MANIFEST_SCHEMA = "edgelab-release/1"
DEFAULT_REPO = "ShiningRedstone/AI-Backtesting"
VERSION_RE = re.compile(r"^v?(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_RE = re.compile(r"^EdgeLab-(\d+\.\d+\.\d+)-([a-z0-9]+-[a-z0-9_]+)\.zip$")
APP_DIR_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
MAX_ARTIFACT_BYTES = 2 * 1024 ** 3
MAX_NOTES_CHARS = 20000


class UpdateError(Exception):
    """A user-facing updater failure with a machine-readable code (never fatal to the app)."""

    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(f"[{code}] {message}")
        self.code, self.message, self.detail = code, message, detail

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.detail}


def parse_version(v: Any) -> tuple[int, int, int]:
    """MAJOR.MINOR.PATCH (an optional leading 'v' is accepted); anything else is refused."""
    m = VERSION_RE.match(str(v).strip()) if isinstance(v, str) else None
    if not m:
        raise UpdateError("MALFORMED_VERSION", f"not a MAJOR.MINOR.PATCH version: {v!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


def current_platform() -> str:
    """The artifact platform tag this process can install (e.g. windows-x64)."""
    osname = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    mach = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(mach, mach or "unknown")
    return f"{osname}-{arch}"


def validate_manifest(m: Any, *, tag: str | None = None) -> dict:
    """Strictly validate a release manifest; returns a normalized copy or raises MALFORMED_METADATA."""
    def bad(msg: str) -> UpdateError:
        return UpdateError("MALFORMED_METADATA", f"release manifest: {msg}")

    if not isinstance(m, Mapping):
        raise bad("not a JSON object")
    if m.get("schema") != MANIFEST_SCHEMA:
        raise bad(f"schema must be {MANIFEST_SCHEMA!r}, got {m.get('schema')!r}")
    if m.get("app") != "EdgeLab":
        raise bad("app must be 'EdgeLab'")
    try:
        version = ".".join(str(x) for x in parse_version(m.get("version")))
    except UpdateError:
        raise bad(f"version {m.get('version')!r} is not MAJOR.MINOR.PATCH") from None
    if str(m.get("version")).startswith("v"):
        raise bad("version must not carry a 'v' prefix (the tag does)")
    if m.get("tag") != f"v{version}":
        raise bad(f"tag must be 'v{version}', got {m.get('tag')!r}")
    if tag is not None and tag != m["tag"]:
        raise bad(f"the GitHub release tag {tag!r} does not match the manifest tag {m['tag']!r}")
    a = m.get("artifact")
    if not isinstance(a, Mapping):
        raise bad("artifact must be an object")
    name = a.get("name")
    am = ARTIFACT_RE.match(name) if isinstance(name, str) else None
    if not am:
        raise bad(f"artifact name {name!r} must look like EdgeLab-<version>-<platform>.zip")
    if am.group(1) != version:
        raise bad(f"artifact name version {am.group(1)} does not match manifest version {version}")
    plat = m.get("platform")
    if plat != am.group(2):
        raise bad(f"platform {plat!r} does not match the artifact name ({am.group(2)})")
    size = a.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_ARTIFACT_BYTES:
        raise bad("artifact size must be a positive integer (bytes)")
    sha = a.get("sha256")
    if not isinstance(sha, str) or not SHA256_RE.match(sha):
        raise bad("artifact sha256 must be 64 lowercase hex characters")
    app_dir = a.get("app_dir", "EdgeLab")
    if not isinstance(app_dir, str) or not APP_DIR_RE.match(app_dir):
        raise bad("artifact app_dir must be a simple folder name")
    notes = m.get("notes") or ""
    if not isinstance(notes, str):
        raise bad("notes must be text")
    pub = m.get("published_at")
    if pub is not None and not isinstance(pub, str):
        raise bad("published_at must be an ISO timestamp string")
    return {"schema": MANIFEST_SCHEMA, "app": "EdgeLab", "version": version, "tag": m["tag"], "platform": plat,
            "published_at": pub, "notes": notes[:MAX_NOTES_CHARS],
            "artifact": {"name": name, "size": size, "sha256": sha, "app_dir": app_dir},
            "build": dict(m["build"]) if isinstance(m.get("build"), Mapping) else {}}
