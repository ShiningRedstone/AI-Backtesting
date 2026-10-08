/* Charts (ADR-111): every drawing tool. A tool is data + a draw function on the shared painter: how many points it
   takes, which settings its dialog shows, its default style / levels and, where a plain point list is not enough,
   extra anchors (rectangle corners, position target / stop / end). Tools only draw: they never change prices or bars. */
import type { Drawing, Level, Pt, Style } from "./model";
import { fmtPrice, fmtSpan, signed, withAlpha } from "./model";
import type { Env, Painter, XY } from "./paint";
import { add, circle3, dist, lerp, mid, mul, norm, perp, sub } from "./paint";

export type Field = keyof Style | "levels" | "text";
export type GroupId = "lines" | "fib" | "patterns" | "forecast" | "shapes" | "text" | "icons";
export interface Anchor { xy: XY; cursor?: string; move: (d: Drawing, to: Pt, e: Env, q: XY) => void }
export interface ToolDef {
  id: string; name: string; group: GroupId; section: string; icon: string;
  n: number; clicks?: number;                       // clicks: default n; 0 = freehand; -1 = until double-click / Enter
  init?: (pts: Pt[], e: Env) => Pt[];
  fields: Field[]; defaults?: Partial<Style>; levels?: Level[]; text?: string; askText?: boolean; screen?: boolean; note?: string;
  key?: string;                                     // keyboard shortcut (Alt + key)
  draw: (P: Painter, px: XY[], d: Drawing, e: Env) => void;
  anchors?: (px: XY[], d: Drawing, e: Env) => Anchor[];
  priceAxis?: boolean; timeAxis?: boolean;          // always show the axis labels (horizontal / vertical lines)
}

// ------------------------------------------------------------------ field presets
const LINE: Field[] = ["color", "width", "dash"];
const EXT: Field[] = ["extendLeft", "extendRight"];
const ENDS: Field[] = ["leftEnd", "rightEnd"];
const STATS: Field[] = ["showPrice", "showPercent", "showTicks", "showBars", "showTime", "showAngle", "showDistance"];
const TEXT: Field[] = ["text", "textColor", "fontSize", "bold", "italic"];
const BOXF: Field[] = ["bg", "bgOn", "border", "borderOn"];
const FILL: Field[] = ["fill", "fillOn"];
const FIBF: Field[] = ["levels", "showLabels", "labelsLeft", "showLevelPrices", "reverse", "fill", "fillOn"];

const PAL = ["#787b86", "#f23645", "#ff9800", "#4caf50", "#089981", "#00bcd4", "#787b86", "#2962ff", "#f23645", "#9c27b0", "#e91e63",
  "#ff9800", "#4caf50", "#089981", "#00bcd4", "#2962ff", "#9c27b0"];
const lv = (vals: number[], off: number[] = []): Level[] =>
  [...vals.map((v) => ({ v, on: true })), ...off.map((v) => ({ v, on: false }))].sort((a, b) => a.v - b.v)
    .map((x, i) => ({ ...x, color: PAL[i % PAL.length] }));
const FIB_RET = lv([0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.618, 2.618, 3.618, 4.236], [1.272, 1.414, 2, 2.272, 2.414, 3, 3.272, 4, 4.618]);
const FIB_EXT = lv([0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.618, 2.618, 3.618, 4.236], [1.272, 1.414, 2, 2.272, 2.414, 3, 4.618]);
const FIB_TZ = lv([0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89]);
const FIB_FAN = lv([0, 0.25, 0.382, 0.5, 0.618, 0.75, 1]);
const FIB_TIME = lv([0, 0.382, 0.5, 0.618, 1, 1.382, 1.618, 2, 2.382, 2.618, 3, 3.618, 4.236]);
const FIB_CIRC = lv([0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.618, 2.618, 3.618, 4.236]);
const FIB_ARC = lv([0.236, 0.382, 0.5, 0.618, 0.786, 1]);
const PITCH = lv([0.5, 1], [0.25, 0.382, 0.618, 0.75, 1.5, 1.75, 2]);
const PFAN = lv([0, 0.25, 0.382, 0.5, 0.618, 0.75, 1]);
const GANN_BOX = lv([0, 0.25, 0.382, 0.5, 0.618, 0.75, 1]);
const GANN_FAN = lv([0.125, 0.25, 0.3333, 0.5, 1, 2, 3, 4, 8]);

// ------------------------------------------------------------------ helpers
const on = (d: Drawing) => (d.levels ?? []).filter((l) => l.on);
const fill = (s: Style) => (s.fillOn ? s.fill : null);
const deg = (a: XY, b: XY) => (Math.atan2(a.y - b.y, b.x - a.x) * 180) / Math.PI;
const lvlText = (v: number) => (Number.isInteger(v) ? String(v) : String(+v.toFixed(3)));
function ends(P: Painter, a: XY, b: XY, s: Style) {
  if (s.rightEnd === "arrow") P.arrowHead(a, b);
  if (s.leftEnd === "arrow") P.arrowHead(b, a);
}
/** The stats box of trend / info lines: price change, %, ticks, bars, time, angle, distance as switched on. */
function statLines(e: Env, s: Style, p0: Pt, p1: Pt, a: XY, b: XY, all = false): string {
  const dp = p1.p - p0.p, out: string[] = [];
  const tick = e.sym?.tick ?? 0.25;
  const parts: string[] = [];
  if (all || s.showPrice) parts.push(signed(dp, e.dp));
  if (all || s.showPercent) parts.push(`(${signed((dp / p0.p) * 100, 2)}%)`);
  if (all || s.showTicks) parts.push(`${Math.round(dp / tick).toLocaleString()} ticks`);
  if (parts.length) out.push(parts.join(" "));
  const t: string[] = [];
  if (all || s.showBars) t.push(`${e.map.barsBetween(p0.t, p1.t)} bars`);
  if (all || s.showTime) t.push(fmtSpan(p1.t - p0.t));
  if (t.length) out.push(t.join(", "));
  const g: string[] = [];
  if (all || s.showDistance) g.push(`distance ${Math.round(dist(a, b))} px`);
  if (all || s.showAngle) g.push(`${deg(a, b).toFixed(1)}°`);
  if (g.length) out.push(g.join(", "));
  return out.join("\n");
}
function statBox(P: Painter, e: Env, d: Drawing, a: XY, b: XY, all = false) {
  const txt = statLines(e, d.style, d.points[0], d.points[1], a, b, all);
  if (txt) P.pill(txt, b.x + 8, b.y + (b.y < a.y ? -4 : 4), { base: b.y < a.y ? "bottom" : "top", bg: withAlpha(d.style.color, 0.85) });
}
function caption(P: Painter, d: Drawing, a: XY, b: XY) {        // optional text along a line
  if (!d.text) return;
  const s = d.style, m = mid(a, b);
  const x = s.hAlign === "left" ? Math.min(a.x, b.x) : s.hAlign === "right" ? Math.max(a.x, b.x) : m.x;
  const y = s.hAlign === "left" ? (a.x < b.x ? a.y : b.y) : s.hAlign === "right" ? (a.x < b.x ? b.y : a.y) : m.y;
  P.text(d.text, x, y + (s.vAlign === "top" ? -6 : s.vAlign === "bottom" ? 6 : 0),
    { align: s.hAlign, base: s.vAlign === "top" ? "bottom" : s.vAlign === "bottom" ? "top" : "middle",
      bg: s.bgOn ? s.bg : undefined, border: s.borderOn ? s.border : undefined });
}
/** Price of a fib level between two prices (0 at `to`, 1 at `from`, TradingView's convention). */
const fibP = (from: number, to: number, v: number, rev: boolean) => (rev ? from + (to - from) * v : to - (to - from) * v);
function levelLabel(P: Painter, e: Env, d: Drawing, v: number, price: number, x: number, y: number, right: number, color: string) {
  const s = d.style;
  if (!s.showLabels && !s.showLevelPrices) return;
  const t = `${s.showLabels ? lvlText(v) : ""}${s.showLevelPrices ? ` (${fmtPrice(price, e.dp)})` : ""}`.trim();
  P.text(t, s.labelsLeft ? x - 4 : right + 4, y, { align: s.labelsLeft ? "right" : "left", base: "middle", color, size: 11, noHit: true });
}
/** Horizontal fib-style bands between level lines (retracement, extension, channels). */
function fibBands(P: Painter, e: Env, d: Drawing, x0: number, x1: number, from: number, to: number) {
  const s = d.style, L = on(d);
  const left = s.extendLeft ? 0 : Math.min(x0, x1), right = s.extendRight ? e.w : Math.max(x0, x1);
  const ys = L.map((l) => e.yOf(fibP(from, to, l.v, s.reverse)));
  if (s.fillOn) for (let i = 1; i < L.length; i++)
    P.fillPoly([{ x: left, y: ys[i - 1] }, { x: right, y: ys[i - 1] }, { x: right, y: ys[i] }, { x: left, y: ys[i] }], withAlpha(L[i].color, 0.12));
  L.forEach((l, i) => {
    P.line({ x: left, y: ys[i] }, { x: right, y: ys[i] }, { color: l.color, width: 1 });
    levelLabel(P, e, d, l.v, fibP(from, to, l.v, s.reverse), left, ys[i], right, l.color);
  });
}
/** Rectangle corner anchors: the two stored corners plus the two others (each sets one time and one price). */
const rectAnchors = (px: XY[]): Anchor[] => [
  { xy: px[0], cursor: "nwse-resize", move: (d, to) => { d.points[0] = to; } },
  { xy: px[1], cursor: "nwse-resize", move: (d, to) => { d.points[1] = to; } },
  { xy: { x: px[0].x, y: px[1].y }, cursor: "nesw-resize", move: (d, to) => { d.points[0].t = to.t; d.points[1].p = to.p; } },
  { xy: { x: px[1].x, y: px[0].y }, cursor: "nesw-resize", move: (d, to) => { d.points[1].t = to.t; d.points[0].p = to.p; } },
  { xy: { x: (px[0].x + px[1].x) / 2, y: px[0].y }, cursor: "ns-resize", move: (d, to) => { d.points[0].p = to.p; } },
  { xy: { x: (px[0].x + px[1].x) / 2, y: px[1].y }, cursor: "ns-resize", move: (d, to) => { d.points[1].p = to.p; } },
  { xy: { x: px[0].x, y: (px[0].y + px[1].y) / 2 }, cursor: "ew-resize", move: (d, to) => { d.points[0].t = to.t; } },
  { xy: { x: px[1].x, y: (px[0].y + px[1].y) / 2 }, cursor: "ew-resize", move: (d, to) => { d.points[1].t = to.t; } },
];
const sample = (f: (t: number) => XY, n = 48) => Array.from({ length: n + 1 }, (_, i) => f(i / n));
const quad = (a: XY, c: XY, b: XY) => (t: number) => ({ x: (1 - t) ** 2 * a.x + 2 * (1 - t) * t * c.x + t * t * b.x, y: (1 - t) ** 2 * a.y + 2 * (1 - t) * t * c.y + t * t * b.y });
const cubic = (a: XY, c1: XY, c2: XY, b: XY) => (t: number) => {
  const u = 1 - t;
  return { x: u ** 3 * a.x + 3 * u * u * t * c1.x + 3 * u * t * t * c2.x + t ** 3 * b.x, y: u ** 3 * a.y + 3 * u * u * t * c1.y + 3 * u * t * t * c2.y + t ** 3 * b.y };
};
/** Bars (indexes) whose start lies in [t0, t1]. */
function barRange(e: Env, t0: number, t1: number): [number, number] {
  const a = Math.min(t0, t1), b = Math.max(t0, t1), r = e.map.real;
  let i0 = e.map.floorIdx(a);
  if (i0 < 0 || r[i0] < a) i0 += 1;
  return [Math.max(0, i0), Math.min(r.length - 1, e.map.floorIdx(b))];
}
const ratio = (a: number, b: number) => (Math.abs(b) < 1e-12 ? "–" : Math.abs(a / b).toFixed(3));

