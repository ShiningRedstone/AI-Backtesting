"""Capability matrix of the strategy factory (ADR-62): what each variation dimension REALLY does.

Every row names exactly one status and the code that backs it (``evidence`` = ``module:qualified.name``,
resolved by a test). The factory may only vary dimensions whose status is ``executable`` (or ``partial``
with the limitation stated); everything else is listed here with the reason and is refused by name.

Statuses
  executable      implemented and numerically executed by the engine (DSL -> compiler -> engine)
  partial         implemented, with a stated limitation
  not_executable  not executed by the engine; never approximated; refused by name
  data_unavailable  the canonical (Dukascopy) data cannot support it

Documentation is generated from this data (``render_markdown``) and drift-tested.
"""
from __future__ import annotations

import importlib
from typing import Any

from edgelab.engine.sizing import DEFAULT_RESEARCH_ACCOUNT
from edgelab.strategy import factory_space as S

EXEC, PARTIAL, NOT_EXEC, NO_DATA = "executable", "partial", "not_executable", "data_unavailable"
STATUSES = (EXEC, PARTIAL, NOT_EXEC, NO_DATA)


def _row(dim: str, status: str, exposed: str, evidence: list, note: str) -> dict:
    return {"dimension": dim, "status": status, "factory": exposed, "evidence": evidence, "note": note}


