"""Native EdgeLab window (pywebview / WebView2): deterministic tests that run anywhere.

A real native window cannot be created in headless CI, so the GUI loop is replaced by a FAKE
``webview`` module that behaves like a real window: it fetches the page and bundle from the local
URL, reports "loaded", uses the control channel, then "closes". Everything around it is real: the
launcher, the HTTP server, the workspace, the instance lock, SQLite and shutdown. The real window is
exercised only by packaging/window_test_windows.py on Windows (never faked here)."""
import io
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from edgelab import desktop, desktop_window as dw

REPO = Path(__file__).resolve().parents[1]


class _Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, h):
        self.handlers.append(h)
        return self

    def fire(self):
        for h in self.handlers:
            h()


class FakeWindow:
    def __init__(self, title, url, **kw):
        self.title, self.url, self.kw = title, url, kw
        self.events = types.SimpleNamespace(loaded=_Event(), closed=_Event())
        self.loaded_urls, self.focus_calls, self.on_top = [], 0, False
        self.destroyed = threading.Event()

    def load_url(self, u):
        self.loaded_urls.append(u)

    def restore(self):
        self.focus_calls += 1

    def show(self):
        pass

    def destroy(self):
        self.destroyed.set()


def fake_webview(script=None):
    """A stand-in for pywebview; `script(window)` runs inside start() (the GUI loop), then the window closes."""
    m = types.ModuleType("webview")
    m.settings, m.windows, m.starts = {}, [], []

    def create_window(title, url=None, **kw):
        w = FakeWindow(title, url, **kw)
        m.windows.append(w)
        return w

    def start(**kw):
        m.starts.append(kw)
        w = m.windows[-1]
        for path in ("/", "/app.js"):                                   # what a real window loads first
            with urllib.request.urlopen(w.url + path, timeout=10) as r:
                assert r.status == 200
        w.events.loaded.fire()
        if script:
            script(w)
        w.events.closed.fire()                                         # the user closed the window
    m.create_window, m.start = create_window, start
    return m


class use_webview:
    """Install a fake `webview` module for the block; touches ONLY that sys.modules key (patch.dict
    would also drop modules first imported inside the block, e.g. numpy, which cannot be re-imported)."""

    def __init__(self, module):
        self.module = module

    def __enter__(self):
        self.prev = sys.modules.get("webview")
        sys.modules["webview"] = self.module
        return self.module

    def __exit__(self, *_):
        if self.prev is None:
            sys.modules.pop("webview", None)
        else:
            sys.modules["webview"] = self.prev
        return False


