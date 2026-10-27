"""Noise area (ADR-116): the intraday "noise band" of Zarattini, Aziz & Barbon, "Beat the Market: An Effective Intraday
Momentum Strategy for S&P500 ETF (SPY)" (2024), on the bars of the strategy's own timeframe.

For a bar of the New York regular session (opening at 9:30 ... before 16:00) of trading date d, at slot s (minutes from
9:30 to the bar's open):

    move(d', s)  = |close(d', s) / open_930(d') - 1|                       (close of the slot-s bar of an earlier day)
    sigma(d, s)  = mean of move(d', s) over the ``lookback`` previous trading dates that had a 9:30 bar
                   (at least ceil(0.7 x lookback) of them must have that slot, else NaN)
    upper        = max(open_930(d), prev_close) x (1 + mult x sigma)       (``gap``: with the previous RTH close)
    lower        = min(open_930(d), prev_close) x (1 - mult x sigma)
    gap off      : open_930(d) x (1 +/- mult x sigma)

``prev_close`` = the close of the previous trading date's last regular-session bar. Only earlier days enter ``sigma``;
the bar's own close is compared with the band at its close, so everything is known at the bar close. Outside the
regular session, and on a day without a 9:30 bar, every output is NaN.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from edgelab.features.library._util import NAN
from edgelab.features.spec import FeatureDef, Param, register

RTH_START, RTH_END = 9 * 60 + 30, 16 * 60


def _noise(inp, p):
    b, cal = inp.bars, inp.calendar
    n = len(b)
    out = {k: np.full(n, NAN) for k in ("upper", "lower", "sigma", "rth_open", "prev_rth_close", "slot")}
    if n == 0:
        return out
    ny = b.ts.tz_convert("America/New_York")
    mod = np.asarray(ny.hour * 60 + ny.minute, dtype=np.int64)
    td = cal.trading_dates_of(b).astype("datetime64[D]").astype(np.int64)
    nyday = np.asarray(ny.normalize().tz_localize(None).values.astype("datetime64[D]").astype(np.int64))
    rth = (mod >= RTH_START) & (mod < RTH_END) & (nyday == td)
    if not rth.any():
        return out
    idx = np.flatnonzero(rth)
    slot = mod[idx] - RTH_START
    days = td[idx]
    o, c = np.asarray(b.open, float)[idx], np.asarray(b.close, float)[idx]
    df = pd.DataFrame({"i": idx, "d": days, "s": slot, "o": o, "c": c})
    first = df[df["s"] == 0].drop_duplicates("d").set_index("d")["o"]             # the 9:30 open of each date
    df["open930"] = df["d"].map(first)
    last_close = df.groupby("d", sort=True)["c"].last()                            # last regular-session close per date
    dates = np.array(sorted(first.index.to_numpy()))                               # dates that had a 9:30 bar
    prev_close = pd.Series(last_close.shift(1).to_numpy(), index=last_close.index)
    df["prev_close"] = df["d"].map(prev_close)
    df["move"] = (df["c"] / df["open930"] - 1.0).abs()
    L = int(p["lookback"])
    need = int(math.ceil(0.7 * L))
    mv = df[df["d"].isin(dates)].pivot_table(index="d", columns="s", values="move", aggfunc="last")
    mv = mv.reindex(dates)
    sig = mv.rolling(L, min_periods=need).mean().shift(1)                          # previous L dates only
    rows = sig.index.get_indexer(df["d"].to_numpy())
    cols = sig.columns.get_indexer(df["s"].to_numpy())
    vals = sig.to_numpy(dtype=float)
    ok = (rows >= 0) & (cols >= 0)
    sigma = np.full(len(df), NAN)
    sigma[ok] = vals[rows[ok], cols[ok]]
    k = float(p["mult"])
    op = df["open930"].to_numpy(float)
    pc = df["prev_close"].to_numpy(float)
    if p["gap"]:
        hi = np.where(np.isfinite(pc), np.fmax(op, pc), NAN)
        lo = np.where(np.isfinite(pc), np.fmin(op, pc), NAN)
    else:
        hi, lo = op, op
    up, dn = hi * (1.0 + k * sigma), lo * (1.0 - k * sigma)
    out["upper"][idx], out["lower"][idx], out["sigma"][idx] = up, dn, sigma
    out["rth_open"][idx], out["prev_rth_close"][idx], out["slot"][idx] = op, pc, slot.astype(float)
    return out


register(FeatureDef(
    feature_id="noise_area", version=1, category="session",
    params=(Param("lookback", 14, int, "previous trading dates averaged per time-of-day slot", min=2),
            Param("mult", 1.0, float, "band width multiplier of the average move", min=0.0),
            Param("gap", True, bool, "bands from max/min(9:30 open, previous regular-session close); off: the 9:30 open")),
    outputs=(("upper", "upper noise band at the bar's slot (NaN outside 9:30-16:00 New York)"),
             ("lower", "lower noise band at the bar's slot"),
             ("sigma", "average absolute move from the 9:30 open at this slot over the previous `lookback` dates"),
             ("rth_open", "the 9:30 New York open of the bar's trading date"),
             ("prev_rth_close", "close of the previous trading date's last regular-session bar"),
             ("slot", "minutes from 9:30 to the bar's open (NaN outside the regular session)")),
    compute=_noise,
    summary="Intraday noise area (Zarattini / Aziz / Barbon 2024): bands around the 9:30 open that widen by the "
            "average move seen at the same time of day on the previous days.",
    calculation="sigma(d, slot) = mean over the previous `lookback` dates of |close(slot) / open_930 - 1|; "
                "upper = max(open_930, prev close) x (1 + mult x sigma), lower = min(open_930, prev close) x "
                "(1 - mult x sigma) (gap off: the 9:30 open on both sides). Bars of the strategy's own timeframe.",
    edge_cases="Regular session = bars OPENING 9:30 ... before 16:00 New York on their trading date. A date without a "
               "9:30 bar has no bands and is not counted in later averages; a slot needs at least 70 % of the "
               "lookback dates to have it (half days are skipped there). On timeframes that do not start a bar at "
               "9:30 there are no bands.",
    warmup="`lookback` trading dates"))