// ------------------------------------------------------------------ pattern helpers
function labelAt(P: Painter, px: XY[], i: number, text: string, s: Style) {
  const p = px[i], prev = px[i - 1] ?? px[i + 1], next = px[i + 1] ?? px[i - 1];
  const high = p.y <= Math.min(prev?.y ?? p.y, next?.y ?? p.y);
  P.text(text, p.x, p.y + (high ? -6 : 6), { align: "center", base: high ? "bottom" : "top", color: s.textColor, size: s.fontSize, bold: true, noHit: true });
}
function ratioLabel(P: Painter, a: XY, b: XY, text: string, s: Style) {
  P.line(a, b, { dash: 1, width: 1, noHit: true });
  const m = mid(a, b);
  P.pill(text, m.x, m.y, { align: "center", base: "middle", bg: withAlpha(s.color, 0.85) });
}
function harmonic(P: Painter, px: XY[], d: Drawing, names: string[]) {
  const s = d.style, f = fill(s), p = d.points;
  if (px.length >= 3 && f) P.fillPoly([px[0], px[1], px[2]], f);
  if (px.length >= 5 && f) P.fillPoly([px[2], px[3], px[4]], f);
  P.path(px);
  const R = (i: number, j: number, k: number, l: number) => ratio(p[i].p - p[j].p, p[k].p - p[l].p);
  if (px.length >= 3) ratioLabel(P, px[0], px[2], R(2, 1, 1, 0), s);
  if (px.length >= 4) ratioLabel(P, px[1], px[3], R(3, 2, 2, 1), s);
  if (px.length >= 5) { ratioLabel(P, px[2], px[4], R(4, 3, 3, 2), s); ratioLabel(P, px[0], px[4], R(4, 1, 1, 0), s); }
  px.forEach((_, i) => labelAt(P, px, i, names[i] ?? "", s));
}
const WAVES: Record<string, string[]> = {
  impulse: ["0", "1", "2", "3", "4", "5"], correction: ["0", "A", "B", "C"], triangle: ["0", "A", "B", "C", "D", "E"],
  double: ["0", "W", "X", "Y"], triple: ["0", "W", "X", "Y", "X", "Z"],
};
const CIRCLED: Record<string, string> = { "1": "①", "2": "②", "3": "③", "4": "④", "5": "⑤", A: "Ⓐ", B: "Ⓑ", C: "Ⓒ", D: "Ⓓ", E: "Ⓔ", W: "Ⓦ", X: "Ⓧ", Y: "Ⓨ", Z: "Ⓩ" };
const ROMAN: Record<string, string> = { "1": "i", "2": "ii", "3": "iii", "4": "iv", "5": "v", A: "a", B: "b", C: "c", D: "d", E: "e", W: "w", X: "x", Y: "y", Z: "z" };
/** Wave degree styles: 0 circled, 1 [1], 2 (1), 3 1, 4 i. */
const waveName = (n: string, deg: number) => (n === "0" ? "" : deg === 0 ? CIRCLED[n] ?? n : deg === 1 ? `[${n}]` : deg === 2 ? `(${n})` : deg === 4 ? ROMAN[n] ?? n : n);
function elliott(kind: string) {
  return (P: Painter, px: XY[], d: Drawing) => {
    P.path(px);
    px.forEach((_, i) => labelAt(P, px, i, waveName(WAVES[kind][i] ?? "", d.style.degree), d.style));
  };
}

// ------------------------------------------------------------------ position tools
function position(long: boolean) {
  const init = (pts: Pt[], e: Env): Pt[] => {
    const p = pts[0], y = e.yOf(p.p), t1 = e.map.toTime(e.map.toLogical(p.t) + 20);
    return [p, { t: t1, p: e.pOf(long ? y - 80 : y + 80) }, { t: t1, p: e.pOf(long ? y + 40 : y - 40) }];
  };
  const draw = (P: Painter, px: XY[], d: Drawing, e: Env) => {
    const s = d.style, [E, T, S] = d.points, x0 = px[0].x, x1 = px[1].x, yE = px[0].y, yT = px[1].y, yS = px[2].y;
    const pv = e.sym?.point_value ?? 1, tick = e.sym?.tick ?? 0.25, dp = e.dp;
    const riskPts = Math.abs(E.p - S.p), rewardPts = Math.abs(T.p - E.p);
    const riskCash = s.riskPct ? (s.account * s.risk) / 100 : s.risk;
    const qty = riskPts > 0 && riskCash > 0 ? Math.max(0, Math.floor(riskCash / (riskPts * pv))) : s.qty;
    P.rect({ x: x0, y: yE }, { x: x1, y: yT }, { fill: withAlpha("#089981", 0.22), color: withAlpha("#089981", 0.6), width: 1 });
    P.rect({ x: x0, y: yE }, { x: x1, y: yS }, { fill: withAlpha("#f23645", 0.22), color: withAlpha("#f23645", 0.6), width: 1 });
    P.line({ x: x0, y: yE }, { x: x1, y: yE }, { color: e.theme.muted, width: 1 });
    // what happened after the entry (first touch of target or stop within the box; both in one bar = stop first)
    const [i0, i1] = barRange(e, E.t, T.t);
    let exit: { i: number; p: number; why: string } | null = null, entered = -1;
    for (let i = i0; i <= i1 && i < e.bars.length; i++) {
      const b = e.bars[i];
      if (entered < 0) { if (b.low <= E.p && b.high >= E.p) entered = i; else continue; }
      const hitS = long ? b.low <= S.p : b.high >= S.p, hitT = long ? b.high >= T.p : b.low <= T.p;
      if (hitS) { exit = { i, p: S.p, why: "stop" }; break; }
      if (hitT) { exit = { i, p: T.p, why: "target" }; break; }
    }
    const lastI = Math.min(i1, e.bars.length - 1);
    const outP = exit ? exit.p : entered >= 0 && lastI >= 0 ? e.bars[lastI].close : null;
    if (entered >= 0 && outP != null) {
      const xa = e.xOfL(entered), xb = e.xOfL(exit ? exit.i : lastI);
      P.line({ x: xa, y: yE }, { x: xb, y: e.yOf(outP) }, { color: e.theme.text, width: 1, dash: 1, noHit: true });
      const pl = (long ? outP - E.p : E.p - outP) * pv * qty;
      P.rect({ x: xa, y: yE }, { x: xb, y: e.yOf(outP) }, { fill: withAlpha(pl >= 0 ? "#089981" : "#f23645", 0.25), stroke: false, noHit: true });
    }
    const cx = (x0 + x1) / 2, up = (y: number, other: number) => y < other;
    const tTxt = `Target: ${fmtPrice(T.p, dp)} (${signed(((T.p - E.p) / E.p) * 100)}%) ${Math.round(rewardPts / tick)} ticks, $${fmtPrice(rewardPts * pv * qty, 0)}`;
    const sTxt = `Stop: ${fmtPrice(S.p, dp)} (${signed(((S.p - E.p) / E.p) * 100)}%) ${Math.round(riskPts / tick)} ticks, $${fmtPrice(riskPts * pv * qty, 0)}`;
    P.pill(tTxt, cx, yT + (up(yT, yE) ? -4 : 4), { align: "center", base: up(yT, yE) ? "bottom" : "top", bg: "#089981e6" });
    P.pill(sTxt, cx, yS + (up(yS, yE) ? -4 : 4), { align: "center", base: up(yS, yE) ? "bottom" : "top", bg: "#f23645e6" });
    const rr = riskPts > 0 ? (rewardPts / riskPts).toFixed(2) : "–";
    const res = outP == null ? "not entered" : exit ? `Closed (${exit.why}): $${signed((long ? outP - E.p : E.p - outP) * pv * qty, 0)}` :
      `Open: $${signed((long ? outP - E.p : E.p - outP) * pv * qty, 0)}`;
    P.pill(`${res}, ${qty} contract${qty === 1 ? "" : "s"}${e.sym ? ` ${e.sym.symbol}` : ""}\nRisk/reward ${rr}`, cx, yE, {
      align: "center", base: "middle", bg: withAlpha(e.theme.bg, 0.92), color: e.theme.text, border: e.theme.muted });
  };
  const anchors = (px: XY[]): Anchor[] => [
    { xy: px[0], cursor: "move", move: (d, to) => {
      const dp = to.p - d.points[0].p, dt = to.t - d.points[0].t;
      d.points = d.points.map((q, i) => (i === 0 ? to : { t: q.t + dt, p: q.p + dp }));
    } },
    { xy: { x: px[0].x, y: px[1].y }, cursor: "ns-resize", move: (d, to) => { d.points[1].p = to.p; } },
    { xy: { x: px[0].x, y: px[2].y }, cursor: "ns-resize", move: (d, to) => { d.points[2].p = to.p; } },
    { xy: { x: px[1].x, y: px[0].y }, cursor: "ew-resize", move: (d, to) => { d.points[1].t = to.t; d.points[2].t = to.t; } },
  ];
  return { init, draw, anchors };
}

// ------------------------------------------------------------------ volume tools (Dukascopy tick volume)
const VOL_NOTE = "Uses Dukascopy tick volume (how often the price changed), not exchange contract volume.";
function profile(P: Painter, e: Env, d: Drawing, i0: number, i1: number, xL: number, xR: number) {
  const s = d.style, bars = e.bars.slice(i0, i1 + 1);
  if (!bars.length) return;
  const lo = Math.min(...bars.map((b) => b.low)), hi = Math.max(...bars.map((b) => b.high));
  const rows = Math.max(4, Math.min(200, Math.round(s.rows))), step = (hi - lo) / rows || 1;
  const upV = new Array(rows).fill(0), dnV = new Array(rows).fill(0);
  for (const b of bars) {
    const a = Math.max(0, Math.floor((b.low - lo) / step)), z = Math.min(rows - 1, Math.floor((b.high - lo) / step));
    const per = b.volume / (z - a + 1);
    for (let r = a; r <= z; r++) (b.close >= b.open ? upV : dnV)[r] += per;
  }
  const tot = upV.map((u, i) => u + dnV[i]), max = Math.max(...tot);
  if (!(max > 0)) { P.text("No volume in this range", xL, e.yOf(hi), { base: "bottom", size: 11, noHit: true }); return; }
  const poc = tot.indexOf(max), want = (tot.reduce((a, b) => a + b, 0) * s.valueArea) / 100;
  let vaLo = poc, vaHi = poc, acc = tot[poc];
  while (acc < want && (vaLo > 0 || vaHi < rows - 1)) {
    const dn = vaLo > 0 ? tot[vaLo - 1] : -1, upx = vaHi < rows - 1 ? tot[vaHi + 1] : -1;
    if (upx >= dn) { vaHi++; acc += upx; } else { vaLo--; acc += dn; }
  }
  const W = Math.max(20, (xR - xL) * 0.35);
  for (let r = 0; r < rows; r++) {
    const yA = e.yOf(lo + r * step), yB = e.yOf(lo + (r + 1) * step), inVA = r >= vaLo && r <= vaHi, a = inVA ? 1 : 0.45;
    const wu = (upV[r] / max) * W, wd = (dnV[r] / max) * W;
    P.fillPoly([{ x: xL, y: yB + 0.5 }, { x: xL + wu, y: yB + 0.5 }, { x: xL + wu, y: yA - 0.5 }, { x: xL, y: yA - 0.5 }], withAlpha(s.upColor, a * 0.8));
    P.fillPoly([{ x: xL + wu, y: yB + 0.5 }, { x: xL + wu + wd, y: yB + 0.5 }, { x: xL + wu + wd, y: yA - 0.5 }, { x: xL + wu, y: yA - 0.5 }], withAlpha(s.downColor, a * 0.8));
  }
  const yPoc = e.yOf(lo + (poc + 0.5) * step);
  P.line({ x: xL, y: yPoc }, { x: xR, y: yPoc }, { color: s.color, width: 1.5 });
  P.text(`POC ${fmtPrice(lo + (poc + 0.5) * step, e.dp)}`, xR, yPoc, { align: "right", base: "bottom", size: 11, color: s.color, noHit: true });
  P.rect({ x: xL, y: e.yOf(lo + vaHi * step + step) }, { x: xR, y: e.yOf(lo + vaLo * step) }, { color: withAlpha(s.color, 0.4), width: 1, dash: 1, fill: null });
}

