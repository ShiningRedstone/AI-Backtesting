"""Reproducible desktop build: frontend -> build manifest -> PyInstaller -> dist/EdgeLab/.

    python packaging/build.py [--skip-frontend] [--smoke] [--channel BRANCH --build-number N]

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


def version_resource(version: str, build_id: str, build_number: int = 0) -> str:
    """PyInstaller VSVersionInfo text: Windows file properties show the same version as the app (the 4th field
    carries the CI build number, modulo the 16-bit limit of that field)."""
    a, b, c = (int(x) for x in version.split("."))
    d = int(build_number) % 65536
    fields = {"CompanyName": "Munyun Lab", "FileDescription": "Munyun Lab research application",
              "FileVersion": version, "InternalName": "EdgeLab", "ProductName": "Munyun Lab",
              "ProductVersion": version, "Comments": f"build {build_id}"}
    strings = ",\n            ".join(f"StringStruct('{k}', '{v}')" for k, v in fields.items())
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({a}, {b}, {c}, {d}), prodvers=({a}, {b}, {c}, {d}), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
            {strings}])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])])])
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-frontend", action="store_true", help="use the committed bundle (must be up to date)")
    ap.add_argument("--smoke", action="store_true", help="run packaging/smoke_packaged.py on the result")
    ap.add_argument("--channel", help="git branch this build comes from (CI branch builds; the app updates from it)")
    ap.add_argument("--build-number", type=int, default=0, help="monotonic CI build number (e.g. github.run_number)")
    a = ap.parse_args(argv)
    if bool(a.channel) != bool(a.build_number):
        sys.exit("--channel and --build-number go together (a branch build needs both)")
    if a.build_number < 0:
        sys.exit("--build-number must be positive")
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

    import edgelab
    if st.get("app_version") != edgelab.__version__:
        sys.exit(f"the frontend bundle was built for version {st.get('app_version')}, the backend is "
                 f"{edgelab.__version__}: rebuild the frontend")

    step("generating the build manifest")
    import PyInstaller
    from edgelab import runtime
    extra = {"pyinstaller": PyInstaller.__version__}
    if a.channel:
        extra.update(channel=a.channel, build_number=a.build_number)
    m = runtime.generate_build_manifest(REPO, extra=extra)
    if m["git_tracked_changes"]:
        print("WARNING: tracked files have uncommitted changes; the manifest records git_tracked_changes=true "
              "and the source hash of the working tree.")
    out = REPO / "build"
    out.mkdir(exist_ok=True)
    mf = out / runtime.BUILD_MANIFEST
    mf.write_text(json.dumps(m, indent=1, sort_keys=True) + "\n")
    print(f"build id {m['build_id']}  source {m['source_sha256'][:16]}  commit {m['git_commit']}"
          + (f"  branch {a.channel} build {a.build_number}" if a.channel else ""))

    vf = out / "edgelab_version_info.txt"
    vf.write_text(version_resource(edgelab.__version__, m["build_id"], a.build_number), encoding="utf-8")

    step("running PyInstaller (folder mode)")
    env = {**os.environ, "EDGELAB_BUILD_MANIFEST": str(mf), "EDGELAB_VERSION_FILE": str(vf)}
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
