"""Phase 5 research analytics: descriptive breakdowns of STORED run trades.

Every statistic comes from the Phase 1 functions in ``analytics.metrics`` (``compute_metrics``,
``cost_sensitivity``, ``breakeven_cost_multiplier``) applied to the right subset of trades; this
module only selects, orders and labels. Nothing here is a new metric or a score.

  * Pooled: all trades of the report concatenated in entry-time order, then measured once.
    Pooled figures are NOT averages of per-dataset figures (drawdowns and streaks run across
    dataset boundaries; expectancy is weighted by trade count).
  * Per dataset: the same metrics on each dataset's trades alone.
  * Stability: counts and spread of the per-dataset figures (descriptive only).
  * Sessions: the canonical windows of ``configs/sessions.yaml``, by trade ENTRY time. Windows
    overlap (NY_AM is inside NY_RTH), so a trade counts in every window containing its entry.
  * Hours: entry hour in one stated timezone.
  * Cost sensitivity: exact post hoc (fills never depend on costs), from each trade's stored
    1x cost; gross R is unchanged by construction.

All results are historical, in-sample and descriptive.
"""
from __future__ import annotations

import math
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from edgelab.analytics.metrics import breakeven_cost_multiplier, compute_metrics, cost_sensitivity

REPORT_METRICS = ("trade_count", "sample_label", "gross_r", "net_r", "cost_r", "expectancy_r",
                  "expectancy_ci95", "profit_factor", "profit_factor_gross", "win_rate", "win_rate_ci95",
                  "max_drawdown_r", "avg_winner_r", "avg_loser_r", "best_trade_r", "worst_trade_r",
                  "max_win_streak", "max_loss_streak")
BUCKET_METRICS = ("trade_count", "sample_label", "gross_r", "net_r", "cost_r", "expectancy_r",
                  "expectancy_ci95", "profit_factor", "win_rate", "win_rate_ci95")

# Standing research caveats attached to every report that touches these data / cost profiles.
# Keys: (provider, symbol) -> notes; "SYMBOL@PROVIDER" -> note. Currently none are defined.
DATASET_NOTES: dict[tuple[str, str], tuple[str, ...]] = {}
PROFILE_NOTES: dict[str, str] = {}


def _pick(metrics: Mapping, keys: Sequence[str]) -> dict:
    return {k: metrics.get(k) for k in keys}


