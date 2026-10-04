/* Candlestick chart (SVG, no library) for My strategy trades: candles in New York time, horizontal price lines
   (entry / stop / target / breakeven / draw), level boxes (gaps, CISDs, rejection blocks) and point markers.
   Works like TradingView (ADR-98): drag the chart to move it freely, also into the empty space past the newest or
   before the oldest candle; dragging up / down switches the price scale to manual (the "A" button, bottom right,
   switches automatic fitting back on); the mouse wheel zooms around the candle under the mouse; drag the price scale to
   stretch / squeeze vertically and the time scale horizontally; double-click a scale to reset it, the chart to go back
   to the trade. A crosshair follows the mouse with its price and time on the scales; the candle's open / high / low /
   close sit top-left. Up candles use --c1, down candles a neutral grey: red stays reserved for losses (the stop line). */
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
const NY_DAY = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric", timeZone: "America/New_York" });
export const nyTime = (sec: number) => NY_FULL.format(sec * 1000);
const fmtP = (v: number) => v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const MIN_SPAN = 8;            // the fewest candle slots a view shows
const KEEP = 3;                // at least this many candles stay on screen while moving into empty space

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

/** A view = a continuous range of candle slots [a, b] (b - a + 1 slots across the plot). It may reach into empty space
    before the first or after the last candle, as long as KEEP candles stay visible. */
type View = [number, number];
function clampView(v: View, n: number): View {
  let [a, b] = v;
  let span = Math.max(MIN_SPAN - 1, b - a);
  span = Math.min(span, Math.max(MIN_SPAN - 1, n * 3));
  const keep = Math.min(KEEP, Math.max(1, n));
  a = Math.max(a, keep - 1 - span);                // the first `keep` candles stay at the right edge at most
  a = Math.min(a, n - keep);                       // the last `keep` candles stay at the left edge at most
  b = a + span;
  return [a, b];
}

