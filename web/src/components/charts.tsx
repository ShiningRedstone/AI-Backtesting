/* Chart kit (SVG, no chart library). Every chart: responsive width, recessive grid, one y-axis with a
   stated unit, hover values, legend (with toggles) when there are >= 2 series, values in text so colour is
   never the only carrier. Series colours come from the validated --c1..--c3 tokens; negative values sit
   below the zero line (position is the primary encoding, colour secondary). */
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { Hist } from "../api/types";

export const SERIES = ["var(--c1)", "var(--c2)", "var(--c3)"];
const PAD = { l: 52, r: 14, t: 10, b: 26 };

function useWidth(): [{ current: HTMLDivElement | null }, number] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [w, setW] = useState(600);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const set = () => setW(Math.max(200, Math.floor(el.getBoundingClientRect().width)));
    set();
    const RO = (window as unknown as { ResizeObserver?: new (cb: () => void) => { observe: (e: Element) => void; disconnect: () => void } }).ResizeObserver;
    if (!RO) { window.addEventListener("resize", set); return () => window.removeEventListener("resize", set); }
    const ro = new RO(set);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

export function niceTicks(lo: number, hi: number, count = 5): number[] {
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [0];
  if (lo === hi) { lo -= 1; hi += 1; }
  const span = hi - lo, step0 = span / count, mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) ?? 10 * mag;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  return out;
}
export const fmtTick = (v: number) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(Math.abs(v) >= 10000 ? 0 : 1)}k`
  : Math.abs(v) < 10 && v !== Math.round(v) ? v.toFixed(Math.abs(v) < 1 ? 2 : 1) : String(Math.round(v)));

function Tip({ x, y, children }: { x: number; y: number; children?: ReactNode }) {
  return <div className="chart-tip" style={{ left: x, top: y }}>{children}</div>;
}

export interface LegendItem { id: string; label: string; color: string; box?: boolean }
export function Legend({ items, hidden, onToggle }: { items: LegendItem[]; hidden?: Set<string>; onToggle?: (id: string) => void }) {
  if (items.length < 2) return null;
  return (
    <div className="legend" role="group" aria-label="legend">
      {items.map((it) => (
        <button key={it.id} type="button" className={hidden?.has(it.id) ? "off" : ""} onClick={() => onToggle?.(it.id)}
          aria-pressed={!hidden?.has(it.id)} title={onToggle ? "Show / hide" : undefined}>
          <span className={`sw${it.box ? " box" : ""}`} style={{ background: it.color }} />{it.label}
        </button>))}
    </div>
  );
}

function YAxis({ ticks, y, width, unit, zero = true }: { ticks: number[]; y: (v: number) => number; width: number; unit: string; zero?: boolean }) {
  return (
    <g>
      {ticks.map((t) => (
        <g key={t}>
          <line className={t === 0 && zero ? "zero" : "gridline"} x1={PAD.l} x2={width - PAD.r} y1={y(t)} y2={y(t)} />
          <text x={PAD.l - 6} y={y(t) + 3.5} textAnchor="end">{fmtTick(t)}</text>
        </g>))}
      <text x={4} y={PAD.t + 4} textAnchor="start" style={{ fontWeight: 650 }}>{unit}</text>
    </g>
  );
}

// ------------------------------------------------------------------------------------------ line / area
export interface LineSeries { id: string; label: string; color?: string; values: (number | null)[]; area?: boolean; dashed?: boolean }
export function LineChart({ x, series, height = 220, unit = "R", xLabel, fmtX, testId, emptyText = "No data." }: {
  x: string[]; series: LineSeries[]; height?: number; unit?: string; xLabel?: string; fmtX?: (s: string) => string; testId?: string;
  emptyText?: string;
}) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const vis = series.filter((s) => !hidden.has(s.id));
  const { lo, hi } = useMemo(() => {
    let lo = Infinity, hi = -Infinity;
    for (const s of vis) for (const v of s.values) if (v != null && Number.isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    if (!Number.isFinite(lo)) return { lo: 0, hi: 1 };
    return { lo: Math.min(lo, 0), hi: Math.max(hi, 0) };
  }, [vis]);
  if (!x.length) return <div className="empty small">{emptyText}</div>;
  const ticks = niceTicks(lo, hi);
  const t0 = Math.min(lo, ticks[0]), t1 = Math.max(hi, ticks[ticks.length - 1]);
  const iw = width - PAD.l - PAD.r, ih = height - PAD.t - PAD.b;
  const X = (i: number) => PAD.l + (x.length === 1 ? iw / 2 : (i / (x.length - 1)) * iw);
  const Y = (v: number) => PAD.t + ih - ((v - t0) / (t1 - t0 || 1)) * ih;
  const f = fmtX ?? ((s: string) => s.slice(0, 10));
  const xt = [0, Math.floor((x.length - 1) / 3), Math.floor((2 * (x.length - 1)) / 3), x.length - 1].filter((v, i, a) => a.indexOf(v) === i);
  const path = (vals: (number | null)[]) => {
    let d = "", pen = false;
    vals.forEach((v, i) => {
      if (v == null || !Number.isFinite(v)) { pen = false; return; }
      d += `${pen ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };
  const onMove = (e: MouseEvent) => {
    const box = (e.currentTarget as SVGElement).getBoundingClientRect();
    const px = e.clientX - box.left;
    setHover(Math.max(0, Math.min(x.length - 1, Math.round(((px - PAD.l) / iw) * (x.length - 1)))));
  };
  return (
    <div className="chart" ref={ref} data-testid={testId}>
      <Legend items={series.map((s, i) => ({ id: s.id, label: s.label, color: s.color ?? SERIES[i % 3] }))} hidden={hidden}
        onToggle={(id) => setHidden((h) => { const n = new Set(h); if (n.has(id)) n.delete(id); else n.add(id); return n; })} />
      <svg width={width} height={height} onMouseMove={onMove} onMouseLeave={() => setHover(null)} role="img"
        aria-label={`${series.map((s) => s.label).join(", ")} (${unit})`}>
        <YAxis ticks={ticks} y={Y} width={width} unit={unit} />
        <line className="axis-line" x1={PAD.l} x2={width - PAD.r} y1={PAD.t + ih} y2={PAD.t + ih} />
        {xt.map((i) => <text key={i} x={X(i)} y={height - 8} textAnchor={i === 0 ? "start" : i === x.length - 1 ? "end" : "middle"}>{f(x[i])}</text>)}
        {xLabel && <text x={width - PAD.r} y={height - 20} textAnchor="end" className="faint">{xLabel}</text>}
        {series.map((s, si) => hidden.has(s.id) ? null : (
          <g key={s.id}>
            {s.area && <path d={`${path(s.values)}L${X(x.length - 1)},${Y(0)}L${X(0)},${Y(0)}Z`} fill={s.color ?? SERIES[si % 3]} opacity={0.14} />}
            <path d={path(s.values)} fill="none" stroke={s.color ?? SERIES[si % 3]} strokeWidth={2} strokeLinejoin="round"
              strokeDasharray={s.dashed ? "4 3" : undefined} />
          </g>))}
        {hover != null && <>
          <line className="crosshair" x1={X(hover)} x2={X(hover)} y1={PAD.t} y2={PAD.t + ih} />
          {vis.map((s) => { const v = s.values[hover]; const si = series.indexOf(s);
            return v == null ? null : <circle key={s.id} cx={X(hover)} cy={Y(v)} r={4} fill={s.color ?? SERIES[si % 3]} stroke="var(--surface)" strokeWidth={2} />; })}
        </>}
      </svg>
      {hover != null && (
        <Tip x={Math.min(Math.max(X(hover), 80), width - 80)} y={PAD.t + 4}>
          <div className="t">{f(x[hover])}{xLabel ? ` · #${hover + 1}` : ""}</div>
          {vis.map((s) => { const si = series.indexOf(s); const v = s.values[hover];
            return <div key={s.id}><span className="sw" style={{ background: s.color ?? SERIES[si % 3] }} />{s.label}: <b>{v == null ? "—" : `${v.toFixed(3)} ${unit}`}</b></div>; })}
        </Tip>)}
    </div>
  );
}