def pooled_trades(groups: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """All groups' trades in one frame, ordered by entry time (ties: group name), with a ``group``
    column. Deterministic for identical inputs."""
    frames = [df.assign(group=name) for name, df in groups.items() if len(df)]
    if not frames:
        return pd.DataFrame(columns=["group"])
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values(["entry_ts", "group"], kind="mergesort").reset_index(drop=True)


def pooled_summary(groups: Mapping[str, pd.DataFrame], sample_thresholds: Mapping | None = None) -> dict:
    return {"method": "pooled: every trade concatenated in entry-time order, measured once "
                      "(not an average of per-dataset metrics)",
            **_pick(compute_metrics(pooled_trades(groups), sample_thresholds=sample_thresholds),
                    REPORT_METRICS)}


def group_table(groups: Mapping[str, pd.DataFrame], sample_thresholds: Mapping | None = None) -> list[dict]:
    rows = []
    for name, df in groups.items():
        row = {"group": name, **_pick(compute_metrics(df, sample_thresholds=sample_thresholds), REPORT_METRICS)}
        row["breakeven_cost_multiplier"] = breakeven_cost_multiplier(df) if len(df) else math.nan
        rows.append(row)
    return rows


def stability_summary(rows: Sequence[Mapping]) -> dict:
    """Descriptive consistency of per-group results: counts and spread only, no score."""
    rows = [r for r in rows if r.get("trade_count")]
    n = len(rows)
    exp = np.array([r["expectancy_r"] for r in rows], float)
    net = np.array([r["net_r"] for r in rows], float)
    gross = np.array([r["gross_r"] for r in rows], float)
    pf = np.array([r["profit_factor"] if r["profit_factor"] is not None else math.nan for r in rows], float)
    return {
        "groups": n,
        "groups_net_positive": int((net > 0).sum()),
        "groups_gross_positive": int((gross > 0).sum()),
        "groups_profit_factor_above_1": int((pf > 1).sum()),
        "expectancy_r_min": float(exp.min()) if n else math.nan,
        "expectancy_r_median": float(np.median(exp)) if n else math.nan,
        "expectancy_r_max": float(exp.max()) if n else math.nan,
        "expectancy_r_std": float(exp.std(ddof=1)) if n > 1 else math.nan,
        "net_r_min": float(net.min()) if n else math.nan,
        "net_r_max": float(net.max()) if n else math.nan,
    }


def _entry_ns(trades: pd.DataFrame) -> np.ndarray:
    return pd.DatetimeIndex(trades["entry_ts"]).tz_convert("UTC").as_unit("ns").asi8   # membership() takes ns


def bucket_table(trades: pd.DataFrame, labels: pd.Series | np.ndarray, order: Iterable | None = None,
                 sample_thresholds: Mapping | None = None) -> list[dict]:
    labels = np.asarray(labels)
    keys = list(order) if order is not None else sorted(set(labels.tolist()))
    return [{"bucket": k, **_pick(compute_metrics(trades.loc[labels == k], sample_thresholds=sample_thresholds),
                                  BUCKET_METRICS)} for k in keys]


def session_breakdown(trades: pd.DataFrame, sessions: Mapping, sample_thresholds: Mapping | None = None) -> dict:
    """Metrics per canonical session window (by entry time), plus trades in none of them."""
    ns = _entry_ns(trades) if len(trades) else np.array([], dtype=np.int64)
    rows, any_in = [], np.zeros(len(trades), dtype=bool)
    for name, win in sessions.items():
        inside = win.membership(ns)[0] if len(ns) else np.zeros(0, dtype=bool)
        any_in |= inside
        rows.append({"session": name, "timezone": win.timezone, "window": f"{win.start}-{win.end}",
                     **_pick(compute_metrics(trades.loc[inside], sample_thresholds=sample_thresholds),
                             BUCKET_METRICS)})
    rows.append({"session": "(none of the listed windows)", "timezone": None, "window": None,
                 **_pick(compute_metrics(trades.loc[~any_in], sample_thresholds=sample_thresholds),
                         BUCKET_METRICS)})
    return {"note": "by trade entry time; windows overlap, so a trade counts in every window that "
                    "contains its entry (rows do not sum to the total)", "rows": rows}


def hour_breakdown(trades: pd.DataFrame, timezone: str = "America/New_York",
                   sample_thresholds: Mapping | None = None) -> dict:
    hours = (pd.DatetimeIndex(trades["entry_ts"]).tz_convert(timezone).hour if len(trades)
             else pd.Index([], dtype=int))
    labels = np.array([f"{h:02d}:00" for h in hours])
    return {"timezone": timezone, "note": "by entry hour (local wall clock)",
            "rows": bucket_table(trades, labels, sample_thresholds=sample_thresholds)}


def cost_sensitivity_table(trades: pd.DataFrame, multipliers: Sequence[float]) -> dict:
    """The existing exact cost sensitivity, with the gross and cost columns made explicit."""
    base = cost_sensitivity(trades, tuple(multipliers))
    gross = float(trades["gross_r"].sum()) if len(trades) else 0.0
    cost_1x = float(trades["cost_r_base"].sum()) if len(trades) else 0.0
    rows = [{"cost_multiplier": float(r.cost_multiplier), "gross_r": gross,
             "cost_r": float(r.cost_multiplier) * cost_1x, "net_r": float(r.net_r),
             "expectancy_r": float(r.expectancy_r), "profit_factor": float(r.profit_factor),
             "win_rate": float(r.win_rate), "trades": int(r.trades)} for r in base.itertuples()]
    return {"note": "exact post hoc from each trade's stored 1x cost; gross R does not change",
            "rows": rows,
            "breakeven_cost_multiplier": breakeven_cost_multiplier(trades) if len(trades) else math.nan}


OOS_STATUSES = frozenset({"OUT_OF_SAMPLE", "WALK_FORWARD"})


def sample_scope(records: Sequence[Mapping]) -> str:
    """Wording for the records' run statuses; a record without a status counts as IN_SAMPLE."""
    st = {r.get("status") or "IN_SAMPLE" for r in records}
    if st and st <= OOS_STATUSES:
        return "out-of-sample"
    return "in-sample and out-of-sample" if st & OOS_STATUSES else "in-sample"


def research_labels(records: Sequence[Mapping], instruments: Mapping | None = None) -> list[str]:
    """Caveats every report must carry, derived from what the runs recorded."""
    labels = [f"Historical, {sample_scope(records)}, descriptive results under the stated assumptions; "
              "not a forecast and not by itself evidence of a trading edge."]
    if any(str(r.get("notes", "")).startswith("SYNTHETIC") for r in records):
        labels.append("SYNTHETIC data included - engine testing only, no market conclusions.")
    statuses = sorted({str((r.get("assumptions") or {}).get("cost_status")) for r in records})
    profiles = sorted({str(((r.get("assumptions") or {}).get("costs") or {}).get("profile")) for r in records})
    if set(statuses) - {"broker_verified"}:
        labels.append(f"Costs are not broker-verified (cost status: {', '.join(statuses)}; "
                      f"profile: {', '.join(profiles)}).")
    for p in profiles:
        if p in PROFILE_NOTES:
            labels.append(PROFILE_NOTES[p])
    seen = set()
    for r in records:
        d = r.get("dataset") or {}
        key = (d.get("provider"), d.get("instrument"))
        if key in seen:
            continue
        seen.add(key)
        labels.extend(DATASET_NOTES.get(key, ()))
        inst = (instruments or {}).get(d.get("instrument"))
        if inst is not None and getattr(inst, "extra", {}).get("research_proxy"):
            labels.append(f"{d.get('instrument')} is a research proxy, not a tradable contract.")
        if d.get("price_basis") in ("bid", "ask"):
            labels.append(f"{d.get('instrument')}: {str(d['price_basis']).upper()}-only prices; "
                          f"stops and targets trigger on that side for both directions.")
    return list(dict.fromkeys(labels))
