"""Windows validation of a working-tree EdgeLab change set (build, packaged app, native window, updater).

Run it FROM THE EXTRACTED VALIDATION PACKAGE (not from the repository), with the repository's own venv:

    <repo>\\.venv\\Scripts\\python.exe <package>\\packaging\\windows_validation.py --repo <repo> --out <new folder>

<package> holds CHANGED_FILES.txt ("M path" / "A path") and those files. Order of work:

  0. guards: the interpreter is <repo>\\.venv\\Scripts\\python.exe, HEAD is the expected commit, no EdgeLab
     process is running, --out is a new folder outside the repository and outside the user's data;
  1. BEFORE: recursive manifests (relative path, size, mtime, SHA-256) of the real EdgeLab data folders
     (%LOCALAPPDATA%\\EdgeLab, %LOCALAPPDATA%\\EdgeLab-Updater, %APPDATA%\\EdgeLab, plus a workspace named by
     EDGELAB_DATA_ROOT or the saved settings), fingerprints of the protected repository items, git status
     (porcelain v1, all untracked files), HEAD and the stash list;
  2. copy the package into the working tree, only if every "M" file is still unmodified from HEAD and every
     "A" file is absent or identical (the protected items are never part of the package);
  3. build dependencies into the venv, frontend bundle check (PASS, or EXPECTED-REBUILD when it
     differs only by CRLF line endings: then build.py rebuilds it with Node/npm), PyInstaller build, unit tests
     (after the build: they include a test of dist/EdgeLab), packaged smoke, native window, packaged updater test (local release fixtures only);
  4. AFTER: the same manifests / fingerprints / git status; any difference outside CHANGED_FILES.txt fails.

Every child process gets a scratch TEMP/TMP, EDGELAB_SETTINGS, EDGELAB_UPDATE_CACHE and an empty local
release folder as EDGELAB_UPDATE_SOURCE (EDGELAB_DATA_ROOT and other EDGELAB_* variables are removed), so
nothing reads or writes the user's settings, update state or data, and nothing contacts GitHub.
Nothing is committed, pushed, stashed, reset, cleaned or deleted in the repository.

Output: <out>\\validation.log, <out>\\SUMMARY.txt and a final line
"WINDOWS VALIDATION: PASS" (every required item passed) or "WINDOWS VALIDATION: NOT PASSED".
Standard library only (it runs before the package is copied into the repository).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

EXPECTED_HEAD = "353ddd90e08979fc55e130524b46e974037c4ec2"
PROTECTED = ("tests/test_directional_quotes.py", "histdata_4year_sets.patch")
FINGERPRINTED = ("data/edgelab.sqlite",) + PROTECTED
STATIC_PREFIX = "edgelab/web/static/"
PASS, EXPECTED, SKIP, FAIL = "PASS", "EXPECTED-REBUILD", "SKIP", "FAIL"
IS_WIN = sys.platform == "win32"


class Log:
    def __init__(self, path: Path):
        self.fh = open(path, "a", encoding="utf-8")

    def __call__(self, text: str = "") -> None:
        print(text, flush=True)
        self.fh.write(text + "\n")
        self.fh.flush()


# ------------------------------------------------------------------------------------------- snapshots
def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_entry(p: Path) -> dict:
    st = p.stat()
    try:
        digest = sha256(p)
    except OSError as e:                              # e.g. opened exclusively by another program
        digest = f"UNREADABLE: {e.__class__.__name__}"
    return {"size": st.st_size, "mtime_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
            "mtime_ns": st.st_mtime_ns, "sha256": digest}


# Generated / environment output inside a repository-rooted workspace. The user's saved workspace may BE this
# repository (configs/, data/, strategy_library/ live next to the sources); packaging and the build environment
# legitimately rewrite these, so they are not research data. Everything else stays protected: data/ (the SQLite
# store and imported datasets), strategy_library/, configs/, logs/, reports/ ... Source files and the rebuilt bundle
# are covered by the working-tree (git status) check instead.
BUILD_OUTPUT_DIRS = frozenset({"build", "dist", ".venv", "venv", ".venv-build", ".git", "node_modules", "__pycache__",
                               ".pytest_cache", ".mypy_cache", ".ruff_cache"})


def is_build_output(rel: str, exclude_paths=frozenset()) -> bool:
    """rel: repository-relative POSIX path. True for generated build/environment output and for paths owned by the
    working-tree check (the package's CHANGED_FILES and the frontend bundle directory)."""
    parts = rel.split("/")
    if any(p in BUILD_OUTPUT_DIRS for p in parts) or rel.endswith((".pyc", ".pyo")):
        return True
    return rel in exclude_paths or rel.startswith(STATIC_PREFIX)


def tree_manifest(root: Path, repo: Path | None = None, exclude_paths=frozenset()) -> dict:
    """{relative path: entry} for every file under root (recursive); a missing root is {"<absent>": True}.
    If root contains the repository (a repository-rooted workspace), generated build output inside the repository
    (is_build_output) is not part of the manifest; nothing else is skipped."""
    if not root.is_dir():
        return {"<absent>": True}
    repo_rel = None
    if repo is not None:
        try:
            repo_rel = repo.resolve().relative_to(root.resolve())
        except ValueError:
            repo_rel = None
        else:
            if repo.resolve() == root.resolve():
                repo_rel = Path()
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        if repo_rel is not None:                      # prune generated trees before hashing anything in them
            here = Path(dirpath).resolve()
            try:
                rr = here.relative_to(root.resolve() / repo_rel)
            except ValueError:
                rr = None
            if rr is not None:
                dirnames[:] = [d for d in dirnames if d not in BUILD_OUTPUT_DIRS]
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if repo_rel is not None:
                try:
                    if is_build_output((p.resolve().relative_to(root.resolve() / repo_rel)).as_posix(), exclude_paths):
                        continue
                except ValueError:
                    pass
            out[p.relative_to(root).as_posix()] = file_entry(p)
    return out


def real_data_dirs() -> dict[str, Path]:
    """The user's real EdgeLab folders (same rules as edgelab.runtime, computed WITHOUT test overrides)."""
    env = os.environ
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    if IS_WIN:
        local = Path(env["LOCALAPPDATA"]) if env.get("LOCALAPPDATA") else home / "AppData" / "Local"
        roaming = Path(env["APPDATA"]) if env.get("APPDATA") else home / "AppData" / "Roaming"
        dirs = {"LOCALAPPDATA/EdgeLab": local / "EdgeLab", "LOCALAPPDATA/EdgeLab-Updater": local / "EdgeLab-Updater",
                "APPDATA/EdgeLab": roaming / "EdgeLab"}
        settings = roaming / "EdgeLab" / "settings.json"
    else:                                             # dry runs on Linux: the XDG equivalents
        data = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else home / ".local" / "share"
        conf = Path(env["XDG_CONFIG_HOME"]) if env.get("XDG_CONFIG_HOME") else home / ".config"
        dirs = {"data/EdgeLab": data / "EdgeLab", "data/EdgeLab-Updater": data / "EdgeLab-Updater",
                "config/edgelab": conf / "edgelab"}
        settings = conf / "edgelab" / "settings.json"
    extra = []
    if env.get("EDGELAB_DATA_ROOT"):
        extra.append(("EDGELAB_DATA_ROOT", Path(env["EDGELAB_DATA_ROOT"])))
    try:
        saved = json.loads(settings.read_text(encoding="utf-8")).get("workspace")
        if saved:
            extra.append(("saved workspace", Path(saved)))
    except (OSError, ValueError):
        pass
    for label, p in extra:
        p = p.expanduser().resolve()
        if not any(_inside(p, d.resolve()) for d in dirs.values()):
            dirs[f"{label}: {p}"] = p
    return dirs


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def git(repo: Path, *args: str, check=True) -> str:
    r = subprocess.run(["git", "--no-optional-locks", *args], cwd=repo, capture_output=True)
    if check and r.returncode:
        raise SystemExit(f"git {' '.join(args)} failed: {r.stderr.decode(errors='replace')}")
    return r.stdout.decode("utf-8", errors="surrogateescape")


def git_status(repo: Path) -> dict[str, str]:
    """{path: 'XY'} from `git status --porcelain=v1 --untracked-files=all` (-z form; renames keep both paths)."""
    raw = git(repo, "status", "--porcelain=v1", "--untracked-files=all", "-z").split("\0")
    out, i = {}, 0
    while i < len(raw):
        rec = raw[i]
        i += 1
        if not rec:
            continue
        xy, path = rec[:2], rec[3:]
        if "R" in xy or "C" in xy:
            out[raw[i]] = xy + " (renamed to " + path + ")"
            i += 1
        out[path] = xy
    return out


def snapshot(repo: Path, dirs: dict[str, Path], exclude_paths=frozenset()) -> dict:
    return {"dirs": {k: tree_manifest(p, repo, exclude_paths) for k, p in dirs.items()},
            "repo_files": {r: (file_entry(repo / r) if (repo / r).is_file() else "<absent>") for r in FINGERPRINTED},
            "git_status": git_status(repo),
            "git_status_text": git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
            "head": git(repo, "rev-parse", "HEAD").strip(),
            "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip(),
            "stash": git(repo, "stash", "list").strip()}


def diff_trees(a: dict, b: dict) -> list[str]:
    out = []
    for k in sorted(set(a) | set(b)):
        if k not in b:
            out.append(f"removed: {k}")
        elif k not in a:
            out.append(f"added: {k}")
        elif a[k] != b[k]:
            what = [f for f in ("size", "mtime_utc", "sha256") if isinstance(a[k], dict) and a[k].get(f) != b[k].get(f)]
            out.append(f"changed ({', '.join(what) or 'entry'}): {k}")
    return out


# ------------------------------------------------------------------------------------------- frontend bundle
def web_source_hash(repo: Path, normalize_eol: bool) -> str:
    """edgelab.web.bundle.source_hash, optionally with CRLF -> LF (what a CRLF checkout changes)."""
    web = repo / "web"
    h = hashlib.sha256()

    def data(p: Path) -> bytes:
        b = p.read_bytes()
        return b.replace(b"\r\n", b"\n") if normalize_eol else b

    def walk(d: Path) -> None:
        for p in sorted(d.iterdir(), key=lambda x: x.name):
            if p.is_dir():
                walk(p)
            else:
                h.update(p.relative_to(web).as_posix().encode())
                h.update(data(p))

    walk(web / "src")
    for f in ("index.html", "build.mjs", "package.json", "tsconfig.json"):
        h.update(f.encode())
        h.update(data(web / f))
    return h.hexdigest()


def earlier_validation_bundle(repo: Path, pkg: Path) -> bool:
    """True if the tree's frontend bundle (edgelab/web/static) was rebuilt by an earlier validation of THIS package
    version (same app_version in its build-info.json as the package's): it is then generated output that a
    CRLF checkout re-creates, not a user edit, and the package may replace it. A bundle of another version, or
    without build-info, is treated as a local modification."""
    try:
        here = json.loads((repo / STATIC_PREFIX / "build-info.json").read_text(encoding="utf-8")).get("app_version")
        there = json.loads((pkg / STATIC_PREFIX / "build-info.json").read_text(encoding="utf-8")).get("app_version")
    except (OSError, ValueError):
        return False
    return bool(here) and here == there


def bundle_state(repo: Path) -> tuple[str, str]:
    info = json.loads((repo / "edgelab/web/static/build-info.json").read_text(encoding="utf-8"))
    version = re.search(r'__version__\s*=\s*"([^"]+)"', (repo / "edgelab/__init__.py").read_text()).group(1)
    if info.get("app_version") != version:
        return FAIL, f"bundle built for {info.get('app_version')}, backend is {version}"
    if web_source_hash(repo, False) == info.get("source_sha256"):
        return PASS, "committed bundle matches web/ sources"
    if web_source_hash(repo, True) == info.get("source_sha256"):
        return EXPECTED, "bundle hash differs only by CRLF line endings of the checkout: build.py rebuilds it (Node/npm)"
    return FAIL, "web/ sources differ from the committed bundle beyond line endings"


# ------------------------------------------------------------------------------------------- processes
def edgelab_processes() -> list[str]:
    if IS_WIN:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, errors="replace")
        return [l for l in r.stdout.splitlines() if l.lower().startswith(('"edgelab.exe"', '"edgelabconsole.exe"'))]
    r = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True)
    return [l.strip() for l in r.stdout.splitlines() if re.search(r"/EdgeLab(Console)?( |$)", l)]


