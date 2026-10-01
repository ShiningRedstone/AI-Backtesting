"""The update helper: replaces an installed EdgeLab folder with a VERIFIED staged build.

Runs in its own process (``EdgeLab.exe --apply-update ...`` launched from the *staged* new build, so
the installed files are not in use), because Windows cannot replace a running executable:

  1. wait until the running EdgeLab process has exited (timeout -> nothing changed);
  2. refuse unless the target is a packaged EdgeLab folder (build manifest + executable) and holds no
     research workspace (user data never lives in the install folder, and is never touched);
  3. copy the staged build to ``<install>.new-<version>`` and check its build manifest version;
  4. swap with two renames: ``<install>`` -> ``<install>.old-<stamp>``, ``.new`` -> ``<install>``
     (if the second rename fails, the first is undone);
  5. relaunch the new executable and wait until IT reports ready (a ready file it writes only after its own
     server answered); if it exits first or is not ready in time (e.g. stuck behind a start-up error dialog),
     it is terminated, the previous version is restored and relaunched, and the failed build is kept aside;
  6. remove the old folder (best effort; leftovers are removed at the next start).

Every step is appended to the update log (JSON lines) for diagnostics. The helper never deletes
the current installation before the replacement is in place and verified.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_FILE = "edgelab_build.json"
WORKSPACE_MARKERS = ("data", "strategy_library", "logs/edgelab.lock", "edgelab.sqlite", "data/edgelab.sqlite")


def exe_name(windowed: bool = True) -> str:
    base = "EdgeLab" if windowed else "EdgeLabConsole"
    return base + (".exe" if sys.platform == "win32" else "")


def _manifest_version(folder: Path) -> str | None:
    for cand in (folder / MANIFEST_FILE, folder / "_internal" / MANIFEST_FILE):
        try:
            return json.loads(cand.read_text(encoding="utf-8")).get("app_version")
        except (OSError, ValueError):
            continue
    return None


def _manifest_build(folder: Path) -> int:
    """The CI build number recorded in a packaged build's manifest (0 when it has none)."""
    for cand in (folder / MANIFEST_FILE, folder / "_internal" / MANIFEST_FILE):
        try:
            n = json.loads(cand.read_text(encoding="utf-8")).get("build_number") or 0
            return int(n) if isinstance(n, int) and not isinstance(n, bool) else 0
        except (OSError, ValueError):
            continue
    return 0


def looks_like_install(folder: Path) -> bool:
    return (folder / exe_name()).is_file() and _manifest_version(folder) is not None


def unsafe_reason(install: Path, protected: list[Path] | None = None) -> str | None:
    """Why this folder must NOT be replaced (or None when it is a plain packaged app folder)."""
    install = install.resolve()
    if not looks_like_install(install):
        return f"{install} is not a packaged EdgeLab folder ({exe_name()} + {MANIFEST_FILE})"
    for m in WORKSPACE_MARKERS:
        if (install / m).exists():
            return (f"{install} contains research data ({m}); the updater never replaces a folder that holds a "
                    "workspace. Install EdgeLab in its own folder (e.g. %LOCALAPPDATA%\\Programs\\EdgeLab).")
    for p in protected or []:
        try:
            p = Path(p).resolve()
        except OSError:
            continue
        if p == install or install in p.parents or p in install.parents:
            return f"{install} overlaps the protected folder {p} (workspace, data root or settings)"
    return None


def _log(log: Path | None, **event) -> None:
    event = {"at": datetime.now(timezone.utc).isoformat(), **event}
    if log is None:
        return
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        SYNCHRONIZE, WAIT_TIMEOUT = 0x00100000, 0x00000102
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                                   # a zombie child counts as exited
        waited, _ = os.waitpid(pid, os.WNOHANG)
        return waited == 0
    except ChildProcessError:
        return True


def wait_for_exit(pid: int | None, timeout: float) -> bool:
    if not pid:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.25)
    return not pid_alive(pid)


def _rename(src: Path, dst: Path, attempts: int = 40) -> None:
    """Rename with retries (antivirus / indexers briefly hold files open on Windows)."""
    if dst.exists():
        raise OSError(f"cannot rename {src} -> {dst}: the destination exists")
    last = None
    for _ in range(attempts):
        try:
            os.rename(src, dst)
            return
        except OSError as exc:
            last = exc
            time.sleep(0.5)
    raise OSError(f"cannot rename {src} -> {dst}: {last}")


