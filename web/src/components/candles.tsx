/* Candlestick chart (SVG, no library) for My strategy trades: candles in New York time, horizontal price lines
   (entry / stop / target / breakeven / draw), level boxes (gaps, CISDs, rejection blocks) and point markers.
   Zoom with the mouse wheel or the buttons, drag to pan, double-click to fit the trade again. Like TradingView: drag
   the price scale to stretch / squeeze vertically, drag the time scale to stretch / squeeze horizontally, double-click a
   scale to reset it; once stretched vertically, dragging the chart also moves it up and down. Up candles use --c1,
   down candles a neutral grey: red stays reserved for losses (the stop line). */
import { useEffect, useMemo, useRef, useState } from "react";
import type { Candle } from "../api/my";

export interface PriceLine { price: number; label: string; color: string; dash?: string; from?: number; to?: number; noRange?: boolean }
export interface PriceBox { top: number; bottom: number; from: number; to?: number; label: string; color: string }
export interface Marker { t: number; price: number; label: string; color: string }

const PAD = { l: 8, r: 128, t: 10, b: 26 };
const NY_HM = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "America/New_York" });
const NY_D = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "America/New_York" });
const NY_FULL = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric", hour: "2-digit",
  minute: "2-digit", hour12: false, timeZone: "America/New_York" });
export const nyTime = (sec: number) => NY_FULL.format(sec * 1000);
const fmtP = (v: number) => v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function useWidth(): [{ current: HTMLDivElement | null }, number] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [w, setW] = useState(800);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const set = () => setW(Math.max(260, Math.floor(el.getBoundingClientRect().width)));
    set();
    const RO = (window as unknown as { ResizeObserver?: new (cb: () => void) => { observe: (e: Element) => void; disconnect: () => void } }).ResizeObserver;
    if (!RO) { window.addEventListener("resize", set); return () => window.removeEventListener("resize", set); }
    const ro = new RO(set);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

function priceTicks(lo: number, hi: number, n = 6): number[] {
  const span = hi - lo;
  if (!(span > 0)) return [lo];
  const raw = span / n;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
  return out;
}

/** Index of the first candle whose start is >= t (binary search). */
function idxAt(c: Candle[], t: number): number {
  let lo = 0, hi = c.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (c[m][0] < t) lo = m + 1; else hi = m; }
  return lo;
}

