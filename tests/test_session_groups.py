"""ADR-79: the 'By session' breakdown groups strategies by market hours from each strategy's own session definition,
and recomputes the median over the strategies of each group (never averaged from the per-window rows)."""
import statistics
import unittest

from edgelab.features.sessions import SessionWindow
from edgelab.research.results_view import SESSION_GROUPS, session_group


class TestSessionGroups(unittest.TestCase):
    def test_known_answers(self):
        sessions = {"NY_RTH": SessionWindow("NY_RTH", "America/New_York", "09:30", "16:00"),
                    "NY_PM": SessionWindow("NY_PM", "America/New_York", "12:00", "16:00"),
                    "ASIA_NY_EVENING": SessionWindow("ASIA_NY_EVENING", "America/New_York", "19:00", "02:00"),
                    "LONDON": SessionWindow("LONDON", "Europe/London", "08:00", "16:30")}
        cases = {
            "FXE_NY_0930_1130_MF": "NY AM", "FXE_NY_1030_1430_MF": "NY AM", "FXE_NY_1200_1430_MF": "NY PM",
            "FXE_NY_0930_1530_MF": "NY full day", "FXE_NY_0800_1100_MF": "London–NY overlap",
            "FXE_LDN_0800_1000_MF": "London", "FXE_TYO_0900_1200_MF": "Asia",
            "asia": "Asia", "london": "London", "london_ny_overlap": "London–NY overlap", "ny_first30": "NY AM",
            "ny_afternoon": "NY PM", "ny_rth": "NY full day", "ny_last90": "NY PM",
            "NY_RTH": "NY full day", "NY_PM": "NY PM", "ASIA_NY_EVENING": "Asia", "LONDON": "London",
            None: "Any time", "": "Any time", "SOMETHING_UNKNOWN": "Other", "FXE_ZZZ_0900_1000_MF": "Other"}
        for name, want in cases.items():
            self.assertEqual(session_group(name, sessions), want, name)
            self.assertIn(want, SESSION_GROUPS)

    def test_group_medians_are_recomputed_over_member_strategies(self):
        import shutil
        import tempfile
        from pathlib import Path
        from edgelab.research import results_view as rv
        from edgelab.services import Services
        from edgelab.web.demo import create_demo_workspace
        repo = Path(__file__).resolve().parents[1]
        tmp = Path(tempfile.mkdtemp())
        try:
            create_demo_workspace(tmp / "demo", repo)
            svc = Services(root=tmp / "demo")
            try:
                for s in svc.library.list():
                    for d in svc.backtest_readiness(s["strategy_id"])["datasets"]:
                        if d["runnable"]:
                            svc.backtest_strategy(s["strategy_id"], d["dataset_id"], record=True)
                            break
                o = rv.results_overview(svc, {"scope": "any"})
                groups = o["breakdowns"]["session_group"]
                self.assertTrue(groups)
                self.assertEqual([g["group"] for g in groups],
                                 [k for k in SESSION_GROUPS if k in {g["group"] for g in groups}])   # fixed order
                pts = {p["strategy_id"]: p for p in o["points"]}
                rows, _ = rv._latest_scoped(svc, "any", None)
                tested = [x for x in rows if x["ref"] and x["ref"]["trade_count"] > 0]
                self.assertEqual(sum(g["strategies"] for g in groups), len(tested))
                for g in groups:
                    members = [x for x in tested if session_group(x["facets"].get("session"), svc.sessions) == g["group"]]
                    self.assertEqual(g["strategies"], len(members))
                    self.assertAlmostEqual(g["median_expectancy_r"],
                                           statistics.median(x["ref"]["expectancy_r"] for x in members))
                    self.assertTrue(all(x["facets"]["strategy_id"] in pts for x in members))
            finally:
                svc.store.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
