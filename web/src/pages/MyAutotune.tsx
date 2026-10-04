/* Strategy autotuner (ADR-97): 10,000 reasoned settings combinations of My strategy on the discovery period, the run
   (start / stop / resume, CPU cores), the overview scatter with free axes, the best combinations for prop payouts and a
   combination's panel (its changes from test 37 with the reasons, numbers, prop results, re-run with trades + charts). */
import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, viewCache } from "../api/client";
import { my } from "../api/my";
import type { AutotuneDetail, AutotunePoint, AutotunePoints, AutotuneStatus, MyJob } from "../api/my";
import { useApi, useApp } from "../app/context";
import { go, href } from "../app/router";
import { profileLabel } from "../app/labels";
import { useMoney } from "../app/money";
import { BarChart, XYScatter } from "../components/charts";
import type { ScatterGroup, XYAxis } from "../components/charts";
import { Badge, Banner, Button, Card, Checkbox, Drawer, Empty, ErrorPanel, Field, Kpi, NumberInput, PageSkeleton, Select, TableWrap,
  TechDetails, n, pct, r, signCls } from "../components/ui";
import { JobLine, PageHead, useJob } from "./MyStrategy";

type MetricKey = "win_rate" | "trades_per_week" | "net_r" | "expectancy_r" | "profit_factor" | "max_drawdown_r" | "months_losing"
  | "avg_planned_rr" | "prop_payouts" | "net_usd" | "trade_count";
