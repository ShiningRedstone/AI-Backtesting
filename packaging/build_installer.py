"""Build the Munyun Lab Windows installer from a finished build (ADR-76). Windows only; needs Inno Setup 6 (ISCC).

    python packaging/build_installer.py [--dist dist/EdgeLab] [--out dist/release] [--iscc PATH]

Produces in --out:
  MunyunLab-Setup-<version>[-b<n>].exe   per-user installer (no administrator rights) of the packaged folder
and appends its SHA-256 to SHA256SUMS.txt there. The installer puts the program in %LOCALAPPDATA%\\Programs\\EdgeLab,
the folder the built-in updater replaces, so an installed copy keeps updating itself. Refuses when the packaged
folder or its build manifest is missing. ISCC is looked up in --iscc, the ISCC environment variable, PATH and the
default Inno Setup 6 folders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ISS = REPO / "packaging" / "installer.iss"
ICON = REPO / "packaging" / "munyun.ico"
MANIFEST_FILE = "edgelab_build.json"


def find_iscc(explicit: str | None = None) -> Path | None:
    cands = [explicit, os.environ.get("ISCC"), shutil.which("iscc"), shutil.which("ISCC.exe")]
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            cands.append(str(Path(base) / "Inno Setup 6" / "ISCC.exe"))
            cands.append(str(Path(base) / "Programs" / "Inno Setup 6" / "ISCC.exe"))
    for c in cands:
        if c and Path(c).is_file():
            return Path(c)
    return None


def read_manifest(dist: Path) -> dict:
    for base in (dist, dist / "_internal"):
        p = base / MANIFEST_FILE
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
    raise SystemExit(f"no {MANIFEST_FILE} in {dist}: build the app first (python packaging/build.py)")


def installer_name(version: str, build_number: int) -> str:
    return f"MunyunLab-Setup-{version}" + (f"-b{build_number}" if build_number else "")


def iscc_defines(m: dict, dist: Path, out: Path) -> list[str]:
    version = str(m["app_version"])
    n = int(m.get("build_number") or 0)
    nums = (version.split(".") + ["0", "0", "0"])[:3]
    return [f"/DAppVersion={version}", f"/DBuildNumber={n}",
            f"/DFileVersion={'.'.join(nums)}.{n % 65536}",
            f"/DSourceDir={dist.resolve()}", f"/DOutputDir={out.resolve()}",
            f"/DOutputBase={installer_name(version, n)}", f"/DIconFile={ICON}"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dist", default=str(REPO / "dist" / "EdgeLab"))
    ap.add_argument("--out", default=str(REPO / "dist" / "release"))
    ap.add_argument("--iscc", help="path to ISCC.exe (Inno Setup 6 compiler)")
    a = ap.parse_args(argv)
    if sys.platform != "win32":
        raise SystemExit("the Windows installer is built on Windows (Inno Setup); use the GitHub Actions build")
    dist, out = Path(a.dist), Path(a.out)
    if not (dist / "EdgeLab.exe").is_file():
        raise SystemExit(f"no EdgeLab.exe in {dist}: build the app first (python packaging/build.py)")
    m = read_manifest(dist)
    iscc = find_iscc(a.iscc)
    if iscc is None:
        raise SystemExit("Inno Setup 6 not found: install it (https://jrsoftware.org/isdl.php or "
                         "`choco install innosetup`) or pass --iscc / set ISCC")
    out.mkdir(parents=True, exist_ok=True)
    defines = iscc_defines(m, dist, out)
    subprocess.run([str(iscc), "/Q", *defines, str(ISS)], check=True)
    exe = out / (installer_name(str(m["app_version"]), int(m.get("build_number") or 0)) + ".exe")
    if not exe.is_file():
        raise SystemExit(f"ISCC finished but {exe} is missing")
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    with open(out / "SHA256SUMS.txt", "a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{digest}  {exe.name}\n")
    print(f"installer: {exe}\nsha256:    {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
