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

    def test_histdata_feed_has_assumed_mnq_equivalent_costs(self):
        cm = cost_model_from_config(CFG, "NAS100_HISTDATA", provider="HISTDATA")
        self.assertEqual((cm.status, cm.profile), ("assumed", "NAS100_HISTDATA@HISTDATA"))   # never broker_verified
        self.assertEqual((cm.commission_per_side, cm.fees_per_side), (0.50, 0.0))           # $ per unit per side
        self.assertEqual((cm.slippage_unit, cm.slippage_ticks_market, cm.slippage_ticks_stop,
                          cm.slippage_ticks_limit), ("points", 0.25, 0.25, 0.0))
        self.assertEqual((cm.spread_source, cm.spread_points), ("fixed", 0.50))
        self.assertEqual(cm.financing_mode, "none")

    def test_only_the_histdata_feed_is_configured(self):
        with self.assertRaises(CostConfigError):                     # symbol level stays unconfigured
            cost_model_from_config(CFG, "NAS100_HISTDATA")
        with self.assertRaises(CostConfigError):                     # any other feed is refused
            cost_model_from_config(CFG, "NAS100_HISTDATA", provider="OTHERFEED")
        for sym in ("NAS100_CFD", "US100_CFD", "NQ_CFD"):            # CFD profiles still unconfigured
            for prov in (None, "HISTDATA"):
                with self.assertRaises(CostConfigError):
                    cost_model_from_config(CFG, sym, provider=prov)

    def test_deterministic_round_trip_cost(self):
        """2 research units = 1 MNQ. Market entry + stop exit: commission 2 x 0.50 x 2 = $2,
        slippage (0.25 + 0.25) pts x $1 x 2 = $1, spread 0.50 pts x $1 x 2 = $1 -> $4 (a 10-point
        stop risks $20, so cost = 0.2 R). Limit in / limit out pays no slippage -> $3."""
        cm = cost_model_from_config(CFG, "NAS100_HISTDATA", provider="HISTDATA")
        rt = cm.round_trip_base("market", "stop", 2.0, HD)
        self.assertEqual({k: round(v, 10) for k, v in rt.items()},
                         {"commission_usd": 2.0, "fees_usd": 0.0, "slippage_usd": 1.0, "spread_usd": 1.0,
                          "slippage_ticks": 500.0})              # 0.50 points on the 0.001 proxy grid
        total = sum(rt[k] for k in ("commission_usd", "fees_usd", "slippage_usd", "spread_usd"))
        self.assertAlmostEqual(total, 4.0)
        self.assertAlmostEqual(total / (10.0 * HD.point_value * 2.0), 0.2)
        lim = cm.round_trip_base("limit", "limit", 2.0, HD)
        self.assertAlmostEqual(lim["commission_usd"] + lim["slippage_usd"] + lim["spread_usd"], 3.0)
        week = int(pd.Timedelta(days=7).value)                     # held over nights: no financing
        self.assertEqual(cm.financing_usd(1, 20000.0, 2.0, HD, 0, week), 0.0)


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