export function CandleChart({ candles, tfMinutes, lines = [], boxes = [], markers = [], focus, height = 420, cut, testId }: {
  candles: Candle[]; tfMinutes: number; lines?: PriceLine[]; boxes?: PriceBox[]; markers?: Marker[];
  focus?: { from: number; to: number }; height?: number; cut?: number; testId?: string;
}) {
  const [ref, width] = useWidth();
  const n = candles.length;
  const fit = useMemo((): View => {
    if (!n) return [0, MIN_SPAN];
    if (!focus) return clampView([n - 160, n - 1 + 8], n);
    const a = idxAt(candles, focus.from), b = Math.min(n - 1, idxAt(candles, focus.to + 1) - 1);
    const span = Math.max(30, b - a + 1);
    const pad = Math.round(span * 0.35);
    return clampView([a - pad, Math.max(b, a) + Math.round(pad * 0.6)], n);
  }, [candles, focus?.from, focus?.to]); // eslint-disable-line react-hooks/exhaustive-deps
  const [viewState, setView] = useState<View>(fit);
  useEffect(() => setView(fit), [fit]);
  // ADR-98: never trust a stored view for other candles (a different trade on the same tab used to crash the page)
  const view = clampView(viewState, n);
  const [mouse, setMouse] = useState<{ x: number; y: number } | null>(null);
  const [manualY, setManualY] = useState<[number, number] | null>(null);       // price scale moved / stretched (null = auto)
  useEffect(() => setManualY(null), [fit]);
  const drag = useRef<{ mode: "pan" | "y" | "x"; x: number; y: number; v: View; yr: [number, number]; moved: boolean } | null>(null);

  const [a, b] = view;
  const plotW = width - PAD.l - PAD.r, plotH = height - PAD.t - PAD.b;
  const slots = b - a + 1;
  const cw = plotW / slots;
  const i0 = Math.max(0, Math.ceil(a - 0.5)), i1 = Math.min(n - 1, Math.floor(b + 0.5));
  const vis = i1 >= i0 ? candles.slice(i0, i1 + 1) : [];
  const tFrom = vis.length ? vis[0][0] : 0, tTo = vis.length ? vis[vis.length - 1][0] + tfMinutes * 60 : 0;
  let autoLo = Infinity, autoHi = -Infinity;
  for (const c of vis) { autoLo = Math.min(autoLo, c[3]); autoHi = Math.max(autoHi, c[2]); }
  if (!vis.length && n) { const last = candles[Math.min(n - 1, Math.max(0, Math.round((a + b) / 2)))]; autoLo = last[3]; autoHi = last[2]; }
  for (const l of lines) if (Number.isFinite(l.price) && !l.noRange && (l.from === undefined || l.from <= tTo)) { autoLo = Math.min(autoLo, l.price); autoHi = Math.max(autoHi, l.price); }
  for (const bx of boxes) if (bx.from <= tTo && (bx.to === undefined || bx.to >= tFrom) && Math.abs(bx.top - (autoLo + autoHi) / 2) < (autoHi - autoLo) * 3 + 50) {
    autoLo = Math.min(autoLo, bx.bottom); autoHi = Math.max(autoHi, bx.top);
  }
  if (!Number.isFinite(autoLo)) { autoLo = 0; autoHi = 1; }
  const padY = (autoHi - autoLo) * 0.06 || 1;
  let yLo = autoLo - padY, yHi = autoHi + padY;
  if (manualY) [yLo, yHi] = manualY;
  const y = (p: number) => PAD.t + (1 - (p - yLo) / (yHi - yLo)) * plotH;
  const priceAt = (yy: number) => yLo + (1 - (yy - PAD.t) / plotH) * (yHi - yLo);
  const xIdx = (i: number) => PAD.l + (i - a + 0.5) * cw;
  const idxOfX = (x: number) => a + (x - PAD.l) / cw - 0.5;
  /** x of a time: the left edge of the candle that contains it (beyond the data: the last candle's right edge). */
  const xT = (t: number) => {
    if (!n) return PAD.l;
    const i = idxAt(candles, t);
    if (i >= n) return xIdx(n - 1) + cw / 2;
    if (candles[i][0] === t || i === 0) return xIdx(i) - cw / 2;
    return xIdx(i - 1) - cw / 2;            // t falls inside the previous candle
  };
  /** The time at slot position i (candles: their own time; empty space: extended by the timeframe). */
  const timeAt = (i: number) => (!n ? 0 : i < 0 ? candles[0][0] + i * tfMinutes * 60
    : i > n - 1 ? candles[n - 1][0] + (i - (n - 1)) * tfMinutes * 60 : candles[i][0]);
  const clampX = (x: number) => Math.max(PAD.l, Math.min(PAD.l + plotW, x));

  const where = (x: number, yy: number): "pan" | "y" | "x" =>
    x > PAD.l + plotW ? "y" : yy > PAD.t + plotH ? "x" : "pan";
  const zoom = (factor: number, centre?: number) => {
    const c = centre ?? (a + b) / 2;
    const span = Math.max(MIN_SPAN - 1, (b - a) * factor);
    const na = c - (c - a) * span / Math.max(1e-9, b - a);
    setView(clampView([na, na + span], n));
  };
  const pan = (d: number) => setView(clampView([a + d, b + d], n));
  const svgRef = useRef<SVGSVGElement | null>(null);
  useEffect(() => {              // wheel must be non-passive to stop the page from scrolling
    const el = svgRef.current;
    if (!el) return;
    const h = (e: Event) => {
      const ev = e as WheelEvent;
      ev.preventDefault();
      const rect = el.getBoundingClientRect();
      zoom(ev.deltaY > 0 ? 1.15 : 1 / 1.15, idxOfX(ev.clientX - rect.left));
    };
    el.addEventListener("wheel", h, { passive: false });
    return () => el.removeEventListener("wheel", h);
  });
  // a drag keeps going outside the chart (window listeners), like TradingView
  useEffect(() => {
    const move = (e: MouseEvent) => {
      const d = drag.current, el = svgRef.current;
      if (!d || !el) return;
      const rect = el.getBoundingClientRect();
      const x = e.clientX - rect.left, yy = e.clientY - rect.top;
      const dx = x - d.x, dy = yy - d.y;
      if (Math.abs(dx) + Math.abs(dy) > 2) d.moved = true;
      if (d.mode === "y") {                       // price scale: drag down squeezes, up stretches
        const f = Math.exp(dy / 160);
        const mid = (d.yr[0] + d.yr[1]) / 2, half = (d.yr[1] - d.yr[0]) / 2 * f;
        setManualY([mid - half, mid + half]);
      } else if (d.mode === "x") {                // time scale: drag right stretches, left squeezes (right edge stays)
        const span0 = d.v[1] - d.v[0];
        const span = Math.max(MIN_SPAN - 1, span0 * Math.exp(-dx / 220));
        setView(clampView([d.v[1] - span, d.v[1]], n));
      } else {
        const cw0 = plotW / (d.v[1] - d.v[0] + 1);
        const shift = -dx / cw0;
        setView(clampView([d.v[0] + shift, d.v[1] + shift], n));
        if (Math.abs(dy) > 3 || manualY) {        // moving up / down: the price scale becomes manual
          const ppp = (d.yr[1] - d.yr[0]) / plotH;
          setManualY([d.yr[0] + dy * ppp, d.yr[1] + dy * ppp]);
        }
      }
    };
    const up = () => { drag.current = null; };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    return () => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
  });

  const ticks = priceTicks(yLo, yHi);
  const timeTicks: { x: number; label: string }[] = [];
  const every = Math.max(1, Math.ceil(70 / cw));
  for (let i = Math.ceil(Math.max(0, i0) / every) * every; i <= i1; i += every) {
    const t = candles[i][0];
    const prev = i - every >= 0 ? candles[i - every][0] : null;
    const dayLbl = NY_D.format(t * 1000);
    const newDay = prev === null || NY_D.format(prev * 1000) !== dayLbl;
    timeTicks.push({ x: xIdx(i), label: tfMinutes >= 1440 ? dayLbl : newDay ? `${dayLbl} ${NY_HM.format(t * 1000)}` : NY_HM.format(t * 1000) });
  }
  // crosshair: the candle slot under the mouse (snapped), its price at the mouse
  const inPlot = mouse && mouse.x >= PAD.l && mouse.x <= PAD.l + plotW && mouse.y >= PAD.t && mouse.y <= PAD.t + plotH;
  const slot = inPlot ? Math.round(idxOfX(mouse!.x)) : null;
  const hc = slot !== null && slot >= 0 && slot < n ? candles[slot] : null;
  const legend = hc ?? (n ? candles[Math.min(n - 1, Math.max(0, i1))] : null);
  const cx = slot !== null ? xIdx(slot) : 0;
  const fullTime = (t: number) => (tfMinutes >= 1440 ? NY_DAY.format(t * 1000) : nyTime(t));
  const auto = manualY === null;
  const clipId = `clip-${testId ?? "c"}`;

  return (
    <div className="chart candle-chart" ref={ref} data-testid={testId}>
      <div className="candle-tools">
        <button className="btn btn-ghost btn-sm" onClick={() => zoom(1 / 1.5)} title="Zoom in">+</button>
        <button className="btn btn-ghost btn-sm" onClick={() => zoom(1.5)} title="Zoom out">−</button>
        <button className="btn btn-ghost btn-sm" onClick={() => pan(-(b - a) / 3)} title="Earlier">◀</button>
        <button className="btn btn-ghost btn-sm" onClick={() => pan((b - a) / 3)} title="Later">▶</button>
        <button className="btn btn-ghost btn-sm" onClick={() => { setView(fit); setManualY(null); }} title="Back to the trade">Fit trade</button>
        <span className="muted small">New York time · drag to move (also past the last candle) · wheel to zoom · drag the price or time
          scale to stretch · A = automatic price scale</span>
      </div>
      <div className="candle-stage">
        <svg ref={svgRef} width={width} height={height} role="img" aria-label="candlestick chart"
          onMouseMove={(e: { clientX: number; clientY: number; currentTarget: SVGSVGElement }) => {
            const rect = e.currentTarget.getBoundingClientRect();
            setMouse({ x: e.clientX - rect.left, y: e.clientY - rect.top });
          }}
          onMouseDown={(e: { clientX: number; clientY: number; currentTarget: SVGSVGElement; preventDefault: () => void }) => {
            e.preventDefault();
            const rect = e.currentTarget.getBoundingClientRect();
            const x = e.clientX - rect.left, yy = e.clientY - rect.top;
            drag.current = { mode: where(x, yy), x, y: yy, v: view, yr: [yLo, yHi], moved: false };
          }}
          onMouseLeave={() => setMouse(null)}
          onDoubleClick={(e: { clientX: number; clientY: number; currentTarget: SVGSVGElement }) => {
            const rect = e.currentTarget.getBoundingClientRect();
            const m = where(e.clientX - rect.left, e.clientY - rect.top);
            if (m !== "x") setManualY(null);
            if (m !== "y") setView(fit);
          }}>
          <defs><clipPath id={clipId}><rect x={PAD.l} y={PAD.t} width={plotW} height={plotH} /></clipPath></defs>
          {ticks.map((v) => (
            <g key={v}>
              <line className="gridline" x1={PAD.l} x2={PAD.l + plotW} y1={y(v)} y2={y(v)} />
              <text x={PAD.l + plotW + 6} y={y(v) + 3.5}>{fmtP(v)}</text>
            </g>
          ))}
          {timeTicks.map((t, k) => t.x >= PAD.l && t.x <= PAD.l + plotW ? <text key={k} x={t.x} y={height - 8} textAnchor="middle">{t.label}</text> : null)}
          <line className="axis-line" x1={PAD.l} x2={PAD.l + plotW} y1={PAD.t + plotH} y2={PAD.t + plotH} />
          <rect className="scale-y" x={PAD.l + plotW} y={PAD.t} width={PAD.r} height={plotH} data-testid={testId ? `${testId}-yscale` : undefined}>
            <title>Drag to stretch the price scale, double-click to reset</title></rect>
          <rect className="scale-x" x={PAD.l} y={PAD.t + plotH} width={plotW} height={PAD.b} data-testid={testId ? `${testId}-xscale` : undefined}>
            <title>Drag to stretch the time scale, double-click to go back to the trade</title></rect>
          <g clipPath={`url(#${clipId})`}>
            {boxes.map((bx, k) => {
              const x1 = clampX(xT(bx.from)), x2 = clampX(bx.to === undefined ? PAD.l + plotW : xT(bx.to) + cw);
              if (x2 <= x1) return null;
              return (
                <g key={`b${k}`}>
                  <rect x={x1} width={x2 - x1} y={y(bx.top)} height={Math.max(1, y(bx.bottom) - y(bx.top))} fill={bx.color}
                    fillOpacity={0.14} stroke={bx.color} strokeOpacity={0.55} strokeWidth={1} />
                  <text x={x1 + 3} y={y(bx.top) + 11} style={{ fill: bx.color }}>{bx.label}</text>
                </g>
              );
            })}
            {vis.map((c, k) => {
              const i = i0 + k;
              const up = c[4] >= c[1];
              const col = up ? "var(--c1)" : "var(--c-neutral)";
              const bxx = xIdx(i), bw = Math.max(1, cw * 0.7);
              const top = y(Math.max(c[1], c[4])), bot = y(Math.min(c[1], c[4]));
              return (
                <g key={i}>
                  <line x1={bxx} x2={bxx} y1={y(c[2])} y2={y(c[3])} stroke={col} strokeWidth={1} />
                  <rect x={bxx - bw / 2} width={bw} y={top} height={Math.max(1, bot - top)} fill={col} />
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
              if (i < i0 || i > i1) return null;
              return (
                <g key={`m${k}`}>
                  <circle cx={xIdx(i)} cy={y(m.price)} r={4} fill="none" stroke={m.color} strokeWidth={1.6} />
                  <text x={xIdx(i) + 6} y={y(m.price) - 6} style={{ fill: m.color }}>{m.label}</text>
                </g>
              );
            })}
            {cut !== undefined && n > 0 && (() => {
              const x = xT(cut) + cw;
              return <line x1={x} x2={x} y1={PAD.t} y2={PAD.t + plotH} stroke="var(--warn)" strokeDasharray="4 3" />;
            })()}
            {inPlot && slot !== null && <>
              <line className="crosshair" x1={cx} x2={cx} y1={PAD.t} y2={PAD.t + plotH} data-testid={testId ? `${testId}-crosshair` : undefined} />
              <line className="crosshair" x1={PAD.l} x2={PAD.l + plotW} y1={mouse!.y} y2={mouse!.y} />
            </>}
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
          {inPlot && slot !== null && <>
            <g data-testid={testId ? `${testId}-price-label` : undefined}>
              <rect x={PAD.l + plotW + 2} y={mouse!.y - 8} width={PAD.r - 4} height={16} rx={3} className="xh-label" />
              <text x={PAD.l + plotW + 6} y={mouse!.y + 4} className="xh-text">{fmtP(priceAt(mouse!.y))}</text>
            </g>
            <g data-testid={testId ? `${testId}-time-label` : undefined}>
              <rect x={Math.max(PAD.l, Math.min(PAD.l + plotW - 150, cx - 75))} y={PAD.t + plotH + 3} width={150} height={18} rx={3} className="xh-label" />
              <text x={Math.max(PAD.l, Math.min(PAD.l + plotW - 150, cx - 75)) + 75} y={PAD.t + plotH + 16} textAnchor="middle" className="xh-text">
                {fullTime(timeAt(slot))}</text>
            </g>
          </>}
        </svg>
        {legend && (
          <div className="candle-legend mono small" data-testid={testId ? `${testId}-legend` : undefined}>
            <span className="muted">{fullTime(legend[0])}</span>{" "}
            <span className={legend[4] >= legend[1] ? "up" : "down"}>O {fmtP(legend[1])} H {fmtP(legend[2])} L {fmtP(legend[3])} C {fmtP(legend[4])}</span>
          </div>
        )}
        <button type="button" className={`candle-auto${auto ? " on" : ""}`} style={{ top: PAD.t + plotH + 3, left: PAD.l + plotW + 4 }}
          onClick={() => setManualY(null)} title={auto ? "Automatic price scale (on)" : "Switch the automatic price scale back on"}
          aria-pressed={auto} data-testid={testId ? `${testId}-auto` : undefined}>A</button>
      </div>
    </div>
  );
}
