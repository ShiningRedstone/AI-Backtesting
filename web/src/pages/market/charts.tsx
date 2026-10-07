/* Market simulator charts (ADR-110): skill bars with their 95 % range, said-vs-happened calibration, and skill month by
   month. Values are always also written as text; colour is never the only carrier. */
import { LineChart } from "../../components/charts";
import { pct } from "../../components/ui";

export interface SkillRow { id: string; label: string; skill: number | null | undefined; ci?: [number, number] | null; real?: boolean; note?: string }

/** One row per forecast: a bar from 0 to its skill (better than the baseline to the right), the 95 % range as a
    whisker, the 0 line marked. Green = beats the baseline beyond chance, grey = within chance, red = worse. */
export function SkillBars({ rows, testId }: { rows: SkillRow[]; testId?: string }) {
  const vals = rows.flatMap((r) => [r.skill, ...(r.ci ?? [])]).filter((v): v is number => v != null && Number.isFinite(v));
  if (!vals.length) return <div className="empty small">No scores yet.</div>;
  const m = Math.max(0.05, ...vals.map(Math.abs)) * 1.08;
  const X = (v: number) => 50 + (v / m) * 50;                          // percent of the track
  return (
    <div className="mk-skill" data-testid={testId}>
      <div className="mk-skill-axis"><span /><div className="mk-skill-scale"><span>{`−${(m * 100).toFixed(0)} %`}</span>
        <span>0 = as good as the baseline</span><span>{`+${(m * 100).toFixed(0)} %`}</span></div><span /></div>
      {rows.map((r) => {
        const v = r.skill;
        const tone = v == null ? "" : r.real ? "pos" : v < 0 && r.ci && r.ci[1] < 0 ? "neg" : "flat";
        return (
          <div key={r.id} className="mk-skill-row" title={`${r.label}: ${v == null ? "no score" : `${(v * 100).toFixed(1)} %`}${r.ci ? ` (95 % range ${(r.ci[0] * 100).toFixed(1)} … ${(r.ci[1] * 100).toFixed(1)} %)` : ""}`}>
            <div className="mk-skill-label">{r.label}</div>
            <div className="mk-skill-track">
              <span className="mk-skill-zero" />
              {v != null && <span className={`mk-skill-bar ${tone}`} style={{ left: `${Math.min(X(0), X(v))}%`, width: `${Math.abs(X(v) - X(0))}%` }} />}
              {r.ci && <span className="mk-skill-ci" style={{ left: `${X(r.ci[0])}%`, width: `${Math.max(0.5, X(r.ci[1]) - X(r.ci[0]))}%` }} />}
            </div>
            <div className={`mk-skill-value num ${tone === "neg" ? "neg" : tone === "pos" ? "pos" : ""}`}>
              {v == null ? "–" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(1)} %`}{r.note && <span className="faint"> {r.note}</span>}</div>
          </div>);
      })}
    </div>
  );
}

export interface CalPoint { from: number; n: number; said: number; happened: number }
const CAL_COLORS = ["var(--c1)", "var(--c2)", "var(--c3)"];

/** Said vs happened: each dot is a group of predictions with about the same probability; on the diagonal = honest. */
export function CalibrationChart({ series, testId }: { series: { id: string; label: string; points: CalPoint[] }[]; testId?: string }) {
  const pts = series.flatMap((s) => s.points);
  if (!pts.length) return <div className="empty small">Not enough predictions for a calibration chart.</div>;
  const lo = Math.max(0, Math.floor((Math.min(...pts.map((p) => Math.min(p.said, p.happened))) - 0.05) * 20) / 20);
  const hi = Math.min(1, Math.ceil((Math.max(...pts.map((p) => Math.max(p.said, p.happened))) + 0.05) * 20) / 20);
  const S = 220, P = 34;
  const C = (v: number) => P + ((v - lo) / (hi - lo || 1)) * (S - P - 8);
  const Yc = (v: number) => S - P - ((v - lo) / (hi - lo || 1)) * (S - P - 8);
  const nMax = Math.max(...pts.map((p) => p.n));
  const ticks = [lo, (lo + hi) / 2, hi];
  return (
    <div className="mk-cal" data-testid={testId}>
      <svg viewBox={`0 0 ${S} ${S}`} role="img" aria-label="Calibration: said versus happened">
        <line x1={C(lo)} y1={Yc(lo)} x2={C(hi)} y2={Yc(hi)} className="mk-cal-diag" />
        <line x1={P} y1={S - P} x2={S - 8} y2={S - P} className="axis-line" />
        <line x1={P} y1={8} x2={P} y2={S - P} className="axis-line" />
        {ticks.map((t) => <g key={t}><text x={C(t)} y={S - P + 14} textAnchor="middle">{pct(t, 0)}</text>
          <text x={P - 4} y={Yc(t) + 3} textAnchor="end">{pct(t, 0)}</text></g>)}
        <text x={(S + P) / 2} y={S - 4} textAnchor="middle" className="faint">said</text>
        <text x={10} y={(S - P) / 2} textAnchor="middle" className="faint" transform={`rotate(-90 10 ${(S - P) / 2})`}>happened</text>
        {series.map((s, si) => s.points.map((p) => (
          <circle key={`${s.id}${p.from}`} cx={C(p.said)} cy={Yc(p.happened)} r={2.5 + 5 * Math.sqrt(p.n / nMax)} fill={CAL_COLORS[si % 3]} opacity={0.85}>
            <title>{`${s.label}: said ${pct(p.said, 1)}, happened ${pct(p.happened, 1)} (${p.n.toLocaleString()} predictions)`}</title></circle>)))}
      </svg>
      {series.length > 1 && <div className="mk-cal-legend">{series.map((s, i) => <span key={s.id}><i style={{ background: CAL_COLORS[i % 3] }} />{s.label}</span>)}</div>}
    </div>
  );
}

export interface MonthPoint { month: string; skill: number | null; n?: number }

/** Skill (%) month by month for each source; gaps where a source has no month. */
export function MonthlyChart({ sources, testId }: { sources: { id: string; label: string; points: MonthPoint[] }[]; testId?: string }) {
  const months = [...new Set(sources.flatMap((s) => s.points.map((p) => p.month)))].sort();
  if (!months.length) return <div className="empty small">No monthly scores yet.</div>;
  const colors = ["var(--c1)", "var(--c2)", "var(--c3)"];
  return (
    <LineChart x={months} unit="%" testId={testId} fmtX={(s) => s}
      series={sources.map((s, i) => {
        const m = new Map(s.points.map((p) => [p.month, p.skill]));
        return { id: s.id, label: s.label, color: colors[i % 3], values: months.map((mo) => { const v = m.get(mo); return v == null ? null : v * 100; }) };
      })} />
  );
}

/** Horizontal 0-100 % bars, several values per row (e.g. traded within 2 h and reacted), each with its number. */
export function PctBars({ rows, series, testId }: { rows: { id: string; label: string; values: (number | null)[]; note?: string }[];
  series: { label: string; color: string }[]; testId?: string }) {
  return (
    <div className="mk-pct" data-testid={testId}>
      <div className="mk-cal-legend">{series.map((s) => <span key={s.label}><i style={{ background: s.color }} />{s.label}</span>)}</div>
      {rows.map((r) => (
        <div key={r.id} className="mk-pct-row" title={r.note}>
          <div className="mk-skill-label">{r.label}{r.note && <span className="faint"> · {r.note}</span>}</div>
          <div className="mk-pct-bars">{r.values.map((v, i) => (
            <div key={i} className="mk-pct-line"><div className="mk-pct-track"><span style={{ width: `${Math.max(0, Math.min(100, (v ?? 0) * 100))}%`,
              background: series[i]?.color }} /></div><span className="num">{v == null ? "–" : pct(v, 0)}</span></div>))}</div>
        </div>))}
    </div>
  );
}