const METRICS: Record<MetricKey, { label: string; fmt: (v: number) => string }> = {
  win_rate: { label: "Win rate", fmt: (v) => `${(v * 100).toFixed(0)}%` },
  trades_per_week: { label: "Trades per week", fmt: (v) => v.toFixed(1) },
  net_r: { label: "Net R", fmt: (v) => v.toFixed(0) },
  expectancy_r: { label: "R per trade", fmt: (v) => v.toFixed(2) },
  profit_factor: { label: "Profit factor", fmt: (v) => v.toFixed(2) },
  max_drawdown_r: { label: "Worst drawdown (R)", fmt: (v) => v.toFixed(0) },
  months_losing: { label: "Losing months", fmt: (v) => v.toFixed(0) },
  avg_planned_rr: { label: "Planned reward : risk", fmt: (v) => v.toFixed(2) },
  prop_payouts: { label: "Prop payouts", fmt: (v) => v.toFixed(0) },
  net_usd: { label: "Net profit ($)", fmt: (v) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(0)}k` : v.toFixed(0)) },
  trade_count: { label: "Trades", fmt: (v) => v.toFixed(0) },
};
const STAGE: Record<string, string> = { base: "Test 37 itself", single: "1 change", pair: "2 changes", triple: "3 changes", quad: "4 changes" };

/** ADR-99: "meet your goals" = every rule that is switched on; saved in the workspace (Settings preferences), so it survives
    restarts and updates. Display only: no backtest reads it. */
export interface GoalRule { on: boolean; value?: number }
export type Goals = Record<"win_rate" | "trades_per_week" | "losing_months" | "profit" | "rr" | "prop", GoalRule>;
export const GOAL_DEFAULTS: Goals = { win_rate: { on: false, value: 70 }, trades_per_week: { on: true, value: 3 },
  losing_months: { on: false, value: 4 }, profit: { on: true }, rr: { on: true, value: 1 }, prop: { on: true, value: 1 } };
const load = <T,>(k: string, d: T): T => { try { const v = localStorage.getItem(k); return v ? { ...d, ...JSON.parse(v) } : d; } catch { return d; } };
const save = (k: string, v: unknown) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* per-viewer convenience only */ } };

/** True when the combination meets EVERY rule that is on. A missing number (e.g. no trades) never meets a rule. */
export function isGood(p: AutotunePoint, g: Goals): boolean {
  const v = (rule: GoalRule) => rule.value ?? 0;
  if (g.win_rate.on && !(p.win_rate != null && p.win_rate * 100 >= v(g.win_rate) - 1e-9)) return false;
  if (g.trades_per_week.on && !(p.trades_per_week != null && p.trades_per_week >= v(g.trades_per_week) - 1e-9)) return false;
  if (g.losing_months.on && !(p.months_losing != null && p.months_losing <= v(g.losing_months))) return false;
  if (g.profit.on && !(p.net_r != null && p.net_r > 0)) return false;
  if (g.rr.on && !(p.avg_planned_rr != null && p.avg_planned_rr >= v(g.rr) - 1e-9)) return false;
  if (g.prop.on && !(p.prop_evaluation === "PASS" && (p.prop_payouts ?? 0) >= v(g.prop))) return false;
  return true;
}

export function MyAutotunePage() {
  const { data: st, error, reload } = useApi<AutotuneStatus>(my.autotuneUrl);
  const { data: pts, reload: reloadPts } = useApi<AutotunePoints>(st ? my.autotunePointsUrl(st.criteria_profile) : null, [st?.criteria_profile]);
  const [cores, setCores] = useState<number | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const [many, setMany] = useState<number[] | null>(null);
  const running = !!st?.run.running;
  useEffect(() => {                                       // live while running: status every 5 s, the scatter every 30 s
    if (!running) return;
    const a = window.setInterval(() => reload(), 5000);
    const b = window.setInterval(() => reloadPts(), 30000);
    return () => { window.clearInterval(a); window.clearInterval(b); };
  }, [running]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (st && !running) reloadPts(); }, [running]); // eslint-disable-line react-hooks/exhaustive-deps
  const start = async () => {
    setBusy(true); setErr(null);
    try { await my.autotuneStart(cores ?? st?.processes_default ?? 1); reload(); } catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  };
  const stop = async () => { setBusy(true); try { await my.autotuneStop(); reload(); } catch (e) { setErr(e as ApiError); } finally { setBusy(false); } };
  if (error) return <div className="page"><PageHead title="Strategy autotuner" /><ErrorPanel error={error} /></div>;
  if (!st) return <div className="page"><PageHead title="Strategy autotuner" /><PageSkeleton layout="overview" label="Loading the autotuner" /></div>;
  const d = st.design, p = st.protocol, run = st.run;
  const total = d.total, done = st.done;
  const coreOpts = Array.from({ length: Math.max(1, st.cpu_count) }, (_, i) => String(i + 1));
  return (
    <div className="page" data-testid="my-autotune-page">
      <PageHead title="Strategy autotuner" />
      {!p.ready && <Banner tone="warn">{p.problem}</Banner>}
      {p.ready && p.config_ok === false && <Banner tone="error">The research settings differ from the protocol's. Restore them under
        Run backtest → Research runs before running the autotuner.</Banner>}
      {(st.set_aside ?? 0) > 0 && <Banner tone="info" testId="at-set-aside">{st.set_aside} combination result{st.set_aside === 1 ? " was" : "s were"} computed
        with the earlier rule code, which reused a cached value for the wrong gap. They are kept in a backup file but not shown, and run again with the
        corrected rules (same tries, not counted twice).</Banner>}
      <div className="kpis" data-testid="at-kpis">
        <Kpi label="Combinations tested" value={`${done.toLocaleString()} of ${total.toLocaleString()}`} meter={done / (total || 1)}
          sub={st.failed ? `${st.failed} failed (they run again on the next start)` : "discovery period only"} accent />
        <Kpi label="Time per combination" value={st.median_seconds == null ? "–" : `${Math.round(st.median_seconds)} s`}
          sub={`one core; ${run.processes ?? cores ?? st.processes_default} run side by side`} />
        <Kpi label="Tries used (autotuner protocol)" value={`${(p.trials_used ?? 0).toLocaleString()} of ${(p.trial_budget ?? total).toLocaleString()}`}
          sub={`${p.holdout_looks ?? 1} holdout look, not used here`} />
      </div>
      <Card title="Run" testId="at-run" actions={<div className="inline">
        {!running && <Field label="CPU cores"><Select value={String(cores ?? st.processes_default)} onChange={(v) => setCores(Number(v))}
          options={coreOpts} ariaLabel="CPU cores" testId="at-cores" /></Field>}
        {running ? <Button onClick={stop} busy={busy || run.stopping} busyLabel="Stopping…" testId="at-stop">Stop</Button>
          : <Button kind="primary" onClick={start} busy={busy} busyLabel="Starting…" disabled={!p.ready || done >= total} testId="at-start">
            {done ? "Continue" : "Start"}</Button>}</div>}>
        <p className="muted">Every combination is a full backtest of the discovery period
          {p.discovery ? ` (${new Date(p.discovery.start).toLocaleDateString()} – ${new Date(p.discovery.end).toLocaleDateString()})` : ""} through the same
          engine as My strategy, with the lookahead check, BID/ASK costs, MNQ sizing and the prop check. Stop keeps every finished combination; Continue
          runs the rest. The holdout stays locked.</p>
        {running && <Banner tone="info"><span className="spinner" /> {run.step}{run.done_now ? ` · ${run.done_now} finished in this run` : ""}
          {run.memory_note ? ` · ${run.memory_note}` : ""}</Banner>}
        {!running && run.error && <Banner tone="error">{run.error.message}</Banner>}
        {!running && run.step === "Finished" && done >= total && <Banner tone="ok">All {total.toLocaleString()} combinations are tested.</Banner>}
        <ErrorPanel error={err} />
      </Card>
      <ResultsSection st={st} pts={pts} onOpen={setOpen} onMany={setMany} />
      <DesignCard st={st} />
      {pts && pts.failed.length > 0 && <Card title={`Failed combinations (${st.failed})`} testId="at-failed">
        <TableWrap className="my-report-scroll"><table className="dense"><thead><tr><th className="num">#</th><th>Changes</th><th>Why it failed</th></tr></thead>
          <tbody>{pts.failed.map((f) => <tr key={f.n}><td className="num">{f.n}</td><td>{f.label}</td><td className="small">{f.error.message}</td></tr>)}</tbody></table></TableWrap>
      </Card>}
      <Drawer open={many !== null} onClose={() => setMany(null)} title={`${many?.length ?? 0} combinations here`} testId="at-many">
        {many && pts && <ComboTable rows={pts.points.filter((x) => many.includes(x.n))} onOpen={(k) => { setMany(null); setOpen(k); }} />}
      </Drawer>
      <Drawer open={open !== null} onClose={() => setOpen(null)} title={open !== null ? `Combination #${open}` : ""} testId="at-detail">
        {open !== null && <ComboPanel key={open} n={open} profile={st.criteria_profile} />}
      </Drawer>
    </div>
  );
}

