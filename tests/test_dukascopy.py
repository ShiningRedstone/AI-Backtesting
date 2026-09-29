"""Phase 9 Part A: the Dukascopy source through the EXISTING import pipeline, its provisional identity,
its own (unconfigured) cost profile, gap classification, 1m -> 5m derivation, and the workspace's
Preferred Research Dataset. All data here is a SYNTHETIC Dukascopy-shaped fixture written to temp dirs;
no real Dukascopy file is present in this repository."""
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.data.importer import ImportFailed
from edgelab.engine.costs import CostConfigError, cost_model_from_config
from edgelab.instruments import InstrumentIdentityError, identity_info, identity_problem, load_instruments
from edgelab.services import Services
from tests.dukascopy_fixture import session_minutes, write_fixture

REPO = Path(__file__).resolve().parents[1]
# exactly the options of the documented real import command (DATA_IMPORT.md), on a SYNTHETIC fixture
OPTS = dict(profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY", provider="DUKASCOPY", asset_type="CFD",
            symbol="USATECH.IDX/USD", price_basis="bid", timeframe="1m", dataset_name="NQ_DUKASCOPY_2021_2026",
            derive_timeframes=["5m"], build_features=False)
EMA = REPO / "strategies" / "fixtures" / "ema_crossover.yaml"


def workspace() -> Path:
    root = Path(tempfile.mkdtemp())
    shutil.copytree(REPO / "configs", root / "configs")
    return root


class DukascopyBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = workspace()
        cls.csv = cls.root / "nq_dukascopy_1m.csv"
        cls.facts = write_fixture(cls.csv, end="2024-04-27")
        cls.raw_bytes = cls.csv.read_bytes()
        cls.svc = Services(root=cls.root)
        cls.res = cls.svc.import_file({**OPTS, "file": str(cls.csv)})
        cls.m1 = cls.res["dataset_id"]
        cls.m5 = next(d["dataset_id"] for d in cls.svc.list_datasets() if d["timeframe"] == "5m")

    @classmethod
    def tearDownClass(cls):
        cls.svc.store.close()
        shutil.rmtree(cls.root, ignore_errors=True)


