"""Audited source-quality exclusion windows (ADR-44): refusals, provenance, and the anomaly gate.

Synthetic data only, on a test-local source schedule and a test-local BID proxy instrument (nothing here
describes a real feed)."""
from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.data.calendar import calendar_from_config
from edgelab.data.exclusions import ExclusionError, apply_exclusions, parse_exclusion_set
from edgelab.data.importer import ImportFailed, ImportOptions, import_dataset, load_validated
from edgelab.data.store import SQLiteStore
from edgelab.data.validation import DataIntegrityError, validate_and_freeze, validate_bars
from edgelab.engine.costs import CostConfigError, cost_model_from_config
from edgelab.instruments import load_instruments
from tests.helpers import CALENDARS, CFG, INSTRUMENTS
from tests.phase2_helpers import TEST_FEED, TEST_PROXY, with_test_proxy

NY = "America/New_York"
# Test-local source schedules (a feed's measured hours, NOT exchange hours): one closes 16:15 NY, one 17:00 NY.
CAL_DEFS = {"TEST_SRC_1615": {"timezone": NY, "session_open": "18:00", "session_close": "16:15",
                              "trading_weekdays": ["mon", "tue", "wed", "thu", "fri"], "holidays": [],
                              "early_closes": {}},
            "TEST_SRC_1700": {"timezone": NY, "session_open": "18:00", "session_close": "17:00",
                              "trading_weekdays": ["mon", "tue", "wed", "thu", "fri"], "holidays": [],
                              "early_closes": {}}}
R2, R1 = (calendar_from_config(n, CAL_DEFS[n]) for n in ("TEST_SRC_1615", "TEST_SRC_1700"))
HD = load_instruments(with_test_proxy(CFG))[TEST_PROXY]          # test-local BID proxy on a 0.001 grid
REASON = "test: early 17:00 reopen"


def ny(s):
    return pd.Timestamp(s, tz=NY).tz_convert("UTC")


def win(start, end, reason=REASON):
    return {"start": start, "end": end, "reason": reason}


def week(extra_days=(), start="2022-03-13 18:00", end="2022-03-18 16:14", cal=R2):
    """One week of the 16:15-close schedule (spring US DST week) plus 17:00-17:59 NY bars on the given dates."""
    ts = cal.expected_bar_opens(ny(start), ny(end), 1)
    for d in extra_days:
        ts = ts.append(pd.date_range(ny(f"{d} 17:00"), periods=60, freq="1min"))
    ts = ts.sort_values()
    px = 14000.0 + np.arange(len(ts)) * 0.001
    return pd.DataFrame({"ts": ts, "open": px, "high": px + 0.5, "low": px - 0.5, "close": px,
                         "volume": np.nan})


SPEC = {"windows": [win("2022-03-14T17:00:00-04:00", "2022-03-14T18:00:00-04:00"),
                    win("2022-03-15T17:00:00-04:00", "2022-03-15T18:00:00-04:00")]}


class TestWindowDefinitions(unittest.TestCase):
    def refuses(self, spec, text):
        with self.assertRaisesRegex(ExclusionError, text):
            parse_exclusion_set("T", spec)

    def test_malformed_sets_refused(self):
        self.refuses({}, "non-empty list")
        self.refuses({"windows": []}, "non-empty list")
        self.refuses({"windows": [{"start": "2022-03-14T17:00:00-04:00", "end": "2022-03-14T18:00:00-04:00"}]},
                     "exactly the keys")
        self.refuses({"windows": [win("2022-03-14T17:00:00-04:00", "2022-03-14T18:00:00-04:00", " ")]}, "reason")
        self.refuses({"windows": [win("2022-03-14T17:00:00", "2022-03-14T18:00:00-04:00")]}, "no UTC offset")
        self.refuses({"windows": [win(pd.Timestamp("2022-03-14 21:00", tz="UTC").to_pydatetime(),
                                      "2022-03-14T18:00:00-04:00")]}, "quoted strings")
        self.refuses({"windows": [win("yesterday-ish", "2022-03-14T18:00:00-04:00")]}, "unparseable")

    def test_reversed_and_empty_windows_refused(self):
        self.refuses({"windows": [win("2022-03-14T18:00:00-04:00", "2022-03-14T17:00:00-04:00")]}, "not before")
        self.refuses({"windows": [win("2022-03-14T17:00:00-04:00", "2022-03-14T17:00:00-04:00")]}, "not before")

    def test_overlaps_refused_not_merged(self):
        self.refuses({"windows": [win("2022-03-14T17:00:00-04:00", "2022-03-14T17:40:00-04:00"),
                                  win("2022-03-14T17:30:00-04:00", "2022-03-14T18:00:00-04:00")]}, "overlap")
        # the same instant written with a different offset still overlaps
        self.refuses({"windows": [win("2022-03-14T17:00:00-04:00", "2022-03-14T18:00:00-04:00"),
                                  win("2022-03-14T21:30:00+00:00", "2022-03-14T21:45:00+00:00")]}, "overlap")
        touching = parse_exclusion_set("T", {"windows": [
            win("2022-03-14T17:30:00-04:00", "2022-03-14T18:00:00-04:00"),
            win("2022-03-14T17:00:00-04:00", "2022-03-14T17:30:00-04:00")]})
        self.assertEqual([w["start"] for w in touching],                          # half-open: may touch
                         ["2022-03-14T17:00:00-04:00", "2022-03-14T17:30:00-04:00"])


