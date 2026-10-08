/* Charts (ADR-111): the data model shared by the chart, the drawing tools and the dialogs.
   A drawing point is stored as (t = real UTC seconds, may be fractional or beyond the data, p = price), so drawings
   stay where they were drawn on every timeframe and time zone. Screen-anchored tools store fractions of the pane. */
import type { Bar } from "../../api/charts";

export interface Pt { t: number; p: number }
export interface Level { v: number; color: string; on: boolean }
export interface Vis { m: boolean; h: boolean; d: boolean; w: boolean; mo: boolean }

/** Every style field a tool may use; each tool lists the ones its settings dialog shows. */
export interface Style {
  color: string; width: number; dash: number;            // 0 solid, 1 dashed, 2 dotted
  fill: string; fillOn: boolean;
  textColor: string; fontSize: number; bold: boolean; italic: boolean; bg: string; bgOn: boolean; border: string; borderOn: boolean;
  hAlign: "left" | "center" | "right"; vAlign: "top" | "middle" | "bottom";
  extendLeft: boolean; extendRight: boolean; leftEnd: "none" | "arrow"; rightEnd: "none" | "arrow";
  showPrice: boolean; showPercent: boolean; showTicks: boolean; showBars: boolean; showTime: boolean; showAngle: boolean; showDistance: boolean;
  showLabels: boolean; labelsLeft: boolean; showLevelPrices: boolean; reverse: boolean; middle: boolean; logScaleLevels: boolean;
  upper: number; lower: number;                          // regression / VWAP band deviations
  rows: number; valueArea: number; upColor: string; downColor: string;   // volume profile
  account: number; risk: number; riskPct: boolean; qty: number;          // position tools
  candles: "candles" | "line"; mirror: boolean; flip: boolean;           // bars pattern
  degree: number; size: number; emoji: string;
}

export interface Drawing {
  id: string; type: string; points: Pt[]; style: Style; text: string;
  levels?: Level[]; locked?: boolean; hidden?: boolean; name?: string; vis?: Vis; z: number; screen?: boolean;
}

export const BASE_STYLE: Style = {
  color: "#2962ff", width: 2, dash: 0, fill: "#2962ff33", fillOn: true,
  textColor: "#e4e4e7", fontSize: 14, bold: false, italic: false, bg: "#1e222dcc", bgOn: false, border: "#2962ff", borderOn: false,
  hAlign: "left", vAlign: "bottom", extendLeft: false, extendRight: false, leftEnd: "none", rightEnd: "none",
  showPrice: false, showPercent: false, showTicks: false, showBars: false, showTime: false, showAngle: false, showDistance: false,
  showLabels: true, labelsLeft: true, showLevelPrices: true, reverse: false, middle: false, logScaleLevels: false,
  upper: 2, lower: 2, rows: 24, valueArea: 70, upColor: "#26a69a88", downColor: "#787b8688",
  account: 50000, risk: 1, riskPct: true, qty: 1, candles: "candles", mirror: false, flip: false, degree: 3, size: 28, emoji: "🚀",
};

export const ALL_VIS: Vis = { m: true, h: true, d: true, w: true, mo: true };
export const DAY = 1440, WEEK = 7 * 1440, MONTH = 31 * 1440;
export const tfGroup = (tf: number): keyof Vis => (tf === MONTH ? "mo" : tf === WEEK ? "w" : tf >= DAY ? "d" : tf >= 60 ? "h" : "m");
export const tfSeconds = (tf: number) => (tf === MONTH ? 30.44 * 86400 : tf * 60);
export const tfLabel = (tf: number) => (tf === MONTH ? "1M" : tf === WEEK ? "1W" : tf === DAY ? "1D" : tf % 60 === 0 ? `${tf / 60}h` : `${tf}m`);
export const tfWords = (tf: number) => (tf === MONTH ? "1 month" : tf === WEEK ? "1 week" : tf === DAY ? "1 day" :
  tf % 60 === 0 ? `${tf / 60} hour${tf === 60 ? "" : "s"}` : `${tf} minute${tf === 1 ? "" : "s"}`);