class TestImportAndProvenance(DukascopyBase):
    def test_schema_utc_and_source_identity_recorded(self):
        m = self.svc.dataset_detail(self.m1)["manifest"]
        self.assertTrue(self.m1.startswith("NQ_DUKASCOPY_2021_2026_1M_"))
        self.assertEqual((m["provider"], m["instrument"], m["asset_type"], m["symbol"]),
                         ("DUKASCOPY", "NQ_DUKASCOPY", "CFD", "USATECH.IDX/USD"))         # stated at import, never inferred
        self.assertEqual((m["price_basis"], m["volume_type"]), ("bid", "unknown"))      # source volume != exchange volume
        self.assertEqual((m["source_timezone"], m["source_timestamp_convention"]), ("UTC", "open"))
        self.assertEqual(m["calendar"], "DUKASCOPY_USATECH_OBSERVED")
        self.assertEqual(m["source_file_sha256"], hashlib.sha256(self.raw_bytes).hexdigest())
        self.assertEqual(self.csv.read_bytes(), self.raw_bytes)                          # source never modified
        self.assertEqual(m["source_detail"]["options"]["profile"], "dukascopy_utc_csv")
        self.assertEqual(m["source_detail"]["file"], str(self.csv))
        ds = self.svc.load_dataset(self.m1)
        first = pd.Timestamp(int(ds.bars.ts_ns[0])).tz_localize("UTC")
        self.assertEqual(first, self.facts["first"])                                   # explicit offsets -> exact UTC
        self.assertEqual(len(ds.bars.ts_ns), self.facts["rows"])
        self.assertTrue(np.all(np.diff(ds.bars.ts_ns) > 0))

    def test_identity_and_unverified_calendar_are_visible(self):
        det = self.svc.dataset_detail(self.m1)
        self.assertTrue(any("volume semantics unknown" in x for x in det["limitations"]))
        self.assertFalse(any("price basis" in x for x in det["limitations"]))       # BID is stated
        idn = det["identity"]
        self.assertEqual((idn["identity_status"], idn["source_symbol"], idn["source_feed_code"], idn["asset_class"],
                          idn["price_basis"]), ("user_specified", "USATECH.IDX/USD", "E_NQ-100", "cfd", "bid"))
        self.assertTrue(idn["research_proxy"])
        self.assertIn("NOT CME", idn["volume_semantics"])
        self.assertEqual(idn["economics"], "research_units")
        self.assertIn("OFFER_SIDE_BID", idn["identity_evidence"])
        self.assertEqual(idn["missing_metadata"], [])
        self.assertEqual(idn["calendar_status"], "regular_hours_verified")
        self.assertIsNone(idn["problem"])
        self.assertIn("holidays and early closes", idn["calendar_unverified_scope"])
        self.assertTrue(any("regular session hours verified; NOT verified: holidays" in x for x in det["limitations"]))

    def test_validation_report_covers_the_required_checks(self):
        rep = self.svc.store.get_report(self.m1)
        names = {c["name"]: c for c in rep["checks"]}
        for n in ("timestamp_order", "exact_duplicates", "conflicting_duplicates", "grid_alignment", "tick_alignment",
                  "range_spikes", "positive_prices", "bars_outside_session", "missing_bars", "missing_trading_days"):
            self.assertIn(n, names)
        self.assertEqual(names["bars_outside_session"]["status"], "PASS")               # across the 2024-03-10 DST switch
        self.assertEqual(names["missing_bars"]["count"], len(self.facts["gap"]))        # the real gap, never filled
        self.assertEqual(names["missing_bars"]["status"], "INFO")                       # 0.045% < the 0.1% WARN threshold

    def test_1m_to_5m_derivation_with_lineage(self):
        rows = {d["dataset_id"]: d for d in self.svc.list_datasets()}
        self.assertEqual(rows[self.m5]["parent_dataset_id"], self.m1)
        self.assertEqual(rows[self.m5]["provider"], "DUKASCOPY")
        one, five = self.svc.load_dataset(self.m1), self.svc.load_dataset(self.m5)
        t0 = int(five.bars.ts_ns[0])
        sel = (one.bars.ts_ns >= t0) & (one.bars.ts_ns < t0 + 5 * 60_000_000_000)
        self.assertEqual(five.bars.open[0], one.bars.open[sel][0])
        self.assertEqual(five.bars.high[0], one.bars.high[sel].max())
        self.assertEqual(five.bars.low[0], one.bars.low[sel].min())
        self.assertEqual(five.bars.close[0], one.bars.close[sel][-1])

    def test_gap_classification_is_descriptive(self):
        q = self.svc.dataset_quality(self.m1)
        self.assertEqual(q["summary"]["missing_bars"], len(self.facts["gap"]))
        self.assertEqual(q["summary"]["by_position"], {"intra_session": 1})
        g = q["largest_gaps"][0]
        self.assertEqual((g["missing_bars"], g["length_class"]), (len(self.facts["gap"]), "medium"))
        self.assertEqual(pd.Timestamp(g["start"]), self.facts["gap"][0])
        self.assertEqual(q["coverage"]["missing_trading_days"], [])
        before = self.svc.store.get_manifest(self.m1).to_dict()
        self.assertEqual(self.svc.store.get_manifest(self.m1).to_dict(), before)     # read-only


