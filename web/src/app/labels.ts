/* Plain-English labels for the interface. Machine identifiers (strategy / dataset / run ids, hashes) are shown
   only inside "Technical details"; everything else reads as words. Pure text formatting: no research numbers are
   computed here. */

const ACRONYMS = new Set(["atr", "ema", "sma", "vwap", "rsi", "fvg", "orb", "ny", "bos", "smc", "ict", "oos", "pdhl", "mtf",
  "usd", "cfd", "nq", "es", "mnq", "rth", "eth", "pf", "dd", "id", "ui", "ai", "nas100", "us100", "utc", "mae", "mfe", "cme"]);

/** snake_case / kebab-case / camelCase / SHOUTING_CASE -> "Sentence case words" (acronyms kept upper case). */
export function humanize(raw: unknown): string {
  if (raw === null || raw === undefined) return "—";
  const s = String(raw).trim();
  if (!s) return "—";
  const words = s.replace(/([a-z])([A-Z])/g, "$1 $2").split(/[_\-\s]+/).filter(Boolean)
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

const CITY: Record<string, string> = { NY: "New York", LDN: "London", TYO: "Tokyo", FRA: "Frankfurt", SYD: "Sydney", HK: "Hong Kong", CHI: "Chicago" };
const DAY: Record<string, string> = { MO: "Mon", TU: "Tue", WE: "Wed", TH: "Thu", FR: "Fri", SA: "Sat", SU: "Sun" };
/** Generated session ids (strategy factory, e.g. "FXE_NY_1200_1430_MF") -> "New York 12:00–14:30 · Mon–Fri". */
function factorySession(v: string): string | null {
  const m = /^[A-Z]+_([A-Z]+)_(\d{2})(\d{2})_(\d{2})(\d{2})_([A-Z]+)$/i.exec(v);
  if (!m) return null;
  const days = m[6].toUpperCase() === "MF" ? "Mon–Fri" : (m[6].toUpperCase().match(/.{2}/g) ?? []).map((d) => DAY[d] ?? d).join(", ");
  return `${CITY[m[1].toUpperCase()] ?? m[1]} ${m[2]}:${m[3]}–${m[4]}:${m[5]}${days ? ` · ${days}` : ""}`;
}

/** An instrument id as words: "NQ_DUKASCOPY" -> "NQ (Dukascopy)", "NAS100_CFD" -> "NAS100 (CFD)", "NQ" -> "NQ". */
export function instrumentLabel(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  const [head, ...rest] = String(v).split("_");
  return rest.length ? `${head} (${humanize(rest.join("_")).replace(/^./, (c) => c.toUpperCase())})` : head;
}

/** A facet / enum value as words (e.g. target_type "risk_reward" -> "Risk multiple (R)"; timeframe "5m" -> "5 min"). */
export function facetLabel(key: string, value: unknown): string {
  if (value === null || value === undefined || value === "") return key === "session" ? "Any time" : "—";
  const v = String(value);
  if (key === "timeframe") { const m = /^(\d+)m$/.exec(v); if (m) return +m[1] === 60 ? "1 hour" : `${m[1]} min`;
    const h = /^(\d+)h$/.exec(v); if (h) return `${h[1]} hour${h[1] === "1" ? "" : "s"}`; if (v === "1d") return "Daily"; }
  if (v === "(not set)") return "Not set";
  if (key === "session") { const fs = factorySession(v); if (fs) return fs; }
  if (key === "instrument") return instrumentLabel(v);
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
  expectancy_se: "Net R per trade, standard error", expectancy_ci95: "Net R per trade, 95% range", std_r: "Spread of trade results (R)",
  win_rate_ci95: "Win rate, 95% range", loss_rate: "Loss rate", best_trade_r: "Best trade (R)", worst_trade_r: "Worst trade (R)",
  profit_factor_gross: "Profit factor before costs", sharpe_like_per_trade: "Sharpe-like ratio per trade",
  sortino_like_per_trade: "Sortino-like ratio per trade", max_drawdown_usd: "Max drawdown ($)", median_hold_minutes: "Median hold (minutes)",
  conflict_bars: "Bars where stop and target were both touched", avg_hold_bars: "Average hold (bars)", median_hold_bars: "Median hold (bars)",
};
export const metricLabel = (k: string) => METRIC_LABEL[k] ?? humanize(k);

/** A dataset id as words ("NQ_FUTURE_SYNTHETIC_DEMO_5M_0A42F5A624" -> "NQ future synthetic demo 5m"): the trailing content
    hash is dropped; the full id stays available under Technical details. */
export const datasetLabel = (id: string | null | undefined) => (id ? humanize(id.replace(/_[0-9A-F]{8,}(?=_|$)/g, ""))
  .replace(/ (\d{4})(\d{2})(\d{2})\b/g, " from $1-$2-$3") : "—");

/** A strategy's machine name as words ("mtf_trend_pullback" -> "MTF trend pullback"). */
export const strategyLabel = (name: string | null | undefined) => humanize((name ?? "—").replace(/[_ ][0-9a-f]{6,}$/i, ""));

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
  return /^[A-Za-z0-9]+(_[A-Za-z0-9]+)+$/.test(s) || /^[A-Z][A-Z0-9]*( [A-Z0-9]+)+$/.test(s) ? humanize(s) : s;
}

const RECORD_ID = /^(STR|RUN|CTRL|VAL|RP|CMP|FM|AIP|PB|SB|VB|SRCH|JOB|CR)_/;
/** Backend prose with code tokens turned into words: "cost profile 'NQ_CFD' is unconfigured" -> "… 'NQ CFD' …",
    "America/New_York" -> "America/New York", "reason END_OF_DATA" -> "reason end of data", dotted rule keys
    ("evaluation.drawdown.mode") -> "Evaluation · drawdown · mode". Record ids (STR_…, RUN_…) and file names are kept. */
export function plainProse(t: unknown): string {
  return String(t ?? "").replace(/\bEdgeLab\b/g, "Munyun Lab")
    .replace(/\b[a-z][a-z0-9_]{2,}(?:\.[a-z][a-z0-9_]{1,})+\b/g, (m) => /\.(json|ya?ml|md|csv|py|txt|exe|zip)$/.test(m) ? m
      : m.split(".").map((x, i) => (i ? humanize(x).replace(/^[A-Z][a-z]/, (c) => c.toLowerCase()) : humanize(x))).join(" · "))
    .replace(/\b[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+\b/g, (m) => {
      if (RECORD_ID.test(m)) return m;
      const s = m.replace(/_[0-9A-F]{8,}$/, "");
      const parts = s.split("_");
      if (parts.every((p) => /^[A-Z][a-z]+$/.test(p))) return parts.join(" ");             // New_York
      if (parts.some((p) => /^[A-Z0-9]{2,}$/.test(p) && /\d|^[A-Z]{2,5}$/.test(p)) && /^[A-Z0-9_]+$/.test(s)
        && parts.length <= 3 && parts.some((p) => /\d/.test(p) || ACRONYMS.has(p.toLowerCase()))) return parts.join(" ");  // NQ_CFD
      return humanize(s).replace(/^[A-Z][a-z]/, (c) => c.toLowerCase());
    });
}