class TestApply(unittest.TestCase):
    def test_removes_exactly_the_window_bars_and_records_them(self):
        df = week(["2022-03-14", "2022-03-15"])
        kept, rec = apply_exclusions(df, R2, "T", SPEC)
        self.assertEqual((rec["rows_before"], rec["rows_excluded"], rec["rows_after"]), (len(df), 120, len(df) - 120))
        self.assertEqual(len(kept), len(df) - 120)
        self.assertEqual([w["rows_excluded"] for w in rec["windows"]], [60, 60])
        self.assertEqual(rec["windows"][0]["start_utc"], "2022-03-14T21:00:00+00:00")
        self.assertEqual({w["reason"] for w in rec["windows"]}, {REASON})
        self.assertEqual((rec["set"], rec["calendar"]), ("T", "TEST_SRC_1615"))
        self.assertEqual(len(rec["excluded_rows_sha256"]), 64)
        self.assertTrue(R2.in_session(pd.DatetimeIndex(kept["ts"])).all())
        # identical definition -> identical set hash; a changed reason is a different set
        _, rec2 = apply_exclusions(df, R2, "T", copy.deepcopy(SPEC))
        self.assertEqual(rec["set_hash"], rec2["set_hash"])
        other = {"windows": [dict(w, reason="other") for w in SPEC["windows"]]}
        self.assertNotEqual(rec["set_hash"], apply_exclusions(df, R2, "T", other)[1]["set_hash"])

    def test_window_touching_in_session_bars_refused(self):
        df = week(["2022-03-14"])
        spec = {"windows": [win("2022-03-14T16:00:00-04:00", "2022-03-14T18:00:00-04:00")]}   # 16:00-16:14 in session
        with self.assertRaisesRegex(ExclusionError, "15 bar\\(s\\) inside calendar 'TEST_SRC_1615'"):
            apply_exclusions(df, R2, "T", spec)
        spec = {"windows": [win("2022-03-14T17:00:00-04:00", "2022-03-14T18:01:00-04:00")]}   # 18:00 reopen
        with self.assertRaisesRegex(ExclusionError, "inside calendar"):
            apply_exclusions(df, R2, "T", spec)

    def test_window_matching_no_bar_refused(self):
        with self.assertRaisesRegex(ExclusionError, "matches no bar"):
            apply_exclusions(week(["2022-03-14"]), R2, "T", SPEC)        # 03-15 has no anomaly


class TestAnomalyGate(unittest.TestCase):
    def test_anomaly_fails_without_and_passes_with_exclusion(self):
        df = week(["2022-03-14", "2022-03-15"])
        with self.assertRaises(DataIntegrityError) as ctx:
            validate_and_freeze(df, HD, R2, "1m", 1, TEST_FEED, "NOEXCL", CFG["validation"])
        chk = {c.name: c for c in ctx.exception.report.checks}
        self.assertEqual((chk["bars_outside_session"].status, chk["bars_outside_session"].count), ("FAIL", 120))
        kept, _ = apply_exclusions(df, R2, "T", SPEC)
        ds = validate_and_freeze(kept, HD, R2, "1m", 1, TEST_FEED, "EXCL", CFG["validation"])
        chk = {c.name: c for c in ds.report.checks}
        self.assertEqual((chk["bars_outside_session"].count, chk["missing_bars"].count), (0, 0))
        self.assertEqual((chk["grid_alignment"].status, chk["tick_alignment"].status), ("PASS", "PASS"))

    def test_sparse_outside_bars_still_warn_without_exclusion(self):
        """A few stray outside bars below the 0.1% threshold stay a WARN, untouched."""
        df = week(start="2018-03-04 18:00", end="2018-03-09 16:59", cal=R1)
        stray = pd.DataFrame({"ts": [ny("2018-03-05 17:10"), ny("2018-03-06 17:20")], "open": 14000.0,
                              "high": 14000.5, "low": 13999.5, "close": 14000.0, "volume": np.nan})
        df = pd.concat([df, stray]).sort_values("ts").reset_index(drop=True)
        rep = validate_bars(df, HD, R1, 1, CFG["validation"])
        chk = {c.name: c for c in rep.checks}
        self.assertEqual((chk["bars_outside_session"].status, chk["bars_outside_session"].count), ("WARN", 2))
        self.assertNotEqual(rep.status, "FAIL")

    def test_shipped_config(self):
        v = CFG["validation"]                          # thresholds unchanged by ADR-44
        self.assertEqual((v["max_outside_session_ratio_fail"], v["max_missing_bar_ratio_warn"],
                          v["max_missing_bar_ratio_fail"]), (0.001, 0.001, 0.05))
        for name, spec in (CFG.get("source_exclusions") or {}).items():   # any shipped set is well formed
            for w in parse_exclusion_set(name, spec):
                self.assertTrue(w["reason"])
        # HistData removed (ADR-86) from NEW workspaces; the shipped files keep it only inside histdata-legacy blocks
        # so a workspace that is the source clone keeps its settings fingerprint (ADR-89, tests/test_config_legacy.py)
        from edgelab.core.config import load_config
        from edgelab.runtime import copy_default_configs
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        copy_default_configs(Path(__file__).resolve().parents[1] / "configs", tmp / "configs")
        fresh = load_config(tmp / "configs", environ={})
        for section in (fresh["calendars"], fresh["instruments"], fresh["costs"]["symbols"],
                        fresh.get("source_exclusions") or {}):
            self.assertFalse([k for k in section if "HISTDATA" in k.upper()])
        self.assertEqual((INSTRUMENTS["NAS100_CFD"].tick_size, INSTRUMENTS["NAS100_CFD"].calendar), (0.01, "CME_EQUITY"))
        c = CALENDARS["CME_EQUITY"]
        self.assertEqual((c.session_open, c.session_close), ("18:00", "17:00"))
        with self.assertRaises(CostConfigError):       # proxy symbol level stays unconfigured; only its feed is set
            cost_model_from_config(with_test_proxy(CFG), TEST_PROXY)