class TestObservedDukascopyCalendar(unittest.TestCase):
    """DUKASCOPY_USATECH_OBSERVED = the schedule measured in the real file: New York 18:00 -> 16:15,
    closed 16:15-18:00 daily, Friday last bar 16:14, DST-safe. Tested through the validation gate."""

    @classmethod
    def setUpClass(cls):
        from edgelab.core.config import load_config
        from edgelab.data.calendar import load_calendars
        cls.cfg = load_config(REPO / "configs")
        cls.cal = load_calendars(cls.cfg)["DUKASCOPY_USATECH_OBSERVED"]
        cls.inst = load_instruments(cls.cfg)["NQ_DUKASCOPY"]

    def frame(self, ts: pd.DatetimeIndex) -> pd.DataFrame:
        n = len(ts)
        c = 18000 + np.arange(n) * 0.001
        return pd.DataFrame({"ts": ts.as_unit("ns"), "open": c, "high": c + 1, "low": c - 1, "close": c,
                             "volume": np.full(n, 0.5)})

    def report(self, ts):
        from edgelab.data.validation import validate_bars
        return validate_bars(self.frame(ts), self.inst, self.cal, 1, self.cfg.get("validation"))

    def test_instrument_uses_the_observed_calendar(self):
        self.assertEqual(self.inst.calendar, "DUKASCOPY_USATECH_OBSERVED")
        self.assertEqual((self.cal.session_open, self.cal.session_close, self.cal.timezone),
                         ("18:00", "16:15", "America/New_York"))

    def test_daily_1615_1800_closure_is_not_missing_data(self):
        ts = session_minutes("2024-03-03", "2024-04-27")          # a complete feed of the observed schedule
        rep = self.report(ts)
        self.assertEqual(rep.get("missing_bars").count, 0)
        self.assertEqual(rep.get("bars_outside_session").count, 0)
        self.assertEqual(rep.get("missing_trading_days").count, 0)
        # the old CME-style assumption (close 17:00) would call the closure missing: 45 bars per trading date
        from edgelab.data.calendar import SessionCalendar
        from edgelab.data.validation import validate_bars
        old = SessionCalendar("OLD_ASSUMPTION", "America/New_York", "18:00", "17:00")
        n_dates = len(set(self.cal.trading_dates(ts)))
        # (the last trading date's 16:15-17:00 lies after the last bar, outside the checked range - the same
        #  1,304-of-1,305 seen on the real file)
        self.assertEqual(validate_bars(self.frame(ts), self.inst, old, 1, self.cfg.get("validation"))
                         .get("missing_bars").count, 45 * (n_dates - 1))

    def test_friday_closes_at_1615_and_later_bars_are_outside_the_session(self):
        ny = lambda s: pd.Timestamp(s, tz="America/New_York").tz_convert("UTC")
        exp = self.cal.expected_bar_opens(ny("2024-03-15 00:00"), ny("2024-03-17 23:59"), 1)
        fri = exp[exp < ny("2024-03-16 00:00")]
        self.assertEqual(fri[-1], ny("2024-03-15 16:14"))                     # last Friday bar 16:14 NY
        self.assertEqual(exp[len(fri)], ny("2024-03-17 18:00"))               # next bar: Sunday 18:00 NY
        ts = session_minutes("2024-03-10", "2024-03-16").append(pd.DatetimeIndex([ny("2024-03-15 16:15"),
                                                                                 ny("2024-03-15 16:30")]))
        rep = self.report(ts.sort_values())
        self.assertEqual(rep.get("bars_outside_session").count, 2)             # reported, never silently accepted
        self.assertEqual(rep.get("bars_outside_session").status, "WARN")      # 2 of ~6,700 < the 0.1% FAIL threshold
        heavy = session_minutes("2024-03-10", "2024-03-16").append(
            pd.date_range(ny("2024-03-15 16:15"), ny("2024-03-15 16:59"), freq="1min"))   # a whole extra 16:15-17:00
        self.assertEqual(self.report(heavy.sort_values()).get("bars_outside_session").status, "FAIL")

    def test_boundaries_are_dst_safe(self):
        ny = lambda s: pd.Timestamp(s, tz="America/New_York").tz_convert("UTC")
        for day, utc_close in (("2024-03-08", "2024-03-08 21:14"), ("2024-03-12", "2024-03-12 20:14"),
                               ("2024-11-01", "2024-11-01 20:14"), ("2024-11-05", "2024-11-05 21:14")):
            exp = self.cal.expected_bar_opens(ny(f"{day} 00:00"), ny(f"{day} 23:59"), 1)
            closes = exp[exp.tz_convert("America/New_York").strftime("%H:%M") == "16:14"]
            self.assertEqual(closes[0], pd.Timestamp(utc_close, tz="UTC"))    # 16:14 NY in EST and EDT
            self.assertFalse(((exp.tz_convert("America/New_York").hour * 60 + exp.tz_convert("America/New_York").minute
                               >= 16 * 60 + 15) & (exp.tz_convert("America/New_York").hour < 18)).any())

    def test_genuine_gaps_inside_the_session_still_count(self):
        ts = session_minutes("2024-03-03", "2024-04-27")
        ny = ts.tz_convert("America/New_York")
        hole = (ny.strftime("%Y-%m-%d") == "2024-03-20") & (ny.hour == 16) & (ny.minute < 15)   # 16:00-16:14
        rep = self.report(ts[~hole])
        self.assertEqual(rep.get("missing_bars").count, 15)
        td = (ny.tz_localize(None) + pd.Timedelta(hours=6)).normalize()
        rep = self.report(ts[td != pd.Timestamp("2024-03-29")])                   # a whole trading date absent
        self.assertEqual(rep.get("missing_trading_days").count, 1)
        self.assertEqual(rep.get("missing_trading_days").status, "WARN")          # visible, not a holiday entry

    def test_economics_evidence_and_assumption_are_explicit(self):
        idn = identity_info(self.inst)
        self.assertEqual(idn["economics"], "research_units")                      # not upgraded to verified
        self.assertIn("point value 0.01 USD", idn["economics_evidence"])
        self.assertEqual(idn["economics_assumption"],
                         "1 EdgeLab research unit = 1 Dukascopy USATECH.IDX/USD CFD - not yet broker-verified")
        # values unchanged, and consistent with the evidence: USD 0.01 per 0.01 price = USD 1 per 1.0 price
        self.assertEqual((self.inst.tick_size, self.inst.tick_value, self.inst.point_value,
                          self.inst.min_size, self.inst.size_step), (0.001, 0.001, 1.0, 0.01, 0.01))
        self.assertAlmostEqual(0.01 / 0.01, self.inst.point_value)
        self.assertAlmostEqual(0.01 / self.inst.tick_size, 10.0)                   # 1 Dukascopy point = 10 ticks

    def test_only_regular_hours_are_verified(self):
        idn = identity_info(self.inst)
        self.assertEqual(idn["calendar_status"], "regular_hours_verified")
        self.assertIn("22:00-20:15 GMT", self.inst.extra["calendar_evidence"])            # official hours
        self.assertIn("0 outside-session bars", self.inst.extra["calendar_evidence"])     # real-file evidence
        self.assertIn("holidays and early closes", idn["calendar_unverified_scope"])
        self.assertIsNone(identity_problem(self.inst))
        self.assertEqual(self.cal.holidays, frozenset())                         # no holiday dates invented
        self.assertEqual(dict(self.cal.early_closes), {})                        # no early closes invented

    def test_calendar_status_rules(self):
        from edgelab.instruments import calendar_caveat
        base = dict(self.cfg["instruments"]["NQ_DUKASCOPY"])
        chk = lambda **kw: identity_problem(load_instruments({"instruments": {"NQ_DUKASCOPY": {**base, **kw}}})["NQ_DUKASCOPY"])
        self.assertIn("has not been verified", chk(calendar_status="provisional_unverified"))
        self.assertIn("needs calendar_evidence", chk(calendar_evidence=None))
        self.assertIn("needs calendar_unverified_scope", chk(calendar_unverified_scope=None))
        self.assertIn("unknown calendar_status", chk(calendar_status="mostly_verified"))
        self.assertIn("needs calendar_evidence", chk(calendar_status="verified", calendar_evidence=None))
        self.assertIsNone(chk(calendar_status="verified", calendar_unverified_scope=None))     # full verification
        full = load_instruments({"instruments": {"NQ_DUKASCOPY": {**base, "calendar_status": "verified"}}})["NQ_DUKASCOPY"]
        self.assertIsNone(calendar_caveat(full))                                  # caveat only for the partial status
        self.assertIn("unannounced early close", calendar_caveat(self.inst))


