"""Backtest results read models (ADR-73): the results overview ("the field" of strategies, survivors, breakdowns, exit
comparison, stored random controls, bootstrapped prop evaluations) and the per-strategy / per-control panels.

READ-ONLY, like ``research/overview.py``: it reads the strategy library, the run registry (records and stored trades),
the stored prop audit of every run, stored control records and cached bootstrap results. It evaluates nothing, records
no trial, never touches a protocol ledger or the holdout. Every number is a stored measurement or a direct aggregate of
stored measurements; every block states its scope (in-sample unless labelled) and basis (net of the run's stated costs
unless labelled gross). Synthetic results are flagged on every row.

Survivor (user definition): positive net R per trade AND the run's recorded trade sequence passes an evaluation and
reaches the first payout under at least one prop rule profile (the chronological audit stored with the run). A survivor
is a filter on in-sample measurements: not validated, not a forecast.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np
import pandas as pd

from edgelab.research import overview as ov

FIELD_DIMS = ("target_type", "entry_type", "trailing", "stop_type", "direction", "session")
BE_GRID = [round(x, 3) for x in np.linspace(0.02, 0.98, 49)]
MAX_RR = 20.0
WEEK = pd.Timedelta(days=7)
SURVIVOR_RULE = ("Survivor: positive net R per trade AND the recorded trade sequence passes an evaluation and reaches the "
                 "first payout under at least one prop rule profile (chronological audit stored with the run).")


def _median(v):
    v = [x for x in v if x is not None and math.isfinite(x)]
    return float(np.median(v)) if v else None


# ------------------------------------------------------------------------------ gross per-run stats (one SQL pass)
_GROSS: dict[str, Any] = {}


def gross_stats(svc) -> dict[str, dict]:
    """Per run: gross win rate, average gross winner/loser, gross R per trade (one aggregate query over stored trades)."""
    try:
        meta = svc.store._query("SELECT COUNT(*), MAX(run_id) FROM runs")[0]
    except Exception:                                        # noqa: BLE001
        return {}
    key = f"{id(svc.store)}:{meta[0]}:{meta[1]}"
    if _GROSS.get("key") == key:
        return _GROSS["rows"]
    try:
        rows = svc.store._query(
            "SELECT run_id, COUNT(*), SUM(CASE WHEN gross_r > 0 THEN 1 ELSE 0 END), "
            "AVG(CASE WHEN gross_r > 0 THEN gross_r END), AVG(CASE WHEN gross_r < 0 THEN gross_r END), AVG(gross_r) "
            "FROM trades GROUP BY run_id")
    except Exception:                                        # noqa: BLE001 - no trades table yet
        rows = []
    out = {}
    for rid, n, w, aw, al, avg in rows:
        n = int(n or 0)
        out[rid] = {"win_rate": (w or 0) / n if n else None, "avg_winner_r": ov._f(aw), "avg_loser_r": ov._f(al),
                    "avg_rr": (aw / abs(al)) if aw is not None and al not in (None, 0) else None,
                    "expectancy_r": ov._f(avg)}
    _GROSS.update(key=key, rows=out)
    return out


# ------------------------------------------------------------------------------ helpers
def _latest_scoped(svc, scope: str) -> tuple[list[dict], dict[str, list[dict]]]:
    facets = ov.library_facets(svc)
    by: dict[str, list[dict]] = {}
    for r in ov.run_records(svc):
        by.setdefault(r["strategy_id"], []).append(r)
    rows = []
    for f in facets:
        scoped = [r for r in by.get(f["strategy_id"], []) if r["status"] in ov.SCOPES[scope] and not r["holdout"]]
        rows.append({"facets": f, "ref": scoped[-1] if scoped else None})
    return rows, by


def breakeven_curves(median_cost_r: float | None) -> dict:
    """Average reward:risk needed to break even at each win rate: expectancy = WR x RR - (1 - WR) (losers taken as -1R
    on average), and the same after a per-trade cost c: WR x RR - (1 - WR) - c = 0."""
    def curve(c: float):
        pts = []
        for w in BE_GRID:
            rr = (1 - w + c) / w
            if rr <= MAX_RR:
                pts.append({"win_rate": w, "avg_rr": round(rr, 4)})
        return pts
    out = {"zero": {"label": "Break-even (expectancy 0)", "points": curve(0.0)}}
    if median_cost_r:
        out["after_cost"] = {"label": f"Break-even after the median cost of {median_cost_r:.3f} R per trade",
                             "cost_r": median_cost_r, "points": curve(median_cost_r)}
    out["note"] = ("Curves assume an average loser of -1R (the stop); actual average losers differ, so a point near a curve "
                   "is a rough reading, not a threshold.")
    return out


def _control_points(svc) -> list[dict]:
    from edgelab.research.controls import load_control_records
    pts = []
    for rec in load_control_records(svc.data_root):
        synth = any("synthetic" in str(x).lower() for x in rec.get("labels") or [])
        cand = rec.get("candidate") or {}
        for z in rec.get("realizations") or []:
            n = int(z.get("trade_count") or 0)
            if not n:
                continue
            aw, al = ov._f(z.get("avg_winner_r")), ov._f(z.get("avg_loser_r"))
            pts.append({"control_id": z.get("control_strategy_id"), "candidate_strategy_id": cand.get("strategy_id"),
                        "validation_id": rec.get("validation_id"), "realization": z.get("index"), "seed": z.get("seed"),
                        "trades": n, "win_rate": ov._f(z.get("win_rate")), "avg_rr": aw / abs(al) if aw is not None and al else None,
                        "expectancy_r": ov._f(z.get("expectancy_r")),
                        "gross_r_per_trade": (ov._f(z.get("gross_r")) or 0.0) / n,
                        "net_r": ov._f(z.get("net_r")), "max_drawdown_r": ov._f(z.get("max_drawdown_r")),
                        "max_loss_streak": z.get("max_loss_streak"), "dataset_id": (rec.get("dataset") or {}).get("dataset_id"),
                        "synthetic": synth, "sample_status": rec.get("sample_status")})
    return pts


# ------------------------------------------------------------------------------ results overview
def results_overview(svc, params: Mapping[str, Any]) -> dict:
    scope = str(params.get("scope") or "in_sample")
    if scope not in ov.SCOPES:
        raise ValueError(f"scope must be one of {sorted(ov.SCOPES)}")
    basis = str(params.get("basis") or "net")
    if basis not in ("net", "gross"):
        raise ValueError("basis must be net or gross")
    rows, _ = _latest_scoped(svc, scope)
    gs = gross_stats(svc) if basis == "gross" else {}
    tested = [x for x in rows if x["ref"] and x["ref"]["trade_count"] > 0]
    points = []
    for x in tested:
        f, r = x["facets"], x["ref"]
        g = gs.get(r["run_id"], {})
        points.append({"strategy_id": f["strategy_id"], "name": f["name"], "family_id": f["family_id"],
                       "run_id": r["run_id"], "trades": r["trade_count"], "synthetic": r["synthetic"],
                       "survivor": r["survivor"],
                       "win_rate": g.get("win_rate") if basis == "gross" else r["win_rate"],
                       "avg_rr": g.get("avg_rr") if basis == "gross" else r["avg_rr"],
                       "expectancy_r": r["gross_r_per_trade"] if basis == "gross" else r["expectancy_r"]})
    costs = [x["ref"]["cost_r_per_trade"] for x in tested]
    med_cost = _median(costs)
    survivors = [x for x in tested if x["ref"]["survivor"]]

    def group(dim: str) -> list[dict]:
        gr: dict[str, list[dict]] = {}
        for x in tested:
            gr.setdefault(str(x["facets"].get(dim) or "(not set)"), []).append(x)
        out = []
        for k, xs in sorted(gr.items()):
            vals = [(x["ref"]["gross_r_per_trade"] if basis == "gross" else x["ref"]["expectancy_r"]) for x in xs]
            out.append({"group": k, "strategies": len(xs), "median_expectancy_r": _median(vals),
                        "survivor_rate": sum(1 for x in xs if x["ref"]["survivor"]) / len(xs)})
        return out

    sig = [x for x in tested if x["facets"].get("signal_exit") == "yes"]
    fixed = [x for x in tested if x["facets"].get("signal_exit") != "yes" and x["facets"].get("target_type") == "risk_reward"]
    exp_of = lambda xs: _median([(x["ref"]["gross_r_per_trade"] if basis == "gross" else x["ref"]["expectancy_r"]) for x in xs])  # noqa: E731
    a, b = exp_of(sig), exp_of(fixed)
    multiples = sorted({x["facets"].get("target_multiple") for x in fixed if x["facets"].get("target_multiple")})
    controls = _control_points(svc) if params.get("controls") not in ("0", "false", False) else []
    for c in controls:
        if basis == "gross":
            c["expectancy_r"] = c["gross_r_per_trade"]
    return {"scope": scope, "scope_label": {"in_sample": "In-sample (exploratory)", "oos": "Out-of-sample",
                                            "walk_forward": "Walk-forward", "any": "Latest run of any status"}[scope],
            "basis": basis, "basis_label": "Gross (before costs)" if basis == "gross" else "Net of each run's stated costs",
            "facts": {"strategies": len(rows), "tested": len(tested), "survivors": len(survivors),
                      "gross_positive": sum(1 for x in tested if (x["ref"]["gross_r_per_trade"] or 0) > 0),
                      "net_positive": sum(1 for x in tested if (x["ref"]["expectancy_r"] or 0) > 0),
                      "synthetic_tested": sum(1 for x in tested if x["ref"]["synthetic"]),
                      "median_cost_r_per_trade": med_cost},
            "points": points, "controls": controls,
            "breakeven": breakeven_curves(med_cost if basis == "gross" else None),
            "breakdowns": {d: group(d) for d in FIELD_DIMS},
            "exit_comparison": {"signal_exit": {"strategies": len(sig), "median_expectancy_r": a},
                                "fixed_target": {"strategies": len(fixed), "median_expectancy_r": b,
                                                 "multiples": multiples},
                                "difference_r": (a - b) if a is not None and b is not None else None,
                                "label": "Median expectancy per strategy: exit on the opposite signal vs a fixed "
                                         "risk-multiple target (no signal exit)"},
            "eval_summary": eval_summary(svc, survivors),
            "survivor_rule": SURVIVOR_RULE, "note": ov.DESCRIPTIVE}


def eval_summary(svc, survivors: list[dict]) -> dict:
    """Bootstrapped evaluation results already computed for survivors (cache only; nothing is started here)."""
    from edgelab.prop import bootstrap as bs
    from edgelab.prop.service import default_profiles
    profiles = [p["profile_id"] for p in default_profiles(svc.root)]
    per_profile: dict[str, list[float]] = {p: [] for p in profiles}
    computed = 0
    for x in survivors:
        rid = x["ref"]["run_id"]
        try:
            rec = _record(svc, rid)
        except KeyError:
            continue
        hit = False
        for p in profiles:
            res = bs.cached(svc, rid, rec, p)
            if res and res.get("p_pass") is not None:
                per_profile[p].append(res["p_pass"])
                hit = True
        computed += hit
    return {"survivors": len(survivors), "survivors_simulated": computed,
            "median_p_pass": {p: _median(v) for p, v in per_profile.items()}, "profile_names": profile_names(svc),
            "defaults": {"replays": bs.DEFAULT_N, "block_days": bs.DEFAULT_BLOCK_DAYS, "seed": bs.DEFAULT_SEED},
            "label": "Simulated · bootstrapped evaluations of survivors' recorded trades (default settings, profile rules)"}


def profile_names(svc) -> dict[str, str]:
    """Plain names of the configured prop rule profiles ("Tradeify Growth 50K"), by profile id."""
    from edgelab.prop.service import default_profiles
    out = {}
    for p in default_profiles(svc.root):
        size = ov._f(((p.get("rules") or {}).get("account.size") or {}).get("value")) or ov._f(p.get("account_size"))
        name = " ".join(str(v) for v in (p.get("provider"), p.get("product")) if v)
        out[p["profile_id"]] = f"{name} {size / 1000:g}K" if name and size else (name or p["profile_id"])
    return out


def _record(svc, run_id: str) -> dict:
    rows = svc.store._query("SELECT record_json FROM runs WHERE run_id = ?", (run_id,))
    if not rows:
        raise KeyError(run_id)
    return ov._loads(rows[0][0]) or {}


# ------------------------------------------------------------------------------ strategy panel
DIRECTION_TEXT = {"long": "Long only", "short": "Short only", "both": "Long and short"}


def _operand_text(o: Any) -> str:
    if not isinstance(o, Mapping):
        return str(o)
    t = o.get("type")
    if t == "points":
        return f"{ov._f(o.get('points')) or o.get('points')} points"
    if t == "atr":
        return f"{o.get('multiple')} × ATR({o.get('period', 14)})" + (f" of the {o.get('timeframe')} bars" if o.get("timeframe") else "")
    if t == "risk_reward":
        return f"{o.get('multiple')}R ({o.get('multiple')} × the stop distance)"
    if t == "price":
        return "a price level from the strategy's rules"
    return str(t or "—").replace("_", " ")


def rules_in_plain_english(svc, doc: Mapping, rec: Mapping | None) -> list[dict]:
    """The strategy's rules as plain sentences, from its stored definition (and the run's recorded engine settings)."""
    from edgelab.strategy import presentation as pr
    d = doc.get("definition") or {}
    params = d.get("parameters") or {}

    def resolve(x):                                          # "$stop_atr" -> the parameter's value (text only)
        if isinstance(x, str) and x.startswith("$") and isinstance(params.get(x[1:]), Mapping):
            return params[x[1:]].get("value", params[x[1:]].get("default", x))
        if isinstance(x, Mapping):
            return {k: resolve(v) for k, v in x.items()}
        if isinstance(x, list):
            return [resolve(v) for v in x]
        return x
    entry, ex = resolve(d.get("entry") or {}), resolve(d.get("exit") or {})
    gp = ((doc.get("lineage") or [{}])[0] or {}).get("generation_parameters") or {}
    row = {"definition": d, "variation": gp.get("variation"), "family_id": doc.get("family_id"),
           "strategy_id": doc.get("strategy_id")}
    v = gp.get("variation") if isinstance(gp.get("variation"), Mapping) else None
    order = entry.get("order") or {}
    otype = str(order.get("type") or "market")
    entry_txt = {"market": "Market order at the next bar's open after the signal",
                 "stop": "Stop order beyond the signal level", "limit": "Limit order at the signal level"}.get(otype, otype)
    if otype != "market" and order.get("expiry_bars"):
        entry_txt += f", cancelled if not filled within {order['expiry_bars']} bars"
    tr = ex.get("trailing")
    if isinstance(tr, Mapping):
        mode = tr.get("mode")
        trail = {"breakeven": "Moves the stop to breakeven once the trade is in profit by the trigger",
                 "distance": f"Trails the stop at {_operand_text(tr.get('distance'))}",
                 "level": f"Trails the stop to a level ({pr.humanize(str((tr.get('level') or {}).get('feature') or 'level'))})"}.get(
            mode, pr.humanize(str(mode)))
    else:
        trail = "None"
    sess = entry.get("session")
    session_txt = pr.SESSION_TEXT.get(sess, pr.humanize(sess)) if sess else "Any time the market data covers"
    eng = ((rec or {}).get("assumptions") or {}).get("backtest_config") or svc.cfg.get("backtest") or {}
    sflat = (eng.get("session") or {}) if isinstance(eng, Mapping) else {}
    flat = (f"Flat by {sflat.get('flatten_time')} exchange time (engine setting)" if sflat.get("flatten_daily")
            else "No daily flat (engine setting)")
    filters = []
    if entry.get("trading_weekdays"):
        filters.append("weekdays " + ", ".join(pr.humanize(x) for x in entry["trading_weekdays"]))
    if v and v.get("mtf"):
        filters.append(f"higher-timeframe filter ({pr.humanize(v['mtf'].get('filter'))} on {v['mtf'].get('htf')})")
    if v and v.get("regime") not in (None, "none"):
        filters.append(pr.REGIME_TEXT.get(v.get("regime"), pr.humanize(v.get("regime"))))
    if entry.get("cooldown_bars"):
        filters.append(f"{entry['cooldown_bars']} bars between signals")
    rows = [("Signal", pr.explanation(row).split(". ")[0].rstrip(".") + "."),
            ("Family settings", pr._params_text(v.get("family_params") or {}) if v and v.get("family_params") else "—"),
            ("Direction", DIRECTION_TEXT.get(entry.get("direction"), pr.humanize(entry.get("direction")))),
            ("Entry", entry_txt), ("Stop", _operand_text(ex.get("stop"))),
            ("Target", "None" if not ex.get("target") or (ex.get("target") or {}).get("type") == "none" else _operand_text(ex.get("target"))),
            ("Trailing", trail), ("Exit on signal", "Yes, on the opposite signal" if ex.get("signal") else "No"),
            ("Session", session_txt), ("Daily flat", flat),
            ("Max trades per day", str(entry.get("max_trades_per_day")) if entry.get("max_trades_per_day") else "No limit"),
            ("Max hold", f"{ex['max_hold_bars']} bars" if ex.get("max_hold_bars") else "No limit"),
            ("Filters", "; ".join(filters) if filters else "None")]
    return [{"rule": k, "text": t} for k, t in rows]


def _window_stats(t: pd.DataFrame) -> dict:
    n = int(len(t))
    net = t["net_r"].to_numpy(float) if n else np.array([])
    return {"trades": n, "expectancy_r": float(net.mean()) if n else None, "net_r": float(net.sum()) if n else None,
            "win_rate": float((net > 0).mean()) if n else None}


def strategy_panel(svc, strategy_id: str, params: Mapping[str, Any]) -> dict:
    from edgelab.prop import bootstrap as bs
    from edgelab.strategy import presentation as pr
    scope = str(params.get("scope") or "in_sample")
    if scope not in ov.SCOPES:
        raise ValueError(f"scope must be one of {sorted(ov.SCOPES)}")
    doc = svc.library.load(strategy_id)
    f = ov.strategy_facets(doc)
    gp = ((doc.get("lineage") or [{}])[0] or {}).get("generation_parameters") or {}
    pres = pr.present({"definition": doc.get("definition") or {}, "variation": gp.get("variation"),
                       "family_id": doc.get("family_id"), "strategy_id": strategy_id, "logic_hash": doc.get("logic_hash"),
                       "definition_hash": doc.get("definition_hash")})
    rows, by = _latest_scoped(svc, scope)
    runs = by.get(strategy_id, [])
    ref = next((x["ref"] for x in rows if x["facets"]["strategy_id"] == strategy_id), None)
    risk = svc.risk_per_trade()["risk_per_trade_usd"]
    out = {"strategy_id": strategy_id, "display_name": pres["display_name"], "explanation": pres["explanation"],
           "family_id": f["family_id"], "family_name": f.get("family_name"), "facets": f, "scope": scope,
           "risk_per_trade_usd": risk, "survivor_rule": SURVIVOR_RULE,
           "technical": {"strategy_id": strategy_id, "logic_hash": doc.get("logic_hash"),
                         "definition_hash": doc.get("definition_hash"), "machine_name": pres.get("machine_name")}}
    if not ref or not ref["trade_count"]:
        out.update(tested=False, rules=rules_in_plain_english(svc, doc, None))
        return out
    rec, trades = svc.store.load_run(ref["run_id"])
    t = trades.sort_values(["exit_ts", "entry_ts"], kind="mergesort").reset_index(drop=True)
    entry = pd.DatetimeIndex(pd.to_datetime(t["entry_ts"], utc=True)).tz_convert(ov.LOCAL_TZ)
    exits = pd.DatetimeIndex(pd.to_datetime(t["exit_ts"], utc=True))
    d = rec.get("dataset") or {}
    try:
        start, end = pd.Timestamp(d.get("start")), pd.Timestamp(d.get("end"))
        n_weeks = max(1, math.ceil((end - start) / WEEK))
    except (TypeError, ValueError):
        n_weeks = None
    weeks_with = len({(x.isocalendar()[0], x.isocalendar()[1]) for x in entry})
    last = exits.max()
    w12 = t[exits >= last - pd.Timedelta(days=365)]
    oos = [r for r in runs if r["status"] in ("OUT_OF_SAMPLE", "WALK_FORWARD") and not r["holdout"]]
    holdout = [r for r in runs if r["holdout"]]
    ranked = sorted([x["ref"] for x in rows if x["ref"] and x["ref"]["trade_count"]], key=lambda r: -(r["net_r"] or -1e18))
    rank = next((i + 1 for i, r in enumerate(ranked) if r["run_id"] == ref["run_id"]), None)
    profiles = []
    names = profile_names(svc)
    for p in ref["prop"]:
        p = {**p, "profile_name": p.get("profile_name") or names.get(p["profile_id"])}
        sim = bs.cached(svc, ref["run_id"], rec, p["profile_id"]) if p["profile_id"] else None
        profiles.append({**p, "bootstrap": sim and {k: sim.get(k) for k in ("p_pass", "p_first_payout", "p_evaluation_breach",
                                                                           "median_days_to_pass", "valid_replays")}})
    hm = rec.get("headline_metrics") or {}
    out.update(
        tested=True, run_id=ref["run_id"], synthetic=ref["synthetic"], status=ref["status"], scope_label=ref["scope"],
        survivor=ref["survivor"], cost_status=ref["cost_status"],
        kpis={"expectancy_r": ref["expectancy_r"], "trades": ref["trade_count"], "trades_per_week": ref["trades_per_week"],
              "win_rate": ref["win_rate"], "avg_rr": ref["avg_rr"], "net_r": ref["net_r"],
              "net_usd_at_risk": ref["net_r"] * risk if ref["net_r"] is not None else None,
              "max_drawdown_r": ref["max_drawdown_r"],
              "max_drawdown_usd_at_risk": ref["max_drawdown_r"] * risk if ref["max_drawdown_r"] is not None else None,
              "max_loss_streak": ref["max_loss_streak"], "cost_r_per_trade": ref["cost_r_per_trade"],
              "pct_weeks_with_trade": (weeks_with / n_weeks) if n_weeks else None, "weeks_in_data": n_weeks,
              "avg_hold_minutes": ref["avg_hold_minutes"], "gross_r_per_trade": ref["gross_r_per_trade"],
              "profit_factor": ref["profit_factor"], "sample_label": ref["sample_label"],
              "net_usd_recorded": ov._f(hm.get("net_usd"))},
        last_12_months={**_window_stats(w12), "from": str((last - pd.Timedelta(days=365)).date()), "to": str(last.date()),
                        "label": "Last 12 months of the data (the window ends at the last trade, not today)"},
        out_of_sample=[{"run_id": r["run_id"], "status": r["status"], "scope": r["scope"], "trades": r["trade_count"],
                        "expectancy_r": r["expectancy_r"], "net_r": r["net_r"], "start": r["start"], "end": r["end"],
                        "net_usd_at_risk": r["net_r"] * risk if r["net_r"] is not None else None} for r in oos[-3:]],
        holdout=[{"run_id": r["run_id"], "scope": r["scope"], "trades": r["trade_count"], "expectancy_r": r["expectancy_r"],
                  "net_r": r["net_r"]} for r in holdout],
        rank={"position": rank, "of": len(ranked), "by": "total net R", "label": "Rank by total net R among strategies' "
              "latest runs in this scope (in-sample ordering, not a validation)"},
        prop=profiles, rules=rules_in_plain_english(svc, doc, rec),
        technical={**out["technical"], "run_id": ref["run_id"], "dataset_id": ref["dataset_id"],
                   "trades_hash": rec.get("trades_hash"), "config_hash": rec.get("config_hash")},
        dataset={"instrument": ref["instrument"], "provider": ref["provider"], "timeframe": ref["timeframe"],
                 "start": ref["start"], "end": ref["end"]})
    return out


def control_panel(svc, control_id: str) -> dict:
    for c in _control_points(svc):
        if c["control_id"] == control_id:
            risk = svc.risk_per_trade()["risk_per_trade_usd"]
            return {**c, "kind": "Random control", "risk_per_trade_usd": risk,
                    "net_usd_at_risk": c["net_r"] * risk if c["net_r"] is not None else None,
                    "note": "A random-entry control: entries at random moments the candidate could have traded, with the "
                            "candidate's own exits, costs and sizing. Not a strategy and never counted as one."}
    raise KeyError(f"control {control_id} is not stored in this workspace")


__all__ = ["results_overview", "strategy_panel", "control_panel", "breakeven_curves", "gross_stats", "SURVIVOR_RULE"]