class TestImportProvenance(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = SQLiteStore(self.tmp / "s.sqlite")
        df = week(["2022-03-14", "2022-03-15"])
        loc = pd.DatetimeIndex(df["ts"]).tz_convert(NY)
        out = pd.DataFrame({"ts": loc.strftime("%Y%m%d %H%M%S"), "open": df["open"].round(3),
                            "high": df["high"].round(3), "low": df["low"].round(3), "close": df["close"].round(3),
                            "volume": 0})
        self.file = self.tmp / "hd.csv"
        out.to_csv(self.file, sep=";", index=False)
        self.cfg = with_test_proxy(CFG)
        self.cfg["calendars"]["TEST_SRC_1615"] = copy.deepcopy(CAL_DEFS["TEST_SRC_1615"])
        self.cfg["source_exclusions"] = {"TEST_SET": SPEC}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def opts(self, **kw):
        return ImportOptions(str(self.file), TEST_PROXY, TEST_FEED, "CFD", "1m", source_timezone=NY,
                             calendar="TEST_SRC_1615",
                             delimiter=";", datetime_format="%Y%m%d %H%M%S", volume_type="none",
                             price_basis="bid", build_features=False, **kw)

    def test_import_without_exclusions_is_refused(self):
        with self.assertRaises(ImportFailed) as ctx:
            import_dataset(self.opts(), self.cfg, self.store)
        self.assertEqual(ctx.exception.stage, "validate")

    def test_import_with_exclusions_records_provenance(self):
        raw_before = self.file.read_bytes()
        r = import_dataset(self.opts(source_exclusions="TEST_SET", derive_timeframes=["5m"]), self.cfg, self.store)
        self.assertEqual(self.file.read_bytes(), raw_before)                        # source untouched
        m = r.manifest
        rec = m["source_detail"]["source_exclusions"]
        self.assertEqual((rec["rows_before"], rec["rows_excluded"], rec["rows_after"]),
                         (m["source_detail"]["raw_rows"], 120, m["n_bars"]))
        self.assertEqual(len(rec["windows"]), 2)
        self.assertIn("source exclusions TEST_SET (120 of", m["derivation"])
        self.assertTrue(m["source_file_sha256"])
        self.assertIn("exclude", [s["stage"] for s in r.stages])
        ds = load_validated(self.store, self.cfg, r.dataset_id)                     # gate re-runs on load
        self.assertEqual(ds.manifest.source_detail["source_exclusions"]["set_hash"], rec["set_hash"])
        derived = self.store.get_manifest(r.derived[0])
        self.assertEqual(derived.source_detail["source_exclusions"]["set_hash"], rec["set_hash"])

    def test_unknown_or_unsafe_set_fails_before_storing(self):
        with self.assertRaisesRegex(ImportFailed, "unknown exclusion set"):
            import_dataset(self.opts(source_exclusions="NOPE"), self.cfg, self.store)
        self.cfg["source_exclusions"]["BAD"] = {"windows": [win("2022-03-14T16:00:00-04:00",
                                                               "2022-03-14T18:00:00-04:00")]}
        with self.assertRaises(ImportFailed) as ctx:
            import_dataset(self.opts(source_exclusions="BAD"), self.cfg, self.store)
        self.assertEqual(ctx.exception.stage, "exclude")
        self.assertEqual(self.store.list_datasets(), [])


if __name__ == "__main__":
    unittest.main()