class TestGapRunsSplitAtTradingDates(unittest.TestCase):
    """Regression: an early-close tail on D and a wholly missing D+1 are consecutive in the expected grid
    (D 16:14 -> D+1's session opens at D 18:00). They must be two gaps: a session_close gap on D and a
    whole_trading_day gap on D+1 - not one merged session_close gap (the real-file report showed 14
    missing days but no whole_trading_day gap). Genuine mid-session blocks stay intra-session."""

    def test_early_close_tail_and_following_whole_day_are_separate_gaps(self):
        from edgelab.core.config import load_config
        from edgelab.data.calendar import load_calendars
        from edgelab.data.quality import gap_analysis
        from edgelab.data.validation import validate_and_freeze
        cfg = load_config(REPO / "configs")
        cal = load_calendars(cfg)["DUKASCOPY_USATECH_OBSERVED"]
        inst = load_instruments(cfg)["NQ_DUKASCOPY"]
        ts = session_minutes("2024-03-03", "2024-04-27")
        ny = ts.tz_convert("America/New_York")
        day = ny.strftime("%Y-%m-%d")
        td = (ny.tz_localize(None) + pd.Timedelta(hours=6)).normalize()
        m = ny.hour * 60 + ny.minute
        tail = (day == "2024-03-28") & (m >= 13 * 60 + 15) & (m < 16 * 60 + 15)      # 13:15-16:15 absent (180 bars)
        whole = td == pd.Timestamp("2024-03-29")                                        # trading date absent (1335)
        block = (day == "2024-04-10") & (m >= 10 * 60) & (m < 10 * 60 + 40)            # genuine feed gap (40)
        keep = ts[~(tail | whole | block)]
        n = len(keep)
        c = 18000 + np.arange(n) * 0.001
        df = pd.DataFrame({"ts": keep.as_unit("ns"), "open": c, "high": c + 1, "low": c - 1, "close": c,
                           "volume": np.full(n, 0.5)})
        ds = validate_and_freeze(df, inst, cal, "1m", 1, "DUKASCOPY", "TEST_GAPS", cfg.get("validation"),
                                 volume_type="unknown")
        g = gap_analysis(ds)
        self.assertEqual(g["summary"]["missing_bars"], 180 + 1335 + 40)
        self.assertEqual(g["summary"]["by_position"], {"whole_trading_day": 1, "session_close": 1, "intra_session": 1})
        self.assertEqual(g["summary"]["missing_bars_by_position"],
                         {"whole_trading_day": 1335, "session_close": 180, "intra_session": 40})
        by_pos = {x["position"]: x for x in g["largest_gaps"]}
        self.assertEqual(by_pos["whole_trading_day"]["trading_date"], "2024-03-29")
        self.assertEqual(g["coverage"]["missing_trading_days"], ["2024-03-29"])
        close = by_pos["session_close"]
        self.assertEqual((close["trading_date"], close["missing_bars"], close["minutes_before_session_close"]),
                         ("2024-03-28", 180, 0))
        intra = by_pos["intra_session"]
        self.assertEqual(intra["missing_bars"], 40)
        self.assertEqual(intra["likely"], "feed gap (no bars published)")                 # stays a warning, not hidden
        self.assertEqual(intra["minutes_after_session_open"], 16 * 60)                    # 18:00 -> 10:00 next day
        self.assertEqual(ds.report.get("missing_trading_days").status, "WARN")


