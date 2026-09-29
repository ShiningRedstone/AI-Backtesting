"""Reproducible desktop build: frontend -> build manifest -> PyInstaller -> dist/EdgeLab/.

    python packaging/build.py [--skip-frontend] [--smoke]

Steps (each fails loudly):
  1. install the locked frontend dependencies (`npm ci` from web/package-lock.json into
     web/node_modules; nothing global) and rebuild the React bundle with web/build.mjs
     (or --skip-frontend, only accepted if the committed bundle matches web/src);
  2. verify the bundle is up to date with its sources;
  3. generate build/edgelab_build.json from the real sources (edgelab.runtime);
  4. run PyInstaller with packaging/edgelab.spec (folder mode);
  5. copy the manifest next to the executable for humans; optionally run the packaged smoke test.
Output: dist/EdgeLab/EdgeLab.exe (windowed desktop app) + dist/EdgeLab/EdgeLabConsole.exe (console:
CLI, logs, headless smoke) on Windows; without .exe on other platforms. --smoke runs the headless
packaged smoke test and, on Windows, the native-window test (packaging/window_test_windows.py).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def step(msg: str) -> None:
    print(f"\n==> {msg}", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-frontend", action="store_true", help="use the committed bundle (must be up to date)")
    ap.add_argument("--smoke", action="store_true", help="run packaging/smoke_packaged.py on the result")
    a = ap.parse_args(argv)
    if importlib.util.find_spec("PyInstaller") is None:
        sys.exit("PyInstaller is not installed: pip install -r packaging/requirements-build.txt")
    if importlib.util.find_spec("duckdb") is not None:
        sys.exit("duckdb is installed in this build environment; the desktop build uses SQLite. "
                 "Build from a clean virtual environment without duckdb.")

    if not a.skip_frontend:
        node, npm = shutil.which("node"), shutil.which("npm")
        if not (node and npm):
            sys.exit("Node/npm are not available; install Node 18+ (developers only) or pass --skip-frontend")
        if not (REPO / "web" / "package-lock.json").is_file():
            sys.exit("web/package-lock.json is missing; the frontend dependencies cannot be installed reproducibly")
        step("installing the locked frontend dependencies (npm ci in web/)")
        subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=REPO / "web", check=True)
        step("building the frontend (web/build.mjs)")
        subprocess.run([node, "build.mjs"], cwd=REPO / "web", check=True)
    step("verifying the frontend bundle matches its sources")
    from edgelab.web.bundle import bundle_status
    st = bundle_status()
    if not (st.get("built") and st.get("up_to_date")):
        sys.exit(f"the frontend bundle is missing or stale: {st}")

    step("generating the build manifest")
    import PyInstaller
    from edgelab import runtime
    m = runtime.generate_build_manifest(REPO, extra={"pyinstaller": PyInstaller.__version__})
    if m["git_tracked_changes"]:
        print("WARNING: tracked files have uncommitted changes; the manifest records git_tracked_changes=true "
              "and the source hash of the working tree.")
    out = REPO / "build"
    out.mkdir(exist_ok=True)
    mf = out / runtime.BUILD_MANIFEST
    mf.write_text(json.dumps(m, indent=1, sort_keys=True) + "\n")
    print(f"build id {m['build_id']}  source {m['source_sha256'][:16]}  commit {m['git_commit']}")

    step("running PyInstaller (folder mode)")
    env = {**os.environ, "EDGELAB_BUILD_MANIFEST": str(mf)}
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(REPO / "dist"), "--workpath", str(out / "pyinstaller"),
                    str(REPO / "packaging" / "edgelab.spec")], cwd=REPO, env=env, check=True)
    app = REPO / "dist" / "EdgeLab"
    ext = ".exe" if os.name == "nt" else ""
    exe, console = app / f"EdgeLab{ext}", app / f"EdgeLabConsole{ext}"
    for f in (exe, console):
        if not f.is_file():
            sys.exit(f"PyInstaller finished but {f} is missing")
    shutil.copy2(mf, app / runtime.BUILD_MANIFEST)
    print(f"\nbuilt {exe}\n      {console}")
    if a.smoke:
        step("packaged smoke test (headless, EdgeLabConsole)")
        code = subprocess.run([sys.executable, str(REPO / "packaging" / "smoke_packaged.py"), "--exe", str(exe)]).returncode
        if code:
            return code
        if sys.platform == "win32":
            step("native window test (EdgeLab.exe opens its own window; it closes itself when done)")
            return subprocess.run([sys.executable, str(REPO / "packaging" / "window_test_windows.py"), "--exe", str(exe)]).returncode
        print("native window test skipped: it needs Windows (WebView2); run packaging/window_test_windows.py there")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