// ------------------------------------------------------------------------------------------ grouped bars
export interface BarSeries { id: string; label: string; color?: string; values: (number | null)[] }
export function BarChart({ categories, series, height = 200, unit = "R", signed = true, testId, sub }: {
  categories: string[]; series: BarSeries[]; height?: number; unit?: string; signed?: boolean; testId?: string;
  sub?: (i: number) => string | undefined;
}) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<{ c: number; s: number } | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const vis = series.filter((s) => !hidden.has(s.id));
  if (!categories.length) return <div className="empty small">No data.</div>;
  let lo = 0, hi = 0;
  for (const s of vis) for (const v of s.values) if (v != null && Number.isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  const ticks = niceTicks(lo, hi === lo ? lo + 1 : hi);
  const t0 = Math.min(lo, ticks[0]), t1 = Math.max(hi, ticks[ticks.length - 1]);
  const iw = width - PAD.l - PAD.r, ih = height - PAD.t - PAD.b;
  const Y = (v: number) => PAD.t + ih - ((v - t0) / (t1 - t0 || 1)) * ih;
  const band = iw / categories.length, gap = 2;
  const inner = Math.min(44, Math.max(2, (band * 0.72 - gap * (vis.length - 1)) / Math.max(1, vis.length)));
  const groupW = inner * vis.length + gap * (vis.length - 1);
  const signColor = signed && series.length === 1;          // several series: identity by colour, sign by position
  const every = Math.ceil(categories.length / Math.max(1, Math.floor(iw / 46)));
  return (
    <div className="chart" ref={ref} data-testid={testId}>
      <Legend items={series.map((s, i) => ({ id: s.id, label: s.label, color: s.color ?? SERIES[i % 3], box: true }))} hidden={hidden}
        onToggle={(id) => setHidden((h) => { const n = new Set(h); if (n.has(id)) n.delete(id); else n.add(id); return n; })} />
      <svg width={width} height={height} role="img" aria-label={`${series.map((s) => s.label).join(", ")} by category (${unit})`}
        onMouseLeave={() => setHover(null)}>
        <YAxis ticks={ticks} y={Y} width={width} unit={unit} />
        {categories.map((c, ci) => (
          <g key={c}>
            {ci % every === 0 && <text x={PAD.l + band * ci + band / 2} y={height - 8} textAnchor="middle">{c.length > 9 ? c.slice(0, 8) + "…" : c}</text>}
            {vis.map((s, k) => {
              const v = s.values[ci];
              if (v == null || !Number.isFinite(v)) return null;
              const si = series.indexOf(s);
              const x0 = PAD.l + band * ci + (band - groupW) / 2 + k * (inner + gap);
              const y0 = Y(Math.max(0, v)), h = Math.max(1, Math.abs(Y(v) - Y(0)));
              const col = signColor && v < 0 ? "var(--c-neg)" : (s.color ?? SERIES[si % 3]);
              return <rect key={s.id} x={x0} y={y0} width={inner} height={h} rx={Math.min(3, inner / 2)} fill={col}
                opacity={hover && (hover.c !== ci || hover.s !== si) ? 0.55 : 1}
                onMouseEnter={() => setHover({ c: ci, s: si })} />;
            })}
            <rect className="hit" x={PAD.l + band * ci} y={PAD.t} width={band} height={ih}
              onMouseEnter={() => setHover({ c: ci, s: series.indexOf(vis[0]) })} style={{ pointerEvents: hover ? "none" : "auto" }} />
          </g>))}
      </svg>
      {hover && series[hover.s] && (
        <Tip x={Math.min(Math.max(PAD.l + band * hover.c + band / 2, 80), width - 80)} y={PAD.t + 4}>
          <div className="t">{categories[hover.c]}{sub?.(hover.c) ? ` · ${sub(hover.c)}` : ""}</div>
          {vis.map((s) => { const si = series.indexOf(s); const v = s.values[hover.c];
            return <div key={s.id}><span className="sw" style={{ background: s.color ?? SERIES[si % 3] }} />{s.label}: <b>{v == null ? "—" : `${v.toFixed(3)} ${unit}`}</b></div>; })}
        </Tip>)}
    </div>
  );
}

