/* Strategy autotuner (ADR-101): a step-by-step optimiser of My strategy. Starts from one of the user's backtests, tries
   one-setting tweaks without the lookahead check (numbers only), keeps a tweak only when it improves both the first 70 %
   and the last 30 % of the discovery period, checks every new best for lookahead and backtests the final best normally. */
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiError } from "../api/client";
import { my } from "../api/my";
import type { ChallengeSummary, ChallengeView, OptBest, OptLive, OptRun, OptStatus, OptTry, PartScore } from "../api/my";
import { useApi, useApp } from "../app/context";
import { go, href, useRoute } from "../app/router";
import { useMoney } from "../app/money";
import { XYScatter } from "../components/charts";
import type { ScatterGroup } from "../components/charts";
import { Badge, Banner, Button, Card, Checkbox, Empty, ErrorPanel, Field, Kpi, NumberInput, PageSkeleton, Select, TableWrap,
  TechDetails, n, pct, r, signCls } from "../components/ui";
import { ChainLine, JobLine, PageHead, useJob } from "./MyStrategy";

/** The goals (ADR-99) are workspace preferences; the autotuner scores with them (goals first, then prop net). */
export interface GoalRule { on: boolean; value?: number }
export type Goals = Record<"win_rate" | "trades_per_week" | "losing_months" | "profit" | "rr" | "prop", GoalRule>;
export const GOAL_DEFAULTS: Goals = { win_rate: { on: false, value: 70 }, trades_per_week: { on: true, value: 3 },
  losing_months: { on: false, value: 4 }, profit: { on: true }, rr: { on: true, value: 1 }, prop: { on: true, value: 1 } };
const GOAL_LABEL: Record<string, string> = { win_rate: "Win rate", trades_per_week: "Trades per week", losing_months: "Losing months",
  profit: "Profit after costs", rr: "Planned reward : risk", prop: "Prop pass + payouts" };

const STOP_REASON: Record<string, string> = {
  no_improvement: "No tweak of the best settings improved the first 70 % any more.",
  check_stopped_improving: "Tweaks still improved the first 70 %, but none of them also improved the last 30 %: more tweaking would only fit noise.",
  no_tweaks_left: "Every tweak of the best settings was already tried.",
  limit: "This run's try limit was reached.",
  budget: "The autotuner's tries are used up.",
  stopped: "You stopped it.",
};

const fmtVal = (v: unknown) => (typeof v === "boolean" ? (v ? "on" : "off") : v === null || v === undefined ? "–" : String(v));

export function MyAutotunePage() {
  const route = useRoute();
  const { data: st, error, reload } = useApi<OptStatus>(my.autotuneUrl);
  const running = !!st?.run.running;
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => reload(), 4000);
    return () => window.clearInterval(t);
  }, [running]); // eslint-disable-line react-hooks/exhaustive-deps
  const runId = route.query.get("run") ?? st?.run.run_id ?? st?.runs[0]?.id ?? null;
  if (error) return <div className="page"><PageHead title="Strategy autotuner" /><ErrorPanel error={error} /></div>;
  if (!st) return <div className="page"><PageHead title="Strategy autotuner" /><PageSkeleton layout="overview" label="Loading the autotuner" /></div>;
  const p = st.protocol;
  return (
    <div className="page" data-testid="my-autotune-page">
      <PageHead title="Strategy autotuner" />
      {!p.ready && <Banner tone="warn">{p.problem}</Banner>}
      {p.ready && p.config_ok === false && <Banner tone="error">The research settings differ from the protocol's. Restore them under
        Run backtest → Research runs before running the autotuner.</Banner>}
      <div className="kpis" data-testid="at-kpis">
        <Kpi label="Tries used (autotuner protocol)" value={`${(p.trials_used ?? 0).toLocaleString()} of ${(p.trial_budget ?? 5000).toLocaleString()}`}
          meter={(p.trials_used ?? 0) / (p.trial_budget || 1)} sub={`a settings combination tried again is never a new try · ${p.holdout_looks ?? 1} holdout look`} accent />
        <Kpi label="Runs" value={st.runs.length} sub={st.runs[0] ? `last: ${statusWord(st.runs[0].status)}` : "none yet"} />
        <Kpi label="Scored by" value="Your goals" sub={`then prop net (payouts − fees) on ${st.context?.profile_name ?? "the pass-criteria account"}`} />
      </div>
      <HowCard st={st} />
      <GoalsCard st={st} />
      <StartCard st={st} onChange={reload} />
      <RunsCard st={st} selected={runId} />
      {runId && <RunView key={runId} id={runId} live={st.run.run_id === runId ? st.run : null} onChanged={reload} />}
    </div>
  );
}

