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
    return out


def predict(svc, analysis_key: str, progress=None) -> dict:
    """Train on the whole discovery period, predict every new day (after WARMUP_DAYS) live, score the completed ones."""
    from edgelab.core.fsutil import atomic_write_text
    from edgelab.market import analysis as A
    from edgelab.market import forecast as F
    from edgelab.market import gbm as G
    from edgelab.market import news as N
    step = progress or (lambda s: None)
    nq = load(svc.data_root, "nq")
    if nq is None or len(np.unique(nq.day)) <= WARMUP_DAYS:
        raise NewDaysError("NEWDAYS_TOO_FEW", f"Fewer than {WARMUP_DAYS + 1} new days are downloaded; the first "
                                              f"{WARMUP_DAYS} only build history.")
    es = load(svc.data_root, "es")
    summary = A.latest(svc.data_root)
    if not summary or summary.get("key") != analysis_key:
        raise NewDaysError("NO_ANALYSIS", "Run the analysis first.")
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
    step("Predicting the new days with live knowledge")
    news_n = N.events(svc.data_root, int(nq.ts[0]), int(nq.ts[-1]) + 86_400_000_000_000) if ok_news else []
    cxn = F.Context(nq, es, news_n)
    crn = F.candle_rows(cxn)
    udays = np.unique(nq.day)
    scored_days = udays[WARMUP_DAYS:]
    sel = np.isin(crn["day"], scored_days)
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
    res = {"analysis_key": analysis_key, "days": [str(d) for d in scored_days], "warmup_days": WARMUP_DAYS,
           "computed_at": datetime.now(timezone.utc).isoformat(), "candles": int(sel.sum()),
           "up": {k: F.score_binary(v[m_up], y[m_up], p["baseline"][m_up], dd[m_up]) for k, v in p.items()},
           "size": {k: F.score_real(v, out["size"], dd) for k, v in sz.items()}}
    np.savez_compressed(folder(svc.data_root, "") / f"predictions_{analysis_key}.npz", **out,
                        **{f"p_up_{k}": v for k, v in p.items()}, **{f"size_{k}": v for k, v in sz.items()},
                        day=dd.astype("datetime64[D]").astype(np.int64))
    lm_sum = summary.get("levelmap") or {}
    if lm_sum.get("targets") and len(scored_days):
        from edgelab.market import levelmap as L
        step("Level map: training on discovery, mapping the new days' levels live")
        lchosen = {t: (lm_sum["targets"].get(t) or {}).get("chosen") or "logistic"
                   for t in L.LEVEL_TARGETS + L.DECISION_TARGETS}
        fm = L.fit_frozen(L.build(cxd))
        bn = L.build(cxn, start_ns=int(nq.ts[np.searchsorted(nq.day, scored_days[0])]))
        if bn["n_dec"]:
            bands = L.bands_from_analysis(A.home(svc.data_root) / f"analysis_{analysis_key}" / "levelmap.npz", lchosen)
            fr = L.frozen_period(fm, bn, lchosen, bands, folder(svc.data_root, "") / f"levelmap_{analysis_key}.npz")
            res["levelmap"] = {"chosen": lchosen, "scores": fr["scores"], "decisions": int(bn["n_dec"])}
    res = A._jsonable(res)
    atomic_write_text(folder(svc.data_root, "") / f"scores_{analysis_key}.json", json.dumps(res))
    return res


def scores(data_root, analysis_key: str) -> dict | None:
    try:
        return json.loads((folder(data_root, "") / f"scores_{analysis_key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
