"""UpdateManager: check -> prompt -> download + verify -> stage -> hand off to the helper.

State that survives restarts lives next to the app settings (``%APPDATA%\\EdgeLab\\update_state.json``:
skipped versions, the automatic-check preference, the last check). Downloads are staged under
``%LOCALAPPDATA%\\EdgeLab-Updater\\<version>\\`` - never inside the installation or a research
workspace. Every failure is reported in ``status()`` and is never fatal to the application; offline
start-up simply reports "offline".
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from edgelab.updater import apply as ap
from edgelab.updater.core import UpdateError, current_platform, is_newer, parse_version
from edgelab.updater.source import Release, UrllibTransport, sha256_file, transport_for

STATE_FILE = "update_state.json"
LOG_FILE = "update.log"
CHECK_INTERVAL = timedelta(hours=6)
MAX_UNZIPPED_BYTES = 4 * 1024 ** 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UpdateManager:
    def __init__(self, current_version: str, source, state_dir: Path, cache_dir: Path, *,
                 install_dir: Path | None = None, platform: str | None = None, transport=None,
                 helper_cmd: Callable[[Path, list[str]], list[str]] | None = None,
                 protected: Callable[[], list[Path]] | None = None, restart_args: list[str] | None = None,
                 shutdown: Callable[[], None] | None = None, apply_supported: bool | None = None):
        parse_version(current_version)
        self.current = current_version
        self.source = source
        self.state_dir, self.cache_dir = Path(state_dir), Path(cache_dir)
        self.install_dir = Path(install_dir) if install_dir else None
        self.platform = platform or current_platform()
        self.transport = transport or UrllibTransport()
        self.helper_cmd = helper_cmd
        self.protected = protected or (lambda: [])
        self.restart_args = list(restart_args or [])
        self.shutdown = shutdown
        self.app_pid = os.getpid()                           # the process the helper waits for
        self.apply_supported = (self.install_dir is not None) if apply_supported is None else apply_supported
        self._lock = threading.RLock()
        self._release: Release | None = None
        self._check = {"state": "idle", "checked_at": None, "error": None}
        self._download = {"state": "idle", "bytes": 0, "total": None, "error": None, "version": None}
        self._dismissed: set[str] = set()
        self._install = {"state": "idle", "step": None, "version": None, "error": None}
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- persistent state
    @property
    def state_path(self) -> Path:
        return self.state_dir / STATE_FILE

    @property
    def log_path(self) -> Path:
        return self.state_dir / "logs" / LOG_FILE

    def _state(self) -> dict:
        try:
            s = json.loads(self.state_path.read_text(encoding="utf-8"))
            return s if isinstance(s, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, **updates) -> dict:
        s = {**self._state(), **updates}
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(s, indent=1, sort_keys=True), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass                                             # an unwritable preference file only costs memory
        return s

    def _log(self, **event) -> None:
        ap._log(self.log_path, **event)

    # ---------------------------------------------------------------- status / preferences
    def status(self) -> dict:
        with self._lock:
            st = self._state()
            rel = self._release.manifest if self._release else None
            skipped = list(st.get("skipped_versions") or [])
            newer = False
            note = None
            if rel:
                if rel["platform"] != self.platform:
                    note = f"the latest release is for {rel['platform']}; this installation is {self.platform}"
                elif is_newer(rel["version"], self.current):
                    newer = True
                elif parse_version(rel["version"]) < parse_version(self.current):
                    note = (f"the published release {rel['version']} is older than this build {self.current}; "
                            "downgrades are never offered")
                else:
                    note = "this is the latest published release"
            available = bool(rel and newer)
            last_update = self._last_update_result()
            return {"current_version": self.current, "platform": self.platform,
                    "source": getattr(self.source, "description", str(self.source)),
                    "auto_check": bool(st.get("auto_check", True)), "skipped_versions": skipped,
                    "check": dict(self._check, last_success_at=st.get("last_success_at")),
                    "release": ({"version": rel["version"], "tag": rel["tag"], "published_at": rel["published_at"],
                                 "notes": rel["notes"], "size": rel["artifact"]["size"],
                                 "artifact": rel["artifact"]["name"], "sha256": rel["artifact"]["sha256"],
                                 "platform": rel["platform"]} if rel else None),
                    "available": available, "note": note,
                    "skipped": bool(available and rel["version"] in skipped),
                    "prompt": bool(available and rel["version"] not in skipped and rel["version"] not in self._dismissed),
                    "download": dict(self._download), "install": dict(self._install),
                    "apply_supported": self.apply_supported,
                    "apply_unsupported_reason": None if self.apply_supported else (
                        "updates install only into the packaged Windows application; this is a development run"),
                    "install_dir": str(self.install_dir) if self.install_dir else None,
                    "cache_dir": str(self.cache_dir), "log": str(self.log_path),
                    "last_update": last_update}

    def _last_update_result(self) -> dict | None:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("event") in ("update_completed", "update_failed"):
                return e
        return None

    def set_auto_check(self, enabled: bool) -> dict:
        self._save_state(auto_check=bool(enabled))
        return self.status()

    def skip(self, version: str) -> dict:
        parse_version(version)
        s = self._state()
        skipped = sorted(set(s.get("skipped_versions") or []) | {version}, key=parse_version)
        self._save_state(skipped_versions=skipped)
        self._log(event="version_skipped", version=version)
        return self.status()

    def unskip(self, version: str) -> dict:
        s = self._state()
        self._save_state(skipped_versions=[v for v in s.get("skipped_versions") or [] if v != version])
        return self.status()

    def later(self, version: str) -> dict:
        """Dismiss the prompt for this session only (asked again at the next start)."""
        with self._lock:
            self._dismissed.add(version)
        return self.status()

    # ---------------------------------------------------------------- check
    def check(self) -> dict:
        with self._lock:
            self._check = {"state": "checking", "checked_at": None, "error": None}
        try:
            rel = self.source.latest()
            with self._lock:
                self._release = rel
                self._check = {"state": "done", "checked_at": _now(), "error": None}
            self._save_state(last_success_at=_now(), last_seen_version=rel.version)
        except UpdateError as e:
            with self._lock:
                self._check = {"state": "error", "checked_at": _now(), "error": e.to_dict()}
        except Exception as e:                               # noqa: BLE001 - never fatal to the app
            with self._lock:
                self._check = {"state": "error", "checked_at": _now(),
                               "error": {"code": "CHECK_FAILED", "message": f"{type(e).__name__}: {e}"}}
        self._save_state(last_check_at=_now())
        return self.status()

    def check_async(self) -> None:
        with self._lock:
            if self._check["state"] == "checking":
                return
            self._check = {"state": "checking", "checked_at": None, "error": None}
        threading.Thread(target=self.check, name="edgelab-update-check", daemon=True).start()

    def maybe_auto_check(self) -> None:
        """Background check at start-up: packaged installations only (a development run or test never
        contacts the network by itself; 'Check for updates' still works everywhere)."""
        st = self._state()
        if not st.get("auto_check", True) or not self.apply_supported:
            return
        with self._lock:
            if self._check["state"] != "idle":
                return
        self.check_async()

    # ---------------------------------------------------------------- download + stage
    def _stage_dir(self, version: str) -> Path:
        return self.cache_dir / version

    def download(self, version: str, wait: bool = False) -> dict:
        with self._lock:
            rel = self._release
            if rel is None:
                raise UpdateError("NO_RELEASE", "check for updates first")
            if rel.version != version:
                raise UpdateError("VERSION_MISMATCH", f"the checked release is {rel.version}, not {version}")
            if not is_newer(version, self.current):
                raise UpdateError("DOWNGRADE_REFUSED", f"{version} is not newer than {self.current}")
            if rel.manifest["platform"] != self.platform:
                raise UpdateError("PLATFORM_MISMATCH", f"the release is for {rel.manifest['platform']}")
            if self._download["state"] in ("downloading", "verifying"):
                return self.status()
            self._download = {"state": "downloading", "bytes": 0, "total": rel.manifest["artifact"]["size"],
                              "error": None, "version": version}
        t = threading.Thread(target=self._download_and_stage, args=(rel,), name="edgelab-update-download", daemon=True)
        self._thread = t
        t.start()
        if wait:
            t.join()
        return self.status()

    def _progress(self, n: int) -> None:
        with self._lock:
            self._download["bytes"] = n

    def _download_and_stage(self, rel: Release) -> None:
        m, v = rel.manifest, rel.version
        d = self._stage_dir(v)
        part, final = d / (m["artifact"]["name"] + ".part"), d / m["artifact"]["name"]
        try:
            if d.exists():
                shutil.rmtree(d)                              # an interrupted earlier attempt starts over
            d.mkdir(parents=True)
            self._log(event="download_started", version=v, url=rel.artifact_url)
            transport_for(rel.artifact_url, self.transport).download(rel.artifact_url, part, self._progress, 60.0)
            with self._lock:
                self._download["state"] = "verifying"
            size = part.stat().st_size
            if size != m["artifact"]["size"]:
                raise UpdateError("SIZE_MISMATCH", f"downloaded {size} bytes, the release states "
                                                   f"{m['artifact']['size']}")
            digest = sha256_file(part)
            if digest != m["artifact"]["sha256"]:
                raise UpdateError("CHECKSUM_MISMATCH", "the downloaded file does not match the published SHA-256; "
                                                       "it was discarded and nothing was installed",
                                  expected=m["artifact"]["sha256"], actual=digest)
            part.replace(final)
            staged = self._extract(final, d / "app", m["artifact"]["app_dir"], v)
            (d / "READY.json").write_text(json.dumps({"version": v, "sha256": digest, "staged": str(staged),
                                                      "verified_at": _now()}, indent=1), encoding="utf-8")
            with self._lock:
                self._download.update(state="ready", error=None, staged=str(staged))
            self._log(event="download_verified", version=v, sha256=digest)
        except UpdateError as e:
            shutil.rmtree(d, ignore_errors=True)
            with self._lock:
                self._download.update(state="error", error=e.to_dict())
            self._log(event="download_failed", version=v, error=e.to_dict())
        except Exception as e:                               # noqa: BLE001
            shutil.rmtree(d, ignore_errors=True)
            err = {"code": "DOWNLOAD_FAILED", "message": f"{type(e).__name__}: {e}"}
            with self._lock:
                self._download.update(state="error", error=err)
            self._log(event="download_failed", version=v, error=err)

    @staticmethod
    def _extract(zpath: Path, dest: Path, app_dir: str, version: str) -> Path:
        """Extract a verified artifact safely (no absolute paths, no '..', no links, bounded size)."""
        with zipfile.ZipFile(zpath) as z:
            total = 0
            for info in z.infolist():
                name = info.filename.replace("\\", "/")
                parts = [p for p in name.split("/") if p]
                if (name.startswith("/") or (parts and ":" in parts[0]) or ".." in parts or not parts
                        or parts[0] != app_dir):
                    raise UpdateError("MALFORMED_ARTIFACT", f"unsafe or unexpected path in the artifact: {info.filename}")
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise UpdateError("MALFORMED_ARTIFACT", f"links are not allowed in the artifact: {info.filename}")
                total += info.file_size
                if total > MAX_UNZIPPED_BYTES:
                    raise UpdateError("MALFORMED_ARTIFACT", "the artifact expands beyond the size limit")
            z.extractall(dest)
            if os.name == "posix":                           # keep executable bits (never setuid/setgid)
                for info in z.infolist():
                    mode = (info.external_attr >> 16) & 0o777
                    if mode and not info.is_dir():
                        os.chmod(dest / info.filename, mode)
        staged = dest / app_dir
        if not (staged / ap.exe_name()).is_file():
            raise UpdateError("MALFORMED_ARTIFACT", f"the artifact has no {ap.exe_name()}")
        got = ap._manifest_version(staged)
        if got != version:
            raise UpdateError("VERSION_MISMATCH", f"the artifact's build manifest says {got}, the release says {version}")
        return staged

    # ---------------------------------------------------------------- apply
    def apply(self, version: str) -> dict:
        """Start the helper process (from the staged build) and ask the application to exit."""
        if not self.apply_supported or self.install_dir is None:
            raise UpdateError("APPLY_UNSUPPORTED", "updates install only into the packaged application")
        ready = self._stage_dir(version) / "READY.json"
        try:
            info = json.loads(ready.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise UpdateError("NOT_STAGED", f"{version} has not been downloaded and verified") from None
        staged = Path(info["staged"])
        if info.get("version") != version or not is_newer(version, self.current):
            raise UpdateError("DOWNGRADE_REFUSED", f"{version} is not newer than {self.current}")
        reason = ap.unsafe_reason(self.install_dir, self.protected())
        if reason:
            raise UpdateError("UNSAFE_INSTALL_DIR", reason)
        restart = [str(self.install_dir / ap.exe_name()), *self.restart_args]
        args = ["--target", str(self.install_dir), "--staged", str(staged), "--version", version,
                "--wait-pid", str(self.app_pid), "--log", str(self.log_path), "--restart", json.dumps(restart),
                "--ready-timeout", str(self._ready_timeout())]
        for p in self.protected():
            args += ["--protect", str(p)]
        cmd = self.helper_cmd(staged, args) if self.helper_cmd else [str(staged / ap.exe_name()), "--apply-update", *args]
        self._log(event="apply_requested", version=version, helper=cmd[0])
        ap._spawn(cmd)
        if self.shutdown:
            threading.Timer(0.8, self.shutdown).start()     # let the HTTP response reach the UI first
        return {**self.status(), "applying": True}

    # ---------------------------------------------------------------- one-click update (Settings)
    def install(self, wait: bool = False) -> dict:
        """Update now in one action: check, download + verify (or reuse a verified staged build), then hand
        off to the helper and restart. Every step keeps its own checks (newer only, same platform, SHA-256);
        an up-to-date installation is left alone."""
        if not self.apply_supported or self.install_dir is None:
            raise UpdateError("APPLY_UNSUPPORTED", "updates install only into the packaged application")
        with self._lock:
            if self._install["state"] in ("running", "applying"):
                return self.status()
            self._install = {"state": "running", "step": "checking", "version": None, "error": None}
        t = threading.Thread(target=self._install_run, name="edgelab-update-install", daemon=True)
        t.start()
        if wait:
            t.join()
        return self.status()

    def _set_install(self, **kw) -> None:
        with self._lock:
            self._install.update(kw)

    def _install_run(self) -> None:
        try:
            self.check()
            with self._lock:
                rel, chk = self._release, dict(self._check)
            if chk["state"] == "error":
                e = chk["error"] or {}
                raise UpdateError(e.get("code", "CHECK_FAILED"), e.get("message", "the update check failed"))
            if rel is None or rel.manifest["platform"] != self.platform or not is_newer(rel.version, self.current):
                self._set_install(state="up_to_date", step=None)
                return
            v = rel.version
            self._set_install(version=v)
            try:
                staged = json.loads((self._stage_dir(v) / "READY.json").read_text(encoding="utf-8")).get("version") == v
            except (OSError, ValueError):
                staged = False
            if not staged:
                self._set_install(step="downloading")
                self.download(v, wait=True)
                with self._lock:
                    d = dict(self._download)
                if d["state"] != "ready":
                    e = d.get("error") or {}
                    raise UpdateError(e.get("code", "DOWNLOAD_FAILED"), e.get("message", "the download did not complete"))
            self._set_install(step="applying")
            self.apply(v)
            self._set_install(state="applying")
        except UpdateError as e:
            self._set_install(state="error", step=None, error=e.to_dict())
        except Exception as e:                               # noqa: BLE001 - never fatal to the app
            self._set_install(state="error", step=None, error={"code": "UPDATE_FAILED", "message": f"{type(e).__name__}: {e}"})

    @staticmethod
    def _ready_timeout() -> float:
        """How long a relaunched new version has to report ready (EDGELAB_UPDATE_READY_TIMEOUT: tests only)."""
        try:
            v = float(os.environ.get(ap.READY_TIMEOUT_ENV) or ap.DEFAULT_READY_TIMEOUT)
            return v if 5 <= v <= 1800 else ap.DEFAULT_READY_TIMEOUT
        except ValueError:
            return ap.DEFAULT_READY_TIMEOUT

    def cleanup(self) -> None:
        """Start-up housekeeping: old staging folders and swap leftovers (best effort)."""
        if self.cache_dir.is_dir():
            for d in self.cache_dir.iterdir():
                try:
                    if d.is_dir() and not is_newer(d.name, self.current):
                        shutil.rmtree(d, ignore_errors=True)
                except UpdateError:
                    shutil.rmtree(d, ignore_errors=True)
        if self.install_dir is not None:
            removed = ap.cleanup_leftovers(self.install_dir)
            if removed:
                self._log(event="leftovers_removed", paths=removed)


def wait_until(pred: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


__all__ = ["UpdateManager", "wait_until"]