READY_ENV = "EDGELAB_UPDATE_READY_FILE"          # set by the helper for the relaunched app only
READY_TIMEOUT_ENV = "EDGELAB_UPDATE_READY_TIMEOUT"   # seconds; tests shorten it (default below)
DEFAULT_READY_TIMEOUT = 180.0


def _spawn(cmd: list[str], env: dict | None = None) -> subprocess.Popen:
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                "close_fds": True, "env": env}
    if sys.platform == "win32":
        kw["creationflags"] = 0x00000008 | 0x00000200            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, **kw)


def _await_ready(proc: subprocess.Popen, ready: Path, timeout: float) -> str:
    """'ready' once the relaunched app wrote the ready file (only after its server answered /api/health);
    'exited:<code>' if it ended before that; 'timeout' otherwise. A process that is still alive is NOT
    evidence of success: a windowed build that fails at start-up sits behind a modal error dialog."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready.is_file():
            return "ready"
        code = proc.poll()
        if code is not None:
            return "ready" if ready.is_file() else f"exited:{code}"
        time.sleep(0.25)
    return "ready" if ready.is_file() else "timeout"


def _stop(proc: subprocess.Popen) -> bool:
    if proc.poll() is None:
        try:
            proc.kill()                                          # Windows: TerminateProcess (closes a stuck dialog too)
            proc.wait(15)
        except (OSError, subprocess.TimeoutExpired):
            pass
    return proc.poll() is not None


def apply_update(target: str | Path, staged: str | Path, version: str, *, wait_pid: int | None = None,
                 restart_cmd: list[str] | None = None, log: str | Path | None = None,
                 protected: list[str | Path] | None = None, exit_timeout: float = 180.0,
                 ready_timeout: float = DEFAULT_READY_TIMEOUT, build: int | None = None) -> dict:
    """Swap ``target`` for ``staged`` (see module docstring). Returns a result dict; never raises."""
    import tempfile
    target, staged = Path(target), Path(staged)
    logp = Path(log) if log else None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    new = target.with_name(f"{target.name}.new-{version}")
    old = target.with_name(f"{target.name}.old-{stamp}")
    ready = (logp.parent if logp else Path(tempfile.gettempdir())) / f"edgelab-ready-{version}-{stamp}-{os.getpid()}.json"
    res = {"ok": False, "version": version, "target": str(target), "staged": str(staged), "step": "start",
           "restored_previous": False, "helper_pid": os.getpid()}

    def fail(step: str, msg: str, relaunch: bool = False) -> dict:
        """``relaunch``: EdgeLab has already exited and the installation is unchanged (or restored), so start the
        previous version again rather than leave the user without the application."""
        res.update(step=step, error=msg)
        if relaunch and restart_cmd and (target / exe_name()).is_file():
            try:
                _spawn(restart_cmd)
                res["relaunched_previous"] = True
            except OSError as exc:
                res["relaunch_error"] = str(exc)
        _log(logp, event="update_failed", **res)
        return res

    _log(logp, event="update_started", version=version, target=str(target), staged=str(staged), wait_pid=wait_pid,
         helper_pid=os.getpid())
    if not wait_for_exit(wait_pid, exit_timeout):
        return fail("wait", f"EdgeLab (pid {wait_pid}) did not exit within {exit_timeout:.0f}s; nothing changed")
    reason = unsafe_reason(target, [Path(p) for p in (protected or [])])
    if reason:
        return fail("preflight", reason + "; nothing changed", relaunch=True)
    if _manifest_version(staged) != version or not (staged / exe_name()).is_file():
        return fail("preflight", f"the staged build is not EdgeLab {version}; nothing changed", relaunch=True)
    if build and _manifest_build(staged) != build:
        return fail("preflight", f"the staged build is not build {build}; nothing changed", relaunch=True)
    try:
        if new.exists():
            shutil.rmtree(new)
        shutil.copytree(staged, new)
        if _manifest_version(new) != version or not (new / exe_name()).is_file():
            raise OSError("the copied build failed verification")
    except OSError as exc:
        shutil.rmtree(new, ignore_errors=True)
        return fail("copy", f"could not prepare the new version ({exc}); nothing changed", relaunch=True)
    try:
        _rename(target, old)
    except OSError as exc:
        shutil.rmtree(new, ignore_errors=True)
        return fail("swap", f"the installed folder is in use ({exc}); nothing changed", relaunch=True)
    try:
        _rename(new, target)
    except OSError as exc:
        try:
            _rename(old, target)
            res["restored_previous"] = True
        except OSError as exc2:
            return fail("swap", f"could not activate the new version ({exc}) and could not restore the previous "
                                f"one ({exc2}): rename {old} back to {target} manually")
        shutil.rmtree(new, ignore_errors=True)
        return fail("swap", f"could not activate the new version ({exc}); the previous version is back in place",
                    relaunch=True)
    _log(logp, event="update_swapped", version=version, backup=str(old))
    res["backup"] = str(old)
    if restart_cmd:
        proc = None
        try:
            ready.unlink(missing_ok=True)
            proc = _spawn(restart_cmd, env={**os.environ, READY_ENV: str(ready)})
            res["new_pid"] = proc.pid
            state = _await_ready(proc, ready, ready_timeout)
            if state != "ready":
                raise RuntimeError("the new version did not start: " + (
                    f"it exited with code {state.split(':', 1)[1]} before it was ready" if state.startswith("exited")
                    else f"it was not ready within {ready_timeout:.0f}s (e.g. stuck behind a start-up error dialog)"))
            res["restarted"] = True
        except (OSError, RuntimeError) as exc:
            if proc is not None:
                res["new_process_stopped"] = _stop(proc)        # never leave a stuck new version running
            bad = target.with_name(f"{target.name}.failed-{stamp}")
            try:
                _rename(target, bad)
                _rename(old, target)
                res["restored_previous"] = True
                res["failed_build_kept"] = str(bad)
            except OSError as exc2:
                return fail("restart", f"{exc}; restoring the previous version also failed ({exc2})")
            return fail("restart", f"{exc}; the previous version was restored", relaunch=True)
        finally:
            ready.unlink(missing_ok=True)
    shutil.rmtree(old, ignore_errors=True)
    res.update(ok=True, step="done", backup_removed=not old.exists())
    _log(logp, event="update_completed", **res)
    return res


def cleanup_leftovers(install: Path, keep_failed: int = 1) -> list[str]:
    """Remove ``<install>.old-*`` / ``.new-*`` leftovers of earlier updates (best effort)."""
    removed = []
    if not install.parent.is_dir():
        return removed
    for p in install.parent.glob(f"{install.name}.*-*"):
        kind = p.name[len(install.name) + 1:].split("-", 1)[0]
        if kind in ("old", "new") and p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            if not p.exists():
                removed.append(str(p))
    failed = sorted(install.parent.glob(f"{install.name}.failed-*"))
    for p in failed[:-keep_failed] if keep_failed else failed:
        shutil.rmtree(p, ignore_errors=True)
    return removed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="EdgeLab --apply-update")
    ap.add_argument("--target", required=True)
    ap.add_argument("--staged", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--build", type=int, default=0, help="CI build number the staged build must carry (branch builds)")
    ap.add_argument("--wait-pid", type=int, default=0)
    ap.add_argument("--log")
    ap.add_argument("--protect", action="append", default=[])
    ap.add_argument("--restart", help="JSON list: command to start the new version")
    ap.add_argument("--ready-timeout", type=float, default=DEFAULT_READY_TIMEOUT,
                    help="seconds the relaunched app has to report ready before the update is rolled back")
    a = ap.parse_args(argv)
    restart = json.loads(a.restart) if a.restart else None
    res = apply_update(a.target, a.staged, a.version, wait_pid=a.wait_pid or None, restart_cmd=restart,
                       log=a.log, protected=a.protect, ready_timeout=a.ready_timeout, build=a.build or None)
    if not res["ok"]:
        msg = f"EdgeLab could not be updated to {a.version}:\n{res.get('error')}"
        print(msg, file=sys.stderr)
        # EDGELAB_UPDATER_QUIET: unattended tests (packaging/smoke_update.py); the log still records everything
        if sys.platform == "win32" and getattr(sys, "frozen", False) and not os.environ.get("EDGELAB_UPDATER_QUIET"):
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, msg, "EdgeLab update", 0x30)
            except Exception:                                  # noqa: BLE001
                pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
