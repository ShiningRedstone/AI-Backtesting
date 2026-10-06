"""Shocks on every timeframe (ADR-106): bars whose high-low is at least 3x the median of the SAME time slot over the
previous 20 trading dates. Overlapping shock bars of different timeframes form ONE episode (which timeframes saw it is
recorded: a 1-minute-only spike is not a 15-minute event).

Why (all tags that apply, the first is the main one):
  scheduled news   a USD calendar event (impact medium or high) released between 2 minutes before the bar and its end
                   (name, impact, surprise);
  session clock    the bar contains 18:00 (re-open), 2:00/3:00 (London), 8:30 (US data time), 9:30 (US open),
                   10:00 (US data time), 14:00 (FOMC time), 16:00 (US close);
  market-wide / NQ only   ES's bar over the same minutes was also >= 2x its usual size (or not);
  level run        the bar traded through the previous day's / week's high or low, or the Asia / London high or low;
  unexplained      none of the above: likely unscheduled news (no headline source is used; labelled as a guess).
Effect on the 15-minute chart: the 15-minute candle that contains the shock: its size against its usual size, how much
of the shock's move it kept at its close, the next four 15-minute candles' direction, and the minutes until price came
back to where the shock started.
"""
from __future__ import annotations

import numpy as np

from edgelab.market import data as D
from edgelab.market import patterns as P
from edgelab.market.trend import quantiles, rate

SHOCK_RATIO = 3.0
ES_RATIO = 2.0
CLOCK = ((18 * 60, "18:00 re-open"), (2 * 60, "2:00 London"), (3 * 60, "3:00 London"), (8 * 60 + 30, "8:30 US data"),
         (9 * 60 + 30, "9:30 US open"), (10 * 60, "10:00 US data"), (14 * 60, "14:00 FOMC time"),
         (16 * 60, "16:00 US close"))
INTRADAY = tuple(tf for tf in D.TIMEFRAMES if tf <= 240)


