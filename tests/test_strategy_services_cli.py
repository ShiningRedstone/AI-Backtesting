"""Phase 3: Strategy Lab service contracts (strict JSON) and the `strategy` CLI."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from edgelab.cli import main
from edgelab.engine.costs import CostConfigError
from edgelab.services import Services
from tests.phase2_helpers import synthetic_canonical, write_generic_utc

REPO = Path(__file__).resolve().parents[1]
FX = REPO / "strategies" / "fixtures"


def strict_json(obj):
    return json.dumps(obj, allow_nan=False)


def cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(args))
    return rc, out.getvalue(), err.getvalue()


class TestStrategyServices(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.root / "configs")
        df = synthetic_canonical("2024-03-04", "2024-03-22", tf=5, seed=9)
        write_generic_utc(df, cls.root / "fut.csv")
        write_generic_utc(df, cls.root / "cfd.csv")
        cls.svc = Services(root=cls.root)
        cls.fut = cls.svc.import_file(dict(file=str(cls.root / "fut.csv"), instrument="NQ", provider="SYNTHF",
                                           asset_type="FUTURE", timeframe="5m", source_timezone="UTC"))["dataset_id"]
        cls.cfd = cls.svc.import_file(dict(file=str(cls.root / "cfd.csv"), instrument="NAS100_CFD", provider="SYNTHC",
                                           asset_type="CFD", timeframe="5m", source_timezone="UTC"))["dataset_id"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_every_strategy_contract_is_strict_json(self):
        s = self.svc
        saved = s.save_strategy(str(FX / "ema_crossover.yaml"))
        sid = saved["strategy_id"]
        payloads = [s.validate_strategy(str(FX / "opening_range_breakout.yaml")),
                    s.validate_strategy({"name": "x"}), s.compile_strategy(str(FX / "atr_breakout.yaml")),
                    s.preview_strategy(str(FX / "fvg_entry.yaml")), saved, s.list_strategies(),
                    s.strategy_families(), s.strategy_lineage(sid), s.load_strategy(sid),
                    s.duplicate_strategy(sid, "copy"), s.proposal_menu(5),
                    s.generate_variations(str(FX / "opening_range_breakout.yaml"), str(FX / "orb_variations.yaml"),
                                          save=False),
                    s.ingest_proposals(str(FX / "proposals_example.yaml"), save=False),
                    s.backtest_strategy(sid, self.fut)]
        for p in payloads:
            strict_json(p)

    def test_validate_contract(self):
        ok = self.svc.validate_strategy(str(FX / "rsi_threshold.yaml"))
        self.assertTrue(ok["valid"])
        self.assertTrue(ok["identity"]["strategy_id"].startswith("STR_"))
        bad = self.svc.validate_strategy({"dsl_version": 1, "name": "b", "timeframe": "5m",
                                          "entry": {"direction": "long", "long": {"left": {"bar": "close"},
                                                                                  "op": ">", "right": 1}},
                                          "exit": {"stop": {"type": "points", "points": 5}, "trailing_stop": {}}})
        self.assertFalse(bad["valid"])
        self.assertEqual(bad["errors"][0]["path"], "exit.trailing_stop")

    def test_backtest_through_the_service(self):
        r = self.svc.backtest_strategy(str(FX / "ema_crossover.yaml"), self.fut)
        self.assertEqual(r["dataset"]["asset_type"], "FUTURE")
        self.assertGreater(r["metrics"]["trade_count"], 0)
        self.assertIn("sample_label", r["metrics"])
        self.assertIn("not a conclusion", r["note"])
        with self.assertRaises(CostConfigError):                     # CFD costs are not invented
            self.svc.backtest_strategy(str(FX / "ema_crossover.yaml"), self.cfd)

    def test_edit_duplicate_and_stored_ids(self):
        base = self.svc.save_strategy(str(FX / "rsi_threshold.yaml"))
        draft = self.svc.duplicate_strategy(base["strategy_id"], "rsi_copy")["draft"]
        self.assertEqual(draft["name"], "rsi_copy")
        self.assertEqual(self.svc.validate_strategy(draft)["identity"]["strategy_id"], base["strategy_id"])
        draft["exit"]["time_stop_bars"] = 12
        edited = self.svc.edit_strategy(base["strategy_id"], draft)
        self.assertNotEqual(edited["strategy_id"], base["strategy_id"])
        lin = self.svc.strategy_lineage(edited["strategy_id"])
        self.assertEqual(lin["records"][0]["generation_method"], "manual_edit")
        self.assertEqual(lin["ancestry"][-1]["strategy_id"], base["strategy_id"])
        self.assertTrue(self.svc.validate_strategy(edited["strategy_id"])["valid"])   # stored id as source
        again = self.svc.save_strategy(str(FX / "rsi_threshold.yaml"))
        self.assertFalse(again["created"])

    def test_generation_services_save_lineage(self):
        v = self.svc.generate_variations(str(FX / "opening_range_breakout.yaml"), str(FX / "orb_variations.yaml"))
        self.assertEqual(v["generated"], 35)
        fam = self.svc.list_strategies("ny_opening_range_breakout")
        self.assertEqual(len(fam), 36)
        p = self.svc.ingest_proposals(str(FX / "proposals_example.yaml"))
        self.assertEqual((p["n_accepted"], p["n_rejected"]), (3, 5))
        methods = {x["generation_method"] for x in self.svc.list_strategies()}
        self.assertTrue({"user", "mode_a_variation", "mode_b_proposal"} <= methods)


class TestStrategyCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.root / "configs")
        cls.r = ["--root", str(cls.root)]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_validate_exit_codes(self):
        rc, out, _ = cli(*self.r, "strategy", "validate", str(FX / "opening_range_breakout.yaml"))
        self.assertEqual(rc, 0)
        self.assertIn("strategy_id: STR_", out)
        bad = self.root / "bad.yaml"
        bad.write_text("dsl_version: 1\nname: b\ntimeframe: 5m\nentry: {direction: long, long: "
                       "{left: {feature: ema_fast, output: ema}, op: '>', right: 1}}\nexit: {stop: {type: points, points: 5}}\n")
        rc, out, _ = cli(*self.r, "strategy", "validate", str(bad))
        self.assertEqual(rc, 2)
        self.assertIn("entry.long.left.feature", out)

    def test_compile_error_and_argument_errors(self):
        bad = self.root / "bad2.yaml"
        bad.write_text("name: b\n")
        rc, _, err = cli(*self.r, "strategy", "compile", str(bad))
        self.assertEqual(rc, 2)
        self.assertIn("validation failed", err)
        with self.assertRaises(SystemExit):
            cli(*self.r, "strategy", "variations", str(FX / "opening_range_breakout.yaml"))

    def test_variations_proposals_lineage_menu(self):
        rc, out, _ = cli(*self.r, "strategy", "variations", str(FX / "opening_range_breakout.yaml"),
                         str(FX / "orb_variations.yaml"))
        self.assertEqual(rc, 0)
        self.assertIn("48 combinations -> 35 variants", out)
        child = out.splitlines()[1].split()[0]
        rc, out, _ = cli(*self.r, "--json", "strategy", "lineage", child)
        self.assertEqual(json.loads(out)["records"][0]["generation_method"], "mode_a_variation")
        rc, out, _ = cli(*self.r, "--json", "strategy", "proposals", str(FX / "proposals_example.yaml"), "--no-save")
        self.assertEqual(json.loads(out)["n_accepted"], 3)
        rc, out, _ = cli(*self.r, "strategy", "menu")
        self.assertIn("time_of_day", {f["id"] for f in json.loads(out)["features"]})
        s = self.root / "tight.yaml"
        s.write_text((FX / "orb_variations.yaml").read_text().replace("max_variants: 100", "max_variants: 10"))
        rc, _, err = cli(*self.r, "strategy", "variations", str(FX / "opening_range_breakout.yaml"), str(s))
        self.assertEqual(rc, 2)
        self.assertIn("max_variants", err)


if __name__ == "__main__":
    unittest.main()
