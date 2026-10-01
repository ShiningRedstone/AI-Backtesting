"""Where release metadata and artifacts come from.

``ReleaseSource.latest()`` returns a validated :class:`Release` (manifest + artifact URL) or raises
:class:`UpdateError`. Two implementations:

* :class:`GitHubReleaseSource` - the GitHub Releases API of one repository. With a ``channel`` (the git
  branch the running build came from; ADR-72) it lists the releases, keeps the branch builds of that
  channel (tag ``build-<branch-slug>-<n>``, published by CI as pre-releases) and takes the highest build
  number. Without a channel it reads ``/releases/latest`` (versioned releases; excludes drafts and
  pre-releases). Either way the manifest asset ``edgelab-release.json`` is downloaded and validated; the
  artifact URL is taken from the same release's asset list, and its asset size must equal the manifest's.
* :class:`DirectoryReleaseSource` - a local folder holding the same two files (developer testing of
  the full update flow without GitHub; produced by ``packaging/release.py``).

All network access goes through a :class:`Transport`, so tests use fixtures instead of the network.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from edgelab.updater.core import DEFAULT_REPO, MANIFEST_NAME, UpdateError, channel_slug, validate_manifest

USER_AGENT = "EdgeLab-Updater"
ALLOWED_HOSTS = ("api.github.com", "github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com", "github-releases.githubusercontent.com")
CHUNK = 1 << 16


@dataclass(frozen=True)
class Release:
    manifest: dict
    artifact_url: str
    manifest_url: str
    source: str

    @property
    def version(self) -> str:
        return self.manifest["version"]


class Transport(Protocol):
    def get_json(self, url: str, timeout: float) -> Any: ...
    def download(self, url: str, dest: Path, progress: Callable[[int], None] | None, timeout: float) -> None: ...


def _check_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme != "https" or (u.hostname or "") not in ALLOWED_HOSTS:
        raise UpdateError("MALFORMED_METADATA", f"refusing a non-GitHub or non-HTTPS URL: {url}")


class UrllibTransport:
    """Standard-library HTTPS (system CA store); maps failures onto updater error codes."""

    def _open(self, url: str, timeout: float, accept: str):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
        try:
            return urllib.request.urlopen(req, timeout=timeout)       # noqa: S310 - https only (checked)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and (e.headers.get("X-RateLimit-Remaining") == "0" or e.code == 429):
                raise UpdateError("RATE_LIMITED", "GitHub rate limit reached; try again later",
                                  reset=e.headers.get("X-RateLimit-Reset")) from None
            if e.code == 404:
                raise UpdateError("NO_RELEASE", "no published release was found (a private repository's releases "
                                                "are not visible to the app)") from None
            raise UpdateError("HTTP_ERROR", f"HTTP {e.code} from {urlparse(url).hostname}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            raise UpdateError("OFFLINE", f"cannot reach {urlparse(url).hostname} ({getattr(e, 'reason', e)})") from None

    def get_json(self, url: str, timeout: float) -> Any:
        _check_url(url)
        with self._open(url, timeout, "application/vnd.github+json, application/json") as r:
            raw = r.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise UpdateError("MALFORMED_METADATA", "release metadata is unexpectedly large")
        try:
            return json.loads(raw)
        except ValueError:
            raise UpdateError("MALFORMED_METADATA", "release metadata is not valid JSON") from None

    def download(self, url: str, dest: Path, progress, timeout: float) -> None:
        _check_url(url)
        done = 0
        with self._open(url, timeout, "application/octet-stream") as r, open(dest, "wb") as fh:
            while True:
                try:
                    chunk = r.read(CHUNK)
                except (socket.timeout, TimeoutError, OSError) as e:
                    raise UpdateError("DOWNLOAD_FAILED", f"the download was interrupted ({e})") from None
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done)


class FileTransport:
    """file:// URLs only (the directory source)."""

    def get_json(self, url: str, timeout: float) -> Any:
        p = _file_path(url)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise UpdateError("NO_RELEASE", f"no release manifest at {p}") from None
        except ValueError:
            raise UpdateError("MALFORMED_METADATA", f"{p} is not valid JSON") from None

    def download(self, url: str, dest: Path, progress, timeout: float) -> None:
        src = _file_path(url)
        if not src.is_file():
            raise UpdateError("DOWNLOAD_FAILED", f"artifact missing: {src}")
        done = 0
        with open(src, "rb") as a, open(dest, "wb") as b:
            while chunk := a.read(CHUNK):
                b.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done)


def _file_path(url: str) -> Path:
    u = urlparse(url)
    if u.scheme != "file":
        raise UpdateError("MALFORMED_METADATA", f"not a file URL: {url}")
    from urllib.request import url2pathname
    return Path(url2pathname(u.path if not u.netloc else f"//{u.netloc}{u.path}"))