function ResultsSection({ st, pts, onOpen, onMany }: { st: AutotuneStatus; pts: AutotunePoints | null; onOpen: (n: number) => void;
  onMany: (ns: number[]) => void }) {
  const [axes, setAxes] = useState(() => load("my-autotune-axes", { x: "win_rate" as MetricKey, y: "trades_per_week" as MetricKey }));
  const { prefs, setPref } = useApp();
  const saved = (prefs as { autotune_goals?: Goals }).autotune_goals;
  const [goals, setGoalsState] = useState<Goals>(() => ({ ...GOAL_DEFAULTS, ...(saved ?? {}) }));
  const edited = useRef(false);                       // after the first edit, the page's own goals are the truth
  useEffect(() => { if (saved && !edited.current) setGoalsState({ ...GOAL_DEFAULTS, ...saved }); }, [JSON.stringify(saved)]); // eslint-disable-line react-hooks/exhaustive-deps
  const timer = useRef<number | undefined>(undefined);
  const setGoals = (g: Goals) => {                    // saved in the workspace once typing pauses
    edited.current = true;
    setGoalsState(g);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => { void setPref({ autotune_goals: g }); }, 500);
  };
  const rule = (k: keyof Goals, patch: Partial<GoalRule>) => setGoals({ ...goals, [k]: { ...goals[k], ...patch } });
  useEffect(() => save("my-autotune-axes", axes), [axes]);
  const points = pts?.points ?? [];
  const good = useMemo(() => new Set(points.filter((p) => isGood(p, goals)).map((p) => p.n)), [points, goals]);
  if (!pts) return <PageSkeleton layout="overview" label="Reading the results" />;
  if (!points.length) return <Card title="Results"><Empty>No combination is finished yet. Start the run above.</Empty></Card>;
  const val = (p: AutotunePoint, k: MetricKey) => p[k] as number | null;
  const drawn = points.filter((p) => val(p, axes.x) != null && val(p, axes.y) != null && Number.isFinite(val(p, axes.x)!) && Number.isFinite(val(p, axes.y)!));
  const toPt = (p: AutotunePoint) => ({ id: String(p.n), x: val(p, axes.x)!, y: val(p, axes.y)!, label: `#${p.n} ${p.label}`,
    detail: `${n(p.trades_per_week, 1)} / week · ${pct(p.win_rate, 0)} wins · ${r(p.net_r, 1)} net · ${p.prop_evaluation ?? "–"}, ${p.prop_payouts ?? 0} payouts` });
  const groups: ScatterGroup[] = [
    { id: "other", label: "Combinations", color: "var(--c-strategy)", size: 3.5, cluster: true,
      points: drawn.filter((p) => !good.has(p.n) && p.stage !== "base").map(toPt) },
    { id: "good", label: "Meet your goals", color: "var(--c-survivor)", size: 5, ring: true,
      points: drawn.filter((p) => good.has(p.n) && p.stage !== "base").map(toPt) },
    { id: "base", label: "Test 37 (the base)", color: "var(--text)", size: 7, ring: true, points: drawn.filter((p) => p.stage === "base").map(toPt) },
  ];
  const refOf = (k: MetricKey): XYAxis["ref"] => (
    k === "trades_per_week" && goals.trades_per_week.on ? { value: goals.trades_per_week.value ?? 0, label: `${goals.trades_per_week.value} / week` }
    : k === "win_rate" && goals.win_rate.on ? { value: (goals.win_rate.value ?? 0) / 100, label: `${goals.win_rate.value}%` }
    : k === "months_losing" && goals.losing_months.on ? { value: goals.losing_months.value ?? 0, label: `${goals.losing_months.value} losing` }
    : k === "avg_planned_rr" && goals.rr.on ? { value: goals.rr.value ?? 0, label: `${goals.rr.value} : 1` }
    : k === "prop_payouts" && goals.prop.on ? { value: goals.prop.value ?? 0, label: `${goals.prop.value} payout${goals.prop.value === 1 ? "" : "s"}` }
    : k === "net_r" ? { value: 0, label: "break-even" } : undefined);
  const ax = (k: MetricKey): XYAxis => ({ label: METRICS[k].label, fmt: METRICS[k].fmt, ref: refOf(k) });
  const top = points.filter((p) => good.has(p.n)).sort((a, b) => (b.prop_payouts ?? 0) - (a.prop_payouts ?? 0)
    || (b.prop_trader_payout ?? 0) - (a.prop_trader_payout ?? 0) || (b.win_rate ?? 0) - (a.win_rate ?? 0)).slice(0, 50);
  const base = points.find((p) => p.stage === "base");
  const opts = (Object.keys(METRICS) as MetricKey[]).map((k) => ({ value: k, label: METRICS[k].label }));
  return (
    <>
      <Card title="Your goals" testId="at-goals">
        <p className="small muted">A combination meets your goals only when it passes EVERY rule that is ticked. Saved in this workspace.</p>
        <div className="at-goals">
          <GoalRow label="Win rate at least" unit="%" hint="trades that made money after costs" k="win_rate" goals={goals} rule={rule} step={1} />
          <GoalRow label="Trades per week at least" k="trades_per_week" goals={goals} rule={rule} step={0.5} />
          <GoalRow label="Losing months at most" k="losing_months" goals={goals} rule={rule} step={1} integer hint="months with a net loss" />
          <GoalRow label="Profit after costs" k="profit" goals={goals} rule={rule} hint="net R above 0" />
          <GoalRow label="Planned reward : risk at least" k="rr" goals={goals} rule={rule} step={0.1} />
          <GoalRow label="Prop evaluation passed, payouts at least" k="prop" goals={goals} rule={rule} step={1} integer
            hint={`under ${profileLabel(st.criteria_profile)} (Settings → pass-criteria account)`} />
        </div>
        <p className="small muted" data-testid="at-goal-count"><b>{good.size.toLocaleString()}</b> of {points.length.toLocaleString()} tested combinations
          meet every ticked rule{base && good.has(base.n) ? " (one of them is test 37 itself, the white dot)" : ""}. Ranked by payouts.</p>
      </Card>
      <Card title="Every tested combination" testId="at-scatter" actions={<div className="inline">
        <Field label="Across"><Select value={axes.x} onChange={(v) => setAxes({ ...axes, x: v as MetricKey })} options={opts} ariaLabel="Across" testId="at-x" /></Field>
        <Field label="Up"><Select value={axes.y} onChange={(v) => setAxes({ ...axes, y: v as MetricKey })} options={opts} ariaLabel="Up" testId="at-y" /></Field></div>}>
        <XYScatter groups={groups} x={ax(axes.x)} y={ax(axes.y)} testId="at-xy" onPick={(id) => onOpen(Number(id))}
          onPickMany={(ids) => onMany(ids.map(Number))} />
        <p className="small muted">Each dot is one combination's discovery backtest; click one for its changes and numbers. Overlapping dots are grouped.
          {points.length - drawn.length > 0 ? ` ${points.length - drawn.length} combinations have no value on these axes (e.g. no trades) and are not drawn.` : ""}
          {" "}With {points.length.toLocaleString()} combinations tried, the best-looking ones are partly luck: only the holdout can confirm one.</p>
      </Card>
      <Card title={top.length ? `Best for prop payouts (${top.length} of ${good.size} shown)` : "Best for prop payouts"} testId="at-top">
        {base && <p className="small muted">Test 37 itself: {n(base.trades_per_week, 2)} trades / week, {pct(base.win_rate)} wins, {r(base.net_r, 1)} net,
          {" "}{base.prop_evaluation ?? "–"} with {base.prop_payouts ?? 0} payouts.</p>}
        {!top.length ? <Empty>No tested combination meets your goals yet.</Empty> : <ComboTable rows={top} onOpen={onOpen} />}
      </Card>
    </>
  );
}

