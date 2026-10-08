/* Charts (ADR-111): a small canvas painter for the drawing tools. Everything is drawn in CSS pixels of the chart pane,
   and every stroke / fill / text box is also recorded as a hit shape, so selecting and hovering use exactly what was drawn. */
import type { Bar, ChartSymbol } from "../../api/charts";
import type { Drawing, Pt, Style } from "./model";
import { TimeMap, withAlpha } from "./model";

export interface XY { x: number; y: number }
export type Shape =
  | { k: "seg"; a: XY; b: XY; w: number }
  | { k: "area"; pts: XY[] }
  | { k: "box"; x: number; y: number; w: number; h: number }
  | { k: "ell"; cx: number; cy: number; rx: number; ry: number; rot: number; fill: boolean; w: number };

/** What a tool sees while drawing: pane size, coordinate converters, the loaded bars and the instrument. */
export interface Env {
  w: number; h: number; tf: number; tz: string; dp: number;
  map: TimeMap; bars: Bar[]; sym: ChartSymbol | null; spacing: number;
  xOfL: (l: number) => number; lOfX: (x: number) => number;
  yOf: (p: number) => number; pOf: (y: number) => number;
  theme: { text: string; bg: string; muted: string; up: string; down: string };
  selected: boolean;
}
export const xOf = (e: Env, t: number) => e.xOfL(e.map.toLogical(t));
export const tOf = (e: Env, x: number) => e.map.toTime(e.lOfX(x));
export const toXY = (e: Env, d: Drawing, p: Pt): XY => (d.screen ? { x: p.t * e.w, y: p.p * e.h } : { x: xOf(e, p.t), y: e.yOf(p.p) });
export const toPt = (e: Env, d: Drawing, q: XY): Pt => (d.screen ? { t: q.x / Math.max(1, e.w), p: q.y / Math.max(1, e.h) } : { t: tOf(e, q.x), p: e.pOf(q.y) });

// ------------------------------------------------------------------ geometry
export const add = (a: XY, b: XY): XY => ({ x: a.x + b.x, y: a.y + b.y });
export const sub = (a: XY, b: XY): XY => ({ x: a.x - b.x, y: a.y - b.y });
export const mul = (a: XY, k: number): XY => ({ x: a.x * k, y: a.y * k });
export const lerp = (a: XY, b: XY, t: number): XY => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });
export const mid = (a: XY, b: XY) => lerp(a, b, 0.5);
export const dist = (a: XY, b: XY) => Math.hypot(a.x - b.x, a.y - b.y);
export const perp = (a: XY): XY => ({ x: -a.y, y: a.x });
export const norm = (a: XY): XY => { const l = Math.hypot(a.x, a.y) || 1; return { x: a.x / l, y: a.y / l }; };
export function segDist(p: XY, a: XY, b: XY): number {
  const dx = b.x - a.x, dy = b.y - a.y, L = dx * dx + dy * dy;
  const t = L ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / L)) : 0;
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}
export function inPoly(p: XY, pts: XY[]): boolean {
  let c = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const a = pts[i], b = pts[j];
    if ((a.y > p.y) !== (b.y > p.y) && p.x < ((b.x - a.x) * (p.y - a.y)) / (b.y - a.y || 1e-9) + a.x) c = !c;
  }
  return c;
}
/** The part of the line a->b (extended left / right as asked) inside the pane, or null. */
export function clipLine(a: XY, b: XY, left: boolean, right: boolean, w: number, h: number): [XY, XY] | null {
  const dx = b.x - a.x, dy = b.y - a.y;
  let t0 = left ? -1e9 : 0, t1 = right ? 1e9 : 1;
  const m = 4, box = [-m, w + m, -m, h + m];
  const P = [-dx, dx, -dy, dy], Q = [a.x - box[0], box[1] - a.x, a.y - box[2], box[3] - a.y];
  for (let i = 0; i < 4; i++) {
    if (Math.abs(P[i]) < 1e-12) { if (Q[i] < 0) return null; continue; }
    const r = Q[i] / P[i];
    if (P[i] < 0) { if (r > t1) return null; if (r > t0) t0 = r; } else { if (r < t0) return null; if (r < t1) t1 = r; }
  }
  return [{ x: a.x + t0 * dx, y: a.y + t0 * dy }, { x: a.x + t1 * dx, y: a.y + t1 * dy }];
}
export function circle3(a: XY, b: XY, c: XY): { x: number; y: number; r: number } | null {
  const d = 2 * (a.x * (b.y - c.y) + b.x * (c.y - a.y) + c.x * (a.y - b.y));
  if (Math.abs(d) < 1e-9) return null;
  const a2 = a.x * a.x + a.y * a.y, b2 = b.x * b.x + b.y * b.y, c2 = c.x * c.x + c.y * c.y;
  const x = (a2 * (b.y - c.y) + b2 * (c.y - a.y) + c2 * (a.y - b.y)) / d;
  const y = (a2 * (c.x - b.x) + b2 * (a.x - c.x) + c2 * (b.x - a.x)) / d;
  return { x, y, r: Math.hypot(a.x - x, a.y - y) };
}

