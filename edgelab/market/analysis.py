"""The market-simulator analysis job (ADR-106): statistics of the DISCOVERY period, cached by its inputs.

Order: bars of every timeframe -> per-day levels -> patterns per timeframe from the HIGHEST to the lowest (so every
pattern sees the structure and the open FVGs of the higher timeframes as they were at that moment) -> effects on every
timeframe and on the 15-minute chart -> SMT -> edge scan -> trend / sessions, NQ vs ES, shocks, news. Never a run,
a try or a holdout look; the holdout is never loaded.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from edgelab.market import crosstf as X
from edgelab.market import data as D
from edgelab.market import edges as E
from edgelab.market import news as N
from edgelab.market import newsfx, nqes, shocks, trend
from edgelab.market import patterns as P

ANALYSIS_VERSION = 3                    # 2: ADR-107 fixes (costs per session, stricter shocks, chance levels, constant baseline ...)
                                        # 3: ADR-108 level map (FVG stacks, EQ / OTE, liquidity; reach, react, first, lands)
SEED = 20261006
BASE_SAMPLES = 4000
KIND_WORDS = {
    "FVG": "Fair value gap", "IFVG": "Inverse FVG", "BPR": "Balanced price range", "BOS": "Break of structure",
    "CHOCH": "Change of character", "OB": "Order block", "BREAKER": "Breaker", "OTE": "OTE retracement (62-79 %)",
    "SWING_SWEEP": "Swing high / low taken", "EQUAL_HL": "Equal highs / lows taken",
    "SWEEP_PREV_DAY": "Previous day high / low taken", "SWEEP_PREV_WEEK": "Previous week high / low taken",
    "SWEEP_PREV_RTH": "Previous regular-session high / low taken", "SWEEP_ASIA": "Asia high / low taken",
    "SWEEP_LONDON": "London high / low taken", "NDOG": "New day opening gap", "NWOG": "New week opening gap",
    "RTH_GAP": "9:30 opening gap", "OPENING_RANGE_15": "Opening range 9:30-9:45 break",
    "OPENING_RANGE_30": "Opening range 9:30-10:00 break", "JUDAS_30": "Judas swing (first 30 min reversed)",
    "IPDA_20": "20-day range taken", "IPDA_40": "40-day range taken", "IPDA_60": "60-day range taken",
    "SMT": "SMT divergence (NQ vs ES)"}
KIND_WORDS.update({k + "_FORMED": KIND_WORDS[k] + " (when it forms)" for k in ("FVG", "IFVG", "BPR", "OB", "BREAKER")})


def home(data_root) -> Path:
    p = Path(data_root) / "market"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_key(src: dict, news_sha: str | None, cfg_hash: str) -> str:
    from edgelab.core.identity import hash_obj
    return hash_obj({"v": ANALYSIS_VERSION, "nq": src["nq"]["content_hash"], "es": (src.get("es") or {}).get("content_hash"),
                     "window": src["window"], "news": news_sha, "config": cfg_hash, "seed": SEED}, 16)


def _jsonable(x):
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return [_jsonable(v) for v in x.tolist()]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        v = float(x)
        return v if math.isfinite(v) else None
    if isinstance(x, np.bool_):
        return bool(x)
    return x


# =============================================================================================== base rates
def base_rates(m: D.Minute, bars: dict, atrs: dict, rng: np.random.Generator) -> dict:
    """Per (timeframe, session): the chance of an UP outcome from random minutes (edge race, rest of the 15m candle)
    and the up-rate of 15-minute candles per session. The scan compares every cell with these."""
    out = {"edge": {}, "next15": {}, "rest15": {}}
    sc = P.session_code(m.ny_min)
    b15 = bars[15]
    k15 = np.clip(np.searchsorted(b15.ts, m.ts, side="right") - 1, 0, len(b15) - 1)
    for s in range(len(P.SESSION_NAMES)):
        idx = np.flatnonzero(sc == s)
        if not len(idx):
            continue
        pick = np.sort(rng.choice(idx, size=min(BASE_SAMPLES, len(idx)), replace=False))
        rest = np.sign(b15.c[k15[pick]] - m.o[pick])
        out["rest15"][s] = float((rest > 0).sum() / max(1, (rest != 0).sum()))
        ses15 = P.session_code(P.ny_minutes(b15.ts)) == s
        sg = np.sign(b15.c[ses15] - b15.o[ses15])
        out["next15"][s] = float((sg > 0).sum() / max(1, (sg != 0).sum()))
        for tf, b in bars.items():
            a = atrs[tf]
            j = np.searchsorted(b.known_ns, m.ts[pick], side="right") - 1
            at = np.where(j >= 0, a[np.maximum(j, 0)], np.nan)
            r = P.edge_outcome(m, pick, np.ones(len(pick), np.int8), at, tf)
            out["edge"][(tf, s)] = float((r > 0).sum() / max(1, (r != 0).sum()))
    return out


def _base_arrays(base: dict, tf: int, session: np.ndarray, d: np.ndarray) -> dict:
    s = session.astype(int)
    up_edge = np.array([base["edge"].get((tf, int(x)), 0.5) for x in range(len(P.SESSION_NAMES))])[s]
    up_n15 = np.array([base["next15"].get(int(x), 0.5) for x in range(len(P.SESSION_NAMES))])[s]
    up_r15 = np.array([base["rest15"].get(int(x), 0.5) for x in range(len(P.SESSION_NAMES))])[s]
    flip = lambda p: np.where(d > 0, p, 1 - p)
    return {"edge": flip(up_edge), "next15": flip(up_n15), "rest15": flip(up_r15)}


# =============================================================================================== the job
def run(svc, *, lock=None, progress=None, force: bool = False) -> dict:
    from edgelab.mystrategy import runner as R
    step = progress or (lambda s: None)
    step("Loading NQ and ES (discovery period only)")
    mk = D.discovery(svc, lock)
    cal = N.calendar(svc.data_root)
    news_ok = bool(cal and not cal.get("refused"))
    news_sha = cal["meta"]["sha256"] if news_ok else None
    key = cache_key(mk.source, news_sha, svc._config_hash())
    out_dir = home(svc.data_root) / f"analysis_{key}"
    done = out_dir / "summary.json"
    if done.exists() and not force:
        R._write_json(home(svc.data_root) / "latest.json", {"key": key})
        return json.loads(done.read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    m, es = mk.nq, mk.es
    news_all = N.events(svc.data_root, mk.start.value, mk.end.value) if news_ok else []
    high_ns = np.array(sorted(e["ts"] for e in news_all if e["impact"] >= 3), dtype=np.int64)
    rng = np.random.default_rng(SEED)
    step("Building every timeframe (1m ... 1D)")
    tfs_all = D.TIMEFRAMES + (D.DAY,)
    bars = D.all_timeframes(m, tfs_all)
    atrs = {tf: D.atr(b) for tf, b in bars.items()}
    lv = P.day_levels(m)
    fr = X.Frame(bars, breaks={}, zones={})
    n_days = len(np.unique(m.day))
    cut_ns = int(m.ts[0] + E.FIND_SHARE * (m.ts[-1] - m.ts[0]))
    cost_pts = _cost_points(svc, mk)
    step("Base rates from random minutes")
    base = base_rates(m, bars, atrs, rng)
    summaries, pvals, cells, matrix = [], [], [], []
    for tf in sorted(tfs_all, reverse=True):
        step(f"Patterns on the {D.TF_LABEL[tf]} chart, their effects on every other timeframe")
        f, breaks = P.detect_tf(m, bars[tf], atrs[tf], lv)
        if breaks:
            bk = sorted(breaks, key=lambda x: x["known_ns"])
            fr.breaks[tf] = (np.array([x["known_ns"] for x in bk], np.int64), np.array([x["dir"] for x in bk], np.int8))
        fv = f[f["kind"] == "FVG"]
        if len(fv):
            kn = fv["known_ns"].to_numpy(np.int64)
            fill = fv["fill_min"].to_numpy(float)
            order = np.argsort(kn, kind="stable")
            fr.zones[tf] = {"known_ns": kn[order],
                            "end_ns": np.where(np.isfinite(fill), kn + np.nan_to_num(fill) * D.MIN_NS, np.iinfo(np.int64).max)[order],
                            "top": fv["top"].to_numpy(float)[order], "bottom": fv["bottom"].to_numpy(float)[order],
                            "dir": fv["dir"].to_numpy(np.int8)[order]}
        _process(out_dir, f"tf{tf}", f, m, fr, lv, es, high_ns, base, cut_ns, cost_pts, n_days, mk, summaries, pvals,
                 cells, matrix)
    step("Day patterns (opening gaps, opening range, Judas swing, IPDA ranges)")
    ev = P.Events()
    facts = P.add_day_patterns(ev, m, lv, {"atr15": P.atr15_per_minute(m, bars[15])})
    _process(out_dir, "day", P._clean(ev.frame()), m, fr, lv, es, high_ns, base, cut_ns, cost_pts, n_days, mk,
             summaries, pvals, cells, matrix)
    res: dict = {"key": key, "version": ANALYSIS_VERSION, "computed_at": R._now(), "app_version": R._code_version(),
                 "source": mk.source, "days": n_days, "minutes": int(len(m)), "news": {"used": news_ok,
                 "events": len(news_all), "high": int(len(high_ns))}, "cost_points": cost_pts,
                 "kinds": KIND_WORDS, "sessions": list(P.SESSION_NAMES), "timeframes": [D.TF_LABEL[t] for t in tfs_all]}
    if es is not None and len(es) > 1000:
        step("NQ vs ES: relationship, who leads, divergences, SMT")
        res["nqes"] = nqes.analyse(m, es)
        smt = P._clean(nqes.smt_events(m, es, atrs).frame())
        _process(out_dir, "smt", smt, m, fr, lv, es, high_ns, base, cut_ns, cost_pts, n_days, mk, summaries, pvals,
                 cells, matrix)
    else:
        res["nqes"] = {"missing": True}
    step("Edge scan: found on the first 70 %, confirmed on the last 30 %")
    res["edges"] = E.finish(cells, pvals)
    res["patterns"] = summaries
    res["effect_matrix"] = matrix
    step("Trend days, sessions, 15-minute behaviour")
    news_days = None
    if news_ok:
        news_days = {str(x) for x in D.trading_day(high_ns)}
    res["trend"] = trend.analyse(m, bars, facts, news_days)
    step("Shocks on every timeframe: why, and what they did to the 15-minute chart")
    typ = fr.typ
    es_bars = es_typ = None
    if es is not None and len(es) > 1000:
        es_bars = D.all_timeframes(es, shocks.INTRADAY)
        es_typ = {tf: D.typical_by_slot(b, b.h - b.l) for tf, b in es_bars.items()}
    sh = shocks.detect(m, bars, typ, es, es_bars, es_typ, news_all, lv)
    res["shocks"] = shocks.summarize(sh, n_days)
    if news_ok:
        step("News: what each release did, and why it did or did not move the market")
        res["newsfx"] = newsfx.analyse(m, bars, typ, news_all)
    from edgelab.core.fsutil import atomic_write_text
    atomic_write_text(out_dir / "shocks.json", json.dumps(_jsonable(sh)))
    del fr, bars, atrs
    from edgelab.market import forecast as F
    cx = F.Context(m, es, news_all)
    res["forecast"] = F.run(cx, out_dir, step)
    from edgelab.market import levelmap as L
    res["levelmap"] = L.run(cx, out_dir, step)
    res = _jsonable(res)
    atomic_write_text(done, json.dumps(res))
    R._write_json(home(svc.data_root) / "latest.json", {"key": key})
    return res


def _cost_points(svc, mk) -> dict | None:
    """Round-trip cost of one MNQ contract in index points PER SESSION: the median ASK - BID spread of that session's
    minutes (spreads are much wider overnight than in New York hours) + commission, fees and slippage from the
    configured cost scenario. {'all': ..., 'by_session': [6 values]}; None when the costs are unconfigured."""
    try:
        from edgelab.engine.costs import cost_model_from_config
        from edgelab.instruments import contract_for, execution_view
        from edgelab.mystrategy import runner as R
        parent = R._parent(svc)
        ds = svc._cell_dataset(R.dataset_1m(svc, parent), (mk.start, mk.end))
        costs = cost_model_from_config(svc.cfg, ds.instrument.symbol, provider=ds.manifest.provider)
        if costs.status == "unconfigured":
            return None
        inst = execution_view(ds.instrument, contract_for(svc.cfg, {"contract": "MNQ"}))
        px = float(np.median(ds.bars.close))
        base = costs.round_trip_base("market", "market", 1, inst, spread_points=None, entry_price=px, exit_price=px)
        usd = (base["commission_usd"] + base["fees_usd"] + base["slippage_usd"] + base["spread_usd"]) * costs.multiplier
        fixed = usd / inst.point_value
        if not ds.bars.has_ask_ohlc:
            return {"all": fixed, "by_session": [fixed] * len(P.SESSION_NAMES), "fixed": fixed}
        spread = ds.bars.ask_close - ds.bars.close
        sess = P.session_code(P.ny_minutes(ds.bars.ts_ns))
        by = [float(np.median(spread[sess == s])) + fixed if (sess == s).any() else float(np.median(spread)) + fixed
              for s in range(len(P.SESSION_NAMES))]
        return {"all": float(np.median(spread)) + fixed, "by_session": by, "fixed": fixed}
    except Exception:                                                  # noqa: BLE001 - costs are optional here
        return None


ZONE_KINDS = ("FVG", "IFVG", "BPR", "OB", "BREAKER")


def measurable(f, m: D.Minute):
    """The rows whose ACT moment does not depend on the future: zones at their first touch (only touched ones) plus
    every zone again at its formation ('<kind>_FORMED', all rows: no selection), every other pattern at its own entry.
    A pattern without an entry (an opening range that never broke, an OTE never reached) has no act moment: it stays in
    the descriptive statistics but is never measured from an earlier time (that would use its future)."""
    import pandas as pd
    if f is None or len(f) == 0:
        return f
    has = f["entry_i"].to_numpy(np.int64) >= 0
    parts = [f[has]]
    z = f[f["kind"].isin(ZONE_KINDS)].copy()
    if len(z):
        z["kind"] = z["kind"].astype(str) + "_FORMED"
        z["entry_i"] = z["i_min"]
        z["edge"] = _edge_by_tf(m, z)
        z["held"] = 0
        parts.append(z)
    out = pd.concat(parts, ignore_index=True)
    out["kind"] = out["kind"].astype(str)
    out["flag"] = out["flag"].astype(str)
    return out


def _edge_by_tf(m: D.Minute, z) -> np.ndarray:
    out = np.zeros(len(z), np.int8)
    tfv = z["tf"].to_numpy()
    for tf in np.unique(tfv):
        sel = tfv == tf
        out[sel] = P.edge_outcome(m, z["i_min"].to_numpy(np.int64)[sel], z["dir"].to_numpy(np.int8)[sel],
                                  z["atr"].to_numpy(float)[sel], int(tf))
    return out


def _process(out_dir: Path, name: str, f, m: D.Minute, fr: X.Frame, lv: dict, es, high_ns, base, cut_ns, cost_pts,
             n_days, mk, summaries, pvals, cells, matrix) -> None:
    if f is None or len(f) == 0:
        return
    summaries.extend(P.summarize(f, int(m.ts[-1]), n_days))       # descriptive: every instance
    f = measurable(f, m)
    if len(f) == 0:
        return
    d = f["dir"].to_numpy(np.int8)
    ent = f["entry_i"].to_numpy(np.int64)
    known = f["known_ns"].to_numpy(np.int64)
    i_min = f["i_min"].to_numpy(np.int64)
    act_i = ent                                                        # every row here has an act moment
    act_ns = np.where((act_i >= 0) & (act_i < len(m)), m.ts[np.clip(act_i, 0, len(m) - 1)], known)
    eff = X.effects(m, fr, act_i, act_ns, d)
    ctx = {}
    for tf_own in np.unique(f["tf"].to_numpy()):
        sel = f["tf"].to_numpy() == tf_own
        c = X.contexts(m, fr, int(tf_own), act_i[sel], act_ns[sel], d[sel], lv, es, high_ns)
        for k, v in c.items():
            ctx.setdefault(k, np.zeros(len(f), np.int8))[sel] = v
    arrays = {"kind": f["kind"].to_numpy(str), "tf": f["tf"].to_numpy(np.int32), "dir": d, "known_ns": known,
              "act_ns": act_ns, "session": f["session"].to_numpy(np.int8),
              "top": f["top"].to_numpy(np.float32), "bottom": f["bottom"].to_numpy(np.float32),
              "size_atr": f["size_atr"].to_numpy(np.float32), "fill_min": f["fill_min"].to_numpy(np.float32),
              "touch_min": f["touch_min"].to_numpy(np.float32), "held": f["held"].to_numpy(np.int8),
              "edge": f["edge"].to_numpy(np.int8), "flag": f["flag"].astype(str).to_numpy(str), **eff,
              **{"ctx_" + k: v for k, v in ctx.items()}}
    np.savez_compressed(out_dir / f"events_{name}.npz", **arrays)
    find = act_ns < cut_ns
    kinds, tfs = arrays["kind"], arrays["tf"]
    for (kind, tf, dd) in sorted({(k, int(t), int(x)) for k, t, x in zip(kinds, tfs, d)}):
        g = np.flatnonzero((kinds == kind) & (tfs == tf) & (d == dd))
        if len(g) < E.MIN_FIND:
            continue
        g = g[np.argsort(act_ns[g], kind="stable")]          # time order: 'first per 15-minute window' = first in TIME
        sess = arrays["session"][g]
        outs = {"edge": arrays["edge"][g], "next15": eff.get("next_dir_15", np.zeros(len(f), np.int8))[g],
                "rest15": eff.get("m15_rest", np.zeros(len(f), np.int8))[g]}
        ba = _base_arrays(base, tf, sess, np.full(len(g), dd, np.int8))
        atr_pts = float(np.median(f["atr"].to_numpy(float)[g]))
        cost_rows = None if cost_pts is None else np.asarray(cost_pts["by_session"])[sess.astype(int)]
        key = {"kind": kind, "tf": D.TF_LABEL.get(tf, str(tf)), "dir": dd}
        cells.extend(E.scan_group(key, outs, ba, {k: v[g] for k, v in ctx.items()}, sess, find[g], atr_pts, cost_rows,
                                  pvals, act_ns[g] // (15 * D.MIN_NS)))
        row = {**key, "n": int(len(g)), "effects": []}
        for tf2 in D.TIMEFRAMES:
            nd = eff.get(f"next_dir_{tf2}")
            nr = eff.get(f"next_rng_{tf2}")
            if nd is None:
                continue
            x = nd[g]
            nz = x[x != 0]
            rr = nr[g]
            rr = rr[np.isfinite(rr)]
            row["effects"].append({"tf": D.TF_LABEL[tf2], "same_way": float((nz > 0).mean()) if len(nz) else None,
                                   "n": int(len(nz)), "size_median": float(np.median(rr)) if len(rr) else None,
                                   "size_q90": float(np.quantile(rr, 0.9)) if len(rr) else None})
        if "m15_bos" in eff:
            row["m15_bos"] = float(eff["m15_bos"][g].mean())
            m4 = eff["m15_4"][g]
            m4 = m4[np.isfinite(m4)]
            row["m15_4_atr"] = {"q25": float(np.quantile(m4, 0.25)), "q50": float(np.median(m4)),
                                "q75": float(np.quantile(m4, 0.75))} if len(m4) else None
        matrix.append(row)


def latest(data_root) -> dict | None:
    from edgelab.mystrategy import runner as R
    ptr = R._read_json(home(data_root) / "latest.json")
    if not ptr:
        return None
    p = home(data_root) / f"analysis_{ptr['key']}" / "summary.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
