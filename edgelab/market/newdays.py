"""New days for the market simulator (ADR-106): NQ (USATECH.IDX, E_NQ-100) and ES (USA500.IDX, E_SandP-500) 1-minute
BID bars downloaded with dukascopy-python for the trading dates AFTER the research data ends. Never the holdout (not
even as warm-up history): the first new days are the warm-up; a day is predicted only once enough new history exists.
Kept apart from research data (never a dataset), like the paper-trading feed.

Each new day is predicted with LIVE knowledge by models trained on the whole discovery period, and scored when the day
is complete. Same targets, inputs and baselines as the walk-forward (forecast.py).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from edgelab.market import data as D

CODES = {"nq": "E_NQ-100", "es": "E_SandP-500"}
WARMUP_DAYS = 20


class NewDaysError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def folder(data_root, which: str) -> Path:
    p = Path(data_root) / "market" / "newdays" / which
    p.mkdir(parents=True, exist_ok=True)
    return p


def dukascopy_fetch(code: str, start: datetime, end: datetime) -> pd.DataFrame:
    import dukascopy_python as dp
    return dp.fetch(code, dp.INTERVAL_MIN_1, dp.OFFER_SIDE_BID, start, end)


def first_date(svc) -> date:
    """The first trading date after the research data (the holdout's last bar)."""
    from edgelab.mystrategy import runner as R
    from edgelab.paper import feed as PF
    parent = R._parent(svc)
    if parent is None:
        raise NewDaysError("NO_PROTOCOL", "The market simulator needs the workspace's active research protocol.")
    end = R._ts(R.windows(parent["material"])["holdout"]["end"])
    return PF.next_trading_date(svc.cfg, end.tz_convert(D.NY).date())


def update(svc, fetch: Callable | None = None, now: datetime | None = None, max_days: int = 400) -> dict:
    """Download every missing completed trading date of NQ and ES from first_date on (BID, 1 minute)."""
    from edgelab.core.fsutil import atomic_write_text
    from edgelab.paper import feed as PF
    fetch = fetch or dukascopy_fetch
    start = first_date(svc)
    last = PF.last_completed_date(svc.cfg, now)
    got = {"nq": [], "es": []}
    errors = {}
    for which, code in CODES.items():
        fd = folder(svc.data_root, which)
        for d in PF.trading_dates(svc.cfg, start, last)[-max_days:]:
            p = fd / f"{d.isoformat()}.csv"
            if p.exists():
                continue
            s, e = PF._window(svc.cfg, d)
            try:
                df = fetch(code, s, e)
            except Exception as exc:                                  # network / package: retried next time
                errors[which] = f"{d}: {type(exc).__name__}: {exc}"
                break
            if df is None or not len(df):
                continue
            idx = pd.DatetimeIndex(df.index)
            idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
            keep = (idx >= pd.Timestamp(s)) & (idx < pd.Timestamp(e))
            out = pd.DataFrame({"timestamp": [t.isoformat() for t in idx[keep]],
                                **{k: df[k].to_numpy()[keep] for k in ("open", "high", "low", "close")}})
            if not len(out):
                continue
            atomic_write_text(p, out.to_csv(index=False))
            got[which].append(d.isoformat())
    st = status(svc)
    st.update(downloaded=got, errors=errors, checked_at=datetime.now(timezone.utc).isoformat())
    atomic_write_text(Path(svc.data_root) / "market" / "newdays" / "last_update.json", json.dumps(st))
    return st


def load(data_root, which: str) -> D.Minute | None:
    fd = folder(data_root, which)
    files = sorted(fd.glob("*.csv"))
    if not files:
        return None
    frames = [pd.read_csv(f, dtype={"timestamp": str}) for f in files]
    df = pd.concat(frames, ignore_index=True)
    ts = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601").astype("datetime64[ns, UTC]")
    ns = ts.dt.tz_localize(None).to_numpy().astype("datetime64[ns]").astype(np.int64)
    o = np.argsort(ns, kind="stable")
    ns = ns[o]
    keep = np.r_[True, np.diff(ns) > 0]
    o = o[keep]
    return D.Minute(ns[keep], df["open"].to_numpy(float)[o], df["high"].to_numpy(float)[o],
                    df["low"].to_numpy(float)[o], df["close"].to_numpy(float)[o])


def status(svc) -> dict:
    out = {}
    for which in CODES:
        files = sorted(folder(svc.data_root, which).glob("*.csv"))
        out[which] = {"days": len(files), "first": files[0].stem if files else None, "last": files[-1].stem if files else None}
    try:
        out["first_date"] = first_date(svc).isoformat()
    except NewDaysError as e:
        out["problem"] = e.message
    out["last_problem"] = last_problem(svc.data_root)
    try:
        lu = json.loads((Path(svc.data_root) / "market" / "newdays" / "last_update.json").read_text(encoding="utf-8"))
        out["last_update"] = {"checked_at": lu.get("checked_at"), "errors": lu.get("errors") or {},
                              "downloaded": {k: len(v) for k, v in (lu.get("downloaded") or {}).items()}}
    except (OSError, ValueError):
        out["last_update"] = None
    return out


def _concat(a: D.Minute | None, b: D.Minute | None) -> D.Minute | None:
    """Two minute series as one (sorted; a minute present in both is kept once, from ``a``)."""
    if a is None or not len(a):
        return b
    if b is None or not len(b):
        return a
    ts = np.r_[a.ts, b.ts]
    o = np.argsort(ts, kind="stable")
    ts = ts[o]
    keep = np.r_[True, np.diff(ts) > 0]
    pick = lambda x, y: np.r_[x, y][o][keep]
    return D.Minute(ts[keep], pick(a.o, b.o), pick(a.h, b.h), pick(a.l, b.l), pick(a.c, b.c))


def _history(svc, mk) -> tuple[D.Minute, D.Minute | None] | None:
    """ADR-110: once a Market simulator holdout look is USED, the holdout minutes may serve as plain history in front of
    the new days (the live inputs need the last days / weeks: ATR, previous day and week, swings, open FVGs). They never
    train anything. Before any look: None (the holdout stays unread; new days then need WARMUP_DAYS of their own)."""
    from edgelab.market import direction as DR
    from edgelab.market import holdout as H
    from edgelab.mystrategy import runner as R
    if not (H.status(svc).get("used") or DR.holdout_status(svc).get("used")):
        return None
    parent = R._parent(svc)
    w = R.windows(parent["material"])
    hs, he = R._ts(w["holdout"]["start"]), R._ts(w["holdout"]["end"])
    hb = svc._cell_dataset(R.dataset_1m(svc, parent), (hs, he)).bars
    nq = _concat(mk.nq, D.Minute(hb.ts_ns, hb.open, hb.high, hb.low, hb.close))
    es, _ = D.load_es(svc.data_root, mk.start.value, he.value)
    return nq, es


def last_problem(data_root) -> dict | None:
    try:
        return json.loads((folder(data_root, "") / "last_problem.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def set_problem(data_root, problem: dict | None) -> None:
    from edgelab.core.fsutil import atomic_write_text
    p = folder(data_root, "") / "last_problem.json"
    if problem is None:
        if p.exists():
            p.unlink()
        return
    atomic_write_text(p, json.dumps({**problem, "at": datetime.now(timezone.utc).isoformat()}))


def train_candles(svc, progress=None) -> dict:
    """The final 15-minute candle models (up / down and size), trained on ALL discovery candles and frozen. Shared by the
    new-days prediction and the live chart predictor (ADR-113), so both use exactly the same models."""
    from edgelab.market import forecast as F
    from edgelab.market import gbm as G
    from edgelab.market import news as N
    step = progress or (lambda s: None)
    step("Rebuilding the discovery inputs to train the final models")
    mk = D.discovery(svc)
    cal = N.calendar(svc.data_root)
    ok_news = bool(cal and not cal.get("refused"))
    news_d = N.events(svc.data_root, mk.start.value, mk.end.value) if ok_news else []
    cxd = F.Context(mk.nq, mk.es, news_d)
    crd = F.candle_rows(cxd)
    Xd, names, _ = F.features(cxd, crd["t"])
    upm = crd["up"] != 0
    yb = (crd["up"] > 0).astype(float)
    step("Training on all discovery candles")
    models = {"logistic": G.Logistic().fit(Xd[upm], yb[upm]),
              "boosting": G.GBM(n_trees=90, seed=1).fit(Xd[upm], yb[upm])}
    size_models = {"logistic": G.Ridge().fit(Xd, crd["size"]), "boosting": G.GBM(loss="l2", n_trees=90, seed=2).fit(Xd, crd["size"])}
    return {"ok_news": ok_news, "cxd": cxd, "crd": crd, "Xd": Xd, "names": names, "upm": upm, "yb": yb, "models": models,
            "size_models": size_models}


def size_residuals(data_root, analysis_key: str, model: str) -> np.ndarray | None:
    """q10 / q25 / q75 / q90 of the discovery walk-forward's OUT-OF-SAMPLE size residuals of ``model`` (None = too few)."""
    from edgelab.market import analysis as A
    try:
        with np.load(A.home(data_root) / f"analysis_{analysis_key}" / "forecast.npz") as z:
            pr_, yy_ = z[f"size_{model}"].astype(float), z["size"].astype(float)
    except (OSError, KeyError):
        return None
    ok_ = np.isfinite(pr_)
    return np.quantile((yy_ - pr_)[ok_], [0.1, 0.25, 0.75, 0.9]) if ok_.sum() > 300 else None


def predict(svc, analysis_key: str, progress=None) -> dict:
    """Train on the whole discovery period, predict every new day live, score the completed ones. History in front of
    the new days: the holdout once a holdout look is used (every new day predicted), else the first WARMUP_DAYS new
    days (predicted from the next day on)."""
    from edgelab.core.fsutil import atomic_write_text
    from edgelab.market import analysis as A
    from edgelab.market import forecast as F
    from edgelab.market import news as N
    step = progress or (lambda s: None)
    nq_new = load(svc.data_root, "nq")
    if nq_new is None:
        raise NewDaysError("NEWDAYS_NONE", "No new days are downloaded yet.")
    es_new = load(svc.data_root, "es")
    summary = A.latest(svc.data_root)
    if not summary or summary.get("key") != analysis_key:
        raise NewDaysError("NO_ANALYSIS", "Run the analysis first.")
    mk = D.discovery(svc)
    hist = _history(svc, mk)
    new_days = np.unique(nq_new.day)
    if hist is None:
        if len(new_days) <= WARMUP_DAYS:
            raise NewDaysError("NEWDAYS_TOO_FEW", f"Fewer than {WARMUP_DAYS + 1} new days are downloaded; the first "
                                                  f"{WARMUP_DAYS} only build history (no holdout look is used yet, so "
                                                  f"the holdout cannot serve as history).")
        nq, es, scored_days, warm = nq_new, es_new, new_days[WARMUP_DAYS:], WARMUP_DAYS
    else:
        nq, es = _concat(hist[0], nq_new), _concat(hist[1], es_new)
        scored_days, warm = new_days, 0
    start_new = int(nq_new.ts[np.searchsorted(nq_new.day, scored_days[0])])
    fz = train_candles(svc, progress=step)
    ok_news, crd, Xd, upm, yb = fz["ok_news"], fz["crd"], fz["Xd"], fz["upm"], fz["yb"]
    models, size_models, cxd = fz["models"], fz["size_models"], fz["cxd"]
    news_n = N.events(svc.data_root, int(nq.ts[0]), int(nq.ts[-1]) + 86_400_000_000_000) if ok_news else []
    step("Predicting the new days with live knowledge" + (" (holdout minutes as history)" if hist is not None else ""))
    cxn = F.Context(nq, es, news_n)
    crn = F.candle_rows(cxn)
    sel = np.isin(crn["day"], scored_days) & (crn["t"] >= start_new)
    Xn, _, _ = F.features(cxn, crn["t"][sel])
    out = {"t": crn["t"][sel], "up": crn["up"][sel], "size": crn["size"][sel]}
    p = {k: m.predict(Xn) for k, m in models.items()}
    slot = crn["slot"][sel]
    p["baseline"] = np.full(int(sel.sum()), (yb[upm].sum() + 1) / (upm.sum() + 2))    # overall up-rate (forecast.py)
    p["similar"] = F._similar(Xd[upm], yb[upm], crd["slot"][upm], Xn, slot, "binary")
    sz = {k: m.predict(Xn) for k, m in size_models.items()}
    sz["similar"] = F._similar(Xd, crd["size"], crd["slot"], Xn, slot, "real")
    y = (out["up"] > 0).astype(float)
    m_up = out["up"] != 0
    dd = crn["day"][sel]
    res = {"analysis_key": analysis_key, "days": [str(d) for d in scored_days], "warmup_days": warm,
           "history": "holdout" if hist is not None else "first new days",
           "computed_at": datetime.now(timezone.utc).isoformat(), "candles": int(sel.sum()),
           "up": {k: F.score_binary(v[m_up], y[m_up], p["baseline"][m_up], dd[m_up]) for k, v in p.items()},
           "size": {k: F.score_real(v, out["size"], dd) for k, v in sz.items()}}
    # size ranges: the chosen size model + the discovery walk-forward's out-of-sample residual quantiles (as the holdout test)
    sz_m = ((summary.get("forecast") or {}).get("size") or {}).get("chosen") or "logistic"
    size_q = np.full((int(sel.sum()), 4), np.nan)
    rq = size_residuals(svc.data_root, analysis_key, sz_m)
    if rq is not None and sz_m in sz:
        size_q = sz[sz_m][:, None] + rq[None, :]
    np.savez_compressed(folder(svc.data_root, "") / f"predictions_{analysis_key}.npz", **out,
                        **{f"p_up_{k}": v for k, v in p.items()}, **{f"size_{k}": v for k, v in sz.items()},
                        size_q=size_q.astype(np.float32), day=dd.astype("datetime64[D]").astype(np.int64))
    lm_sum = summary.get("levelmap") or {}
    if lm_sum.get("targets") and len(scored_days):
        from edgelab.market import levelmap as L
        step("Level map: training on discovery, mapping the new days' levels live")
        lchosen = {t: (lm_sum["targets"].get(t) or {}).get("chosen") or "logistic"
                   for t in L.LEVEL_TARGETS + L.DECISION_TARGETS}
        fm = L.fit_frozen(L.build(cxd))
        bn = L.build(cxn, start_ns=start_new)
        if bn["n_dec"]:
            bands = L.bands_from_analysis(A.home(svc.data_root) / f"analysis_{analysis_key}" / "levelmap.npz", lchosen)
            fr = L.frozen_period(fm, bn, lchosen, bands, folder(svc.data_root, "") / f"levelmap_{analysis_key}.npz")
            res["levelmap"] = {"chosen": lchosen, "scores": fr["scores"], "decisions": int(bn["n_dec"])}
    from edgelab.market import direction as DR
    dsum = DR.latest(svc.data_root, analysis_key)
    if dsum and len(scored_days):
        step("Direction calls: training on discovery, calling the new days live")
        fmd = DR.fit_frozen(DR.build(cxd))
        bnd = DR.build(cxn, start_ns=start_new)
        if len(bnd["r"]["t"]):
            pdn = DR.predict_frozen(fmd, bnd)
            _, cdn = DR.official(pdn, bnd, dsum)
            DR.save_period(folder(svc.data_root, "") / f"direction_{analysis_key}.npz", bnd, pdn, cdn)
            res["direction"] = DR.score_period(bnd, pdn, dsum)
    res["by_day"] = by_day(svc.data_root, analysis_key, summary, dsum)
    res = A._jsonable(res)
    atomic_write_text(folder(svc.data_root, "") / f"scores_{analysis_key}.json", json.dumps(res))
    return res


def scores(data_root, analysis_key: str) -> dict | None:
    try:
        return json.loads((folder(data_root, "") / f"scores_{analysis_key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def by_day(data_root, key: str, summary: dict, dsum: dict | None) -> list[dict]:
    """Per scored new day: how the official forecasts did (next 15-min candle up / down, candle size, level traded
    within 2 h, direction calls)."""
    fold = folder(data_root, "")
    out: dict = {}
    fc = summary.get("forecast") or {}
    up_m = (fc.get("up") or {}).get("chosen") or "logistic"
    sz_m = (fc.get("size") or {}).get("chosen") or "logistic"
    p = fold / f"predictions_{key}.npz"
    if p.exists():
        with np.load(p) as z:
            day = z["day"].astype("datetime64[D]")
            up, pu, pb = z["up"], z[f"p_up_{up_m}"], z["p_up_baseline"]
            size, ps = z["size"], z[f"size_{sz_m}"]
        for d in np.unique(day):
            s = day == d
            m = s & (up != 0)
            row = out.setdefault(str(d), {"date": str(d)})
            row["candles"] = int(s.sum())
            if m.any():
                row["up_right"] = float(np.mean((pu[m] > 0.5) == (up[m] > 0)))
                row["up_base_right"] = float(np.mean((pb[m] > 0.5) == (up[m] > 0)))
            ok = s & np.isfinite(size) & np.isfinite(ps)
            if ok.any():
                row["size_error"] = float(np.mean(np.abs(ps[ok] - size[ok])))
                row["size_base_error"] = float(np.mean(np.abs(size[ok])))
    lp = fold / f"levelmap_{key}.npz"
    lm_m = (((summary.get("levelmap") or {}).get("targets") or {}).get("reach2h") or {}).get("chosen") or "logistic"
    if lp.exists():
        with np.load(lp) as z:
            dday = z["dec_day"].astype("datetime64[D]")[z["row_dec"]]
            y, pm, pb = z["row_reach2h"], z[f"p_reach2h_{lm_m}"], z["p_reach2h_baseline"]
        for d in np.unique(dday):
            s = (dday == d) & np.isfinite(pm)
            if s.any():
                row = out.setdefault(str(d), {"date": str(d)})
                row["levels"] = int(s.sum())
                row["level_right"] = float(np.mean((pm[s] > 0.5) == y[s]))
                row["level_base_right"] = float(np.mean((pb[s] > 0.5) == y[s]))
    dp = fold / f"direction_{key}.npz"
    if dsum and dp.exists():
        with np.load(dp) as z:
            dday = z["day"].astype("datetime64[D]")
            called, y = z["called"], z["y"]
            st = z["stage"]
            p = np.full(len(y), np.nan)
            for sg in ("0", "5", "10"):
                ch = (dsum["stages"].get(sg) or {}).get("chosen") or "logistic"
                p[st == int(sg)] = z[f"p_{ch}"][st == int(sg)]
        for d in np.unique(dday):
            s = (dday == d) & called & (y != 0)
            row = out.setdefault(str(d), {"date": str(d)})
            row["calls"] = int(s.sum())
            if s.any():
                row["calls_right"] = float(np.mean((p[s] > 0.5) == (y[s] > 0)))
    return [out[k] for k in sorted(out)]