class TestResearchRefusals(DukascopyBase):
    def test_backtest_refused_only_by_unconfigured_costs(self):
        with self.assertRaises(CostConfigError) as cm:
            self.svc.backtest_strategy(EMA.read_text(), self.m5)
        self.assertIn("NQ_DUKASCOPY@DUKASCOPY", str(cm.exception))
        row = next(d for d in self.svc.backtest_readiness()["datasets"] if d["dataset_id"] == self.m5)
        self.assertFalse(row["runnable"])
        self.assertEqual(row["reasons"], ["broker/provider cost profile is unconfigured - configure verified costs first"])
        self.assertTrue(any("NOT verified: holidays" in x for x in row["limitations"]))       # caveat stays visible

    def test_calendar_back_to_provisional_is_refused_again(self):
        cfg = copy.deepcopy(self.svc.cfg)
        cfg["instruments"]["NQ_DUKASCOPY"]["calendar_status"] = "provisional_unverified"
        svc = Services(cfg=cfg, root=self.root)
        try:
            with self.assertRaises(InstrumentIdentityError):
                svc.backtest_strategy(EMA.read_text(), self.m5)
            row = next(d for d in svc.backtest_readiness()["datasets"] if d["dataset_id"] == self.m5)
            self.assertTrue(any("calendar not yet verified" in r for r in row["reasons"]))
        finally:
            svc.store.close()

    def test_provisional_identity_is_still_refused(self):
        cfg = copy.deepcopy(self.svc.cfg)
        cfg["instruments"]["NQ_DUKASCOPY"].update(identity_status="provisional", calendar_status="verified",
                                                  calendar_evidence="test")
        self.assertIn("PROVISIONAL source identity", identity_problem(load_instruments(cfg)["NQ_DUKASCOPY"]))

    def test_calendar_verified_then_cost_refusal_names_the_dukascopy_profile(self):
        cfg = copy.deepcopy(self.svc.cfg)
        cfg["instruments"]["NQ_DUKASCOPY"].update(calendar_status="verified", calendar_evidence=None)
        self.assertIn("calendar_evidence", identity_problem(load_instruments(cfg)["NQ_DUKASCOPY"]))
        cfg["instruments"]["NQ_DUKASCOPY"]["calendar_evidence"] = "test: inspection report"
        self.assertIsNone(identity_problem(load_instruments(cfg)["NQ_DUKASCOPY"]))
        with self.assertRaises(CostConfigError) as cm:
            cost_model_from_config(cfg, "NQ_DUKASCOPY", provider="DUKASCOPY")
        self.assertIn("NQ_DUKASCOPY@DUKASCOPY", str(cm.exception))
        svc = Services(cfg=cfg, root=self.root)
        try:
            with self.assertRaises(CostConfigError):
                svc.backtest_strategy(EMA.read_text(), self.m5)
        finally:
            svc.store.close()

    def test_histdata_costs_never_apply_to_dukascopy(self):
        cfg = self.svc.cfg
        hist = cost_model_from_config(cfg, "NAS100_HISTDATA", provider="HISTDATA")           # unchanged: assumed
        self.assertEqual((hist.status, hist.profile), ("assumed", "NAS100_HISTDATA@HISTDATA"))
        for prov in ("DUKASCOPY", "HISTDATA", None):
            with self.assertRaises(CostConfigError):
                cost_model_from_config(cfg, "NQ_DUKASCOPY", provider=prov)

    def test_source_verified_needs_evidence(self):
        cfg = copy.deepcopy(self.svc.cfg)
        cfg["instruments"]["NQ_DUKASCOPY"].update(identity_status="source_verified", identity_evidence=None,
                                                  calendar_status="verified", calendar_evidence="test")
        self.assertIn("identity_evidence", identity_problem(load_instruments(cfg)["NQ_DUKASCOPY"]))