class GitHubReleaseSource:
    def __init__(self, repo: str = DEFAULT_REPO, transport: Transport | None = None, timeout: float = 10.0,
                 api: str = "https://api.github.com", channel: str | None = None):
        self.repo, self.transport, self.timeout, self.api = repo, transport or UrllibTransport(), timeout, api
        self.channel = channel or None

    @property
    def description(self) -> str:
        return (f"GitHub builds of branch {self.channel} ({self.repo})" if self.channel
                else f"GitHub Releases of {self.repo}")

    def latest(self) -> Release:
        if self.channel:
            return self._latest_build()
        rel = self.transport.get_json(f"{self.api}/repos/{self.repo}/releases/latest", self.timeout)
        if not isinstance(rel, dict) or not isinstance(rel.get("assets"), list):
            raise UpdateError("MALFORMED_METADATA", "the GitHub release response has no asset list")
        if rel.get("draft") or rel.get("prerelease"):
            raise UpdateError("NO_RELEASE", "the latest release is a draft or pre-release")
        return self._from_release(rel)

    def _latest_build(self) -> Release:
        """The newest CI build of this installation's branch (highest build number in its tag)."""
        rels = self.transport.get_json(f"{self.api}/repos/{self.repo}/releases?per_page=100", self.timeout)
        if not isinstance(rels, list):
            raise UpdateError("MALFORMED_METADATA", "the GitHub releases response is not a list")
        prefix = f"build-{channel_slug(self.channel)}-"
        builds = []
        for rel in rels:
            tag = rel.get("tag_name") if isinstance(rel, dict) else None
            if isinstance(tag, str) and tag.startswith(prefix) and tag[len(prefix):].isdigit() and not rel.get("draft"):
                builds.append((int(tag[len(prefix):]), rel))
        if not builds:
            raise UpdateError("NO_RELEASE", f"no build of branch {self.channel} has been published yet")
        n, rel = max(builds, key=lambda b: b[0])
        if not isinstance(rel.get("assets"), list):
            raise UpdateError("MALFORMED_METADATA", "the GitHub release response has no asset list")
        r = self._from_release(rel)
        if r.manifest["channel"] != self.channel or r.manifest["build_number"] != n:
            raise UpdateError("MALFORMED_METADATA", f"release {rel.get('tag_name')} does not describe build {n} of "
                                                    f"branch {self.channel}")
        return r

    def _from_release(self, rel: dict) -> Release:
        assets = {a.get("name"): a for a in rel["assets"] if isinstance(a, dict)}
        man = assets.get(MANIFEST_NAME)
        if not man or not isinstance(man.get("browser_download_url"), str):
            raise UpdateError("MALFORMED_METADATA", f"the release has no {MANIFEST_NAME} asset")
        manifest = validate_manifest(self.transport.get_json(man["browser_download_url"], self.timeout),
                                     tag=rel.get("tag_name"))
        art = assets.get(manifest["artifact"]["name"])
        if not art or not isinstance(art.get("browser_download_url"), str):
            raise UpdateError("MALFORMED_METADATA", f"the release has no asset {manifest['artifact']['name']}")
        if art.get("size") != manifest["artifact"]["size"]:
            raise UpdateError("MALFORMED_METADATA", "the artifact asset size does not match the manifest",
                              asset_size=art.get("size"), manifest_size=manifest["artifact"]["size"])
        if not manifest.get("published_at"):
            manifest["published_at"] = rel.get("published_at")
        _check_url(art["browser_download_url"])
        return Release(manifest, art["browser_download_url"], man["browser_download_url"], self.description)


class DirectoryReleaseSource:
    """A local release folder: edgelab-release.json + the artifact zip (developer testing)."""

    def __init__(self, folder: str | Path, channel: str | None = None):
        self.folder = Path(folder)
        self.transport = FileTransport()
        self.channel = channel or None

    @property
    def description(self) -> str:
        return f"local release folder {self.folder}"

    def latest(self) -> Release:
        mpath = self.folder / MANIFEST_NAME
        manifest = validate_manifest(self.transport.get_json(mpath.resolve().as_uri(), 0))
        if self.channel and manifest["channel"] and manifest["channel"] != self.channel:
            raise UpdateError("NO_RELEASE", f"the release folder holds a build of branch {manifest['channel']}, "
                                            f"not {self.channel}")
        art = self.folder / manifest["artifact"]["name"]
        if not art.is_file():
            raise UpdateError("MALFORMED_METADATA", f"artifact {art.name} is missing from {self.folder}")
        return Release(manifest, art.resolve().as_uri(), mpath.resolve().as_uri(), self.description)


SOURCE_ENV = "EDGELAB_UPDATE_SOURCE"


def default_source(environ: dict | None = None, channel: str | None = None):
    """GitHub of the official repository (branch builds of ``channel`` when the running build has one);
    ``EDGELAB_UPDATE_SOURCE`` (a local release folder) overrides it for testing the full flow offline.
    The override is shown in the About screen."""
    env = os.environ if environ is None else environ
    override = env.get(SOURCE_ENV)
    if override:
        return DirectoryReleaseSource(override, channel=channel)
    return GitHubReleaseSource(channel=channel)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def transport_for(url: str, default: Transport) -> Transport:
    return FileTransport() if url.startswith("file:") else default


__all__ = ["Release", "Transport", "UrllibTransport", "FileTransport", "GitHubReleaseSource",
           "DirectoryReleaseSource", "default_source", "sha256_file", "transport_for", "SOURCE_ENV"]
