# PyInstaller spec for the EdgeLab desktop app (folder mode). Build with build_windows.ps1 or
#   python packaging/build.py
# which generates the build manifest first and passes it in EDGELAB_BUILD_MANIFEST.
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

REPO = Path(SPECPATH).resolve().parent
manifest = os.environ.get("EDGELAB_BUILD_MANIFEST")
if not manifest or not Path(manifest).is_file():
    raise SystemExit("EDGELAB_BUILD_MANIFEST is not set: build through packaging/build.py")

datas = [
    (str(REPO / "edgelab" / "web" / "static"), "edgelab/web/static"),   # built React frontend
    (str(REPO / "configs"), "configs"),                                 # bundled DEFAULT configs (read-only)
    (str(REPO / "strategies" / "fixtures"), "strategies/fixtures"),     # demo workspace fixtures
    (manifest, "."),                                                    # edgelab_build.json (code identity)
]
try:
    datas += collect_data_files("tzdata")                               # required on Windows
except Exception:
    pass
# Native window: pywebview (+ its WebView2 loader DLLs / JS on Windows). Required, not optional:
# the build fails loudly if it is missing instead of producing an exe without a window.
import importlib.util
if importlib.util.find_spec("webview") is None:
    raise SystemExit("pywebview is not installed in the build environment: pip install -r packaging/requirements-build.txt")
datas += collect_data_files("webview")
hidden = collect_submodules("edgelab") + collect_submodules("webview")
if sys.platform == "win32":
    hidden += ["clr", "clr_loader"]                                     # pythonnet (WinForms + WebView2 host)

a = Analysis(
    [str(REPO / "packaging" / "launcher.py")],
    pathex=[str(REPO)],
    datas=datas,
    hiddenimports=hidden,
    excludes=["duckdb", "tkinter", "matplotlib", "IPython", "pytest", "playwright", "numba"],
    noarchive=False,
)
pyz = PYZ(a.pure)
# Two launchers over one bundle: EdgeLab (windowed, no console: the desktop app) and EdgeLabConsole
# (console: logs visible, CLI pass-through, headless smoke tests). Same code, same manifest.
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="EdgeLab", console=False,
          debug=False, strip=False, upx=False)
exe_console = EXE(pyz, a.scripts, [], exclude_binaries=True, name="EdgeLabConsole", console=True,
                  debug=False, strip=False, upx=False)
coll = COLLECT(exe, exe_console, a.binaries, a.datas, strip=False, upx=False, name="EdgeLab")
