"""packaging/windows_validation.py: a phase can fail, time out or crash, but never vanish without a recorded result."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("windows_validation", REPO / "packaging" / "windows_validation.py")
wv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wv)

OK_OUT = ["....", "Ran 12 tests in 1.2s", "", "OK"]


class TestRunChild(unittest.TestCase):
    def run_child(self, args, timeout=30):
        logs, lines = [], []
        code = wv.run_child(logs.append, args, REPO, None, timeout, lines)
        return code, logs, lines

    def test_captures_output_and_nonzero_exit(self):
        code, logs, lines = self.run_child([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"])
        self.assertEqual(code, 3)
        self.assertIn("out", lines)
        self.assertIn("err", lines)                                   # stderr is captured too
        self.assertTrue(any("[exit 3" in l for l in logs))

    def test_timeout_is_recorded_and_the_child_killed(self):
        code, logs, _ = self.run_child([sys.executable, "-c", "import time; print('start', flush=True); time.sleep(120)"], timeout=2)
        self.assertEqual(code, -9)
        self.assertTrue(any("TIMEOUT" in l for l in logs))

    def test_launch_failure_is_recorded_not_raised(self):
        code, logs, _ = self.run_child([str(REPO / "no_such_executable")])
        self.assertEqual(code, -1)
        self.assertTrue(any("LAUNCH FAILED" in l for l in logs))


class TestUnittestVerdict(unittest.TestCase):
    def test_only_an_explicit_ok_summary_is_success(self):
        self.assertEqual(wv.unittest_verdict(0, OK_OUT), "OK")
        self.assertNotEqual(wv.unittest_verdict(0, []), "OK")                        # silent exit 0: not success
        self.assertNotEqual(wv.unittest_verdict(0, ["...", "WARNING x"]), "OK")      # output but no summary
        self.assertIn("no unittest summary", wv.unittest_verdict(1, ["boom"]))
        self.assertIn("exit code 1", wv.unittest_verdict(1, ["Ran 3 tests in 0.1s", "FAILED (failures=1)"]))
        self.assertIn("Ctrl", wv.unittest_verdict(0xC000013A, ["..."]))              # runner ended by Ctrl+Break
        self.assertIn("timeout", wv.unittest_verdict(-9, OK_OUT))
        self.assertNotEqual(wv.unittest_verdict(0, ["Ran 3 tests in 0.1s", "FAILED (failures=1)"]), "OK")


class TestMainBoundary(unittest.TestCase):
    def test_unexpected_exception_still_writes_summary_and_verdict(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            out.mkdir()
            real = wv._main
            wv._main = lambda argv=None: (_ for _ in ()).throw(RuntimeError("boom"))
            try:
                import contextlib, io
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    code = wv.main(["--repo", d, "--out", str(out)])
            finally:
                wv._main = real
            self.assertEqual(code, 2)
            summary = (out / "SUMMARY.txt").read_text()
            self.assertIn("RuntimeError: boom", summary)
            self.assertTrue(summary.rstrip().endswith("WINDOWS VALIDATION: NOT PASSED"))
            self.assertIn("UNEXPECTED VALIDATOR EXCEPTION", (out / "validation.log").read_text())


if __name__ == "__main__":
    unittest.main()


class TestManifestClassification(unittest.TestCase):
    """A saved workspace may BE the repository: build output is ignored, research data never is."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        for rel, text in {"data/edgelab.sqlite": "db", "data/imported/x.csv": "rows", "strategy_library/s.json": "{}",
                          "configs/a.yaml": "a: 1", "edgelab/mod.py": "x", "edgelab/web/static/app.js": "js",
                          "build/out.bin": "b", "dist/EdgeLab/EdgeLab.exe": "e", ".venv/Lib/x.pyd": "v",
                          "web/node_modules/m/i.js": "n", "edgelab/__pycache__/m.pyc": "c", "tests/t.py": "t"}.items():
            f = self.repo / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)

    def manifest(self, root, **kw):
        return wv.tree_manifest(root, self.repo, **kw)

    def test_build_outputs_are_not_in_a_repository_rooted_workspace_manifest(self):
        m = self.manifest(self.repo)
        for protected in ("data/edgelab.sqlite", "data/imported/x.csv", "strategy_library/s.json", "configs/a.yaml", "edgelab/mod.py"):
            self.assertIn(protected, m)
        for generated in ("build/out.bin", "dist/EdgeLab/EdgeLab.exe", ".venv/Lib/x.pyd", "web/node_modules/m/i.js",
                          "edgelab/__pycache__/m.pyc", "edgelab/web/static/app.js"):
            self.assertNotIn(generated, m)

    def test_build_output_changes_pass_but_research_data_changes_fail(self):
        before = self.manifest(self.repo)
        (self.repo / "dist/EdgeLab/EdgeLab.exe").write_text("rebuilt")
        (self.repo / "build/new.bin").write_text("n")
        (self.repo / ".venv/Lib/y.pyd").write_text("pip")
        self.assertEqual(wv.diff_trees(before, self.manifest(self.repo)), [])
        for rel, new in (("data/edgelab.sqlite", "changed"), ("data/imported/x.csv", "other"), ("strategy_library/s.json", "[]")):
            base = self.manifest(self.repo)
            (self.repo / rel).write_text(new)
            self.assertEqual(len(wv.diff_trees(base, self.manifest(self.repo))), 1, rel)
        before = self.manifest(self.repo)
        (self.repo / "data/imported/new.csv").write_text("added")
        self.assertEqual(wv.diff_trees(before, self.manifest(self.repo)), ["added: data/imported/new.csv"])

    def test_package_paths_are_left_to_the_working_tree_check(self):
        before = self.manifest(self.repo, exclude_paths=frozenset({"edgelab/mod.py"}))
        (self.repo / "edgelab/mod.py").write_text("package applied")
        self.assertEqual(wv.diff_trees(before, self.manifest(self.repo, exclude_paths=frozenset({"edgelab/mod.py"}))), [])
        self.assertNotEqual(wv.diff_trees(self.manifest(self.repo), wv.tree_manifest(self.repo, None)), [])

    def test_an_ordinary_data_folder_gets_no_exclusions(self):
        other = Path(self.tmp.name) / "workspace"
        (other / "build").mkdir(parents=True)
        (other / "build" / "x").write_text("user file called build")
        self.assertIn("build/x", wv.tree_manifest(other, self.repo))        # not repository-rooted: everything counts

    def test_repository_inside_a_larger_workspace(self):
        parent = Path(self.tmp.name)
        (parent / "notes.txt").write_text("user")
        m = wv.tree_manifest(parent, self.repo)
        self.assertIn("notes.txt", m)
        self.assertIn("repo/data/edgelab.sqlite", m)
        self.assertNotIn("repo/dist/EdgeLab/EdgeLab.exe", m)


class TestEarlierBundle(unittest.TestCase):
    def test_only_a_bundle_of_the_same_version_counts_as_regenerated(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            repo, pkg = Path(d) / "repo", Path(d) / "pkg"
            for root, ver in ((repo, "0.2.0"), (pkg, "0.2.0")):
                f = root / wv.STATIC_PREFIX / "build-info.json"
                f.parent.mkdir(parents=True)
                f.write_text(json.dumps({"app_version": ver}))
            self.assertTrue(wv.earlier_validation_bundle(repo, pkg))
            (repo / wv.STATIC_PREFIX / "build-info.json").write_text(json.dumps({"app_version": "0.1.0"}))   # HEAD's bundle
            self.assertFalse(wv.earlier_validation_bundle(repo, pkg))
            (repo / wv.STATIC_PREFIX / "build-info.json").unlink()
            self.assertFalse(wv.earlier_validation_bundle(repo, pkg))
