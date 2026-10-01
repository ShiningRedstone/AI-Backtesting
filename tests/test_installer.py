"""ADR-76: the Windows installer. Its icon is a valid multi-size .ico, the Inno Setup script installs per user into the
folder the updater replaces (uninstaller outside it, both shortcuts optional), the build script names the installer
after the build manifest, and the permanent "installer-main" release never reaches the updater."""
import importlib.util
import struct
import tempfile
import unittest
from pathlib import Path

from tests.test_updater import FakeTransport, MANIFEST_NAME, PLAT, build_manifest_v2

REPO = Path(__file__).resolve().parents[1]


def _load(name):
    """packaging/<name>.py by path (packaging/ is a folder of scripts, not a package; `packaging` is also a PyPI name)."""
    spec = importlib.util.spec_from_file_location(f"_pkg_{name}", REPO / "packaging" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestIcon(unittest.TestCase):
    def test_committed_icon_is_the_generated_multi_size_ico(self):
        icon = _load("icon")
        committed = (REPO / "packaging" / "munyun.ico").read_bytes()
        with tempfile.TemporaryDirectory() as d:
            fresh = icon.write_ico(Path(d) / "x.ico").read_bytes()
        self.assertEqual(committed, fresh)                                    # deterministic; committed file is current
        reserved, kind, count = struct.unpack("<HHH", committed[:6])
        self.assertEqual((reserved, kind, count), (0, 1, len(icon.SIZES)))
        for k, size in enumerate(icon.SIZES):
            w, h, _, _, planes, bpp, length, offset = struct.unpack("<BBBBHHII", committed[6 + 16 * k: 22 + 16 * k])
            self.assertEqual((w or 256, h or 256, planes, bpp), (size, size, 1, 32))
            png = committed[offset: offset + length]
            self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(struct.unpack(">II", png[16:24]), (size, size))


class TestInstallerScript(unittest.TestCase):
    def setUp(self):
        self.iss = (REPO / "packaging" / "installer.iss").read_text(encoding="utf-8")

    def setting(self, key):
        for line in self.iss.splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
        return None

    def test_per_user_into_the_updated_folder(self):
        self.assertEqual(self.setting("PrivilegesRequired"), "lowest")       # no administrator rights
        self.assertEqual(self.setting("DefaultDirName"), r"{localappdata}\Programs\EdgeLab")
        uninst = self.setting("UninstallFilesDir")
        self.assertFalse(uninst.startswith("{app}"))                          # an auto-update swaps the whole {app}
        self.assertNotEqual(uninst.rstrip("\\"), self.setting("DefaultDirName"))
        self.assertIn(r'Filename: "{app}\EdgeLab.exe"', self.iss)            # the updater relies on this name

    def test_both_shortcuts_are_optional_tasks(self):
        self.assertIn('Name: "startmenuicon"', self.iss)
        self.assertIn('Name: "desktopicon"', self.iss)
        self.assertIn(r'Name: "{autoprograms}\Munyun Lab"', self.iss)
        self.assertIn(r'Name: "{autodesktop}\Munyun Lab"', self.iss)
        self.assertIn("Tasks: startmenuicon", self.iss)
        self.assertIn("Tasks: desktopicon", self.iss)

    def test_never_touches_workspaces(self):
        self.assertNotIn(r"{localappdata}\EdgeLab\\", self.iss)
        for line in self.iss.split("[UninstallDelete]", 1)[1].splitlines():
            if line.startswith("Type:"):
                self.assertIn('Name: "{app}', line)                         # only the program folder and its leftovers


class TestBuildInstaller(unittest.TestCase):
    def test_name_and_defines_follow_the_build_manifest(self):
        bi = _load("build_installer")
        self.assertEqual(bi.installer_name("0.2.0", 12), "MunyunLab-Setup-0.2.0-b12")
        self.assertEqual(bi.installer_name("0.2.0", 0), "MunyunLab-Setup-0.2.0")
        with tempfile.TemporaryDirectory() as d:
            dist = Path(d) / "EdgeLab"
            (dist / "_internal").mkdir(parents=True)
            (dist / "_internal" / "edgelab_build.json").write_text('{"app_version": "0.2.0", "build_number": 70000}')
            m = bi.read_manifest(dist)
            defs = bi.iscc_defines(m, dist, Path(d) / "out")
            self.assertIn("/DAppVersion=0.2.0", defs)
            self.assertIn("/DFileVersion=0.2.0.4464", defs)                  # Windows version parts are 16-bit
            self.assertIn("/DOutputBase=MunyunLab-Setup-0.2.0-b70000", defs)
            with self.assertRaises(SystemExit):
                bi.read_manifest(Path(d) / "missing")

    def test_refuses_off_windows_or_without_a_build(self):
        bi = _load("build_installer")
        with tempfile.TemporaryDirectory() as d, self.assertRaises(SystemExit):
            bi.main(["--dist", str(Path(d) / "none")])                     # off Windows, or no EdgeLab.exe there


class TestInstallerReleaseIsIgnoredByTheUpdater(unittest.TestCase):
    def test_installer_main_release_never_counts_as_a_build(self):
        from edgelab.updater.core import UpdateError
        from edgelab.updater.source import GitHubReleaseSource
        api = "https://api.github.com/repos/ShiningRedstone/AI-Backtesting/releases?per_page=100"
        base = "https://github.com/ShiningRedstone/AI-Backtesting/releases/download"
        fixed = {"tag_name": "installer-main", "draft": False, "prerelease": True,
                 "published_at": "2026-10-02T00:00:00Z",
                 "assets": [{"name": "MunyunLab-Setup.exe", "browser_download_url": f"{base}/installer-main/x.exe",
                             "size": 1}]}
        b4 = {"tag_name": "build-main-4", "draft": False, "prerelease": True, "published_at": "2026-10-01T00:00:00Z",
              "assets": [{"name": MANIFEST_NAME, "browser_download_url": f"{base}/build-main-4/{MANIFEST_NAME}",
                          "size": 9},
                         {"name": f"EdgeLab-0.2.0-b4-{PLAT}.zip", "browser_download_url": f"{base}/build-main-4/a.zip",
                          "size": 100}]}
        routes = {api: [fixed, b4], f"{base}/build-main-4/{MANIFEST_NAME}": build_manifest_v2("0.2.0", 4, "main", 100,
                                                                                              "d" * 64)}
        r = GitHubReleaseSource(transport=FakeTransport(routes), channel="main").latest()
        self.assertEqual(r.manifest["build_number"], 4)
        routes[api] = [fixed]
        with self.assertRaises(UpdateError) as cm:
            GitHubReleaseSource(transport=FakeTransport(routes), channel="main").latest()
        self.assertEqual(cm.exception.code, "NO_RELEASE")


class TestWorkflow(unittest.TestCase):
    def test_workflow_builds_smokes_and_publishes_the_installer(self):
        wf = (REPO / ".github" / "workflows" / "windows-build.yml").read_text(encoding="utf-8")
        self.assertIn("python packaging/build_installer.py", wf)
        self.assertIn("/VERYSILENT", wf)
        self.assertIn("smoke_packaged.py --exe \"$app\\EdgeLab.exe\"", wf)
        self.assertIn("installer-main", wf)
        self.assertIn("if: github.ref == 'refs/heads/main'", wf)


if __name__ == "__main__":
    unittest.main()
