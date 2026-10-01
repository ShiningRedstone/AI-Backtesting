"""Updater core: the one version format, the release-manifest schema and its strict validation.

A release is a GitHub Release of ShiningRedstone/AI-Backtesting carrying two assets: the packaged
application as a zip and a machine-readable manifest ``edgelab-release.json`` that states the version,
the artifact name, its size and its SHA-256. Two manifest schemas (ADR-72):

* ``edgelab-release/2`` - a **branch build** made by CI (.github/workflows/windows-build.yml): it also
  states the ``channel`` (the git branch the build came from), a monotonically increasing ``build_number``
  and the ``commit``. Tag ``build-<branch-slug>-<n>``, artifact ``EdgeLab-<version>-b<n>-<platform>.zip``.
  An installation only ever takes builds of its OWN channel with a higher build number.
* ``edgelab-release/1`` - a versioned release (tag ``v<version>``, artifact ``EdgeLab-<version>-<platform>.zip``),
  compared by version; kept so older installations and local builds keep working.

The updater never runs source code: it downloads the named artifact, verifies size + SHA-256 against the
manifest, and only then stages it.
"""
from __future__ import annotations

import platform
import re
import sys
from typing import Any, Mapping

MANIFEST_NAME = "edgelab-release.json"
MANIFEST_SCHEMA = "edgelab-release/1"                 # versioned release (tag v<version>)
MANIFEST_SCHEMA_BUILD = "edgelab-release/2"           # branch build (channel + build number)
DEFAULT_REPO = "ShiningRedstone/AI-Backtesting"
VERSION_RE = re.compile(r"^v?(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_RE = re.compile(r"^EdgeLab-(\d+\.\d+\.\d+)-([a-z0-9]+-[a-z0-9_]+)\.zip$")
ARTIFACT_BUILD_RE = re.compile(r"^EdgeLab-(\d+\.\d+\.\d+)-b(\d+)-([a-z0-9]+-[a-z0-9_]+)\.zip$")
KEY_RE = re.compile(r"^(\d+\.\d+\.\d+)(?:-b(\d{1,9}))?$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_BUILD_NUMBER = 999_999_999
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


def channel_slug(channel: str) -> str:
    """A git branch name as a release-tag fragment (e.g. claude/foo-1 -> claude-foo-1)."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(channel)).strip("-.")[:100]
    if not slug:
        raise UpdateError("MALFORMED_METADATA", f"not a usable channel (branch) name: {channel!r}")
    return slug


def build_tag(channel: str, build_number: int) -> str:
    return f"build-{channel_slug(channel)}-{int(build_number)}"


def release_key(version: str, build_number: int = 0) -> str:
    """The identity used for skip / staging / apply: '<version>' (versioned release) or '<version>-b<n>' (build)."""
    return f"{version}-b{build_number}" if build_number else version


def parse_key(key) -> tuple[int, int, int, int]:
    m = KEY_RE.match(key) if isinstance(key, str) else None
    if not m:
        raise UpdateError("MALFORMED_VERSION", f"not a release key (MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH-bN): {key!r}")
    return (*parse_version(m.group(1)), int(m.group(2) or 0))


def is_newer_release(manifest: Mapping, current_version: str, current_build: int = 0,
                     current_channel: str | None = None) -> bool:
    """A branch build is newer only for an installation of the SAME channel and only with a higher build number;
    a versioned release is compared by version (never a downgrade)."""
    if manifest.get("build_number"):
        return bool(current_channel) and manifest.get("channel") == current_channel \
            and int(manifest["build_number"]) > int(current_build or 0)
    return is_newer(manifest["version"], current_version)


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
    build_schema = m.get("schema") == MANIFEST_SCHEMA_BUILD
    if m.get("schema") != MANIFEST_SCHEMA and not build_schema:
        raise bad(f"schema must be {MANIFEST_SCHEMA!r} or {MANIFEST_SCHEMA_BUILD!r}, got {m.get('schema')!r}")
    if m.get("app") != "EdgeLab":
        raise bad("app must be 'EdgeLab'")
    try:
        version = ".".join(str(x) for x in parse_version(m.get("version")))
    except UpdateError:
        raise bad(f"version {m.get('version')!r} is not MAJOR.MINOR.PATCH") from None
    if str(m.get("version")).startswith("v"):
        raise bad("version must not carry a 'v' prefix (the tag does)")
    channel, build_number, commit = None, 0, None
    if build_schema:
        channel, build_number, commit = m.get("channel"), m.get("build_number"), m.get("commit")
        if not isinstance(channel, str) or not channel.strip() or len(channel) > 200:
            raise bad("channel must be the branch name the build came from")
        if isinstance(build_number, bool) or not isinstance(build_number, int) or not 0 < build_number <= MAX_BUILD_NUMBER:
            raise bad("build_number must be a positive integer")
        if commit is not None and (not isinstance(commit, str) or not COMMIT_RE.match(commit)):
            raise bad("commit must be a 40-character git commit id")
        try:
            want = build_tag(channel, build_number)
        except UpdateError:
            raise bad(f"channel {channel!r} cannot form a release tag") from None
    else:
        want = f"v{version}"
    if m.get("tag") != want:
        raise bad(f"tag must be {want!r}, got {m.get('tag')!r}")
    if tag is not None and tag != m["tag"]:
        raise bad(f"the GitHub release tag {tag!r} does not match the manifest tag {m['tag']!r}")
    a = m.get("artifact")
    if not isinstance(a, Mapping):
        raise bad("artifact must be an object")
    name = a.get("name")
    if build_schema:
        am = ARTIFACT_BUILD_RE.match(name) if isinstance(name, str) else None
        if not am:
            raise bad(f"artifact name {name!r} must look like EdgeLab-<version>-b<build>-<platform>.zip")
        if int(am.group(2)) != build_number:
            raise bad(f"artifact name build {am.group(2)} does not match manifest build_number {build_number}")
        art_version, art_plat = am.group(1), am.group(3)
    else:
        am = ARTIFACT_RE.match(name) if isinstance(name, str) else None
        if not am:
            raise bad(f"artifact name {name!r} must look like EdgeLab-<version>-<platform>.zip")
        art_version, art_plat = am.group(1), am.group(2)
    if art_version != version:
        raise bad(f"artifact name version {art_version} does not match manifest version {version}")
    plat = m.get("platform")
    if plat != art_plat:
        raise bad(f"platform {plat!r} does not match the artifact name ({art_plat})")
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
    return {"schema": m["schema"], "app": "EdgeLab", "version": version, "tag": m["tag"], "platform": plat,
            "channel": channel, "build_number": build_number, "commit": commit,
            "key": release_key(version, build_number),
            "published_at": pub, "notes": notes[:MAX_NOTES_CHARS],
            "artifact": {"name": name, "size": size, "sha256": sha, "app_dir": app_dir},
            "build": dict(m["build"]) if isinstance(m.get("build"), Mapping) else {}}
