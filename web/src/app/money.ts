/* ADR-90: money on screen in USD or CHF. CHF is DISPLAY ONLY: every calculation, rule, fee and balance stays in USD; a
   CHF figure is the USD figure × the rate typed in Settings (1 USD = rate CHF), and says so. Inputs stay in USD. */
import { useApp } from "./context";

export interface Money {
  /** Format a USD amount in the chosen display currency. */
  fmt: (usd: number | null | undefined, digits?: number) => string;
  /** "USD" or "CHF" (CHF only when a rate is set). */
  unit: "USD" | "CHF";
  /** Short note on the conversion, or "" in USD. */
  note: string;
  /** A USD number converted for charts (unchanged in USD mode). */
  conv: (usd: number | null) => number | null;
  /** The CHF equivalent of a USD input, for the grey hint next to USD input fields ("" in USD mode). */
  hint: (usd: number | null | undefined) => string;
}

const num = (v: number, digits: number) => Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });

export function formatMoney(usd: number | null | undefined, digits: number, unit: "USD" | "CHF", rate: number | null): string {
  if (usd == null || !Number.isFinite(usd)) return "—";
  const v = unit === "CHF" && rate ? usd * rate : usd;
  const sign = v < 0 ? "−" : "";
  return unit === "CHF" && rate ? `${sign}CHF ${num(v, digits)}` : `${sign}$${num(v, digits)}`;
}

export function useMoney(): Money {
  const { prefs } = useApp();
  const rate = prefs.chf_per_usd && prefs.chf_per_usd > 0 ? prefs.chf_per_usd : null;
  const unit: "USD" | "CHF" = prefs.currency === "CHF" && rate ? "CHF" : "USD";
  return {
    unit,
    fmt: (usd, digits = 0) => formatMoney(usd, digits, unit, rate),
    conv: (usd) => (usd == null ? null : unit === "CHF" && rate ? usd * rate : usd),
    note: unit === "CHF" ? `shown in CHF at your rate 1 USD = ${rate} CHF (calculated in USD)` : "",
    hint: (usd) => (unit === "CHF" && usd != null && Number.isFinite(usd) ? `≈ ${formatMoney(usd, 2, "CHF", rate)}` : ""),
  };
}