const statusWord = (s: string) => ({ running: "running", finished: "finished", stopped: "stopped", failed: "failed" } as Record<string, string>)[s] ?? s;

function HowCard({ st }: { st: OptStatus }) {
  const d = st.protocol.discovery;
  return (
    <Card title="How it works" testId="at-how">
      <ol className="small at-how">
        <li>Pick one of your backtests. Its settings are the starting point (position size and the flip switch are never changed).</li>
        <li>The autotuner tries one change at a time: a switch flipped, another choice, a number one step up or down, a time 15 minutes earlier or later.
          Settings that cannot change any trade with the current settings are skipped.</li>
        <li>Every try is a backtest of the discovery period{d ? ` (${new Date(d.start).toLocaleDateString()} – ${new Date(d.end).toLocaleDateString()})` : ""} through
          the same engine, BID/ASK costs and MNQ sizing, but without the slow lookahead check and without trade records or candles: numbers only.</li>
        <li>The period is split by trading days: the first {Math.round(st.train_share * 100)} % chooses, the last {100 - Math.round(st.train_share * 100)} % checks.
          A change is kept only when it is better on BOTH parts. Better = more of your goals met, then closer to the unmet ones, then more prop
          net (payouts − every challenge fee), then more net R.</li>
        <li>It keeps going from the new best until no change improves the last {100 - Math.round(st.train_share * 100)} % any more, or the try limit.</li>
        <li>Every new best gets the full lookahead check in parallel; one that fails is thrown away with everything built on it. The final best
          is backtested normally (trade records, candles, lookahead check) and appears in your Backtest list. The holdout stays locked.</li>
      </ol>
    </Card>
  );
}

function GoalsCard({ st }: { st: OptStatus }) {
  const { prefs, setPref } = useApp();
  const saved = (prefs as { autotune_goals?: Goals }).autotune_goals;
  const [goals, setGoalsState] = useState<Goals>(() => ({ ...GOAL_DEFAULTS, ...(saved ?? {}) }));
  const edited = useRef(false);
  useEffect(() => { if (saved && !edited.current) setGoalsState({ ...GOAL_DEFAULTS, ...saved }); }, [JSON.stringify(saved)]); // eslint-disable-line react-hooks/exhaustive-deps
  const timer = useRef<number | undefined>(undefined);
  const setGoals = (g: Goals) => {
    edited.current = true;
    setGoalsState(g);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => { void setPref({ autotune_goals: g }); }, 500);
  };
  const rule = (k: keyof Goals, patch: Partial<GoalRule>) => setGoals({ ...goals, [k]: { ...goals[k], ...patch } });
  const ctx = st.context;
  const fees = ctx?.fees ?? {};
  return (
    <Card title="Your goals" testId="at-goals">
      <p className="small muted">The autotuner aims for these first. Saved in this workspace; a run uses the goals and fees from when it started.
        Losing months and payouts are counted for the whole discovery period and scaled to each part's share of the trading days.</p>
      <div className="at-goals">
        <GoalRow label="Win rate at least" unit="%" hint="trades that made money after costs" k="win_rate" goals={goals} rule={rule} step={1} />
        <GoalRow label="Trades per week at least" k="trades_per_week" goals={goals} rule={rule} step={0.5} />
        <GoalRow label="Losing months at most" k="losing_months" goals={goals} rule={rule} step={1} integer hint="months with a net loss" />
        <GoalRow label="Profit after costs" k="profit" goals={goals} rule={rule} hint="net R above 0" />
        <GoalRow label="Planned reward : risk at least" k="rr" goals={goals} rule={rule} step={0.1} />
        <GoalRow label="A passed prop challenge and payouts at least" k="prop" goals={goals} rule={rule} step={1} integer
          hint={`challenge after challenge on ${ctx?.profile_name ?? "the pass-criteria account"}`} />
      </div>
      {st.context_problem ? <Banner tone="warn" testId="at-fees-missing">{st.context_problem.message} <a href={href("/settings")}>Open Settings</a></Banner>
        : <p className="small muted" data-testid="at-fees">Prop account: <b>{ctx?.profile_name}</b> (Settings → pass-criteria account) · evaluation{" "}
          {money0(fees.eval_price)} · reset {fees.reset_fee == null ? "= evaluation price" : money0(fees.reset_fee)} · activation{" "}
          {fees.activation_fee == null ? "none" : money0(fees.activation_fee)} (Settings → prop account fees, discount applied).</p>}
    </Card>
  );
}
const money0 = (v: number | null | undefined) => (v == null ? "–" : `$${Math.round(v).toLocaleString()}`);

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

