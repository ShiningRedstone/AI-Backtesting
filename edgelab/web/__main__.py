"""Start the EdgeLab web application.

    python -m edgelab.web                 # real workspace (this directory)
    python -m edgelab.web --demo          # separate synthetic demo workspace (./demo_workspace)
    python -m edgelab.web --root DIR --port 8765
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from edgelab.runtime import resource_dir

REPO = resource_dir()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m edgelab.web", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="project root (default: current directory; demo: ./demo_workspace)")
    ap.add_argument("--demo", action="store_true", help="synthetic demo workspace, kept separate from real data")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--allow-remote", action="store_true",
                    help="allow binding to a non-loopback address (there is NO authentication)")
    a = ap.parse_args(argv)

    from edgelab.web.config import load_web_config
    from edgelab.web.demo import NOTICE, create_demo_workspace, is_demo_root
    root = Path(a.root or ("demo_workspace" if a.demo else ".")).resolve()
    if a.demo:
        info = create_demo_workspace(root, REPO)
        print(("created" if info["created"] else "using") + f" demo workspace {root}\n{NOTICE}")
    elif is_demo_root(root):
        print(f"{root} is a demo workspace; start it with --demo", file=sys.stderr)
        return 2
    if not (root / "configs").is_dir():
        print(f"no configs/ directory in {root}; run from the project root or pass --root", file=sys.stderr)
        return 2
    web = load_web_config(root)
    host, port = a.host or web.host, a.port or web.port
    from dataclasses import replace
    web = replace(web, host=host, port=port)
    if not web.is_loopback and not a.allow_remote:
        print(f"refusing to bind to {host}: the app has no authentication. Use --allow-remote to override.",
              file=sys.stderr)
        return 2
    from edgelab.web.app import create_app
    app = create_app(root, demo=a.demo, web=web)
    print(f"Munyun Lab running at http://{host}:{port}  (root: {root}{', DEMO' if a.demo else ''})  Ctrl+C to stop")
    from werkzeug.serving import run_simple
    run_simple(host, port, app, threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
