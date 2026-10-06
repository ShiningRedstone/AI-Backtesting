"""NQ against ES (ADR-106): how closely they move, who leads, when and why they diverge, how long until they line up
again and which market closes the gap. Both series are BID 1-minute bars of the Dukascopy index CFDs (USATECH = NQ
stand-in, USA500 = ES stand-in); only minutes that BOTH have are compared (nothing is filled).

Divergence, two definitions:
* MOVE GAP: on 5-minute bars, the last hour's NQ log move minus beta x ES's log move (beta from the previous 20 trading
  dates), divided by that gap's usual size (std over the previous 20 trading dates). |z| >= 2 opens an episode; it ends
  when |z| <= 0.5 (lined up again) or at the end of the session. Who closed it: the share of the gap's shrinkage that
  came from NQ's own move.
* SMT at swings (5m / 15m / 1h): NQ makes a higher swing high while ES's matching swing is lower (or the reverse; and
  the same for lows). Known when the swing is confirmed. Expected direction: a bearish SMT at highs, bullish at lows.
"""
from __future__ import annotations

import math

import numpy as np

from edgelab.market import data as D
from edgelab.market import patterns as P
from edgelab.market.trend import quantiles, rate

SMT_TFS = (5, 15, 60)


def _ret(b: D.Bars) -> np.ndarray:
    r = np.full(len(b), np.nan)
    if len(b) > 1:
        r[1:] = np.log(b.c[1:] / b.c[:-1])
        r[1:][b.day[1:] != b.day[:-1]] = np.nan                   # no return across the session break
    return r


def paired(nq: D.Minute, es: D.Minute) -> tuple[D.Minute, D.Minute]:
    a, b = D.align(nq, es)
    return (D.Minute(nq.ts[a], nq.o[a], nq.h[a], nq.l[a], nq.c[a]), D.Minute(es.ts[b], es.o[b], es.h[b], es.l[b], es.c[b]))


def analyse(nq: D.Minute, es: D.Minute) -> dict:
    pn, pe = paired(nq, es)
    out: dict = {"paired_minutes": int(len(pn)), "nq_minutes": int(len(nq)), "es_minutes": int(len(es))}
    if len(pn) < 1000:
        out["too_few"] = True
        return out
    rel = []
    for tf in (1, 5, 15, 60):
        bn, be = D.resample(pn, tf), D.resample(pe, tf)
        rn, re_ = _ret(bn), _ret(be)
        ok = np.isfinite(rn) & np.isfinite(re_)
        c = float(np.corrcoef(rn[ok], re_[ok])[0, 1]) if ok.sum() > 30 else None
        beta = float(np.cov(rn[ok], re_[ok])[0, 1] / np.var(re_[ok], ddof=1)) if ok.sum() > 30 else None
        same = np.sign(bn.c - bn.o) == np.sign(be.c - be.o)
        nz = (bn.c != bn.o) & (be.c != be.o)
        years = {}
        yr = bn.day.astype("datetime64[Y]").astype(int) + 1970
        for y in np.unique(yr[ok]):
            k = ok & (yr == y)
            if k.sum() > 30:
                years[int(y)] = float(np.corrcoef(rn[k], re_[k])[0, 1])
        rel.append({"tf": D.TF_LABEL[tf], "corr": c, "beta": beta, "same_direction": rate(int((same & nz).sum()),
                                                                                         int(nz.sum())), "by_year": years})
    out["relationship"] = rel
    # lead / lag on 1-minute returns: corr(NQ_t, ES_t+k); a peak at k > 0 means NQ moves first
    b1n, b1e = D.resample(pn, 1), D.resample(pe, 1)
    rn, re_ = _ret(b1n), _ret(b1e)
    lags = []
    for k in range(-5, 6):
        if k >= 0:
            x, y = rn[:len(rn) - k], re_[k:]
        else:
            x, y = rn[-k:], re_[:len(re_) + k]
        ok = np.isfinite(x) & np.isfinite(y)
        lags.append({"lag_min": k, "corr": float(np.corrcoef(x[ok], y[ok])[0, 1]) if ok.sum() > 30 else None,
                     "band": 1.96 / math.sqrt(max(1, int(ok.sum())))})
    out["lead_lag"] = lags
    out["divergence"] = move_gap(pn, pe)
    return out


