"""Phase 4 step 4: in-sample ranking of stored search cells and the shortlist tag.

Known-answer tests use hand-built cells (pure function); integration tests use a real stored
search. Ranking never labels anything validated and a shortlist never changes run status."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from edgelab.research.ranking import RankingError, rank_cells
from tests.test_batch_runner import build_workspace


def cell(cid, sid, status="completed", current=True, **h):
    headline = None if status != "completed" else {"trade_count": h.pop("n", 150),
                                                   "sample_label": h.pop("label", "MODERATE SAMPLE"), **h}
    return {"cell_id": cid, "strategy_id": sid, "dataset_id": "DS", "status": status, "current": current,
            "run_id": f"RUN_{cid}" if status == "completed" else None, "trades_hash": "h", "headline": headline}


CELLS = [
    cell("C1", "STR_A", expectancy_r=0.10, profit_factor=1.2, net_r=15.0),
    cell("C2", "STR_B", expectancy_r=0.30, profit_factor=1.1, net_r=45.0),
    cell("C3", "STR_C", expectancy_r=-0.05, profit_factor=1.6, net_r=60.0, n=600, label="ADEQUATE SAMPLE"),
    cell("C4", "STR_D", expectancy_r=0.50, profit_factor=3.0, net_r=5.0, n=10, label="LOW SAMPLE SIZE"),
    cell("C5", "STR_E", status="failed"),
    cell("C6", "STR_F", status="ineligible"),
    cell("C7", "STR_G", status="pending"),
    cell("C8", "STR_H", expectancy_r=9.9, profit_factor=9.9, net_r=999.0, current=False),   # historical
    cell("C9", "STR_I", n=0, label="LOW SAMPLE SIZE"),                                        # zero trades
    cell("C10", "STR_J", expectancy_r=0.2, profit_factor=None, net_r=30.0),                  # PF non-finite
]


def order(r):
    return [x["strategy_id"] for x in r["ranked"]]


class TestRankCells(unittest.TestCase):
    def test_expectancy_is_the_default_known_answer(self):
        r = rank_cells(CELLS)
        self.assertEqual(r["metric"], "expectancy_r")
        self.assertEqual(r["min_sample_label"], "MODERATE SAMPLE")
        self.assertEqual(order(r), ["STR_B", "STR_J", "STR_A", "STR_C"])
        self.assertEqual([x["rank"] for x in r["ranked"]], [1, 2, 3, 4])
        self.assertEqual(r["ranked"][0]["value"], 0.30)
        self.assertEqual(r["ranked"][0]["run_id"], "RUN_C2")

    def test_profit_factor_and_net_r(self):
        pf = rank_cells(CELLS, "profit_factor")
        self.assertEqual(order(pf), ["STR_C", "STR_A", "STR_B"])       # C10 has no finite PF
        self.assertEqual(pf["excluded"]["metric_unavailable"], 1)
        self.assertEqual(order(rank_cells(CELLS, "net_r")), ["STR_C", "STR_B", "STR_J", "STR_A"])

    def test_win_rate_and_invalid_arguments_are_refused(self):
        with self.assertRaises(RankingError) as cm:
            rank_cells(CELLS, "win_rate")
        self.assertIn("never a ranking metric", str(cm.exception))
        for bad in (("sharpe", "MODERATE SAMPLE"), ("expectancy_r", "HUGE")):
            with self.assertRaises(RankingError):
                rank_cells(CELLS, *bad)

    def test_minimum_sample_filter(self):
        low = rank_cells(CELLS, min_sample_label="LOW SAMPLE SIZE")
        self.assertEqual(order(low), ["STR_D", "STR_B", "STR_J", "STR_A", "STR_C"])
        self.assertEqual(rank_cells(CELLS)["excluded"]["below_min_sample"], 1)
        adequate = rank_cells(CELLS, min_sample_label="ADEQUATE SAMPLE")
        self.assertEqual(order(adequate), ["STR_C"])
        self.assertEqual(adequate["excluded"]["below_min_sample"], 4)

    def test_ties_are_deterministic(self):
        tied = [cell("Z2", "STR_B", expectancy_r=0.1), cell("Z1", "STR_B", expectancy_r=0.1),
                cell("Z0", "STR_A", expectancy_r=0.1)]
        for perm in (tied, tied[::-1], [tied[1], tied[2], tied[0]]):
            r = rank_cells(perm)
            self.assertEqual([x["cell_id"] for x in r["ranked"]], ["Z0", "Z1", "Z2"])

    def test_exclusions_trials_and_labels(self):
        r = rank_cells(CELLS)
        self.assertEqual(r["excluded"], {"historical": 1, "ineligible": 1, "pending": 1, "cancelled": 0,
                                         "failed": 1, "no_trades": 1, "metric_unavailable": 0,
                                         "unknown_sample_label": 0, "below_min_sample": 1})
        self.assertNotIn("STR_H", order(rank_cells(CELLS, min_sample_label="LOW SAMPLE SIZE")))
        self.assertEqual(r["n_trials"], 7)          # current completed (6) + failed (1); not historical
        self.assertEqual(r["n_current_cells"], 9)
        self.assertIs(r["in_sample"], True)
        self.assertIs(r["validated"], False)
        self.assertEqual(r["status"], "IN_SAMPLE")
        self.assertIn("NOT VALIDATED", r["label"])
        self.assertIn("7 trial(s)", r["label"])
        text = json.dumps(r).lower()
        for word in ("robust", "profitable", "production"):
            self.assertNotIn(word, text)

    def test_infinite_profit_factor_ranks_first_without_a_numeric_substitute(self):
        cells = CELLS + [
            cell("I2", "STR_Y", expectancy_r=0.4, profit_factor=None, loss_rate=0.0, net_r=60.0),   # no losses
            cell("I1", "STR_X", expectancy_r=0.1, profit_factor=None, loss_rate=0.0, net_r=15.0),   # no losses
            cell("N1", "STR_Z", expectancy_r=0.0, profit_factor=None, loss_rate=0.0, net_r=0.0),    # undefined
            cell("N2", "STR_W", expectancy_r=0.1, profit_factor=None, loss_rate=0.2, net_r=15.0),   # null w/ losses
        ]
        r = rank_cells(cells, "profit_factor")
        self.assertEqual(order(r), ["STR_X", "STR_Y", "STR_C", "STR_A", "STR_B"])  # inf ties: strategy_id
        top = r["ranked"][0]
        self.assertIs(top["value_infinite"], True)
        self.assertIsNone(top["value"])                                          # never a made-up number
        self.assertIsNone(top["metrics"]["profit_factor"])
        self.assertTrue(all(x["value_infinite"] is False for x in r["ranked"][2:]))
        self.assertEqual(r["excluded"]["metric_unavailable"], 3)                  # C10, N1, N2 stay excluded
        self.assertNotIn("STR_Y", order(rank_cells(cells, "profit_factor", "ADEQUATE SAMPLE")))  # sample floor
        self.assertEqual(order(rank_cells(cells))[:2], ["STR_Y", "STR_B"])        # other metrics unaffected
        json.dumps(r, allow_nan=False)

    def test_infinite_profit_factor_through_the_real_metric_path(self):
        import numpy as np
        import pandas as pd
        from edgelab.analytics.metrics import compute_metrics
        from edgelab.services import _jsonable

        def stored_headline(net_r):                        # exactly what run_search stores per cell
            n = len(net_r)
            t0 = pd.Timestamp("2024-03-04T14:30Z")
            trades = pd.DataFrame({"trade_no": range(n), "net_r": net_r, "gross_r": net_r,
                                   "cost_r": np.zeros(n), "net_usd": np.array(net_r) * 100,
                                   "entry_ts": [t0 + pd.Timedelta(hours=i) for i in range(n)],
                                   "exit_ts": [t0 + pd.Timedelta(hours=i, minutes=30) for i in range(n)],
                                   "holding_minutes": np.full(n, 30.0), "exit_reason": ["target"] * n,
                                   "conflict_resolution": [""] * n})
            met = compute_metrics(trades, sample_thresholds={"min_trades": 2, "preferred_trades": 4})
            return _jsonable({k: v for k, v in met.items() if not isinstance(v, (dict, tuple, list))})

        winners, undefined, mixed = stored_headline([1.0, 2.0, 0.5]), stored_headline([0.0, 0.0, 0.0]), \
            stored_headline([1.0, -1.0, 2.0])
        self.assertIsNone(winners["profit_factor"])                              # +inf stored as null
        self.assertIsNone(undefined["profit_factor"])                            # nan stored as null
        mk = lambda cid, sid, h: {"cell_id": cid, "strategy_id": sid, "dataset_id": "DS", "status": "completed",  # noqa: E731
                                  "current": True, "run_id": "R", "trades_hash": "h", "headline": h}
        r = rank_cells([mk("A", "STR_MIX", mixed), mk("B", "STR_WIN", winners), mk("C", "STR_ZERO", undefined)],
                       "profit_factor")
        self.assertEqual(order(r), ["STR_WIN", "STR_MIX"])
        self.assertIs(r["ranked"][0]["value_infinite"], True)
        self.assertEqual(r["ranked"][1]["value"], 3.0)
        self.assertEqual(r["excluded"]["metric_unavailable"], 1)

    def test_ranking_does_not_mutate_cells(self):
        before = copy.deepcopy(CELLS)
        rank_cells(CELLS, "net_r", "LOW SAMPLE SIZE")
        self.assertEqual(CELLS, before)


class TestStoredRankingAndShortlist(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        cls.svc, cls.ids = build_workspace(cls.root)
        i = cls.ids
        cls.search = cls.svc.run_search({"strategies": {"ids": [i["ema"], i["ema3"], i["never"], i["ema15"]]},
                                         "datasets": [i["fut"], i["cfd"]]})
        cls.sid = cls.search["search_id"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_rank_stored_search(self):
        i = self.ids
        r = self.svc.rank_search(self.sid)                                  # spec defaults
        self.assertEqual((r["metric"], r["min_sample_label"]), ("expectancy_r", "MODERATE SAMPLE"))
        self.assertEqual(r["n_trials"], 3)
        self.assertEqual(r["excluded"]["ineligible"], 5)
        self.assertEqual(r["excluded"]["no_trades"], 1)                     # the zero-trade strategy
        low = self.svc.rank_search(self.sid, "net_r", "LOW SAMPLE SIZE")    # synthetic data: small samples
        self.assertEqual({x["strategy_id"] for x in low["ranked"]}, {i["ema"], i["ema3"]})
        vals = [x["value"] for x in low["ranked"]]
        self.assertEqual(vals, sorted(vals, reverse=True))
        self.assertIs(low["validated"], False)
        with self.assertRaises(RankingError):
            self.svc.rank_search(self.sid, "win_rate")
        with self.assertRaises(KeyError):
            self.svc.rank_search("SRCH_NOPE")

    def test_ranking_does_not_mutate_stored_cells(self):
        before = self.svc.store.list_search_cells(self.sid)
        self.svc.rank_search(self.sid, "profit_factor", "LOW SAMPLE SIZE")
        self.assertEqual(self.svc.store.list_search_cells(self.sid), before)

    def test_shortlist_is_a_tag_only(self):
        i = self.ids
        runs_before = self.svc.store.list_runs()
        cells_before = self.svc.store.list_search_cells(self.sid)
        out = self.svc.select_shortlist(self.sid, [i["ema3"], i["ema"], i["ema3"], i["ema"]])
        self.assertEqual(out["strategy_ids"], [i["ema3"], i["ema"]])        # first occurrence order
        self.assertEqual(out["duplicates_removed"], 2)
        self.assertIs(out["validated"], False)
        stored = json.loads(self.svc.store.get_search_batch(self.sid)["shortlist_json"])
        self.assertEqual(stored["strategy_ids"], [i["ema3"], i["ema"]])
        self.assertEqual(self.svc.rank_search(self.sid)["search_status"], "completed")
        after = self.svc.store.list_runs()                                  # no run row or status changed
        self.assertTrue(after.equals(runs_before))
        self.assertEqual(set(after["status"]), {"IN_SAMPLE"})
        self.assertEqual(self.svc.store.list_search_cells(self.sid), cells_before)
        again = self.svc.run_search({"strategies": {"ids": [i["ema"], i["ema3"], i["never"], i["ema15"]]},
                                     "datasets": [i["fut"], i["cfd"]]})     # a resume keeps the tag
        self.assertEqual(again["shortlist"]["strategy_ids"], [i["ema3"], i["ema"]])

    def test_shortlist_refuses_unknown_or_non_current_ids(self):
        i = self.ids
        before = self.svc.store.get_search_batch(self.sid)["shortlist_json"]
        for bad in (["STR_NOT_IN_SEARCH"], [i["ema"], "STR_NOT_IN_SEARCH"], i["ema"], [1]):
            with self.subTest(bad=bad):
                with self.assertRaises(RankingError):
                    self.svc.select_shortlist(self.sid, bad)
        self.assertEqual(self.svc.store.get_search_batch(self.sid)["shortlist_json"], before)
        other = self.svc.run_search({"strategies": {"ids": [i["never"]]},
                                     "datasets": [i["fut"]]})
        with self.assertRaises(RankingError):                               # belongs to another search
            self.svc.select_shortlist(other["search_id"], [i["ema"]])
        with self.assertRaises(KeyError):
            self.svc.select_shortlist("SRCH_NOPE", [i["ema"]])
        fam = self.svc.library.load(i["ema"])["family_id"]                  # historical cells are not current
        spec = {"strategies": {"families": [fam]}, "datasets": [i["fut"]]}
        self.svc.run_search(spec)
        self.svc.archive_strategy(i["ema3"])
        try:
            fs = self.svc.run_search(spec)
            self.assertIn(i["ema3"], {c["strategy_id"] for c in fs["historical_cells"]})
            with self.assertRaises(RankingError):
                self.svc.select_shortlist(fs["search_id"], [i["ema3"]])
            self.assertNotIn(i["ema3"], {x["strategy_id"] for x in
                                         self.svc.rank_search(fs["search_id"], "net_r", "LOW SAMPLE SIZE")["ranked"]})
        finally:
            self.svc.restore_strategy(i["ema3"])


if __name__ == "__main__":
    unittest.main()
