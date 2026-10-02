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

function Tip({ x, y, inside, children }: { x: number; y: number; inside?: boolean; children?: ReactNode }) {
  // inside: hang below y within the plot (never clipped by the card above), instead of sitting above the chart
  return <div className="chart-tip" style={{ left: x, top: y, ...(inside ? { transform: "translate(-50%, 0)" } : {}) }}>{children}</div>;
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

// ------------------------------------------------------------------------------------------ step line over time
/** A running total over REAL time (x spaced by date, not by point number): a step line with a light area, since the
    total only changes at each point and holds in between. Starts at 0 at `start`, holds the last value until `end`.
    Optional `band`: a shaded period after the main line (e.g. a locked holdout) with its own label; with `band.points`
    a second line continues there from the main line's last value, in its own colour. Hover shows the date and the
    total on that date. Dates are shown in New York time. */
export interface TimePoint { t: string; v: number; n?: number }   // n: how many trades the total includes
export interface TimeBand { from: string; to: string; label: string; points?: TimePoint[]; seriesLabel?: string; color?: string }
const tms = (s: string) => Date.parse(s.includes("T") ? s : s.replace(" ", "T"));
const NY_YMD = new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit", timeZone: "America/New_York" });
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const NY_DAY = { format: (t: number) => { const [y, m, d] = NY_YMD.format(t).split("-"); return `${Number(d)} ${MON[Number(m) - 1]} ${y}`; } };
const NY_YEAR = new Intl.DateTimeFormat("en-US", { year: "numeric", timeZone: "America/New_York" });
const toPts = (ps: TimePoint[]) => ps.map((p, i) => ({ t: tms(p.t), v: p.v, n: p.n ?? i + 1 })).filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v));
export function StepTimeChart({ points, start, end, band, height = 220, unit = "R", label = "Cumulative net R", color = SERIES[0], testId }: {
  points: TimePoint[]; start?: string; end?: string; band?: TimeBand; height?: number; unit?: string; label?: string; color?: string;
  testId?: string;
}) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  const pts = useMemo(() => toPts(points), [points]);
  const bandPts = useMemo(() => toPts(band?.points ?? []), [band]);
  if (!pts.length) return <div className="empty small">No trades: no curve.</div>;
  const s0 = start ? tms(start) : NaN, s1 = end ? tms(end) : NaN;
  const b0 = band ? tms(band.from) : NaN, b1 = band ? tms(band.to) : NaN;
  const hasBand = Number.isFinite(b0) && Number.isFinite(b1) && b1 > b0;
  const x0 = Number.isFinite(s0) ? Math.min(s0, pts[0].t) : pts[0].t;
  const x1 = Math.max(Number.isFinite(s1) ? s1 : pts[pts.length - 1].t, pts[pts.length - 1].t, hasBand ? b1 : -Infinity,
    bandPts.length ? bandPts[bandPts.length - 1].t : -Infinity, x0 + 1);
  const mainEnd = hasBand ? Math.max(b0, pts[pts.length - 1].t) : x1;   // the main line stops where the band starts
  const base = pts[pts.length - 1].v;                                   // the band line continues from here
  const bColor = band?.color ?? SERIES[1];
  let lo = 0, hi = 0;
  for (const p of pts) { lo = Math.min(lo, p.v); hi = Math.max(hi, p.v); }
  for (const p of bandPts) { lo = Math.min(lo, base + p.v); hi = Math.max(hi, base + p.v); }
  const ticks = niceTicks(lo, hi === lo ? lo + 1 : hi);
  const t0 = Math.min(lo, ticks[0]), t1 = Math.max(hi, ticks[ticks.length - 1]);
  const iw = width - PAD.l - PAD.r, ih = height - PAD.t - PAD.b;
  const X = (t: number) => PAD.l + ((t - x0) / (x1 - x0)) * iw;
  const Y = (v: number) => PAD.t + ih - ((v - t0) / (t1 - t0 || 1)) * ih;
  let d = `M${X(x0).toFixed(1)},${Y(0).toFixed(1)}`;
  for (const p of pts) d += `H${X(p.t).toFixed(1)}V${Y(p.v).toFixed(1)}`;
  d += `H${X(mainEnd).toFixed(1)}`;
  let bd = "";
  if (hasBand && bandPts.length) {
    bd = `M${X(b0).toFixed(1)},${Y(base).toFixed(1)}`;
    for (const p of bandPts) bd += `H${X(p.t).toFixed(1)}V${Y(base + p.v).toFixed(1)}`;
    bd += `H${X(x1).toFixed(1)}`;
  }
  // x ticks: 1 January of each year inside the range (every other year when crowded); otherwise the two end dates
  const y0 = Number(NY_YEAR.format(x0)), y1 = Number(NY_YEAR.format(x1));
  let years: number[] = [];
  for (let y = y0 + 1; y <= y1; y++) years.push(y);
  if (years.length > Math.max(2, Math.floor(iw / 60))) years = years.filter((y) => y % 2 === 0);
  const yearT = (y: number) => Date.parse(`${y}-01-01T05:00:00Z`);    // midnight New York (EST)
  // the total on a date = the last point at or before it (0 before the first point)
  const valueAt = (ps: typeof pts, t: number) => { let v = 0, k = 0; for (const p of ps) { if (p.t > t) break; v = p.v; k = p.n; } return { v, k }; };
  const onMove = (e: MouseEvent) => {
    const box = (e.currentTarget as SVGElement).getBoundingClientRect();
    setHover(Math.max(x0, Math.min(x1, x0 + ((e.clientX - box.left - PAD.l) / iw) * (x1 - x0))));
  };
  const inBand = hover != null && hasBand && hover >= b0;
  const h = hover == null ? null : inBand ? (bandPts.length ? valueAt(bandPts, hover) : null) : valueAt(pts, hover);
  return (
    <div className="chart" ref={ref} data-testid={testId}>
      <svg width={width} height={height} onMouseMove={onMove} onMouseLeave={() => setHover(null)} role="img"
        aria-label={`${label} (${unit}) over time, ending ${base.toFixed(2)} ${unit}${hasBand ? `; ${band!.label}` : ""}`}>
        {hasBand && <g data-testid={testId ? `${testId}-band` : undefined}>
          <rect x={X(b0)} y={PAD.t} width={Math.max(0, X(b1) - X(b0))} height={ih} fill="var(--c-neutral)" opacity={0.16} />
          <text x={X(b0) + 6} y={PAD.t + 12} textAnchor="start" style={{ fontWeight: 600 }}>{band!.label}</text></g>}
        <YAxis ticks={ticks} y={Y} width={width} unit={unit} />
        <line className="axis-line" x1={PAD.l} x2={width - PAD.r} y1={PAD.t + ih} y2={PAD.t + ih} />
        {years.length >= 2 ? years.map((y) => <g key={y}>
          <line className="gridline" x1={X(yearT(y))} x2={X(yearT(y))} y1={PAD.t} y2={PAD.t + ih} opacity={0.5} />
          <text x={X(yearT(y))} y={height - 8} textAnchor="middle">{y}</text></g>)
          : <>
            <text x={X(x0)} y={height - 8} textAnchor="start">{NY_DAY.format(x0)}</text>
            <text x={X(x1)} y={height - 8} textAnchor="end">{NY_DAY.format(x1)}</text></>}
        <path d={`${d}V${Y(0).toFixed(1)}H${X(x0).toFixed(1)}Z`} fill={color} opacity={0.14} />
        <path d={d} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" />
        {bd && <path d={bd} fill="none" stroke={bColor} strokeWidth={2} strokeLinejoin="round" />}
        {hover != null && <>
          <line className="crosshair" x1={X(hover)} x2={X(hover)} y1={PAD.t} y2={PAD.t + ih} />
          {h && <circle cx={X(hover)} cy={Y(inBand ? base + h.v : h.v)} r={4} fill={inBand ? bColor : color} stroke="var(--surface)" strokeWidth={2} />}
        </>}
      </svg>
      {hover != null && (
        <Tip x={Math.min(Math.max(X(hover), 140), width - 140)} y={PAD.t + 22} inside>
          <div className="t">{NY_DAY.format(hover)}</div>
          {!inBand && h && <>
            <div><span className="sw" style={{ background: color }} />{label}: <b>{h.v.toFixed(2)} {unit}</b></div>
            <div className="t" style={{ margin: 0 }}>after {h.k} trade{h.k === 1 ? "" : "s"}</div></>}
          {inBand && !h && <div>{band!.label}</div>}
          {inBand && h && <>
            <div><span className="sw" style={{ background: bColor }} />{band!.seriesLabel ?? band!.label}: <b>{h.v >= 0 ? "+" : ""}{h.v.toFixed(2)} {unit}</b></div>
            <div className="t" style={{ margin: 0 }}>{h.k} holdout trade{h.k === 1 ? "" : "s"} · running total {(base + h.v).toFixed(2)} {unit}</div></>}
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
    <div className="hbars" data-testid={testId} style={{ display: "grid", gridTemplateColumns: "minmax(80px, 30%) minmax(60px, 1fr) max-content",
      gap: "3px 10px", alignItems: "center", fontSize: 12 }}>
      {rows.map((r) => {
        const v = r.value;
        const w = v == null ? 0 : (Math.abs(v) / m) * (anyNeg ? 50 : 100);
        const left = anyNeg ? (v != null && v < 0 ? 50 - w : 50) : 0;
        return [
          <div key={r.label + "l"} title={r.label} style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: "var(--text-2)" }}>{r.label}</div>,
          <div key={r.label + "b"} style={{ position: "relative", height: 11, background: "var(--surface-inset)", borderRadius: 3, border: "1px solid var(--border)" }}
            title={`${r.label}: ${v == null ? "—" : v.toFixed(digits)} ${unit}${r.note ? ` · ${r.note}` : ""}`}>
            {anyNeg && <span style={{ position: "absolute", left: "50%", top: -1, bottom: -1, width: 1, background: "var(--border-focus)" }} />}
            {v != null && <span style={{ position: "absolute", left: `${left}%`, width: `${w}%`, top: 2, bottom: 2, borderRadius: 2,
              background: v < 0 ? "var(--c-neg)" : "var(--c1)" }} />}
          </div>,
          <div key={r.label + "v"} style={{ textAlign: "right", fontSize: 11.5, whiteSpace: "nowrap" }}>
            <span className={`num ${v != null && v < 0 ? "neg" : v != null && v > 0 ? "pos" : ""}`}>{v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(digits)}`}</span>
            {r.note && <span className="faint" style={{ fontSize: 10.5, marginLeft: 8 }}>{r.note}</span>}</div>,
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
        {hist.clipped > 0 ? ` · ${hist.clipped.toLocaleString()} value${hist.clipped === 1 ? "" : "s"} beyond the axis ${hist.clipped === 1 ? "is" : "are"} counted in the edge bar` : ""}</div>
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

// ------------------------------------------------------------------------------------------ scatter (x = share, y = value)
export interface ScatterPoint { id: string; x: number; y: number; label: string; detail?: string }
/** `cluster` (ADR-86): dots of this group that overlap on screen are drawn as one bigger circle (capped size). */
export interface ScatterGroup { id: string; label: string; color: string; points: ScatterPoint[]; hollow?: boolean; size?: number; ring?: boolean;
  cluster?: boolean; clusterDistance?: number }      // ADR-90: Settings grouping distance (1 = touching dots)
export interface ScatterCurve { id: string; label: string; points: { x: number; y: number }[]; tone?: "warn" | "neutral" }
interface Mark { g: number; px: number; py: number; r: number; members: ScatterPoint[] }
const SPAD = { l: 52, r: 14, t: 24, b: 40 };       // room above the plot for the y title and below for the x title
const CLUSTER_MAX_R = 13;                          // the largest a grouped circle gets

/** Group a cluster-able group's dots that overlap on screen (greedy, grid-indexed): each mark is one dot or a circle at
 *  its members' average position whose radius grows with sqrt(count) up to CLUSTER_MAX_R. Display only. */
export function clusterMarks(pts: { px: number; py: number; p: ScatterPoint }[], base: number, g: number, dist = 1): Mark[] {
  const D = (2 * base + 1) * dist, cell = D, grid = new Map<string, number[]>();
  pts.forEach((q, i) => { const k = `${Math.floor(q.px / cell)}:${Math.floor(q.py / cell)}`; (grid.get(k) ?? grid.set(k, []).get(k)!).push(i); });
  const used = new Uint8Array(pts.length), out: Mark[] = [];
  const order = pts.map((_, i) => i).sort((a, b) => pts[a].px - pts[b].px || pts[a].py - pts[b].py);
  for (const i of order) {
    if (used[i]) continue;
    used[i] = 1;
    const m = [i], cx = Math.floor(pts[i].px / cell), cy = Math.floor(pts[i].py / cell);
    for (let a = cx - 1; a <= cx + 1; a++) for (let b = cy - 1; b <= cy + 1; b++) for (const j of grid.get(`${a}:${b}`) ?? []) {
      if (used[j]) continue;
      const dx = pts[j].px - pts[i].px, dy = pts[j].py - pts[i].py;
      if (dx * dx + dy * dy < D * D) { used[j] = 1; m.push(j); }
    }
    const px = m.reduce((s, k) => s + pts[k].px, 0) / m.length, py = m.reduce((s, k) => s + pts[k].py, 0) / m.length;
    out.push({ g, px, py, r: m.length === 1 ? base : Math.min(CLUSTER_MAX_R, base + 1.7 * Math.sqrt(m.length - 1)), members: m.map((k) => pts[k].p) });
  }
  return out;
}

/** Points on a percentage x-axis (0-100 %) against a value y-axis; reference curves are drawn dashed and labelled in the
   legend. Values above `yMax` are pinned to the top edge and say so in the tooltip (never silently dropped). Groups with
   `cluster` merge overlapping dots into bigger circles; a click on one opens `onPickMany` with every member. */
export function ScatterChart({ groups, curves = [], height = 340, xUnit = "win rate", yUnit, yMax, testId, onPick, onPickMany }: {
  groups: ScatterGroup[]; curves?: ScatterCurve[]; height?: number; xUnit?: string; yUnit: string; yMax?: number; testId?: string;
  onPick?: (id: string) => void; onPickMany?: (ids: string[]) => void;
}) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState<number | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const all = groups.filter((g) => !hidden.has(g.id)).flatMap((g) => g.points);
  let hi = 0;
  for (const p of all) if (Number.isFinite(p.y)) hi = Math.max(hi, p.y);
  hi = Math.min(yMax ?? Infinity, Math.max(1, hi * 1.08));
  const ticks = niceTicks(0, hi);
  const t1 = Math.max(hi, ticks[ticks.length - 1]);
  const iw = width - SPAD.l - SPAD.r, ih = height - SPAD.t - SPAD.b;
  const X = (v: number) => SPAD.l + Math.max(0, Math.min(1, v)) * iw;
  const Y = (v: number) => SPAD.t + ih - (Math.max(0, Math.min(t1, v)) / (t1 || 1)) * ih;
  const marks = useMemo(() => groups.flatMap((g, gi) => {
    if (hidden.has(g.id)) return [];
    const pts = g.points.map((p) => ({ px: X(p.x), py: Y(p.y), p }));
    return g.cluster ? clusterMarks(pts, g.size ?? 4, gi, g.clusterDistance ?? 1) : pts.map((q) => ({ g: gi, px: q.px, py: q.py, r: g.size ?? 4, members: [q.p] }));
  }), [groups, hidden, width, height, t1]);  // eslint-disable-line react-hooks/exhaustive-deps
  if (!groups.some((g) => g.points.length)) return <div className="empty small">No points.</div>;
  const xt = [0, 0.2, 0.4, 0.6, 0.8, 1];
  const curveD = (c: ScatterCurve) => c.points.filter((p) => p.y <= t1).map((p, i) => `${i ? "L" : "M"}${X(p.x).toFixed(1)},${Y(p.y).toFixed(1)}`).join("");
  const items: LegendItem[] = [...groups.map((g) => ({ id: g.id, label: `${g.label} (${g.points.length})`, color: g.color })),
    ...curves.map((c) => ({ id: `curve:${c.id}`, label: c.label, color: c.tone === "warn" ? "var(--warn)" : "var(--c-neutral)" }))];
  const hm = hover != null ? marks[hover] : null;
  const range = (vals: number[], f: (v: number) => string) => { const a = Math.min(...vals), b = Math.max(...vals); return a === b ? f(a) : `${f(a)} – ${f(b)}`; };
  const pick = (m: Mark) => { if (m.members.length > 1 && onPickMany) onPickMany(m.members.map((p) => p.id)); else onPick?.(m.members[0].id); };
  return (
    <div className="chart" ref={ref} data-testid={testId}>
      <Legend items={items} hidden={hidden}
        onToggle={(id) => setHidden((h) => { const n = new Set(h); if (n.has(id)) n.delete(id); else n.add(id); return n; })} />
      <svg width={width} height={height} role="img" aria-label={`${groups.map((g) => g.label).join(", ")}: ${xUnit} against ${yUnit}`}
        onMouseLeave={() => setHover(null)}>
        {ticks.map((t) => <g key={t}><line className="gridline" x1={SPAD.l} x2={width - SPAD.r} y1={Y(t)} y2={Y(t)} />
          <text x={SPAD.l - 6} y={Y(t) + 3.5} textAnchor="end">{fmtTick(t)}</text></g>)}
        <text x={4} y={12} textAnchor="start" style={{ fontWeight: 650 }} data-testid="scatter-y-title">{yUnit}</text>
        <line className="axis-line" x1={SPAD.l} x2={width - SPAD.r} y1={SPAD.t + ih} y2={SPAD.t + ih} />
        {xt.map((v) => <text key={v} x={X(v)} y={SPAD.t + ih + 16} textAnchor={v === 0 ? "start" : v === 1 ? "end" : "middle"}>{Math.round(v * 100)}%</text>)}
        <text x={width - SPAD.r} y={height - 4} textAnchor="end" className="faint" data-testid="scatter-x-title">{xUnit} →</text>
        {curves.map((c) => hidden.has(`curve:${c.id}`) ? null :
          <path key={c.id} d={curveD(c)} fill="none" stroke={c.tone === "warn" ? "var(--warn)" : "var(--c-neutral)"} strokeWidth={1.4}
            strokeDasharray="6 4" />)}
        {marks.map((m, i) => {
          const g = groups[m.g], on = hover === i, many = m.members.length > 1;
          return <circle key={`${g.id}:${m.members[0].id}:${i}`} cx={m.px} cy={m.py} r={m.r + (on ? 2 : 0)} fill={g.color} stroke="none"
            fillOpacity={g.ring ? 0.95 : many ? 0.6 : 0.75} style={{ cursor: onPick || onPickMany ? "pointer" : undefined }}
            data-point={many ? undefined : m.members[0].id} data-cluster={many ? m.members.length : undefined}
            onMouseEnter={() => setHover(i)} onClick={() => pick(m)} />;
        })}
      </svg>
      {hm && (
        <Tip x={Math.min(Math.max(hm.px, 90), width - 90)} y={Math.max(SPAD.t, hm.py - 64)}>
          {hm.members.length > 1 ? <>
            <div className="t">{hm.members.length} strategies here</div>
            <div><span className="sw" style={{ background: groups[hm.g].color }} />{groups[hm.g].label}</div>
            <div>{xUnit}: <b>{range(hm.members.map((p) => p.x * 100), (v) => `${v.toFixed(1)}%`)}</b> · {yUnit}: <b>{range(hm.members.map((p) => p.y), (v) => v.toFixed(2))}</b></div>
            <div className="faint">click to list them</div>
          </> : <>
            <div className="t">{hm.members[0].label}</div>
            <div><span className="sw" style={{ background: groups[hm.g].color }} />{groups[hm.g].label}</div>
            <div>{xUnit}: <b>{(hm.members[0].x * 100).toFixed(1)}%</b> · {yUnit}: <b>{hm.members[0].y.toFixed(2)}{hm.members[0].y > t1 ? " (above the chart, pinned to the top)" : ""}</b></div>
            {hm.members[0].detail && <div className="faint">{hm.members[0].detail}</div>}
          </>}
        </Tip>)}
    </div>
  );
}
