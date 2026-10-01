"""Phase 7 desktop hardening: runtime path resolution, persistent workspace, build manifest (code
identity when source files are absent), launcher startup / port / readiness / single instance /
shutdown, SQLite in the desktop process model. The packaged-build test runs only when a build
exists in dist/EdgeLab (it is produced by packaging/build.py)."""
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from edgelab import runtime

REPO = Path(__file__).resolve().parents[1]


class TestPaths(unittest.TestCase):
    def test_development_resources_are_the_repository(self):
        self.assertFalse(runtime.is_frozen())
        self.assertEqual(runtime.resource_dir(), REPO)
        self.assertEqual(runtime.static_dir(), REPO / "edgelab" / "web" / "static")
        self.assertEqual(runtime.default_config_dir(), REPO / "configs")
        self.assertIsNone(runtime.build_manifest())
        from edgelab.core import config
        self.assertEqual(config.DEFAULT_CONFIG_DIR, REPO / "configs")

    def test_user_data_root_per_platform_and_override(self):
        w = runtime.user_data_root({"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}, "Windows")
        self.assertEqual(w, Path(r"C:\Users\u\AppData\Local") / "EdgeLab")
        self.assertEqual(runtime.user_data_root({"HOME": "/home/u"}, "Linux"), Path("/home/u/.local/share/EdgeLab"))
        self.assertEqual(runtime.user_data_root({"HOME": "/Users/u"}, "Darwin"),
                         Path("/Users/u/Library/Application Support/EdgeLab"))
        self.assertEqual(runtime.user_data_root({"EDGELAB_DATA_ROOT": "/x/y", "LOCALAPPDATA": "C:/z"}, "Windows"),
                         Path("/x/y"))

    def test_frozen_paths_come_from_the_bundle(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", d, create=True):
            self.assertEqual(runtime.resource_dir(), Path(d))
            self.assertEqual(runtime.static_dir(), Path(d) / "edgelab" / "web" / "static")
            self.assertEqual(runtime.default_config_dir(), Path(d) / "configs")


class TestWorkspace(unittest.TestCase):
    def test_configs_copied_once_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "EdgeLab"
            first = runtime.init_workspace(root)
            self.assertTrue(first["configs_created"])
            self.assertEqual(first["config_differences"], [])
            for p in ("configs/storage.yaml", "configs/prop/synthetic_static_eval.yaml", "data/import", "logs",
                      "reports", runtime.WORKSPACE_MARKER):
                self.assertTrue((root / p).exists(), p)
            self.assertFalse((root / "edgelab").exists() or (root / "tests").exists())   # no repository copy
            (root / "configs" / "web.yaml").write_text("web:\n  port: 9999\n")
            again = runtime.init_workspace(root)
            self.assertFalse(again["configs_created"])
            self.assertEqual((root / "configs" / "web.yaml").read_text(), "web:\n  port: 9999\n")
            self.assertEqual(again["config_differences"], ["web.yaml"])        # reported, not applied
            from edgelab.core.config import config_hash, load_config
            self.assertEqual(config_hash(load_config(root / "configs")), config_hash(load_config(REPO / "configs")))

    def test_refuses_a_data_root_inside_the_bundle(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", d, create=True):
            with self.assertRaisesRegex(ValueError, "inside the application bundle"):
                runtime.init_workspace(Path(d) / "data", defaults=REPO / "configs")


class TestBuildManifest(unittest.TestCase):
    def setUp(self):
        runtime._load_manifest.cache_clear()
        self.addCleanup(runtime._load_manifest.cache_clear)

    def frozen(self, bundle):
        return (mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "_MEIPASS", str(bundle), create=True))

    def test_packaged_identity_equals_development_identity(self):
        from edgelab.core.identity import code_version, git_commit, source_hash
        from edgelab.features.spec import all_defs
        from edgelab.strategy.compiler import compiler_source_hash
        dev = {"source": source_hash(), "commit": git_commit(), "compiler": compiler_source_hash(),
               "features": {fd.feature_id: fd.impl_hash for fd in all_defs()}}
        m = runtime.generate_build_manifest(REPO, extra={"pyinstaller": "test"})
        self.assertEqual((m["source_sha256"], m["compiler_source_sha256"], m["feature_impl_hashes"]),
                         (dev["source"], dev["compiler"], dev["features"]))
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / runtime.BUILD_MANIFEST).write_text(json.dumps(m))
            a, b = self.frozen(d)
            with a, b:
                cv = code_version()
                self.assertEqual((cv["source_sha256"], cv["git_commit"], cv["build_id"], cv["packaged"]),
                                 (dev["source"], dev["commit"], m["build_id"], True))
                self.assertEqual(compiler_source_hash(), dev["compiler"])
                self.assertEqual({fd.feature_id: fd.impl_hash for fd in all_defs()}, dev["features"])
                self.assertEqual(runtime.runtime_info()["build_id"], m["build_id"])
        self.assertEqual(set(code_version()), {"git_commit", "source_sha256"})       # development unchanged

    def test_build_id_changes_with_the_build(self):
        a = runtime.generate_build_manifest(REPO, extra={"pyinstaller": "6.0"})
        b = runtime.generate_build_manifest(REPO, extra={"pyinstaller": "6.1"})
        c = runtime.generate_build_manifest(REPO, extra={"pyinstaller": "6.0"})
        self.assertNotEqual(a["build_id"], b["build_id"])
        self.assertEqual(a["build_id"], c["build_id"])                   # built_at is not part of the id
        changed = dict(a, source_sha256="0" * 64)
        ids = {k: changed.get(k) for k in ("app_version", "git_commit", "source_sha256", "compiler_source_sha256",
                                           "feature_impl_hashes", "frontend_source_sha256", "python", "pyinstaller")}
        import hashlib
        self.assertNotEqual(hashlib.sha256(json.dumps(ids, sort_keys=True).encode()).hexdigest()[:16].upper(), a["build_id"])

    def test_packaged_without_manifest_refuses_instead_of_faking(self):
        from edgelab.core.identity import code_version
        from edgelab.features.spec import all_defs
        with tempfile.TemporaryDirectory() as d:
            a, b = self.frozen(d)
            with a, b:
                with self.assertRaises(runtime.BuildManifestError):
                    code_version()
                with self.assertRaises(runtime.BuildManifestError):
                    all_defs()[0].impl_hash
            (Path(d) / runtime.BUILD_MANIFEST).write_text(json.dumps({"schema": 1}))
            runtime._load_manifest.cache_clear()
            with a, b:
                with self.assertRaisesRegex(runtime.BuildManifestError, "not a valid"):
                    code_version()
            m = runtime.generate_build_manifest(REPO)
            m["feature_impl_hashes"].pop(all_defs()[0].feature_id)
            (Path(d) / runtime.BUILD_MANIFEST).write_text(json.dumps(m))
            runtime._load_manifest.cache_clear()
            with a, b:
                with self.assertRaisesRegex(runtime.BuildManifestError, "no implementation hash"):
                    all_defs()[0].impl_hash


def _wait_runtime(path, proc, timeout=180):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except ValueError:
                pass
        if proc.poll() is not None:
            return None
        time.sleep(0.2)
    return None


class TestLoopbackIgnoresProxy(unittest.TestCase):
    def test_readiness_check_never_goes_through_a_proxy(self):
        """A user environment with HTTP(S)_PROXY set must not break start-up: the launcher's own calls to
        127.0.0.1 bypass any proxy (regression: the offline update smoke test found this)."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from edgelab.desktop import wait_ready

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/api/health" else 404)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        dead = "http://127.0.0.1:9"                                   # nothing listens there
        with mock.patch.dict(os.environ, {"HTTP_PROXY": dead, "http_proxy": dead, "HTTPS_PROXY": dead,
                                          "https_proxy": dead, "NO_PROXY": "", "no_proxy": ""}):
            wait_ready(f"http://127.0.0.1:{srv.server_port}", timeout=10)   # raises StartupError if proxied


class TestLauncher(unittest.TestCase):
    """The development launcher (python -m edgelab.desktop) on a scratch workspace."""

    def launch(self, root, *extra):
        return subprocess.Popen([sys.executable, "-m", "edgelab.desktop", "--data-root", str(root), "--no-browser",
                                 *extra], cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                # Windows: CTRL_BREAK_EVENT below is addressed to a process GROUP. Without its own group
                                # it is broadcast to every process on the console, ending the test runner (and anything
                                # that started it) without a traceback.
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)

    def test_start_ready_single_instance_and_clean_shutdown(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "ws"
            p = self.launch(root)
            try:
                info = _wait_runtime(root / "logs" / "runtime.json", p)
                self.assertIsNotNone(info, p.stdout.read() if p.poll() is not None else "")
                self.assertTrue(info["url"].startswith("http://127.0.0.1:"))
                self.assertGreater(info["port"], 0)
                with urllib.request.urlopen(info["url"] + "/api/status", timeout=30) as r:
                    st = json.loads(r.read())
                self.assertEqual(st["store_backend"], "sqlite")
                self.assertEqual(Path(st["root"]), root.resolve())
                self.assertFalse(st["runtime"]["packaged"])
                with urllib.request.urlopen(info["url"] + "/app.js", timeout=30) as r:
                    self.assertEqual(r.status, 200)
                # nothing listens on other interfaces: the socket is bound to loopback only
                with socket.socket() as s:
                    s.settimeout(1)
                    ext = [a for a in socket.gethostbyname_ex(socket.gethostname())[2] if not a.startswith("127.")]
                    if ext:
                        self.assertNotEqual(s.connect_ex((ext[0], info["port"])), 0)
                second = subprocess.run([sys.executable, "-m", "edgelab.desktop", "--data-root", str(root),
                                         "--no-browser"], cwd=REPO, capture_output=True, text=True, timeout=120)
                self.assertEqual(second.returncode, 0)
                self.assertIn("already running", second.stdout)
            finally:
                p.send_signal(signal.SIGTERM if os.name != "nt" else signal.CTRL_BREAK_EVENT)
                out, _ = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, out[-2000:])
            self.assertFalse((root / "logs" / "runtime.json").exists())
            con = sqlite3.connect(root / "data" / "edgelab.sqlite")
            self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            con.close()

    def test_busy_port_is_a_visible_error(self):
        with tempfile.TemporaryDirectory() as d, socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            r = subprocess.run([sys.executable, "-m", "edgelab.desktop", "--data-root", str(Path(d) / "ws"),
                                "--no-browser", "--port", str(s.getsockname()[1])], cwd=REPO,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 1)
            self.assertIn("Munyun Lab could not start", r.stderr)
            self.assertIn("already in use", r.stderr)
            self.assertTrue((Path(d) / "ws" / "logs" / "desktop.log").read_text())

    def test_cli_passthrough_uses_the_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "EDGELAB_DATA_ROOT": str(Path(d) / "ws"), "PYTHONPATH": str(REPO)}
            r = subprocess.run([sys.executable, "-m", "edgelab.desktop", "cli", "--json", "datasets"], cwd=d, env=env,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr[-1000:])
            self.assertEqual(json.loads(r.stdout), [])
            self.assertTrue((Path(d) / "ws" / "configs" / "storage.yaml").exists())
            self.assertTrue((Path(d) / "ws" / "data" / "edgelab.sqlite").exists())


@unittest.skipUnless((REPO / "dist" / "EdgeLab" / runtime.BUILD_MANIFEST).exists(), "no packaged build in dist/EdgeLab")
class TestPackagedBuild(unittest.TestCase):
    def test_packaged_smoke(self):
        exe = REPO / "dist" / "EdgeLab" / ("EdgeLab.exe" if os.name == "nt" else "EdgeLab")
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run([sys.executable, str(REPO / "packaging" / "smoke_packaged.py"), "--exe", str(exe),
                                "--data-root", str(Path(d) / "root")], capture_output=True, text=True, timeout=1500)
        self.assertEqual(r.returncode, 0, r.stdout[-3000:])
        self.assertIn("PACKAGED SMOKE TEST: PASS", r.stdout)


if __name__ == "__main__":
    unittest.main()