def matrix() -> list[dict]:
    tfs = "/".join(S.TIMEFRAMES)
    trail = ", ".join(k for k in S.TRAIL_KINDS if k != "none")
    return [
        _row("1 Timeframe", EXEC, f"varied: {tfs}",
             ["edgelab.strategy.compiler:DSLStrategy.bind", "edgelab.data.importer:import_dataset"],
             "A strategy runs only on a dataset of its own timeframe (bind refuses otherwise). Every timeframe is a "
             "multiple of 1m and is derived at import (`derive_timeframes`); the search marks a mismatched dataset "
             "ineligible. The campaign needs a stored dataset per timeframe."),
        _row("2 MTF", EXEC, "varied: " + ", ".join(f"{k}->{'/'.join(v)}" for k, v in S.MTF_PAIRS.items() if v),
             ["edgelab.features.engine:FeatureEngine.compute", "edgelab.engine.signals:check_causality"],
             "Higher-timeframe operands are the last COMPLETED higher-timeframe bar (feature engine); filters: "
             + ", ".join(S.MTF_FILTERS) + "."),
        _row("3 Trading / session windows", EXEC, "varied: " + ", ".join(S.ALL_SESSIONS),
             ["edgelab.features.library.session_levels:_session", "edgelab.strategy.daytrading:validate_day_trading"],
             "Wall-clock windows in their own IANA timezone (DST-correct). Entry and hold windows are strategy-local; "
             "a forced flat inside one New York trading date is required for every strategy."),
        _row("4 Entry parameters", EXEC, "varied: family parameters, confirmation, entry delay, order type",
             ["edgelab.strategy.compiler:_Eval", "edgelab.engine.fills:find_entry"],
             "Conditions over registered causal features; market / stop / limit entries with expiry."),
        _row("5 Fixed-point stop", EXEC, "varied: points by timeframe",
             ["edgelab.engine.signals:OrderSpec", "edgelab.engine.fills:simulate_exit"],
             "Ticks are points x tick size (offered once, as points)."),
        _row("6 Dynamic structural / indicator stop", EXEC,
             "varied: " + ", ".join(list(S.STOP_DOMAINS) + ["range_side", "zone"]),
             ["edgelab.strategy.compiler:DSLStrategy._orders"],
             "Price stops from causal features; a stop on the wrong side of the entry is skipped and counted. "
             "Short stops are levels of the dataset's (BID) series tested against ASK touches, as for every price stop."),
        _row("7 Fixed-point target", EXEC, "varied: points, reward:risk, ATR multiple",
             ["edgelab.engine.signals:OrderSpec", "edgelab.strategy.compiler:DSLStrategy._orders"], ""),
        _row("8 Dynamic target", EXEC, "varied: " + ", ".join(k for k in S.TARGET_DOMAINS if k not in ("none", "points", "rr", "atr")),
             ["edgelab.strategy.compiler:DSLStrategy._orders"],
             "Price targets from causal features (prior-day level, swing, session level, moving average, band, range)."),
        _row("9 Fixed exit time / time stop", EXEC, "varied: window, " + ", ".join(str(k) for k in S.TIME_EXITS if k != "window") + " min",
             ["edgelab.engine.fills:simulate_exit", "edgelab.strategy.daytrading:flat_condition"],
             "max_hold_bars (exit at the close of the Nth bar) and a strategy-local flat time (clock time in the window's timezone)."),
        _row("10 Signal-based exit", EXEC, "varied: none, opposite_signal, reversal",
             ["edgelab.engine.backtester:run_backtest"], "Executes at the next bar's open; a gap through the stop fills at the open."),
        _row("11 Trailing stop", EXEC, f"varied: {trail}",
             ["edgelab.engine.fills:simulate_exit_trailing", "edgelab.strategy.dsl:_validate_trailing"],
             "ADR-61: bar-close decisions effective next bar, ratchet only, exit-side quote extremes."),
        _row("12 Breakeven", EXEC, "varied: breakeven kind, and breakeven-before-trail",
             ["edgelab.engine.fills:simulate_exit_trailing"], "Trigger points / R / ATR, offset points."),
        _row("13a Execution contract (MNQ, whole contracts)", EXEC, f"every strategy trades {S.EXECUTION_CONTRACT}",
             ["edgelab.instruments:execution_view", "edgelab.engine.sizing:check_quantity",
              "edgelab.engine.backtester:run_backtest"],
             "ADR-63: the ONLY contract specification is the MNQ entry of configs/instruments.yaml (tick 0.25, tick value "
             "$0.50 -> $2 per point, whole contracts). The Dukascopy index-CFD series stays the DATA; 1 index point = "
             "$2 per contract. Prices are not snapped to the contract tick. A strategy naming a contract without its "
             "spec, or MNQ on a different futures series, is refused."),
        _row("13 Fixed position size", EXEC, "varied: fixed_1 (1 whole contract)",
             ["edgelab.engine.sizing:size_trade", "edgelab.engine.sizing:check_quantity"],
             "A fixed quantity for a named contract must be a whole number (DSL and engine both refuse fractions)."),
        _row("14 Fixed-dollar-risk sizing", EXEC, "varied: risk_250/500/1000, optional cap",
             ["edgelab.engine.sizing:contracts_for_risk"],
             "contracts = floor(budget / (planned stop points x point value)); exact decimal arithmetic, never rounds "
             "up; 0 = trade rejected; the optional cap is a hard deterministic limit."),
        _row("15 Dynamic / equity-based risk sizing", EXEC, "varied: " + ", ".join(S.EQUITY_RISK),
             ["edgelab.engine.sizing:contracts_for_risk", "edgelab.engine.backtester:run_backtest"],
             "ADR-62/64: risk = risk_pct % of equity at the signal; equity = the RUN ACCOUNT's starting equity (default "
             f"${DEFAULT_RESEARCH_ACCOUNT['starting_equity']:,.0f} research account; never part of a strategy's identity) + net P&L of "
             "trades already exited. No open or future trade is visible; quantity from the planned initial stop, rounded down, "
             f"capped at {S.RISK_QUANTITY_CAP} micros. Path-dependent within one run; R metrics are unaffected."),
        _row("16a Filters from OHLC (canonical Dukascopy)", EXEC, "varied: " + ", ".join(r for r in S.REGIMES if r != "none"),
             ["edgelab.features.library.indicators:_adx", "edgelab.features.library.session_levels:_daily"],
             "Only price-derived features are used."),
        _row("16b VWAP / volume filters and levels", NO_DATA, "NOT varied",
             ["edgelab.features.engine:FeatureEngine._check_requirements"],
             "Dukascopy volume is provider-defined decimal volume, NOT CME exchange volume. ADR-62 makes the feature "
             "engine refuse volume-weighted features for such instruments (structural, not just unused)."),
        _row("16c Event / news filters", NO_DATA, "NOT varied", [],
             "No trustworthy event dataset exists; event information is never fabricated."),
        _row("17a Re-entry: cooldown between signals", EXEC, "varied: none, 15/30/60 min, one per window",
             ["edgelab.strategy.compiler:DSLStrategy._apply_cooldown"], "Measured between signals."),
        _row("17b Re-entry: after a trade exit", EXEC, "varied: none, block day after stop, block day after target, cooldown after exit 15/30/60 min",
             ["edgelab.engine.backtester:run_backtest", "edgelab.strategy.dsl:_validate_resolved"],
             "ADR-62: decided from exits that have already happened (`entry.reentry`)."),
        _row("17c Re-entry: one direction per session", NOT_EXEC, "NOT varied", [],
             "Needs per-session direction state in the signal layer; excluded this phase."),
        _row("18a Per-strategy trade cap", EXEC, "varied: none, 1, 2, 3 executed trades per trading date",
             ["edgelab.engine.backtester:run_backtest", "edgelab.strategy.dsl:_validate_resolved"],
             "ADR-62: `entry.max_trades_per_day`, combined with the config cap by minimum; counts executed trades."),
        _row("18b Global trade cap (config)", EXEC, "not varied (backtest config, part of the frozen protocol)",
             ["edgelab.engine.backtester:run_backtest"], "`backtest.max_trades_per_day`."),
        _row("19a No-progress exit", EXEC, "varied: bars 3/6/12, progress in R / ATR / points",
             ["edgelab.engine.fills:simulate_exit_trailing", "edgelab.strategy.dsl:_validate_no_progress"],
             "ADR-62: at the close of bar N since entry, exit if the favorable excursion is below the threshold."),
        _row("19b Stop-distance bounds", EXEC, "varied: none, 0.5-3 ATR",
             ["edgelab.strategy.compiler:_Eval"], "An entry filter on the initial stop distance."),
        _row("19c Direction, weekday filter", EXEC, "varied: long/short/both; weekday sets",
             ["edgelab.strategy.compiler:DSLStrategy._entry_allowed"], "Weekday = the calendar trading date's weekday."),
        _row("19d Prior-day range", PARTIAL, "varied: contraction (NR7) only",
             ["edgelab.features.library.smc:_daily_nr"], "Contraction is a filter; expansion has no feature (declared unavailable)."),
        _row("19e Partial exits, scaling, pyramiding, target trailing, tiered equity scaling", NOT_EXEC, "NOT varied", [],
             "One fill in, one fill out; the target is fixed; equity-tier tables would be arbitrary additions."),
    ]


