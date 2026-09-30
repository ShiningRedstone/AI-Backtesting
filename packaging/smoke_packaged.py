"""Read-only smoke test of the desktop app (packaged executable or the development launcher).

    python packaging/smoke_packaged.py --exe dist/EdgeLab/EdgeLab.exe
    python packaging/smoke_packaged.py --cmd "python -m edgelab.desktop"      # development launcher

Uses ONLY a fresh scratch data root (a temporary directory, or --data-root, which must not exist
yet) with the synthetic demo workspace. It never touches a real research store. Checks: the
executable exists and launches; the backend starts and the UI, API, static assets, config, SQLite
store and build metadata respond; the data root is outside the bundle; datasets can be listed;
the strategy / backtest / result / prop workflows answer through the same API the UI uses; a
second launch does not start a second server; shutdown is clean and the store is intact; the CLI
runs a process-parallel search (worker processes spawned from the executable).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

problems: list[str] = []
_procs: list[subprocess.Popen] = []          # every process this script starts: all are reaped in main()'s finally


def kill_tree(proc: subprocess.Popen) -> None:
    """Terminate proc AND its children (packaged workers), then reap it. Windows: taskkill /T (Popen.kill ends only
    the parent and would leave worker processes holding the SQLite file)."""
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL) if os.getpgid(proc.pid) == proc.pid else proc.kill()
            except OSError:
                pass
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(30)
    except subprocess.TimeoutExpired:
        pass
    if proc.stdout:
        try:
            proc.stdout.close()
        except OSError:
            pass


def run_tree(cmd, timeout):
    """subprocess.run(capture_output, timeout) that also ends the child's process tree on a timeout."""
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)
    _procs.append(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(p)
        raise
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


def check(cond, what):
    print(("  OK   " if cond else "  FAIL ") + what, flush=True)
    if not cond:
        problems.append(what)
    return cond


def http(url, body=None, timeout=120):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as r:   # loopback: no proxy
        raw = r.read()
        return r.status, raw, r.headers.get("Content-Type", "")


def api(base, path, body=None):
    return json.loads(http(base + path, body)[1])


def start(cmd, root, extra=()):
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    # scratch settings / update staging / an empty local release folder: the smoke test never touches the
    # user's settings and never contacts GitHub (the packaged updater would otherwise check at start-up)
    scratch = Path(root)
    (scratch / "no-release").mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "EDGELAB_SETTINGS": str(scratch / "settings" / "settings.json"),
           "EDGELAB_UPDATE_CACHE": str(scratch / "update-cache"), "EDGELAB_UPDATE_SOURCE": str(scratch / "no-release")}
    proc = subprocess.Popen([*cmd, "--data-root", str(root), "--no-browser", "--demo", *extra],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, **kw)
    _procs.append(proc)
    return proc


def stop(proc):
    if os.name == "nt":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGTERM)
    try:
        out, _ = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out = ""
    return proc.returncode, out