export function parseTf(s: string): number | null {
  const x = s.trim();
  if (/^1?D$/i.test(x)) return DAY;
  if (/^1?W$/i.test(x)) return WEEK;
  if (/^1?M$/.test(x)) return MONTH;
  const m = /^(\d{1,4})\s*([mh]?)$/i.exec(x);
  if (!m) return null;
  const v = parseInt(m[1], 10) * (m[2].toLowerCase() === "h" ? 60 : 1);
  return v >= 1 && v < DAY ? v : null;
}

let seq = 0;
export const newId = () => `d${Date.now().toString(36)}${(seq++).toString(36)}${Math.floor(Math.random() * 1e6).toString(36)}`;

// ------------------------------------------------------------------ time zones
export const TIME_ZONES: { id: string; label: string }[] = [
  { id: "America/New_York", label: "New York" }, { id: "America/Chicago", label: "Chicago (exchange)" },
  { id: "local", label: "Local (this computer)" }, { id: "UTC", label: "UTC" },
  { id: "America/Los_Angeles", label: "Los Angeles" }, { id: "America/Denver", label: "Denver" }, { id: "America/Toronto", label: "Toronto" },
  { id: "America/Sao_Paulo", label: "São Paulo" }, { id: "Europe/London", label: "London" }, { id: "Europe/Zurich", label: "Zurich" },
  { id: "Europe/Berlin", label: "Berlin" }, { id: "Europe/Paris", label: "Paris" }, { id: "Europe/Madrid", label: "Madrid" },
  { id: "Europe/Moscow", label: "Moscow" }, { id: "Asia/Dubai", label: "Dubai" }, { id: "Asia/Kolkata", label: "Kolkata" },
  { id: "Asia/Singapore", label: "Singapore" }, { id: "Asia/Hong_Kong", label: "Hong Kong" }, { id: "Asia/Shanghai", label: "Shanghai" },
  { id: "Asia/Tokyo", label: "Tokyo" }, { id: "Australia/Sydney", label: "Sydney" }, { id: "Pacific/Auckland", label: "Auckland" },
];
export const resolveTz = (tz: string) => (tz === "local" ? Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC" : tz);

const fmtCache = new Map<string, Intl.DateTimeFormat>();
const offCache = new Map<string, number>();
/** Seconds to add to a UTC time to get the wall-clock time of `tz` (cached per zone and hour). */
export function tzOffset(tz: string, utcSec: number): number {
  const zone = resolveTz(tz);
  if (zone === "UTC") return 0;
  const hour = Math.floor(utcSec / 3600);
  const key = `${zone}|${hour}`;
  const hit = offCache.get(key);
  if (hit !== undefined) return hit;
  let f = fmtCache.get(zone);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: zone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit" });
    fmtCache.set(zone, f);
  }
  const parts: Record<string, number> = {};
  for (const x of f.formatToParts(new Date(hour * 3600 * 1000))) if (x.type !== "literal") parts[x.type] = parseInt(x.value, 10);
  const wall = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour % 24, parts.minute, parts.second) / 1000;
  const off = wall - hour * 3600;
  if (offCache.size > 20000) offCache.clear();
  offCache.set(key, off);
  return off;
}
export function tzName(tz: string, utcSec: number): string {
  const o = tzOffset(tz, utcSec) / 3600;
  return `UTC${o >= 0 ? "+" : "−"}${Math.abs(o) % 1 ? Math.abs(o).toFixed(1) : Math.abs(o)}`;
}

/** Real start (UTC seconds) of a daily / weekly / monthly bar: the 18:00 New York session open before its trading date. */
export const sessionOpen = (dateSec: number) => dateSec - 6 * 3600 - tzOffset("America/New_York", dateSec);

