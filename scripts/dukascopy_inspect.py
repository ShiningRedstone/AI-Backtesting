"""READ-ONLY onboarding inspection of a Dukascopy-layout 1m CSV (Phase 9, Step 2).

Uses the production code paths only: `inspect_file` (layout, SHA-256, ordering, duplicates,
hour profiles) and, in memory, `normalize` -> `validate_and_freeze` -> `gap_analysis` against a
calendar. NOTHING is stored: no dataset, no report file, no store write; the CSV is only read
(its SHA-256 is checked before and after).

    python scripts/dukascopy_inspect.py <PATH_TO>\\nq_1min_5years.csv --root . [--calendar NAME] [--out report.json]

Its output is the evidence for verifying (or replacing) the session calendar before
`calendar_status: verified` is set for NQ_DUKASCOPY (DATA_IMPORT.md). A timestamp/OHLCV CSV carries no
symbol, asset class or price side: those come from the download call (USATECH.IDX/USD, feed E_NQ-100, BID).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edgelab.core.config import load_config                           # noqa: E402
from edgelab.data.calendar import load_calendars                       # noqa: E402
from edgelab.data.importer import ImportOptions, apply_profile, file_sha256, inspect_file, normalize, read_raw  # noqa: E402
from edgelab.data.quality import gap_analysis                          # noqa: E402
from edgelab.data.validation import DataIntegrityError, validate_and_freeze  # noqa: E402
from edgelab.instruments import load_instruments                       # noqa: E402

NY = "America/New_York"


def boundaries(ts: pd.DatetimeIndex) -> dict:
    """Session-boundary evidence in New York time: per-week first/last bar, daily pauses."""
    ny = ts.tz_convert(NY)
    s = pd.Series(1, index=ny)
    wk = ny.tz_localize(None).to_period("W-SAT")                                        # weeks Sun..Sat
    firsts = pd.Series(ny, index=ny).groupby(wk).min()
    lasts = pd.Series(ny, index=ny).groupby(wk).max()
    fmt = lambda x: f"{x.day_name()[:3]} {x.strftime('%H:%M')}"
    minute = ny.hour * 60 + ny.minute
    per_min = pd.Series(minute).value_counts().reindex(range(1440), fill_value=0)
    weekdays = ny.weekday < 5
    n_days = max(1, len(np.unique(ny[weekdays].date)))
    empty = [m for m in range(1440) if per_min[m] <= 0.02 * n_days]    # minutes almost never present
    runs, start = [], None
    for m in range(1441):
        on = m < 1440 and m in set(empty)
        if on and start is None:
            start = m
        if not on and start is not None:
            runs.append((start, m))
            start = None
    hhmm = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    gap = (np.diff(ts.asi8) / 60e9)
    big = pd.Series(gap[gap > 1]).value_counts().sort_index()
    return {
        "week_first_bar_ny": pd.Series([fmt(x) for x in firsts]).value_counts().head(8).to_dict(),
        "week_last_bar_ny": pd.Series([fmt(x) for x in lasts]).value_counts().head(8).to_dict(),
        "minutes_of_day_rarely_present_ny": [f"{hhmm(a)}-{hhmm(b)}" for a, b in runs],
        "bars_on_saturday_ny": int((ny.weekday == 5).sum()),
        "bars_by_weekday_ny": pd.Series(ny.day_name().str[:3]).value_counts().to_dict(),
        "gaps_between_consecutive_bars_minutes": {"n_gaps": int((gap > 1).sum()),
                                                  "most_common": {str(k): int(v) for k, v in big.sort_values(ascending=False).head(12).items()}},
        "rows_by_year": pd.Series(ts.year).value_counts().sort_index().to_dict(),
        "dst_offsets_seen_ny_hours": sorted({o.total_seconds() / 3600 for o in ny[:: max(1, len(ny) // 5000)].map(lambda x: x.utcoffset())}),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("--root", default=".")
    ap.add_argument("--profile", default="dukascopy_utc_csv")
    ap.add_argument("--instrument", default="NQ_DUKASCOPY")
    ap.add_argument("--calendar", default=None, help="calendar to test against (default: the instrument's)")
    ap.add_argument("--out", help="also write the full JSON report here (outside the source file's folder is fine)")
    a = ap.parse_args(argv)
    cfg = load_config(Path(a.root) / "configs")
    sha_before = file_sha256(a.file)
    o = apply_profile(ImportOptions(file=a.file, instrument=a.instrument, provider="DUKASCOPY", asset_type="unspecified",
                                    timeframe="1m", profile=a.profile), cfg)
    rep: dict = {"read_only": True, "file": a.file, "sha256": sha_before}
    rep["inspect_ny"] = inspect_file(o, cfg, view_tz=NY)
    rep["inspect_utc_session_profile"] = inspect_file(o, cfg, view_tz="UTC").get("session_profile")
    raw = read_raw(o)
    rep["missing_fields"] = {c: int(raw[c].isna().sum()) for c in raw.columns}
    df, facts = normalize(raw, o)
    rep["normalize_facts"] = facts
    ts = pd.DatetimeIndex(df["ts"]).tz_localize("UTC") if pd.DatetimeIndex(df["ts"]).tz is None else pd.DatetimeIndex(df["ts"])
    vol = df["volume"].to_numpy(float)
    rep["volume"] = {"min": float(np.nanmin(vol)), "median": float(np.nanmedian(vol)), "max": float(np.nanmax(vol)),
                     "share_zero": float(np.mean(vol == 0)), "share_non_integer": float(np.mean(vol % 1 != 0))}
    rep["boundaries"] = boundaries(ts.sort_values())
    inst = load_instruments(cfg)[a.instrument]
    cal_name = a.calendar or inst.calendar
    cal = load_calendars(cfg)[cal_name]
    rep["calendar_tested"] = cal_name
    try:
        ds = validate_and_freeze(df, inst, cal, "1m", 1, "DUKASCOPY", "DRY_RUN_NOT_STORED", cfg.get("validation"),
                                 source_detail={"dry_run": True}, volume_type=facts["volume_type"])
        rep["validation"] = ds.report.to_dict() if hasattr(ds.report, "to_dict") else str(ds.report)
        rep["gap_analysis"] = gap_analysis(ds, max_listed=1_000_000)       # every gap, for classification
    except DataIntegrityError as exc:
        rep["validation"] = exc.report.to_dict() if hasattr(exc.report, "to_dict") else str(exc.report)
        rep["validation_failed"] = True
        rep["gap_analysis"] = "not available: validation FAILED under this calendar (see validation checks)"
    rep["sha256_after"] = file_sha256(a.file)
    assert rep["sha256_after"] == sha_before, "source file changed during inspection"
    if a.out:
        Path(a.out).write_text(json.dumps(rep, indent=1, default=str))
    _summary(rep)
    return 0


def _summary(r: dict) -> None:
    i = r["inspect_ny"]
    print("==== DUKASCOPY READ-ONLY INSPECTION (nothing stored) ====")
    print(f"file {r['file']}\nsha256 {r['sha256']} (unchanged after: {r['sha256_after'] == r['sha256']})")
    print(f"rows {i['rows']:,}  columns {i['columns']}  missing fields {r['missing_fields']}")
    print(f"first {i.get('first_bar_open_utc')}  last {i.get('last_bar_open_utc')} (UTC)  monotonic {i.get('monotonic')}  "
          f"duplicate timestamps {i.get('duplicate_timestamps')}  spacing {i.get('inferred_bar_minutes')}m  "
          f"price decimals {i.get('price_decimals')}")
    print(f"volume {r['volume']}")
    b = r["boundaries"]
    for k in ("rows_by_year", "bars_by_weekday_ny", "bars_on_saturday_ny", "week_first_bar_ny", "week_last_bar_ny",
              "minutes_of_day_rarely_present_ny", "gaps_between_consecutive_bars_minutes", "dst_offsets_seen_ny_hours"):
        print(f"{k}: {b[k]}")
    for tz, prof in (("New York", i.get("session_profile")), ("UTC", r["inspect_utc_session_profile"])):
        if prof:
            print(f"\nbars per hour ({tz}):")
            print(pd.DataFrame(prof).T.to_string())
    v = r["validation"]
    print(f"\nvalidation under calendar {r['calendar_tested']}: {'FAILED' if r.get('validation_failed') else 'passes the gate'}")
    for c in (v.get("checks", []) if isinstance(v, dict) else []):
        if c["status"] != "PASS":
            print(f"  {c['status']:5s} {c['name']:26s} n={c['count']:,}  {c['message']}  e.g. {c.get('examples', [])[:3]}")
    g = r["gap_analysis"]
    if isinstance(g, dict):
        print(f"\ngaps: {g['summary']}\ncoverage: first {g['coverage']['first_bar']} last {g['coverage']['last_bar']} "
              f"days with bars {g['coverage']['trading_days_with_bars']}/{g['coverage']['expected_trading_days']}")
        print(f"missing trading days ({len(g['coverage']['missing_trading_days'])}): {g['coverage']['missing_trading_days'][:60]}")
        print(f"coverage by year: {g['coverage']['by_year']}")
        for x in g["largest_gaps"][:25]:
            print(f"  {x['start']}  {x['missing_bars']:5d} bars  {x['trading_date']} {x['weekday']}  {x['position']:17s} "
                  f"{x['length_class']:10s} +{x['minutes_after_session_open']}m after open, "
                  f"{x['minutes_before_session_close']}m before close")
        print(f"(all {g['gaps_listed']} gaps are in the --out JSON under gap_analysis.largest_gaps)")
    else:
        print(g)


if __name__ == "__main__":
    raise SystemExit(main())