def verify_evidence() -> list[str]:
    """Return the evidence references that do not resolve to a real object (empty = all resolve)."""
    bad = []
    for r in matrix():
        for ref in r["evidence"]:
            mod, _, qual = ref.partition(":")
            try:
                obj: Any = importlib.import_module(mod)
                for part in qual.split("."):
                    obj = getattr(obj, part)
            except (ImportError, AttributeError):
                bad.append(f"{r['dimension']}: {ref}")
    return bad


def render_markdown() -> str:
    lines = ["# Strategy-factory capability matrix",
             "",
             "Generated by `python -m edgelab.cli factory capabilities` from `edgelab/strategy/capabilities.py` "
             "(drift-tested). Each row names one status and the code that backs it.",
             "",
             "Statuses: **executable** (implemented and numerically executed), **partial** (implemented with a stated "
             "limitation), **not_executable** (not executed by the engine; refused by name, never approximated), "
             "**data_unavailable** (the canonical Dukascopy data cannot support it).",
             "",
             "| Dimension | Status | In the factory | Evidence | Notes |", "|---|---|---|---|---|"]
    for r in matrix():
        ev = "<br>".join(f"`{e}`" for e in r["evidence"]) or "-"
        lines.append(f"| {r['dimension']} | **{r['status']}** | {r['factory']} | {ev} | {r['note']} |")
    return "\n".join(lines) + "\n"
