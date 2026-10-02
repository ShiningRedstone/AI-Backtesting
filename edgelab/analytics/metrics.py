"""Core trade metrics (Phase 1 subset; Phase 5 adds breakdowns & distributions).

Principles enforced here:
  * Sample size travels with every statistic (``n``, and a sample label).
  * Win rate is never reported alone: expectancy, profit factor, average win/loss
    and drawdown come with it.
  * Expectancy carries a standard error and a normal-approx 95% CI; the win rate a
    Wilson interval. These describe HISTORICAL SAMPLE uncertainty, not a forecast.
"""
from __future__ import annotations

import math
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

DEFAULT_SAMPLE = {"min_trades": 100, "preferred_trades": 500}


def sample_label(n: int, thresholds: Mapping | None = None) -> str:
    t = {**DEFAULT_SAMPLE, **(thresholds or {})}
    if n < t["min_trades"]:
        return "LOW SAMPLE SIZE"
    if n < t["preferred_trades"]:
        return "MODERATE SAMPLE"
    return "ADEQUATE SAMPLE"


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (centre - half, centre + half)


def _streaks(sign: np.ndarray) -> tuple[int, int]:
    best_w = best_l = cur_w = cur_l = 0
    for s in sign:
        if s > 0:
            cur_w, cur_l = cur_w + 1, 0
        elif s < 0:
            cur_l, cur_w = cur_l + 1, 0
        else:
            cur_w = cur_l = 0
        best_w, best_l = max(best_w, cur_w), max(best_l, cur_l)
    return best_w, best_l


def max_drawdown(pnl: np.ndarray) -> float:
    """Largest peak-to-trough decline of the cumulative series (starting from 0)."""
    if len(pnl) == 0:
        return 0.0
    eq = np.r_[0.0, np.cumsum(pnl)]
    return float(np.max(np.maximum.accumulate(eq) - eq))


def profit_factor(x: np.ndarray) -> float:
    gains, losses = x[x > 0].sum(), -x[x < 0].sum()
    if losses == 0:
        return math.inf if gains > 0 else math.nan
    return float(gains / losses)


def trades_per_week(n: int, start, end) -> float | None:
    """Trades per calendar week of the TESTED window ``[start, end]`` (first to last bar of the evaluated data; ADR-89).
    Dividing by the first-entry-to-last-exit span instead made one 1-minute trade read as 10,080 per week."""
    try:
        days = (pd.Timestamp(end) - pd.Timestamp(start)).total_seconds() / 86400
    except (TypeError, ValueError):
        return None
    if not math.isfinite(days) or days <= 0:
        return None
    return n / max(days, 1.0) * 7


def recorded_trades_per_week(rec: Mapping) -> float | None:
    """``trades_per_week`` of a stored run record, from its evaluated dataset window (stored runs are corrected at read
    time; nothing is re-run). Falls back to the stored value when the record has no window."""
    d, hm = rec.get("dataset") or {}, rec.get("headline_metrics") or {}
    v = trades_per_week(int(hm.get("trade_count") or 0), d.get("start"), d.get("end")) if d.get("start") and d.get("end") else None
    return v if v is not None else hm.get("trades_per_week")