def detect(m: D.Minute, bars: dict, typ: dict, es: D.Minute | None, es_bars: dict | None, es_typ: dict | None,
           news: list[dict], lv: dict) -> list[dict]:
    cands = []
    for tf in INTRADAY:
        b = bars[tf]
        r = (b.h - b.l) / np.where(typ[tf] > 0, typ[tf], np.nan)
        for k in np.flatnonzero(r >= SHOCK_RATIO):
            cands.append((int(b.ts[k]), int(b.ts[k] + tf * D.MIN_NS), tf, int(k), float(r[k])))
    cands.sort()
    # merge overlapping bars into episodes
    eps = []
    for s, e, tf, k, r in cands:
        if eps and s < eps[-1]["end"]:
            ep = eps[-1]
            ep["end"] = max(ep["end"], e)
            ep["tfs"][tf] = max(ep["tfs"].get(tf, 0), r)
            if tf < ep["tf_min"]:
                ep["tf_min"], ep["k_min"] = tf, k
        else:
            eps.append({"start": s, "end": e, "tfs": {tf: r}, "tf_min": tf, "k_min": k})
    news_ts = np.array([n["ts"] for n in news], dtype=np.int64) if news else np.zeros(0, np.int64)
    b15 = bars[15]
    a15 = D.atr(b15)
    out = []
    for ep in eps:
        s, e = ep["start"], ep["end"]
        i0 = int(np.searchsorted(m.ts, s, side="left"))
        i1 = int(np.searchsorted(m.ts, e, side="left")) - 1
        if i1 < i0:
            continue
        seg = slice(i0, i1 + 1)
        move = m.c[i1] - m.o[i0]
        hi, lo = float(m.h[seg].max()), float(m.l[seg].min())
        tags = []
        if len(news_ts):
            j0 = np.searchsorted(news_ts, s - 2 * D.MIN_NS, side="left")
            j1 = np.searchsorted(news_ts, e, side="left")
            hits = [news[j] for j in range(j0, j1) if news[j]["impact"] >= 2]
            if hits:
                hits.sort(key=lambda x: -x["impact"])
                h0 = hits[0]
                tags.append({"tag": "scheduled news", "detail": h0["name"], "impact": h0["impact"],
                             "surprise_z": h0.get("surprise_z"), "others": [x["name"] for x in hits[1:4]]})
        ny0 = int(m.ny_min[i0])
        ny_end = ny0 + int((e - s) // D.MIN_NS)
        for minute, name in CLOCK:
            mm = minute if minute >= ny0 else minute + 1440
            if ny0 <= mm < ny_end or (minute == ny0):
                tags.append({"tag": "session clock", "detail": name})
                break
        if es is not None and es_bars is not None:
            j0 = np.searchsorted(es.ts, s, side="left")
            j1 = np.searchsorted(es.ts, e, side="left") - 1
            if j1 >= j0:
                tf = ep["tf_min"]
                eb = es_bars[tf]
                kk = np.searchsorted(eb.ts, s, side="right") - 1
                ok = 0 <= kk < len(eb) and es_typ[tf][kk] > 0
                er = float((es.h[j0:j1 + 1].max() - es.l[j0:j1 + 1].min()) / es_typ[tf][kk]) if ok else None
                if er is not None:
                    tags.append({"tag": "market-wide" if er >= ES_RATIO else "NQ only",
                                 "detail": f"ES {er:.1f}x its usual size"})
        di = int(np.clip(np.searchsorted(lv["days"], m.day[i0]), 0, len(lv["days"]) - 1))
        for key, name in (("pdh", "previous day high"), ("pdl", "previous day low"), ("pwh", "previous week high"),
                          ("pwl", "previous week low"), ("asia_hi", "Asia high"), ("asia_lo", "Asia low"),
                          ("lon_hi", "London high"), ("lon_lo", "London low")):
            v = lv[key][di]
            final = (not key.startswith(("asia", "lon"))) or \
                (120 <= ny0 < 1080 if key.startswith("asia") else 420 <= ny0 < 1080)   # session levels once complete
            if final and np.isfinite(v) and lo < v < hi:
                tags.append({"tag": "level run", "detail": name})
                break
        main = tags[0]["tag"] if tags and tags[0]["tag"] in ("scheduled news",) else None
        if main is None:
            kinds = [t["tag"] for t in tags]
            main = "session clock" if "session clock" in kinds else ("level run" if "level run" in kinds else None)
        if main is None:
            main = "unexplained" if not any(t["tag"] == "market-wide" for t in tags) else "unexplained (market-wide)"
        # effect on the 15-minute chart
        k15 = int(np.clip(np.searchsorted(b15.ts, s, side="right") - 1, 0, len(b15) - 1))
        kept = float((b15.c[k15] - m.o[i0]) / move) if move else None
        nxt = [int(np.sign(b15.c[k] - b15.o[k]) * np.sign(move)) for k in range(k15 + 1, min(k15 + 5, len(b15)))]
        back = D.first_touch(m, [i1 + 1], [m.o[i0]], [-1 if move > 0 else 1])[0]
        back_min = float((m.ts[back] - e) / D.MIN_NS) if back >= 0 else None
        out.append({"start": s, "end": e, "tfs": {D.TF_LABEL[k]: round(v, 2) for k, v in sorted(ep["tfs"].items())},
                    "tf_min": D.TF_LABEL[ep["tf_min"]], "biggest_tf": D.TF_LABEL[max(ep["tfs"])],
                    "move_pts": float(move), "range_pts": hi - lo, "session": P.SESSION_NAMES[int(P.session_code([ny0])[0])],
                    "main": main, "tags": tags, "k15": k15, "m15_kept": kept, "m15_next4": nxt,
                    "minutes_to_return": back_min, "atr15": float(a15[max(0, k15 - 1)]) if len(a15) else None})
    if out:
        typ15 = D.typical_by_slot(b15, b15.h - b15.l)
        for x in out:
            t = typ15[x["k15"]]
            x["m15_size"] = float((b15.h[x["k15"]] - b15.l[x["k15"]]) / t) if t and np.isfinite(t) and t > 0 else None
    return out


def summarize(shocks: list[dict], n_days: int) -> dict:
    def block(rows):
        kept = np.array([r["m15_kept"] for r in rows if r["m15_kept"] is not None])
        nxt = np.array([v for r in rows for v in r["m15_next4"][:1]])
        size = [r["m15_size"] for r in rows if r.get("m15_size") is not None]
        ret = [r["minutes_to_return"] for r in rows if r["minutes_to_return"] is not None]
        return {"n": len(rows), "per_day": len(rows) / max(1, n_days),
                "changed_15m": rate(sum(1 for s in size if s >= 1.5), len(size)),
                "m15_size": quantiles(size), "kept_at_15m_close": quantiles(kept),
                "reversed_by_15m_close": rate(int((kept < 0).sum()), len(kept)),
                "next_15m_continues": rate(int((nxt > 0).sum()), int((nxt != 0).sum())),
                "returned_to_start": rate(len(ret), len(rows)), "minutes_to_return": quantiles(ret)}
    by_main: dict = {}
    for r in shocks:
        by_main.setdefault(r["main"], []).append(r)
    by_tf: dict = {}
    for r in shocks:
        by_tf.setdefault(r["biggest_tf"], []).append(r)
    by_news: dict = {}
    for r in shocks:
        for t in r["tags"]:
            if t["tag"] == "scheduled news":
                by_news.setdefault(t["detail"], []).append(r)
    return {"all": block(shocks), "by_cause": {k: block(v) for k, v in by_main.items()},
            "by_largest_timeframe": {k: block(v) for k, v in by_tf.items()},
            "by_event": sorted([{"event": k, **block(v)} for k, v in by_news.items()], key=lambda x: -x["n"])[:40],
            "recent": [{k: v for k, v in r.items() if k not in ("k15",)} for r in shocks[-300:]]}
