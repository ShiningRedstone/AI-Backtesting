"""ADR-64: the prop lifecycle runs automatically with EVERY backtest, is stored with the run, never selects or ranks anything,
keeps the account out of strategy identity, and stays on the research-account / MNQ / proxy-dataset model.
SYNTHETIC Dukascopy-shaped data only."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from edgelab.prop import lifecycle as L
from edgelab.prop import profiles as P
from edgelab.prop import service as PS
from edgelab.services import Services
from edgelab.strategy import factory as FX
from edgelab.strategy import factory_space as S
from tests.dukascopy_fixture import write_fixture

REPO = Path(__file__).resolve().parents[1]
FXD = REPO / "strategies" / "fixtures"


def fixture(name):
    import yaml
    return yaml.safe_load((FXD / name).read_text())


class TestProp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.root / "configs")
        csv = cls.root / "combined.csv"
        write_fixture(csv, end="2024-04-27")
        f = pd.read_csv(csv, dtype=str)
        s = 1.0 + 0.25 * (np.arange(len(f)) % 7)
        for k, extra in (("open", 0.0), ("high", 0.5), ("low", 0.0), ("close", 0.0)):
            f[f"ask_{k}"] = (f[k].astype(float) + s + extra).map(lambda x: f"{x:.3f}")
        f.to_csv(csv, index=False)
        cls.svc = Services(root=cls.root)
        cls.did = cls.svc.import_file(dict(
            file=str(csv), profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY", provider="DUKASCOPY", asset_type="CFD",
            symbol="USATECH.IDX/USD", price_basis="bid", timeframe="1m", derive_timeframes=["5m"], build_features=False,
            bid_close_column="close", ask_close_column="ask_close", ask_open_column="ask_open", ask_high_column="ask_high",
            ask_low_column="ask_low", dataset_name="DUKA_SYN"))["derived"][0]

    @classmethod
    def tearDownClass(cls):
        cls.svc.store.close()
        shutil.rmtree(cls.root, ignore_errors=True)

    def mnq_strategy(self, sizing):
        d = fixture("ema_crossover.yaml")
        d["sizing"] = sizing
        return d

    MNQ_RISK = {"mode": "risk", "risk_usd": 250, "max_quantity": S.RISK_QUANTITY_CAP, "contract": "MNQ"}

    def test_every_recorded_backtest_carries_base_and_four_profile_results(self):
        out = self.svc.backtest_strategy(self.mnq_strategy(self.MNQ_RISK), self.did, record=True)
        rec, trades = self.svc.store.load_run(out["run_id"])
        prop = rec["prop"]
        self.assertEqual(prop["research_account"]["starting_equity"], 50_000.0)
        self.assertEqual(prop["execution_contract"], "MNQ")
        b = prop["base"]
        self.assertEqual(b["trade_count"], len(trades))                             # the FULL research period
        self.assertAlmostEqual(b["net_pnl"], float(trades["net_usd"].sum()), places=4)
        self.assertAlmostEqual(b["total_costs"], float(trades["cost_usd"].sum()), places=4)
        self.assertAlmostEqual(b["gross_profit"] - b["gross_loss"], b["net_pnl"], places=4)
        for k in ("expectancy", "profit_factor", "max_drawdown", "max_contracts", "average_contracts", "quantity"):
            self.assertIn(k, b)
        self.assertEqual(sum(b["quantity"]["distribution"].values()), len(trades))
        self.assertEqual([p["profile"]["profile_id"] for p in prop["profiles"]],
                         ["LUCID_LUCIDFLEX_50K", "TRADEIFY_GROWTH_50K", "TRADEIFY_SELECT_DAILY_50K", "TRADEIFY_SELECT_FLEX_50K"])
        for p in prop["profiles"]:
            self.assertIn(p["status"], ("PASS", "FAIL", "INCOMPATIBLE"))
            self.assertEqual(p["rule_basis_state"], "RULE_ASSUMED")
            s = p["summary"]
            self.assertEqual((s["account_size"], s["contract"], s["rule_profile_version"]), (50000, "MNQ", 4))
            self.assertEqual(s["rule_profile_id"], p["profile"]["profile_id"])
            self.assertGreater(s["verified_rule_count"], 0)
            self.assertGreater(s["assumed_rule_count"], 0)
            self.assertIn("UNDER DEFAULT ASSUMED RULES", p["final_status"])
            self.assertTrue(p["profile"]["profile_hash"])
            self.assertTrue(p["sizing_account_matches_profile"])
        blob = json.dumps(prop)
        for bad in ('"score"', '"rank"', '"grade"', "IN_PROGRESS", "PROVIDER PASS"):
            self.assertNotIn(bad, blob)
        self.assertIn("never select, rank or alter", prop["note"])
        self.assertEqual(out["prop"]["profiles"], prop["profiles"])                   # shown with the backtest result

    def test_dataset_unit_trades_are_not_judged_against_micro_limits(self):
        out = self.svc.backtest_strategy(fixture("ema_crossover.yaml"), self.did, record=True)
        rec, _ = self.svc.store.load_run(out["run_id"])
        for p in rec["prop"]["profiles"]:
            self.assertEqual(p["status"], "NOT_APPLICABLE")
            self.assertEqual(p["summary"]["evaluation_status"], "NOT_APPLICABLE")
        self.assertIsNone(rec["prop"]["execution_contract"])

    def test_a_non_recorded_run_and_the_parallel_parents_record_step_also_compute_it(self):
        cell = self.svc._run_cell(self.mnq_strategy(self.MNQ_RISK), self.did, record=False)
        self.assertEqual(len(cell["prop"]["profiles"]), 4)
        rid = self.svc._record_cell(cell["result"], cell["metrics"], True)               # no prop passed: computed from the trades
        rec, _ = self.svc.store.load_run(rid)
        self.assertEqual(rec["prop"]["profiles"], cell["prop"]["profiles"])

    def test_mnq_strategy_on_the_proxy_series_trades_whole_contracts_within_the_cap(self):
        out = self.svc.backtest_strategy(self.mnq_strategy(self.MNQ_RISK), self.did, record=True)
        rec, trades = self.svc.store.load_run(out["run_id"])
        self.assertGreater(len(trades), 0)
        self.assertTrue((trades["contracts"].astype(float) % 1 == 0).all())
        self.assertLessEqual(float(trades["contracts"].max()), S.RISK_QUANTITY_CAP)
        a = rec["assumptions"]["execution_contract"]
        self.assertEqual((a["contract"], a["data_instrument"]), ("MNQ", "NQ_DUKASCOPY"))
        self.assertEqual(rec["dataset"]["instrument"], "NQ_DUKASCOPY")                  # the dataset is never relabelled MNQ
        self.assertEqual(rec["prop"]["base"]["max_contracts"], float(trades["contracts"].max()))
        eq = self.svc.backtest_strategy(self.mnq_strategy({"mode": "equity_risk", "risk_pct": 0.5,
                                                           "max_quantity": S.RISK_QUANTITY_CAP, "contract": "MNQ"}),
                                        self.did, record=True)
        rec2, t2 = self.svc.store.load_run(eq["run_id"])
        self.assertEqual(rec2["assumptions"]["equity_sizing"]["starting_equity"], 50_000.0)
        self.assertEqual(float(t2["equity_before"].iloc[0]), 50_000.0)

    def test_prop_audit_is_deterministic_and_never_changes_the_trades(self):
        cell = self.svc._run_cell(self.mnq_strategy({"mode": "fixed", "quantity": 35, "contract": "MNQ"}), self.did,
                                  record=False)
        trades = cell["result"].trades
        keep = trades.copy()
        a = PS.outcomes(self.root, trades, assumptions=cell["result"].assumptions)
        b = PS.outcomes(self.root, trades.copy(), assumptions=cell["result"].assumptions)
        self.assertEqual(a, b)
        pd.testing.assert_frame_equal(trades, keep)
        self.assertEqual(a["base"]["trade_count"], len(trades))
        self.assertTrue((trades["contracts"] == 35).all())                             # never resized to a prop limit

    def test_changing_an_assumed_rule_changes_only_the_prop_result(self):
        sz = {"mode": "fixed", "quantity": 35, "contract": "MNQ"}
        base_cell = self.svc._run_cell(self.mnq_strategy(sz), self.did, record=False)
        defaults = P.load_default_profiles(self.root)
        changed = [P.customize(p, {"evaluation.drawdown.measurement": "closed_balance",
                                   "evaluation.drawdown.lock_enabled": False}, "test: assumed rules changed")
                   for p in defaults]
        with mock.patch("edgelab.prop.service.default_profiles", return_value=changed):
            cell = self.svc._run_cell(self.mnq_strategy(sz), self.did, record=False)
        self.assertEqual(cell["result"].trades_hash, base_cell["result"].trades_hash)      # strategy result unchanged
        self.assertEqual(cell["metrics"], base_cell["metrics"])
        self.assertEqual(cell["prop"]["base"], base_cell["prop"]["base"])
        for x, y in zip(base_cell["prop"]["profiles"], cell["prop"]["profiles"]):
            self.assertNotEqual(x["summary"], y["summary"])                           # the prop audit changed
            self.assertEqual(y["summary"]["custom_rule_count"], 2)
            self.assertEqual(y["summary"]["rule_profile_version"], 5)

    def test_service_cli_and_http_surfaces(self):
        st = self.svc.prop_profiles()
        self.assertEqual(st["registry_problems"], [])
        self.assertEqual(len(st["profiles"]), 4)
        self.assertEqual({p["verification_status"] for p in st["profiles"]}, {"rulebook"})
        self.assertTrue(all(p["assumed_rule_count"] > 0 and p["assumed_rules"] for p in st["profiles"]))
        rid = self.svc.backtest_strategy(self.mnq_strategy(self.MNQ_RISK), self.did, record=True)["run_id"]
        lc = self.svc.prop_lifecycle(rid)
        self.assertEqual(len(lc["profiles"]), 4)
        self.assertTrue({p["status"] for p in lc["profiles"]} <= {"PASS", "FAIL", "INCOMPATIBLE"})
        self.assertIn("days", lc["profiles"][0]["evaluation"])                          # full detail on demand
        from edgelab.web.app import create_app
        c = create_app(self.root).test_client()
        self.assertEqual(c.get("/api/prop/profiles").status_code, 200)
        self.assertEqual(c.get(f"/api/prop/lifecycle/{rid}").status_code, 200)
        self.assertEqual(c.get("/api/prop/lifecycle/bad").status_code, 400)

    def test_the_account_and_provider_never_enter_strategy_identity_or_the_universe(self):
        res = FX.generate(seed=77, quotas={f.fid: 3 for f in S.FAMILIES})
        for r in res.strategies:
            blob = json.dumps(r["definition"]).lower()
            for bad in ("50000", "50k", "lucid", "tradeify", "prop"):
                self.assertNotIn(bad, blob, (r["strategy_id"], bad))
            self.assertNotIn("starting_equity", json.dumps(r["definition"]))
        # generation is independent of any prop result: same seed, same universe
        again = FX.generate(seed=77, quotas={f.fid: 3 for f in S.FAMILIES})
        self.assertEqual(res.manifest_id, again.manifest_id)

    def test_prop_layer_never_touches_the_protocol_or_ledgers(self):
        import inspect
        src = inspect.getsource(PS) + inspect.getsource(L) + inspect.getsource(P)
        for banned in ("evaluate_holdout", "trial_key", "protocol", "holdout"):
            self.assertNotIn(banned, src)


if __name__ == "__main__":
    unittest.main()
