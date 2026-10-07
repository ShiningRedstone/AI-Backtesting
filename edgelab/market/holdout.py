"""The Market simulator's holdout prediction test (ADR-107): ONE recorded look.

Order (nothing about the holdout is read before the look is recorded):
1. checks: an analysis exists and is CURRENT (same data, news, settings), exactly one active research protocol, the
   research settings equal the protocol's, the look of this test is not used yet;
2. the models are trained on the WHOLE discovery period and FROZEN: per target (next candle up, candle size, daily
   bias, level reach) the model chosen on the discovery walk-forward is the official one (chosen BEFORE the look);
   the other models are reported too, marked 'not chosen in advance';
3. the look is recorded in a companion protocol (role ``market_sim``: 1 look, no trials) - from here on it counts,
   even if something fails later;
4. the holdout minutes are loaded (discovery minutes before them are the history every live input needs), every
   holdout 15-minute candle is predicted with LIVE knowledge only, and scored against the same baselines with
   day-bootstrap intervals, per month too.
Never a backtest run or a try. A second look is refused.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from edgelab.market import data as D
from edgelab.market import forecast as F
from edgelab.market import gbm as G

TARGETS = ("up", "size", "bias", "levels")
LEVELMAP_TARGETS = ("reach2h", "reach", "react", "first", "land2h", "land")
GBM_TREES = 90


class HoldoutTestError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def home(data_root) -> Path:
    p = Path(data_root) / "market" / "holdout"
    p.mkdir(parents=True, exist_ok=True)
    return p


# =============================================================================================== protocol
def _scope(parent: dict) -> str:
    from edgelab.mystrategy import runner as R
    from edgelab.research.protocol import MARKET_SIM_SCOPE_SUFFIX
    sc = parent["material"]["scope"]
    return R.svc_scope(sc["instrument"], sc["provider"]) + MARKET_SIM_SCOPE_SUFFIX


def protocol(svc, lock=None, create: bool = True) -> tuple[dict, dict | None]:
    from contextlib import nullcontext

    from edgelab.mystrategy import runner as R
    from edgelab.research import protocol as rp
    with (lock if lock is not None else nullcontext()):
        parent = R._parent(svc)
        if parent is None:
            raise HoldoutTestError("NO_PROTOCOL", "Exactly one active research protocol is needed.")
        rows = [p for p in svc.store.list_protocols(_scope(parent), "ACTIVE")
                if (p["material"].get("parent") or {}).get("protocol_id") == parent["protocol_id"]]
        mine = rows[0] if rows else None
        if mine is None and create:
            mat = R.build_material(parent, budget=1, looks=1)
            mat.update({
                "role": rp.MARKET_SIM_ROLE, "name": "Market simulator holdout prediction (one look)",
                "search_constraints": {
                    "strategies": "none: a frozen forecaster of 15-minute candles (up / size), daily bias and level "
                                  "reach, trained on the discovery period only; no trading strategy, no trials",
                    "evaluation_windows": "the holdout window only (discovery minutes are the history of live inputs)",
                    "stages": {"discovery": "none here (the Market simulator's walk-forward ran on discovery only)",
                               "holdout": "ONE look: every holdout 15-minute candle predicted live and scored"}}})
            mat["trial_budget"]["unit"] += "; no trial is counted under this protocol"
            mat["pre_protocol_exposure"] = {
                "statement": "Created when the user asked for the Market simulator's holdout prediction test. The "
                             "forecaster's inputs, models and the choice of model per target were made on the "
                             "discovery period only (walk-forward), before this look.", "runs": []}
            rec = rp.make_record(mat, {"code_version": R._code_version()})
            svc.store.save_protocol(rec, _scope(parent))
            mine = svc.store.get_protocol(rec["protocol_id"])
        if mine is not None:
            rp.verify_record(mine)
        return parent, mine


def _looks(svc, mine: dict | None) -> list[dict]:
    if mine is None:
        return []
    return [a for a in svc.store.list_holdout_access(mine["protocol_id"]) if a["status"] != "refused"]


def status(svc) -> dict:
    from edgelab.mystrategy import runner as R
    try:
        parent, mine = protocol(svc, create=False)
    except HoldoutTestError as e:
        return {"available": False, "problem": e.message}
    w = R.windows(parent["material"])
    looks = _looks(svc, mine)
    res = R._read_json(home(svc.data_root) / "result.json")
    return {"available": True, "used": bool(looks), "look": looks[0] if looks else None,
            "holdout": {"start": str(w["holdout"]["start"]), "end": str(w["holdout"]["end"])}, "result": res}


# =============================================================================================== the look
def _fit(X, y, slot, kind: str, sim_cols=None, exact=False, base_X=None) -> dict:
    """Every model of one target trained on all discovery rows."""
    m: dict = {"kind": kind, "slot": slot, "X": X, "y": y, "sim_cols": sim_cols, "exact": exact}
    if kind == "binary":
        if base_X is not None:
            m["baseline"] = G.Logistic(l2=1.0).fit(base_X, y)
        else:
            m["p_all"] = (y.sum() + 1) / (len(y) + 2)                   # the overall up-rate (see forecast.py)
        m["logistic"] = G.Logistic(l2=1.0).fit(X, y)
        m["boosting"] = G.GBM(loss="logloss", n_trees=GBM_TREES, seed=7).fit(X, y)
    else:
        m["logistic"] = G.Ridge(l2=10.0).fit(X, y)
        m["boosting"] = G.GBM(loss="l2", n_trees=GBM_TREES, seed=7).fit(X, y)
    return m


def _predict(m: dict, X, slot, base_X=None) -> dict:
    out = {}
    if m["kind"] == "binary":
        if "baseline" in m:
            out["baseline"] = m["baseline"].predict(base_X)
        else:
            out["baseline"] = np.full(len(X), m["p_all"])
    else:
        out["baseline"] = np.zeros(len(X))
    out["logistic"] = m["logistic"].predict(X)
    out["boosting"] = m["boosting"].predict(X)
    sc = m["sim_cols"]
    Xtr, Xte = (m["X"], X) if sc is None else (m["X"][:, sc], X[:, sc])
    out["similar"] = F._similar(Xtr, m["y"], m["slot"], Xte, slot, m["kind"], exact=m["exact"])
    return out


def _fingerprint(analysis_key: str, chosen: dict, names: list, lchosen: dict | None = None,
                 lnames: dict | None = None) -> str:
    from edgelab.core.identity import hash_obj
    from edgelab.mystrategy import runner as R
    obj = {"analysis": analysis_key, "chosen": chosen, "inputs": names, "gbm_trees": GBM_TREES,
           "neighbours": F.NEIGHBOURS, "code": R._code_version()}
    if lchosen is not None:
        obj["levelmap"] = {"chosen": lchosen, "inputs": lnames}
    return hash_obj(obj, 16)


def run(svc, *, lock=None, progress=None) -> dict:
    from contextlib import nullcontext

    from edgelab.market import analysis as A
    from edgelab.market import levelmap as L
    from edgelab.market import news as N
    from edgelab.mystrategy import runner as R
    step = progress or (lambda s: None)
    guard = lock if lock is not None else nullcontext()
    # ---------------------------------------------------------------- 1. checks (nothing of the holdout is read)
    step("Checking: current analysis, protocol, settings, unused look")
    latest = A.latest(svc.data_root)
    if not latest or not latest.get("forecast"):
        raise HoldoutTestError("NO_ANALYSIS", "Run the analysis first: the test freezes its models.")
    mk = D.discovery(svc, lock)
    cal = N.calendar(svc.data_root)
    news_ok = bool(cal and not cal.get("refused"))
    if A.cache_key(mk.source, cal["meta"]["sha256"] if news_ok else None, svc._config_hash()) != latest["key"]:
        raise HoldoutTestError("ANALYSIS_OUTDATED", "The data, news or settings changed since the last analysis. Run "
                                                    "the analysis again first, so the frozen models match it.")
    parent, mine = protocol(svc, lock)
    if svc._config_hash() != mine["material"]["config_hash"]:
        raise HoldoutTestError("PROTOCOL_CONFIG_CHANGED", "The research settings differ from the protocol's.")
    if _looks(svc, mine):
        raise HoldoutTestError("HOLDOUT_LOOK_USED", "The holdout prediction test is already used (one look only).")
    fc = latest["forecast"]
    chosen = {t: (fc.get(t) or {}).get("chosen") or "logistic" for t in TARGETS}
    lm_sum = latest.get("levelmap") or {}
    if not (lm_sum.get("targets") or {}):
        raise HoldoutTestError("ANALYSIS_OUTDATED", "The analysis has no level map. Run the analysis again first.")
    lchosen = {t: (lm_sum["targets"].get(t) or {}).get("chosen") or "logistic" for t in LEVELMAP_TARGETS}
    # ---------------------------------------------------------------- 2. train and freeze on discovery only
    step("Training the frozen models on the whole discovery period")
    news_d = N.events(svc.data_root, mk.start.value, mk.end.value) if news_ok else []
    cxd = F.Context(mk.nq, mk.es, news_d)
    crd = F.candle_rows(cxd)
    Xd, names, _ = F.features(cxd, crd["t"])
    upm = crd["up"] != 0
    m_up = _fit(Xd[upm], (crd["up"][upm] > 0).astype(float), crd["slot"][upm], "binary")
    m_size = _fit(Xd, crd["size"], crd["slot"], "real")
    br = F.bias_rows(cxd)
    Xb, _, _ = F.features(cxd, br["t"])
    bm = br["up"] != 0
    m_bias = _fit(Xb[bm], (br["up"][bm] > 0).astype(float), (P_ny(br["t"]) // 30)[bm], "binary")
    lr = F.level_rows(cxd)
    XL, lnames = F.level_matrix(lr)
    ltype = np.array([F.LEVEL_KEYS.index(k) for k in lr["level"]])
    keep = ["abs_dist", "side", "log_dist", "time_left", "dist_per_sqrt_time", "size60", "size15_lag1", "move_day"]
    lcols = [lnames.index(c) for c in keep if c in lnames]
    m_lev = _fit(XL, lr["reached"].astype(float), ltype, "binary", sim_cols=lcols, exact=True, base_X=XL[:, :5])
    # size bands from the discovery walk-forward's OUT-OF-SAMPLE residuals of the chosen size model
    with np.load(A.home(svc.data_root) / f"analysis_{latest['key']}" / "forecast.npz") as z:
        pr, yy = z[f"size_{chosen['size']}"].astype(float), z["size"].astype(float)
    ok = np.isfinite(pr)
    band_q = np.quantile((yy - pr)[ok], [0.1, 0.25, 0.75, 0.9]) if ok.sum() > 300 else np.zeros(4)
    step("Training the frozen level-map models (FVGs, EQ / OTE, liquidity) on the whole discovery period")
    bd = L.build(cxd, progress=step)
    lfm = L.fit_frozen(bd, GBM_TREES)
    vol_lm = lm_sum.get("volatility_cuts") or L.volatility_cuts(bd)
    jv = names.index("size60")
    vol_c = [float(np.quantile(Xd[:, jv], 1 / 3)), float(np.quantile(Xd[:, jv], 2 / 3))]
    lband = L.bands_from_analysis(A.home(svc.data_root) / f"analysis_{latest['key']}" / "levelmap.npz", lchosen)
    fp = _fingerprint(latest["key"], chosen, names, lchosen, lfm["names"])
    del cxd, bd
    # ---------------------------------------------------------------- 3. the look is recorded
    w = R.windows(parent["material"])
    hs, he = R._ts(w["holdout"]["start"]), R._ts(w["holdout"]["end"])
    with guard:
        if _looks(svc, mine):
            raise HoldoutTestError("HOLDOUT_LOOK_USED", "The holdout prediction test is already used.")
        access_id = "HA_MARKET_" + R.new_id("X")[2:]
        svc.store.add_holdout_access({"access_id": access_id, "protocol_id": mine["protocol_id"],
                                      "strategy_id": "market_sim:" + fp, "logic_hash": fp, "definition_hash": fp,
                                      "frozen_hash": fp, "search_id": None, "status": "granted", "reason_code": None,
                                      "reason": None, "run_id": None, "result_json": None, "created_at": R._now(),
                                      "completed_at": None})
    try:
        # ------------------------------------------------------------ 4. the holdout, predicted live and scored
        step("Loading the holdout (the look is recorded)")
        ds = svc._cell_dataset(R.dataset_1m(svc, parent), (hs, he), lock)
        hb = ds.bars
        nq = D.Minute(np.r_[mk.nq.ts, hb.ts_ns], np.r_[mk.nq.o, hb.open], np.r_[mk.nq.h, hb.high],
                      np.r_[mk.nq.l, hb.low], np.r_[mk.nq.c, hb.close])
        es, _ = D.load_es(svc.data_root, mk.start.value, he.value)
        news_all = N.events(svc.data_root, mk.start.value, he.value) if news_ok else []
        step("Predicting every holdout 15-minute candle with live knowledge")
        cx = F.Context(nq, es, news_all)
        cr = F.candle_rows(cx)
        sel = cr["t"] >= hs.value
        Xh, _, _ = F.features(cx, cr["t"][sel])
        slot = cr["slot"][sel]
        p_up = _predict(m_up, Xh, slot)
        p_sz = _predict(m_size, Xh, slot)
        t_h, up_h, size_h = cr["t"][sel], cr["up"][sel], cr["size"][sel]
        day_h = cr["day"][sel].astype("datetime64[D]")
        step("Daily bias and levels on the holdout")
        brh = F.bias_rows(cx)
        sb = brh["t"] >= hs.value
        Xbh, _, _ = F.features(cx, brh["t"][sb])
        p_b = _predict(m_bias, Xbh, P_ny(brh["t"][sb]) // 30)
        lrh = F.level_rows(cx)
        sl = lrh["t"] >= hs.value
        XLh, _ = F.level_matrix({k: (v[sl] if isinstance(v, np.ndarray) else v) for k, v in lrh.items()})
        lth = np.array([F.LEVEL_KEYS.index(k) for k in lrh["level"][sl]])
        p_l = _predict(m_lev, XLh, lth, base_X=XLh[:, :5])
        # ------------------------------------------------------------ scores
        step("Scoring against the baselines")
        mu = up_h != 0
        yb = (up_h > 0).astype(float)
        res = {"access_id": access_id, "fingerprint": fp, "analysis_key": latest["key"], "chosen": chosen,
               "computed_at": R._now(), "holdout": {"start": hs.isoformat(), "end": he.isoformat()},
               "candles": int(len(t_h)), "days": int(len(np.unique(day_h))), "targets": {}}
        res["targets"]["up"] = _scores(p_up, yb, mu, day_h, "binary", chosen["up"])
        res["targets"]["size"] = _scores(p_sz, size_h, np.isfinite(size_h), day_h, "real", chosen["size"])
        q = p_sz[chosen["size"]][:, None] + band_q[None, :]
        okq = np.isfinite(q[:, 0]) & np.isfinite(size_h)
        res["targets"]["size"]["bands"] = {"inside_50": float(np.mean((size_h[okq] >= q[okq, 1]) & (size_h[okq] <= q[okq, 2]))),
                                           "inside_80": float(np.mean((size_h[okq] >= q[okq, 0]) & (size_h[okq] <= q[okq, 3]))),
                                           "n": int(okq.sum())}
        ybb = (brh["up"][sb] > 0).astype(float)
        res["targets"]["bias"] = _scores(p_b, ybb, brh["up"][sb] != 0, brh["day"][sb].astype("datetime64[D]"), "binary",
                                         chosen["bias"])
        res["targets"]["levels"] = _scores(p_l, lrh["reached"][sl].astype(float), np.ones(int(sl.sum()), bool),
                                           lrh["day"][sl].astype("datetime64[D]"), "binary", chosen["levels"])
        step("Level map on the holdout: every level, tapped first, reactions, where price landed")
        bh = L.build(cx, start_ns=hs.value, progress=step)
        if bh["n_dec"]:
            fr = L.frozen_period(lfm, bh, lchosen, lband, home(svc.data_root) / "levelmap.npz")
            lp, lres = fr["preds"], fr["scores"]
            yl = L._targets(bh)
            disc_m = lm_sum.get("mistakes") or {}
            lres["mistakes"] = {}
            for t in LEVELMAP_TARGETS:
                mres = L.mistakes(bh, lp[t], yl[t], t, lchosen[t], vol_lm)
                _compare(mres, disc_m.get(t))
                lres["mistakes"][t] = mres
            res["levelmap"] = lres
        step("Mistakes report: where and why the predictions failed (descriptive, nothing is retrained)")
        cb = L.candle_buckets(Xh, names, t_h, vol_c)
        res["mistakes"] = {"up": L.mistakes_core(p_up[chosen["up"]], p_up["baseline"], yb, "binary", cb, mu),
                           "size": L.mistakes_core(p_sz[chosen["size"]], None, size_h, "real", cb, np.isfinite(size_h))}
        for k in ("up", "size"):
            res["mistakes"][k].update(target=k, model=chosen[k])
        res = _jsonable(res)
        np.savez_compressed(home(svc.data_root) / "predictions.npz", t=t_h, up=up_h, size=size_h,
                            day=day_h.astype(np.int64), size_q=q.astype(np.float32),
                            **{f"p_up_{k}": v.astype(np.float32) for k, v in p_up.items()},
                            **{f"size_{k}": v.astype(np.float32) for k, v in p_sz.items()},
                            contrib_idx=np.full((len(t_h), 3), -1, np.int16), contrib_val=np.zeros((len(t_h), 3), np.float32),
                            bias_t=brh["t"][sb], **{f"bias_{k}": v.astype(np.float32) for k, v in p_b.items()},
                            level_t=lrh["t"][sl], level_key=lrh["level"][sl].astype(str), level_price=lrh["price"][sl],
                            level_reached=lrh["reached"][sl], **{f"level_{k}": v.astype(np.float32) for k, v in p_l.items()})
        from edgelab.core.fsutil import atomic_write_text
        atomic_write_text(home(svc.data_root) / "result.json", json.dumps(res))
        with guard:
            summary = {t: (v.get("official") or {}).get("skill") for t, v in res["targets"].items()}
            summary.update({"levelmap_" + t: ((res.get("levelmap") or {}).get(t, {}).get("official") or {}).get("skill")
                            for t in LEVELMAP_TARGETS})
            svc.store.update_holdout_access(access_id, status="completed", completed_at=R._now(),
                                            result_json=json.dumps(summary))
        return res
    except Exception as exc:                                       # the look stays spent; the failure is recorded
        with guard:
            svc.store.update_holdout_access(access_id, status="failed", completed_at=R._now(),
                                            reason_code=type(exc).__name__, reason=str(exc)[:500])
        raise


def _compare(mres: dict, disc: dict | None) -> None:
    """Puts the discovery walk-forward's skill of the same bucket next to each holdout bucket (same weak spots, or new
    ones?)."""
    if not disc or not mres.get("groups"):
        return
    ref = {(g["group"], r["bucket"]): r.get("skill") for g in disc.get("groups", []) for r in g["rows"]}
    for g in mres["groups"]:
        for r in g["rows"]:
            r["discovery_skill"] = ref.get((g["group"], r["bucket"]))
    mres["discovery_skill"] = disc.get("skill")


def P_ny(t):
    from edgelab.market.patterns import ny_minutes
    return ny_minutes(t)


def _scores(preds: dict, y, mask, days, kind: str, chosen: str) -> dict:
    mask = np.asarray(mask, bool)
    months = days.astype("datetime64[M]")
    out = {"official_model": chosen, "models": {}, "by_month": []}
    for k, p in preds.items():
        if kind == "real" and k == "baseline":
            continue
        s = (F.score_binary(p[mask], y[mask], preds["baseline"][mask], days[mask]) if kind == "binary"
             else F.score_real(p[mask], y[mask], days[mask]))
        out["models"][k] = s
    out["official"] = out["models"].get(chosen)
    for mo in np.unique(months[mask]):
        mm = mask & (months == mo)
        p = preds[chosen]
        s = (F.score_binary(p[mm], y[mm], preds["baseline"][mm], days[mm]) if kind == "binary"
             else F.score_real(p[mm], y[mm], days[mm]))
        if s:
            out["by_month"].append({"month": str(mo), "n": s["n"], "skill": s["skill"], "accuracy": s.get("accuracy")})
    return out


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