function chainOf(v: ChallengeView | null | undefined): ChallengeSummary | null {
  const p = v?.profiles ? Object.values(v.profiles)[0] : null;
  return (p as ChallengeSummary | null) ?? null;
}

function StartCard({ st, onChange }: { st: OptStatus; onChange: () => void }) {
  const money = useMoney();
  const run = st.run;
  const running = !!run.running;
  const [pick, setPick] = useState<string | null>(null);
  const [cores, setCores] = useState<number | null>(null);
  const [maxTries, setMaxTries] = useState<number>(st.default_max_tries);
  const [favOnly, setFavOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const rows = favOnly ? st.starts.filter((b) => b.favorite) : st.starts;
  const chosen = pick ?? st.starts[0]?.id ?? null;
  const start = async () => {
    if (!chosen) return;
    setBusy(true); setErr(null);
    try {
      const res = await my.autotuneStart({ start_id: chosen, processes: cores ?? st.processes_default, max_tries: maxTries });
      onChange();
      go(res.run_id ? `/my-autotune?run=${res.run_id}` : "/my-autotune");
    } catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  };
  const stop = async () => { setBusy(true); try { await my.autotuneStop(); onChange(); } catch (e) { setErr(e as ApiError); } finally { setBusy(false); } };
  const coreOpts = Array.from({ length: Math.max(1, st.cpu_count) }, (_, i) => String(i + 1));
  return (
    <Card title="Start a run" testId="at-run" actions={<div className="inline">
      {!running && <Field label="CPU cores"><Select value={String(cores ?? st.processes_default)} onChange={(v) => setCores(Number(v))}
        options={coreOpts} ariaLabel="CPU cores" testId="at-cores" /></Field>}
      {!running && <Field label="Most tries this run"><NumberInput value={maxTries} integer step={100}
        onChange={(v) => setMaxTries(Math.max(1, Math.min(5000, v ?? st.default_max_tries)))} ariaLabel="Most tries this run" testId="at-max-tries" /></Field>}
      {running ? <Button onClick={stop} busy={busy || run.stopping} busyLabel="Stopping…" testId="at-stop">Stop</Button>
        : <Button kind="primary" onClick={start} busy={busy} busyLabel="Starting…" testId="at-start"
          disabled={!st.protocol.ready || !chosen || !!st.context_problem}>Start autotuning</Button>}</div>}>
      {running && <LiveLine live={run} />}
      {!running && run.error && <Banner tone="error" testId="at-run-error">{run.error.message}</Banner>}
      <ErrorPanel error={err} />
      {!running && <>
        <div className="inline" style={{ justifyContent: "space-between" }}>
          <p className="small muted" style={{ margin: 0 }}>Start from (your discovery backtests, favourites first):</p>
          <Checkbox checked={favOnly} onChange={setFavOnly} label="Favourites only" testId="at-fav-only" />
        </div>
        {!rows.length ? <Empty>{favOnly ? "No favourite backtest. Star one in the Backtest tab." : "No backtest yet. Run one in the Backtest tab first."}</Empty> : (
          <TableWrap className="my-report-scroll" testId="at-starts"><table className="dense">
            <thead><tr><th style={{ width: 28 }} /><th>★</th><th>Name</th><th>When</th><th className="num">Trades</th><th className="num">Per week</th>
              <th className="num">Win rate</th><th className="num">Net R</th><th className="num">Losing months</th><th>Prop challenges</th></tr></thead>
            <tbody>{rows.map((b) => {
              const c = chainOf(b.challenge);
              return (
                <tr key={b.id} className={b.id === chosen ? "selected" : ""} onClick={() => setPick(b.id)} style={{ cursor: "pointer" }}>
                  <td><input type="radio" name="at-start" checked={b.id === chosen} onChange={() => setPick(b.id)} aria-label={`Start from ${b.label || b.id}`}
                    data-testid={`at-start-${b.id}`} /></td>
                  <td>{b.favorite ? <span className="at-star on" aria-label="favourite">★</span> : ""}</td>
                  <td>{b.label || "–"}</td><td>{new Date(b.created_at).toLocaleString()}</td>
                  <td className="num">{b.trade_count}</td><td className="num">{n(b.metrics.trades_per_week, 2)}</td>
                  <td className="num">{pct(b.metrics.win_rate)}</td><td className={`num ${signCls(b.metrics.net_r)}`}>{r(b.metrics.net_r, 1)}</td>
                  <td className="num">{b.metrics.months_losing ?? 0} / {b.metrics.months_total ?? 0}</td>
                  <td>{c ? <ChainLine c={c} money={money.fmt} /> : <span className="muted small">run it again to see</span>}</td>
                </tr>);
            })}</tbody></table></TableWrap>)}
      </>}
    </Card>
  );
}

function LiveLine({ live }: { live: OptLive }) {
  return (
    <Banner tone="info" testId="at-live"><span className="spinner" /> {live.step}
      {` · ${(live.tries_now ?? 0).toLocaleString()} tries`}{live.reused_now ? ` (+${live.reused_now} reused)` : ""}
      {` · ${Math.max(0, (live.bests_now ?? 1) - 1)} improvement${(live.bests_now ?? 1) - 1 === 1 ? "" : "s"}`}
      {live.checks_pending ? ` · ${live.checks_pending} lookahead check${live.checks_pending === 1 ? "" : "s"} running` : ""}
      {live.last_duration_s ? ` · ${Math.round(live.last_duration_s)} s per try on one core` : ""}
      {live.memory_note ? ` · ${live.memory_note}` : ""}</Banner>
  );
}

function RunsCard({ st, selected }: { st: OptStatus; selected: string | null }) {
  const money = useMoney();
  if (!st.runs.length) return null;
  return (
    <Card title="Runs" testId="at-runs">
      <TableWrap className="my-report-scroll"><table className="dense">
        <thead><tr><th>Started</th><th>From</th><th>Status</th><th className="num">Tries</th><th className="num">Improvements</th>
          <th className="num">Net R start → final</th><th>Final prop challenges</th><th>Why it stopped</th></tr></thead>
        <tbody>{st.runs.map((x) => {
          const fin = x.final_full ?? x.start_full;
          const c = fin?.chains ? (fin.chains[x.profile] as ChallengeSummary | undefined) : undefined;
          return (
            <tr key={x.id} className={x.id === selected ? "selected" : ""} onClick={() => go(`/my-autotune?run=${x.id}`)} style={{ cursor: "pointer" }}
              data-testid={`at-run-${x.id}`}>
              <td>{new Date(x.created_at).toLocaleString()}</td><td>{x.start.label || "–"}</td>
              <td><Badge tone={x.status === "finished" ? "ok" : x.status === "failed" ? "error" : x.status === "running" ? "info" : "neutral"}>{statusWord(x.status)}</Badge></td>
              <td className="num">{x.tries.toLocaleString()}{x.reused ? ` +${x.reused}` : ""}</td><td className="num">{Math.max(0, x.bests - 1)}</td>
              <td className="num">{r(x.start_full?.metrics.net_r, 1)} → <span className={signCls(x.final_full?.metrics.net_r)}>{r(x.final_full?.metrics.net_r, 1)}</span></td>
              <td>{c ? <ChainLine c={c} money={money.fmt} /> : "–"}</td>
              <td className="small">{x.error ? x.error.message : x.stop_reason ? STOP_REASON[x.stop_reason] ?? x.stop_reason : ""}</td>
            </tr>);
        })}</tbody></table></TableWrap>
    </Card>
  );
}

type YKey = "net_r_train" | "net_r_check" | "prop_train" | "prop_check" | "goals_train" | "goals_check";
const YAXES: Record<YKey, { label: string; get: (t: OptTry) => number | null; fmt: (v: number) => string }> = {
  net_r_train: { label: "Net R, first 70 %", get: (t) => t.train.net_r, fmt: (v) => v.toFixed(0) },
  net_r_check: { label: "Net R, last 30 %", get: (t) => t.check.net_r, fmt: (v) => v.toFixed(0) },
  prop_train: { label: "Prop net $, first 70 %", get: (t) => t.train.prop_net, fmt: (v) => `${Math.round(v)}` },
  prop_check: { label: "Prop net $, last 30 %", get: (t) => t.check.prop_net, fmt: (v) => `${Math.round(v)}` },
  goals_train: { label: "Goals met, first 70 %", get: (t) => t.train.met, fmt: (v) => v.toFixed(0) },
  goals_check: { label: "Goals met, last 30 %", get: (t) => t.check.met, fmt: (v) => v.toFixed(0) },
};

function RunView({ id, live, onChanged }: { id: string; live: OptLive | null; onChanged: () => void }) {
  const { data, error, reload } = useApi<OptRun>(my.autotuneRunUrl(id), [id]);
  const money = useMoney();
  const [y, setY] = useState<YKey>("net_r_check");
  const running = !!live?.running;
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(() => reload(), 6000);
    return () => window.clearInterval(t);
  }, [running]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (!running) reload(); }, [running]); // eslint-disable-line react-hooks/exhaustive-deps
  const [job, setJob] = useJob(() => { reload(); onChanged(); });
  const [err, setErr] = useState<ApiError | null>(null);
  const label = (k: string | null | undefined) => (k ? data?.labels[k] ?? k : "");
  const groups = useMemo<ScatterGroup[]>(() => {
    if (!data) return [];
    const ax = YAXES[y];
    const pt = (t: OptTry) => ({ id: String(t.i), x: t.i, y: ax.get(t) as number, label: `Try ${t.i}: ${label(t.key)} ${fmtVal(t.from)} → ${fmtVal(t.to)}`,
      detail: `first 70 %: ${t.train.met}/${t.train.goals} goals, ${r(t.train.net_r, 1)} · last 30 %: ${t.check.met}/${t.check.goals} goals, ${r(t.check.net_r, 1)}` });
    const ok = data.tries_log.filter((t) => { const v = ax.get(t); return v != null && Number.isFinite(v); });
    return [
      { id: "tries", label: "Tries", color: "var(--c-strategy)", size: 3, points: ok.filter((t) => !t.accepted && !t.rejected_by_check).map(pt) },
      { id: "rejected", label: "Better on the first 70 %, not on the last 30 %", color: "var(--faint)", size: 3.5,
        points: ok.filter((t) => t.rejected_by_check).map(pt) },
      { id: "bests", label: "New best", color: "var(--c-survivor)", size: 6, ring: true, points: ok.filter((t) => t.accepted).map(pt) },
    ];
  }, [data, y]); // eslint-disable-line react-hooks/exhaustive-deps
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <PageSkeleton layout="overview" label="Loading the run" />;
  const bests = data.bests;
  const fin = data.final != null ? bests[data.final] : [...bests].reverse().find((b) => b.status === "best") ?? null;
  const first = bests[0] ?? null;
  const save = async (k: number) => { setErr(null); try { setJob(await my.autotuneSave(data.id, k)); } catch (e) { setErr(e as ApiError); } };
  const savedOf = (k: number) => data.saved.find((s) => s.best === k);
  return (
    <>
      <Card title={`Run of ${new Date(data.created_at).toLocaleString()}`} testId="at-run-view"
        actions={data.final_backtest ? <a className="btn btn-secondary btn-sm" href={href(`/my-backtest?r=${data.final_backtest}`)}
          data-testid="at-final-link">Final backtest with trades</a> : undefined}>
        <p className="muted small">Started from <b>{data.start.label || data.start.id}</b> · scored on {data.profile_name} ·
          first 70 % until {new Date(data.window.split).toLocaleDateString()} · {data.tries.toLocaleString()} tries
          {data.reused ? ` (+${data.reused} reused from earlier runs)` : ""} · {data.rejected_by_check} rejected by the last 30 %</p>
        {running && live && <LiveLine live={live} />}
        {!running && data.stop_reason && <Banner tone={data.status === "failed" ? "error" : "ok"} testId="at-stop-reason">
          {STOP_REASON[data.stop_reason] ?? data.stop_reason}</Banner>}
        {data.error && <Banner tone="error">{data.error.message}</Banner>}
        {data.final_error && <Banner tone="error">The final backtest could not be made: {data.final_error}</Banner>}
        <JobLine job={job} />
        <ErrorPanel error={err} />
        {first?.full && fin?.full ? <Compare a={first} b={fin} profile={data.profile} money={money.fmt} />
          : <p className="small muted">The whole-period numbers appear when the lookahead checks are done.</p>}
        {(data.changes ?? []).length > 0 && <>
          <h3 className="small-head">What changed from the starting settings</h3>
          <dl className="kv" data-testid="at-changes">{(data.changes ?? []).map((c) => (
            <div key={c.key} className="kv-row"><dt>{c.label}</dt><dd>{fmtVal(c.from)} → <b>{fmtVal(c.to)}</b></dd></div>))}</dl>
        </>}
      </Card>
      <Card title="Every try" testId="at-chart" actions={<Field label="Up"><Select value={y} onChange={(v) => setY(v as YKey)} ariaLabel="Vertical axis"
        options={(Object.keys(YAXES) as YKey[]).map((k) => ({ value: k, label: YAXES[k].label }))} testId="at-y" /></Field>}>
        {data.tries_log.length ? <XYScatter groups={groups} x={{ label: "Try", fmt: (v) => v.toFixed(0) }}
          y={{ label: YAXES[y].label, fmt: YAXES[y].fmt, ref: y.startsWith("net_r") || y.startsWith("prop") ? { value: 0, label: "break-even" } : undefined }}
          height={320} testId="at-scatter" /> : <Empty>No try finished yet.</Empty>}
      </Card>
      <Card title="Path of improvements" testId="at-path">
        <TableWrap><table className="dense">
          <thead><tr><th className="num">#</th><th>Change</th><th>First 70 %</th><th>Last 30 %</th><th>Lookahead check</th><th /></tr></thead>
          <tbody>{bests.map((b) => (
            <tr key={b.n} className={b.status !== "best" ? "muted" : b.n === data.final ? "selected" : ""} data-testid={`at-best-${b.n}`}>
              <td className="num">{b.n}</td>
              <td>{b.change ? <>{label(b.change.key)}: {fmtVal(b.change.from)} → <b>{fmtVal(b.change.to)}</b></> : "Starting settings"}
                {b.status === "discarded" && <span className="muted small"> (thrown away: built on a result that failed the check)</span>}</td>
              <td><PartLine s={b.train} money={money.fmt} /></td><td><PartLine s={b.check} money={money.fmt} /></td>
              <td><LookBadge b={b} /></td>
              <td>{savedOf(b.n) ? <a href={href(`/my-backtest?r=${savedOf(b.n)!.id}`)}>Open backtest</a>
                : b.status === "best" && b.n > 0 && !running ? <Button small onClick={() => save(b.n)} busy={job?.state === "running"}
                  testId={`at-save-${b.n}`}>Save as backtest</Button> : null}</td>
            </tr>))}</tbody></table></TableWrap>
        <TechDetails rows={[["Run", data.id], ["Tries per batch", String(data.batch)], ["Final settings hash", fin?.settings_hash ?? ""]]} />
      </Card>
    </>
  );
}

