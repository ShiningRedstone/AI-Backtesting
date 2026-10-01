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