export function CandleChart({ candles, tfMinutes, lines = [], boxes = [], markers = [], focus, height = 420, cut, testId }: {
  candles: Candle[]; tfMinutes: number; lines?: PriceLine[]; boxes?: PriceBox[]; markers?: Marker[];
  focus?: { from: number; to: number }; height?: number; cut?: number; testId?: string;
}) {
  const [ref, width] = useWidth();
  const n = candles.length;
  const fit = useMemo((): [number, number] => {
    if (!n) return [0, 0];
    if (!focus) return [Math.max(0, n - 160), n - 1];
    const a = idxAt(candles, focus.from), b = Math.min(n - 1, idxAt(candles, focus.to + 1) - 1);
    const span = Math.max(30, b - a + 1);
    const pad = Math.round(span * 0.35);
    return [Math.max(0, a - pad), Math.min(n - 1, Math.max(b, a) + Math.round(pad * 0.6))];
  }, [candles, focus?.from, focus?.to]); // eslint-disable-line react-hooks/exhaustive-deps
  const [view, setView] = useState<[number, number]>(fit);
  useEffect(() => setView(fit), [fit]);
  const [hover, setHover] = useState<number | null>(null);
  const [manualY, setManualY] = useState<[number, number] | null>(null);       // a stretched price scale (null = auto)
  useEffect(() => setManualY(null), [fit]);
  const drag = useRef<{ mode: "pan" | "y" | "x"; x: number; y: number; v: [number, number]; yr: [number, number] } | null>(null);

  const [lo, hi] = view;
  const vis = candles.slice(lo, hi + 1);
  const plotW = width - PAD.l - PAD.r, plotH = height - PAD.t - PAD.b;
  const count = Math.max(1, hi - lo + 1);
  const cw = plotW / count;
  const tFrom = vis.length ? vis[0][0] : 0, tTo = vis.length ? vis[vis.length - 1][0] + tfMinutes * 60 : 0;
  let yLo = Infinity, yHi = -Infinity;
  for (const c of vis) { yLo = Math.min(yLo, c[3]); yHi = Math.max(yHi, c[2]); }
  for (const l of lines) if (Number.isFinite(l.price) && !l.noRange && (l.from === undefined || l.from <= tTo)) { yLo = Math.min(yLo, l.price); yHi = Math.max(yHi, l.price); }
  for (const b of boxes) if (b.from <= tTo && (b.to === undefined || b.to >= tFrom) && Math.abs(b.top - (yLo + yHi) / 2) < (yHi - yLo) * 3 + 50) {
    yLo = Math.min(yLo, b.bottom); yHi = Math.max(yHi, b.top);
  }
  if (!Number.isFinite(yLo)) { yLo = 0; yHi = 1; }
  const padY = (yHi - yLo) * 0.06 || 1;
  yLo -= padY; yHi += padY;
  if (manualY) [yLo, yHi] = manualY;
  const y = (p: number) => PAD.t + (1 - (p - yLo) / (yHi - yLo)) * plotH;
  const xIdx = (i: number) => PAD.l + (i - lo + 0.5) * cw;
  /** x of a time: interpolated inside the visible candles (also used for line / box start and end). */
  const xT = (t: number) => {
    if (!n) return PAD.l;
    const i = idxAt(candles, t);
    if (i >= n) return xIdx(n - 1) - cw / 2;
    if (candles[i][0] === t || i === 0) return xIdx(i) - cw / 2;
    return xIdx(i - 1) - cw / 2;            // t falls inside the previous candle
  };
  const clampX = (x: number) => Math.max(PAD.l, Math.min(PAD.l + plotW, x));

  const where = (x: number, yy: number): "pan" | "y" | "x" =>
    x > PAD.l + plotW ? "y" : yy > PAD.t + plotH ? "x" : "pan";
  const zoom = (factor: number, centre?: number) => {
    const c = centre ?? (lo + hi) / 2;
    const span = Math.max(15, Math.min(n, Math.round((hi - lo + 1) * factor)));
    let a = Math.round(c - (c - lo) * span / (hi - lo + 1));
    a = Math.max(0, Math.min(n - span, a));
    setView([a, a + span - 1]);
  };
  const pan = (d: number) => {
    const span = hi - lo;
    const a = Math.max(0, Math.min(n - 1 - span, lo + d));
    setView([a, a + span]);
  };
  const onWheel = (e: WheelEvent & { currentTarget: SVGSVGElement }) => {
    e.preventDefault();
    const rect = e.currentTarget.getBoundingClientRect();
    const i = lo + (e.clientX - rect.left - PAD.l) / cw;
    zoom(e.deltaY > 0 ? 1.2 : 1 / 1.2, i);
  };
  const svgRef = useRef<SVGSVGElement | null>(null);
  useEffect(() => {              // wheel must be non-passive to stop the page from scrolling
    const el = svgRef.current;
    if (!el) return;
    const h = (e: Event) => onWheel(e as WheelEvent & { currentTarget: SVGSVGElement });
    el.addEventListener("wheel", h, { passive: false });
    return () => el.removeEventListener("wheel", h);
  });

  const ticks = priceTicks(yLo, yHi);
  const timeTicks: { x: number; label: string }[] = [];
  const every = Math.max(1, Math.ceil(70 / cw));
  for (let i = lo; i <= hi; i += every) {
    const t = candles[i][0];
    const prev = i - every >= 0 ? candles[i - every][0] : null;
    const day = NY_D.format(t * 1000);
    const newDay = prev === null || NY_D.format(prev * 1000) !== day;
    timeTicks.push({ x: xIdx(i), label: tfMinutes >= 1440 ? day : newDay ? `${day} ${NY_HM.format(t * 1000)}` : NY_HM.format(t * 1000) });
  }
  const hc = hover !== null && hover >= lo && hover <= hi ? candles[hover] : null;

  return (
    <div className="chart candle-chart" ref={ref} data-testid={testId}>
      <div className="candle-tools">
        <button className="btn btn-ghost btn-sm" onClick={() => zoom(1 / 1.5)} title="Zoom in">+</button>
        <button className="btn btn-ghost btn-sm" onClick={() => zoom(1.5)} title="Zoom out">−</button>
        <button className="btn btn-ghost btn-sm" onClick={() => pan(-Math.round((hi - lo) / 3))} title="Earlier">◀</button>
        <button className="btn btn-ghost btn-sm" onClick={() => pan(Math.round((hi - lo) / 3))} title="Later">▶</button>
        <button className="btn btn-ghost btn-sm" onClick={() => { setView(fit); setManualY(null); }} title="Back to the trade">Fit trade</button>
        <span className="muted small">{count} candles · New York time · wheel to zoom, drag to move · drag the price or time scale
          to stretch, double-click it to reset</span>
      </div>
      <svg ref={svgRef} width={width} height={height} role="img" aria-label="candlestick chart"
        onMouseMove={(e: { clientX: number; clientY: number; currentTarget: SVGSVGElement }) => {
          const rect = e.currentTarget.getBoundingClientRect();
          const x = e.clientX - rect.left, yy = e.clientY - rect.top;
          const d = drag.current;
          if (d) {
            const dx = x - d.x, dy = yy - d.y;
            if (d.mode === "y") {                       // price scale: drag down squeezes, up stretches
              const f = Math.exp(dy / 160);
              const mid = (d.yr[0] + d.yr[1]) / 2, half = (d.yr[1] - d.yr[0]) / 2 * f;
              setManualY([mid - half, mid + half]);
            } else if (d.mode === "x") {                // time scale: drag right stretches, left squeezes
              const span0 = d.v[1] - d.v[0] + 1;
              const span = Math.max(10, Math.min(n, Math.round(span0 * Math.exp(-dx / 220))));
              const b = d.v[1], a = Math.max(0, b - span + 1);
              setView([a, Math.min(n - 1, a + span - 1)]);
            } else {
              const span = d.v[1] - d.v[0];
              const a = Math.max(0, Math.min(n - 1 - span, d.v[0] + Math.round(-dx / cw)));
              setView([a, a + span]);
              if (manualY) {                            // a stretched price scale also pans vertically
                const ppp = (d.yr[1] - d.yr[0]) / plotH;
                setManualY([d.yr[0] + dy * ppp, d.yr[1] + dy * ppp]);
              }
            }
            return;
          }
          const i = Math.floor((x - PAD.l) / cw) + lo;
          setHover(where(x, yy) === "pan" && i >= lo && i <= hi ? i : null);
        }}
        onMouseDown={(e: { clientX: number; clientY: number; currentTarget: SVGSVGElement }) => {
          const rect = e.currentTarget.getBoundingClientRect();
          const x = e.clientX - rect.left, yy = e.clientY - rect.top;
          drag.current = { mode: where(x, yy), x, y: yy, v: view, yr: [yLo, yHi] };
        }}
        onMouseUp={() => { drag.current = null; }}
        onMouseLeave={() => { drag.current = null; setHover(null); }}
        onDoubleClick={(e: { clientX: number; clientY: number; currentTarget: SVGSVGElement }) => {
          const rect = e.currentTarget.getBoundingClientRect();
          const m = where(e.clientX - rect.left, e.clientY - rect.top);
          if (m !== "x") setManualY(null);
          if (m !== "y") setView(fit);
        }}>
        <defs><clipPath id={`clip-${testId ?? "c"}`}><rect x={PAD.l} y={PAD.t} width={plotW} height={plotH} /></clipPath></defs>
        {ticks.map((v) => (
          <g key={v}>
            <line className="gridline" x1={PAD.l} x2={PAD.l + plotW} y1={y(v)} y2={y(v)} />
            <text x={PAD.l + plotW + 6} y={y(v) + 3.5}>{fmtP(v)}</text>
          </g>
        ))}
        {timeTicks.map((t, k) => <text key={k} x={t.x} y={height - 8} textAnchor="middle">{t.label}</text>)}
        <line className="axis-line" x1={PAD.l} x2={PAD.l + plotW} y1={PAD.t + plotH} y2={PAD.t + plotH} />
        <rect className="scale-y" x={PAD.l + plotW} y={PAD.t} width={PAD.r} height={plotH} data-testid={testId ? `${testId}-yscale` : undefined}>
          <title>Drag to stretch the price scale, double-click to reset</title></rect>
        <rect className="scale-x" x={PAD.l} y={PAD.t + plotH} width={plotW} height={PAD.b} data-testid={testId ? `${testId}-xscale` : undefined}>
          <title>Drag to stretch the time scale, double-click to reset</title></rect>
        <g clipPath={`url(#clip-${testId ?? "c"})`}>
          {boxes.map((b, k) => {
            const x1 = clampX(xT(b.from)), x2 = clampX(b.to === undefined ? PAD.l + plotW : xT(b.to) + cw);
            if (x2 <= x1) return null;
            return (
              <g key={`b${k}`}>
                <rect x={x1} width={x2 - x1} y={y(b.top)} height={Math.max(1, y(b.bottom) - y(b.top))} fill={b.color}
                  fillOpacity={0.14} stroke={b.color} strokeOpacity={0.55} strokeWidth={1} />
                <text x={x1 + 3} y={y(b.top) + 11} style={{ fill: b.color }}>{b.label}</text>
              </g>
            );
          })}
          {vis.map((c, k) => {
            const i = lo + k;
            const up = c[4] >= c[1];
            const col = up ? "var(--c1)" : "var(--c-neutral)";
            const bx = xIdx(i), bw = Math.max(1, cw * 0.7);
            const top = y(Math.max(c[1], c[4])), bot = y(Math.min(c[1], c[4]));
            return (
              <g key={i}>
                <line x1={bx} x2={bx} y1={y(c[2])} y2={y(c[3])} stroke={col} strokeWidth={1} />
                <rect x={bx - bw / 2} width={bw} y={top} height={Math.max(1, bot - top)} fill={col} />
              </g>
            );
          })}
          {lines.map((l, k) => {
            if (!Number.isFinite(l.price)) return null;
            const x1 = l.from === undefined ? PAD.l : clampX(xT(l.from)), x2 = l.to === undefined ? PAD.l + plotW : clampX(xT(l.to) + cw);
            return <line key={`l${k}`} x1={x1} x2={x2} y1={y(l.price)} y2={y(l.price)} stroke={l.color} strokeWidth={1.4} strokeDasharray={l.dash} />;
          })}
          {markers.map((m, k) => {
            const i = idxAt(candles, m.t);
            if (i < lo || i > hi) return null;
            return (
              <g key={`m${k}`}>
                <circle cx={xIdx(i)} cy={y(m.price)} r={4} fill="none" stroke={m.color} strokeWidth={1.6} />
                <text x={xIdx(i) + 6} y={y(m.price) - 6} style={{ fill: m.color }}>{m.label}</text>
              </g>
            );
          })}
          {cut !== undefined && (() => {
            const x = xT(cut) + cw;
            return <line x1={x} x2={x} y1={PAD.t} y2={PAD.t + plotH} stroke="var(--warn)" strokeDasharray="4 3" />;
          })()}
        </g>
        {lines.map((l, k) => {
          if (!Number.isFinite(l.price)) return null;
          const off = l.price > yHi ? "↑ " : l.price < yLo ? "↓ " : "";
          const ly = Math.max(PAD.t + 7, Math.min(PAD.t + plotH - 7, y(l.price)));
          return (
            <g key={`ll${k}`}>
              <rect x={PAD.l + plotW + 2} y={ly - 7} width={PAD.r - 4} height={14} rx={3} fill={l.color} opacity={0.9} />
              <text x={PAD.l + plotW + 5} y={ly + 3.5} style={{ fill: "var(--bg)", fontWeight: 600 }}>{off}{l.label}</text>
            </g>);
        })}
        {hc && <line className="crosshair" x1={xIdx(hover as number)} x2={xIdx(hover as number)} y1={PAD.t} y2={PAD.t + plotH} />}
      </svg>
      {hc && (
        <div className="candle-info mono small">
          {nyTime(hc[0])} · O {fmtP(hc[1])} H {fmtP(hc[2])} L {fmtP(hc[3])} C {fmtP(hc[4])}
        </div>
      )}
    </div>
  );
}