class TestInspectionScriptIsReadOnly(unittest.TestCase):
    def test_script_reports_and_stores_nothing(self):
        import contextlib
        import io
        import runpy
        root = workspace()
        self.addCleanup(shutil.rmtree, root, True)
        csv = root / "nq_1min_5years.csv"
        write_fixture(csv, end="2024-04-27")
        before = csv.read_bytes()
        mod = runpy.run_path(str(REPO / "scripts" / "dukascopy_inspect.py"), run_name="dukascopy_inspect")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(mod["main"]([str(csv), "--root", str(root), "--out", str(root / "r.json")]), 0)
        text = out.getvalue()
        for needle in ("rows 53,375", "duplicate timestamps 0", "monotonic True", "bars per hour (New York)",
                       "bars per hour (UTC)", "week_first_bar_ny: {'Sun 18:00': 8}", "week_last_bar_ny: {'Fri 16:14': 8}",
                       "minutes_of_day_rarely_present_ny: ['16:15-18:00']", "coverage by year", "missing trading days (0)",
                       "passes the gate"):
            self.assertIn(needle, text)
        self.assertEqual(csv.read_bytes(), before)
        self.assertFalse((root / "data").exists())                   # no store, no dataset, no report
        rep = json.loads((root / "r.json").read_text())
        self.assertEqual(rep["sha256"], hashlib.sha256(before).hexdigest())


