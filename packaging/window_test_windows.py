"""WINDOWS-ONLY integration test of the native EdgeLab window (real WebView2, real window).

    python packaging\\window_test_windows.py --exe dist\\EdgeLab\\EdgeLab.exe

Uses a fresh SCRATCH data root with the synthetic demo workspace; never a real research store.
Steps (each is checked, nothing is assumed):
  1. launch the windowed EdgeLab.exe (default UI mode: its own window, no browser);
  2. wait for logs\\runtime.json (backend ready: written only after /api/health answered);
  3. find the process's visible top-level window titled "EdgeLab" (Win32 EnumWindows);
  4. confirm the React UI loaded INSIDE that window: the backend served "/", "/app.js" and API
     calls to it (loopback control channel, per-launch token) and the window reported loaded;
  5. navigate the window to Datasets and Strategy Lab and confirm their API calls were served;
  6. close the window like a user (WM_CLOSE) and confirm the process exits with code 0, runtime.json
     is removed, the lock is released and SQLite integrity_check passes.
Prints PASS/FAIL; exit code 0 only on PASS. Exits 2 (not run) on non-Windows systems.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

TOKEN_HEADER = "X-EdgeLab-Desktop-Token"
problems: list[str] = []


def check(cond, what):
    print(("  OK   " if cond else "  FAIL ") + what, flush=True)
    if not cond:
        problems.append(what)
    return cond


def control(info, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(info["url"] + path, data=data, method="POST" if data is not None else "GET",
                                 headers={TOKEN_HEADER: info["control_token"], "Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=15) as r:   # loopback: no proxy
        return json.loads(r.read())


def edgelab_windows(pid: int) -> list[tuple[int, str]]:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            found.append((hwnd, buf.value))
        return True
    user32.EnumWindows(cb, 0)
    return found


def wait_for(fn, timeout, what):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            v = fn()
            if v:
                return v
        except OSError:
            pass
        time.sleep(0.5)
    check(False, f"{what} (timed out after {timeout}s)")
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True, help="dist\\EdgeLab\\EdgeLab.exe (the windowed launcher)")
    ap.add_argument("--keep", action="store_true", help="keep the scratch data root")
    a = ap.parse_args(argv)
    if sys.platform != "win32":
        print("NOT RUN: the native-window test needs Windows (Microsoft Edge WebView2).")
        return 2
    exe = Path(a.exe).resolve()
    base = Path(tempfile.mkdtemp(prefix="edgelab_window_")) / "root"
    root = base / "demo"
    print(f"exe {exe}\nscratch data root {base}")
    check(exe.is_file(), "EdgeLab.exe exists")
    # scratch settings (incl. the WebView2 profile and update state), update staging and an EMPTY local release
    # folder: the test never touches %APPDATA%\EdgeLab or %LOCALAPPDATA%\EdgeLab and never contacts GitHub
    (base.parent / "no-release").mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "EDGELAB_SETTINGS": str(base.parent / "settings" / "settings.json"),
           "EDGELAB_UPDATE_CACHE": str(base.parent / "update-cache"), "EDGELAB_UPDATE_SOURCE": str(base.parent / "no-release")}
    proc = subprocess.Popen([str(exe), "--data-root", str(base), "--demo"], env=env)
    try:
        print("\n1-2. launch and backend readiness")
        rt = root / "logs" / "runtime.json"
        info = wait_for(lambda: rt.exists() and json.loads(rt.read_text()), 240, "runtime.json written after /api/health")
        if not info:
            return 1
        check(info["ui"] == "window", f"UI mode is the native window (ui={info['ui']}); no browser is opened")
        check(info["url"].startswith("http://127.0.0.1:"), f"backend on loopback {info['url']}")
        check(Path(info["data_root"]).resolve() == root.resolve(), "persistent data root is the scratch root")
        check(not str(root.resolve()).startswith(str(exe.parent)), "data root outside the bundle")

        print("\n3. native window")
        wins = wait_for(lambda: [w for w in edgelab_windows(proc.pid) if w[1] == "EdgeLab"], 60, "visible 'EdgeLab' window")
        check(bool(wins), f"visible top-level window titled 'EdgeLab' owned by pid {proc.pid}")
        hwnd = wins[0][0] if wins else None

        print("\n4. React UI inside the window")
        st = wait_for(lambda: (lambda s: s if s["window"]["loaded"] and s["served"].get("/app.js") else None)(
            control(info, "/api/desktop/status")), 60, "window loaded the UI")
        if st:
            check(st["served"].get("/", 0) >= 1 and st["served"].get("/app.js", 0) >= 1,
                  f"page and bundle served to the window ({st['served']})")
            check(any(k.startswith("/api/") for k in st["served"]), "the UI called the API")

        print("\n5. Datasets and Strategy Lab pages")
        for route, api in (("/datasets", "/api/datasets"), ("/strategies", "/api/strategies")):
            before = control(info, "/api/desktop/status")["served"].get(api, 0)
            check(control(info, "/api/desktop/navigate", {"route": route})["navigated"], f"navigate window to #{route}")
            if wait_for(lambda: control(info, "/api/desktop/status")["served"].get(api, 0) > before, 60,
                        f"{route} page loaded ({api} served)"):
                check(True, f"{route} page loaded ({api} served)")
        check(control(info, "/api/desktop/status")["served"].get("/api/update", 0) >= 1,
              "the window asked for the update status (local empty release: no GitHub contact)")

        print("\n6. close the window -> clean shutdown")
        if hwnd:
            import ctypes
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)          # WM_CLOSE, as the title-bar X
        try:
            code = proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            code = None
        check(code == 0, f"process exited cleanly after the window closed (exit code {code})")
        check(not rt.exists(), "runtime.json removed")
        db = root / "data" / "edgelab.sqlite"
        con = sqlite3.connect(db)
        try:
            check(con.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity_check ok")
        finally:
            con.close()
    finally:
        if proc.poll() is None:
            proc.kill()
    lock = root / "logs" / "edgelab.lock"
    try:
        import msvcrt
        with open(lock, "a+") as fh:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        check(True, "instance lock released")
    except OSError:
        check(False, "instance lock released")
    if not a.keep:
        shutil.rmtree(base.parent, ignore_errors=True)
    print("\n==== NATIVE WINDOW TEST:", "PASS" if not problems else f"FAIL {problems}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