// ------------------------------------------------------------------ bars <-> time
/** Maps real times to fractional bar indexes and back (between bars linearly, beyond the data by the bar length). */
export class TimeMap {
  real: number[] = [];
  constructor(public bars: Bar[], public tf: number) {
    this.real = tf >= DAY ? bars.map((b) => sessionOpen(b.time)) : bars.map((b) => b.time);
  }
  get dur() { return tfSeconds(this.tf); }
  get n() { return this.real.length; }
  /** Index of the last bar starting at or before t (-1 before the first). */
  floorIdx(t: number): number {
    const r = this.real;
    let lo = 0, hi = r.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (r[m] <= t) lo = m + 1; else hi = m; }
    return lo - 1;
  }
  toLogical(t: number): number {
    const r = this.real, n = r.length;
    if (!n) return 0;
    if (t <= r[0]) return (t - r[0]) / this.dur;
    if (t >= r[n - 1]) return n - 1 + (t - r[n - 1]) / this.dur;
    const i = this.floorIdx(t);
    return i + (t - r[i]) / (r[i + 1] - r[i]);
  }
  toTime(l: number): number {
    const r = this.real, n = r.length;
    if (!n) return 0;
    if (l <= 0) return r[0] + l * this.dur;
    if (l >= n - 1) return r[n - 1] + (l - (n - 1)) * this.dur;
    const i = Math.floor(l);
    return r[i] + (l - i) * (r[i + 1] - r[i]);
  }
  /** Bars between two times (signed). */
  barsBetween(t0: number, t1: number) { return Math.round(this.toLogical(t1) - this.toLogical(t0)); }
}

// ------------------------------------------------------------------ formatting
export const fmtPrice = (v: number, dp = 2) => (Number.isFinite(v) ? v.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp }) : "–");
export const signed = (v: number, dp = 2) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${fmtPrice(Math.abs(v), dp)}`;
export function fmtSpan(sec: number): string {
  const s = Math.abs(sec);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.round((s % 3600) / 60);
  if (d >= 1) return `${d}d${h ? ` ${h}h` : ""}`;
  if (h >= 1) return `${h}h${m ? ` ${m}m` : ""}`;
  return `${m}m`;
}
const WALL = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", weekday: "short", day: "2-digit", month: "short", year: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const WALL_D = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", weekday: "short", day: "2-digit", month: "short", year: "2-digit" });
/** A real UTC time as wall-clock text in `tz`. */
export const fmtTime = (utcSec: number, tz: string, dateOnly = false) =>
  (dateOnly ? WALL_D : WALL).format((utcSec + tzOffset(tz, utcSec)) * 1000);

/** Heikin Ashi bars from real bars (same times). */
export function heikinAshi(bars: Bar[]): Bar[] {
  const out: Bar[] = [];
  let po = 0, pc = 0;
  bars.forEach((b, i) => {
    const c = (b.open + b.high + b.low + b.close) / 4;
    const o = i === 0 ? (b.open + b.close) / 2 : (po + pc) / 2;
    out.push({ time: b.time, open: o, high: Math.max(b.high, o, c), low: Math.min(b.low, o, c), close: c, volume: b.volume });
    po = o; pc = c;
  });
  return out;
}

/** Colour helpers: #rrggbb(aa) <-> (hex6, alpha 0..1). */
export function splitColor(c: string): [string, number] {
  const m = /^#([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(c.trim());
  if (!m) return ["#2962ff", 1];
  return [`#${m[1]}`, m[2] ? parseInt(m[2], 16) / 255 : 1];
}
export const joinColor = (hex: string, a: number) =>
  `${hex}${a >= 0.999 ? "" : Math.round(Math.max(0, Math.min(1, a)) * 255).toString(16).padStart(2, "0")}`;
export const withAlpha = (c: string, a: number) => joinColor(splitColor(c)[0], a);