def run_child(log, args: list[str], cwd, env, timeout: float, out_lines: list[str]) -> int:
    """Run a child to completion: output streamed into the log (and out_lines), return code recorded. Never raises;
    a launch/read failure is -1 and a timeout -9 (child tree killed), both logged. The child gets its own console
    process group, so a stray Ctrl+Break aimed at another process cannot end it or this validator."""
    t0 = time.monotonic()
    try:
        kw = {"creationflags": 0x00000200} if IS_WIN else {}             # CREATE_NEW_PROCESS_GROUP
        p = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, **kw)
    except Exception as e:                                                  # noqa: BLE001 - recorded, never fatal
        log(f"   LAUNCH FAILED: {e.__class__.__name__}: {e}")
        return -1

    def pump() -> None:
        for raw in p.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            out_lines.append(line)
            log("   " + line)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        code = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"   TIMEOUT after {timeout}s: killing the process tree")
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        p.kill()
        p.wait()
        code = -9
    except Exception as e:                                                  # noqa: BLE001
        log(f"   WAIT FAILED: {e.__class__.__name__}: {e}")
        p.kill()
        p.wait()
        code = -1
    reader.join(30)
    p.stdout.close()
    if code in (0xC000013A, -1073741510):
        log("   the child was ended by a console Ctrl+C / Ctrl+Break (STATUS_CONTROL_C_EXIT)")
    log(f"   [exit {code}, {time.monotonic() - t0:.0f}s]")
    return code