function ComboTable({ rows, onOpen }: { rows: AutotunePoint[]; onOpen: (n: number) => void }) {
  return (
    <TableWrap className="my-setup-scroll"><table className="dense" data-testid="at-table"><thead><tr><th className="num">#</th><th>Changes from test 37</th>
      <th className="num">Per week</th><th className="num">Win rate</th><th className="num">R : R</th><th className="num">Net R</th>
      <th className="num">Losing months</th><th className="num">Drawdown R</th><th>Evaluation</th><th className="num">Payouts</th></tr></thead>
      <tbody>{rows.map((p) => (
        <tr key={p.n} onClick={() => onOpen(p.n)} style={{ cursor: "pointer" }}>
          <td className="num">{p.n}</td><td>{p.label}</td><td className="num">{n(p.trades_per_week, 2)}</td><td className="num">{pct(p.win_rate)}</td>
          <td className="num">{n(p.avg_planned_rr, 2)}</td><td className={`num ${signCls(p.net_r)}`}>{r(p.net_r, 1)}</td>
          <td className="num">{p.months_losing ?? "–"} / {p.months_total ?? "–"}</td><td className="num">{n(p.max_drawdown_r, 1)}</td>
          <td>{p.prop_evaluation === "PASS" ? <Badge tone="ok">Pass</Badge> : <Badge>{p.prop_evaluation ?? "–"}</Badge>}</td>
          <td className="num">{p.prop_payouts ?? 0}</td></tr>))}</tbody></table></TableWrap>
  );
}