class TestImportRefusals(unittest.TestCase):
    def setUp(self):
        self.root = workspace()
        self.svc = Services(root=self.root)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(self.svc.store.close)
        self.csv = self.root / "d.csv"

    def imp(self, **kw):
        return self.svc.import_file({**OPTS, "file": str(self.csv), **kw})

    def test_mixed_offsets_and_added_hours_are_refused(self):
        write_fixture(self.csv, extra_rows=["2024-03-15T21:30:00,18000.000,18001.000,17999.000,18000.500,0.1"])
        with self.assertRaises(ImportFailed) as cm:
            self.imp()
        self.assertIn("no UTC offset", str(cm.exception))
        write_fixture(self.csv)
        with self.assertRaises(ImportFailed) as cm:
            self.imp(source_timezone="UTC+2h")
        self.assertIn("do not add +Nh", str(cm.exception))
        self.assertEqual(self.svc.list_datasets(), [])

    def test_other_explicit_offsets_convert_exactly(self):
        write_fixture(self.csv)
        a = self.imp(derive_timeframes=[])
        # the same instants written with +02:00 offsets import to the SAME content
        df = pd.read_csv(self.csv)
        ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Etc/GMT-2")
        df["timestamp"] = ts.dt.strftime("%Y-%m-%dT%H:%M:%S+02:00")
        other = self.root / "e.csv"
        df.to_csv(other, index=False, float_format="%.4f")
        b = self.svc.import_file({**OPTS, "file": str(other), "derive_timeframes": [], "dataset_name": "X"})
        self.assertEqual(a["manifest"]["content_hash"], b["manifest"]["content_hash"])

    def test_wrong_timezone_shows_as_outside_session_and_is_refused(self):
        write_fixture(self.csv)
        df = pd.read_csv(self.csv)
        df["timestamp"] = (pd.to_datetime(df["timestamp"], utc=True) + pd.Timedelta(hours=1)).dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        df.to_csv(self.csv, index=False, float_format="%.4f")
        with self.assertRaises(ImportFailed) as cm:
            self.imp()
        self.assertEqual(cm.exception.stage, "validate")
        self.assertEqual(cm.exception.report.get("bars_outside_session").status, "FAIL")      # 1h shift = DST/tz signature
        self.assertEqual(self.svc.list_datasets(), [])

    def test_duplicates_are_cleaned_or_refused_never_guessed(self):
        write_fixture(self.csv)
        lines = self.csv.read_text().splitlines()
        self.csv.write_text("\n".join(lines + [lines[100]]) + "\n")                        # exact duplicate row
        r = self.imp(derive_timeframes=[])
        self.assertEqual(r["manifest"]["source_detail"]["raw_duplicate_bars"], 1)
        t, *_ = lines[100].split(",")
        self.csv.write_text("\n".join(lines + [f"{t},1.000,2.000,0.500,1.500,0.1"]) + "\n")  # conflicting duplicate
        with self.assertRaises(ImportFailed):
            self.imp(derive_timeframes=[], dataset_name="Y")

    def test_coverage_failure_stores_nothing(self):
        allm = session_minutes("2024-03-03", "2024-03-16")
        td = (allm.tz_convert("America/New_York").tz_localize(None) + pd.Timedelta(hours=6)).normalize()
        write_fixture(self.csv, drop=allm[td == pd.Timestamp("2024-03-06")])                # 10% missing in 2 weeks
        with self.assertRaises(ImportFailed):
            self.imp()
        self.assertEqual(self.svc.list_datasets(), [])