export function hitShape(s: Shape, p: XY, tol: number): number | null {
  switch (s.k) {
    case "seg": { const d = segDist(p, s.a, s.b); return d <= tol + s.w / 2 ? d : null; }
    case "area": return inPoly(p, s.pts) ? tol : null;
    case "box": return p.x >= s.x - 2 && p.x <= s.x + s.w + 2 && p.y >= s.y - 2 && p.y <= s.y + s.h + 2 ? 1 : null;
    case "ell": {
      const c = Math.cos(-s.rot), si = Math.sin(-s.rot), dx = p.x - s.cx, dy = p.y - s.cy;
      const u = dx * c - dy * si, v = dx * si + dy * c;
      const r = Math.hypot(u / Math.max(1, s.rx), v / Math.max(1, s.ry));
      if (s.fill && r <= 1) return tol;
      const d = Math.abs(r - 1) * Math.min(s.rx, s.ry);
      return d <= tol + s.w / 2 ? d : null;
    }
  }
}

// ------------------------------------------------------------------ painter
export interface LineOpt { color?: string; width?: number; dash?: number; noHit?: boolean; cap?: CanvasLineCap }
export interface TextOpt { align?: "left" | "center" | "right"; base?: "top" | "middle" | "bottom"; color?: string; size?: number;
  bold?: boolean; italic?: boolean; bg?: string; border?: string; pad?: number; radius?: number; noHit?: boolean; maxW?: number }

