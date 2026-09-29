"""Descriptive source-quality read model (Phase 9): gap classification and coverage.

Read-only over a ``ValidatedDataset``: nothing is filled, excluded, re-labelled or stored. It
explains the ``missing_bars`` / ``missing_trading_days`` checks of the validation report by
grouping the absent in-session bars into runs (gaps) and classifying each run by length and by
where it sits in its session, so the user can tell a data-feed outage from an unlisted holiday or a
calendar that is wrong for the source. Classification is a description, never evidence for an
exclusion: excluding anything still needs an audited ``source_exclusions`` set (ADR-44).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

QUALITY_VERSION = 1
LENGTH_CLASSES = (("single_bar", 1), ("short", 15), ("medium", 120), ("long", None))


def _length_class(n_bars: int, tf: int) -> str:
    minutes = n_bars * tf
    if n_bars == 1:
        return "single_bar"
    if minutes <= 15:
        return "short"
    if minutes <= 120:
        return "medium"
    return "long"


def gap_analysis(ds, max_listed: int = 200) -> dict:
    cal, tf = ds.calendar, ds.bars.tf_minutes
    ts = ds.bars.ts_ns
    if not len(ts):
        return {"quality_version": QUALITY_VERSION, "gaps": [], "summary": {}, "coverage": {}}
    idx = pd.DatetimeIndex(ts).tz_localize("UTC")
    expected = cal.expected_bar_opens(idx[0], idx[-1], tf)
    exp_ns = expected.asi8
    missing = np.setdiff1d(exp_ns, ts, assume_unique=True)
    step = tf * 60_000_000_000
    exp_pos = np.searchsorted(exp_ns, missing)
    # runs of consecutive EXPECTED bars (a run may span a session pause only if nothing was expected there)
    breaks = np.flatnonzero(np.diff(exp_pos) != 1) + 1 if len(missing) else np.array([], int)
    runs = np.split(np.arange(len(missing)), breaks) if len(missing) else []
    tds_exp = cal.trading_dates(expected).astype(str)
    have_td = set(np.unique(cal.trading_dates(idx)).astype(str).tolist())
    per_day_expected = pd.Series(1, index=tds_exp).groupby(level=0).size()
    gaps = []
    counts: dict = {"by_length": {}, "by_position": {}, "by_weekday": {}}
    for r in runs:
        first, last = missing[r[0]], missing[r[-1]]
        p0, p1 = exp_pos[r[0]], exp_pos[r[-1]]
        td = str(tds_exp[p0])
        n = len(r)
        day_total = int(per_day_expected.get(td, 0))
        if td not in have_td and n >= day_total:
            pos = "whole_trading_day"
        elif p0 == 0 or tds_exp[p0 - 1] != td:
            pos = "session_open"
        elif p1 == len(exp_ns) - 1 or tds_exp[p1 + 1] != td:
            pos = "session_close"
        else:
            pos = "intra_session"
        length = "whole_day" if pos == "whole_trading_day" else _length_class(n, tf)
        wd = pd.Timestamp(td).day_name()[:3]
        for k, v in (("by_length", length), ("by_position", pos), ("by_weekday", wd)):
            counts[k][v] = counts[k].get(v, 0) + 1
        gaps.append({"start": pd.Timestamp(first, tz="UTC").isoformat(),
                     "end": (pd.Timestamp(last, tz="UTC") + pd.Timedelta(nanoseconds=step)).isoformat(),
                     "missing_bars": n, "trading_date": str(td), "weekday": wd,
                     "length_class": length, "position": pos,
                     "likely": ("unlisted holiday or full-day outage (check the exchange/provider calendar)"
                                if pos == "whole_trading_day" else
                                "late open / early close vs the calendar (calendar may not match the source)"
                                if pos in ("session_open", "session_close") and n * tf >= 15 else
                                "feed gap (no bars published)")})
    gaps.sort(key=lambda g: (-g["missing_bars"], g["start"]))
    years = pd.DatetimeIndex(expected).year
    have_mask = np.isin(exp_ns, ts)
    by_year = {int(y): {"expected": int((years == y).sum()), "present": int(have_mask[years == y].sum())}
               for y in np.unique(years)}
    for y in by_year.values():
        y["coverage"] = round(y["present"] / y["expected"], 6) if y["expected"] else None
    n_days_exp = len(per_day_expected)
    return {"quality_version": QUALITY_VERSION, "calendar": cal.name, "timeframe_minutes": tf,
            "summary": {"expected_bars": int(len(exp_ns)), "present_in_session": int(have_mask.sum()),
                        "missing_bars": int(len(missing)),
                        "missing_ratio": round(len(missing) / max(len(exp_ns), 1), 6),
                        "n_gaps": len(gaps), **counts},
            "coverage": {"first_bar": idx[0].isoformat(), "last_bar": idx[-1].isoformat(),
                         "expected_trading_days": n_days_exp,
                         "trading_days_with_bars": int(len(have_td & set(per_day_expected.index))),
                         "missing_trading_days": sorted(set(per_day_expected.index) - have_td),
                         "by_year": by_year},
            "largest_gaps": gaps[:max_listed], "gaps_listed": min(len(gaps), max_listed),
            "note": "descriptive only: nothing is filled or excluded; exclusions need an audited "
                    "source_exclusions set (DATA_IMPORT.md)"}