class TestPreferredDataset(unittest.TestCase):
    """The preference is a workspace default for NEW research; it never changes stored research."""

    @classmethod
    def setUpClass(cls):
        from tests.test_workspace import make_workspace, tree_fingerprint
        cls.root = Path(tempfile.mkdtemp())
        cls.made = make_workspace(cls.root)                                                  # runnable dataset + a run
        svc = Services(root=cls.root)
        csv = cls.root / "d.csv"
        write_fixture(csv, end="2024-04-27")
        svc.import_file({**OPTS, "file": str(csv)})
        cls.duka5 = next(d["dataset_id"] for d in svc.list_datasets() if d["provider"] == "DUKASCOPY" and d["timeframe"] == "5m")
        svc.store.close()
        cls.fingerprint = staticmethod(tree_fingerprint)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def svc(self):
        s = Services(root=self.root)
        self.addCleanup(s.store.close)
        return s

    def test_persistence_switching_and_runs_untouched(self):
        s = self.svc()
        self.assertEqual(s.preferred_dataset()["state"], "unset")
        before_runs = json.dumps(s.list_runs(), sort_keys=True, default=str)
        before_tree = self.fingerprint(self.root)
        s.set_preferred_dataset(self.duka5)
        s2 = self.svc()                                                                       # a new process/app
        self.assertEqual(s2.preferred_dataset_id(), self.duka5)
        rd = s2.backtest_readiness(EMA.read_text())
        self.assertEqual(rd["preferred_dataset_id"], self.duka5)
        self.assertTrue(next(d for d in rd["datasets"] if d["dataset_id"] == self.duka5)["preferred"])
        s2.set_preferred_dataset(self.made["dataset_id"])
        s2.clear_preferred_dataset()
        s2.set_preferred_dataset(self.made["dataset_id"])
        p = s2.preferred_dataset()
        self.assertEqual(p["preferred"]["dataset_id"], self.made["dataset_id"])
        self.assertEqual([h["dataset_id"] for h in p["history"]], [self.duka5, self.made["dataset_id"], None, self.made["dataset_id"]])
        # stored research is byte-identical: runs, datasets, strategies (the preference file is not part of it)
        after_tree = {k: v for k, v in self.fingerprint(self.root).items() if not k.endswith("workspace_preferences.json")}
        self.assertEqual(after_tree, {k: v for k, v in before_tree.items() if not k.endswith("workspace_preferences.json")})
        self.assertEqual(json.dumps(s2.list_runs(), sort_keys=True, default=str), before_runs)
        self.assertTrue((self.root / "data" / "workspace_preferences.json").exists())
        # a new backtest is ordinary: it records the dataset it was actually given
        out = s2.backtest_strategy(EMA.read_text(), self.made["dataset_id"], record=True)
        self.assertEqual(s2.get_run(out["run_id"])["record"]["dataset"]["dataset_id"], self.made["dataset_id"])

    def test_only_stored_valid_datasets_are_eligible(self):
        s = self.svc()
        with self.assertRaises(KeyError):
            s.set_preferred_dataset("NOPE_5M_0000000000")
        with self.assertRaises(ValueError):
            s.set_preferred_dataset("")

    def test_web_api(self):
        from edgelab.web.app import create_app
        c = create_app(self.root).test_client()
        r = c.post("/api/preferences/research-dataset", json={"dataset_id": self.duka5})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["preferred"]["dataset_id"], self.duka5)
        rows = c.get("/api/datasets").get_json()
        self.assertTrue(next(d for d in rows if d["dataset_id"] == self.duka5)["preferred"])
        self.assertEqual(next(d for d in rows if d["dataset_id"] == self.duka5)["identity"]["calendar_status"],
                         "regular_hours_verified")
        q = c.get(f"/api/datasets/{self.duka5}/quality")
        self.assertEqual(q.status_code, 200)
        self.assertIn("largest_gaps", q.get_json())
        bt = c.post("/api/backtests", json={"strategy": self.made["strategy_id"], "dataset_id": self.duka5})
        self.assertEqual((bt.status_code, bt.get_json()["error"]["kind"]), (409, "cost_unconfigured"))
        self.assertEqual(c.post("/api/preferences/research-dataset", json={"dataset_id": "../x"}).status_code, 400)
        r = c.post("/api/preferences/research-dataset", json={"dataset_id": None})
        self.assertEqual(r.get_json()["state"], "unset")


if __name__ == "__main__":
    unittest.main()
