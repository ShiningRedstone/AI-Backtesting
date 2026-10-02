"""ADR-68: frozen-manifest discovery campaign. One strategy = one cell = one counted trial, through the protocol-gated search.

Pure checks use 10,000 synthetic manifest rows (no workspace). The end-to-end checks use a temporary SYNTHETIC workspace
(Dukascopy-shaped fixture, 1m + 4 derived timeframes) and a small real factory manifest (seed 4, one strategy per family:
all five timeframes, 9 with MTF). Nothing touches a real protocol, trial or holdout."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from edgelab.core.identity import hash_obj
from edgelab.research import campaign as C
from edgelab.services import Services
from edgelab.strategy import factory as FX
from edgelab.strategy import factory_space as S
from tests.dukascopy_fixture import write_fixture
from tests.test_research_protocol import DISC, HOLD

REPO = Path(__file__).resolve().parents[1]
TFS = ("1m", "5m", "15m", "30m", "60m")


def fake_rows(n=10_000):
    rows = []
    for i in range(n):
        tf = TFS[i % 5]
        d = {"timeframe": tf, "sizing": {"mode": "fixed", "quantity": 1 + i % 10, "contract": "MNQ"},
             "entry": {"long": {"left": {"feature": "ema", "params": {"period": 5}, "timeframe": "60m"}}} if i % 3 == 0 else {}}
        rows.append({"strategy_id": f"STR_{i:012X}", "logic_hash": hash_obj(["l", i]), "definition_hash": hash_obj(["d", i]),
                     "family_id": f"fam{i % 30}", "candidate_seq": i, "definition": d})
    return rows


def header_for(rows):
    from collections import Counter
    idn = {"strategies_sha256": hash_obj([[r["strategy_id"], r["definition_hash"], r["family_id"], r["candidate_seq"]]
                                          for r in rows]), "quotas": dict(Counter(r["family_id"] for r in rows))}
    return {"manifest_id": "FM_" + hash_obj(idn)[:16].upper(), "identity": idn, "counts": {"valid_unique": len(rows)}}


BY_TF = {tf: {"dataset_id": f"DS_{tf}"} for tf in TFS}


class TestPureInvariants(unittest.TestCase):
    def test_10000_rows_give_10000_cells_one_per_strategy_regardless_of_timeframe_or_mtf(self):
        rows = fake_rows()
        cells = C.assign_cells(rows, BY_TF)
        self.assertEqual(len(cells), 10_000)                                       # five timeframes, MTF: no multiplication
        self.assertEqual(len({c["strategy_id"] for c in cells}), 10_000)
        self.assertEqual({c["dataset_id"] for c in cells}, {f"DS_{tf}" for tf in TFS})
        self.assertTrue(all(c["dataset_id"] == f"DS_{c['timeframe']}" for c in cells))
        self.assertEqual(sum(1 for c in cells if c["mtf"]), 3_334)                 # MTF strategies: still one cell each
        h = header_for(rows)
        self.assertEqual(C.manifest_problems(h["manifest_id"], h, rows), [])
        self.assertEqual([p for r in rows[:50] for p in C.row_problems(r, set(TFS), 40)], [])

    def test_duplicates_and_manifest_hash_mismatch_are_refused(self):
        rows = fake_rows(200)
        h = header_for(rows)
        dup_id = copy.deepcopy(rows)
        dup_id[7]["strategy_id"] = dup_id[3]["strategy_id"]
        self.assertTrue(any("duplicate strategy ids" in p for p in C.manifest_problems(h["manifest_id"], h, dup_id)))
        dup_logic = copy.deepcopy(rows)
        dup_logic[9]["logic_hash"] = dup_logic[2]["logic_hash"]
        self.assertTrue(any("duplicate logic hashes" in p for p in C.manifest_problems(h["manifest_id"], h, dup_logic)))
        changed = copy.deepcopy(rows)
        changed[0]["definition_hash"] = "x"
        self.assertTrue(any("strategies_sha256" in p for p in C.manifest_problems(h["manifest_id"], h, changed)))
        self.assertTrue(any("does not hash" in p for p in C.manifest_problems("FM_0000000000000000", h, rows)))

    def test_missing_timeframe_and_mtf_dependency_are_unresolved(self):
        rows = fake_rows(10)
        no30 = {k: v for k, v in BY_TF.items() if k != "30m"}
        with self.assertRaises(C.CampaignError):
            C.assign_cells(rows, no30)
        self.assertIn("no dataset resolved for timeframe 30m", C.row_problems(rows[3], set(no30), 40)[0])
        bad = copy.deepcopy(rows[0])
        bad["definition"]["timeframe"] = "15m"
        bad["definition"]["entry"]["long"]["left"]["timeframe"] = "20m"                 # 20m cannot come from 15m bars
        self.assertTrue(any("MTF dependency 20m" in p for p in C.row_problems(bad, set(TFS), 40)))
        frac = copy.deepcopy(rows[1])
        frac["definition"]["sizing"]["quantity"] = 2.5
        self.assertTrue(any("whole number" in p for p in C.row_problems(frac, set(TFS), 40)))
        cfd = copy.deepcopy(rows[1])
        cfd["definition"]["sizing"].pop("contract")
        self.assertTrue(any("does not bind MNQ" in p for p in C.row_problems(cfd, set(TFS), 40)))

    def test_capacity_max_cells_budget_and_workers(self):
        self.assertEqual(C.check_capacity(10_000, 10_000, 10_000, 1), [])
        self.assertTrue(C.check_capacity(10_000, 10_001, 10_000, 1))                 # max_cells 10,001 refused
        self.assertTrue(C.check_capacity(10_000, 10_000, 9_999, 1))                  # budget 9,999 refused
        self.assertTrue(C.check_capacity(10_000, 10_000, 10_000, 2))                 # workers 2 refused


class TestCampaignWorkspace(unittest.TestCase):
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
        svc = Services(root=cls.root)
        imp = svc.import_file(dict(
            file=str(csv), profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY", provider="DUKASCOPY", asset_type="CFD",
            symbol="USATECH.IDX/USD", price_basis="bid", timeframe="1m", derive_timeframes=["5m", "15m", "30m", "60m"],
            build_features=False, bid_close_column="close", ask_close_column="ask_close", ask_open_column="ask_open",
            ask_high_column="ask_high", ask_low_column="ask_low", dataset_name="DUKA_SYN"))
        cls.src = imp["derived"][0]                                                   # the 5m child: root resolution
        res = FX.generate(seed=4, quotas={f.fid: 1 for f in S.FAMILIES})
        cls.n = len(res.strategies)
        cls.mid = res.manifest_id
        FX.write_manifest(res, svc.factory_dir / cls.mid)
        cls.rows = res.strategies
        p = svc.create_protocol(cls.src, DISC, HOLD, name="campaign", exposure_statement="none",
                                trial_budget=cls.n)                                   # budget exactly = universe
        cls.pid = p["protocol_id"]
        cls.hold_open = pd.Timestamp(p["material"]["windows"]["holdout"]["boundary_open"])
        svc.store.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def svc(self):
        s = Services(root=self.root)
        self.addCleanup(s.store.close)
        return s

    def test_end_to_end(self):
        s = self.svc()
        runs0 = len(s.store.list_runs()) if hasattr(s.store, "list_runs") else None
        # ---- freeze (materialize) and preflight: no trial, no run
        fr = s.campaign_freeze(self.mid)
        cid = fr["campaign_id"]
        self.assertEqual((fr["n_strategies"], fr["materialized"]["created"]), (self.n, self.n))
        self.assertEqual(sorted(fr["datasets"]), sorted(TFS))
        self.assertEqual(s.campaign_freeze(self.mid)["campaign_id"], cid)              # frozen: same id, nothing rewritten
        self.assertEqual(s.campaign_freeze(self.mid)["materialized"]["created"], 0)
        rep = s.campaign_check(cid)
        self.assertTrue(rep["ready"], [c for c in rep["checks"] if not c["ok"]])
        self.assertEqual(s.store.count_trials(self.pid), 0)                           # preflight consumed nothing
        self.assertEqual(s.store.list_trial_events(self.pid), [])
        if runs0 is not None:
            self.assertEqual(len(s.store.list_runs()), runs0)
        spec = C.load(s, cid)
        self.assertEqual((spec["execution"]["workers"], spec["execution"]["max_cells"], spec["execution"]["holdout"]),
                         (1, self.n, {"enabled": False}))
        self.assertLess(pd.Timestamp(spec["search"]["spec"]["period"]["end"]), self.hold_open)   # discovery only
        self.assertEqual(spec["protocol"]["protocol_id"], self.pid)
        # ---- identity preserved
        for r in self.rows:
            doc = s.library.load(r["strategy_id"])
            self.assertEqual((doc["strategy_id"], doc["logic_hash"], doc["definition_hash"]),
                             (r["strategy_id"], r["logic_hash"], r["definition_hash"]))
            lin = [x for x in doc["lineage"] if x["generation_batch_id"] == self.mid][0]
            gp = lin["generation_parameters"]
            self.assertEqual((gp["manifest_id"], gp["campaign_id"], gp["family_spec_sha256"], lin["family_id"]),
                             (self.mid, cid, r["lineage"]["family_spec_sha256"], r["family_id"]))
            self.assertIn("catalog_sha256", gp)
        # ---- workers=1 enforced
        with self.assertRaises(C.CampaignError) as cm:
            s.campaign_run(cid, workers=2)
        self.assertEqual(cm.exception.code, "WORKERS_NOT_ALLOWED")
        # ---- interrupted after 5 strategies (an infrastructure error), then resumed
        real = s._run_cell
        calls = {"n": 0}

        def crash_after_5(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 6:
                raise OSError("simulated infrastructure failure (disk)")
            return real(*a, **kw)

        with mock.patch.object(s, "_run_cell", side_effect=crash_after_5):
            r1 = s.campaign_run(cid)
        self.assertFalse(r1["complete"])
        self.assertEqual((r1["ledger"]["campaign_trials"], r1["ledger"]["campaign_failed_events"], r1["n_failed_cells"]),
                         (5, 1, 1))                                                    # failure recorded, NOT a trial
        self.assertIn("OSError", r1["failed_cells"][0]["error"])
        self.assertTrue(s.campaign_check(cid)["ready"])                                 # resumable state
        calls2 = {"n": 0}

        def spy(*a, **kw):
            calls2["n"] += 1
            return real(*a, **kw)

        with mock.patch.object(s, "_run_cell", side_effect=spy):
            r2 = s.campaign_run(cid)
        self.assertTrue(r2["complete"], r2)
        self.assertEqual(calls2["n"], self.n - 5)                                       # completed ones not re-run
        led = r2["ledger"]
        self.assertEqual((led["campaign_trials"], led["protocol_trials_total"], led["strategies_with_trial"],
                          led["foreign_trials"], led["holdout_accesses"]), (self.n, self.n, self.n, 0, 0))
        self.assertEqual(led["strategies_with_multiple_trial_keys"], [])               # one trial key per strategy
        self.assertEqual(s.store.count_trials(self.pid), self.n)                         # budget exactly used
        # ---- MTF / prop / timeframes did not multiply: one counted event per strategy, all discovery
        ev = [e for e in s.store.list_trial_events(self.pid) if e["counted"]]
        self.assertEqual(sorted(e["strategy_id"] for e in ev), sorted(r["strategy_id"] for r in self.rows))
        for e in ev:
            self.assertLess(pd.Timestamp(e["window_end"]), self.hold_open)
        self.assertEqual(len({e["trial_key"] for e in ev}), self.n)
        # ---- prop audit present, base result unchanged by it
        from edgelab.analytics.metrics import compute_metrics
        for c in s.store.list_search_cells(spec["search"]["search_id"]):
            if c["status"] != "completed":
                continue
            rec, trades = s.store.load_run(c["run_id"])
            self.assertEqual(len(rec["prop"]["profiles"]), 4)
            self.assertEqual(rec["prop"]["base"]["trade_count"], len(trades))
            self.assertEqual(rec["trades_hash"], c["trades_hash"])
            self.assertEqual(json.loads(c["headline_json"])["trade_count"],
                             compute_metrics(trades, sample_thresholds=s.cfg.get("sample_size"))["trade_count"])
        # ---- rerun: nothing evaluated, no new event
        n_events = len(s.store.list_trial_events(self.pid))
        with mock.patch.object(s, "_run_cell", side_effect=AssertionError("must not run")):
            r3 = s.campaign_run(cid)
        self.assertTrue(r3["complete"])
        self.assertEqual(len(s.store.list_trial_events(self.pid)), n_events)
        # ---- a retry of a completed strategy through the protocol path is a duplicate, never a second trial
        sid0, tf0 = self.rows[0]["strategy_id"], self.rows[0]["definition"]["timeframe"]
        per = spec["search"]["spec"]["period"]
        s.backtest_strategy(sid0, spec["dataset_resolution"]["by_timeframe"][tf0]["dataset_id"], False,
                            (per["start"], per["end"]))
        self.assertEqual(s.store.count_trials(self.pid), self.n)
        # ---- the protocol is now exhausted: an extra strategy is refused (no 10,001st)
        from edgelab.research.protocol import ProtocolRefusal
        from tests.test_research_protocol import fixture
        extra = s.save_strategy(fixture("rsi_threshold.yaml"))["strategy_id"]
        with self.assertRaises(ProtocolRefusal):
            s.backtest_strategy(extra, self.src, False, (per["start"], per["end"]))
        # ---- frozen spec and manifest are bound: tampering refuses
        path = Path(C.campaigns_dir(s) / cid / "campaign.json")
        orig = path.read_text()
        doc = json.loads(orig)
        doc["spec"]["search"]["spec"]["period"]["end"] = str(self.hold_open + pd.Timedelta(days=5))   # into the holdout
        path.write_text(json.dumps(doc))
        with self.assertRaises(C.CampaignError) as cm:
            s.campaign_run(cid)
        self.assertEqual(cm.exception.code, "CAMPAIGN_TAMPERED")
        path.write_text(orig)
        mf = s.factory_dir / self.mid / "strategies.jsonl"
        morig = mf.read_text()
        lines = morig.splitlines()
        row = json.loads(lines[0])
        row["definition"]["exit"]["max_hold_bars"] = 3
        mf.write_text("\n".join([json.dumps(row, sort_keys=True)] + lines[1:]) + "\n")
        rep = s.campaign_check(cid)
        self.assertFalse(rep["ready"])
        with self.assertRaises(C.CampaignError) as cm:
            s.campaign_run(cid)
        self.assertEqual(cm.exception.code, "PREFLIGHT_FAILED")
        mf.write_text(morig)
        self.assertEqual(s.protocol_status(self.pid)["holdout"]["looks_used"], 0)
        # ---- ADR-89: the workspace's settings change after the protocol (the user's case: the HistData entries
        # disappear from a workspace that is the source clone). The preflight says exactly what differs; restoring the
        # protocol's recorded settings makes it ready again, nothing else changes.
        from edgelab.runtime import strip_legacy_blocks
        before = {f.name: f.read_text() for f in (self.root / "configs").glob("*.yaml")}
        for name in ("costs.yaml", "data.yaml", "instruments.yaml"):
            f = self.root / "configs" / name
            f.write_text(strip_legacy_blocks(f.read_text()))
        s2 = self.svc()
        rep = s2.campaign_check(cid)
        bad = {c["check"]: c for c in rep["checks"] if not c["ok"]}
        self.assertEqual(set(bad), {"research config = protocol config", "search id = frozen search id"})
        det = bad["research config = protocol config"]["detail"]
        paths = {d["path"]: d["change"] for d in det["differences"]}
        self.assertEqual(paths["costs.symbols.NAS100_HISTDATA"], "removed")
        self.assertEqual(paths["instruments.NAS100_HISTDATA"], "removed")
        self.assertEqual(paths["costs.symbols.NQ_DUKASCOPY.notes"], "changed")
        self.assertTrue(det["restorable"])
        self.assertEqual(det["protocol_hash"], s2.get_protocol(self.pid)["material"]["config_hash"])
        self.assertIn("settings fingerprint", bad["search id = frozen search id"]["detail"])
        with self.assertRaises(C.CampaignError) as cm:
            s2.campaign_run(cid)
        self.assertEqual(cm.exception.code, "PREFLIGHT_FAILED")
        with self.assertRaises(ValueError):
            s2.restore_protocol_config(self.pid, "yes")                               # typed confirmation required
        out = s2.restore_protocol_config(self.pid, "RESTORE")
        self.assertTrue(out["restored"])
        self.assertEqual(sorted(out["files"]), ["costs.yaml", "data.yaml", "instruments.yaml"])
        self.assertTrue(Path(out["backup"]).is_dir())
        self.assertTrue(s2.campaign_check(cid)["ready"])                              # reloaded in place
        self.assertTrue(self.svc().campaign_check(cid)["ready"])                      # and on a fresh start
        self.assertEqual(s2.store.count_trials(self.pid), self.n)                     # nothing evaluated or counted
        for name, text in before.items():                                              # back to the shipped files
            (self.root / "configs" / name).write_text(text)

    def test_missing_timeframe_dataset_fails_before_anything(self):
        s = self.svc()
        rows = s.list_datasets()
        without30 = [d for d in rows if d["timeframe"] != "30m"]
        with mock.patch.object(s, "list_datasets", return_value=without30):
            with self.assertRaises(C.CampaignError) as cm:
                s.campaign_freeze(self.mid)
        e = cm.exception.to_dict()
        self.assertEqual(e["code"], "PREFLIGHT_UNRESOLVED")
        self.assertEqual([u["timeframe"] for u in e["unresolved_timeframes"]], ["30m"])
        n30 = sum(1 for r in self.rows if r["definition"]["timeframe"] == "30m")
        self.assertEqual(len(e["unresolved_rows"]), n30)
        self.assertFalse(any(p.name.startswith("CMP_") and p.stat().st_mtime > 0 and False
                             for p in C.campaigns_dir(s).glob("*")))


if __name__ == "__main__":
    unittest.main()