// ------------------------------------------------------------------------------------------ horizontal signed bars
export function HBars({ rows, unit = "R/trade", testId, digits = 3 }: {
  rows: { label: string; value: number | null; note?: string; value2?: number | null }[]; unit?: string; testId?: string; digits?: number;
}) {
  if (!rows.length) return <div className="empty small">No data.</div>;
  const vals = rows.flatMap((r) => [r.value, r.value2]).filter((v): v is number => v != null && Number.isFinite(v));
  const m = Math.max(1e-9, ...vals.map(Math.abs));
  const anyNeg = vals.some((v) => v < 0);
  return (
    <div className="hbars" data-testid={testId} style={{ display: "grid", gridTemplateColumns: "minmax(80px, 28%) 1fr 104px", gap: "4px 8px", alignItems: "center", fontSize: 12 }}>
      {rows.map((r) => {
        const v = r.value;
        const w = v == null ? 0 : (Math.abs(v) / m) * (anyNeg ? 50 : 100);
        const left = anyNeg ? (v != null && v < 0 ? 50 - w : 50) : 0;
        return [
          <div key={r.label + "l"} title={r.label} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--text-2)" }}>{r.label}</div>,
          <div key={r.label + "b"} style={{ position: "relative", height: 14, background: "var(--surface-inset)", borderRadius: 3, border: "1px solid var(--border)" }}
            title={`${r.label}: ${v == null ? "—" : v.toFixed(digits)} ${unit}${r.note ? ` · ${r.note}` : ""}`}>
            {anyNeg && <span style={{ position: "absolute", left: "50%", top: -1, bottom: -1, width: 1, background: "var(--border-focus)" }} />}
            {v != null && <span style={{ position: "absolute", left: `${left}%`, width: `${w}%`, top: 2, bottom: 2, borderRadius: 2,
              background: v < 0 ? "var(--c-neg)" : "var(--c1)" }} />}
          </div>,
          <div key={r.label + "v"} className={`num ${v != null && v < 0 ? "neg" : v != null && v > 0 ? "pos" : ""}`} style={{ textAlign: "right", fontSize: 11.5 }}>
            {v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}`}{r.note && <div className="faint" style={{ fontSize: 10 }}>{r.note}</div>}</div>,
        ];
      })}
      <div /><div className="faint" style={{ fontSize: 10.5, textAlign: "center" }}>{unit}</div><div />
    </div>
  );
}

// ------------------------------------------------------------------------------------------ histogram
export function Histogram({ hist, unit, color = "var(--c2)", marker, height = 170, testId, signed }: {
  hist: Hist | null | undefined; unit: string; color?: string; marker?: { value: number; label: string } | null; height?: number;
  testId?: string; signed?: boolean;
}) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  if (!hist || !hist.counts.length) return <div className="empty small">No data.</div>;
  const { edges, counts } = hist;
  const lo = edges[0], hi = edges[edges.length - 1];
  const maxC = Math.max(1, ...counts);
  const ticks = niceTicks(0, maxC, 4);
  const iw = width - PAD.l - PAD.r, ih = height - PAD.t - PAD.b;
  const X = (v: number) => PAD.l + ((v - lo) / (hi - lo || 1)) * iw;
  const Y = (c: number) => PAD.t + ih - (c / (ticks[ticks.length - 1] || 1)) * ih;
  const xt = niceTicks(lo, hi, Math.max(3, Math.floor(iw / 70))).filter((t) => t >= lo && t <= hi);
  return (
    <div className="chart" ref={ref} data-testid={testId}>
      <svg width={width} height={height} role="img" aria-label={`distribution of ${unit}`} onMouseLeave={() => setHover(null)}>
        <YAxis ticks={ticks} y={Y} width={width} unit="count" zero={false} />
        <line className="axis-line" x1={PAD.l} x2={width - PAD.r} y1={PAD.t + ih} y2={PAD.t + ih} />
        {xt.map((t) => <text key={t} x={X(t)} y={height - 8} textAnchor="middle">{fmtTick(t)}</text>)}
        {signed && lo < 0 && hi > 0 && <line className="zero" x1={X(0)} x2={X(0)} y1={PAD.t} y2={PAD.t + ih} />}
        {counts.map((c, i) => {
          const x0 = X(edges[i]) + 1, x1 = X(edges[i + 1]) - 1;
          const neg = signed && edges[i + 1] <= 0;
          return (
            <g key={i} onMouseEnter={() => setHover(i)}>
              <rect className="hit" x={x0 - 1} y={PAD.t} width={Math.max(1, x1 - x0 + 2)} height={ih} />
              {c > 0 && <rect x={x0} y={Y(c)} width={Math.max(1, x1 - x0)} height={PAD.t + ih - Y(c)} rx={Math.min(2, (x1 - x0) / 2)}
                fill={neg ? "var(--c-neg)" : color} opacity={hover == null || hover === i ? 1 : 0.6} />}
            </g>);
        })}
        {marker && Number.isFinite(marker.value) && (() => {
          const mx = X(Math.max(lo, Math.min(hi, marker.value)));
          return <g><line x1={mx} x2={mx} y1={PAD.t} y2={PAD.t + ih} stroke="var(--accent)" strokeWidth={2} />
            <rect x={mx - 3} y={PAD.t - 2} width={6} height={6} fill="var(--accent)" />
            <text x={mx + 5} y={PAD.t + 10} style={{ fill: "var(--accent)", fontWeight: 650 }}
              textAnchor={mx > width - 140 ? "end" : "start"} dx={mx > width - 140 ? -10 : 0}>{marker.label}</text></g>;
        })()}
      </svg>
      {hover != null && (
        <Tip x={Math.min(Math.max((X(edges[hover]) + X(edges[hover + 1])) / 2, 80), width - 80)} y={PAD.t + 4}>
          <div className="t">{edges[hover].toFixed(3)} … {edges[hover + 1].toFixed(3)} {unit}</div>
          <b>{counts[hover]}</b> of {hist.n}
        </Tip>)}
      <div className="chart-foot"><b style={{ color: "var(--text-2)" }}>x: {unit}</b> · n = {hist.n}{hist.median != null ? ` · median ${hist.median.toFixed(3)} · mean ${hist.mean?.toFixed(3)}` : ""}
        {hist.clipped > 0 ? ` · ${hist.clipped} value(s) outside the axis are drawn in the edge bins` : ""}</div>
    </div>
  );
}

// ------------------------------------------------------------------------------------------ monthly heatmap
export function MonthHeatmap({ years, months, cells, testId }: {
  years: string[]; months: string[]; cells: Record<string, Record<string, { net_r: number; trades: number }>>; testId?: string;
}) {
  if (!years.length) return <div className="empty small">No data.</div>;
  let m = 1e-9;
  for (const y of years) for (const mo of months) { const c = cells[y]?.[mo]; if (c) m = Math.max(m, Math.abs(c.net_r)); }
  const bg = (v: number) => {
    const a = 0.12 + 0.6 * Math.min(1, Math.abs(v) / m);
    return v >= 0 ? `rgba(22, 168, 119, ${a})` : `rgba(216, 84, 95, ${a})`;
  };
  return (
    <div className="heat" data-testid={testId} style={{ gridTemplateColumns: `44px repeat(${months.length}, minmax(0, 1fr)) 56px` }}>
      <div className="cell h" />{months.map((mo) => <div key={mo} className="cell h">{mo}</div>)}<div className="cell h">Year</div>
      {years.map((y) => {
        const tot = months.reduce((s, mo) => s + (cells[y]?.[mo]?.net_r ?? 0), 0);
        return [
          <div key={y} className="cell h">{y}</div>,
          ...months.map((mo) => { const c = cells[y]?.[mo];
            return <div key={y + mo} className="cell" style={{ background: c ? bg(c.net_r) : "var(--surface-inset)" }}
              title={c ? `${mo} ${y}: ${c.net_r.toFixed(2)} net R over ${c.trades} trade(s)` : `${mo} ${y}: no trades`}>
              {c ? (c.net_r > 0 ? "+" : "") + c.net_r.toFixed(1) : ""}</div>; }),
          <div key={y + "t"} className="cell" style={{ background: bg(tot), fontWeight: 700 }}>{(tot > 0 ? "+" : "") + tot.toFixed(1)}</div>,
        ];
      })}
    </div>
  );
}

// ------------------------------------------------------------------------------------------ many paths (Monte Carlo, prop)
export function PathsChart({ paths, highlight, height = 220, unit = "R", testId, xLabel = "trade #", refLines }: {
  paths: number[][]; highlight?: { label: string; values: number[] } | null; height?: number; unit?: string; testId?: string;
  xLabel?: string; refLines?: { value: number; label: string; tone?: "warn" | "ok" }[];
}) {
  const [ref, width] = useWidth();
  const len = Math.max(0, ...paths.map((p) => p.length), highlight?.values.length ?? 0);
  if (!len) return <div className="empty small">No paths.</div>;
  let lo = Infinity, hi = -Infinity;
  for (const p of [...paths, highlight?.values ?? []]) for (const v of p) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  for (const rl of refLines ?? []) { lo = Math.min(lo, rl.value); hi = Math.max(hi, rl.value); }
  const ticks = niceTicks(lo, hi);
  const t0 = Math.min(lo, ticks[0]), t1 = Math.max(hi, ticks[ticks.length - 1]);
  const iw = width - PAD.l - PAD.r, ih = height - PAD.t - PAD.b;
  const X = (i: number) => PAD.l + (len <= 1 ? 0 : (i / (len - 1)) * iw);
  const Y = (v: number) => PAD.t + ih - ((v - t0) / (t1 - t0 || 1)) * ih;
  const d = (p: number[]) => p.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join("");
  return (
    <div className="chart" ref={ref} data-testid={testId}>
      <Legend items={[{ id: "p", label: `${paths.length} resampled paths`, color: "var(--c2)" },
        ...(highlight ? [{ id: "h", label: highlight.label, color: "var(--c1)" }] : [])]} />
      <svg width={width} height={height} role="img" aria-label={`${paths.length} paths (${unit})`}>
        <YAxis ticks={ticks} y={Y} width={width} unit={unit} />
        <text x={width - PAD.r} y={height - 8} textAnchor="end">{xLabel} →</text>
        {paths.map((p, i) => <path key={i} d={d(p)} fill="none" stroke="var(--c2)" strokeWidth={1} opacity={Math.max(0.06, Math.min(0.35, 6 / paths.length))} />)}
        {(refLines ?? []).map((rl) => <g key={rl.label}><line x1={PAD.l} x2={width - PAD.r} y1={Y(rl.value)} y2={Y(rl.value)}
          stroke={rl.tone === "ok" ? "var(--accent)" : "var(--warn)"} strokeDasharray="5 4" strokeWidth={1.2} />
          <text x={width - PAD.r} y={Y(rl.value) - 4} textAnchor="end" style={{ fill: rl.tone === "ok" ? "var(--accent)" : "var(--warn)" }}>{rl.label}</text></g>)}
        {highlight && <path d={d(highlight.values)} fill="none" stroke="var(--c1)" strokeWidth={2} />}
      </svg>
    </div>
  );
}
