"""EdgeLab desktop launcher: the packaged application's entry point (``EdgeLab.exe``).

    EdgeLab.exe                                   start the local app in its own EdgeLab window
    EdgeLab.exe --data-root D:\\Research\\EdgeLab    use another persistent workspace
    EdgeLab.exe --demo                            separate synthetic demo workspace (<data root>\\demo)
    EdgeLab.exe --ui browser                      show the UI in the default web browser instead
    EdgeLab.exe --ui none   (alias --no-browser)  headless: serve only (tests, automation)
    EdgeLabConsole.exe ...                        the same launcher with a console (logs visible)
    EdgeLabConsole.exe cli research run spec.yaml ...   the command line (edgelab.cli), same workspace rules

Development equivalent: ``python -m edgelab.desktop`` (defaults to the per-user data root too;
``python -m edgelab.web`` keeps serving the repository workspace).

Behaviour:
* ``multiprocessing.freeze_support()`` runs first, so process-parallel research can spawn workers
  from the frozen executable.
* The workspace (persistent user data + user configs) is resolved by ``edgelab.runtime`` and
  created on first run (bundled default configs copied once, never overwritten).
* One running instance per workspace (a lock file): a second launch opens the running instance's
  UI instead of starting a second server on the same store.
* The server binds to 127.0.0.1 only, on an OS-assigned free port (or ``--port``), and the UI is
  shown only after ``/api/health`` answers: by default in a native EdgeLab window (pywebview /
  Microsoft Edge WebView2, see ``edgelab.desktop_window``); closing the window stops EdgeLab. A
  missing WebView2 runtime is reported before anything starts (never a silent fallback).
* A second launch on the same workspace brings the running window to the front (loopback control
  channel, token in runtime.json) instead of starting a second server.
* ``<workspace>/logs/runtime.json`` records pid, URL and build while running (removed on exit).
* Ctrl+C, closing the console window, SIGTERM or Ctrl+Break stop the server, close the store and
  release the lock. Every committed write is already durable in SQLite.
* Startup failures are written to ``<workspace>/logs/desktop.log`` and shown in a message box
  (packaged Windows build) or on stderr.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import signal
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
RUNTIME_FILE = "runtime.json"
LOCK_FILE = "edgelab.lock"


class StartupError(RuntimeError):
    """A user-facing startup failure."""


# ------------------------------------------------------------------------------------ helpers
def show_error(message: str) -> None:
    print(f"EdgeLab could not start:\n{message}", file=sys.stderr)
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "EdgeLab could not start", 0x10)
        except Exception:                                    # noqa: BLE001 - best effort UI
            pass


class InstanceLock:
    """Exclusive, process-lifetime lock on ``<workspace>/logs/edgelab.lock`` (released by the OS
    if the process dies)."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.fh.close()
            self.fh = None
            return False

    def release(self) -> None:
        if self.fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()
            self.fh = None


def wait_ready(url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=5) as r:
                if r.status == 200:
                    return
        except OSError as exc:
            last = exc
        time.sleep(0.2)
    raise StartupError(f"the local server did not become ready within {timeout:.0f}s ({last})")


def _check_timezones() -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        raise StartupError("the IANA timezone database is missing (install the 'tzdata' package into the "
                           "build environment and rebuild)") from None


def _stop_on_signals(stop: threading.Event) -> None:
    def handler(*_):
        stop.set()
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), handler)
            except (ValueError, OSError):
                pass
    if sys.platform == "win32":                       # closing the console window
        try:
            import ctypes
            from ctypes import wintypes

            @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
            def console_handler(event):
                stop.set()
                time.sleep(4)                         # give the main thread time to shut down
                return True
            _stop_on_signals.keep = console_handler   # keep a reference alive
            ctypes.windll.kernel32.SetConsoleCtrlHandler(console_handler, True)
        except Exception:                             # noqa: BLE001
            pass


