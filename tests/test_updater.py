"""Application updater (ADR-58): release metadata, prompt decisions, verified staging and the
separate helper that swaps the installation. Fixture sources/transports only - no network, no
GitHub, no real installation touched."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from edgelab.updater import apply as ap
from edgelab.updater.core import (MANIFEST_NAME, MANIFEST_SCHEMA, UpdateError, current_platform, is_newer,
                                  parse_version, validate_manifest)
from edgelab.updater.manager import UpdateManager, wait_until
from edgelab.updater.source import DirectoryReleaseSource, GitHubReleaseSource, Release

REPO = Path(__file__).resolve().parents[1]
PLAT = current_platform()


def fake_app(folder: Path, version: str, marker: str = "") -> Path:
    """A folder that looks like a packaged EdgeLab build (executable stub + build manifest)."""
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / ap.exe_name()
    # the stub reports ready the way desktop.run does (edgelab.updater.apply.READY_ENV) and exits
    exe.write_text('#!/bin/sh\n[ -n "$EDGELAB_UPDATE_READY_FILE" ] && echo "{}" > "$EDGELAB_UPDATE_READY_FILE"\nexit 0\n'
                   if os.name == "posix" else "stub")
    exe.chmod(0o755)
    (folder / "edgelab_build.json").write_text(json.dumps({"app_version": version}))
    (folder / "_internal").mkdir(exist_ok=True)
    (folder / "_internal" / "payload.txt").write_text(f"version {version} {marker}")
    return folder


def zip_app(app: Path, out: Path) -> tuple[int, str]:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(app.rglob("*")):
            if p.is_file():
                z.write(p, (Path(app.name) / p.relative_to(app)).as_posix())
    data = out.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def manifest(version: str, size: int, sha: str, platform: str = PLAT, **kw) -> dict:
    return {"schema": MANIFEST_SCHEMA, "app": "EdgeLab", "version": version, "tag": f"v{version}", "platform": platform,
            "published_at": "2026-10-01T00:00:00+00:00", "notes": f"notes for {version}",
            "artifact": {"name": f"EdgeLab-{version}-{platform}.zip", "size": size, "sha256": sha, "app_dir": "EdgeLab"},
            **kw}


def local_release(folder: Path, version: str, *, corrupt: bool = False, platform: str = PLAT) -> Path:
    """A local release folder (what packaging/release.py writes)."""
    folder.mkdir(parents=True, exist_ok=True)
    app = fake_app(folder / "_build" / "EdgeLab", version)
    z = folder / f"EdgeLab-{version}-{platform}.zip"
    size, sha = zip_app(app, z)
    shutil.rmtree(folder / "_build")
    if corrupt:                                                   # same size, different bytes
        b = bytearray(z.read_bytes())
        b[-30] ^= 0xFF
        z.write_bytes(bytes(b))
    (folder / MANIFEST_NAME).write_text(json.dumps(manifest(version, size, sha, platform)))
    return folder


class FakeSource:
    def __init__(self, result):
        self.result, self.calls = result, 0
        self.description = "fixture"

    def latest(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.children: list[subprocess.Popen] = []
        real_spawn = ap._spawn

        def spawn(cmd, env=None):                     # the test owns every process it (indirectly) starts
            p = real_spawn(cmd, env)
            self.children.append(p)
            return p

        ap._spawn = spawn
        self.addCleanup(setattr, ap, "_spawn", real_spawn)
        self.addCleanup(self.reap)
        self.state, self.cache = self.tmp / "state", self.tmp / "cache"

    def own(self, p: subprocess.Popen) -> subprocess.Popen:
        self.children.append(p)
        return p

    def reap(self):
        """Stop and wait for every child (no leaked processes, no ResourceWarning: 'subprocess is still running')."""
        for p in self.children:
            if p.poll() is None:
                p.kill()
            p.wait(30)
            if p.stdout:
                p.stdout.close()

    def mgr(self, source, current="0.2.0", **kw) -> UpdateManager:
        return UpdateManager(current, source, self.state, self.cache, **kw)


class TestVersionAndManifest(unittest.TestCase):
    def test_version_format_and_order(self):
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertTrue(is_newer("0.10.0", "0.9.9"))
        self.assertFalse(is_newer("0.2.0", "0.2.0"))
        for bad in ("1.2", "1.2.3-beta", "01.2.3", "x", "", None, "1.2.3.4"):
            with self.assertRaises(UpdateError):
                parse_version(bad)

    def test_single_authoritative_version(self):
        import edgelab
        parse_version(edgelab.__version__)
        pkg = json.loads((REPO / "web" / "package.json").read_text())
        self.assertEqual(pkg["version"], edgelab.__version__, "web/package.json must carry edgelab.__version__")
        lock = json.loads((REPO / "web" / "package-lock.json").read_text())
        self.assertEqual(lock["version"], edgelab.__version__)
        info = json.loads((REPO / "edgelab" / "web" / "static" / "build-info.json").read_text())
        self.assertEqual(info.get("app_version"), edgelab.__version__, "rebuild the frontend (web/build.mjs)")

    def test_manifest_validation(self):
        good = manifest("0.3.0", 100, "a" * 64)
        self.assertEqual(validate_manifest(good)["version"], "0.3.0")
        cases = {
            "schema": {**good, "schema": "x"},
            "tag": {**good, "tag": "v9.9.9"},
            "version": {**good, "version": "0.3"},
            "sha": {**good, "artifact": {**good["artifact"], "sha256": "ABC"}},
            "size": {**good, "artifact": {**good["artifact"], "size": -1}},
            "name-version": {**good, "artifact": {**good["artifact"], "name": f"EdgeLab-0.4.0-{PLAT}.zip"}},
            "platform": {**good, "platform": "plan9-x"},
            "app_dir": {**good, "artifact": {**good["artifact"], "app_dir": "../x"}},
            "not-object": [1, 2],
        }
        for label, m in cases.items():
            with self.assertRaises(UpdateError, msg=label) as cm:
                validate_manifest(m)
            self.assertEqual(cm.exception.code, "MALFORMED_METADATA", label)
        with self.assertRaises(UpdateError):
            validate_manifest(good, tag="v0.2.9")                  # release tag disagrees with the manifest


class FakeTransport:
    def __init__(self, routes):
        self.routes = routes

    def get_json(self, url, timeout):
        r = self.routes.get(url)
        if isinstance(r, Exception):
            raise r
        if r is None:
            raise UpdateError("HTTP_ERROR", f"no route {url}")
        return r

    def download(self, url, dest, progress, timeout):
        dest.write_bytes(self.routes[url])


class TestGitHubSource(unittest.TestCase):
    API = "https://api.github.com/repos/ShiningRedstone/AI-Backtesting/releases/latest"
    MURL = "https://github.com/ShiningRedstone/AI-Backtesting/releases/download/v0.3.0/edgelab-release.json"
    AURL = f"https://github.com/ShiningRedstone/AI-Backtesting/releases/download/v0.3.0/EdgeLab-0.3.0-{PLAT}.zip"

    def release(self, **kw):
        return {"tag_name": "v0.3.0", "draft": False, "prerelease": False, "published_at": "2026-10-01T00:00:00Z",
                "assets": [{"name": MANIFEST_NAME, "browser_download_url": self.MURL, "size": 500},
                           {"name": f"EdgeLab-0.3.0-{PLAT}.zip", "browser_download_url": self.AURL, "size": 1234}],
                **kw}

    def src(self, rel, man):
        return GitHubReleaseSource(transport=FakeTransport({self.API: rel, self.MURL: man}))

    def test_latest_release_parsed_and_cross_checked(self):
        r = self.src(self.release(), manifest("0.3.0", 1234, "b" * 64)).latest()
        self.assertEqual((r.version, r.artifact_url), ("0.3.0", self.AURL))

    def test_malformed_releases_refused(self):
        bad = {
            "size-mismatch": (self.release(), manifest("0.3.0", 999, "b" * 64)),
            "tag-mismatch": (self.release(tag_name="v0.2.9"), manifest("0.3.0", 1234, "b" * 64)),
            "no-manifest": (self.release(assets=[]), None),
            "prerelease": (self.release(prerelease=True), manifest("0.3.0", 1234, "b" * 64)),
            "garbage": ({"message": "Not Found"}, None),
            "non-github-url": (self.release(assets=[{"name": MANIFEST_NAME, "browser_download_url": self.MURL},
                                                    {"name": f"EdgeLab-0.3.0-{PLAT}.zip", "size": 1234,
                                                     "browser_download_url": "http://evil.example/x.zip"}]),
                               manifest("0.3.0", 1234, "b" * 64)),
        }
        for label, (rel, man) in bad.items():
            with self.assertRaises(UpdateError, msg=label):
                self.src(rel, man).latest()

    def test_network_failures_are_codes(self):
        for code in ("OFFLINE", "RATE_LIMITED", "HTTP_ERROR"):
            s = GitHubReleaseSource(transport=FakeTransport({self.API: UpdateError(code, "x")}))
            with self.assertRaises(UpdateError) as cm:
                s.latest()
            self.assertEqual(cm.exception.code, code)


class TestPromptDecisions(Base):
    def rel(self, version):
        m = validate_manifest(manifest(version, 10, "c" * 64))
        return Release(m, "file:///nope", "file:///nope", "fixture")

    def test_up_to_date_newer_and_downgrade(self):
        self.assertFalse(self.mgr(FakeSource(self.rel("0.2.0"))).check()["available"])      # current == latest
        s = self.mgr(FakeSource(self.rel("0.3.0"))).check()
        self.assertTrue(s["available"] and s["prompt"])
        self.assertEqual((s["release"]["version"], s["release"]["notes"]), ("0.3.0", "notes for 0.3.0"))
        m = self.mgr(FakeSource(self.rel("0.1.9")))
        s = m.check()
        self.assertFalse(s["available"])                                                     # never offered
        self.assertIn("downgrades are never offered", s["note"])
        with self.assertRaises(UpdateError) as cm:
            m.download("0.1.9")
        self.assertEqual(cm.exception.code, "DOWNGRADE_REFUSED")

    def test_offline_and_malformed_are_non_fatal(self):
        for err in (UpdateError("OFFLINE", "no network"), UpdateError("MALFORMED_METADATA", "bad"),
                    RuntimeError("anything")):
            s = self.mgr(FakeSource(err)).check()
            self.assertEqual(s["check"]["state"], "error")
            self.assertFalse(s["available"] or s["prompt"])
            self.assertEqual(s["current_version"], "0.2.0")

    def test_later_is_session_only_and_skip_persists(self):
        m = self.mgr(FakeSource(self.rel("0.3.0")))
        m.check()
        self.assertFalse(m.later("0.3.0")["prompt"])
        m2 = self.mgr(FakeSource(self.rel("0.3.0")))                                          # next start
        self.assertTrue(m2.check()["prompt"])
        s = m2.skip("0.3.0")
        self.assertTrue(s["skipped"] and not s["prompt"])
        m3 = self.mgr(FakeSource(self.rel("0.3.0")))
        self.assertFalse(m3.check()["prompt"])                                                # skipped for good
        self.assertTrue(self.mgr(FakeSource(self.rel("0.3.1"))).check()["prompt"])            # a newer one asks again
        self.assertTrue(m3.unskip("0.3.0")["prompt"])

    def test_auto_check_preference(self):
        dev = self.mgr(FakeSource(self.rel("0.3.0")))                                        # development run: never automatic
        dev.maybe_auto_check()
        self.assertEqual(dev.status()["check"]["state"], "idle")
        m = self.mgr(FakeSource(self.rel("0.3.0")), install_dir=self.tmp / "inst")
        m.set_auto_check(False)
        m.maybe_auto_check()
        self.assertEqual(m.status()["check"]["state"], "idle")
        m.set_auto_check(True)
        m.maybe_auto_check()
        self.assertTrue(wait_until(lambda: m.status()["check"]["state"] == "done"))


class TestDownloadAndStage(Base):
    def test_verified_download_is_staged(self):
        m = self.mgr(DirectoryReleaseSource(local_release(self.tmp / "rel", "0.3.0")))
        m.check()
        s = m.download("0.3.0", wait=True)
        self.assertEqual(s["download"]["state"], "ready", s["download"])
        staged = Path(s["download"]["staged"])
        self.assertEqual(ap._manifest_version(staged), "0.3.0")
        self.assertTrue((self.cache / "0.3.0" / "READY.json").is_file())

    def test_checksum_mismatch_discards_everything(self):
        m = self.mgr(DirectoryReleaseSource(local_release(self.tmp / "rel", "0.3.0", corrupt=True)))
        m.check()
        s = m.download("0.3.0", wait=True)
        self.assertEqual((s["download"]["state"], s["download"]["error"]["code"]), ("error", "CHECKSUM_MISMATCH"))
        self.assertFalse((self.cache / "0.3.0").exists())
        with self.assertRaises(UpdateError) as cm:
            self.mgr(DirectoryReleaseSource(self.tmp / "rel"), install_dir=self.tmp / "inst").apply("0.3.0")
        self.assertEqual(cm.exception.code, "NOT_STAGED")

    def test_interrupted_and_failed_downloads(self):
        rel_dir = local_release(self.tmp / "rel", "0.3.0")

        class Broken:
            def download(self, url, dest, progress, timeout):
                dest.write_bytes(b"partial")
                raise UpdateError("DOWNLOAD_FAILED", "connection reset")

            def get_json(self, url, timeout):
                raise AssertionError

        src = DirectoryReleaseSource(rel_dir)
        m = self.mgr(src, transport=Broken())
        rel = src.latest()
        m._release = Release(rel.manifest, "https://github.com/x/y.zip", rel.manifest_url, "fixture")
        s = m.download("0.3.0", wait=True)
        self.assertEqual(s["download"]["error"]["code"], "DOWNLOAD_FAILED")
        self.assertFalse((self.cache / "0.3.0").exists())                                    # no partial file kept
        (rel_dir / MANIFEST_NAME).write_text(json.dumps(manifest("0.3.0", 5, "d" * 64)))       # size lies
        m = self.mgr(DirectoryReleaseSource(rel_dir))
        m.check()
        self.assertEqual(m.download("0.3.0", wait=True)["download"]["error"]["code"], "SIZE_MISMATCH")

    def test_unsafe_zip_members_refused(self):
        z = self.tmp / "evil.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("EdgeLab/../../outside.txt", "x")
        with self.assertRaises(UpdateError) as cm:
            UpdateManager._extract(z, self.tmp / "x", "EdgeLab", "0.3.0")
        self.assertEqual(cm.exception.code, "MALFORMED_ARTIFACT")
        self.assertFalse((self.tmp.parent / "outside.txt").exists())
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("Other/EdgeLab.exe", "x")
        with self.assertRaises(UpdateError):
            UpdateManager._extract(z, self.tmp / "y", "EdgeLab", "0.3.0")


# a relaunched "new version" that reports ready (as desktop.run does) and keeps running for a moment
READY_THEN_RUN = ("import json, os, time; json.dump({'pid': os.getpid()}, open(os.environ['EDGELAB_UPDATE_READY_FILE'], 'w')); "
                  "time.sleep(3)")


class TestApplyHelper(Base):
    def install(self, version="0.2.0"):
        return fake_app(self.tmp / "Programs" / "EdgeLab", version, "installed")

    def test_swap_restart_and_cleanup(self):
        inst = self.install()
        staged = fake_app(self.tmp / "staged" / "EdgeLab", "0.3.0", "new")
        log = self.tmp / "update.log"
        restart = [sys.executable, "-c", READY_THEN_RUN]
        res = ap.apply_update(inst, staged, "0.3.0", restart_cmd=restart, log=log, ready_timeout=20)
        self.assertTrue(res["ok"], res)
        self.assertEqual(ap._manifest_version(inst), "0.3.0")
        self.assertIn("new", (inst / "_internal" / "payload.txt").read_text())
        self.assertFalse(list(inst.parent.glob("EdgeLab.old-*")))                             # backup removed
        self.assertIn("update_completed", log.read_text())

    def test_failed_restart_restores_previous_version(self):
        inst = self.install()
        staged = fake_app(self.tmp / "staged" / "EdgeLab", "0.3.0", "new")
        res = ap.apply_update(inst, staged, "0.3.0", restart_cmd=[sys.executable, "-c", "raise SystemExit(3)"],
                              log=self.tmp / "u.log", ready_timeout=20)
        self.assertFalse(res["ok"])
        self.assertTrue(res["restored_previous"])
        self.assertEqual(ap._manifest_version(inst), "0.2.0")                                 # installation preserved
        self.assertIn("installed", (inst / "_internal" / "payload.txt").read_text())

    def test_new_version_stuck_at_startup_is_not_mistaken_for_success(self):
        """A windowed build that fails at start-up sits behind a modal error dialog: alive but never ready. The
        helper must not report success: it stops the stuck process, restores the previous version and relaunches it."""
        inst = self.install()
        staged = fake_app(self.tmp / "staged" / "EdgeLab", "0.3.0", "new")
        marker, pidfile = self.tmp / "previous_started.txt", self.tmp / "stuck.pid"
        script = self.tmp / "launcher.py"                       # behaves by the version now installed at `inst`
        script.write_text(
            "import json, os, sys, time\n"
            f"v = json.load(open(r'{inst / 'edgelab_build.json'}'))['app_version']\n"
            f"if v == '0.3.0':\n    open(r'{pidfile}', 'w').write(str(os.getpid())); time.sleep(120)   # stuck, never ready\n"
            f"else:\n    open(r'{marker}', 'w').write(v)\n")
        res = ap.apply_update(inst, staged, "0.3.0", restart_cmd=[sys.executable, str(script)], log=self.tmp / "u.log",
                              ready_timeout=3)
        self.assertFalse(res["ok"])
        self.assertEqual(res["step"], "restart")
        self.assertIn("not ready within", res["error"])
        self.assertTrue(res["restored_previous"] and res.get("new_process_stopped") and res.get("relaunched_previous"))
        self.assertFalse(ap.pid_alive(int(pidfile.read_text())))                          # no stuck process left
        self.assertEqual(ap._manifest_version(inst), "0.2.0")                             # previous version intact
        self.assertIn("installed", (inst / "_internal" / "payload.txt").read_text())
        self.assertTrue(wait_until(marker.exists, 10))
        self.assertEqual(marker.read_text(), "0.2.0")                                     # and running again
        self.assertEqual([p.name.split("-")[0] for p in inst.parent.iterdir() if p.name != "EdgeLab"], ["EdgeLab.failed"])
        log = [json.loads(line) for line in (self.tmp / "u.log").read_text().splitlines()]
        self.assertEqual(log[-1]["event"], "update_failed")
        self.assertNotIn("update_completed", [e["event"] for e in log])

    def test_refusals_change_nothing(self):
        inst = self.install()
        before = sorted(p.name for p in inst.parent.iterdir())
        wrong = fake_app(self.tmp / "staged" / "EdgeLab", "0.9.9")
        self.assertIn("not EdgeLab 0.3.0", ap.apply_update(inst, wrong, "0.3.0")["error"])
        (inst / "data").mkdir()                                                               # a workspace inside
        staged = fake_app(self.tmp / "staged2" / "EdgeLab", "0.3.0")
        res = ap.apply_update(inst, staged, "0.3.0")
        self.assertIn("research data", res["error"])
        (inst / "data").rmdir()
        res = ap.apply_update(inst, staged, "0.3.0", protected=[inst.parent])                 # overlaps a protected root
        self.assertIn("overlaps the protected folder", res["error"])
        res = ap.apply_update(self.tmp / "nothing-here", staged, "0.3.0")
        self.assertIn("not a packaged EdgeLab folder", res["error"])
        self.assertEqual(sorted(p.name for p in inst.parent.iterdir()), before)
        self.assertEqual(ap._manifest_version(inst), "0.2.0")

    def test_locked_install_relaunches_the_unchanged_previous_version(self):
        """Windows: a file held open inside the installation makes the folder rename fail. The update is
        abandoned, the installation is unchanged, and the previous version is started again."""
        inst = self.install()
        staged = fake_app(self.tmp / "staged" / "EdgeLab", "0.3.0")
        marker = self.tmp / "relaunched.txt"
        real = ap._rename

        def locked(src, dst, attempts=40):
            raise OSError("[WinError 5] Access is denied (simulated open handle)")

        ap._rename = locked
        try:
            res = ap.apply_update(inst, staged, "0.3.0", log=self.tmp / "u.log",
                                  restart_cmd=[sys.executable, "-c", f"open(r'{marker}', 'w').write('x')"])
        finally:
            ap._rename = real
        self.assertFalse(res["ok"])
        self.assertEqual(res["step"], "swap")
        self.assertTrue(res.get("relaunched_previous"))
        self.assertTrue(wait_until(marker.exists, 10))
        self.assertEqual(ap._manifest_version(inst), "0.2.0")
        self.assertEqual(sorted(p.name for p in inst.parent.iterdir()), ["EdgeLab"])       # no .new / .old left
        wait_pid_fail = ap.apply_update(inst, staged, "0.3.0", wait_pid=os.getpid(), exit_timeout=0.3,
                                        restart_cmd=[sys.executable, "-c", "pass"])
        self.assertFalse(wait_pid_fail.get("relaunched_previous"))                       # the app is still running

    def test_waits_for_the_running_app_and_times_out_safely(self):
        inst = self.install()
        staged = fake_app(self.tmp / "staged" / "EdgeLab", "0.3.0")
        p = self.own(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]))
        res = ap.apply_update(inst, staged, "0.3.0", wait_pid=p.pid, exit_timeout=0.5)
        self.assertEqual(res["step"], "wait")
        self.assertEqual(ap._manifest_version(inst), "0.2.0")
        p.kill()
        p.wait()
        self.assertTrue(ap.apply_update(inst, staged, "0.3.0", wait_pid=p.pid, exit_timeout=5)["ok"])

    def test_interrupted_swap_leaves_previous_version(self):
        inst = self.install()
        staged = fake_app(self.tmp / "staged" / "EdgeLab", "0.3.0")
        real = ap._rename
        calls = []

        def flaky(src, dst, attempts=20):
            calls.append((src, dst))
            if len(calls) == 2:                                     # activating the new folder fails
                raise OSError("simulated crash / lock")
            return real(src, dst, 1)

        ap._rename = flaky
        try:
            res = ap.apply_update(inst, staged, "0.3.0")
        finally:
            ap._rename = real
        self.assertFalse(res["ok"])
        self.assertTrue(res["restored_previous"])
        self.assertEqual(ap._manifest_version(inst), "0.2.0")
        ap.cleanup_leftovers(inst)
        self.assertEqual(sorted(p.name for p in inst.parent.iterdir()), ["EdgeLab"])


class TestEndToEnd(Base):
    """Update now: check -> download -> verify -> stage -> helper process swaps and restarts."""

    def test_update_now_through_the_helper_process(self):
        inst = fake_app(self.tmp / "Programs" / "EdgeLab", "0.2.0")
        rel = local_release(self.tmp / "rel", "0.3.0")
        shut = []
        app_proc = self.own(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]))   # stands in for EdgeLab

        def shutdown():                                      # the app exits when asked (as desktop.run does)
            shut.append(1)
            app_proc.kill()
            app_proc.wait()

        helper = lambda staged, args: [sys.executable, "-m", "edgelab.updater.apply", *args,   # noqa: E731
                                       "--ready-timeout", "20"]
        m = self.mgr(DirectoryReleaseSource(rel), install_dir=inst, helper_cmd=helper,
                     protected=lambda: [self.tmp / "workspace"], shutdown=shutdown)
        m.app_pid = app_proc.pid
        self.assertTrue(m.check()["prompt"])
        self.assertEqual(m.download("0.3.0", wait=True)["download"]["state"], "ready")
        if os.name != "posix":
            self.skipTest("the stub executable is a POSIX shell script")
        os.environ["PYTHONPATH"] = str(REPO)                   # the helper imports edgelab from this checkout
        out = m.apply("0.3.0")
        self.assertTrue(out["applying"])
        self.assertTrue(wait_until(lambda: shut == [1], 5))                              # the app is asked to exit
        self.assertTrue(wait_until(lambda: ap._manifest_version(inst) == "0.3.0", 20), m.log_path.read_text()
                        if m.log_path.exists() else "no log")
        self.assertTrue(wait_until(lambda: "update_completed" in m.log_path.read_text(), 20))
        self.assertEqual(json.loads([l for l in m.log_path.read_text().splitlines() if "update_completed" in l][-1])["ok"],
                         True)

    def test_one_click_install_checks_downloads_and_restarts(self):
        """Settings 'Update now': one action goes from check to the helper restart; up to date is left alone."""
        if os.name != "posix":
            self.skipTest("the stub executable is a POSIX shell script")
        inst = fake_app(self.tmp / "Programs" / "EdgeLab", "0.2.0")
        rel = local_release(self.tmp / "rel", "0.3.0")
        shut = []
        app_proc = self.own(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]))

        def shutdown():
            shut.append(1)
            app_proc.kill()
            app_proc.wait()

        helper = lambda staged, args: [sys.executable, "-m", "edgelab.updater.apply", *args,   # noqa: E731
                                       "--ready-timeout", "20"]
        m = self.mgr(DirectoryReleaseSource(rel), install_dir=inst, helper_cmd=helper,
                     protected=lambda: [self.tmp / "workspace"], shutdown=shutdown)
        m.app_pid = app_proc.pid
        os.environ["PYTHONPATH"] = str(REPO)
        out = m.install(wait=True)
        self.assertEqual((out["install"]["state"], out["install"]["version"]), ("applying", "0.3.0"), out["install"])
        self.assertTrue(wait_until(lambda: shut == [1], 5))
        self.assertTrue(wait_until(lambda: ap._manifest_version(inst) == "0.3.0", 20))
        self.assertTrue(wait_until(lambda: "update_completed" in m.log_path.read_text(), 20))
        same = self.mgr(DirectoryReleaseSource(rel), current="0.3.0", install_dir=self.tmp / "other", shutdown=shutdown)
        self.assertEqual(same.install(wait=True)["install"]["state"], "up_to_date")
        with self.assertRaises(UpdateError) as cm:                                       # development run
            self.mgr(DirectoryReleaseSource(rel)).install()
        self.assertEqual(cm.exception.code, "APPLY_UNSUPPORTED")
        bad = self.mgr(DirectoryReleaseSource(local_release(self.tmp / "bad", "0.4.0", corrupt=True)),
                       install_dir=fake_app(self.tmp / "P2" / "EdgeLab", "0.2.0"))
        r = bad.install(wait=True)["install"]
        self.assertEqual((r["state"], r["error"]["code"]), ("error", "CHECKSUM_MISMATCH"))
        self.assertEqual(ap._manifest_version(self.tmp / "P2" / "EdgeLab"), "0.2.0")      # nothing installed

    def test_apply_refused_in_development_and_for_unsafe_folders(self):
        rel = local_release(self.tmp / "rel", "0.3.0")
        m = self.mgr(DirectoryReleaseSource(rel))
        m.check()
        m.download("0.3.0", wait=True)
        with self.assertRaises(UpdateError) as cm:
            m.apply("0.3.0")
        self.assertEqual(cm.exception.code, "APPLY_UNSUPPORTED")
        ws = fake_app(self.tmp / "ws", "0.2.0")
        (ws / "strategy_library").mkdir()
        m2 = self.mgr(DirectoryReleaseSource(rel), install_dir=ws)
        m2.check()
        m2.download("0.3.0", wait=True)
        with self.assertRaises(UpdateError) as cm:
            m2.apply("0.3.0")
        self.assertEqual(cm.exception.code, "UNSAFE_INSTALL_DIR")


class TestReleaseTool(Base):
    def test_release_tool_output_is_accepted_by_the_updater(self):
        import edgelab
        dist = fake_app(self.tmp / "dist" / "EdgeLab", edgelab.__version__)
        (dist / "edgelab_build.json").write_text(json.dumps({"app_version": edgelab.__version__, "build_id": "X",
                                                             "git_tracked_changes": False}))
        static = dist / "_internal" / "edgelab" / "web" / "static"
        static.mkdir(parents=True)
        (static / "build-info.json").write_text(json.dumps({"app_version": edgelab.__version__}))
        out = self.tmp / "release"
        code = subprocess.run([sys.executable, str(REPO / "packaging" / "release.py"), "--dist", str(dist), "--out",
                               str(out)], capture_output=True, text=True, cwd=REPO)
        self.assertEqual(code.returncode, 0, code.stderr + code.stdout)
        self.assertIn("NOT published", code.stdout)
        rel = DirectoryReleaseSource(out).latest()
        self.assertEqual(rel.version, edgelab.__version__)
        m = UpdateManager("0.0.1", DirectoryReleaseSource(out), self.state, self.cache)
        m.check()
        self.assertEqual(m.download(edgelab.__version__, wait=True)["download"]["state"], "ready")
        (static / "build-info.json").write_text(json.dumps({"app_version": "0.0.9"}))           # UI/backend mismatch
        bad = subprocess.run([sys.executable, str(REPO / "packaging" / "release.py"), "--dist", str(dist), "--out",
                              str(self.tmp / "r2")], capture_output=True, text=True, cwd=REPO)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("frontend bundle says 0.0.9", bad.stderr)


def fake_build_app(folder: Path, version: str, build: int, channel: str) -> Path:
    app = fake_app(folder, version, marker=f"b{build}")
    (app / "edgelab_build.json").write_text(json.dumps({"app_version": version, "build_number": build, "channel": channel}))
    return app


def build_manifest_v2(version: str, build: int, channel: str, size: int, sha: str, platform: str = PLAT, **kw) -> dict:
    from edgelab.updater.core import MANIFEST_SCHEMA_BUILD, build_tag
    return {"schema": MANIFEST_SCHEMA_BUILD, "app": "EdgeLab", "version": version, "tag": build_tag(channel, build),
            "channel": channel, "build_number": build, "commit": "a" * 40, "platform": platform,
            "published_at": "2026-10-01T00:00:00+00:00", "notes": f"build {build}",
            "artifact": {"name": f"EdgeLab-{version}-b{build}-{platform}.zip", "size": size, "sha256": sha,
                         "app_dir": "EdgeLab"}, **kw}


def build_release(folder: Path, version: str, build: int, channel: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    app = fake_build_app(folder / "_build" / "EdgeLab", version, build, channel)
    z = folder / f"EdgeLab-{version}-b{build}-{PLAT}.zip"
    size, sha = zip_app(app, z)
    shutil.rmtree(folder / "_build")
    (folder / MANIFEST_NAME).write_text(json.dumps(build_manifest_v2(version, build, channel, size, sha)))
    return folder


class TestBranchBuilds(Base):
    """ADR-72: CI branch builds - an installation takes only newer builds of its OWN branch, in one click."""
    CH = "claude/zealous-heisenberg-7xwqax"

    def test_build_manifest_validation(self):
        from edgelab.updater.core import build_tag, parse_key
        ok = validate_manifest(build_manifest_v2("0.2.0", 7, self.CH, 10, "c" * 64))
        self.assertEqual((ok["key"], ok["channel"], ok["build_number"]), ("0.2.0-b7", self.CH, 7))
        self.assertEqual(build_tag(self.CH, 7), "build-claude-zealous-heisenberg-7xwqax-7")
        self.assertEqual(parse_key("0.2.0-b7"), (0, 2, 0, 7))
        for label, kw in {"tag": {"tag": "v0.2.0"}, "channel": {"channel": ""}, "build-bool": {"build_number": True},
                          "commit": {"commit": "xyz"}}.items():
            with self.assertRaises(UpdateError, msg=label):
                validate_manifest({**build_manifest_v2("0.2.0", 7, self.CH, 10, "c" * 64), **kw})
        wrong_art = build_manifest_v2("0.2.0", 7, self.CH, 10, "c" * 64)
        wrong_art["artifact"]["name"] = f"EdgeLab-0.2.0-b8-{PLAT}.zip"
        with self.assertRaises(UpdateError):
            validate_manifest(wrong_art)

    def test_newer_only_within_own_branch(self):
        from edgelab.updater.core import is_newer_release
        b9 = {"version": "0.2.0", "build_number": 9, "channel": self.CH}
        self.assertTrue(is_newer_release(b9, "0.2.0", 8, self.CH))
        self.assertFalse(is_newer_release(b9, "0.2.0", 9, self.CH))                 # same build
        self.assertFalse(is_newer_release(b9, "0.2.0", 3, "main"))                  # another branch's build
        self.assertFalse(is_newer_release(b9, "0.2.0", 0, None))                    # a build without a channel
        self.assertTrue(is_newer_release({"version": "0.3.0", "build_number": 0, "channel": None}, "0.2.0", 5, self.CH))

    def test_github_source_picks_highest_build_of_its_branch(self):
        api = "https://api.github.com/repos/ShiningRedstone/AI-Backtesting/releases?per_page=100"

        def rel(tag, n, ch=self.CH, **kw):
            base = f"https://github.com/ShiningRedstone/AI-Backtesting/releases/download/{tag}"
            return ({"tag_name": tag, "draft": False, "prerelease": True, "published_at": "2026-10-01T00:00:00Z",
                     "assets": [{"name": MANIFEST_NAME, "browser_download_url": f"{base}/{MANIFEST_NAME}", "size": 9},
                                {"name": f"EdgeLab-0.2.0-b{n}-{PLAT}.zip", "browser_download_url": f"{base}/a.zip",
                                 "size": 100}], **kw},
                    f"{base}/{MANIFEST_NAME}", build_manifest_v2("0.2.0", n, ch, 100, "d" * 64))
        mine3, mine7, other9 = rel(f"build-claude-zealous-heisenberg-7xwqax-3", 3), \
            rel(f"build-claude-zealous-heisenberg-7xwqax-7", 7), rel("build-main-9", 9, ch="main")
        draft8 = rel("build-claude-zealous-heisenberg-7xwqax-8", 8, draft=True)
        routes = {api: [other9[0], draft8[0], mine3[0], mine7[0], {"tag_name": "v0.1.0"}]}
        routes.update({u: m for _, u, m in (mine3, mine7, other9, draft8)})
        r = GitHubReleaseSource(transport=FakeTransport(routes), channel=self.CH).latest()
        self.assertEqual((r.manifest["build_number"], r.manifest["key"]), (7, "0.2.0-b7"))
        with self.assertRaises(UpdateError) as cm:
            GitHubReleaseSource(transport=FakeTransport(routes), channel="feature/none").latest()
        self.assertEqual(cm.exception.code, "NO_RELEASE")
        routes[mine7[1]] = build_manifest_v2("0.2.0", 7, "main", 100, "d" * 64)    # tag says this branch, manifest does not
        with self.assertRaises(UpdateError):
            GitHubReleaseSource(transport=FakeTransport(routes), channel=self.CH).latest()

    def test_one_click_install_of_a_branch_build(self):
        if os.name != "posix":
            self.skipTest("the stub executable is a POSIX shell script")
        inst = fake_build_app(self.tmp / "Programs" / "EdgeLab", "0.2.0", 3, self.CH)
        rel = build_release(self.tmp / "rel", "0.2.0", 5, self.CH)
        shut = []
        app_proc = self.own(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]))

        def shutdown():
            shut.append(1)
            app_proc.kill()
            app_proc.wait()

        helper = lambda staged, args: [sys.executable, "-m", "edgelab.updater.apply", *args,   # noqa: E731
                                       "--ready-timeout", "20"]
        m = self.mgr(DirectoryReleaseSource(rel, channel=self.CH), install_dir=inst, helper_cmd=helper,
                     protected=lambda: [self.tmp / "workspace"], shutdown=shutdown, build_number=3, channel=self.CH)
        m.app_pid = app_proc.pid
        st = m.check()
        self.assertTrue(st["prompt"])
        self.assertEqual((st["release"]["key"], st["release"]["build_number"], st["current_key"]), ("0.2.0-b5", 5, "0.2.0-b3"))
        os.environ["PYTHONPATH"] = str(REPO)
        out = m.install(wait=True)
        self.assertEqual((out["install"]["state"], out["install"]["version"]), ("applying", "0.2.0-b5"), out["install"])
        self.assertTrue(wait_until(lambda: shut == [1], 5))
        self.assertTrue(wait_until(lambda: ap._manifest_build(inst) == 5, 20),
                        m.log_path.read_text() if m.log_path.exists() else "no log")
        self.assertTrue(wait_until(lambda: "update_completed" in m.log_path.read_text(), 20))
        same = self.mgr(DirectoryReleaseSource(rel, channel=self.CH), install_dir=self.tmp / "x", build_number=5,
                        channel=self.CH, shutdown=shutdown)
        self.assertEqual(same.install(wait=True)["install"]["state"], "up_to_date")
        other = self.mgr(DirectoryReleaseSource(rel), install_dir=self.tmp / "y", build_number=1, channel="main")
        self.assertFalse(other.check()["available"])                                   # never another branch's build

    def test_auto_check_runs_at_start_and_then_every_interval(self):
        from edgelab.updater.manager import CHECK_INTERVAL
        src = FakeSource(UpdateError("OFFLINE", "x"))
        m = self.mgr(src, install_dir=fake_app(self.tmp / "P" / "EdgeLab", "0.2.0"))
        m.maybe_auto_check()
        self.assertTrue(wait_until(lambda: src.calls == 1 and m.status()["check"]["state"] == "error", 5))
        m.maybe_auto_check()
        time.sleep(0.2)
        self.assertEqual(src.calls, 1)                                                 # not again within the interval
        m._last_auto -= CHECK_INTERVAL.total_seconds() + 1
        m.maybe_auto_check()
        self.assertTrue(wait_until(lambda: src.calls == 2, 5))
        dev = self.mgr(FakeSource(UpdateError("OFFLINE", "x")))                        # development run: never automatic
        dev.maybe_auto_check()
        time.sleep(0.2)
        self.assertEqual(dev.source.calls, 0)

    def test_release_tool_prepares_a_branch_build(self):
        import edgelab
        dist = fake_app(self.tmp / "dist" / "EdgeLab", edgelab.__version__)
        (dist / "edgelab_build.json").write_text(json.dumps({"app_version": edgelab.__version__, "build_id": "X",
                                                             "git_tracked_changes": False, "git_commit": "e" * 40,
                                                             "channel": self.CH, "build_number": 42}))
        static = dist / "_internal" / "edgelab" / "web" / "static"
        static.mkdir(parents=True)
        (static / "build-info.json").write_text(json.dumps({"app_version": edgelab.__version__}))
        out = self.tmp / "release"
        code = subprocess.run([sys.executable, str(REPO / "packaging" / "release.py"), "--dist", str(dist), "--out",
                               str(out)], capture_output=True, text=True, cwd=REPO)
        self.assertEqual(code.returncode, 0, code.stderr + code.stdout)
        self.assertEqual((out / "release-tag.txt").read_text().strip(), "build-claude-zealous-heisenberg-7xwqax-42")
        self.assertTrue((out / f"EdgeLab-{edgelab.__version__}-b42-{PLAT}.zip").is_file())
        r = DirectoryReleaseSource(out, channel=self.CH).latest()
        self.assertEqual((r.manifest["key"], r.manifest["commit"]), (f"{edgelab.__version__}-b42", "e" * 40))
        with self.assertRaises(UpdateError) as cm:
            DirectoryReleaseSource(out, channel="main").latest()
        self.assertEqual(cm.exception.code, "NO_RELEASE")

    def test_routes_accept_build_keys(self):
        from edgelab.updater import service
        from edgelab.web.app import create_app
        rel = build_release(self.tmp / "rel", "0.2.0", 12, self.CH)
        service.set_manager(self.mgr(DirectoryReleaseSource(rel, channel=self.CH), build_number=4, channel=self.CH))
        self.addCleanup(service.set_manager, None)
        root = self.tmp / "ws"
        shutil.copytree(REPO / "configs", root / "configs")
        c = create_app(root).test_client()
        self.assertTrue(c.post("/api/update/check", json={}).get_json()["prompt"])
        self.assertTrue(c.post("/api/update/skip", json={"version": "0.2.0-b12"}).get_json()["skipped"])
        self.assertEqual(c.post("/api/update/skip", json={"version": "0.2.0-bx"}).status_code, 409)
        c.application.config["EDGELAB"]["services"].store.close()


class TestHttpRoutes(Base):
    def test_update_routes(self):
        from edgelab.updater import service
        from edgelab.web.app import create_app
        rel = local_release(self.tmp / "rel", "9.0.0")
        service.set_manager(self.mgr(DirectoryReleaseSource(rel)))
        self.addCleanup(service.set_manager, None)
        root = self.tmp / "ws"
        shutil.copytree(REPO / "configs", root / "configs")
        c = create_app(root).test_client()
        import edgelab
        self.assertEqual(c.get("/api/version").get_json()["version"], edgelab.__version__)
        s = c.post("/api/update/check", json={}).get_json()
        self.assertTrue(s["prompt"])
        self.assertFalse(c.post("/api/update/later", json={"version": "9.0.0"}).get_json()["prompt"])
        self.assertTrue(c.post("/api/update/skip", json={"version": "9.0.0"}).get_json()["skipped"])
        r = c.post("/api/update/apply", json={"version": "9.0.0"})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (409, "APPLY_UNSUPPORTED"))
        r = c.post("/api/update/install", json={})
        self.assertEqual((r.status_code, r.get_json()["error"]["code"]), (409, "APPLY_UNSUPPORTED"))
        self.assertEqual(c.post("/api/update/skip", json={"version": 3}).status_code, 400)
        self.assertEqual(c.post("/api/update/preferences", json={"auto_check": "yes"}).status_code, 400)
        self.assertFalse(c.post("/api/update/preferences", json={"auto_check": False}).get_json()["auto_check"])
        app = c.application.config["EDGELAB"]["services"]
        app.store.close()


if __name__ == "__main__":
    unittest.main()