function ComboPanel({ n: num, profile }: { n: number; profile: string | null }) {
  const { data, error, reload } = useApi<AutotuneDetail>(my.autotuneComboUrl(num), [num]);
  const money = useMoney();
  const [err, setErr] = useState<ApiError | null>(null);
  const [job, setJob] = useJob((j: MyJob) => { viewCache.clear(); reload(); if (j.state === "completed") setErr(null); });
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <PageSkeleton layout="overview" label="Loading the combination" />;
  const res = data.result, m = res?.metrics;
  const rerun = async () => { setErr(null); try { setJob(await my.autotuneRerun(num)); } catch (e) { setErr(e as ApiError); } };
  const pr = profile ? res?.prop?.[profile] : undefined;
  const done = (job?.result as { id?: string } | null)?.id;
  return (
    <div data-testid="at-panel">
      <p><b>{data.row.label}</b> <span className="muted small">· {STAGE[data.row.stage] ?? data.row.stage}</span></p>
      {!res ? <Banner tone="info">Not tested yet.</Banner> : res.error ? <Banner tone="error">{res.error.message}</Banner> : m && <>
        <div className="kpis">
          <Kpi label="Trades per week" value={n(m.trades_per_week, 2)} sub={`${m.trade_count} trades`} />
          <Kpi label="Win rate" value={pct(m.win_rate)} sub={`planned R : R ${n(m.avg_planned_rr, 2)}`} accent />
          <Kpi label="Net" value={r(m.net_r, 1)} tone={signCls(m.net_r) as "pos" | "neg" | ""} sub={money.fmt(m.net_usd)} />
          <Kpi label="Losing months" value={`${m.months_losing ?? 0} of ${m.months_total ?? 0}`} tone={(m.months_losing ?? 0) > 0 ? "neg" : "pos"}
            sub={`worst drawdown ${n(m.max_drawdown_r, 1)} R`} />
          <Kpi label={`Prop: ${profileLabel(profile)}`} value={pr?.evaluation ?? "–"} sub={`${pr?.payouts ?? 0} payouts · ${money.fmt(pr?.trader_payout ?? 0)} to you`} />
        </div>
        {res.monthly && res.monthly.length > 0 && <BarChart categories={res.monthly.map((x) => x.month)} series={[{ id: "r", label: "Net R per month",
          values: res.monthly.map((x) => x.net_r) }]} height={170} testId="at-monthly" />}
      </>}
      <h3>Changes from {data.base_label}</h3>
      {!data.row.changes.length ? <p className="muted">None: this is test 37 itself, the reference for every other combination.</p> :
        <TableWrap><table className="dense" data-testid="at-changes"><thead><tr><th>Change</th><th>Why</th><th>Basis</th></tr></thead>
          <tbody>{data.row.changes.map((c) => <tr key={c.option}><td><b>{c.label}</b><div className="muted small">
            {Object.entries(c.changes).map(([k, v]) => `${k} = ${String(v)}`).join(", ")}</div></td><td className="small">{c.reason}</td>
            <td className="small">{c.source}</td></tr>)}</tbody></table></TableWrap>}
      {res?.prop && <><h3>Prop accounts</h3>
        <TableWrap><table className="dense"><thead><tr><th>Account</th><th>Evaluation</th><th className="num">Payouts</th><th className="num">To you</th></tr></thead>
          <tbody>{Object.entries(res.prop).map(([k, v]) => <tr key={k}><td>{profileLabel(k)}</td><td>{v.evaluation ?? v.status}</td>
            <td className="num">{v.payouts ?? 0}</td><td className="num">{money.fmt(v.trader_payout ?? 0)}</td></tr>)}</tbody></table></TableWrap>
        <p className="small muted">Under the default assumed rules; historical result under stated assumptions, not a forecast.</p></>}
      <h3>Trades and charts</h3>
      <p className="small muted">The autotuner keeps the numbers only. Re-running gives this combination as a normal My strategy backtest with every trade and its
        charts (same settings and data, so it is not a new try).</p>
      <div className="inline">
        <Button kind="primary" onClick={rerun} busy={job?.state === "running"} busyLabel="Running…" disabled={!res || !!res.error} testId="at-rerun">
          Re-run with trades and charts</Button>
        {done && <a className="btn btn-secondary" href={href(`/my-trades/${done}`)} data-testid="at-open-trades">Open the trades</a>}
      </div>
      <JobLine job={job} />
      <ErrorPanel error={err} />
      {data.reruns.length > 0 && <ul className="small">{data.reruns.map((b) => <li key={b.id}>
        <a href={href(`/my-trades/${b.id}`)}>Re-run of {new Date(b.created_at).toLocaleString()}</a> · {b.trade_count} trades</li>)}</ul>}
      {res && !res.error && <TechDetails rows={[["Combination", String(num)], ["Settings fingerprint", data.row.settings_hash],
        ["Trades fingerprint", res.trades_hash ?? "–"], ["Lookahead check", res.causality_passed ? "passed" : String(res.causality_passed)],
        ["Run time", `${n(res.duration_s, 0)} s`]]} />}
      <p className="small"><a href="#" onClick={(e: { preventDefault: () => void }) => { e.preventDefault(); go("/my-settings"); }}>My strategy settings</a></p>
    </div>
  );
}