def unittest_verdict(code: int, lines: list[str]) -> str:
    """'OK' only with exit 0 AND unittest's own summary ("Ran N tests" ... "OK"); otherwise the reason. A missing
    summary (the runner was killed, crashed, or printed nothing) is never success."""
    text = "\n".join(lines)
    ran = re.search(r"^Ran (\d+) tests? in ", text, re.M)
    if code == -9:
        return "timeout: the unit-test process was killed"
    if code == -1:
        return "the unit-test process could not be started or read (see the log)"
    if code in (0xC000013A, -1073741510):
        return "the unit-test process was ended by a console Ctrl+C / Ctrl+Break"
    if code != 0:
        return f"exit code {code}" + ("" if ran else " and no unittest summary")
    if not ran:
        return "exit 0 but no unittest summary: not treated as success"
    if not re.search(r"^OK\b", text[ran.end():], re.M):
        return "no 'OK' line after the unittest summary"
    return "OK"


# ------------------------------------------------------------------------------------------- main
def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, help="the repository checkout")
    ap.add_argument("--out", required=True, help="new scratch folder for logs, temp files and test data")
    ap.add_argument("--package", default=str(Path(__file__).resolve().parents[1]), help="extracted package (default: this file's package)")
    ap.add_argument("--expected-head", default=EXPECTED_HEAD)
    ap.add_argument("--previous-package", action="append", default=[], metavar="DIR",
                    help="an EARLIER extracted validation package that was already applied to this tree (repeatable): "
                         "files still identical to it may be replaced by this package's version")
    ap.add_argument("--allow-non-windows", action="store_true", help="dry run elsewhere (Windows-only items SKIP -> NOT PASSED)")
    a = ap.parse_args(argv)
    repo, out, pkg = Path(a.repo).resolve(), Path(a.out).resolve(), Path(a.package).resolve()
    if not IS_WIN and not a.allow_non_windows:
        sys.exit("this validation is for Windows (use --allow-non-windows for a dry run)")
    if out.exists():
        sys.exit(f"refusing: {out} already exists (choose a new folder)")
    dirs = real_data_dirs()
    for bad in [repo, *dirs.values()]:
        if _inside(out, bad.resolve()) or _inside(bad.resolve(), out):
            sys.exit(f"refusing: {out} overlaps {bad}")
    if _inside(pkg, repo):
        sys.exit(f"refusing: run the package from outside the repository ({pkg})")
    out.mkdir(parents=True)
    for d in ("tmp", "settings", "update-cache", "no-release"):
        (out / d).mkdir()
    log = Log(out / "validation.log")
    status: dict[str, tuple[str, str]] = {}

    def record(name: str, st: str, note: str = "") -> str:
        status[name] = (st, note)
        log(f"  -> {st:16} {name}{'  (' + note + ')' if note else ''}")
        return st

    log(f"EdgeLab Windows validation {datetime.now().isoformat(timespec='seconds')}")
    log(f"repo    {repo}\npackage {pkg}\nout     {out}\npython  {sys.executable}  ({sys.version.split()[0]})")

    # ---------------------------------------------------------------- 0. guards (read-only)
    stop = None
    venv_py = repo / ".venv" / "Scripts" / "python.exe"
    if IS_WIN and os.path.normcase(os.path.abspath(sys.executable)) != os.path.normcase(str(venv_py)):
        stop = f"run this with {venv_py} (not {sys.executable})"
    head = git(repo, "rev-parse", "HEAD").strip()
    if not stop and head != a.expected_head:
        stop = f"HEAD is {head}, expected {a.expected_head}"
    procs = edgelab_processes()
    if not stop and procs:
        stop = "close every running EdgeLab window/process first: " + "; ".join(procs)
    listing = pkg / "CHANGED_FILES.txt"
    entries = []
    if not stop:
        for line in listing.read_text(encoding="utf-8").splitlines():
            if re.match(r"^[MA] \S", line):
                entries.append((line[0], line[2:].strip()))
        bad = [r for _, r in entries if r in PROTECTED] + [r for r in PROTECTED if (pkg / r).exists()]
        if bad:
            stop = f"the package contains protected items: {bad}"
    changed = {r for _, r in entries}
    for k, p in dirs.items():
        log(f"real data folder  {k:32} {p}")

    log("\n== 1. BEFORE: manifests of real data, protected items, git state")
    before = snapshot(repo, dirs, changed)
    (out / "before.json").write_text(json.dumps(before, indent=1))
    for k, m in before["dirs"].items():
        log(f"  {k:32} {'absent' if '<absent>' in m else f'{len(m)} files'}")
    for r, e in before["repo_files"].items():
        log(f"  repo {r:28} {e if isinstance(e, str) else e['sha256'][:16] + '... ' + str(e['size']) + ' B'}")
    log(f"  git status: {len(before['git_status'])} entries; HEAD {before['head'][:12]} on {before['branch']}")

    # ---------------------------------------------------------------- 2. copy the package into the working tree
    rebuild = False
    if not stop:
        log(f"\n== 2. package: {len(entries)} files ({sum(k == 'M' for k, _ in entries)} modified, {sum(k == 'A' for k, _ in entries)} added)")
        problems, todo = [], []
        regenerated = earlier_validation_bundle(repo, pkg)
        if regenerated:
            log("  frontend bundle in the tree was rebuilt by an earlier validation of this version: the package may replace it")
        for kind, rel in entries:
            src, dst = pkg / rel, repo / rel
            if not src.is_file():
                problems.append(f"missing from the package: {rel}")
                continue
            if dst.is_file() and sha256(dst) == sha256(src):
                continue
            todo.append(rel)
            if dst.is_file() and any((Path(q) / rel).is_file() and sha256(Path(q) / rel) == sha256(dst) for q in a.previous_package):
                continue                                                # exactly what an earlier package put there
            if rel.startswith(STATIC_PREFIX) and regenerated:
                continue                                                # generated by an earlier validation's rebuild
            if kind == "M" and subprocess.run(["git", "--no-optional-locks", "diff", "--quiet", "HEAD", "--", rel], cwd=repo).returncode:
                problems.append(f"locally modified (would be overwritten): {rel}")
            if kind == "A" and dst.exists():
                problems.append(f"already exists with other content (would be overwritten): {rel}")
        if problems:
            stop = "package not copied:\n    " + "\n    ".join(problems)
        else:
            for rel in todo:
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(pkg / rel, repo / rel)
            log(f"  copied {len(todo)} files ({len(entries) - len(todo)} already identical); nothing staged or committed")

    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("EDGELAB_")}
    env.update({"TEMP": str(out / "tmp"), "TMP": str(out / "tmp"), "TMPDIR": str(out / "tmp"),
                "EDGELAB_SETTINGS": str(out / "settings" / "settings.json"),
                "EDGELAB_UPDATE_CACHE": str(out / "update-cache"),
                "EDGELAB_UPDATE_SOURCE": str(out / "no-release"),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"})

    def run(name: str, args: list[str], timeout: int = 3600) -> int:
        out_lines: list[str] = []
        run.output = out_lines
        log(f"\n== {name}\n   $ {' '.join(args)}")
        return run_child(log, args, repo, env, timeout, out_lines)

    run.output = []

    py = sys.executable
    exe = repo / "dist" / "EdgeLab" / ("EdgeLab.exe" if IS_WIN else "EdgeLab")
    def phases() -> None:
        nonlocal rebuild
        record("preflight", PASS, "guards ok, package applied")
        # ------------------------------------------------------------ 3. build environment, tests, build
        c = run("duckdb must not be in the venv", [py, "-c", "import importlib.util,sys; print(sys.version); "
                                                   "sys.exit(3 if importlib.util.find_spec('duckdb') else 0)"])
        ok = record("venv without duckdb", PASS if c == 0 else FAIL, "" if c == 0 else "duckdb installed: the desktop build refuses it") == PASS
        if ok:
            c = run("build requirements into the venv", [py, "-m", "pip", "install", "-r", "packaging/requirements-build.txt"])
            ok = record("build requirements", PASS if c == 0 else FAIL) == PASS
        bst, bnote = bundle_state(repo)
        log(f"\n== frontend bundle: {bst}: {bnote}")
        rebuild = bst == EXPECTED
        if rebuild and not (shutil.which("node") and shutil.which("npm")):
            bst, bnote = FAIL, bnote + "; but Node/npm are not installed"
        record("frontend bundle", bst, bnote)
        built = False
        if ok and bst != FAIL:
            c = run("PyInstaller build", [py, "packaging/build.py"] + ([] if rebuild else ["--skip-frontend"]))
            after_bundle = bundle_state(repo)[0] if c == 0 else FAIL
            built = c == 0 and exe.is_file() and after_bundle == PASS
            record("build", PASS if built else FAIL,
                   "" if built else f"exit {c}; bundle after build: {after_bundle}")
        else:
            record("build", FAIL, "not run")
        # Unit tests run AFTER the build: tests.test_desktop.TestPackagedBuild exercises whatever dist/EdgeLab holds, and a
        # stale build left by an earlier run (before this package) would fail it for reasons unrelated to this change.
        if built:
            c = run("unit tests: updater, desktop launcher, read models",
                    [py, "-m", "unittest", "tests.test_updater", "tests.test_desktop", "tests.test_research_overview"]) 
            verdict = unittest_verdict(c, run.output) if c is not None else "not run"
            record("unit tests", PASS if verdict == "OK" else FAIL, "" if verdict == "OK" else verdict)
            stray = edgelab_processes()
            if stray:                                     # a leaked packaged app would lock dist\\ and the scratch store: stop here
                log("  LEAKED PROCESSES after the unit tests: " + "; ".join(stray))
                record("no EdgeLab process left by the unit tests", FAIL, "; ".join(stray))
                built = False                              # do not start more packaged apps on top of a leak
        else:
            record("unit tests", FAIL, "not run: no fresh build to test against")
        # ------------------------------------------------------------ packaged tests
        if built:
            c = run("packaged smoke (headless)", [py, "packaging/smoke_packaged.py", "--exe", str(exe), "--data-root", str(out / "smoke-packaged")])
            record("packaged smoke", PASS if c == 0 else FAIL)
            if IS_WIN:
                c = run("native window", [py, "packaging/window_test_windows.py", "--exe", str(exe)])
                record("native window", PASS if c == 0 else FAIL)
            else:
                record("native window", SKIP, "Windows only")
            res_file = out / "update_results.json"
            c = run("packaged updater (local release fixtures)", [py, "packaging/smoke_update.py", "--dist", str(exe.parent),
                                                                   "--scratch", str(out / "update-smoke"), "--results-json", str(res_file)])
            try:
                results = json.loads(res_file.read_text())["results"]
            except (OSError, ValueError, KeyError):
                results = []

            def phase_status(*phases: str) -> tuple[str, str]:
                rows = [r for r in results if r["phase"] in phases]
                if not rows:
                    return FAIL, "not run"
                if any(r["status"] == FAIL for r in rows):
                    return FAIL, "; ".join(r["check"] for r in rows if r["status"] == FAIL)
                if all(r["status"] == SKIP for r in rows):
                    return SKIP, rows[0]["check"]
                return PASS, f"{sum(r['status'] == PASS for r in rows)} checks"

            core = phase_status("version", "check", "stage", "update", "corrupted", "integrity")
            record("updater smoke", core[0] if c in (0, 1) else FAIL, core[1] if c in (0, 1) else f"exit {c}")
            record("new version fails to start -> rollback", *phase_status("startup-fail"))
            record("offline start", *phase_status("offline"))
            record("locked-folder rollback", *phase_status("locked"))
        else:
            for n in ("packaged smoke", "native window", "updater smoke", "new version fails to start -> rollback",
                      "offline start", "locked-folder rollback"):
                record(n, FAIL, "not run (no build)")

    if stop:
        log(f"\nSTOP: {stop}")
        record("preflight", FAIL, stop.splitlines()[0])
    else:
        try:
            phases()
        except BaseException as e:                                          # noqa: BLE001 - never vanish silently
            log("\nUNEXPECTED VALIDATOR EXCEPTION:\n" + traceback.format_exc())
            record("validator exception", FAIL, f"{e.__class__.__name__}: {e}")

    # ---------------------------------------------------------------- 4. AFTER
    log("\n== 4. AFTER: real data, protected items, working tree")
    time.sleep(2)
    stray = edgelab_processes()
    record("no EdgeLab process left running", PASS if not stray else FAIL, "; ".join(stray))
    after = snapshot(repo, dirs, changed)
    (out / "after.json").write_text(json.dumps(after, indent=1))
    problems = []
    for k in before["dirs"]:
        problems += [f"{k}: {d}" for d in diff_trees(before["dirs"][k], after["dirs"][k])]
    for r in FINGERPRINTED:
        if before["repo_files"][r] != after["repo_files"][r]:
            problems.append(f"repo {r}: fingerprint changed")
    for p in problems:
        log(f"  CHANGED  {p}")
    record("real data manifests unchanged", PASS if not problems else FAIL, f"{len(problems)} differences" if problems else
           f"{sum(len(m) for m in after['dirs'].values() if '<absent>' not in m)} files + {len(FINGERPRINTED)} repo items identical")

    allowed = set(changed) if status["preflight"][0] == PASS else set()
    wt = []
    for path in sorted(set(before["git_status"]) | set(after["git_status"])):
        b, x = before["git_status"].get(path), after["git_status"].get(path)
        if b == x:
            continue
        if path in allowed or (rebuild and path.startswith(STATIC_PREFIX)):
            continue
        wt.append(f"{path}: {b or 'clean'} -> {x or 'clean'}")
    for r in PROTECTED:
        if before["git_status"].get(r) != after["git_status"].get(r):
            wt.append(f"protected {r}: status {before['git_status'].get(r)} -> {after['git_status'].get(r)}")
    if status["preflight"][0] == PASS:                # the applied files still hold exactly the package content
        for rel in sorted(changed):
            if rebuild and rel.startswith(STATIC_PREFIX):
                continue
            if not (repo / rel).is_file() or sha256(repo / rel) != sha256(pkg / rel):
                wt.append(f"{rel}: differs from the package after the run")
    for what in ("head", "branch", "stash"):
        if before[what] != after[what]:
            wt.append(f"git {what} changed: {before[what]!r} -> {after[what]!r}")
    staged = [p for p, xy in after["git_status"].items() if xy[0] not in (" ", "?")]
    if staged:
        wt.append(f"staged entries: {staged}")
    for p in wt:
        log(f"  UNEXPECTED  {p}")
    record("no unexpected working-tree changes", PASS if not wt else FAIL,
           f"{len(wt)} problems" if wt else f"only CHANGED_FILES.txt paths{' + rebuilt bundle' if rebuild else ''}; nothing staged/committed")

    # ---------------------------------------------------------------- summary
    required = ["preflight", "unit tests", "frontend bundle", "build", "packaged smoke", "native window", "updater smoke",
                "new version fails to start -> rollback", "offline start", "locked-folder rollback",
                "no EdgeLab process left running", "real data manifests unchanged", "no unexpected working-tree changes"]
    ok = all(status.get(n, (FAIL,))[0] in (PASS, EXPECTED if n == "frontend bundle" else PASS) for n in required)
    lines = [f"EdgeLab Windows validation  {datetime.now().isoformat(timespec='seconds')}", f"log: {out / 'validation.log'}", ""]
    lines += [f"{status.get(n, (FAIL, 'not run'))[0]:17} {n}" + (f"  ({status.get(n, ('', ''))[1]})" if status.get(n, ('', ''))[1] else "")
              for n in required]
    extra = [n for n in status if n not in required]
    lines += [""] + [f"{status[n][0]:17} {n}" for n in extra]
    lines += ["", "WINDOWS VALIDATION: PASS" if ok else "WINDOWS VALIDATION: NOT PASSED"]
    (out / "SUMMARY.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log("\n================ SUMMARY ================")
    for l in lines:
        log(l)
    return 0 if ok else 1


def main(argv=None) -> int:
    """_main plus a last-resort boundary: an unexpected exception anywhere still leaves a log entry, a
    SUMMARY.txt and an explicit NOT PASSED verdict (exit 2) instead of a silent exit."""
    try:
        return _main(argv)
    except SystemExit:
        raise
    except BaseException:                                                   # noqa: BLE001
        tb = traceback.format_exc()
        print(tb, file=sys.stderr, flush=True)
        pre = argparse.ArgumentParser(add_help=False)
        pre.add_argument("--out")
        out = pre.parse_known_args(argv)[0].out
        if out and Path(out).is_dir():
            try:
                with open(Path(out) / "validation.log", "a", encoding="utf-8") as fh:
                    fh.write("\nUNEXPECTED VALIDATOR EXCEPTION:\n" + tb)
                (Path(out) / "SUMMARY.txt").write_text(
                    "FAIL              validator exception\n" + tb + "\nWINDOWS VALIDATION: NOT PASSED\n", encoding="utf-8")
            except OSError:
                pass
        print("WINDOWS VALIDATION: NOT PASSED")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