def control(info, path, body=None):
    req = urllib.request.Request(info["url"] + path, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST",
                                 headers={dw.TOKEN_HEADER: info["control_token"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read())


class TestNativeWindowLifecycle(unittest.TestCase):
    def test_window_startup_navigation_focus_and_clean_close(self):
        seen = {}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ws"

            def script(w):
                info = json.loads((root / "logs" / "runtime.json").read_text())
                seen["info"] = info
                seen["status"] = control(info, "/api/desktop/status")[1]
                seen["navigate"] = control(info, "/api/desktop/navigate", {"route": "/datasets"})
                seen["focus"] = control(info, "/api/desktop/focus", {})
                with urllib.request.urlopen(info["url"] + "/api/datasets", timeout=10) as r:
                    seen["datasets"] = json.loads(r.read())

            wv = fake_webview(script)
            settings = Path(d) / "app" / "settings.json"
            with use_webview(wv), mock.patch.dict("os.environ", {"EDGELAB_SETTINGS": str(settings)}), \
                    mock.patch.object(dw, "window_runtime_problem", return_value=None), \
                    redirect_stdout(io.StringIO()):
                code = desktop.run(["--data-root", str(root)])          # default: the native window
            self.assertEqual(code, 0)
            info = seen["info"]
            self.assertEqual(info["ui"], "window")
            self.assertTrue(info["url"].startswith("http://127.0.0.1:"))
            self.assertEqual(Path(info["data_root"]), root.resolve())
            w = wv.windows[0]
            self.assertEqual((w.title, w.url), ("EdgeLab", info["url"]))
            self.assertEqual((w.kw["width"], w.kw["height"], w.kw["resizable"]), (dw.SIZE[0], dw.SIZE[1], True))
            self.assertEqual(wv.starts[0]["debug"], False)                 # no developer tooling
            self.assertEqual(wv.starts[0]["private_mode"], False)
            self.assertEqual(Path(wv.starts[0]["storage_path"]), settings.parent / "webview")   # app-level, not per workspace
            self.assertFalse(settings.exists())                     # --data-root is explicit: nothing persisted
            self.assertEqual(wv.starts[0]["gui"], "edgechromium" if sys.platform == "win32" else None)
            self.assertFalse(wv.settings["ALLOW_DOWNLOADS"])
            st = seen["status"]
            self.assertEqual(st["ui"], "window")
            self.assertEqual(st["window"]["loaded"], 1)
            self.assertGreaterEqual(st["served"].get("/", 0), 1)
            self.assertGreaterEqual(st["served"].get("/app.js", 0), 1)
            self.assertEqual(seen["navigate"], (200, {"navigated": True, "url": f"{info['url']}/#/datasets"}))
            self.assertEqual(w.loaded_urls, [f"{info['url']}/#/datasets"])
            self.assertEqual(seen["focus"], (200, {"focused": True}))
            self.assertEqual(w.focus_calls, 1)
            self.assertEqual(seen["datasets"], [])
            # window closed -> backend stopped, runtime file removed, lock released, store intact
            self.assertFalse((root / "logs" / "runtime.json").exists())
            with socket.socket() as s:
                self.assertNotEqual(s.connect_ex(("127.0.0.1", info["port"])), 0)
            lock = desktop.InstanceLock(root / "logs" / desktop.LOCK_FILE)
            self.assertTrue(lock.acquire())
            lock.release()
            con = sqlite3.connect(root / "data" / "edgelab.sqlite")
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            con.close()
            self.assertTrue((root / "configs" / "storage.yaml").exists())    # persistent workspace kept

    def test_missing_window_runtime_refuses_clearly_before_starting(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ws"
            err = io.StringIO()
            with mock.patch.object(dw, "window_runtime_problem", return_value=dw.WEBVIEW2_MISSING), redirect_stderr(err):
                code = desktop.run(["--data-root", str(root)])
            self.assertEqual(code, 1)
            self.assertIn("WebView2 Runtime", err.getvalue())
            self.assertIn("--ui browser", err.getvalue())
            self.assertIn("WebView2 Runtime", (root / "logs" / "desktop.log").read_text())
            self.assertFalse((root / "logs" / "runtime.json").exists())      # nothing was started
            lock = desktop.InstanceLock(root / "logs" / desktop.LOCK_FILE)
            self.assertTrue(lock.acquire())
            lock.release()

    def test_second_launch_uses_the_running_backend(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ws"
            first = subprocess.Popen([sys.executable, "-m", "edgelab.desktop", "--data-root", str(root), "--ui", "none"],
                                     cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                rt = root / "logs" / "runtime.json"
                for _ in range(600):
                    if rt.exists():
                        break
                    first.wait(0) if first.poll() is not None else threading.Event().wait(0.2)
                info = json.loads(rt.read_text())
                with self.assertRaises(urllib.error.HTTPError) as cm:              # headless: no window to focus
                    control(info, "/api/desktop/focus", {})
                self.assertEqual(cm.exception.code, 409)
                wv = fake_webview()
                with use_webview(wv), \
                        mock.patch.object(dw, "window_runtime_problem", return_value=None), redirect_stdout(io.StringIO()):
                    code = desktop.run(["--data-root", str(root)])
                self.assertEqual(code, 0)
                self.assertEqual(wv.windows[0].url, info["url"])                    # a window onto the SAME backend
                self.assertEqual(json.loads(rt.read_text())["pid"], first.pid)     # no second server
            finally:
                first.terminate()
                first.wait(30)


class TestControlChannelAndRuntime(unittest.TestCase):
    def test_control_routes_require_the_token_and_validate_input(self):
        from flask import Flask
        app = Flask("t")
        ctl = mock.Mock()
        ctl.state = {"created": True, "loaded": 1, "closed": False, "last_url": "x"}
        ctl.navigate.return_value = True
        dw.install_control(app, "secret", "window", ctl)
        c = app.test_client()
        self.assertEqual(c.get("/api/desktop/status").status_code, 403)
        self.assertEqual(c.post("/api/desktop/focus", headers={dw.TOKEN_HEADER: "wrong"}).status_code, 403)
        ok = {dw.TOKEN_HEADER: "secret"}
        self.assertEqual(c.post("/api/desktop/navigate", json={"route": "javascript:alert(1)"}, headers=ok).status_code, 400)
        self.assertEqual(c.post("/api/desktop/navigate", json={"route": "/strategies"}, headers=ok).status_code, 200)
        ctl.navigate.assert_called_once_with("/strategies")
        self.assertEqual(c.get("/api/desktop/status", headers=ok).get_json()["ui"], "window")

    def test_webview2_detection_from_the_registry(self):
        class Key:                                                      # context manager yielding the "pv" value
            def __init__(self, v):
                self.v = v

            def __enter__(self):
                return self.v

            def __exit__(self, *_):
                return False

        class R:                                                        # fake winreg
            HKEY_LOCAL_MACHINE, HKEY_CURRENT_USER = "HKLM", "HKCU"

            def __init__(self, values):
                self.values = values

            def OpenKey(self, hive, path):
                if (hive, path) not in self.values:
                    raise OSError("absent")
                return Key(self.values[(hive, path)])

            def QueryValueEx(self, key, name):
                return key, 1

        wow = ("HKLM", r"SOFTWARE\WOW6432Node" + "\\" + dw.WEBVIEW2_CLIENT)
        user = ("HKCU", r"Software" + "\\" + dw.WEBVIEW2_CLIENT)
        self.assertEqual(dw.webview2_version(R({wow: "128.0.2739.42"})), "128.0.2739.42")
        self.assertEqual(dw.webview2_version(R({user: "129.0.1"})), "129.0.1")
        self.assertIsNone(dw.webview2_version(R({wow: "0.0.0.0"})))       # registered but not installed
        self.assertIsNone(dw.webview2_version(R({})))

    def test_windowed_exe_output_goes_to_a_log(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d)
            with mock.patch.object(sys, "stdout", None), mock.patch.object(sys, "stderr", None):
                desktop._redirect_missing_streams(logs)
                print("hello from a windowed exe")
                sys.stdout.flush()
                self.assertIn("hello from a windowed exe", (logs / "console.log").read_text())
                sys.stdout.close()


if __name__ == "__main__":
    unittest.main()