function DesignCard({ st }: { st: AutotuneStatus }) {
  const d = st.design;
  const [theme, setTheme] = useState<string>("all");
  const rows = d.options.filter((o) => theme === "all" || o.theme === theme);
  const label = Object.fromEntries(d.themes.map((t) => [t.id, t.label]));
  return (
    <Card title={`How the ${d.total.toLocaleString()} combinations were chosen`} testId="at-design" actions={
      <Select value={theme} onChange={setTheme} options={[{ value: "all", label: "Every theme" }, ...d.themes.map((t) => ({ value: t.id, label: t.label }))]}
        ariaLabel="Theme" testId="at-theme" />}>
      <p className="small">Every combination is {d.base_label.toLowerCase()} plus 0-4 of the {d.options.length} reasoned changes below, never two from one theme,
        and every change must still matter (e.g. no rejection-block setting while rejection blocks are off). Nothing is drawn at random:
        test 37 itself ({d.counts.base}), every change on its own ({d.counts.single}), pairs ({d.counts.pair.toLocaleString()}: every direction mode,
        including flip, with every other change, then the other pairs with the most Blake-central first), and groups of 3 ({d.counts.triple.toLocaleString()})
        and 4 ({d.counts.quad.toLocaleString()}) changes spread evenly over all theme groups, Blake's own rules about three times as often as threshold
        calibrations. {d.frozen ? `Frozen ${new Date(d.frozen_at as string).toLocaleString()}.` : "Frozen on the first start."}</p>
      <TableWrap className="my-setup-scroll"><table className="dense" data-testid="at-options"><thead><tr><th>Theme</th><th>Change</th><th>Why</th><th>Basis</th></tr></thead>
        <tbody>{rows.map((o) => <tr key={o.id}><td className="small">{label[o.theme] ?? o.theme}</td><td><b>{o.label}</b><div className="muted small">
          {Object.entries(o.changes).map(([k, v]) => `${k} = ${String(v)}`).join(", ")}</div></td><td className="small">{o.reason}</td>
          <td className="small">{o.source}</td></tr>)}</tbody></table></TableWrap>
      <TechDetails rows={[["Design version", String(d.autotune_version)], ["Design fingerprint", d.manifest_hash]]} />
    </Card>
  );
}

function GoalRow({ label, unit, hint, k, goals, rule, step, integer }: { label: string; unit?: string; hint?: string; k: keyof Goals; goals: Goals;
  rule: (k: keyof Goals, patch: Partial<GoalRule>) => void; step?: number; integer?: boolean }) {
  const g = goals[k];
  return (
    <div className={`at-goal${g.on ? " on" : ""}`} data-testid={`at-goal-${k}`}>
      <Checkbox checked={g.on} onChange={(on) => rule(k, { on })} label={label} testId={`at-goal-${k}-on`} />
      {g.value !== undefined && <span className="inline">
        <NumberInput value={g.value} step={step} integer={integer} onChange={(v) => { if (v !== undefined) rule(k, { value: v }); }}
          ariaLabel={label} testId={`at-goal-${k}-value`} />{unit && <span className="muted">{unit}</span>}</span>}
      {hint && <span className="muted small">{hint}</span>}
    </div>
  );
}
