"""Research workspace selection (desktop): discovery, read-only validation, first run, selecting an
existing workspace, switching, persistence, invalid folders, no mutation of datasets/runs, API and
one browser flow (choose workspace -> datasets appear -> Strategy Lab populated)."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import yaml

from edgelab import runtime

REPO = Path(__file__).resolve().parents[1]
EMA = yaml.safe_load((REPO / "strategies" / "fixtures" / "ema_crossover.yaml").read_text())


def make_workspace(root: Path, *, runs: bool = True, seed: int = 5) -> dict:
    """A real research workspace: configs/, one imported synthetic dataset, one saved strategy, one run."""
    from edgelab.services import Services
    from tests.phase2_helpers import synthetic_canonical, write_generic_utc
    shutil.copytree(REPO / "configs", root / "configs")
    write_generic_utc(synthetic_canonical("2024-01-02", "2024-02-28", tf=5, seed=seed), root / "h.csv")
    svc = Services(root=root)
    did = svc.import_file(dict(file=str(root / "h.csv"), instrument="NAS100_HISTDATA", provider="HISTDATA",
                               asset_type="CFD", timeframe="5m", source_timezone="UTC", calendar="CME_EQUITY",
                               price_basis="bid", build_features=False))["dataset_id"]
    sid = svc.save_strategy(EMA)["strategy_id"]
    run_id = svc.backtest_strategy(EMA, did, True)["run_id"] if runs else None
    svc.store.close()
    return {"dataset_id": did, "strategy_id": sid, "run_id": run_id}


DERIVED = {"data/strategy_library/index.json"}      # derived library index (ADR-37), rebuilt from instances on listing


def tree_fingerprint(root: Path) -> dict:
    """Content hash of every file under data/ (store, datasets, runs, strategy instances, caches),
    except the derived library index, which the library (re)builds when strategies are listed."""
    out = {}
    for p in sorted((root / "data").rglob("*")):
        if p.is_file() and p.relative_to(root).as_posix() not in DERIVED:
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def db_counts(root: Path) -> tuple:
    con = sqlite3.connect(f"file:{(root / 'data' / 'edgelab.sqlite').as_posix()}?mode=ro", uri=True)
    try:
        return tuple(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("datasets", "runs"))
    finally:
        con.close()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.ws_a = cls.tmp / "AI-Backtesting"                  # "the existing development workspace"
        cls.ws_a.mkdir()
        cls.a = make_workspace(cls.ws_a)
        cls.ws_b = cls.tmp / "Other"
        cls.ws_b.mkdir()
        cls.b = make_workspace(cls.ws_b, runs=False, seed=9)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.settings = Path(tempfile.mkdtemp()) / "settings.json"
        self.env = mock.patch.dict(os.environ, {runtime.SETTINGS_ENV: str(self.settings)})
        self.env.start()
        os.environ.pop(runtime.DATA_ROOT_ENV, None)
        self.addCleanup(self.env.stop)
        self.addCleanup(shutil.rmtree, self.settings.parent, True)


class TestDiscoveryAndValidation(Base):
    def test_settings_location_and_resolution_precedence(self):
        self.assertEqual(runtime.settings_path({"APPDATA": r"C:\U\AppData\Roaming"}, "Windows"),
                         Path(r"C:\U\AppData\Roaming") / "EdgeLab" / "settings.json")
        self.assertEqual(runtime.settings_path({"HOME": "/h"}, "Linux"), Path("/h/.config/edgelab/settings.json"))
        self.assertEqual(runtime.resolve_workspace(), (None, "none"))              # first run: nothing chosen
        runtime.save_settings({"workspace": str(self.ws_a)})
        self.assertEqual(runtime.resolve_workspace(), (self.ws_a.resolve(), "saved selection"))
        self.assertEqual(runtime.resolve_workspace(self.ws_b)[1], "--data-root")   # explicit wins
        with mock.patch.dict(os.environ, {runtime.DATA_ROOT_ENV: str(self.ws_b)}):
            self.assertEqual(runtime.resolve_workspace(), (self.ws_b.resolve(), runtime.DATA_ROOT_ENV))
        self.assertNotIn(str(self.ws_a), str(self.settings.parent))               # settings live outside workspaces

    def test_inspect_existing_empty_and_invalid(self):
        before = tree_fingerprint(self.ws_a)
        a = runtime.inspect_workspace(self.ws_a)
        self.assertTrue(a["valid"] and a["has_store"] and a["writable"])
        self.assertEqual((a["datasets"], a["runs"], a["strategies"], a["store_backend"]), (1, 1, 1, "sqlite"))
        self.assertEqual(tree_fingerprint(self.ws_a), before)                     # inspection is read-only
        self.assertFalse((self.ws_a / "logs").exists())
        missing = runtime.inspect_workspace(self.tmp / "nope")
        self.assertFalse(missing["valid"])
        self.assertIn("does not exist", missing["problems"][0])
        plain = self.tmp / "plain"
        plain.mkdir(exist_ok=True)
        self.assertIn("no configs/", runtime.inspect_workspace(plain)["problems"][0])
        self.assertEqual(list(plain.iterdir()), [])                                # nothing created
        empty = runtime.create_workspace(self.tmp / "fresh")
        self.assertTrue(empty["valid"] and empty["empty"] and not empty["has_store"])
        with self.assertRaisesRegex(ValueError, "not empty"):
            runtime.create_workspace(self.ws_a)                                    # never re-initialises
        bad = self.tmp / "corrupt"
        shutil.copytree(REPO / "configs", bad / "configs")
        (bad / "data").mkdir()
        (bad / "data" / "edgelab.sqlite").write_bytes(b"not a database")
        self.assertFalse(runtime.inspect_workspace(bad)["valid"])
        demo = self.tmp / "demo_like"
        shutil.copytree(REPO / "configs", demo / "configs")
        (demo / "DEMO_WORKSPACE").write_text("x")
        self.assertFalse(runtime.inspect_workspace(demo)["valid"])


class TestWorkspaceHost(Base):
    def host(self):
        from werkzeug.test import Client
        from edgelab.desktop import InstanceLock
        from edgelab.web.app import create_app
        from edgelab.workspace_host import WorkspaceHost
        h = WorkspaceHost(lambda ws: create_app(ws), lambda ws: InstanceLock(ws / "logs" / "edgelab.lock"))
        self.addCleanup(h.close)
        return h, Client(h)

    def test_first_run_select_switch_persist_and_no_mutation(self):
        fa, fb = tree_fingerprint(self.ws_a), tree_fingerprint(self.ws_b)
        ca, cb = db_counts(self.ws_a), db_counts(self.ws_b)
        h, c = self.host()
        st = c.get("/api/workspace").get_json()
        self.assertIsNone(st["current"])
        self.assertTrue(st["switchable"])
        r = c.get("/api/datasets")
        self.assertEqual((r.status_code, r.get_json()["error"]["kind"]), (409, "no_workspace"))
        self.assertIn(b'id="root"', c.get("/").data)                              # the UI still loads
        r = c.post("/api/workspace/select", json={"path": str(self.tmp / "nope")})
        self.assertEqual(r.status_code, 422)
        self.assertIsNone(c.get("/api/workspace").get_json()["current"])          # nothing switched or created
        self.assertFalse(self.settings.exists())
        chk = c.post("/api/workspace/inspect", json={"path": str(self.ws_a)}).get_json()
        self.assertEqual((chk["datasets"], chk["runs"]), (1, 1))
        st = c.post("/api/workspace/select", json={"path": str(self.ws_a)}).get_json()
        self.assertEqual(st["current"]["path"], str(self.ws_a.resolve()))
        self.assertEqual([d["dataset_id"] for d in c.get("/api/datasets").get_json()], [self.a["dataset_id"]])
        self.assertEqual([s["strategy_id"] for s in c.get("/api/strategies").get_json()], [self.a["strategy_id"]])
        self.assertEqual([r["run_id"] for r in c.get("/api/results").get_json()], [self.a["run_id"]])
        self.assertEqual(json.loads(self.settings.read_text())["workspace"], str(self.ws_a.resolve()))   # persisted
        st = c.post("/api/workspace/select", json={"path": str(self.ws_b)}).get_json()
        self.assertEqual(st["current"]["path"], str(self.ws_b.resolve()))
        self.assertEqual([d["dataset_id"] for d in c.get("/api/datasets").get_json()], [self.b["dataset_id"]])
        self.assertEqual(c.get("/api/results").get_json(), [])                   # never merged
        self.assertEqual(json.loads(self.settings.read_text())["workspace"], str(self.ws_b.resolve()))
        from edgelab.desktop import InstanceLock
        la = InstanceLock(self.ws_a / "logs" / "edgelab.lock")
        self.assertTrue(la.acquire())                                             # the old lock was released
        la.release()
        h.close()
        self.assertEqual((tree_fingerprint(self.ws_a), tree_fingerprint(self.ws_b)), (fa, fb))
        self.assertIn("data/edgelab.sqlite", fa)                                  # the store itself is byte-identical
        self.assertEqual((db_counts(self.ws_a), db_counts(self.ws_b)), (ca, cb))

    def test_workspace_in_use_elsewhere_is_refused(self):
        from edgelab.desktop import InstanceLock
        other = InstanceLock(self.ws_a / "logs" / "edgelab.lock")
        (self.ws_a / "logs").mkdir(exist_ok=True)
        self.assertTrue(other.acquire())
        try:
            h, c = self.host()
            r = c.post("/api/workspace/select", json={"path": str(self.ws_a)})
            self.assertEqual(r.status_code, 409)
            self.assertIn("another EdgeLab", r.get_json()["error"]["message"])
            self.assertIsNone(c.get("/api/workspace").get_json()["current"])
        finally:
            other.release()

    def test_create_new_workspace_only_in_empty_folder(self):
        h, c = self.host()
        new = self.tmp / "created_by_gui"
        st = c.post("/api/workspace/create", json={"path": str(new)}).get_json()
        self.assertEqual(st["current"]["path"], str(new.resolve()))
        self.assertEqual(c.get("/api/datasets").get_json(), [])
        r = c.post("/api/workspace/create", json={"path": str(self.ws_b)})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(c.get("/api/workspace").get_json()["current"]["path"], str(new.resolve()))

    def test_dev_server_reports_its_fixed_workspace(self):
        from edgelab.web.app import create_app
        c = create_app(self.ws_b).test_client()
        st = c.get("/api/workspace").get_json()
        self.assertFalse(st["switchable"])
        self.assertEqual((st["current"]["path"], st["current"]["datasets"]), (str(self.ws_b.resolve()), 1))


def launch(env, *args):
    return subprocess.Popen([sys.executable, "-m", "edgelab.desktop", "--ui", "none", *args], cwd=REPO, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def wait_file(p: Path, proc, timeout=120):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if p.exists():
            try:
                return json.loads(p.read_text())
            except ValueError:
                pass
        if proc.poll() is not None:
            raise AssertionError(proc.stdout.read())
        time.sleep(0.2)
    raise AssertionError(f"{p} not written")


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


class TestLauncherPersistence(Base):
    def test_restart_reconnects_saved_workspace_and_first_run_has_none(self):
        env = {**os.environ, runtime.SETTINGS_ENV: str(self.settings)}
        env.pop(runtime.DATA_ROOT_ENV, None)
        p = launch(env)                                                           # first run: nothing selected
        try:
            info = wait_file(self.settings.parent / "logs" / "runtime.json", p)
            self.assertIsNone(info["data_root"])
            self.assertIsNone(get(info["url"] + "/api/workspace")["current"])
        finally:
            p.terminate()
            p.wait(30)
        runtime.save_settings({"workspace": str(self.ws_a)}, self.settings)       # as "Use this workspace" does
        p = launch(env)
        try:
            info = wait_file(self.ws_a / "logs" / "runtime.json", p)
            self.assertEqual((info["data_root"], info["workspace_source"]), (str(self.ws_a.resolve()), "saved selection"))
            self.assertEqual([d["dataset_id"] for d in get(info["url"] + "/api/datasets")], [self.a["dataset_id"]])
        finally:
            p.terminate()
            p.wait(30)
        runtime.save_settings({"workspace": str(self.tmp / "gone")}, self.settings)   # saved folder disappeared
        p = launch(env)
        try:
            info = wait_file(self.settings.parent / "logs" / "runtime.json", p)
            st = get(info["url"] + "/api/workspace")
            self.assertIsNone(st["current"])
            self.assertIn("not available", st["notice"])
            self.assertFalse((self.tmp / "gone").exists())                       # never silently created
        finally:
            p.terminate()
            p.wait(30)


class TestDemoLaunch(Base):
    def test_explicit_demo_launch_opens_its_demo_workspace(self):
        env = {**os.environ, runtime.SETTINGS_ENV: str(self.settings)}
        base = self.tmp / "demo_base"
        p = launch(env, "--demo", "--data-root", str(base))
        try:
            info = wait_file(base / "demo" / "logs" / "runtime.json", p, timeout=240)
            self.assertEqual(info["data_root"], str((base / "demo").resolve()))
            self.assertTrue(get(info["url"] + "/api/datasets"))                   # synthetic demo datasets
            self.assertFalse(self.settings.exists())                              # --demo is not remembered
        finally:
            p.terminate()
            p.wait(30)
        self.assertFalse(runtime.inspect_workspace(base / "demo")["valid"])       # the GUI still refuses demo folders


try:
    from playwright.sync_api import sync_playwright
except ImportError:                                                               # pragma: no cover
    sync_playwright = None


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class TestWorkspaceBrowserFlow(Base):
    def test_choose_workspace_then_datasets_and_strategy_lab(self):
        if Path("/opt/pw-browsers").is_dir():
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
        env = {**os.environ, runtime.SETTINGS_ENV: str(self.settings)}
        env.pop(runtime.DATA_ROOT_ENV, None)
        p = launch(env)
        try:
            info = wait_file(self.settings.parent / "logs" / "runtime.json", p)
            with sync_playwright() as pw:
                try:
                    b = pw.chromium.launch()
                except Exception as exc:                                          # pragma: no cover
                    self.skipTest(f"chromium unavailable: {exc}")
                pg = b.new_page()
                pg.set_default_timeout(30000)
                errors = []
                pg.on("pageerror", lambda e: errors.append(str(e)))
                t = lambda x: pg.locator(f"[data-testid='{x}']")                   # noqa: E731
                pg.goto(info["url"] + "/#/datasets")
                t("welcome-page").wait_for()
                t("ws-path").fill(str(self.ws_a))
                t("ws-inspect").click()
                t("ws-check").wait_for()
                self.assertEqual(t("ws-candidate-datasets").inner_text(), "1")
                self.assertEqual(t("ws-candidate-runs").inner_text(), "1")
                t("ws-select").click()
                table = t("datasets-table")
                table.wait_for()
                self.assertIn(self.a["dataset_id"], table.text_content())          # id under Technical details
                self.assertIn("AI-Backtesting", t("ws-chip").inner_text())
                pg.locator("[data-testid='nav-strategies']").click()
                t(f"row-{self.a['strategy_id']}").wait_for()
                pg.goto(info["url"] + "/#/prop")
                t("prop-run").wait_for()
                self.assertIn(self.a["run_id"], t("prop-run").inner_html())           # option value; the label is in words
                pg.goto(info["url"] + "/#/settings")
                self.assertIn(str(self.ws_a.resolve()), t("ws-connected").inner_text())
                b.close()
            self.assertEqual(errors, [])
            self.assertEqual(json.loads(self.settings.read_text())["workspace"], str(self.ws_a.resolve()))
        finally:
            p.terminate()
            p.wait(30)


if __name__ == "__main__":
    unittest.main()
