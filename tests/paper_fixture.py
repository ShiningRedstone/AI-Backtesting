"""SYNTHETIC stand-in for the Dukascopy downloader in paper-trading tests (never market data, never network).

``fake_fetcher(side, start, end)`` returns 1-minute bars for the DUKASCOPY_USATECH_OBSERVED schedule in [start, end):
values depend only on the bar's timestamp (a seeded walk from a fixed origin), so overlapping or repeated requests
return identical bars, exactly like a real archive. ASK = BID + a varying positive spread."""
from __future__ import annotations

import numpy as np
import pandas as pd

ORIGIN = pd.Timestamp("2023-01-01", tz="UTC")


def _minutes(start, end) -> pd.DatetimeIndex:
    idx = pd.date_range(pd.Timestamp(start).tz_convert("UTC"), pd.Timestamp(end).tz_convert("UTC"), freq="1min",
                        inclusive="left")
    loc = idx.tz_convert("America/New_York")
    wd, m = loc.weekday, loc.hour * 60 + loc.minute
    before_close, after_open = m < 16 * 60 + 15, m >= 18 * 60
    keep = ((wd <= 3) & (before_close | after_open)) | ((wd == 4) & before_close) | ((wd == 6) & after_open)
    return idx[keep]


def _bid(idx: pd.DatetimeIndex) -> pd.DataFrame:
    k = ((idx - ORIGIN) // pd.Timedelta(minutes=1)).to_numpy(np.int64)
    # a smooth, deterministic path with trends and swings (synthetic): sum of sines + a per-minute hash wiggle
    t = k.astype(float)
    base = 18000 + 60 * np.sin(t / 900.0) + 25 * np.sin(t / 97.0) + 8 * np.sin(t / 13.0)
    wig = ((k * 2654435761) % 1000) / 1000.0 - 0.5
    close = np.round(base + wig * 3, 3)
    open_ = np.round(base + np.roll(wig, 1) * 3, 3)
    high = np.round(np.maximum(open_, close) + 1.5 + np.abs(wig), 3)
    low = np.round(np.minimum(open_, close) - 1.5 - np.abs(wig), 3)
    vol = np.round(1 + np.abs(wig) * 10, 4)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


def fake_fetcher(side: str, start, end, *, holidays=()) -> pd.DataFrame:
    idx = _minutes(start, end)
    if holidays:
        loc = idx.tz_convert("America/New_York")
        td = (loc + pd.Timedelta(hours=6)).normalize().date      # trading date: 18:00 NY rolls to the next date
        idx = idx[[d not in set(holidays) for d in td]]
    df = _bid(idx)
    if side == "ASK":
        k = ((idx - ORIGIN) // pd.Timedelta(minutes=1)).to_numpy(np.int64)
        sp = 0.75 + (k % 4) * 0.25
        for c in ("open", "high", "low", "close"):
            df[c] = np.round(df[c].to_numpy() + sp, 3)
    df.index.name = "timestamp"
    return df
