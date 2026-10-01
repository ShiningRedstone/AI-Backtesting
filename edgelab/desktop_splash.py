"""The start-up splash of the desktop app (ADR-79): a small native "Starting Munyun Lab…" window.

Started by the launcher as a separate process (``EdgeLab.exe --splash --parent <pid>``) so the main window's GUI loop is
never involved. It has no server and uses a private (in-memory) WebView2 profile. It closes itself when the launcher
process ends, after at most MAX_SECONDS, or when the launcher terminates it (once the app window has loaded).
"""
from __future__ import annotations

import argparse
import sys
import time

MAX_SECONDS = 180.0
HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#151516;color:#f4f4f5;font:15px system-ui,-apple-system,"Segoe UI",sans-serif;
  display:flex;align-items:center;justify-content:center;overflow:hidden;user-select:none}
.box{text-align:center}.logo{font-weight:700;letter-spacing:.14em;font-size:13px;color:#e4e4e7;margin-bottom:14px}
.bar{width:180px;height:3px;border-radius:3px;background:#2a2a2e;overflow:hidden;margin:16px auto 0}
.bar i{display:block;width:40%;height:100%;border-radius:3px;background:linear-gradient(90deg,#7a2350,#de5c96);
  animation:s 1.2s ease-in-out infinite}@keyframes s{0%{transform:translateX(-100%)}100%{transform:translateX(250%)}}
.sub{color:#a1a1aa;font-size:12.5px;margin-top:6px}</style></head>
<body><div class="box"><div class="logo">MUNYUN LAB</div><div>Starting Munyun Lab…</div>
<div class="sub">Preparing your research workspace</div><div class="bar"><i></i></div></div></body></html>"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="EdgeLab --splash")
    ap.add_argument("--parent", type=int, default=0)
    a = ap.parse_args(argv)
    try:
        import webview
    except Exception:                                         # noqa: BLE001 - no splash without pywebview
        return 0
    from edgelab.updater.apply import pid_alive
    window = webview.create_window("Munyun Lab", html=HTML, width=380, height=200, resizable=False, frameless=True,
                                   on_top=True, background_color="#151516")

    def watch():
        t0 = time.monotonic()
        while time.monotonic() - t0 < MAX_SECONDS and (not a.parent or pid_alive(a.parent)):
            time.sleep(0.3)
        try:
            window.destroy()
        except Exception:                                     # noqa: BLE001 - already gone
            pass
    try:
        webview.start(watch, gui="edgechromium" if sys.platform == "win32" else None, private_mode=True, debug=False)
    except Exception:                                         # noqa: BLE001 - a splash is never required
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
