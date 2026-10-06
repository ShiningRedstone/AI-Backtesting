"""What scheduled USD news did to NQ (ADR-106). For every release inside the data:

* BEFORE: the 30 minutes before the release (move and size against usual for that time);
* the release MINUTE, the first 5 minutes and the 15-minute candle that contains it: size against the usual size of
  that slot (the median of the previous 20 trading dates at the same time), direction;
* AFTER: the next hour: did the first 5-minute move continue or reverse;
* IMPACTED = the release's 15-minute candle was at least 1.5x its usual size;
* direction against the SURPRISE (actual - forecast, scaled by that event's earlier surprises): for each event type,
  how often a positive surprise pushed NQ up in the first 5 minutes (the sign of the relationship is measured, not
  assumed: higher inflation can be bad for stocks, higher payrolls good or bad);
* WHY NOT (no impact): the surprise was small (|z| < 0.5), another release in the same minute, quiet or wild hour
  before, or simply no reaction - each counted.
"""
from __future__ import annotations

import numpy as np

from edgelab.market import data as D
from edgelab.market.trend import quantiles, rate

IMPACT_RATIO = 1.5


def analyse(m: D.Minute, bars: dict, typ: dict, events: list[dict]) -> dict:
    b15, b5, b1 = bars[15], bars[5], bars[1]
    rows = []
    for e in events:
        t = e["ts"]
        i = int(np.searchsorted(m.ts, t, side="left"))
        if i >= len(m) or m.ts[i] - t > 2 * D.MIN_NS or i < 30:
            continue                                              # no bar at the release (closed market / gap)
        k15 = int(np.searchsorted(b15.ts, t, side="right") - 1)
        k5 = int(np.searchsorted(b5.ts, t, side="right") - 1)
        if k15 < 0 or k5 < 0:
            continue
        t15, t5 = typ[15][k15], typ[5][k5]
        size15 = float((b15.h[k15] - b15.l[k15]) / t15) if t15 and np.isfinite(t15) and t15 > 0 else None
        j5 = min(i + 5, len(m) - 1)
        first5 = float(m.c[j5 - 1] - m.o[i])
        j60 = min(i + 65, len(m) - 1)
        later = float(m.c[j60] - m.c[j5 - 1])
        pre = float(m.c[i - 1] - m.o[i - 30])
        k1 = int(np.searchsorted(b1.ts, t, side="left"))
        t1 = typ[1][min(k1, len(b1) - 1)]
        minute = float((m.h[i] - m.l[i]) / t1) if t1 and np.isfinite(t1) and t1 > 0 else None
        rows.append({"ts": t, "name": e["name"], "impact": e["impact"], "surprise_z": e.get("surprise_z"),
                     "surprise": e.get("surprise"), "size15": size15, "minute_size": minute, "first5": first5,
                     "next_hour": later, "pre30": pre, "impacted": size15 is not None and size15 >= IMPACT_RATIO,
                     "same_minute": 0})
    if rows:
        ts = np.array([r["ts"] for r in rows])
        for r in rows:
            r["same_minute"] = int((ts == r["ts"]).sum() - 1)
    out = {"releases": len(rows), "by_impact": {}, "by_event": []}
    for imp in (3, 2, 1):
        rr = [r for r in rows if r["impact"] == imp]
        out["by_impact"][str(imp)] = _block(rr)
    by: dict = {}
    for r in rows:
        by.setdefault(r["name"], []).append(r)
    ev = []
    for name, rr in by.items():
        if len(rr) < 4:
            continue
        blk = _block(rr)
        blk["name"] = name
        blk["impact"] = max(r["impact"] for r in rr)
        ev.append(blk)
    ev.sort(key=lambda x: (-x["impact"], -(x["impacted"]["p"] or 0)))
    out["by_event"] = ev
    out["recent"] = rows[-200:]
    return out


def _block(rr: list[dict]) -> dict:
    sized = [r for r in rr if r["size15"] is not None]
    imp = [r for r in sized if r["impacted"]]
    no = [r for r in sized if not r["impacted"]]
    z = np.array([r["surprise_z"] for r in rr if r["surprise_z"] is not None and r["first5"] != 0], float)
    f5 = np.array([r["first5"] for r in rr if r["surprise_z"] is not None and r["first5"] != 0], float)
    big = np.abs(z) >= 0.5
    agree = np.sign(z[big]) == np.sign(f5[big])
    cont = np.array([np.sign(r["first5"]) * np.sign(r["next_hour"]) for r in imp if r["first5"] and r["next_hour"]])
    why_not = {"small surprise (|z| < 0.5)": sum(1 for r in no if r["surprise_z"] is not None and abs(r["surprise_z"]) < 0.5),
               "no forecast / actual": sum(1 for r in no if r["surprise_z"] is None),
               "another release the same minute": sum(1 for r in no if r["same_minute"] > 0),
               "big surprise, still no reaction": sum(1 for r in no if r["surprise_z"] is not None and
                                                      abs(r["surprise_z"]) >= 0.5 and r["same_minute"] == 0)}
    return {"n": len(rr), "impacted": rate(len(imp), len(sized)), "size15": quantiles([r["size15"] for r in sized]),
            "minute_size": quantiles([r["minute_size"] for r in rr if r["minute_size"] is not None]),
            "first5_pts": quantiles([abs(r["first5"]) for r in rr]),
            "surprise_direction": {"positive_surprise_moves_nq_up": rate(int(agree.sum()), int(big.sum())),
                                   "note": "only releases with |surprise z| >= 0.5"},
            "first5_continues_next_hour": rate(int((cont > 0).sum()), int((cont != 0).sum())),
            "pre30_same_as_first5": rate(sum(1 for r in rr if r["pre30"] * r["first5"] > 0),
                                         sum(1 for r in rr if r["pre30"] * r["first5"] != 0)),
            "why_not": why_not}
