"""Native application window for the desktop launcher (pywebview; Microsoft Edge WebView2 on Windows).

The window only PRESENTS the existing local web app: it navigates to the loopback URL the launcher
already serves (``http://127.0.0.1:<port>``). No frontend or backend code changes; the React bundle
and the Flask API are exactly those of ``python -m edgelab.web``.

Windows requirement: the Microsoft Edge WebView2 Runtime (Evergreen). It ships with Windows 11 and is
distributed to Windows 10 through Windows Update; when it is absent the launcher refuses with a
clear message (``window_runtime_problem``) instead of silently falling back to anything else.

Also here: a small loopback control channel the launcher adds to the app (token-protected; the token
lives only in ``<workspace>/logs/runtime.json``) so a second launch can bring the running window to
the front, and so the Windows integration test can navigate the window and read what it loaded.
"""
from __future__ import annotations

import hmac
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

TITLE = "EdgeLab"
SIZE = (1440, 900)
MIN_SIZE = (1000, 680)
WEBVIEW2_CLIENT = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
TOKEN_HEADER = "X-EdgeLab-Desktop-Token"
HASH_RE = re.compile(r"^/[A-Za-z0-9_\-./?=&]{0,200}$")

WEBVIEW2_MISSING = (
    "EdgeLab needs the Microsoft Edge WebView2 Runtime to show its window, and it was not found on this PC.\n\n"
    "It is part of Windows 11 and is normally installed on Windows 10 by Windows Update. Install the free "
    "\"Evergreen\" runtime from Microsoft (https://developer.microsoft.com/microsoft-edge/webview2/), then start "
    "EdgeLab again.\n\nMeanwhile you can run EdgeLab in your web browser instead: EdgeLab.exe --ui browser")


def webview2_version(reg: Any = None) -> str | None:
    """Installed WebView2 Runtime version from the registry (Windows), else None."""
    if reg is None:
        if sys.platform != "win32":
            return None
        import winreg as reg
    for hive, base in ((reg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node"), (reg.HKEY_LOCAL_MACHINE, r"SOFTWARE"),
                       (reg.HKEY_CURRENT_USER, r"Software")):
        try:
            with reg.OpenKey(hive, base + "\\" + WEBVIEW2_CLIENT) as k:
                v, _ = reg.QueryValueEx(k, "pv")
        except OSError:
            continue
        if v and str(v) != "0.0.0.0":
            return str(v)
    return None


def window_runtime_problem() -> str | None:
    """None when a native window can be created here, else a user-facing explanation."""
    try:
        import webview  # noqa: F401
    except Exception as exc:                                    # noqa: BLE001 - reported to the user
        return (f"The desktop window component (pywebview) is not available: {exc}. "
                "Rebuild with packaging/requirements-build.txt, or start with --ui browser.")
    if sys.platform == "win32" and webview2_version() is None:
        return WEBVIEW2_MISSING
    return None


class WindowController:
    """The one EdgeLab window: created, shown, focused, navigated and closed through pywebview."""

    def __init__(self, url: str, storage_path: Path):
        self.url, self.storage_path = url, storage_path
        self.window = None
        self.state = {"created": False, "loaded": 0, "closed": False, "last_url": None}

    def run(self, stop: threading.Event | None = None) -> None:
        """Blocks on the GUI loop (must be the main thread) until the window closes."""
        import webview
        settings = getattr(webview, "settings", None)          # dict-like (not a dict subclass in pywebview 6)
        if settings is not None and hasattr(settings, "__setitem__"):   # no downloads: the UI offers none
            settings["ALLOW_DOWNLOADS"] = False
            settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        self.window = webview.create_window(TITLE, self.url, width=SIZE[0], height=SIZE[1], min_size=MIN_SIZE,
                                            resizable=True, text_select=True, zoomable=True)
        self.state.update(created=True, last_url=self.url)
        try:
            self.window.events.loaded += self._loaded
            self.window.events.closed += self._closed
        except AttributeError:
            pass
        if stop is not None:                                    # Ctrl+C / signals close the window too
            threading.Thread(target=self._close_when, args=(stop,), name="edgelab-window-stop", daemon=True).start()
        self.storage_path.mkdir(parents=True, exist_ok=True)
        webview.start(gui="edgechromium" if sys.platform == "win32" else None, debug=False,
                      private_mode=False, storage_path=str(self.storage_path))
        self.state["closed"] = True

    def _loaded(self, *_):
        self.state["loaded"] += 1

    def _closed(self, *_):
        self.state["closed"] = True

    def _close_when(self, stop: threading.Event) -> None:
        stop.wait()
        if not self.state["closed"] and self.window is not None:
            try:
                self.window.destroy()
            except Exception:                                   # noqa: BLE001 - already closing
                pass

    def focus(self) -> bool:
        w = self.window
        if w is None or self.state["closed"]:
            return False
        for step in (w.restore, w.show):
            try:
                step()
            except Exception:                                   # noqa: BLE001 - best effort
                pass
        try:                                                    # bring to front without staying on top
            w.on_top = True
            time.sleep(0.2)
            w.on_top = False
        except Exception:                                       # noqa: BLE001
            pass
        return True

    def navigate(self, route: str) -> bool:
        if self.window is None or self.state["closed"]:
            return False
        target = f"{self.url}/#{route}"
        self.window.load_url(target)
        self.state["last_url"] = target
        return True


def install_control(app, token: str, ui: str, controller: WindowController | None = None) -> dict:
    """Loopback control routes on the launcher's app (never part of `python -m edgelab.web`).
    Every call needs the per-launch token from runtime.json. Also counts what the app served, so
    it can be shown that the UI loaded inside the window."""
    from flask import jsonify, request
    served: dict[str, int] = {}

    def authorized() -> bool:
        return hmac.compare_digest(request.headers.get(TOKEN_HEADER, ""), token)

    @app.after_request
    def _count(resp):                                           # noqa: ANN001
        p = request.path
        key = p if not p.startswith("/api/") else "/".join(p.split("/")[:3])
        if not p.startswith("/api/desktop/"):
            served[key] = served.get(key, 0) + 1
        return resp

    @app.get("/api/desktop/status")
    def desktop_status():
        if not authorized():
            return jsonify({"error": {"kind": "forbidden", "message": "desktop token required"}}), 403
        return jsonify({"ui": ui, "window": controller.state if controller else None, "served": served})

    @app.post("/api/desktop/focus")
    def desktop_focus():
        if not authorized():
            return jsonify({"error": {"kind": "forbidden", "message": "desktop token required"}}), 403
        if controller is None or not controller.focus():
            return jsonify({"focused": False, "reason": f"no EdgeLab window in this instance (ui: {ui})"}), 409
        return jsonify({"focused": True})

    @app.post("/api/desktop/navigate")
    def desktop_navigate():
        if not authorized():
            return jsonify({"error": {"kind": "forbidden", "message": "desktop token required"}}), 403
        route = (request.get_json(silent=True) or {}).get("route", "")
        if not isinstance(route, str) or not HASH_RE.match(route):
            return jsonify({"error": {"kind": "bad_request", "message": "route must be an app route like /datasets"}}), 400
        if controller is None or not controller.navigate(route):
            return jsonify({"navigated": False, "reason": "no EdgeLab window in this instance"}), 409
        return jsonify({"navigated": True, "url": controller.state["last_url"]})

    return served
