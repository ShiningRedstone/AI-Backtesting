"""Session-window and trading-day level features.

"Previous" levels are those of the most recent instance whose SCHEDULED end is at or
before the bar's close: a session's high/low are never exposed before the session is
over, even if the data happens to contain its final bar early.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from edgelab.features.library._util import NAN, last_completed, take_or_nan
from edgelab.features.spec import FeatureDef, Param, register


def _instance_levels(b, ts_close, member, inst, end_of_instance):
    """Running + previous-instance levels for instances labelled by ``inst`` (>= 0 inside)."""
    n = len(b)
    out = {k: np.full(n, NAN) for k in ("open", "high", "low", "bars_into",
                                        "prev_open", "prev_high", "prev_low", "prev_close")}
    if not member.any():
        return out
    df = pd.DataFrame({"i": inst[member], "o": b.open[member], "h": b.high[member],
                       "l": b.low[member], "c": b.close[member]})
    g = df.groupby("i", sort=False)
    out["open"][member] = g["o"].transform("first").to_numpy()
    out["high"][member] = g["h"].cummax().to_numpy()
    out["low"][member] = g["l"].cummin().to_numpy()
    out["bars_into"][member] = g.cumcount().to_numpy(float)
    agg = g.agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"))
    agg = agg.sort_index()
    ends = end_of_instance(agg.index.to_numpy(np.int64))
    order = np.argsort(ends, kind="stable")
    ends, agg = ends[order], agg.iloc[order]
    j = last_completed(ends, ts_close)
    for src, dst in (("o", "prev_open"), ("h", "prev_high"), ("l", "prev_low"), ("c", "prev_close")):
        out[dst] = take_or_nan(agg[src].to_numpy(float), j)
    return out


def _session(inp, p):
    w = inp.session(p["session"])
    b = inp.bars
    member, inst, into = w.membership(b.ts_ns)
    lv = _instance_levels(b, inp.ts_close_ns, member, inst, w.end_ns)
    return {"in_session": member.astype(float), "minutes_into": into,
            "bars_into": lv["bars_into"], "session_open": lv["open"],
            "session_high": lv["high"], "session_low": lv["low"],
            "prev_open": lv["prev_open"], "prev_high": lv["prev_high"],
            "prev_low": lv["prev_low"], "prev_close": lv["prev_close"]}


register(FeatureDef(
    feature_id="session", version=1, category="session",
    params=(Param("session", "NY_RTH", str, "name of a window in configs/sessions.yaml"),),
    outputs=(("in_session", "1 if the bar OPENS inside the window (local wall clock), else 0"),
             ("minutes_into", "wall-clock minutes since the window start (NaN outside)"),
             ("bars_into", "0-based count of available bars so far in this instance (NaN outside)"),
             ("session_open", "open of the instance's first available bar"),
             ("session_high", "running high of the instance incl. bar t"),
             ("session_low", "running low of the instance incl. bar t"),
             ("prev_open", "open of the last COMPLETED instance"),
             ("prev_high", "high of the last completed instance"),
             ("prev_low", "low of the last completed instance"),
             ("prev_close", "close of the last completed instance")),
    compute=_session, session_params=("session",),
    summary="Session window membership, running session levels, previous session levels.",
    calculation="Membership in the window's own timezone (DST-safe). Instance = local start date. "
                "Previous = latest instance with scheduled end <= bar close.",
    edge_cases="Instance levels use available bars only (missing bars are not invented). A holiday "
               "with no bars produces no instance, so 'previous' refers to the last one that traded. "
               "HTF bars are members if they OPEN inside the window.",
    warmup="one completed instance for prev_*"))


def _daily(inp, p):
    b, cal = inp.bars, inp.calendar
    td = cal.trading_dates(b.ts).astype(np.int64)
    member = np.ones(len(b), bool)

    def ends(days):
        return np.array([cal.session_bounds(pd.Timestamp(np.datetime64(int(d), "D")).date())[1]
                         .tz_convert("UTC").as_unit("ns").value for d in days], dtype=np.int64)

    lv = _instance_levels(b, inp.ts_close_ns, member, td, ends)
    return {"day_open": lv["open"], "day_high": lv["high"], "day_low": lv["low"],
            "bars_into_day": lv["bars_into"], "prev_day_open": lv["prev_open"],
            "prev_day_high": lv["prev_high"], "prev_day_low": lv["prev_low"],
            "prev_day_close": lv["prev_close"], "gap": lv["open"] - lv["prev_close"]}


register(FeatureDef(
    feature_id="daily_levels", version=1, category="session",
    params=(),
    outputs=(("day_open", "open of the trading date (calendar session, e.g. 18:00 ET)"),
             ("day_high", "running high of the trading date incl. bar t"),
             ("day_low", "running low of the trading date incl. bar t"),
             ("bars_into_day", "0-based bar count within the trading date"),
             ("prev_day_open", "previous completed trading date: open"),
             ("prev_day_high", "previous completed trading date: high"),
             ("prev_day_low", "previous completed trading date: low"),
             ("prev_day_close", "previous completed trading date: close"),
             ("gap", "day_open - prev_day_close")),
    compute=_daily,
    summary="Trading-date levels from the dataset's exchange calendar.",
    calculation="Trading date per calendar (CME: 18:00 ET previous day -> 17:00 ET). Previous = latest "
                "trading date whose scheduled session close <= bar close.",
    edge_cases="Depends on the calendar (holidays/early closes must be configured). For CFD feeds the "
               "calendar is whatever you assigned at import - check bars_outside_session in validation.",
    warmup="one completed trading date for prev_*"))