# ------------------------------------------------------------------------------------ main
def parse_args(argv):
    ap = argparse.ArgumentParser(prog="EdgeLab", description="EdgeLab local research application")
    ap.add_argument("--data-root", help="persistent workspace (default: %%LOCALAPPDATA%%\\EdgeLab or "
                                        "$EDGELAB_DATA_ROOT)")
    ap.add_argument("--demo", action="store_true", help="separate synthetic demo workspace (<data root>/demo)")
    ap.add_argument("--port", type=int, default=0, help="loopback port (default: a free port)")
    ap.add_argument("--ui", choices=("window", "browser", "none"), default="window",
                    help="window: native EdgeLab window (default); browser: default web browser; none: headless")
    ap.add_argument("--no-browser", action="store_true", help="alias for --ui none (headless)")
    ap.add_argument("--ready-timeout", type=float, default=60.0)
    return ap.parse_args(argv)


def run(argv=None) -> int:
    args = parse_args(argv)
    ui = "none" if args.no_browser else args.ui
    from edgelab import runtime
    base = Path(args.data_root).expanduser() if args.data_root else runtime.user_data_root()
    base = base.resolve()
    root = base / "demo" if args.demo else base
    logs = root / "logs"
    lock = InstanceLock(logs / LOCK_FILE)
    try:
        if args.demo:                                 # before anything is written into the demo root
            from edgelab.web.demo import create_demo_workspace
            create_demo_workspace(root, runtime.resource_dir())
        logs.mkdir(parents=True, exist_ok=True)
    except ValueError as exc:
        show_error(str(exc))
        return 1
    except OSError as exc:
        show_error(f"Cannot create the data folder {root}: {exc}")
        return 1
    _redirect_missing_streams(logs)                   # windowed exe: no console, keep output in a log
    if not lock.acquire():
        return _hand_off(root, logs, ui)
    if ui == "window":
        from edgelab.desktop_window import window_runtime_problem
        problem = window_runtime_problem()            # before anything starts: never a silent fallback
        if problem:
            _log(logs, problem)
            show_error(problem)
            lock.release()
            return 1
    server = None
    svc = None
    try:
        _check_timezones()
        runtime.build_manifest()                      # packaged: refuse early without code identity
        if args.demo:
            ws = runtime.init_workspace(root)
        else:
            from edgelab.web.demo import is_demo_root
            if is_demo_root(root):
                raise StartupError(f"{root} is a demo workspace; start it with --demo")
            ws = runtime.init_workspace(root)
        from dataclasses import replace
        from edgelab.web.app import create_app
        from edgelab.web.config import load_web_config
        from werkzeug.serving import make_server
        web = replace(load_web_config(root), host=HOST, port=args.port or 8765)
        app = create_app(root, demo=args.demo, web=web)
        svc = app.config["EDGELAB"]["services"]
        if svc.store.backend != "sqlite":
            raise StartupError(f"the desktop app requires the SQLite store (got {svc.store.backend}); set "
                               "storage.backend: sqlite or auto in configs/storage.yaml")
        try:
            server = make_server(HOST, args.port, app, threaded=True)  # port 0 -> OS-assigned free port
        except SystemExit:                            # werkzeug reports a busy port by exiting
            raise StartupError(f"port {args.port} on {HOST} is already in use; start without --port to use "
                               "a free port") from None
        url = f"http://{HOST}:{server.server_port}"
        import secrets
        from edgelab.desktop_window import WindowController, install_control
        token = secrets.token_hex(24)
        controller = WindowController(url, root / "webview") if ui == "window" else None
        install_control(app, token, ui, controller)
        threading.Thread(target=server.serve_forever, name="edgelab-http", daemon=True).start()
        wait_ready(url, args.ready_timeout)
        from edgelab.core.identity import code_version
        info = {"pid": os.getpid(), "url": url, "port": server.server_port, "data_root": str(root),
                "demo": args.demo, "ui": ui, "control_token": token,
                "runtime": runtime.runtime_info(), "code_version": code_version(),
                "config_differences_from_bundled_defaults": ws["config_differences"],
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        (logs / RUNTIME_FILE).write_text(json.dumps(info, indent=1))
        print(f"EdgeLab running at {url}\n  data: {root}\n  build: {runtime.build_label()}\n  ui: {ui}\n"
              + ("Close the EdgeLab window to stop." if ui == "window" else
                 "Keep this window open while you use EdgeLab; close it (or press Ctrl+C) to stop."), flush=True)
        if ws["config_differences"]:
            print("  note: your configs differ from this build's bundled defaults in: "
                  + ", ".join(ws["config_differences"]) + " (not changed automatically)", flush=True)
        stop = threading.Event()
        _stop_on_signals(stop)
        if ui == "window":
            controller.run(stop)                      # blocks until the window is closed
        else:
            if ui == "browser":
                webbrowser.open(url)
            while not stop.wait(0.5):
                pass
        print("EdgeLab stopping…", flush=True)
        return 0
    except StartupError as exc:
        _log(logs, str(exc))
        show_error(str(exc))
        return 1
    except Exception as exc:                          # noqa: BLE001 - shown to the user, full trace in the log
        _log(logs, traceback.format_exc())
        show_error(f"{type(exc).__name__}: {exc}\n\nDetails: {logs / 'desktop.log'}")
        return 1
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if svc is not None:
            try:
                jm = svc._jobs                        # cancel a running background search cleanly
                if jm is not None:
                    for j in jm.list():
                        if j.get("state") in ("queued", "running"):
                            jm.cancel(j["job_id"])
                    jm.join(timeout=30)
            finally:
                with svc.lock:
                    svc.store.close()
        try:
            (logs / RUNTIME_FILE).unlink(missing_ok=True)
        except OSError:
            pass
        lock.release()


def _hand_off(root: Path, logs: Path, ui: str) -> int:
    """Another instance already serves this workspace: bring its window to the front; if it has no
    window, show its URL in a window (or browser) of this process. Never a second server."""
    info = _read_runtime(logs)
    if not info:
        show_error(f"EdgeLab is already running for {root} (it is still starting, or a previous instance is "
                   "shutting down). Try again in a moment.")
        return 1
    print(f"EdgeLab is already running for {root} at {info['url']}")
    if ui == "none":
        return 0
    from edgelab.desktop_window import TOKEN_HEADER
    try:
        req = urllib.request.Request(info["url"] + "/api/desktop/focus", data=b"{}", method="POST",
                                     headers={TOKEN_HEADER: info.get("control_token", ""),
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status == 200:
                return 0                              # the running window is now in front
    except OSError:
        pass
    if ui == "browser":
        webbrowser.open(info["url"])
        return 0
    from edgelab.desktop_window import WindowController, window_runtime_problem
    problem = window_runtime_problem()
    if problem:
        show_error(problem)
        return 1
    WindowController(info["url"], root / "webview").run()     # a viewer onto the running backend
    return 0


def _redirect_missing_streams(logs: Path) -> None:
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        fh = open(logs / "console.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = sys.stdout or fh
    sys.stderr = sys.stderr or fh


def _read_runtime(logs: Path) -> dict | None:
    try:
        return json.loads((logs / RUNTIME_FILE).read_text())
    except (OSError, ValueError):
        return None


def _log(logs: Path, text: str) -> None:
    try:
        with open(logs / "desktop.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


def main(argv=None) -> int:
    multiprocessing.freeze_support()                  # MUST run first in a frozen executable
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["cli"]:                           # command line against the same workspace rules
        from edgelab import runtime
        from edgelab.cli import main as cli_main
        rest = argv[1:]
        if "--root" not in rest:
            root = runtime.user_data_root()
            runtime.init_workspace(root)
            rest = ["--root", str(root), *rest]
        return cli_main(rest)
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