// ------------------------------------------------------------------ the catalogue
const T = (x: ToolDef) => x;
export const TOOLS: ToolDef[] = [
  // ---- lines
  T({ id: "trend_line", name: "Trend line", group: "lines", section: "Lines", icon: "M3 15L15 3", n: 2, key: "t",
    fields: [...LINE, ...EXT, ...ENDS, ...STATS, ...TEXT, ...BOXF, "hAlign", "vAlign"],
    draw: (P, px, d, e) => { const s = d.style; P.xline(px[0], px[1], s.extendLeft, s.extendRight); ends(P, px[0], px[1], s); caption(P, d, px[0], px[1]); statBox(P, e, d, px[0], px[1]); } }),
  T({ id: "ray", name: "Ray", group: "lines", section: "Lines", icon: "M3 15L15 3M13 3h2v2", n: 2, defaults: { extendRight: true },
    fields: [...LINE, ...EXT, ...ENDS, ...STATS, ...TEXT],
    draw: (P, px, d, e) => { const s = d.style; P.xline(px[0], px[1], s.extendLeft, s.extendRight); ends(P, px[0], px[1], s); caption(P, d, px[0], px[1]); statBox(P, e, d, px[0], px[1]); } }),
  T({ id: "info_line", name: "Info line", group: "lines", section: "Lines", icon: "M3 15L15 3M9 13h6v3H9z", n: 2,
    defaults: { showPrice: true, showPercent: true, showTicks: true, showBars: true, showTime: true, showAngle: true, showDistance: true },
    fields: [...LINE, ...EXT, ...ENDS, ...STATS, ...TEXT],
    draw: (P, px, d, e) => { const s = d.style; P.xline(px[0], px[1], s.extendLeft, s.extendRight); ends(P, px[0], px[1], s); caption(P, d, px[0], px[1]); statBox(P, e, d, px[0], px[1]); } }),
  T({ id: "extended_line", name: "Extended line", group: "lines", section: "Lines", icon: "M1 17L17 1", n: 2, defaults: { extendLeft: true, extendRight: true },
    fields: [...LINE, ...EXT, ...STATS, ...TEXT],
    draw: (P, px, d, e) => { const s = d.style; P.xline(px[0], px[1], s.extendLeft, s.extendRight); caption(P, d, px[0], px[1]); statBox(P, e, d, px[0], px[1]); } }),
  T({ id: "trend_angle", name: "Trend angle", group: "lines", section: "Lines", icon: "M3 15L15 5M3 15h12M10 15a6 6 0 0 0-1-4", n: 2,
    fields: [...LINE, ...EXT, ...TEXT],
    draw: (P, px, d) => {
      const s = d.style, a = px[0], b = px[1];
      P.xline(a, b, s.extendLeft, s.extendRight);
      const r = Math.min(60, dist(a, b) * 0.6), ang = -Math.atan2(a.y - b.y, b.x - a.x);
      P.line(a, { x: a.x + r + 14, y: a.y }, { width: 1, dash: 1, noHit: true });
      P.ellipse(a.x, a.y, r, r, 0, { width: 1, a0: Math.min(0, ang), a1: Math.max(0, ang), noHit: true });
      P.pill(`${deg(a, b).toFixed(2)}°`, a.x + r + 18, a.y, { base: "middle" });
      caption(P, d, a, b);
    } }),
  T({ id: "horizontal_line", name: "Horizontal line", group: "lines", section: "Lines", icon: "M1 9h16", n: 1, key: "h", priceAxis: true,
    fields: [...LINE, "showPrice", ...TEXT, "hAlign", "vAlign"], defaults: { showPrice: true },
    draw: (P, px, d, e) => { P.line({ x: 0, y: px[0].y }, { x: e.w, y: px[0].y }); caption(P, d, { x: 0, y: px[0].y }, { x: e.w - 70, y: px[0].y }); } }),
  T({ id: "horizontal_ray", name: "Horizontal ray", group: "lines", section: "Lines", icon: "M3 9h14M3 7v4", n: 1, key: "j", priceAxis: true,
    fields: [...LINE, "showPrice", ...TEXT, "hAlign", "vAlign"], defaults: { showPrice: true },
    draw: (P, px, d, e) => { P.line(px[0], { x: e.w, y: px[0].y }); caption(P, d, px[0], { x: e.w - 70, y: px[0].y }); } }),
  T({ id: "vertical_line", name: "Vertical line", group: "lines", section: "Lines", icon: "M9 1v16", n: 1, key: "v", timeAxis: true,
    fields: [...LINE, ...TEXT],
    draw: (P, px, d, e) => { P.line({ x: px[0].x, y: 0 }, { x: px[0].x, y: e.h }); if (d.text) P.text(d.text, px[0].x + 4, 8, { base: "top" }); } }),
  T({ id: "cross_line", name: "Cross line", group: "lines", section: "Lines", icon: "M9 1v16M1 9h16", n: 1, key: "c", priceAxis: true, timeAxis: true,
    fields: [...LINE],
    draw: (P, px, _d, e) => { P.line({ x: px[0].x, y: 0 }, { x: px[0].x, y: e.h }); P.line({ x: 0, y: px[0].y }, { x: e.w, y: px[0].y }); } }),
  // ---- channels
  T({ id: "parallel_channel", name: "Parallel channel", group: "lines", section: "Channels", icon: "M2 12L12 2M6 16L16 6", n: 3,
    init: (p, e) => p.length >= 3 ? p : [p[0], p[1] ?? p[0], { t: p[1]?.t ?? p[0].t, p: e.pOf(e.yOf((p[1] ?? p[0]).p) + 40) }],
    fields: [...LINE, ...FILL, "middle", ...EXT, ...TEXT],
    defaults: { middle: true },
    draw: (P, px, d) => {
      const s = d.style, off = sub(px[2], px[1]), a2 = add(px[0], off), b2 = add(px[1], off);
      const A = P.xline(px[0], px[1], s.extendLeft, s.extendRight), B = P.xline(a2, b2, s.extendLeft, s.extendRight);
      if (A && B && s.fillOn) P.fillPoly([A[0], A[1], B[1], B[0]], s.fill, true);
      if (s.middle) P.xline(mid(px[0], a2), mid(px[1], b2), s.extendLeft, s.extendRight, { dash: 1, width: 1 });
      caption(P, d, px[0], px[1]);
    },
    anchors: (px) => {
      const off = sub(px[2], px[1]);
      return [
        { xy: px[0], move: (d, to) => { d.points[0] = to; } },
        { xy: px[1], move: (d, to, e) => { const y2 = e.yOf(d.points[2].p) - e.yOf(d.points[1].p); d.points[1] = to; d.points[2] = { t: to.t, p: e.pOf(e.yOf(to.p) + y2) }; } },
        { xy: add(px[0], off), cursor: "ns-resize", move: (d, to, e) => { const dy = e.yOf(to.p) - e.yOf(d.points[0].p) - (e.yOf(d.points[2].p) - e.yOf(d.points[1].p)); d.points[2] = { t: d.points[1].t, p: e.pOf(e.yOf(d.points[2].p) + dy) }; } },
        { xy: px[2], cursor: "ns-resize", move: (d, to) => { d.points[2] = { t: d.points[1].t, p: to.p }; } },
      ];
    } }),
  T({ id: "regression_trend", name: "Regression trend", group: "lines", section: "Channels", icon: "M2 13L16 5M2 9L16 1M2 17L16 9", n: 2,
    fields: [...LINE, ...FILL, "upper", "lower", "extendRight", "showLabels"], defaults: { fill: "#2962ff1f" },
    draw: (P, px, d, e) => {
      const s = d.style, [i0, i1] = barRange(e, d.points[0].t, d.points[1].t);
      if (i1 - i0 < 1) { P.line(px[0], px[1], { dash: 1 }); return; }
      let n = 0, sx = 0, sy = 0, sxx = 0, sxy = 0, syy = 0;
      for (let i = i0; i <= i1; i++) { const y = e.bars[i].close; n++; sx += i; sy += y; sxx += i * i; sxy += i * y; syy += y * y; }
      const k = (n * sxy - sx * sy) / (n * sxx - sx * sx || 1), c = (sy - k * sx) / n;
      let sd = 0;
      for (let i = i0; i <= i1; i++) sd += (e.bars[i].close - (k * i + c)) ** 2;
      sd = Math.sqrt(sd / Math.max(1, n - 1));
      const r = (n * sxy - sx * sy) / Math.sqrt(Math.max(1e-12, (n * sxx - sx * sx) * (n * syy - sy * sy)));
      const i2 = s.extendRight ? i1 + (e.lOfX(e.w) - i1) : i1;
      const at = (i: number, off: number) => ({ x: e.xOfL(i), y: e.yOf(k * i + c + off) });
      const U0 = at(i0, s.upper * sd), U1 = at(i2, s.upper * sd), L0 = at(i0, -s.lower * sd), L1 = at(i2, -s.lower * sd);
      if (s.fillOn) P.fillPoly([U0, U1, L1, L0], s.fill, true);
      P.line(at(i0, 0), at(i2, 0), { dash: 1 });
      P.line(U0, U1); P.line(L0, L1);
      P.line({ x: U0.x, y: U0.y }, { x: L0.x, y: L0.y }, { width: 1, dash: 1, noHit: true });
      if (s.showLabels) P.pill(`Pearson's R ${r.toFixed(3)}, ${n} bars, ±${s.upper}/${s.lower} sd`, L0.x, L0.y + 6, { base: "top" });
    },
    anchors: (px, d, e) => {
      const yAt = (t: number) => { const l = e.map.toLogical(t); return e.yOf(e.bars[Math.max(0, Math.min(e.bars.length - 1, Math.round(l)))]?.close ?? d.points[0].p); };
      return [0, 1].map((i) => ({ xy: { x: px[i].x, y: yAt(d.points[i].t) }, cursor: "ew-resize", move: (dd: Drawing, to: Pt) => { dd.points[i] = to; } }));
    } }),
  T({ id: "flat_top_bottom", name: "Flat top / bottom", group: "lines", section: "Channels", icon: "M2 4h14M2 15L16 9", n: 3,
    init: (p, e) => p.length >= 3 ? p : [p[0], p[1] ?? p[0], { t: (p[1] ?? p[0]).t, p: e.pOf(e.yOf(p[0].p) - 40) }],
    fields: [...LINE, ...FILL, ...EXT],
    draw: (P, px, d) => {
      const s = d.style, a2 = { x: px[0].x, y: px[2].y }, b2 = { x: px[1].x, y: px[2].y };
      const A = P.xline(px[0], px[1], s.extendLeft, s.extendRight), B = P.xline(a2, b2, s.extendLeft, s.extendRight);
      if (A && B && s.fillOn) P.fillPoly([A[0], A[1], B[1], B[0]], s.fill, true);
    } }),
  T({ id: "disjoint_channel", name: "Disjoint channel", group: "lines", section: "Channels", icon: "M2 4L16 8M2 15L16 11", n: 3,
    init: (p, e) => p.length >= 3 ? p : [p[0], p[1] ?? p[0], { t: (p[1] ?? p[0]).t, p: e.pOf(e.yOf(p[0].p) + 60) }],
    fields: [...LINE, ...FILL, ...EXT],
    draw: (P, px, d) => {
      const s = d.style, dy = px[1].y - px[0].y, a2 = { x: px[0].x, y: px[2].y }, b2 = { x: px[1].x, y: px[2].y - dy };
      const A = P.xline(px[0], px[1], s.extendLeft, s.extendRight), B = P.xline(a2, b2, s.extendLeft, s.extendRight);
      if (A && B && s.fillOn) P.fillPoly([A[0], A[1], B[1], B[0]], s.fill, true);
    } }),
  // ---- pitchforks
  ...([["pitchfork", "Pitchfork", 0], ["schiff_pitchfork", "Schiff pitchfork", 1], ["modified_schiff", "Modified Schiff pitchfork", 2],
    ["inside_pitchfork", "Inside pitchfork", 3]] as const).map(([id, name, kind]) => T({
    id, name, group: "lines", section: "Pitchforks", icon: "M2 9L16 9M2 9L16 3M2 9L16 15", n: 3, levels: PITCH,
    fields: [...LINE, ...FILL, "levels", "extendRight"], defaults: { extendRight: true, fill: "#2962ff14" },
    draw: (P, px, d) => {
      const [a, b, c] = px, m = mid(b, c), s = d.style;
      const o = kind === 1 ? { x: a.x, y: (a.y + b.y) / 2 } : kind === 2 ? mid(a, b) : kind === 3 ? mid(a, m) : a;
      const dir = sub(m, o);
      P.xline(o, m, false, s.extendRight);
      P.line(a, o, { width: 1, dash: kind ? 1 : 0, noHit: !kind });
      P.line(b, c, { width: 1 });
      const L = on(d);
      const lines = L.flatMap((l) => [-1, 1].map((sg) => { const q = lerp(m, sg < 0 ? b : c, l.v); return { q, l, sg }; }));
      const segs = lines.map(({ q, l }) => ({ seg: P.xline(q, add(q, dir), false, s.extendRight, { color: l.color, width: 1 }), l }));
      if (s.fillOn) {
        const byV = (sg: number) => lines.map((x, i) => ({ ...x, seg: segs[i].seg })).filter((x) => x.sg === sg && x.seg);
        for (const sg of [-1, 1]) {
          const arr = byV(sg);
          let prev: [XY, XY] | null = P.xline(m, add(m, dir), false, s.extendRight, { width: 0, noHit: true });
          for (const x of arr) { if (prev && x.seg) P.fillPoly([prev[0], prev[1], x.seg[1], x.seg[0]], withAlpha(x.l.color, 0.1)); prev = x.seg; }
        }
      }
    } })),
  // ---- fibonacci
  T({ id: "fib_retracement", name: "Fib retracement", group: "fib", section: "Fibonacci", icon: "M2 3h14M2 7h14M2 11h14M2 15h14M3 15L15 3", n: 2, key: "f",
    levels: FIB_RET, fields: [...FIBF, "extendLeft", "extendRight", "color", "width", "dash"], defaults: { color: "#787b86", width: 1, dash: 1 },
    draw: (P, px, d, e) => { P.line(px[0], px[1]); fibBands(P, e, d, px[0].x, px[1].x, d.points[0].p, d.points[1].p); } }),
  T({ id: "fib_extension", name: "Trend-based fib extension", group: "fib", section: "Fibonacci", icon: "M2 15L8 3L12 10M10 5h7M10 9h7M10 13h7", n: 3,
    levels: FIB_EXT, fields: [...FIBF, "extendLeft", "extendRight", "color", "width", "dash"], defaults: { color: "#787b86", width: 1, dash: 1 },
    draw: (P, px, d, e) => {
      P.path(px, { fill: null });
      const [a, b, c] = d.points, s = d.style, L = on(d);
      const left = s.extendLeft ? 0 : Math.min(px[2].x, px[1].x), right = s.extendRight ? e.w : Math.max(px[2].x + 80, px[1].x + 80);
      const price = (v: number) => (s.reverse ? c.p - (b.p - a.p) * v : c.p + (b.p - a.p) * v);
      const ys = L.map((l) => e.yOf(price(l.v)));
      if (s.fillOn) for (let i = 1; i < L.length; i++)
        P.fillPoly([{ x: left, y: ys[i - 1] }, { x: right, y: ys[i - 1] }, { x: right, y: ys[i] }, { x: left, y: ys[i] }], withAlpha(L[i].color, 0.12));
      L.forEach((l, i) => { P.line({ x: left, y: ys[i] }, { x: right, y: ys[i] }, { color: l.color, width: 1 }); levelLabel(P, e, d, l.v, price(l.v), left, ys[i], right, l.color); });
    } }),
  T({ id: "fib_channel", name: "Fib channel", group: "fib", section: "Fibonacci", icon: "M2 10L12 2M4 14L14 6M6 18L16 10", n: 3, levels: FIB_RET,
    init: (p, e) => p.length >= 3 ? p : [p[0], p[1] ?? p[0], { t: (p[1] ?? p[0]).t, p: e.pOf(e.yOf((p[1] ?? p[0]).p) + 40) }],
    fields: ["levels", "showLabels", "fill", "fillOn", "extendLeft", "extendRight", "color", "width"], defaults: { extendRight: true },
    draw: (P, px, d, e) => {
      const s = d.style, off = sub(px[2], px[1]), L = on(d);
      let prev: [XY, XY] | null = null;
      L.forEach((l) => {
        const o = mul(off, l.v), seg = P.xline(add(px[0], o), add(px[1], o), s.extendLeft, s.extendRight, { color: l.color, width: 1 });
        if (s.fillOn && prev && seg) P.fillPoly([prev[0], prev[1], seg[1], seg[0]], withAlpha(l.color, 0.1));
        if (s.showLabels && seg) P.text(lvlText(l.v), seg[0].x + 4, seg[0].y, { base: "bottom", size: 11, color: l.color, noHit: true });
        prev = seg;
      });
      void e;
    } }),
  T({ id: "fib_time_zone", name: "Fib time zone", group: "fib", section: "Fibonacci", icon: "M2 1v16M5 1v16M8 1v16M13 1v16", n: 2, levels: FIB_TZ,
    fields: ["levels", "showLabels", "width", "dash"], defaults: { width: 1 },
    draw: (P, px, d, e) => {
      const l0 = e.map.toLogical(d.points[0].t), dl = e.map.toLogical(d.points[1].t) - l0;
      P.line(px[0], px[1], { dash: 1, width: 1 });
      on(d).forEach((l) => {
        const x = e.xOfL(l0 + dl * l.v);
        if (x < -10 || x > e.w + 10) return;
        P.line({ x, y: 0 }, { x, y: e.h }, { color: l.color });
        if (d.style.showLabels) P.text(lvlText(l.v), x + 3, e.h - 4, { color: l.color, size: 11, noHit: true });
      });
    } }),
  T({ id: "fib_speed_fan", name: "Fib speed resistance fan", group: "fib", section: "Fibonacci", icon: "M2 16L16 2M2 16L16 8M2 16L10 2M2 16L16 13", n: 2,
    levels: FIB_FAN, fields: ["levels", "showLabels", "fill", "fillOn", "width"], defaults: { width: 1, fill: "#2962ff14" },
    draw: (P, px, d) => {
      const [a, b] = px, L = on(d), s = d.style;
      P.rect(a, b, { width: 1, color: withAlpha(s.color, 0.4), fill: null, noHit: true });
      let prev: XY | null = null;
      L.forEach((l) => {
        const q = { x: b.x, y: b.y + (a.y - b.y) * l.v }, r = { x: a.x + (b.x - a.x) * l.v, y: b.y };
        const seg = P.xline(a, q, false, true, { color: l.color, width: 1 });
        P.xline(a, r, false, true, { color: l.color, width: 1 });
        P.line({ x: a.x, y: q.y }, { x: b.x, y: q.y }, { color: withAlpha(l.color, 0.4), width: 1, noHit: true });
        P.line({ x: r.x, y: a.y }, { x: r.x, y: b.y }, { color: withAlpha(l.color, 0.4), width: 1, noHit: true });
        if (s.fillOn && prev && seg) P.fillPoly([a, prev, seg[1]], withAlpha(l.color, 0.1));
        if (seg) prev = seg[1];
        if (s.showLabels) P.text(lvlText(l.v), b.x + 3, q.y, { base: "middle", size: 11, color: l.color, noHit: true });
      });
    } }),
  T({ id: "fib_time_trend", name: "Trend-based fib time", group: "fib", section: "Fibonacci", icon: "M2 15L7 3L11 12M12 1v16M15 1v16", n: 3,
    levels: FIB_TIME, fields: ["levels", "showLabels", "width", "dash"], defaults: { width: 1 },
    draw: (P, px, d, e) => {
      P.path(px, { dash: 1, width: 1 });
      const l0 = e.map.toLogical(d.points[0].t), l1 = e.map.toLogical(d.points[1].t), l2 = e.map.toLogical(d.points[2].t);
      on(d).forEach((l) => {
        const x = e.xOfL(l2 + (l1 - l0) * l.v);
        P.line({ x, y: 0 }, { x, y: e.h }, { color: l.color });
        if (d.style.showLabels) P.text(lvlText(l.v), x + 3, e.h - 4, { color: l.color, size: 11, noHit: true });
      });
    } }),
  T({ id: "fib_circles", name: "Fib circles", group: "fib", section: "Fibonacci", icon: "M9 9m-7 0a7 7 0 1 0 14 0a7 7 0 1 0-14 0M9 9m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0", n: 2,
    levels: FIB_CIRC, fields: ["levels", "showLabels", "fill", "fillOn", "width"], defaults: { width: 1, fillOn: false },
    draw: (P, px, d) => {
      const c = mid(px[0], px[1]), R = dist(px[0], px[1]) / 2;
      P.line(px[0], px[1], { dash: 1, width: 1 });
      on(d).forEach((l) => {
        P.ellipse(c.x, c.y, R * l.v, R * l.v, 0, { color: l.color, fill: d.style.fillOn ? withAlpha(l.color, 0.05) : null });
        if (d.style.showLabels) P.text(lvlText(l.v), c.x + R * l.v + 3, c.y, { base: "middle", size: 11, color: l.color, noHit: true });
      });
    } }),
  T({ id: "fib_spiral", name: "Fib spiral", group: "fib", section: "Fibonacci", icon: "M9 9a2 2 0 1 1 2 2a4 4 0 1 1-4-4a7 7 0 1 1 7 7", n: 2,
    fields: ["color", "width", "dash"], defaults: { width: 1.5 },
    draw: (P, px) => {
      const c = px[0], r0 = Math.max(1, dist(px[0], px[1])), a0 = Math.atan2(px[1].y - c.y, px[1].x - c.x), phi = (1 + Math.sqrt(5)) / 2;
      const pts: XY[] = [];
      for (let th = -10 * Math.PI; th <= 2.5 * Math.PI; th += Math.PI / 36) {
        const r = r0 * Math.pow(phi, th / (Math.PI / 2));
        if (r > 6000) break;
        pts.push({ x: c.x + r * Math.cos(a0 + th), y: c.y + r * Math.sin(a0 + th) });
      }
      P.line(px[0], px[1], { dash: 1, width: 1 });
      P.curve(pts);
    } }),
  T({ id: "fib_arcs", name: "Fib speed resistance arcs", group: "fib", section: "Fibonacci", icon: "M16 16a12 12 0 0 0-12-12M16 16a7 7 0 0 0-7-7", n: 2,
    levels: FIB_ARC, fields: ["levels", "showLabels", "width"], defaults: { width: 1 },
    draw: (P, px, d) => {
      const [a, b] = px, R = dist(a, b), dir = Math.atan2(a.y - b.y, a.x - b.x);
      P.line(a, b, { dash: 1, width: 1 });
      on(d).forEach((l) => {
        P.ellipse(b.x, b.y, R * l.v, R * l.v, 0, { color: l.color, a0: dir - Math.PI / 2, a1: dir + Math.PI / 2 });
        if (d.style.showLabels) P.text(lvlText(l.v), b.x + Math.cos(dir) * R * l.v, b.y + Math.sin(dir) * R * l.v, { size: 11, color: l.color, noHit: true });
      });
    } }),
  T({ id: "fib_wedge", name: "Fib wedge", group: "fib", section: "Fibonacci", icon: "M2 9L16 2M2 9L16 16M12 4a8 8 0 0 1 0 10", n: 3, levels: FIB_ARC,
    fields: ["levels", "showLabels", "fill", "fillOn", "width"], defaults: { width: 1 },
    draw: (P, px, d) => {
      const [a, b, c] = px, R = dist(a, b), t1 = Math.atan2(b.y - a.y, b.x - a.x);
      let t2 = Math.atan2(c.y - a.y, c.x - a.x);
      if (t2 < t1) t2 += Math.PI * 2;
      if (t2 - t1 > Math.PI) t2 -= Math.PI * 2;
      const lo = Math.min(t1, t2), hi = Math.max(t1, t2);
      P.line(a, b); P.line(a, { x: a.x + R * Math.cos(t2), y: a.y + R * Math.sin(t2) });
      on(d).forEach((l) => {
        P.ellipse(a.x, a.y, R * l.v, R * l.v, 0, { color: l.color, a0: lo, a1: hi });
        if (d.style.showLabels) P.text(lvlText(l.v), a.x + R * l.v * Math.cos(hi), a.y + R * l.v * Math.sin(hi), { size: 11, color: l.color, noHit: true });
      });
    } }),
  T({ id: "pitchfan", name: "Pitchfan", group: "fib", section: "Fibonacci", icon: "M2 9L16 2M2 9L16 9M2 9L16 16M16 2v14", n: 3, levels: PFAN,
    fields: ["levels", "fill", "fillOn", "width", "extendRight"], defaults: { width: 1, extendRight: true },
    draw: (P, px, d) => {
      const [a, b, c] = px;
      P.line(b, c, { width: 1, dash: 1 });
      let prev: [XY, XY] | null = null;
      on(d).forEach((l) => {
        const q = lerp(b, c, l.v), seg = P.xline(a, q, false, d.style.extendRight, { color: l.color });
        if (d.style.fillOn && prev && seg) P.fillPoly([a, prev[1], seg[1]], withAlpha(l.color, 0.1));
        prev = seg;
      });
    } }),
  // ---- gann
  T({ id: "gann_box", name: "Gann box", group: "fib", section: "Gann", icon: "M2 2h14v14H2zM2 6h14M2 11h14M6 2v14M11 2v14", n: 2, levels: GANN_BOX,
    fields: ["levels", "showLabels", "fill", "fillOn", "middle", "width"], defaults: { width: 1, middle: true },
    draw: (P, px, d) => {
      const [a, b] = px, L = on(d);
      P.rect(a, b, { fill: null });
      L.forEach((l, i) => {
        const y = a.y + (b.y - a.y) * l.v, x = a.x + (b.x - a.x) * l.v;
        P.line({ x: a.x, y }, { x: b.x, y }, { color: l.color, width: 1 });
        P.line({ x, y: a.y }, { x, y: b.y }, { color: l.color, width: 1 });
        if (d.style.fillOn && i > 0) {
          const pv = L[i - 1].v, y0 = a.y + (b.y - a.y) * pv;
          P.fillPoly([{ x: a.x, y: y0 }, { x: b.x, y: y0 }, { x: b.x, y }, { x: a.x, y }], withAlpha(l.color, 0.08));
        }
        if (d.style.showLabels) { P.text(lvlText(l.v), a.x - 3, y, { align: "right", base: "middle", size: 10, color: l.color, noHit: true });
          P.text(lvlText(l.v), x, Math.max(a.y, b.y) + 3, { align: "center", base: "top", size: 10, color: l.color, noHit: true }); }
      });
      if (d.style.middle) { P.line(a, b, { dash: 1, width: 1 }); P.line({ x: a.x, y: b.y }, { x: b.x, y: a.y }, { dash: 1, width: 1 }); }
    }, anchors: rectAnchors }),
  ...([["gann_square_fixed", "Gann square fixed", true], ["gann_square", "Gann square", false]] as const).map(([id, name, fixed]) => T({
    id, name, group: "fib", section: "Gann", icon: "M2 2h14v14H2zM2 16L16 2M2 16a14 14 0 0 1 14-14", n: 2, levels: GANN_BOX,
    fields: ["levels", "fill", "fillOn", "width"], defaults: { width: 1, fillOn: false },
    draw: (P, px, d) => {
      const a = px[0];
      let b = px[1];
      if (fixed) { const s = Math.max(Math.abs(b.x - a.x), Math.abs(b.y - a.y)); b = { x: a.x + Math.sign(b.x - a.x || 1) * s, y: a.y + Math.sign(b.y - a.y || 1) * s }; }
      P.rect(a, b, { fill: d.style.fillOn ? d.style.fill : null });
      const L = on(d), rx = Math.abs(b.x - a.x), ry = Math.abs(b.y - a.y);
      const a0 = Math.atan2(Math.sign(b.y - a.y), 0), a1 = Math.atan2(0, Math.sign(b.x - a.x));
      L.forEach((l) => {
        P.line({ x: a.x, y: a.y + (b.y - a.y) * l.v }, { x: b.x, y: a.y + (b.y - a.y) * l.v }, { color: withAlpha(l.color, 0.5), width: 1, noHit: true });
        P.line({ x: a.x + (b.x - a.x) * l.v, y: a.y }, { x: a.x + (b.x - a.x) * l.v, y: b.y }, { color: withAlpha(l.color, 0.5), width: 1, noHit: true });
        P.line(a, { x: b.x, y: a.y + (b.y - a.y) * l.v }, { color: l.color, width: 1 });
        P.line(a, { x: a.x + (b.x - a.x) * l.v, y: b.y }, { color: l.color, width: 1 });
        if (l.v > 0) P.ellipse(a.x, a.y, rx * l.v, ry * l.v, 0, { color: l.color, width: 1, a0: Math.min(a0, a1), a1: Math.max(a0, a1), noHit: true });
      });
    }, anchors: fixed ? undefined : rectAnchors })),
  T({ id: "gann_fan", name: "Gann fan", group: "fib", section: "Gann", icon: "M2 16L16 2M2 16L16 9M2 16L9 2M2 16L16 13M2 16L13 2", n: 2, levels: GANN_FAN,
    fields: ["levels", "showLabels", "fill", "fillOn", "width"], defaults: { width: 1, fill: "#2962ff10" },
    draw: (P, px, d) => {
      const [a, b] = px, dx = b.x - a.x, dy = b.y - a.y;
      let prev: [XY, XY] | null = null;
      const NAMES: Record<string, string> = { "0.125": "1/8", "0.25": "1/4", "0.3333": "1/3", "0.5": "1/2", "1": "1/1", "2": "2/1", "3": "3/1", "4": "4/1", "8": "8/1" };
      on(d).forEach((l) => {
        const seg = P.xline(a, { x: a.x + dx, y: a.y + dy * l.v }, false, true, { color: l.color });
        if (d.style.fillOn && prev && seg) P.fillPoly([a, prev[1], seg[1]], withAlpha(l.color, 0.08));
        if (d.style.showLabels) P.text(NAMES[String(l.v)] ?? lvlText(l.v), a.x + dx, a.y + dy * l.v, { base: "middle", size: 11, color: l.color, noHit: true });
        prev = seg;
      });
    } }),
  // ---- patterns
  T({ id: "xabcd", name: "XABCD pattern", group: "patterns", section: "Patterns", icon: "M2 14L5 4L9 10L13 3L16 15", n: 5, fields: [...LINE, ...FILL, "textColor", "fontSize"],
    defaults: { fill: "#2962ff26" }, draw: (P, px, d) => harmonic(P, px, d, ["X", "A", "B", "C", "D"]) }),
  T({ id: "cypher", name: "Cypher pattern", group: "patterns", section: "Patterns", icon: "M2 14L5 4L9 9L13 2L16 12", n: 5, fields: [...LINE, ...FILL, "textColor", "fontSize"],
    defaults: { fill: "#2962ff26" }, draw: (P, px, d) => harmonic(P, px, d, ["X", "A", "B", "C", "D"]) }),
  T({ id: "abcd", name: "ABCD pattern", group: "patterns", section: "Patterns", icon: "M2 14L7 4L11 10L16 2", n: 4, fields: [...LINE, "textColor", "fontSize"],
    draw: (P, px, d) => {
      P.path(px); const p = d.points, s = d.style;
      if (px.length >= 3) ratioLabel(P, px[0], px[2], ratio(p[2].p - p[1].p, p[1].p - p[0].p), s);
      if (px.length >= 4) ratioLabel(P, px[1], px[3], ratio(p[3].p - p[2].p, p[2].p - p[1].p), s);
      px.forEach((_, i) => labelAt(P, px, i, "ABCD"[i], s));
    } }),
  T({ id: "head_shoulders", name: "Head and shoulders", group: "patterns", section: "Patterns", icon: "M1 14L4 8L6 11L9 3L12 11L14 8L17 14", n: 7,
    fields: [...LINE, ...FILL, "textColor", "fontSize"], defaults: { fill: "#2962ff1f" },
    draw: (P, px, d) => {
      const s = d.style;
      if (s.fillOn) P.fillPoly(px, s.fill);
      P.path(px);
      if (px.length >= 5) P.xline(px[2], px[4], true, true, { dash: 1, width: 1 });
      [[1, "Left shoulder"], [3, "Head"], [5, "Right shoulder"]].forEach(([i, t]) => { if (px[i as number]) labelAt(P, px, i as number, t as string, s); });
    } }),
  T({ id: "triangle_pattern", name: "Triangle pattern", group: "patterns", section: "Patterns", icon: "M2 3L16 9L2 15M2 3L7 13L11 6", n: 4,
    fields: [...LINE, ...FILL, "textColor", "fontSize"], defaults: { fill: "#2962ff1f" },
    draw: (P, px, d) => {
      const s = d.style;
      P.path(px);
      if (px.length >= 4) {
        const [a, b, c, dd] = px, d1 = sub(c, a), d2 = sub(dd, b), den = d1.x * d2.y - d1.y * d2.x;
        if (Math.abs(den) > 1e-9) {
          const t = ((b.x - a.x) * d2.y - (b.y - a.y) * d2.x) / den, apex = add(a, mul(d1, t));
          if (t > 0 && t < 50) { P.line(a, apex, { dash: 1, width: 1 }); P.line(b, apex, { dash: 1, width: 1 }); if (s.fillOn) P.fillPoly([a, apex, b], s.fill); }
        }
      }
      px.forEach((_, i) => labelAt(P, px, i, "ABCD"[i], s));
    } }),
  T({ id: "three_drives", name: "Three drives pattern", group: "patterns", section: "Patterns", icon: "M1 15L3 10L5 12L8 6L10 9L13 2L17 5", n: 7,
    fields: [...LINE, "textColor", "fontSize"],
    draw: (P, px, d) => {
      const p = d.points, s = d.style;
      P.path(px);
      if (px.length >= 5) ratioLabel(P, px[1], px[3], ratio(p[3].p - p[2].p, p[1].p - p[0].p), s);
      if (px.length >= 7) ratioLabel(P, px[3], px[5], ratio(p[5].p - p[4].p, p[3].p - p[2].p), s);
      [[1, "Drive 1"], [3, "Drive 2"], [5, "Drive 3"]].forEach(([i, t]) => { if (px[i as number]) labelAt(P, px, i as number, t as string, s); });
    } }),
  T({ id: "elliott_impulse", name: "Elliott impulse wave (1 2 3 4 5)", group: "patterns", section: "Elliott waves", icon: "M1 15L4 9L6 12L10 3L12 8L16 2", n: 6,
    fields: [...LINE, "degree", "textColor", "fontSize"], draw: elliott("impulse") }),
  T({ id: "elliott_correction", name: "Elliott correction wave (A B C)", group: "patterns", section: "Elliott waves", icon: "M2 3L7 12L10 7L16 16", n: 4,
    fields: [...LINE, "degree", "textColor", "fontSize"], draw: elliott("correction") }),
  T({ id: "elliott_triangle", name: "Elliott triangle wave (A B C D E)", group: "patterns", section: "Elliott waves", icon: "M1 3L4 14L7 5L10 12L13 7L16 10", n: 6,
    fields: [...LINE, "degree", "textColor", "fontSize"], draw: elliott("triangle") }),
  T({ id: "elliott_double", name: "Elliott double combo wave (W X Y)", group: "patterns", section: "Elliott waves", icon: "M2 3L7 12L11 7L16 16", n: 4,
    fields: [...LINE, "degree", "textColor", "fontSize"], draw: elliott("double") }),
  T({ id: "elliott_triple", name: "Elliott triple combo wave (W X Y X Z)", group: "patterns", section: "Elliott waves", icon: "M1 2L4 9L7 6L10 13L13 9L16 16", n: 6,
    fields: [...LINE, "degree", "textColor", "fontSize"], draw: elliott("triple") }),
  T({ id: "cyclic_lines", name: "Cyclic lines", group: "patterns", section: "Cycles", icon: "M2 1v16M7 1v16M12 1v16M17 1v16", n: 2, fields: ["color", "width", "dash"], defaults: { width: 1 },
    draw: (P, px, _d, e) => {
      const dx = px[1].x - px[0].x;
      if (Math.abs(dx) < 3) { P.line({ x: px[0].x, y: 0 }, { x: px[0].x, y: e.h }); return; }
      for (let x = px[0].x, k = 0; dx > 0 ? x <= e.w : x >= 0; x += dx, k++) { if (k > 400) break; P.line({ x, y: 0 }, { x, y: e.h }, { dash: k ? 0 : 1 }); }
    } }),
  T({ id: "time_cycles", name: "Time cycles", group: "patterns", section: "Cycles", icon: "M1 14a4 4 0 0 1 8 0a4 4 0 0 1 8 0", n: 2, fields: ["color", "width", "fill", "fillOn"],
    defaults: { width: 1, fillOn: false },
    draw: (P, px, d, e) => {
      const dx = px[1].x - px[0].x, r = Math.abs(dx) / 2, ry = Math.max(8, Math.abs(px[1].y - px[0].y) || r), up = px[1].y <= px[0].y;
      if (r < 2) return;
      for (let x = px[0].x, k = 0; dx > 0 ? x < e.w : x > 0; x += dx, k++) {
        if (k > 300) break;
        P.ellipse(x + dx / 2, px[0].y, r, ry, 0, { a0: up ? Math.PI : 0, a1: up ? Math.PI * 2 : Math.PI, fill: d.style.fillOn ? d.style.fill : null });
      }
    } }),
  T({ id: "sine_line", name: "Sine line", group: "patterns", section: "Cycles", icon: "M1 9c2-6 4-6 6 0s4 6 6 0s3-6 4-3", n: 2, fields: ["color", "width", "dash"],
    draw: (P, px, _d, e) => {
      const hp = px[1].x - px[0].x, A = (px[0].y - px[1].y) / 2, c = (px[0].y + px[1].y) / 2;
      if (Math.abs(hp) < 2) { P.line(px[0], px[1]); return; }
      const pts: XY[] = [];
      for (let x = 0; x <= e.w; x += 3) pts.push({ x, y: c + A * Math.cos((Math.PI * (x - px[0].x)) / hp) });
      P.curve(pts);
    } }),
  // ---- forecasting / projection
  T({ id: "long_position", name: "Long position", group: "forecast", section: "Projection", icon: "M2 3h14v6H2zM2 9h14v5H2z", n: 3, clicks: 1, ...position(true),
    fields: ["account", "risk", "riskPct", "qty"], note: "Sizes the position from the risk you set (or uses the quantity), with the symbol's point value. A drawing only: nothing is traded or recorded." }),
  T({ id: "short_position", name: "Short position", group: "forecast", section: "Projection", icon: "M2 4h14v5H2zM2 9h14v6H2z", n: 3, clicks: 1, ...position(false),
    fields: ["account", "risk", "riskPct", "qty"], note: "Sizes the position from the risk you set (or uses the quantity), with the symbol's point value. A drawing only: nothing is traded or recorded." }),
  T({ id: "forecast", name: "Forecast", group: "forecast", section: "Projection", icon: "M2 13L8 9M8 9l8-6M13 3h3v3", n: 2, fields: [...LINE, "textColor"],
    defaults: { color: "#2962ff", dash: 1 },
    draw: (P, px, d, e) => {
      const [a, b] = d.points;
      P.line(px[0], px[1]); P.arrowHead(px[0], px[1]);
      P.dot(px[0], 4, d.style.color);
      P.pill(`${signed(b.p - a.p, e.dp)} (${signed(((b.p - a.p) / a.p) * 100)}%)\n${fmtPrice(b.p, e.dp)} in ${fmtSpan(b.t - a.t)}, ${e.map.barsBetween(a.t, b.t)} bars`,
        px[1].x + 8, px[1].y, { base: "middle" });
    } }),
  T({ id: "bars_pattern", name: "Bars pattern", group: "forecast", section: "Projection", icon: "M3 5v8M6 3v10M9 6v6M12 4v9M15 7v5", n: 3,
    init: (p, e) => p.length >= 3 ? p : [p[0], p[1] ?? p[0], { t: e.map.toTime(e.map.toLogical((p[1] ?? p[0]).t) + 1), p: (p[1] ?? p[0]).p }],
    fields: ["color", "candles", "mirror", "flip"], defaults: { color: "#2962ff" },
    draw: (P, px, d, e) => {
      const [i0, i1] = barRange(e, d.points[0].t, d.points[1].t), s = d.style;
      P.rect({ x: px[0].x, y: 0 }, { x: px[1].x, y: e.h }, { fill: withAlpha(s.color, 0.06), stroke: false, noHit: true });
      if (i1 < i0) return;
      let src = e.bars.slice(i0, i1 + 1);
      if (s.mirror) src = [...src].reverse().map((b) => ({ ...b, open: b.close, close: b.open }));
      const base = src[0].open, l2 = e.map.toLogical(d.points[2].t), p2 = d.points[2].p;
      const tr = (v: number) => (s.flip ? p2 - (v - base) : p2 + (v - base));
      const half = Math.max(1, e.spacing * 0.35), pts: XY[] = [];
      src.forEach((b, k) => {
        const x = e.xOfL(l2 + k);
        if (s.candles === "line") { pts.push({ x, y: e.yOf(tr(b.close)) }); return; }
        const o = e.yOf(tr(b.open)), c = e.yOf(tr(b.close)), h = e.yOf(tr(s.flip ? b.low : b.high)), l = e.yOf(tr(s.flip ? b.high : b.low));
        P.line({ x, y: h }, { x, y: l }, { width: 1, color: withAlpha(s.color, 0.6) });
        P.rect({ x: x - half, y: o }, { x: x + half, y: c }, { fill: withAlpha(s.color, (s.flip ? b.close < b.open : b.close >= b.open) ? 0.2 : 0.55), color: withAlpha(s.color, 0.7), width: 1 });
      });
      if (pts.length) P.path(pts, { color: withAlpha(s.color, 0.8) });
    } }),
  T({ id: "ghost_feed", name: "Ghost feed", group: "forecast", section: "Projection", icon: "M2 12L6 8L10 11L16 4", n: 2, clicks: -1, fields: ["color"],
    note: "Candles drawn along the path you click: an imagined price path, not data.",
    draw: (P, px, d, e) => {
      const s = d.style, L = d.points.map((q) => e.map.toLogical(q.t)), half = Math.max(1, e.spacing * 0.35);
      for (let i = 1; i < px.length; i++) {
        const nb = Math.max(1, Math.round(L[i] - L[i - 1]));
        for (let k = 0; k < nb; k++) {
          const o = lerp(px[i - 1], px[i], k / nb), c = lerp(px[i - 1], px[i], (k + 1) / nb), x = e.xOfL(L[i - 1] + ((L[i] - L[i - 1]) * (k + 0.5)) / nb);
          const wick = Math.max(2, Math.abs(c.y - o.y) * 0.4);
          P.line({ x, y: Math.min(o.y, c.y) - wick }, { x, y: Math.max(o.y, c.y) + wick }, { color: withAlpha(s.color, 0.5), width: 1, noHit: true });
          P.rect({ x: x - half, y: o.y }, { x: x + half, y: c.y }, { fill: withAlpha(s.color, c.y < o.y ? 0.25 : 0.6), color: withAlpha(s.color, 0.6), width: 1, noHit: true });
        }
      }
      P.path(px, { width: 1, dash: 1, color: withAlpha(s.color, 0.5) });
    } }),
  T({ id: "projection", name: "Projection", group: "forecast", section: "Projection", icon: "M2 15L8 4L14 12M8 4a8 8 0 0 1 6 8", n: 3, fields: [...LINE, ...FILL],
    defaults: { fill: "#2962ff1a" },
    draw: (P, px, d, e) => {
      const [a, b, c] = px, s = d.style, R = dist(b, c), A0 = Math.atan2(a.y - b.y, a.x - b.x), A1 = Math.atan2(c.y - b.y, c.x - b.x);
      if (s.fillOn) P.fillPoly([a, b, c], s.fill);
      P.line(a, b); P.line(b, c);
      P.ellipse(b.x, b.y, R, R, 0, { width: 1, dash: 1, a0: Math.min(A0, A1), a1: Math.max(A0, A1), noHit: true });
      const base = d.points[1].p - d.points[0].p, proj = d.points[2].p - d.points[1].p;
      P.pill(`${(Math.abs(base) > 1e-12 ? (Math.abs(proj / base) * 100).toFixed(1) : "–")}% of the first move\n${signed(proj, e.dp)}`, c.x + 8, c.y, { base: "middle" });
    } }),
  // ---- volume based
  T({ id: "anchored_vwap", name: "Anchored VWAP", group: "forecast", section: "Volume-based", icon: "M2 14C6 8 10 10 16 4M2 14v-4", n: 1, note: VOL_NOTE,
    fields: ["color", "width", "upper", "lower", "fill", "fillOn"], defaults: { color: "#ff9800", fill: "#ff98001a", upper: 1, lower: 1, fillOn: false },
    draw: (P, px, d, e) => {
      const i0 = Math.max(0, Math.ceil(e.map.toLogical(d.points[0].t) - 1e-6)), s = d.style;
      let pv = 0, v = 0, pv2 = 0;
      const mids: XY[] = [], ups: XY[] = [], dns: XY[] = [];
      for (let i = i0; i < e.bars.length; i++) {
        const b = e.bars[i], tp = (b.high + b.low + b.close) / 3, w = b.volume || 0;
        pv += tp * w; v += w; pv2 += tp * tp * w;
        if (!(v > 0)) continue;
        const m = pv / v, sd = Math.sqrt(Math.max(0, pv2 / v - m * m)), x = e.xOfL(i);
        mids.push({ x, y: e.yOf(m) }); ups.push({ x, y: e.yOf(m + s.upper * sd) }); dns.push({ x, y: e.yOf(m - s.lower * sd) });
      }
      P.dot(px[0], 3.5, s.color);
      if (!mids.length) { P.text("No volume after this point", px[0].x + 6, px[0].y, { base: "middle", size: 11, noHit: true }); return; }
      if (s.fillOn) { P.fillPoly([...ups, ...[...dns].reverse()], s.fill); }
      if (s.upper > 0) P.path(ups, { width: 1, dash: 1 });
      if (s.lower > 0) P.path(dns, { width: 1, dash: 1 });
      P.path(mids);
    } }),
  T({ id: "fixed_range_vp", name: "Fixed range volume profile", group: "forecast", section: "Volume-based", icon: "M2 2v14M2 4h8M2 7h12M2 10h6M2 13h9", n: 2, note: VOL_NOTE,
    fields: ["color", "rows", "valueArea", "upColor", "downColor"], defaults: { color: "#ff9800" },
    draw: (P, px, d, e) => {
      const [i0, i1] = barRange(e, d.points[0].t, d.points[1].t), xL = Math.min(px[0].x, px[1].x), xR = Math.max(px[0].x, px[1].x);
      P.rect({ x: xL, y: 0 }, { x: xR, y: e.h }, { fill: withAlpha(d.style.color, 0.04), color: withAlpha(d.style.color, 0.3), width: 1, dash: 1, noHit: true });
      P.shapes.push({ k: "box", x: xL, y: 0, w: xR - xL, h: 14 });
      profile(P, e, d, i0, i1, xL, xR);
    },
    anchors: (px) => [0, 1].map((i) => ({ xy: { x: px[i].x, y: 10 }, cursor: "ew-resize", move: (d: Drawing, to: Pt) => { d.points[i] = to; } })) }),
  T({ id: "anchored_vp", name: "Anchored volume profile", group: "forecast", section: "Volume-based", icon: "M2 2v14M2 5h10M2 9h13M2 13h7", n: 1, note: VOL_NOTE,
    fields: ["color", "rows", "valueArea", "upColor", "downColor"], defaults: { color: "#ff9800" },
    draw: (P, px, d, e) => {
      const i0 = Math.max(0, Math.ceil(e.map.toLogical(d.points[0].t) - 1e-6)), i1 = e.bars.length - 1, xR = e.xOfL(i1);
      P.line({ x: px[0].x, y: 0 }, { x: px[0].x, y: e.h }, { width: 1, dash: 1 });
      profile(P, e, d, i0, i1, px[0].x, Math.max(px[0].x + 30, xR));
    } }),
  // ---- measurer
  T({ id: "price_range", name: "Price range", group: "forecast", section: "Measurer", icon: "M9 2v14M6 5l3-3l3 3M6 13l3 3l3-3M3 2h12M3 16h12", n: 2,
    fields: ["color", "fill", "fillOn", "textColor"], defaults: { fill: "#2962ff26" },
    draw: (P, px, d, e) => {
      const [a, b] = px, [p0, p1] = d.points, x = (a.x + b.x) / 2;
      P.rect(a, b, { fill: fill(d.style), width: 1 });
      P.line({ x, y: a.y }, { x, y: b.y }, { width: 1 }); P.arrowHead({ x, y: a.y }, { x, y: b.y }, { size: 7 });
      const dp = p1.p - p0.p;
      P.pill(`${signed(dp, e.dp)} (${signed((dp / p0.p) * 100)}%) ${Math.round(dp / (e.sym?.tick ?? 0.25)).toLocaleString()} ticks`,
        x, b.y + (b.y < a.y ? -6 : 6), { align: "center", base: b.y < a.y ? "bottom" : "top" });
    }, anchors: rectAnchors }),
  T({ id: "date_range", name: "Date range", group: "forecast", section: "Measurer", icon: "M2 9h14M5 6l-3 3l3 3M13 6l3 3l-3 3M2 3v12M16 3v12", n: 2,
    fields: ["color", "fill", "fillOn", "textColor"], defaults: { fill: "#2962ff26" },
    draw: (P, px, d, e) => {
      const [a, b] = px, [p0, p1] = d.points, y = (a.y + b.y) / 2;
      P.rect(a, b, { fill: fill(d.style), width: 1 });
      P.line({ x: a.x, y }, { x: b.x, y }, { width: 1 }); P.arrowHead({ x: a.x, y }, { x: b.x, y }, { size: 7 });
      const [i0, i1] = barRange(e, p0.t, p1.t);
      const vol = i1 >= i0 ? e.bars.slice(i0, i1 + 1).reduce((s, x) => s + x.volume, 0) : 0;
      P.pill(`${e.map.barsBetween(p0.t, p1.t)} bars, ${fmtSpan(p1.t - p0.t)}\ntick volume ${Math.round(vol).toLocaleString()}`, (a.x + b.x) / 2, Math.max(a.y, b.y) + 6, { align: "center", base: "top" });
    }, anchors: rectAnchors }),
  T({ id: "date_price_range", name: "Date and price range", group: "forecast", section: "Measurer", icon: "M2 2h14v14H2zM9 4v10M4 9h10", n: 2,
    fields: ["color", "fill", "fillOn", "textColor"], defaults: { fill: "#2962ff26" },
    draw: (P, px, d, e) => {
      const [a, b] = px, [p0, p1] = d.points, dp = p1.p - p0.p;
      P.rect(a, b, { fill: withAlpha(dp >= 0 ? "#2962ff" : "#f23645", 0.18), width: 1, color: dp >= 0 ? "#2962ff" : "#f23645" });
      P.line({ x: (a.x + b.x) / 2, y: a.y }, { x: (a.x + b.x) / 2, y: b.y }, { width: 1, noHit: true, color: dp >= 0 ? "#2962ff" : "#f23645" });
      P.line({ x: a.x, y: (a.y + b.y) / 2 }, { x: b.x, y: (a.y + b.y) / 2 }, { width: 1, noHit: true, color: dp >= 0 ? "#2962ff" : "#f23645" });
      P.pill(`${signed(dp, e.dp)} (${signed((dp / p0.p) * 100)}%) ${Math.round(dp / (e.sym?.tick ?? 0.25)).toLocaleString()} ticks\n` +
        `${e.map.barsBetween(p0.t, p1.t)} bars, ${fmtSpan(p1.t - p0.t)}`, (a.x + b.x) / 2, Math.max(a.y, b.y) + 6,
        { align: "center", base: "top", bg: dp >= 0 ? "#2962ffe6" : "#f23645e6" });
    }, anchors: rectAnchors }),
  // ---- brushes
  T({ id: "brush", name: "Brush", group: "shapes", section: "Brushes", icon: "M2 15c3 0 3-4 6-4s2-6 8-8", n: 2, clicks: 0, fields: ["color", "width", ...FILL],
    defaults: { fillOn: false },
    draw: (P, px, d) => { if (d.style.fillOn && px.length > 2) P.fillPoly(px, d.style.fill); P.path(px, { smooth: true, cap: "round" }); } }),
  T({ id: "highlighter", name: "Highlighter", group: "shapes", section: "Brushes", icon: "M3 14l9-9l3 3l-9 9H3z", n: 2, clicks: 0, fields: ["color", "width"],
    defaults: { color: "#ffeb3b59", width: 14 }, draw: (P, px) => P.path(px, { smooth: true, cap: "round" }) }),
  // ---- arrows
  T({ id: "arrow_marker", name: "Arrow marker", group: "shapes", section: "Arrows", icon: "M2 12L12 4l1 4l3-1l-4 8l-1-3z", n: 2, fields: ["color", ...TEXT],
    defaults: { color: "#2962ff" },
    draw: (P, px, d) => {
      const [a, b] = px, L = dist(a, b), dir = norm(sub(b, a)), n = perp(dir), w = Math.max(4, Math.min(14, L / 6)), head = Math.min(L * 0.45, w * 2.6);
      const neck = sub(b, mul(dir, head));
      P.fillPoly([add(a, mul(n, w / 2)), add(neck, mul(n, w / 2)), add(neck, mul(n, w * 1.4)), b, sub(neck, mul(n, w * 1.4)), sub(neck, mul(n, w / 2)), sub(a, mul(n, w / 2))], d.style.color, true);
      if (d.text) P.text(d.text, a.x, a.y + 8, { align: "center", base: "top" });
    } }),
  T({ id: "arrow", name: "Arrow", group: "shapes", section: "Arrows", icon: "M3 15L15 3M9 3h6v6", n: 2, defaults: { rightEnd: "arrow" },
    fields: [...LINE, ...EXT, ...ENDS, ...STATS, ...TEXT],
    draw: (P, px, d, e) => { const s = d.style; P.xline(px[0], px[1], s.extendLeft, s.extendRight); ends(P, px[0], px[1], s); caption(P, d, px[0], px[1]); statBox(P, e, d, px[0], px[1]); } }),
  T({ id: "arrow_up", name: "Arrow mark up", group: "shapes", section: "Arrows", icon: "M9 3l5 6h-3v6H7V9H4z", n: 1, fields: ["color", ...TEXT, "size"],
    defaults: { color: "#089981", size: 20 },
    draw: (P, px, d) => {
      const p = px[0], s = d.style.size, w = s * 0.55;
      P.fillPoly([p, { x: p.x + w, y: p.y + s * 0.55 }, { x: p.x + w * 0.4, y: p.y + s * 0.55 }, { x: p.x + w * 0.4, y: p.y + s }, { x: p.x - w * 0.4, y: p.y + s },
        { x: p.x - w * 0.4, y: p.y + s * 0.55 }, { x: p.x - w, y: p.y + s * 0.55 }], d.style.color, true);
      if (d.text) P.text(d.text, p.x, p.y + s + 4, { align: "center", base: "top", color: d.style.textColor });
    } }),
  T({ id: "arrow_down", name: "Arrow mark down", group: "shapes", section: "Arrows", icon: "M9 15l5-6h-3V3H7v6H4z", n: 1, fields: ["color", ...TEXT, "size"],
    defaults: { color: "#f23645", size: 20 },
    draw: (P, px, d) => {
      const p = px[0], s = d.style.size, w = s * 0.55;
      P.fillPoly([p, { x: p.x + w, y: p.y - s * 0.55 }, { x: p.x + w * 0.4, y: p.y - s * 0.55 }, { x: p.x + w * 0.4, y: p.y - s }, { x: p.x - w * 0.4, y: p.y - s },
        { x: p.x - w * 0.4, y: p.y - s * 0.55 }, { x: p.x - w, y: p.y - s * 0.55 }], d.style.color, true);
      if (d.text) P.text(d.text, p.x, p.y - s - 4, { align: "center", base: "bottom", color: d.style.textColor });
    } }),
  // ---- shapes
  T({ id: "rectangle", name: "Rectangle", group: "shapes", section: "Shapes", icon: "M2 4h14v10H2z", n: 2, key: "R",
    fields: [...LINE, ...FILL, "middle", ...EXT, ...TEXT, "hAlign", "vAlign"], defaults: { fill: "#2962ff26" },
    draw: (P, px, d, e) => {
      const s = d.style, [a, b] = px;
      const x0 = s.extendLeft ? 0 : Math.min(a.x, b.x), x1 = s.extendRight ? e.w : Math.max(a.x, b.x);
      P.rect({ x: x0, y: a.y }, { x: x1, y: b.y }, { fill: fill(s) });
      if (s.middle) P.line({ x: x0, y: (a.y + b.y) / 2 }, { x: x1, y: (a.y + b.y) / 2 }, { dash: 1, width: 1, noHit: true });
      if (d.text) {
        const X = s.hAlign === "left" ? x0 + 6 : s.hAlign === "right" ? x1 - 6 : (x0 + x1) / 2, top = Math.min(a.y, b.y), bot = Math.max(a.y, b.y);
        const Y = s.vAlign === "top" ? top + 4 : s.vAlign === "bottom" ? bot - 4 : (top + bot) / 2;
        P.text(d.text, X, Y, { align: s.hAlign, base: s.vAlign === "top" ? "top" : s.vAlign === "bottom" ? "bottom" : "middle" });
      }
    }, anchors: rectAnchors }),
  T({ id: "rotated_rectangle", name: "Rotated rectangle", group: "shapes", section: "Shapes", icon: "M3 8L9 2L15 8L9 14z", n: 3,
    init: (p, e) => p.length >= 3 ? p : [p[0], p[1] ?? p[0], { t: (p[1] ?? p[0]).t, p: e.pOf(e.yOf((p[1] ?? p[0]).p) + 30) }],
    fields: [...LINE, ...FILL], defaults: { fill: "#2962ff26" },
    draw: (P, px, d) => {
      const [a, b, c] = px, n = norm(perp(sub(b, a))), off = mul(n, (c.x - b.x) * n.x + (c.y - b.y) * n.y);
      P.path([a, b, add(b, off), add(a, off)], { close: true, fill: fill(d.style) });
    } }),
  T({ id: "path", name: "Path", group: "shapes", section: "Shapes", icon: "M2 14L6 6L11 11L16 3M14 3h2v2", n: 2, clicks: -1, fields: [...LINE, ...ENDS],
    defaults: { rightEnd: "arrow" }, draw: (P, px, d) => { P.path(px); if (px.length >= 2) ends(P, px[px.length - 2], px[px.length - 1], d.style); } }),
  T({ id: "circle", name: "Circle", group: "shapes", section: "Shapes", icon: "M9 9m-7 0a7 7 0 1 0 14 0a7 7 0 1 0-14 0", n: 2, fields: [...LINE, ...FILL, ...TEXT],
    defaults: { fill: "#2962ff26" },
    draw: (P, px, d) => { const r = dist(px[0], px[1]); P.ellipse(px[0].x, px[0].y, r, r, 0, { fill: fill(d.style) }); if (d.text) P.text(d.text, px[0].x, px[0].y, { align: "center", base: "middle" }); } }),
  T({ id: "ellipse", name: "Ellipse", group: "shapes", section: "Shapes", icon: "M9 9m-8 0a8 5 0 1 0 16 0a8 5 0 1 0-16 0", n: 3,
    init: (p, e) => p.length >= 3 ? p : [p[0], p[1] ?? p[0], { t: e.map.toTime((e.map.toLogical(p[0].t) + e.map.toLogical((p[1] ?? p[0]).t)) / 2), p: e.pOf((e.yOf(p[0].p) + e.yOf((p[1] ?? p[0]).p)) / 2 - 30) }],
    fields: [...LINE, ...FILL, ...TEXT], defaults: { fill: "#2962ff26" },
    draw: (P, px, d) => {
      const [a, b, c] = px, m = mid(a, b), rx = dist(a, b) / 2, n = norm(perp(sub(b, a))), ry = Math.max(2, Math.abs((c.x - m.x) * n.x + (c.y - m.y) * n.y));
      P.ellipse(m.x, m.y, rx, ry, Math.atan2(b.y - a.y, b.x - a.x), { fill: fill(d.style) });
      if (d.text) P.text(d.text, m.x, m.y, { align: "center", base: "middle" });
    } }),
  T({ id: "polyline", name: "Polyline", group: "shapes", section: "Shapes", icon: "M2 13L6 3L12 6L16 14z", n: 3, clicks: -1, fields: [...LINE, ...FILL],
    defaults: { fill: "#2962ff26" }, draw: (P, px, d) => P.path(px, { close: px.length > 2, fill: px.length > 2 ? fill(d.style) : null }) }),
  T({ id: "triangle", name: "Triangle", group: "shapes", section: "Shapes", icon: "M9 2L16 15H2z", n: 3, fields: [...LINE, ...FILL], defaults: { fill: "#2962ff26" },
    draw: (P, px, d) => P.path(px, { close: true, fill: fill(d.style) }) }),
  T({ id: "arc", name: "Arc", group: "shapes", section: "Shapes", icon: "M2 14a8 8 0 0 1 14 0", n: 3,
    init: (p, e) => p.length >= 3 ? p : [p[0], p[1] ?? p[0], { t: e.map.toTime((e.map.toLogical(p[0].t) + e.map.toLogical((p[1] ?? p[0]).t)) / 2), p: e.pOf((e.yOf(p[0].p) + e.yOf((p[1] ?? p[0]).p)) / 2 - 40) }],
    fields: [...LINE, ...FILL], defaults: { fillOn: false },
    draw: (P, px, d) => {
      const [a, b, c] = px, k = circle3(a, c, b);
      if (!k) { P.line(a, b); return; }
      const t0 = Math.atan2(a.y - k.y, a.x - k.x), t1 = Math.atan2(b.y - k.y, b.x - k.x), tc = Math.atan2(c.y - k.y, c.x - k.x);
      const norm2 = (x: number) => ((x % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI);
      const ccw = norm2(tc - t0) < norm2(t1 - t0);
      const span = ccw ? norm2(t1 - t0) : -norm2(t0 - t1);
      const pts = sample((t) => ({ x: k.x + k.r * Math.cos(t0 + span * t), y: k.y + k.r * Math.sin(t0 + span * t) }), 64);
      if (d.style.fillOn) P.fillPoly(pts, d.style.fill);
      P.curve(pts);
    } }),
  T({ id: "curve", name: "Curve", group: "shapes", section: "Shapes", icon: "M2 14Q9 -2 16 14", n: 3, clicks: 2,
    init: (p, e) => p.length >= 3 ? p : [p[0], p[1] ?? p[0], { t: e.map.toTime((e.map.toLogical(p[0].t) + e.map.toLogical((p[1] ?? p[0]).t)) / 2), p: e.pOf((e.yOf(p[0].p) + e.yOf((p[1] ?? p[0]).p)) / 2 - 40) }],
    fields: [...LINE, ...ENDS],
    draw: (P, px, d) => {
      const [a, b, c] = px, ctrl = sub(mul(c, 2), mid(a, b)), pts = sample(quad(a, ctrl, b));
      P.curve(pts); ends(P, pts[pts.length - 2], b, d.style);
    } }),
  T({ id: "double_curve", name: "Double curve", group: "shapes", section: "Shapes", icon: "M2 9C5 0 8 0 9 9S13 18 16 9", n: 4, clicks: 2,
    init: (p, e) => {
      if (p.length >= 4) return p;
      const a = p[0], b = p[1] ?? p[0], la = e.map.toLogical(a.t), lb = e.map.toLogical(b.t), ya = e.yOf(a.p), yb = e.yOf(b.p);
      return [a, b, { t: e.map.toTime(la + (lb - la) / 3), p: e.pOf(ya + (yb - ya) / 3 - 40) }, { t: e.map.toTime(la + ((lb - la) * 2) / 3), p: e.pOf(ya + ((yb - ya) * 2) / 3 + 40) }];
    },
    fields: [...LINE, ...ENDS],
    draw: (P, px, d) => { const pts = sample(cubic(px[0], px[2], px[3], px[1])); P.curve(pts); ends(P, pts[pts.length - 2], px[1], d.style);
      P.line(px[0], px[2], { width: 1, dash: 2, noHit: true, color: withAlpha(d.style.color, 0.35) }); P.line(px[1], px[3], { width: 1, dash: 2, noHit: true, color: withAlpha(d.style.color, 0.35) }); } }),
  // ---- text & notes
  T({ id: "text", name: "Text", group: "text", section: "Text & notes", icon: "M3 3h12M9 3v12", n: 1, askText: true, text: "Text",
    fields: [...TEXT, ...BOXF], defaults: { textColor: "#2962ff" },
    draw: (P, px, d) => { const s = d.style; P.text(d.text || " ", px[0].x, px[0].y, { base: "top", bg: s.bgOn ? s.bg : undefined, border: s.borderOn ? s.border : undefined, pad: s.bgOn || s.borderOn ? 6 : 2 }); } }),
  T({ id: "anchored_text", name: "Anchored text (stays on screen)", group: "text", section: "Text & notes", icon: "M3 3h12M9 3v12M1 1h3M14 1h3", n: 1, askText: true, text: "Text", screen: true,
    fields: [...TEXT, ...BOXF], defaults: { textColor: "#2962ff" },
    draw: (P, px, d) => { const s = d.style; P.text(d.text || " ", px[0].x, px[0].y, { base: "top", bg: s.bgOn ? s.bg : undefined, border: s.borderOn ? s.border : undefined, pad: s.bgOn || s.borderOn ? 6 : 2 }); } }),
  T({ id: "note", name: "Note", group: "text", section: "Text & notes", icon: "M3 2h12v10l-4 4H3zM11 16v-4h4", n: 1, askText: true, text: "Note",
    fields: ["color", ...TEXT, "bg"], defaults: { color: "#ff9800", textColor: "#ffffff", bg: "#2a2e39f0", fontSize: 13 },
    draw: (P, px, d) => {
      const p = px[0], s = d.style;
      P.fillPoly([{ x: p.x - 7, y: p.y - 16 }, { x: p.x + 7, y: p.y - 16 }, { x: p.x + 7, y: p.y - 5 }, { x: p.x + 2, y: p.y - 5 }, p, { x: p.x - 2, y: p.y - 5 }, { x: p.x - 7, y: p.y - 5 }], s.color, true);
      P.text(d.text, p.x, p.y - 22, { align: "center", base: "bottom", bg: s.bg, border: s.color, pad: 8 });
    } }),
  T({ id: "anchored_note", name: "Anchored note (stays on screen)", group: "text", section: "Text & notes", icon: "M3 2h12v10l-4 4H3zM1 1h3", n: 1, askText: true, text: "Note", screen: true,
    fields: ["color", ...TEXT, "bg"], defaults: { color: "#ff9800", textColor: "#ffffff", bg: "#2a2e39f0", fontSize: 13 },
    draw: (P, px, d) => { P.text(d.text, px[0].x, px[0].y, { base: "top", bg: d.style.bg, border: d.style.color, pad: 8 }); } }),
  T({ id: "price_note", name: "Price note", group: "text", section: "Text & notes", icon: "M2 14L9 7M9 4h7v6H9z", n: 2, fields: ["color", "textColor", "fontSize"],
    defaults: { textColor: "#ffffff" },
    draw: (P, px, d, e) => { P.dot(px[0], 3, d.style.color); P.line(px[0], px[1], { width: 1 }); P.pill(fmtPrice(d.points[0].p, e.dp), px[1].x, px[1].y, { align: "center", base: "middle", size: d.style.fontSize, color: d.style.textColor }); } }),
  T({ id: "pin", name: "Pin", group: "text", section: "Text & notes", icon: "M9 16s-5-5-5-9a5 5 0 0 1 10 0c0 4-5 9-5 9zM9 7m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0", n: 1, askText: true, text: "",
    fields: ["color", ...TEXT, "bg"], defaults: { color: "#2962ff", bg: "#2a2e39f0", textColor: "#ffffff", fontSize: 13 },
    draw: (P, px, d, e) => {
      const p = px[0], c = P.c;
      c.beginPath(); c.moveTo(p.x, p.y); c.bezierCurveTo(p.x - 10, p.y - 12, p.x - 9, p.y - 26, p.x, p.y - 26); c.bezierCurveTo(p.x + 9, p.y - 26, p.x + 10, p.y - 12, p.x, p.y);
      c.fillStyle = d.style.color; c.fill();
      c.beginPath(); c.arc(p.x, p.y - 17, 3.5, 0, Math.PI * 2); c.fillStyle = "#ffffff"; c.fill();
      P.shapes.push({ k: "box", x: p.x - 10, y: p.y - 27, w: 20, h: 27 });
      if (d.text && e.selected) P.text(d.text, p.x, p.y - 32, { align: "center", base: "bottom", bg: d.style.bg, pad: 8 });
    } }),
  T({ id: "table", name: "Table", group: "text", section: "Text & notes", icon: "M2 3h14v12H2zM2 7h14M2 11h14M7 3v12M12 3v12", n: 1, askText: true,
    text: "Cell | Cell\nCell | Cell", fields: ["textColor", "fontSize", "bg", "border"], defaults: { bg: "#1e222df0", border: "#787b86", textColor: "#e4e4e7", fontSize: 12 },
    draw: (P, px, d) => {
      const rows = (d.text || "").split("\n").map((r) => r.split("|").map((x) => x.trim()));
      const cols = Math.max(...rows.map((r) => r.length)), c = P.c, s = d.style, pad = 6, rh = s.fontSize * 1.25 + pad * 2;
      c.font = P.font();
      const cw = Array.from({ length: cols }, (_, j) => Math.max(30, ...rows.map((r) => c.measureText(r[j] ?? "").width)) + pad * 2);
      const W = cw.reduce((a, b) => a + b, 0), H = rh * rows.length, x0 = px[0].x, y0 = px[0].y;
      c.fillStyle = s.bg; c.fillRect(x0, y0, W, H);
      c.strokeStyle = s.border; c.lineWidth = 1; c.setLineDash([]); c.strokeRect(x0 + 0.5, y0 + 0.5, W, H);
      let x = x0;
      cw.forEach((w, j) => { if (j) { c.beginPath(); c.moveTo(x + 0.5, y0); c.lineTo(x + 0.5, y0 + H); c.stroke(); }
        rows.forEach((r, i) => { c.fillStyle = s.textColor; c.textBaseline = "middle"; c.fillText(r[j] ?? "", x + pad, y0 + rh * (i + 0.5)); }); x += w; });
      rows.forEach((_, i) => { if (i) { c.beginPath(); c.moveTo(x0, y0 + rh * i + 0.5); c.lineTo(x0 + W, y0 + rh * i + 0.5); c.stroke(); } });
      P.shapes.push({ k: "box", x: x0, y: y0, w: W, h: H });
    } }),
  T({ id: "callout", name: "Callout", group: "text", section: "Text & notes", icon: "M2 2h14v9H8l-4 5v-5H2z", n: 2, askText: true, text: "Callout",
    fields: ["color", ...TEXT, "bg"], defaults: { color: "#2962ff", bg: "#2962ffe6", textColor: "#ffffff" },
    draw: (P, px, d) => {
      const box = P.text(d.text || " ", px[1].x, px[1].y, { align: "center", base: "middle", bg: d.style.bg, pad: 8 });
      const cx = box.x + box.w / 2, cy = box.y + box.h / 2, n = norm(perp(sub(px[0], { x: cx, y: cy })));
      P.fillPoly([px[0], { x: cx + n.x * 8, y: cy + n.y * 8 }, { x: cx - n.x * 8, y: cy - n.y * 8 }], d.style.bg, true);
      P.text(d.text || " ", px[1].x, px[1].y, { align: "center", base: "middle", pad: 8, noHit: true });
    } }),
  T({ id: "comment", name: "Comment", group: "text", section: "Text & notes", icon: "M2 3h14v9H7l-4 4v-4H2z", n: 1, askText: true, text: "Comment",
    fields: [...TEXT, "bg"], defaults: { bg: "#2962ffe6", textColor: "#ffffff" },
    draw: (P, px, d) => {
      const p = px[0], box = P.text(d.text || " ", p.x, p.y - 10, { align: "left", base: "bottom", bg: d.style.bg, pad: 8, radius: 10 });
      P.fillPoly([p, { x: p.x + 2, y: box.y + box.h - 1 }, { x: p.x + 14, y: box.y + box.h - 1 }], d.style.bg, true);
    } }),
  T({ id: "price_label", name: "Price label", group: "text", section: "Text & notes", icon: "M2 9l4-5h10v10H6z", n: 1, fields: ["color", "textColor", "fontSize"],
    defaults: { textColor: "#ffffff" },
    draw: (P, px, d, e) => {
      const p = px[0], m = P.measure(fmtPrice(d.points[0].p, e.dp), { size: d.style.fontSize }), h = m.h + 8, w = m.w + 14;
      P.fillPoly([p, { x: p.x + 8, y: p.y - h / 2 }, { x: p.x + 8 + w, y: p.y - h / 2 }, { x: p.x + 8 + w, y: p.y + h / 2 }, { x: p.x + 8, y: p.y + h / 2 }], d.style.color, true);
      P.text(fmtPrice(d.points[0].p, e.dp), p.x + 14, p.y, { base: "middle", size: d.style.fontSize, noHit: true });
    } }),
  T({ id: "signpost", name: "Signpost", group: "text", section: "Text & notes", icon: "M9 17V5M4 2h10l2 2l-2 2H4z", n: 1, askText: true, text: "Signpost",
    fields: ["color", ...TEXT], defaults: { color: "#2962ff", textColor: "#ffffff" },
    draw: (P, px, d) => {
      const p = px[0];
      P.line(p, { x: p.x, y: p.y - 40 }, { width: 1.5 });
      P.dot(p, 3, d.style.color);
      P.text(d.text || " ", p.x, p.y - 40, { align: "center", base: "bottom", bg: d.style.color, pad: 6, radius: 12 });
    } }),
  T({ id: "flag_mark", name: "Flag mark", group: "text", section: "Text & notes", icon: "M4 17V2M4 2h10l-3 4l3 4H4", n: 1, fields: ["color", "size"],
    defaults: { color: "#2962ff", size: 22 },
    draw: (P, px, d) => {
      const p = px[0], s = d.style.size;
      P.line(p, { x: p.x, y: p.y - s }, { width: 2 });
      P.fillPoly([{ x: p.x, y: p.y - s }, { x: p.x + s * 0.7, y: p.y - s }, { x: p.x + s * 0.5, y: p.y - s * 0.78 }, { x: p.x + s * 0.7, y: p.y - s * 0.56 }, { x: p.x, y: p.y - s * 0.56 }], d.style.color, true);
    } }),
  // ---- icons
  T({ id: "emoji", name: "Emoji / sticker", group: "icons", section: "Icons", icon: "M9 9m-7 0a7 7 0 1 0 14 0a7 7 0 1 0-14 0M6 11c2 2 4 2 6 0M6.5 7h.1M11.5 7h.1", n: 1,
    fields: ["emoji", "size"],
    draw: (P, px, d) => {
      const c = P.c, s = d.style.size;
      c.font = `${s}px "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", sans-serif`; c.textAlign = "center"; c.textBaseline = "middle";
      c.fillText(d.style.emoji || "⭐", px[0].x, px[0].y);
      P.shapes.push({ k: "box", x: px[0].x - s / 2, y: px[0].y - s / 2, w: s, h: s });
    } }),
];

export const TOOL = Object.fromEntries(TOOLS.map((t) => [t.id, t])) as Record<string, ToolDef>;
export const GROUPS: { id: GroupId; name: string; icon: string }[] = [
  { id: "lines", name: "Trend line tools", icon: TOOL.trend_line.icon },
  { id: "fib", name: "Gann and Fibonacci tools", icon: TOOL.fib_retracement.icon },
  { id: "patterns", name: "Patterns", icon: TOOL.xabcd.icon },
  { id: "forecast", name: "Forecasting and measurement tools", icon: TOOL.long_position.icon },
  { id: "shapes", name: "Geometric shapes", icon: TOOL.brush.icon },
  { id: "text", name: "Annotation tools", icon: TOOL.text.icon },
  { id: "icons", name: "Icons", icon: TOOL.emoji.icon },
];
export const EMOJIS = ["🚀", "⭐", "🔥", "💰", "📈", "📉", "⚠️", "✅", "❌", "🎯", "🐂", "🐻", "💎", "⏰", "📌", "👀", "🤔", "😀", "😬", "💡", "🔔", "🏁", "⚡", "🧱"];

/** Points of a drawing being created: the clicks so far plus the cursor, completed to the tool's shape. */
export function draftPoints(tool: ToolDef, clicked: Pt[], cursor: Pt | null, e: Env): Pt[] {
  const pts = cursor ? [...clicked, cursor] : [...clicked];
  if (!pts.length) return pts;
  const clicks = tool.clicks ?? tool.n;
  if (clicks <= 0) return pts;
  if (tool.init) return tool.init(pts.slice(0, Math.max(1, Math.min(pts.length, clicks))), e).slice(0, tool.n);
  while (pts.length < tool.n) pts.push({ ...pts[pts.length - 1] });
  return pts.slice(0, tool.n);
}
