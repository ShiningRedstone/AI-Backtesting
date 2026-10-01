/* Plain-English labels for the interface. Machine identifiers (strategy / dataset / run ids, hashes) are shown
   only inside "Technical details"; everything else reads as words. Pure text formatting: no research numbers are
   computed here. */

const ACRONYMS = new Set(["atr", "ema", "sma", "vwap", "rsi", "fvg", "orb", "ny", "bos", "smc", "ict", "oos", "pdhl", "mtf",
  "usd", "cfd", "nq", "es", "mnq", "rth", "eth", "pf", "dd", "id", "ui", "ai"]);

/** snake_case / kebab-case / camelCase / SHOUTING_CASE -> "Sentence case words" (acronyms kept upper case). */
export function humanize(raw: unknown): string {
  if (raw === null || raw === undefined) return "—";
  const s = String(raw).trim();
  if (!s) return "—";
  const words = s.replace(/([a-z0-9])([A-Z])/g, "$1 $2").split(/[_\-\s]+/).filter(Boolean)
    .map((w) => (ACRONYMS.has(w.toLowerCase()) ? w.toUpperCase() : w.toLowerCase()));
  if (!words.length) return "—";
  const first = words[0];
  words[0] = first === first.toUpperCase() ? first : first.charAt(0).toUpperCase() + first.slice(1);
  return words.join(" ");
}

const FACET_VALUES: Record<string, Record<string, string>> = {
  entry_type: { market: "Market order", limit: "Limit order", stop: "Stop order" },
  stop_type: { atr: "ATR multiple", points: "Fixed points", price: "Price level", swing: "Last swing", risk_reward: "Risk multiple" },
  target_type: { atr: "ATR multiple", points: "Fixed points", price: "Price level", risk_reward: "Risk multiple (R)", none: "No target" },
  trailing: { none: "No trailing stop", breakeven: "Move to breakeven", distance: "Trailing distance", level: "Trail to a level" },
  signal_exit: { yes: "Exits on the opposite signal", no: "No signal exit" },
  direction: { long: "Long only", short: "Short only", both: "Long and short" },
  source: { user: "Written by you", core: "Core library", variation: "Variation", factory: "Strategy factory", ai: "AI proposal",
    ai_proposal: "AI proposal", mode_a: "Variation", mode_b: "AI proposal" },
  session: { NY_RTH: "New York regular hours", NY_0930_1100: "New York 09:30 to 11:00", ny_rth: "New York regular hours",
    ny_open: "New York open", ny_morning: "New York morning", ny_afternoon: "New York afternoon", asia: "Asia session",
    london: "London session", london_ny_overlap: "London and New York overlap" },
};

/** A facet / enum value as words (e.g. target_type "risk_reward" -> "Risk multiple (R)"; timeframe "5m" -> "5 min"). */
export function facetLabel(key: string, value: unknown): string {
  if (value === null || value === undefined || value === "") return key === "session" ? "Any time" : "—";
  const v = String(value);
  if (key === "timeframe") { const m = /^(\d+)m$/.exec(v); if (m) return +m[1] === 60 ? "1 hour" : `${m[1]} min`;
    const h = /^(\d+)h$/.exec(v); if (h) return `${h[1]} hour${h[1] === "1" ? "" : "s"}`; if (v === "1d") return "Daily"; }
  if (v === "(not set)") return "Not set";
  return FACET_VALUES[key]?.[v] ?? humanize(v);
}

const STATUS: Record<string, string> = {
  IN_SAMPLE: "In-sample", OUT_OF_SAMPLE: "Out-of-sample", WALK_FORWARD: "Walk-forward", HOLDOUT: "Holdout",
  PASS: "Passed", FAIL: "Failed", INCOMPATIBLE: "Not compatible", NOT_APPLICABLE: "Not applicable",
  NOT_PASSED_BY_END_OF_DATA: "Not passed by the end of the data", ACTIVE: "Active", SUPERSEDED: "Superseded",
  running: "Running", done: "Finished", error: "Error", completed: "Completed", interrupted: "Interrupted", cancelled: "Cancelled",
  queued: "Queued", failed: "Failed",
};
/** A status or reason code as words. */
export const statusLabel = (s: unknown) => (s === null || s === undefined ? "—" : STATUS[String(s)] ?? humanize(s));

/** A prop rule profile: its stored name, else words from its id. */
export const profileLabel = (id: string | null | undefined, name?: string | null) =>
  name || (id ? humanize(id.replace(/_(\d+)K$/, " $1K")) : "—");

/** Metric keys -> plain English (used wherever a metrics object is listed). */
export const METRIC_LABEL: Record<string, string> = {
  trade_count: "Trades", trades: "Trades", expectancy_r: "Net R per trade", net_r: "Total net R", gross_r: "Total gross R",
  cost_r: "Total costs (R)", win_rate: "Win rate", profit_factor: "Profit factor", max_drawdown_r: "Max drawdown (R)",
  avg_winner_r: "Average winner (R)", avg_loser_r: "Average loser (R)", avg_r: "Average R", median_r: "Median R",
  trades_per_week: "Trades per week", sample_label: "Sample size", sharpe_like: "Sharpe-like ratio", net_usd: "Net P&L ($)",
  gross_usd: "Gross P&L ($)", cost_usd: "Costs ($)", max_loss_streak: "Longest losing streak", max_win_streak: "Longest winning streak",
  expectancy_ci_low: "Net R per trade, low end of range", expectancy_ci_high: "Net R per trade, high end of range",
  expectancy_ci: "Net R per trade, uncertainty range", payoff_ratio: "Average reward to risk", avg_hold_minutes: "Average hold (minutes)",
  long_trades: "Long trades", short_trades: "Short trades", recovery_factor: "Recovery factor", skipped: "Skipped signals",
  n_signals: "Signals", gross_r_per_trade: "Gross R per trade", cost_r_per_trade: "Cost per trade (R)",
};
export const metricLabel = (k: string) => METRIC_LABEL[k] ?? humanize(k);

/** A dataset id as words ("NQ_FUTURE_SYNTHETIC_DEMO_5M_0A42F5A624" -> "NQ future synthetic demo 5m"): the trailing content
    hash is dropped; the full id stays available under Technical details. */
export const datasetLabel = (id: string | null | undefined) => (id ? humanize(id.replace(/_[0-9A-F]{8,}$/, "")) : "—");

/** A strategy's machine name as words ("mtf_trend_pullback" -> "MTF trend pullback"). */
export const strategyLabel = (name: string | null | undefined) => humanize(name ?? "—");

/** A strategy family: its stored name, else words from its id. */
export const familyLabel = (id: string | null | undefined, name?: string | null) => name || humanize(id ?? "—");

/** Any key of a stored object (metric, setting, field) as words. */
export const keyLabel = (k: string) => METRIC_LABEL[k] ?? humanize(k);

/** A stored scalar value for display: enum-like snake_case / SHOUTING_CASE strings become words; ids are left to the caller. */
export function valueLabel(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 4 });
  if (Array.isArray(v)) return v.map(valueLabel).join(", ");
  const s = String(v);
  if (STATUS[s]) return STATUS[s];
  return /^[A-Za-z0-9]+(_[A-Za-z0-9]+)+$/.test(s) ? humanize(s) : s;
}
