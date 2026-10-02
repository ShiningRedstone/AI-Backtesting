"""ADR-89: the shipped configs keep the old HistData entries (inside histdata-legacy blocks) so a workspace that IS the
source clone keeps the settings fingerprint its research protocol is bound to; new workspaces get the HistData-free
configs of ADR-86; the restore of a protocol's recorded settings is verified before anything is written.

The two fingerprints below are KNOWN ANSWERS. If a change to configs/ breaks the first one, every workspace that is the
source clone (the user's) loses its active research protocol (PROTOCOL_CONFIG_CHANGED / PREFLIGHT_FAILED)."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from edgelab.core.config import config_hash, load_config
from edgelab.runtime import copy_default_configs, init_workspace, legacy_hidden, strip_legacy_blocks

REPO = Path(__file__).resolve().parents[1]
PRE_ADR86 = "96702274dd66a6faf9980fb750f9147ed7a61c57f67bf192aa605f9be2eca2c0"     # configs/ before the HistData removal
ADR86_NEW = "43368445f350a423479c66e44c6fcfe012915573d41c63d7eed1cb1992c4c112"     # configs/ as ADR-86 shipped them


class TestShippedConfigs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_source_clone_keeps_the_pre_adr86_fingerprint(self):
        self.assertEqual(config_hash(load_config(REPO / "configs", environ={})), PRE_ADR86)

    def test_new_workspace_gets_the_histdata_free_configs(self):
        out = init_workspace(self.tmp / "ws", defaults=REPO / "configs")
        self.assertTrue(out["configs_created"])
        cfg = load_config(self.tmp / "ws" / "configs", environ={})
        self.assertEqual(config_hash(cfg), ADR86_NEW)
        for p in (self.tmp / "ws" / "configs").glob("*.yaml"):
            text = p.read_text()
            self.assertNotIn("histdata-legacy", text)
            self.assertNotIn("#| ", text)
        self.assertFalse([k for k in cfg["instruments"] if "HISTDATA" in k])
        self.assertEqual(cfg["source_exclusions"], {})

    def test_existing_workspace_configs_are_never_rewritten(self):
        ws = self.tmp / "ws"
        shutil.copytree(REPO / "configs", ws / "configs")
        init_workspace(ws, defaults=REPO / "configs")
        self.assertEqual(config_hash(load_config(ws / "configs", environ={})), PRE_ADR86)

    def test_strip_markers(self):
        text = "a: 1\n# >>> histdata-legacy x\nb: 2\n# <<< histdata-legacy\n#| b: 3\n  #| c: 4\n"
        self.assertEqual(strip_legacy_blocks(text), "a: 1\nb: 3\n  c: 4\n")
        for bad in ("# >>> histdata-legacy\nb: 2\n", "# <<< histdata-legacy\n",
                    "# >>> histdata-legacy\n# >>> histdata-legacy\n# <<< histdata-legacy\n"):
            with self.assertRaises(ValueError):
                strip_legacy_blocks(bad)

    def test_hidden_from_the_app(self):
        from edgelab.services import Services
        from edgelab.web.app import create_app
        ws = self.tmp / "clone"
        shutil.copytree(REPO / "configs", ws / "configs")                 # a workspace with the legacy entries
        app = create_app(ws)
        svc = app.config["EDGELAB"]["services"]
        self.addCleanup(svc.store.close)
        self.assertIn("NAS100_HISTDATA", svc.cfg["instruments"])          # still part of the settings ...
        body = app.test_client().get("/api/config").get_json()
        self.assertFalse([k for k in body["instruments"] if legacy_hidden(k)])   # ... never listed
        self.assertFalse([k for k in body["cost_profiles"] if legacy_hidden(k)])
        opts = Services.research_config_options(svc)
        self.assertFalse([i for i in opts["instruments"] if legacy_hidden(i["symbol"])])
        self.assertFalse([c for c in opts["calendars"] if legacy_hidden(c)])


class TestRestoreRefusals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.services import Services
        from edgelab.web.demo import create_demo_workspace
        cls.tmp = Path(tempfile.mkdtemp())
        create_demo_workspace(cls.tmp / "demo", REPO)
        cls.svc = Services(root=cls.tmp / "demo")
        s = cls.svc.library.list()[0]
        d = next(x["dataset_id"] for x in cls.svc.backtest_readiness(s["strategy_id"])["datasets"] if x["runnable"])
        cls.svc.backtest_strategy(s["strategy_id"], d, record=True)
        cls.h = cls.svc._config_hash()                                    # the "protocol's" settings: a run carries them

    @classmethod
    def tearDownClass(cls):
        cls.svc.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _edit(self):
        f = self.tmp / "demo" / "configs" / "backtest.yaml"
        orig = f.read_text()
        f.write_text(orig + "\nzz_extra_setting: {x: 1}\n")
        self.addCleanup(f.write_text, orig)
        return f

    def test_detail_and_refusals(self):
        from edgelab.research import config_restore as CR
        from edgelab.services import Services
        f = self._edit()
        svc = Services(root=self.tmp / "demo")
        self.addCleanup(svc.store.close)
        det = CR.mismatch_detail(svc, self.h)
        self.assertEqual([(d["path"], d["change"]) for d in det["differences"]], [("zz_extra_setting", "added")])
        self.assertTrue(det["restorable"])
        self.assertIsNone(CR.protocol_settings(svc, "0" * 64))           # nothing recorded under another hash
        with self.assertRaises(CR.ConfigRestoreError) as cm:
            CR.restore(svc, "0" * 64)
        self.assertEqual(cm.exception.code, "NO_RECORDED_SETTINGS")
        with mock.patch.dict(os.environ, {"EDGELAB__BACKTEST__X": "1"}):
            self.assertFalse(CR.mismatch_detail(svc, self.h)["restorable"])
            with self.assertRaises(CR.ConfigRestoreError) as cm:
                CR.restore(svc, self.h)
            self.assertEqual(cm.exception.code, "ENV_OVERRIDES")
        edited = f.read_text()
        with mock.patch.object(CR, "load_config", return_value={"something": "else"}):
            with self.assertRaises(CR.ConfigRestoreError) as cm:
                CR.restore(svc, self.h)
        self.assertEqual(cm.exception.code, "HASH_MISMATCH")
        self.assertEqual(f.read_text(), edited)                           # nothing written on a refusal
        self.assertFalse(list((self.tmp / "demo").glob("configs.backup-*")))
        out = CR.restore(svc, self.h)
        self.assertEqual(out["files"], ["backtest.yaml"])                 # only the differing file is rewritten
        self.assertEqual(config_hash(load_config(self.tmp / "demo" / "configs")), self.h)
        self.assertIn("zz_extra_setting", (Path(out["backup"]) / "backtest.yaml").read_text())
        fresh = Services(root=self.tmp / "demo")
        self.addCleanup(fresh.store.close)
        self.assertEqual(CR.restore(fresh, self.h), {"restored": False, "already_identical": True, "config_hash": self.h})


if __name__ == "__main__":
    unittest.main()