def wait_runtime(path: Path, proc, timeout=180):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except ValueError:
                pass
        if proc.poll() is not None:
            return None
        time.sleep(0.25)
    return None


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--exe", help="packaged executable (dist/EdgeLab/EdgeLab[.exe])")
    g.add_argument("--cmd", help="launcher command line, e.g. 'python -m edgelab.desktop'")
    ap.add_argument("--data-root", help="scratch data root (must not exist); default: a temp directory")
    ap.add_argument("--keep", action="store_true", help="keep the scratch data root")
    a = ap.parse_args(argv)
    exe = Path(a.exe).resolve() if a.exe else None
    # headless checks drive the console launcher (pipes, Ctrl+Break); the windowed EdgeLab.exe is
    # exercised by packaging/window_test_windows.py on Windows
    console = exe.with_name("EdgeLabConsole" + exe.suffix) if exe else None
    cmd = [str(console if console and console.is_file() else exe)] if exe else shlex.split(a.cmd, posix=os.name != "nt")
    bundle = exe.parent if exe else None
    base = Path(a.data_root) if a.data_root else Path(tempfile.mkdtemp(prefix="edgelab_smoke_")) / "root"
    if base.exists():
        sys.exit(f"refusing: scratch data root {base} already exists (the smoke test only uses a fresh root)")
    root = base / "demo"
    print(f"launcher: {cmd}\nscratch data root: {base}")

    print("\n1-2. executable exists and launches")
    if a.exe:
        check(exe.is_file(), f"executable exists ({exe})")
        check(console.is_file(), f"console launcher exists ({console.name})")
    proc = start(cmd, base)
    info = wait_runtime(root / "logs" / "runtime.json", proc)
    if not check(info is not None, "launcher started and wrote logs/runtime.json"):
        out = proc.communicate(timeout=10)[0] if proc.poll() is not None else ""
        print(out)
        return 1
    base_url = info["url"]
    print(f"   url {base_url}  pid {info['pid']}")

    print("\n3-5. backend, UI and API")
    check(base_url.startswith("http://127.0.0.1:") and info["port"] > 0, "bound to 127.0.0.1 on an assigned port")
    st, html, ctype = http(base_url + "/")
    check(st == 200 and b'id="root"' in html and "text/html" in ctype, "UI index served")
    check(api(base_url, "/api/health").get("backend") == "ok", "/api/health responds")
    print("\n6. persistent data root outside the bundle")
    rt = info["runtime"]
    check(Path(info["data_root"]).resolve() == root.resolve(), "runtime reports the scratch data root")
    if bundle:
        check(rt["packaged"] is True, "runtime reports a packaged build")
        check(not str(Path(info["data_root"]).resolve()).startswith(str(bundle.resolve())), "data root is outside the bundle")
        check(str(Path(rt["resource_dir"]).resolve()).startswith(str(bundle.resolve())), "resources come from the bundle")
    check((root / "configs" / "storage.yaml").is_file() and (root / "data").is_dir(), "workspace has configs/ and data/")
    print("\n7. bundled frontend assets")
    for asset in ("/app.js", "/styles.css", "/build-info.json"):
        check(http(base_url + asset)[0] == 200, f"{asset} served")
    print("\n8-10. config, SQLite store, build metadata")
    cfg = api(base_url, "/api/config")
    check(bool(cfg.get("config_hash")), f"config loads (hash {str(cfg.get('config_hash'))[:12]})")
    status = api(base_url, "/api/status")
    check(status.get("store_backend") == "sqlite", "SQLite store open")
    cv = status.get("code_version", {})
    check(bool(cv.get("source_sha256")), f"code version available ({cv})")
    if bundle:
        manifest = json.loads((bundle / "edgelab_build.json").read_text())
        check(cv.get("build_id") == manifest["build_id"] == status["runtime"]["build_id"],
              f"build id {cv.get('build_id')} matches the bundled manifest")
        check(cv.get("source_sha256") == manifest["source_sha256"], "source hash comes from the manifest")
    print("\n10b. version, updater and research-terminal read models")
    ver = api(base_url, "/api/version")
    info_ui = json.loads(http(base_url + "/build-info.json")[1])
    check(ver.get("version") and ver["version"] == info_ui.get("app_version"), f"one version across backend and UI ({ver.get('version')})")
    if bundle:
        check(ver["version"] == manifest.get("app_version"), "the build manifest carries the same version")
    us = api(base_url, "/api/update/status")
    check(us.get("current_version") == ver.get("version"), "updater reports the installed version")
    if bundle:
        check(us.get("apply_supported") is True and Path(us.get("install_dir") or "").resolve() == bundle.resolve(),
              f"updater knows the installation ({us.get('install_dir')})")
    chk = api(base_url, "/api/update/check", {})
    check(chk["check"]["state"] == "error" and chk["check"]["error"]["code"] == "NO_RELEASE" and not chk["available"],
          "a check without a release is reported, never fatal")
    check(api(base_url, "/api/health").get("backend") == "ok", "the app keeps running after a failed check")
    for route in ("/api/overview", "/api/explorer/strategies?scope=any", "/api/research/dashboard?include_synthetic=1", "/api/pipeline"):
        check(isinstance(api(base_url, route), dict), f"{route} responds")
    print("\n11. datasets and UI workflows (same API the UI calls)")
    ds = api(base_url, "/api/datasets")
    check(len(ds) >= 2, f"datasets listed ({len(ds)})")
    fut = next((d for d in ds if d["asset_type"] == "FUTURE"), None)
    check(fut is not None and fut["runnable"], "an eligible dataset is available")
    check(bool(api(base_url, f"/api/datasets/{fut['dataset_id']}")["limitations"]), "dataset caveats available")
    strategies = api(base_url, "/api/strategies")
    check(len(strategies) > 0, f"strategies listed ({len(strategies)})")
    def editable(defn):
        for k, p in (defn.get("parameters") or {}).items():
            if p.get("type") in ("integer", "float") and p.get("max") is not None \
                    and p["value"] + p.get("step", 1) <= p["max"]:
                return k
        return None
    loaded = [api(base_url, f"/api/strategies/{x['strategy_id']}") for x in strategies]
    s = next((x for x in loaded if editable(x["definition"])), loaded[0])
    sid = s["strategy_id"]
    v = api(base_url, "/api/strategies/validate", {"definition": s["definition"]})
    check(v["valid"] and v["identity"]["strategy_id"] == sid and v["identity"]["definition_hash"] == s["definition_hash"],
          f"open + validate {sid}: same strategy id and definition hash")
    edited = json.loads(json.dumps(s["definition"]))
    num = editable(edited)
    if check(num is not None, f"strategy has an editable parameter ({num})"):
        p = edited["parameters"][num]
        p["value"] = p["value"] + p.get("step", 1)
        saved = api(base_url, "/api/strategies/save", {"definition": edited, "parent_strategy_id": sid,
                                                       "method": "manual_edit"})
        check(saved.get("created") is True and saved["strategy_id"] != sid,
              f"edit {num} and save as a new version ({saved.get('strategy_id')})")
        lin = api(base_url, f"/api/strategies/{saved['strategy_id']}/lineage")
        check(lin["records"][0]["parent_strategy_id"] == sid, "lineage records the parent")
        check(api(base_url, f"/api/strategies/{sid}")["definition_hash"] == s["definition_hash"],
              "parent definition hash unchanged")
        check(api(base_url, f"/api/strategies/{saved['strategy_id']}")["definition_hash"] == saved["definition_hash"],
              "new version reloads with its definition hash")
    bt = api(base_url, "/api/backtests", {"strategy": sid, "dataset_id": fut["dataset_id"]})
    run_id = bt.get("run_id")
    check(bool(run_id), f"backtest from the UI path ({run_id}, {bt.get('metrics', {}).get('trade_count')} trades)")
    run = api(base_url, f"/api/results/{run_id}")
    check(run["record"]["code_version"] == cv, "run lineage records the effective code/build version")
    sim = api(base_url, "/api/prop/simulate", {"run_id": run_id, "accounts": [{"config": "SYNTH_STATIC_EVAL"}]})
    check(sim["lineage"]["source_run_id"] == run_id and sim["accounts"], f"prop simulation ({sim['accounts'][0]['summary']['status']})")
    check(api(base_url, f"/api/results/{run_id}") == run, "prop simulation left the run unchanged")
    runs_before = len(api(base_url, "/api/results"))

    print("\n   single instance")
    second = run_tree([*cmd, "--data-root", str(base), "--no-browser", "--demo"], 120)
    check(second.returncode == 0 and "already running" in second.stdout, "a second launch reuses the running instance")

    print("\n12. shutdown")
    code, out = stop(proc)
    check(code == 0, f"launcher exited cleanly (code {code})")
    check(not (root / "logs" / "runtime.json").exists(), "runtime.json removed on exit")
    db = root / "data" / "edgelab.sqlite"
    con = sqlite3.connect(db)
    try:
        check(con.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity_check ok")
        check(con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == runs_before, f"all {runs_before} runs persisted")
    finally:
        con.close()

    print("\n   restart reuses the persistent store")
    proc = start(cmd, base)
    info2 = wait_runtime(root / "logs" / "runtime.json", proc)
    if check(info2 is not None, "relaunch after shutdown"):
        check(len(api(info2["url"], "/api/results")) == runs_before, "runs still listed after restart")
        check(stop(proc)[0] == 0, "second shutdown clean")
    else:
        kill_tree(proc)

    print("\n   CLI and process-parallel research (worker processes)")
    fixtures_ids = [x["strategy_id"] for x in strategies][:3]
    spec = {"search_spec_version": 1, "strategies": {"ids": fixtures_ids}, "datasets": [fut["dataset_id"]],
            "max_cells": 50, "seed": 1, "workers": 2}
    spec_file = base / "smoke_search.json"
    spec_file.write_text(json.dumps(spec))
    cli = run_tree([*cmd, "cli", "--root", str(root), "--json", "research", "run", str(spec_file), "--workers", "2"], 900)
    try:
        res = json.loads(cli.stdout[cli.stdout.index("{"):])
    except ValueError:
        res = {}
        print(cli.stdout[-2000:], cli.stderr[-2000:])
    check(cli.returncode == 0 and res.get("status") == "completed" and res.get("n_evaluated", 0) > 0,
          f"CLI research run --workers 2 completed ({res.get('status')}, {res.get('n_evaluated')} cells evaluated)")

    if not a.keep and not a.data_root:
        shutil.rmtree(base.parent, ignore_errors=True)
    print("\n==== PACKAGED SMOKE TEST:", "PASS" if not problems else f"FAIL {problems}")
    return 0 if not problems else 1


def main(argv=None) -> int:
    """_main inside a guard: whatever happens (failed check, HTTP error, timeout, crash), every process started here
    is ended and reaped before returning, so nothing keeps the scratch SQLite store open; a crash is a recorded FAIL."""
    try:
        return _main(argv)
    except SystemExit:
        raise
    except BaseException:                                                   # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("\n==== PACKAGED SMOKE TEST: FAIL (unexpected exception, see above)", flush=True)
        return 1
    finally:
        for p in _procs:
            kill_tree(p)


if __name__ == "__main__":
    raise SystemExit(main())
