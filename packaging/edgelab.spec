# PyInstaller spec for the EdgeLab desktop app (folder mode). Build with build_windows.ps1 or
#   python packaging/build.py
# which generates the build manifest first and passes it in EDGELAB_BUILD_MANIFEST.
import os
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

a = Analysis(
    [str(REPO / "packaging" / "launcher.py")],
    pathex=[str(REPO)],
    datas=datas,
    hiddenimports=collect_submodules("edgelab"),
    excludes=["duckdb", "tkinter", "matplotlib", "IPython", "pytest", "playwright", "numba"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="EdgeLab", console=True,
          debug=False, strip=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="EdgeLab")