def compute_metrics(trades: pd.DataFrame, r_col: str = "net_r",
                    sample_thresholds: Mapping | None = None, span: tuple | None = None) -> dict:
    """``span`` = (first, last) bar of the tested data: ``trades_per_week`` per week of that window (ADR-89).
    Without it the historical first-entry-to-last-exit formula is kept (Phase 1 demo output unchanged)."""
    n = len(trades)
    out: dict = {"trade_count": n, "sample_label": sample_label(n, sample_thresholds)}
    if n == 0:
        return out
    r = trades[r_col].to_numpy(float)
    usd = trades["net_usd"].to_numpy(float)
    wins, losses = r[r > 0], r[r < 0]
    span_days = max((trades["exit_ts"].max() - trades["entry_ts"].min()).total_seconds() / 86400, 1e-9)
    std = float(r.std(ddof=1)) if n > 1 else math.nan
    se = std / math.sqrt(n) if n > 1 else math.nan
    wr_lo, wr_hi = wilson_interval(len(wins), n)
    streak_w, streak_l = _streaks(np.sign(r))
    downside = r[r < 0]
    dd_std = float(np.sqrt(np.mean(np.minimum(r, 0) ** 2))) if n else math.nan
    out.update({
        "trades_per_week": (tpw if span is not None and (tpw := trades_per_week(n, *span)) is not None
                            else n / span_days * 7),
        "win_rate": len(wins) / n, "win_rate_ci95": (wr_lo, wr_hi),
        "loss_rate": len(losses) / n,
        "expectancy_r": float(r.mean()), "expectancy_se": se,
        "expectancy_ci95": (float(r.mean() - 1.96 * se), float(r.mean() + 1.96 * se)) if n > 1 else (math.nan, math.nan),
        "median_r": float(np.median(r)),
        "avg_winner_r": float(wins.mean()) if len(wins) else math.nan,
        "avg_loser_r": float(losses.mean()) if len(losses) else math.nan,
        "profit_factor": profit_factor(r),
        "profit_factor_gross": profit_factor(trades["gross_r"].to_numpy(float)),
        "gross_r": float(trades["gross_r"].sum()), "cost_r": float(trades["cost_r"].sum()),
        "net_r": float(r.sum()), "net_usd": float(usd.sum()),
        "max_drawdown_r": max_drawdown(r), "max_drawdown_usd": max_drawdown(usd),
        "max_win_streak": streak_w, "max_loss_streak": streak_l,
        "avg_hold_minutes": float(trades["holding_minutes"].mean()),
        "median_hold_minutes": float(trades["holding_minutes"].median()),
        "best_trade_r": float(r.max()), "worst_trade_r": float(r.min()),
        "std_r": std,
        "sharpe_like_per_trade": float(r.mean() / std) if std and std > 0 else math.nan,
        "sortino_like_per_trade": float(r.mean() / dd_std) if dd_std > 0 else math.nan,
        "exit_reasons": trades["exit_reason"].value_counts().to_dict(),
        "conflict_bars": int((trades["conflict_resolution"] != "").sum()),
    })
    return out


def cost_sensitivity(trades: pd.DataFrame, multipliers: Sequence[float] = (0.5, 1, 1.5, 2, 3)) -> pd.DataFrame:
    """Exact: fills do not depend on costs, so net_R(k) = gross_R - k * cost_R(1x)."""
    rows = []
    for k in multipliers:
        r = (trades["gross_r"] - k * trades["cost_r_base"]).to_numpy(float)
        rows.append({"cost_multiplier": k, "trades": len(r),
                     "expectancy_r": float(r.mean()) if len(r) else math.nan,
                     "profit_factor": profit_factor(r) if len(r) else math.nan,
                     "net_r": float(r.sum()), "win_rate": float((r > 0).mean()) if len(r) else math.nan})
    return pd.DataFrame(rows)


def breakeven_cost_multiplier(trades: pd.DataFrame) -> float:
    """Cost multiple at which total net R reaches zero (inf if costs are zero)."""
    g, c = trades["gross_r"].sum(), trades["cost_r_base"].sum()
    if c <= 0:
        return math.inf
    return float(g / c)


def format_metrics(m: Mapping) -> str:
    if m.get("trade_count", 0) == 0:
        return "  no trades"
    lo, hi = m["expectancy_ci95"]
    wlo, whi = m["win_rate_ci95"]
    return "\n".join([
        f"  trades={m['trade_count']:,} [{m['sample_label']}]  trades/week={m['trades_per_week']:.1f}",
        f"  expectancy={m['expectancy_r']:+.3f}R  (95% CI {lo:+.3f}..{hi:+.3f}, se {m['expectancy_se']:.3f})",
        f"  profit factor={m['profit_factor']:.3f} (gross {m['profit_factor_gross']:.3f})"
        f"  win rate={m['win_rate']:.1%} (CI {wlo:.1%}..{whi:.1%})",
        f"  avg win={m['avg_winner_r']:+.2f}R  avg loss={m['avg_loser_r']:+.2f}R  median={m['median_r']:+.2f}R",
        f"  gross={m['gross_r']:+.1f}R  costs={m['cost_r']:.1f}R  net={m['net_r']:+.1f}R  (${m['net_usd']:,.0f})",
        f"  maxDD={m['max_drawdown_r']:.1f}R (${m['max_drawdown_usd']:,.0f})  "
        f"streaks W{m['max_win_streak']}/L{m['max_loss_streak']}  hold med {m['median_hold_minutes']:.0f}m",
    ])
