"""HistData NSXUSD research-proxy configuration (ADR-43): instrument, cost refusal, R1/R2 calendars.

Config/semantics only. No HistData file is available to the test suite, so nothing here claims
that any real HistData year passes validation."""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from edgelab.data.validation import validate_bars
from edgelab.engine.costs import CostConfigError, cost_model_from_config
from tests.helpers import CALENDARS, CFG, INSTRUMENTS

HD = INSTRUMENTS["NAS100_HISTDATA"]
R1, R2 = CALENDARS["HISTDATA_NSX_R1"], CALENDARS["HISTDATA_NSX_R2"]


def ny(*stamps):
    return pd.DatetimeIndex([pd.Timestamp(s, tz="America/New_York") for s in stamps]).tz_convert("UTC")


class TestHistDataInstrument(unittest.TestCase):
    def test_spec(self):
        self.assertEqual((HD.tick_size, HD.underlying, HD.asset_class, HD.calendar),
                         (0.001, "NDX", "cfd", "HISTDATA_NSX_R2"))
        self.assertAlmostEqual(HD.point_value, 1.0)
        self.assertTrue(HD.extra["research_proxy"])
        self.assertIn("not a live tradable contract", HD.description)
        self.assertEqual(HD.round_to_tick(21000.1234), 21000.123)

    def test_nas100_cfd_unchanged(self):
        n = INSTRUMENTS["NAS100_CFD"]
        self.assertEqual((n.tick_size, n.tick_value, n.calendar, n.underlying), (0.01, 0.01, "CME_EQUITY", "NDX"))

    def test_costs_refused_until_configured(self):
        with self.assertRaises(CostConfigError):
            cost_model_from_config(CFG, "NAS100_HISTDATA")
        with self.assertRaises(CostConfigError):
            cost_model_from_config(CFG, "NAS100_HISTDATA", provider="HISTDATA")


class TestHistDataCalendars(unittest.TestCase):
    def test_existing_calendars_unchanged(self):
        c = CALENDARS["CME_EQUITY"]
        self.assertEqual((c.timezone, c.session_open, c.session_close), ("America/New_York", "18:00", "17:00"))
        f = CALENDARS["FX_24_5"]
        self.assertEqual((f.timezone, f.session_open, f.session_close), ("America/New_York", "17:00", "17:00"))

    def test_r2_session_edges_winter_and_summer(self):
        for d in ("2022-01", "2022-07"):            # Mon-Fri week in NY winter (EST) and summer (EDT)
            days = {"2022-01": ("09", "10", "14", "15"), "2022-07": ("10", "11", "15", "16")}[d]
            sun, mon, fri, sat = (f"{d}-{x}" for x in days)
            inside = ny(f"{sun} 18:00", f"{mon} 16:14", f"{mon} 18:00", f"{fri} 16:14")
            outside = ny(f"{sun} 17:59", f"{mon} 16:15", f"{mon} 17:00", f"{mon} 17:59",
                         f"{fri} 16:15", f"{fri} 18:00", f"{sat} 12:00")
            self.assertTrue(R2.in_session(inside).all(), d)
            self.assertFalse(R2.in_session(outside).any(), d)
        # wall-clock sessions: the UTC open moves by one hour across US DST
        self.assertEqual(R2.session_bounds(pd.Timestamp("2022-01-10").date())[0].tz_convert("UTC").hour, 23)
        self.assertEqual(R2.session_bounds(pd.Timestamp("2022-07-11").date())[0].tz_convert("UTC").hour, 22)

    def test_r1_session_edges(self):
        self.assertTrue(R1.in_session(ny("2018-03-05 16:59", "2018-03-05 16:20", "2018-03-04 18:00")).all())
        self.assertFalse(R1.in_session(ny("2018-03-05 17:00", "2018-03-05 17:59", "2018-03-09 17:00")).any())

    def test_dst_transition_week_expected_bars(self):
        # Sun 2022-03-13 (spring forward) 18:00 EDT -> Fri 2022-03-18 16:15 EDT: 5 sessions of 22h15m
        exp = R2.expected_bar_opens(ny("2022-03-13 18:00")[0], ny("2022-03-18 16:14")[0], 1)
        self.assertEqual(len(exp), 5 * (22 * 60 + 15))
        self.assertFalse(R2.in_session(ny("2022-03-14 17:00", "2022-03-14 17:30")).any())


class TestTransitionAnomalyStillFails(unittest.TestCase):
    """The measured 17:00-17:59 NY source anomaly must keep failing bars_outside_session:
    no calendar, threshold or silent filter admits it."""

    def test_17h_block_fails_gate(self):
        exp = R2.expected_bar_opens(ny("2022-03-13 18:00")[0], ny("2022-03-18 16:14")[0], 1)
        extra = pd.date_range(ny("2022-03-14 17:00")[0], periods=60, freq="1min")   # 17:00-17:59 EDT
        ts = exp.append(extra).sort_values()
        px = 14000.0 + np.arange(len(ts)) * 0.001
        df = pd.DataFrame({"ts": ts, "open": px, "high": px + 0.5, "low": px - 0.5, "close": px,
                           "volume": np.nan})
        rep = validate_bars(df, HD, R2, 1, CFG["validation"])
        chk = {c.name: c for c in rep.checks}
        self.assertEqual((chk["bars_outside_session"].status, chk["bars_outside_session"].count), ("FAIL", 60))
        self.assertEqual(rep.status, "FAIL")
        self.assertEqual(chk["tick_alignment"].status, "PASS")                         # 0.001 grid accepted


if __name__ == "__main__":
    unittest.main()