export class Painter {
  shapes: Shape[] = [];
  constructor(public c: CanvasRenderingContext2D, public e: Env, public s: Style) {}
  private set(o: LineOpt = {}) {
    const c = this.c, w = o.width ?? this.s.width, dash = o.dash ?? this.s.dash;
    c.strokeStyle = o.color ?? this.s.color;
    c.lineWidth = w;
    c.lineCap = o.cap ?? (dash === 2 ? "round" : "butt");
    c.lineJoin = "round";
    c.setLineDash(dash === 1 ? [w * 3 + 3, w * 2 + 2] : dash === 2 ? [0.1, w * 2 + 2] : []);
    return w;
  }
  line(a: XY, b: XY, o: LineOpt = {}) {
    const w = this.set(o), c = this.c;
    c.beginPath(); c.moveTo(a.x, a.y); c.lineTo(b.x, b.y); c.stroke();
    if (!o.noHit) this.shapes.push({ k: "seg", a, b, w });
  }
  /** A line through a and b, extended as asked, clipped to the pane; returns the drawn segment. */
  xline(a: XY, b: XY, left: boolean, right: boolean, o: LineOpt = {}): [XY, XY] | null {
    const s = clipLine(a, b, left, right, this.e.w, this.e.h);
    if (s) this.line(s[0], s[1], o);
    return s;
  }
  path(pts: XY[], o: LineOpt & { close?: boolean; fill?: string | null; smooth?: boolean } = {}) {
    if (pts.length < 2) return;
    const c = this.c;
    c.beginPath(); c.moveTo(pts[0].x, pts[0].y);
    if (o.smooth && pts.length > 2) {
      for (let i = 1; i < pts.length - 1; i++) { const m = mid(pts[i], pts[i + 1]); c.quadraticCurveTo(pts[i].x, pts[i].y, m.x, m.y); }
      c.lineTo(pts[pts.length - 1].x, pts[pts.length - 1].y);
    } else for (let i = 1; i < pts.length; i++) c.lineTo(pts[i].x, pts[i].y);
    if (o.close) c.closePath();
    if (o.fill) { c.fillStyle = o.fill; c.fill(); if (!o.noHit) this.shapes.push({ k: "area", pts }); }
    if (o.width !== 0) {
      const w = this.set(o);
      c.stroke();
      if (!o.noHit) for (let i = 1; i < pts.length + (o.close ? 1 : 0); i++) this.shapes.push({ k: "seg", a: pts[i - 1], b: pts[i % pts.length], w });
    }
  }
  fillPoly(pts: XY[], fill: string, hit = false) {
    if (pts.length < 3) return;
    const c = this.c;
    c.beginPath(); c.moveTo(pts[0].x, pts[0].y);
    for (const p of pts.slice(1)) c.lineTo(p.x, p.y);
    c.closePath(); c.fillStyle = fill; c.fill();
    if (hit) this.shapes.push({ k: "area", pts });
  }
  rect(a: XY, b: XY, o: LineOpt & { fill?: string | null; stroke?: boolean } = {}) {
    const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y), w = Math.abs(a.x - b.x), h = Math.abs(a.y - b.y);
    const pts = [{ x, y }, { x: x + w, y }, { x: x + w, y: y + h }, { x, y: y + h }];
    if (o.fill) this.fillPoly(pts, o.fill, !o.noHit);
    if (o.stroke !== false) this.path(pts, { ...o, close: true, fill: null });
  }
  ellipse(cx: number, cy: number, rx: number, ry: number, rot: number, o: LineOpt & { fill?: string | null; a0?: number; a1?: number } = {}) {
    const c = this.c, w = this.set(o);
    const full = o.a0 === undefined;
    c.beginPath(); c.ellipse(cx, cy, Math.max(0.5, rx), Math.max(0.5, ry), rot, o.a0 ?? 0, o.a1 ?? Math.PI * 2, false);
    if (o.fill && full) { c.fillStyle = o.fill; c.fill(); }
    if (o.width !== 0) c.stroke();
    if (o.noHit) return;
    if (full) this.shapes.push({ k: "ell", cx, cy, rx, ry, rot, fill: !!o.fill, w });
    else {                                                               // partial arcs: hit along sampled segments
      const n = 24, a0 = o.a0!, a1 = o.a1!;
      let prev: XY | null = null;
      for (let i = 0; i <= n; i++) {
        const t = a0 + ((a1 - a0) * i) / n, u = rx * Math.cos(t), v = ry * Math.sin(t);
        const q = { x: cx + u * Math.cos(rot) - v * Math.sin(rot), y: cy + u * Math.sin(rot) + v * Math.cos(rot) };
        if (prev) this.shapes.push({ k: "seg", a: prev, b: q, w });
        prev = q;
      }
    }
  }
  curve(pts: XY[], o: LineOpt = {}) {                                    // sampled curve points: drawn smooth, hit as segments
    this.path(pts, { ...o, fill: null });
  }
  arrowHead(from: XY, to: XY, o: { color?: string; size?: number } = {}) {
    const sz = o.size ?? Math.max(8, this.s.width * 4), d = norm(sub(to, from)), n = perp(d);
    const back = sub(to, mul(d, sz));
    const c = this.c;
    c.setLineDash([]);
    c.beginPath(); c.moveTo(back.x + n.x * sz * 0.5, back.y + n.y * sz * 0.5); c.lineTo(to.x, to.y);
    c.lineTo(back.x - n.x * sz * 0.5, back.y - n.y * sz * 0.5);
    c.strokeStyle = o.color ?? this.s.color; c.lineWidth = Math.max(1.5, this.s.width); c.lineCap = "round"; c.stroke();
  }
  font(o: TextOpt = {}) {
    const s = this.s;
    return `${(o.italic ?? s.italic) ? "italic " : ""}${(o.bold ?? s.bold) ? "600 " : "400 "}${o.size ?? s.fontSize}px Inter, system-ui, sans-serif`;
  }
  measure(text: string, o: TextOpt = {}) {
    this.c.font = this.font(o);
    const lines = text.split("\n"), size = o.size ?? this.s.fontSize;
    return { w: Math.max(...lines.map((l) => this.c.measureText(l).width)), h: lines.length * size * 1.25, lines, size };
  }
  /** Multi-line text with optional background / border; returns its box. */
  text(text: string, x: number, y: number, o: TextOpt = {}) {
    if (!text) return { x, y, w: 0, h: 0 };
    const c = this.c, m = this.measure(text, o), pad = o.pad ?? (o.bg || o.border ? 6 : 0);
    const W = m.w + pad * 2, H = m.h + pad * 2;
    const align = o.align ?? "left", base = o.base ?? "bottom";
    const bx = align === "left" ? x : align === "center" ? x - W / 2 : x - W;
    const by = base === "top" ? y : base === "middle" ? y - H / 2 : y - H;
    if (o.bg || o.border) {
      c.beginPath();
      const r = o.radius ?? 4;
      c.roundRect(bx, by, W, H, r);
      if (o.bg) { c.fillStyle = o.bg; c.fill(); }
      if (o.border) { c.setLineDash([]); c.lineWidth = 1; c.strokeStyle = o.border; c.stroke(); }
    }
    c.fillStyle = o.color ?? this.s.textColor; c.textBaseline = "middle"; c.textAlign = "left";
    m.lines.forEach((l, i) => {
      const lw = c.measureText(l).width;
      const lx = align === "left" ? bx + pad : align === "center" ? bx + (W - lw) / 2 : bx + W - pad - lw;
      c.fillText(l, lx, by + pad + m.size * 1.25 * (i + 0.5));
    });
    const box = { x: bx, y: by, w: W, h: H };
    if (!o.noHit) this.shapes.push({ k: "box", ...box });
    return box;
  }
  /** A small filled label (price / stats), like TradingView's info boxes. */
  pill(text: string, x: number, y: number, o: TextOpt = {}) {
    return this.text(text, x, y, { size: 12, bg: o.bg ?? withAlpha(this.s.color, 0.9), color: o.color ?? "#ffffff", pad: 5, radius: 3, bold: false,
      italic: false, ...o });
  }
  dot(p: XY, r: number, fill: string, stroke?: string) {
    const c = this.c;
    c.setLineDash([]); c.beginPath(); c.arc(p.x, p.y, r, 0, Math.PI * 2); c.fillStyle = fill; c.fill();
    if (stroke) { c.lineWidth = 1.5; c.strokeStyle = stroke; c.stroke(); }
    this.shapes.push({ k: "ell", cx: p.x, cy: p.y, rx: r, ry: r, rot: 0, fill: true, w: 1 });
  }
}