def move_gap(pn: D.Minute, pe: D.Minute) -> dict:
    bn, be = D.resample(pn, 5), D.resample(pe, 5)
    n = len(bn)
    if n < 500:
        return {"too_few": True}
    ln, le = np.log(bn.c), np.log(be.c)
    days = np.unique(bn.day)
    di = np.searchsorted(days, bn.day)
    rn, re_ = _ret(bn), _ret(be)
    # beta per trading date from the previous 20 dates (causal)
    beta_d = np.full(len(days), np.nan)
    sd_d = np.full(len(days), np.nan)
    gaps_hist: list = []
    for d in range(len(days)):
        lo = np.searchsorted(di, max(0, d - 20))
        hi = np.searchsorted(di, d)
        x, y = rn[lo:hi], re_[lo:hi]
        ok = np.isfinite(x) & np.isfinite(y)
        if ok.sum() > 200:
            beta_d[d] = float(np.cov(x[ok], y[ok])[0, 1] / np.var(y[ok], ddof=1))
        if len(gaps_hist) >= 5:
            g = np.concatenate(gaps_hist[-20:])
            g = g[np.isfinite(g)]
            if len(g) > 50:
                sd_d[d] = float(np.std(g, ddof=1))
        k = np.arange(np.searchsorted(di, d), np.searchsorted(di, d + 1))
        if len(k) > 12 and np.isfinite(beta_d[d]):
            g = (ln[k[12:]] - ln[k[:-12]]) - beta_d[d] * (le[k[12:]] - le[k[:-12]])
            gaps_hist.append(g)
        else:
            gaps_hist.append(np.zeros(0))
    z = np.full(n, np.nan)
    gap = np.full(n, np.nan)
    for i in range(12, n):
        d = di[i]
        if di[i - 12] != d or not (np.isfinite(beta_d[d]) and np.isfinite(sd_d[d]) and sd_d[d] > 0):
            continue
        gap[i] = (ln[i] - ln[i - 12]) - beta_d[d] * (le[i] - le[i - 12])
        z[i] = gap[i] / sd_d[d]
    episodes = []
    i = 0
    while i < n:
        if np.isfinite(z[i]) and abs(z[i]) >= 2:
            s, sign = i, np.sign(z[i])
            j = i + 1
            peak = abs(z[i])
            while j < n and di[j] == di[s] and np.isfinite(z[j]) and abs(z[j]) > 0.5:
                peak = max(peak, abs(z[j]))
                j += 1
            closed = j < n and di[j] == di[s] and np.isfinite(z[j]) and abs(z[j]) <= 0.5
            end = j if closed else j - 1
            # who closed it: NQ's own move against the gap vs ES's move toward it (beta-scaled)
            d = di[s]
            dn = (ln[end] - ln[s]) * -sign
            de = beta_d[d] * (le[end] - le[s]) * sign
            share = dn / (dn + de) if closed and (dn + de) > 0 else None
            nq_next = (bn.c[min(end + 12, n - 1)] - bn.c[end]) if closed else None
            episodes.append({"start": int(bn.ts[s]), "minutes": int((end - s) * 5), "peak_z": float(peak),
                             "nq_ahead": bool(sign > 0), "closed": bool(closed), "nq_share": share,
                             "session": P.SESSION_NAMES[int(P.session_code(P.ny_minutes([bn.ts[s]]))[0])],
                             "nq_next_hour_pts": None if nq_next is None else float(nq_next)})
            i = j + 1
        else:
            i += 1
    days_n = len(days)
    closed = [e for e in episodes if e["closed"]]
    shares = np.array([e["nq_share"] for e in closed if e["nq_share"] is not None])
    by_sess = {}
    for e in episodes:
        by_sess.setdefault(e["session"], []).append(e)
    # after a closed gap where NQ was AHEAD: does NQ keep falling back (toward ES) over the next hour?
    back = [np.sign(e["nq_next_hour_pts"]) * (-1 if e["nq_ahead"] else 1) for e in closed if e["nq_next_hour_pts"]]
    back = np.array(back)
    return {"episodes": len(episodes), "per_day": len(episodes) / max(1, days_n),
            "closed": rate(len(closed), len(episodes)),
            "minutes_to_line_up": quantiles([e["minutes"] for e in closed]),
            "peak_z": quantiles([e["peak_z"] for e in episodes]),
            "nq_closed_it": rate(int((shares > 0.5).sum()), len(shares)),
            "nq_share": quantiles(shares),
            "nq_ahead": rate(sum(1 for e in episodes if e["nq_ahead"]), len(episodes)),
            "after_close_nq_keeps_reverting": rate(int((back > 0).sum()), int((back != 0).sum())),
            "by_session": [{"session": k, "n": len(v), "closed": rate(sum(1 for e in v if e["closed"]), len(v)),
                            "minutes": quantiles([e["minutes"] for e in v if e["closed"]])} for k, v in by_sess.items()],
            "recent": episodes[-200:]}


def smt_events(nq: D.Minute, es: D.Minute, fr_atr: dict) -> "P.Events":
    """SMT divergences at confirmed swings, as pattern events (kind SMT) on 5m / 15m / 1h."""
    pn, pe = paired(nq, es)
    ev = P.Events()
    for tf in SMT_TFS:
        bn, be = D.resample(pn, tf), D.resample(pe, tf)
        if len(bn) != len(be) or len(bn) < 10:
            continue
        a = D.atr(bn)
        sw = P.swings(bn)
        for key, side in (("hi", 1), ("lo", -1)):
            k = sw[key]
            if len(k) < 2:
                continue
            k1, k2 = k[:-1], k[1:]
            if side > 0:
                nq_more = bn.h[k2] > bn.h[k1]
                es_v1 = np.array([be.h[max(0, x - 2):x + 3].max() for x in k1])
                es_v2 = np.array([be.h[max(0, x - 2):x + 3].max() for x in k2])
                es_more = es_v2 > es_v1
            else:
                nq_more = bn.l[k2] < bn.l[k1]
                es_v1 = np.array([be.l[max(0, x - 2):x + 3].min() for x in k1])
                es_v2 = np.array([be.l[max(0, x - 2):x + 3].min() for x in k2])
                es_more = es_v2 < es_v1
            smt = (nq_more != es_more) & (bn.day[k2] == bn.day[k1])
            if not smt.any():
                continue
            kk = k2[smt]
            conf = np.minimum(kk + 2, len(bn) - 1)
            kn = bn.known_ns[conf]
            # map to the full NQ minute index of that time
            ent = np.searchsorted(nq.ts, kn, side="left")
            d = np.full(len(kk), -side, dtype=np.int8)
            flag = np.where(nq_more[smt], "NQ made the new extreme", "ES made the new extreme")
            lvl = bn.h[kk] if side > 0 else bn.l[kk]
            ev.add(kind="SMT", tf=tf, dir=d, known_ns=kn, i_min=ent, entry_i=ent, day=bn.day[kk].astype(np.int64),
                   session=P.session_code(P.ny_minutes(bn.ts[kk])), top=lvl, bottom=lvl, atr=a[conf],
                   edge=P.edge_outcome(nq, ent, d, a[conf], tf), flag=flag)
    return ev