function PartLine({ s, money }: { s: PartScore; money: (v: number | null) => string }) {
  const c = s.chain;
  return (
    <span className="small" title={s.rows.map((x) => `${GOAL_LABEL[x.goal] ?? x.goal}: ${x.ok ? "met" : "not met"}`).join(" · ")}>
      {s.met}/{s.goals} goals · <span className={signCls(s.net_r)}>{r(s.net_r, 1)}</span>
      {c && !c.error ? <> · {c.passes}P/{c.fails}F · <span className={signCls(c.net)}>{money(c.net)}</span></> : null}
    </span>
  );
}

function LookBadge({ b }: { b: OptBest }) {
  if (b.lookahead === "passed") return <Badge tone="ok">passed</Badge>;
  if (b.lookahead === "failed") return <Badge tone="error" title={b.lookahead_detail}>failed</Badge>;
  if (b.lookahead === "not_checked") return <Badge tone="neutral" title="The run was stopped before the check finished">not checked</Badge>;
  return <Badge tone="info">checking…</Badge>;
}

function Compare({ a, b, profile, money }: { a: OptBest; b: OptBest; profile: string; money: (v: number | null) => string }) {
  const ma = a.full!.metrics, mb = b.full!.metrics;
  const ca = a.full!.chains[profile] as ChallengeSummary | undefined, cb = b.full!.chains[profile] as ChallengeSummary | undefined;
  const rows: [string, ReactNode, ReactNode][] = [
    ["Trades per week", n(ma.trades_per_week, 2), n(mb.trades_per_week, 2)],
    ["Win rate", pct(ma.win_rate), pct(mb.win_rate)],
    ["Net R", <span className={signCls(ma.net_r)}>{r(ma.net_r, 1)}</span>, <span className={signCls(mb.net_r)}>{r(mb.net_r, 1)}</span>],
    ["R per trade", r(ma.expectancy_r), r(mb.expectancy_r)],
    ["Profit factor", n(ma.profit_factor, 2), n(mb.profit_factor, 2)],
    ["Worst drawdown", `${n(ma.max_drawdown_r, 1)} R`, `${n(mb.max_drawdown_r, 1)} R`],
    ["Losing months", `${ma.months_losing ?? 0} of ${ma.months_total ?? 0}`, `${mb.months_losing ?? 0} of ${mb.months_total ?? 0}`],
    ["Planned reward : risk", n(ma.avg_planned_rr, 2), n(mb.avg_planned_rr, 2)],
    ["Prop challenges bought", ca?.challenges ?? "–", cb?.challenges ?? "–"],
    ["Failed / passed", ca ? `${ca.fails} / ${ca.passes}` : "–", cb ? `${cb.fails} / ${cb.passes}` : "–"],
    ["Payouts (average per pass)", ca ? `${ca.payouts} (${money(ca.avg_payout_per_pass)})` : "–", cb ? `${cb.payouts} (${money(cb.avg_payout_per_pass)})` : "–"],
    ["Fees paid", money(ca?.fees_total ?? null), money(cb?.fees_total ?? null)],
    ["Prop net (payouts − fees)", <b className={signCls(ca?.net)}>{money(ca?.net ?? null)}</b>, <b className={signCls(cb?.net)}>{money(cb?.net ?? null)}</b>],
  ];
  return (
    <TableWrap testId="at-compare"><table className="dense">
      <thead><tr><th>Whole discovery period</th><th className="num">Start</th><th className="num">Final (best #{b.n})</th></tr></thead>
      <tbody>{rows.map(([k, x, y]) => <tr key={k}><td>{k}</td><td className="num">{x}</td><td className="num">{y}</td></tr>)}</tbody>
    </table></TableWrap>
  );
}

