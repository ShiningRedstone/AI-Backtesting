"""End-to-end test of the PACKAGED updater against LOCAL release fixtures (no GitHub, copies only).

    python packaging/smoke_update.py --dist dist/EdgeLab [--scratch DIR] [--results-json FILE] [--keep]

Everything happens in the scratch folder: settings (EDGELAB_SETTINGS: settings file, update state, update log,
WebView profile), update staging (EDGELAB_UPDATE_CACHE), release fixtures (EDGELAB_UPDATE_SOURCE = a local folder),
the "installation" (a copy of dist) and a synthetic demo workspace (--data-root). dist/ and the user's real data
(%LOCALAPPDATA%\\EdgeLab, %LOCALAPPDATA%\\EdgeLab-Updater, %APPDATA%\\EdgeLab) are never touched. Only phase 8
uses the GitHub source, with HTTP(S)_PROXY pointed at a closed local port, so nothing leaves the machine.

Phases (each check is PASS / FAIL; Windows-only phases are SKIP elsewhere):
  1 version       installed copy starts; /api/version; install dir and staging are the scratch copies
  2 check         manual check offers the newer fixture release with notes; "Later" hides the prompt
  3 stage         download + size/SHA-256 verification + staging; nothing installed yet
  4 update        "Restart and update": old app exits, helper swaps, the new build reports READY, datasets/runs
                  unchanged, update_completed logged, helper exited, no leftover folders
  5 corrupted     CHECKSUM_MISMATCH, discarded, apply refused, installation unchanged
  6 startup-fail  a new build that cannot start (invalid build manifest; on Windows the windowed exe shows a
                  modal error dialog): never ready -> helper stops it, restores and relaunches the previous
                  version; update_failed (not completed); helper and the stuck process gone
  7 locked        WINDOWS ONLY: an open file inside the installation blocks the rename; update abandoned,
                  installation unchanged, previous version relaunched, helper gone
  8 offline       GitHub source with the network blocked locally: OFFLINE, no prompt, app fully usable
  9 integrity     SQLite integrity_check of the scratch demo workspace
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
results: list[dict] = []
_phase = ["setup"]
LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never send 127.0.0.1 through a proxy


def phase(name: str, title: str) -> None:
    _phase[0] = name
    print(f"\n[{name}] {title}", flush=True)


def check(cond, what) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {what}", flush=True)
    results.append({"phase": _phase[0], "status": "PASS" if cond else "FAIL", "check": what})
    return bool(cond)


def skip(what: str) -> None:
    print(f"  SKIP  {what}", flush=True)
    results.append({"phase": _phase[0], "status": "SKIP", "check": what})


def api(base, path, body=None, timeout=90):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"} if data else {})
    with LOOPBACK.open(req, timeout=timeout) as r:
        return json.loads(r.read())


def wait(pred, timeout=120, step=0.5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            v = pred()
            if v:
                return v
        except Exception:                                   # noqa: BLE001 - polling
            pass
        time.sleep(step)
    return None


def manifest_file(app: Path) -> Path:
    for p in (app / "edgelab_build.json", app / "_internal" / "edgelab_build.json"):
        if p.is_file():
            return p
    raise SystemExit(f"no edgelab_build.json in {app}")


def installed_version(app: Path) -> str:
    return json.loads(manifest_file(app).read_text())["app_version"]


def make_release(app: Path, out: Path, version: str, platform: str, corrupt=False) -> None:
    from edgelab.updater.core import MANIFEST_NAME, MANIFEST_SCHEMA
    out.mkdir(parents=True, exist_ok=True)
    name = f"EdgeLab-{version}-{platform}.zip"
    with zipfile.ZipFile(out / name, "w", zipfile.ZIP_DEFLATED, compresslevel=3) as z:
        for p in sorted(app.rglob("*")):
            if p.is_file():
                z.write(p, (Path("EdgeLab") / p.relative_to(app)).as_posix())
    raw = (out / name).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if corrupt:                                             # same size, different bytes
        b = bytearray(raw)
        b[len(b) // 2] ^= 0xFF
        (out / name).write_bytes(bytes(b))
    (out / MANIFEST_NAME).write_text(json.dumps({
        "schema": MANIFEST_SCHEMA, "app": "EdgeLab", "version": version, "tag": f"v{version}", "platform": platform,
        "published_at": "2026-10-01T00:00:00+00:00", "notes": f"smoke-test release {version} (local fixture)",
        "artifact": {"name": name, "size": len(raw), "sha256": sha, "app_dir": "EdgeLab"}}))


def _alive(pid: int) -> bool:
    from edgelab.updater.apply import pid_alive
    return pid_alive(pid)


def main(argv=None) -> int:
    import edgelab
    from edgelab.updater.apply import exe_name
    from edgelab.updater.core import current_platform
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=str(REPO / "dist" / "EdgeLab"))
    ap.add_argument("--scratch", help="scratch folder (must not exist); default: a new temp folder")
    ap.add_argument("--results-json", help="write the per-check results here (read by packaging/windows_validation.py)")
    ap.add_argument("--keep", action="store_true", help="keep the scratch folder")
    a = ap.parse_args(argv)
    dist = Path(a.dist).resolve()
    if not (dist / exe_name()).is_file():
        sys.exit(f"{dist / exe_name()} not found: build first")
    if a.scratch:
        scratch = Path(a.scratch).resolve()
        if scratch.exists():
            sys.exit(f"refusing: scratch folder {scratch} already exists")
        scratch.mkdir(parents=True)
    else:
        scratch = Path(tempfile.mkdtemp(prefix="edgelab_update_smoke_"))
    maj, mi, pa = (int(x) for x in edgelab.__version__.split("."))
    plat = current_platform()
    v1, v_bad, v_crash, v_lock = (f"{maj}.{mi}.{pa + k}" for k in (1, 2, 3, 4))
    print(f"scratch   {scratch}\ndist      {dist} (not modified)\ncode      {edgelab.__version__}  platform {plat}")
    install = scratch / "Programs" / "EdgeLab"
    data = scratch / "data"
    rt = data / "demo" / "logs" / "runtime.json"
    logf = scratch / "settings" / "logs" / "update.log"
    base_env = {k: v for k, v in os.environ.items() if k not in ("EDGELAB_DATA_ROOT", "EDGELAB_UPDATE_SOURCE")}
    base_env.update({"EDGELAB_SETTINGS": str(scratch / "settings" / "settings.json"),
                     "EDGELAB_UPDATE_CACHE": str(scratch / "update-cache"),
                     "EDGELAB_UPDATER_QUIET": "1",                 # no helper error dialog in an unattended test
                     "EDGELAB_UPDATE_READY_TIMEOUT": "120"})       # phase 6 waits this long on Windows (stuck dialog)
    console = install / ("EdgeLabConsole" + (".exe" if os.name == "nt" else ""))
    cmd = [str(console), "--data-root", str(data), "--no-browser", "--demo"]
    started: list[subprocess.Popen] = []

    def start(env):
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        started.append(p)
        info = wait(lambda: (lambda d: d if d["pid"] == p.pid else None)(json.loads(rt.read_text())), 240)
        return p, info

    def running(other_than: int):
        return wait(lambda: (lambda d: d if d["pid"] != other_than and _alive(d["pid"]) else None)(json.loads(rt.read_text())), 300)

    def stop_pid(pid):
        try:
            os.kill(pid, signal.SIGTERM)                    # Windows: TerminateProcess (a detached app has no console)
        except OSError:
            pass
        wait(lambda: not _alive(pid), 60)

    def events(since=0):
        return [json.loads(l) for l in logf.read_text().splitlines()[since:]] if logf.exists() else []

    def last_event(kind, since=0, timeout=300):
        return wait(lambda: ([e for e in events(since) if e.get("event") == kind] or [None])[-1], timeout)

    def log_len():
        return len(events())

    def leftovers():
        return sorted(p.name for p in install.parent.iterdir() if p.name != "EdgeLab")

    def download(base, version):
        api(base, "/api/update/download", {"version": version})
        return wait(lambda: (lambda x: x if x["download"]["state"] in ("ready", "error") else None)(api(base, "/api/update/status")), 600)

    try:
        shutil.copytree(dist, install, symlinks=False)
        newb = scratch / "newbuild" / "EdgeLab"
        shutil.copytree(dist, newb, symlinks=False)
        mfs = [p for p in (newb / "edgelab_build.json", newb / "_internal" / "edgelab_build.json") if p.is_file()]
        good_manifest = json.loads(manifest_file(newb).read_text())

        def release(version: str, folder: str, corrupt=False, cannot_start=False):
            """The fixture build as `version` (its manifest + marker say so), zipped into a local release.
            cannot_start: the build manifest loses its schema, so the frozen app refuses to start."""
            m = dict(good_manifest, app_version=version)
            if cannot_start:
                m.pop("schema", None)
            for mf in mfs:                                 # every copy (the app reads _internal, the helper the top one)
                mf.write_text(json.dumps(m, indent=1))
            (newb / "UPDATED_MARKER.txt").write_text(version)
            make_release(newb, scratch / folder, version, plat, corrupt=corrupt)

        release(v1, "rel-good")
        release(v_bad, "rel-bad", corrupt=True)
        release(v_crash, "rel-crash", cannot_start=True)
        release(v_lock, "rel-lock")
        v0 = installed_version(install)
        env_good = {**base_env, "EDGELAB_UPDATE_SOURCE": str(scratch / "rel-good")}

        phase("version", "installed copy starts (scratch settings, staging, workspace)")
        proc, info = start(env_good)
        if not check(info is not None, "installed app started and wrote runtime.json"):
            return finish(a, scratch, started, rt)
        base, pid = info["url"], info["pid"]
        ver = api(base, "/api/version")
        check(ver["version"] == edgelab.__version__ and ver["packaged"], f"version reported: {ver['version']} (packaged, build {ver['build_id']})")
        st = api(base, "/api/update/status")
        check(st["apply_supported"] and Path(st["install_dir"]).resolve() == install.resolve(), f"install dir is the scratch copy ({st['install_dir']})")
        check(Path(st["cache_dir"]).resolve() == (scratch / "update-cache").resolve(), "update staging is the scratch folder")
        check(Path(st["log"]).resolve() == logf.resolve(), "update log is in the scratch settings folder")
        check("local release folder" in st["source"], f"release source is the local fixture ({st['source']})")
        datasets0 = sorted(d["dataset_id"] for d in api(base, "/api/datasets"))
        runs0 = sorted(r["run_id"] for r in api(base, "/api/results"))

        phase("check", "manual update check + prompt")
        s = api(base, "/api/update/check", {})
        check(s["available"] and s["prompt"] and s["release"]["version"] == v1, f"newer release {v1} offered with a prompt")
        check(s["release"]["notes"].startswith("smoke-test release"), "release notes shown")
        s2 = api(base, "/api/update/later", {"version": v1})
        check(s2["available"] and not s2["prompt"], "'Later' hides the prompt for this session")

        phase("stage", "download, SHA-256 verification, staging")
        s = download(base, v1)
        check(s and s["download"]["state"] == "ready", f"downloaded and verified ({s and s['download'].get('bytes')} bytes)")
        check((scratch / "update-cache" / v1 / "READY.json").is_file(), "staged build marked READY")
        check(installed_version(install) == v0, "nothing installed before 'Restart and update'")

        phase("update", "restart and update (separate helper process from the staged build)")
        n0 = log_len()
        r = api(base, "/api/update/apply", {"version": v1})
        check(r.get("applying") is True, "apply accepted")
        check(wait(lambda: proc.poll() is not None, 90) is not None, f"old app exited (code {proc.poll()})")
        done = last_event("update_completed", n0)
        check(done is not None and done.get("restarted"), "helper: new build reported READY, update_completed logged")
        check(installed_version(install) == v1 and (install / "UPDATED_MARKER.txt").read_text() == v1, f"installation holds the new build ({installed_version(install)})")
        info2 = running(pid)
        if check(info2 is not None, "new build running on the same workspace"):
            check(api(info2["url"], "/api/health")["backend"] == "ok", "relaunched app serves the API")
            check(sorted(d["dataset_id"] for d in api(info2["url"], "/api/datasets")) == datasets0, "datasets unchanged")
            check(sorted(r["run_id"] for r in api(info2["url"], "/api/results")) == runs0, "runs unchanged")
            lu = api(info2["url"], "/api/update/status")["last_update"] or {}
            check(lu.get("event") == "update_completed" and lu.get("version") == v1, "Settings reports the completed update")
        started_ev = last_event("update_started", n0, 5) or {}
        check(started_ev.get("helper_pid") and wait(lambda: not _alive(started_ev["helper_pid"]), 60), "helper process exited")
        check(leftovers() == [], f"no backup / temp folders left ({leftovers()})")
        if info2:
            stop_pid(info2["pid"])

        phase("corrupted", "corrupted artifact: refused, nothing changes")
        before = sorted(p.relative_to(install).as_posix() for p in install.rglob("*"))
        p3, info3 = start({**base_env, "EDGELAB_UPDATE_SOURCE": str(scratch / "rel-bad")})
        if check(info3 is not None, "app started"):
            api(info3["url"], "/api/update/check", {})
            s = download(info3["url"], v_bad)
            check(s and s["download"]["state"] == "error" and s["download"]["error"]["code"] == "CHECKSUM_MISMATCH", "CHECKSUM_MISMATCH reported")
            check(not (scratch / "update-cache" / v_bad).exists(), "the bad download was discarded")
            try:
                api(info3["url"], "/api/update/apply", {"version": v_bad})
                check(False, "apply after a failed verification is refused")
            except urllib.error.HTTPError as e:
                check(e.code == 409, "apply after a failed verification is refused (409 NOT_STAGED)")
            check(sorted(p.relative_to(install).as_posix() for p in install.rglob("*")) == before, "installation unchanged")
            check(api(info3["url"], "/api/health")["backend"] == "ok", "app keeps running")
            stop_pid(info3["pid"])

        phase("startup-fail", "new build cannot start (Windows: modal error dialog): rolled back, previous relaunched")
        p5, info5 = start({**base_env, "EDGELAB_UPDATE_SOURCE": str(scratch / "rel-crash")})
        if check(info5 is not None, "app started"):
            api(info5["url"], "/api/update/check", {})
            s = download(info5["url"], v_crash)
            check(s and s["download"]["state"] == "ready", "the broken build still verifies (checksum is not the problem)")
            n0 = log_len()
            api(info5["url"], "/api/update/apply", {"version": v_crash})
            check(wait(lambda: p5.poll() is not None, 90) is not None, "old app exited")
            fail_ev = last_event("update_failed", n0, 400)
            check(fail_ev is not None and fail_ev.get("step") == "restart", f"helper recorded a failed start ({(fail_ev or {}).get('error', '')[:110]})")
            check(not [e for e in events(n0) if e.get("event") == "update_completed"], "the broken version is NOT reported as installed")
            fe = fail_ev or {}
            check(fe.get("restored_previous") and fe.get("relaunched_previous"), "previous version restored and relaunched")
            check(fe.get("new_pid") and not _alive(fe["new_pid"]), "the stuck/failed new process is gone")
            check(fe.get("helper_pid") and wait(lambda: not _alive(fe["helper_pid"]), 60), "helper process exited")
            check(installed_version(install) == v1 and (install / "UPDATED_MARKER.txt").read_text() == v1, "installation is the previous version")
            check([n.split("-")[0] for n in leftovers()] == ["EdgeLab.failed"], f"only the failed build is kept aside ({leftovers()})")
            info6 = running(info5["pid"])
            if check(info6 is not None, "previous version running again"):
                check(api(info6["url"], "/api/health")["backend"] == "ok", "it serves the API")
                lu = api(info6["url"], "/api/update/status")["last_update"] or {}
                check(lu.get("event") == "update_failed", "Settings reports the failed update")
                stop_pid(info6["pid"])
            for p in install.parent.glob("EdgeLab.failed-*"):
                shutil.rmtree(p, ignore_errors=True)

        phase("locked", "locked installation: update abandoned, previous version relaunched (Windows file locking)")
        if os.name != "nt":
            skip("POSIX allows renaming a folder with open files; this phase runs on Windows only")
        else:
            p7, info7 = start({**base_env, "EDGELAB_UPDATE_SOURCE": str(scratch / "rel-lock")})
            if check(info7 is not None, "app started"):
                api(info7["url"], "/api/update/check", {})
                s = download(info7["url"], v_lock)
                check(s and s["download"]["state"] == "ready", "downloaded and verified")
                held = open(install / "UPDATED_MARKER.txt", "rb")       # an open handle blocks the folder rename
                n0 = log_len()
                try:
                    api(info7["url"], "/api/update/apply", {"version": v_lock})
                    check(wait(lambda: p7.poll() is not None, 90) is not None, "old app exited")
                    fe = last_event("update_failed", n0, 300) or {}
                    check(fe.get("step") == "swap" and fe.get("relaunched_previous"), f"swap refused, previous relaunched ({fe.get('error', '')[:90]})")
                    check(not [e for e in events(n0) if e.get("event") == "update_completed"], "the new version is NOT reported as installed")
                    check(fe.get("helper_pid") and wait(lambda: not _alive(fe["helper_pid"]), 60), "helper process exited")
                finally:
                    held.close()
                check(installed_version(install) == v1 and (install / "UPDATED_MARKER.txt").read_text() == v1, "installation unchanged")
                check(leftovers() == [], f"no .new/.old folders left ({leftovers()})")
                info8 = running(info7["pid"])
                if check(info8 is not None, "previous version running again"):
                    check(api(info8["url"], "/api/health")["backend"] == "ok", "it serves the API")
                    lu = api(info8["url"], "/api/update/status")["last_update"] or {}
                    check(lu.get("event") == "update_failed", "Settings reports the failed update")
                    stop_pid(info8["pid"])

        phase("offline", "offline start (GitHub source, network blocked locally)")
        off = {k: v for k, v in base_env.items() if k.upper() != "NO_PROXY"}
        off.update({"HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
                    "https_proxy": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"})
        p9, info9 = start(off)
        if check(info9 is not None, "app starts with the network unavailable (and a proxy configured)"):
            s = api(info9["url"], "/api/update/status")
            check("GitHub" in s["source"], f"release source is GitHub ({s['source']}), reached only through the dead proxy")
            t0 = time.monotonic()
            s = api(info9["url"], "/api/update/check", {})
            err = (s["check"].get("error") or {}).get("code")
            check(s["check"]["state"] == "error" and err == "OFFLINE", f"check reports OFFLINE ({err}) in {time.monotonic() - t0:.1f}s")
            check(not s["available"] and not s["prompt"], "no prompt while offline")
            check(api(info9["url"], "/api/health")["backend"] == "ok" and bool(api(info9["url"], "/api/overview")), "app fully usable offline")
            stop_pid(info9["pid"])

        phase("integrity", "scratch workspace integrity")
        db = data / "demo" / "data" / "edgelab.sqlite"
        con = sqlite3.connect(db)
        try:
            check(con.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity_check ok")
        finally:
            con.close()
    finally:
        pass
    return finish(a, scratch, started, rt)


def finish(a, scratch: Path, started, rt: Path) -> int:
    for p in started:
        if p.poll() is None:
            p.kill()
    try:
        d = json.loads(rt.read_text())
        if _alive(d["pid"]):
            os.kill(d["pid"], signal.SIGTERM)
    except (OSError, ValueError, KeyError):
        pass
    fails = [r for r in results if r["status"] == "FAIL"]
    skips = [r for r in results if r["status"] == "SKIP"]
    if a.results_json:
        Path(a.results_json).write_text(json.dumps({"results": results, "failed": len(fails), "skipped": len(skips),
                                                    "platform": sys.platform}, indent=1))
    if not a.keep:
        time.sleep(1)
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"\n==== PACKAGED UPDATE SMOKE TEST: {'PASS' if not fails else 'FAIL'} "
          f"({sum(1 for r in results if r['status'] == 'PASS')} passed, {len(fails)} failed, {len(skips)} skipped)")
    for r in fails:
        print(f"  failed [{r['phase']}]: {r['check']}")
    for r in skips:
        print(f"  skipped [{r['phase']}]: {r['check']}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
