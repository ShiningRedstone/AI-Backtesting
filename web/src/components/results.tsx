/* Backtest results views (ADR-73): "the field" of tested strategies, survivors, breakdowns, the exit comparison, the
   bootstrapped evaluation simulator and the per-strategy / per-control panels. Every number comes from the read-only
   backend read models (/api/results-view/*, /api/prop/bootstrap); this file only lays them out. Scope (in-sample unless
   labelled), basis (net / gross), synthetic data and simulated results are labelled where they are shown. */
import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { BootstrapResponse, ControlPanelData, PropSummaryRow, ResultsOverview, StrategyPanelData } from "../api/types";
import { useApi, useApp } from "../app/context";
import { facetLabel, humanize, profileLabel, statusLabel } from "../app/labels";
import { href, useRoute } from "../app/router";
import { HBars, PathsChart, ScatterChart } from "./charts";
import type { ScatterGroup } from "./charts";
import { Badge, Banner, Button, Card, Empty, ErrorPanel, FavStar, KeyValues, Kpi, Loading, Mono, Scope, TableWrap, TechDetails, n, pct, r, shortTime, signCls } from "./ui";

/** The plain name of the prop account chosen in Settings for "passes evaluation / payout" and survivors. */
export function useCriteriaName(): string {
  const { prefs } = useApp();
  return prefs.profile_choices?.find((p) => p.profile_id === prefs.prop_criteria_profile)?.name ?? profileLabel(prefs.prop_criteria_profile);
}

/** Pick which backtests Backtest results shows: all, or the strategies selected in one (named) research run. */
export function RunPicker({ value, onChange, testId = "run-picker" }: { value: string; onChange: (ref: string) => void; testId?: string }) {
  const { data } = useApi<{ ref: string; name: string | null; created_at: string | null; n_scope: number | null; status: string | null }[]>("/api/results-view/runs");
  return (
    <select className={`input${value ? " active" : ""}`} value={value} aria-label="which backtests" data-testid={testId}
      onChange={(e: { target: HTMLSelectElement }) => onChange(e.target.value)} style={{ maxWidth: 360 }}>
      <option value="">All backtests</option>
      {(data ?? []).map((r) => <option key={r.ref} value={r.ref}>{r.name ?? `Research run of ${shortTime(r.created_at)}`}
        {r.n_scope != null ? ` · ${r.n_scope.toLocaleString()} strategies` : ""}</option>)}
    </select>
  );
}

export const usd = (v: number | null | undefined, digits = 0) => (v == null || !Number.isFinite(v) ? "—"
  : `${v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits })}`);
const hold = (m: number | null | undefined) => (m == null ? "—" : m >= 90 ? `${(m / 60).toFixed(1)} h` : `${Math.round(m)} min`);

const BREAKDOWNS: [string, string][] = [["target_type", "By target"], ["entry_type", "By entry"], ["trailing", "By trailing stop"],
  ["stop_type", "By stop"], ["direction", "By direction"], ["session", "By session"]];

/** The top of Backtest results → Overview. */
export function ResultsOverviewSection({ onOpen, onOpenControl }: { onOpen: (sid: string) => void; onOpenControl: (cid: string) => void }) {
  const [basis, setBasis] = useState<"net" | "gross">("net");
  const [controls, setControls] = useState(true);
  const [allWindows, setAllWindows] = useState(false);
  const route = useRoute();
  const [run, setRun] = useState(route.query.get("run") ?? "");
  const crit = useCriteriaName();
  const qs = `basis=${basis}&controls=${controls ? 1 : 0}${run ? `&campaign_run=${encodeURIComponent(run)}` : ""}`;
  const { data: o, error } = useApi<ResultsOverview>(`/api/results-view/overview?${qs}`, [qs]);
  const picker = <div className="filterbar" data-testid="results-picker"><span className="fgroup">Show</span>
    <RunPicker value={run} onChange={setRun} testId="results-run-picker" /></div>;
  if (error) return <>{picker}<ErrorPanel error={error} /></>;
  if (!o || (o.campaign_run ?? "") !== run) return <>{picker}<Loading label="Reading stored backtests…" /></>;
  const f = o.facts;
  const tags = <><Scope kind="is" /><Scope kind={basis} />{f.synthetic_tested > 0 && <Scope kind="synthetic" />}</>;
  const groups: ScatterGroup[] = [
    { id: "strategies", label: "Strategies", color: "var(--c2)", size: 3.5,
      points: o.points.filter((p) => !p.survivor && p.win_rate != null && p.avg_rr != null).map((p) => ({ id: p.strategy_id, x: p.win_rate!, y: p.avg_rr!,
        label: p.display_name ?? humanize(p.name ?? "Strategy"), detail: `${p.trades} trades · ${r(p.expectancy_r)} per trade${p.synthetic ? " · synthetic data" : ""}` })) },
    { id: "survivors", label: "Survivors", color: "var(--c1)", size: 5, ring: true,
      points: o.points.filter((p) => p.survivor && p.win_rate != null && p.avg_rr != null).map((p) => ({ id: p.strategy_id, x: p.win_rate!, y: p.avg_rr!,
        label: p.display_name ?? humanize(p.name ?? "Strategy"), detail: `${p.trades} trades · ${r(p.expectancy_r)} per trade${p.synthetic ? " · synthetic data" : ""}` })) },
    ...(controls ? [{ id: "controls", label: "Random controls", color: "var(--c-neutral)", size: 3.5,
      points: o.controls.filter((c) => c.win_rate != null && c.avg_rr != null).map((c) => ({ id: c.control_id, x: c.win_rate!, y: c.avg_rr!,
        label: "Random control", detail: `${c.trades} trades · ${r(c.expectancy_r)} per trade · not a strategy` })) }] : []),
  ];
  const curves = [{ id: "zero", label: "Break-even before costs", points: o.breakeven.zero.points.map((p) => ({ x: p.win_rate, y: p.avg_rr })) },
    ...(o.breakeven.after_cost ? [{ id: "cost", label: `Break-even after a typical cost (${o.breakeven.after_cost.cost_r.toFixed(3)} R)`, tone: "warn" as const,
      points: o.breakeven.after_cost.points.map((p) => ({ x: p.win_rate, y: p.avg_rr })) }] : [])];
  const ex = o.exit_comparison, ev = o.eval_summary;
  return (
    <>
      {picker}
      <section className="featured" data-testid="results-facts">
        <h3>The field <span className="labels">{tags}</span></h3>
        <div className="kpis">
          <Kpi label="Strategies" value={f.strategies.toLocaleString()} sub={`${f.tested.toLocaleString()} with trades in this scope`} />
          <Kpi label="Survivors" value={f.survivors.toLocaleString()} accent sub={`positive after costs and pass evaluation and payout under ${crit}`} testId="kpi-survivors" />
          <Kpi label="Positive before costs" value={f.gross_positive.toLocaleString()} sub={f.tested ? `${pct(f.gross_positive / f.tested, 0)} of tested` : undefined} />
          <Kpi label="Positive after costs" value={f.net_positive.toLocaleString()} sub={f.tested ? `${pct(f.net_positive / f.tested, 0)} of tested` : undefined} />
          <Kpi label="Typical cost per trade" value={f.median_cost_r_per_trade == null ? "—" : `${n(f.median_cost_r_per_trade, 3)} R`} sub="median across tested strategies" />
        </div>
      </section>
      <Card title={<>Win rate against average reward to risk {tags}</>} testId="results-field"
        actions={<div className="inline">
          <div className="segmented small" role="group" aria-label="basis">
            {(["net", "gross"] as const).map((b) => <button key={b} className={basis === b ? "on" : ""} onClick={() => setBasis(b)}
              data-testid={`basis-${b}`}>{b === "net" ? "After costs" : "Before costs"}</button>)}</div>
          <label className="check small"><input type="checkbox" checked={controls} data-testid="show-controls"
            onChange={(e: { target: HTMLInputElement }) => setControls(e.target.checked)} />show random controls</label></div>}>
        {!o.points.length && !o.controls.length ? <Empty>No tested strategies in this scope yet. <a href={href("/run")}>Run a backtest</a>.</Empty> : <>
          <ScatterChart groups={groups} curves={curves} yUnit="avg reward : risk" yMax={8} testId="field-scatter"
            onPick={(id) => (id.startsWith("CTRL_") ? onOpenControl(id) : onOpen(id))} />
          <p className="small muted">Each dot is one strategy's latest in-sample backtest ({o.basis_label.toLowerCase()}); click a dot for its panel.
            Points above the dashed line made money on average{basis === "gross" ? " before costs" : ""}. {o.breakeven.note}
            {!o.breakeven.after_cost && basis === "net" && " Switch to Before costs to see the break-even line after a typical cost."}</p>
          <p className="small muted">{o.survivor_rule}</p>
        </>}
      </Card>
      <h3>What the tested strategies have in common</h3>
      <div className="panel-grid">
        {BREAKDOWNS.map(([k, title]) => {
          // ADR-79: sessions are grouped by market hours (Asia, London, NY AM, ...); every window behind a toggle
          const dim = k === "session" && !allWindows && o.breakdowns.session_group ? "session_group" : k;
          const rows = o.breakdowns[dim] ?? [];
          return <Card key={k} title={<>{title} {tags}</>} testId={`results-by-${k.replace(/_/g, "-")}`}
            actions={k === "session" && o.breakdowns.session_group ? <Button small onClick={() => setAllWindows(!allWindows)} testId="session-toggle">
              {allWindows ? "Group by market hours" : "Show all windows"}</Button> : undefined}>
            {!rows.length ? <Empty>No tested strategies.</Empty> : <HBars rows={rows.map((g) => ({
              label: dim === "session_group" ? g.group : facetLabel(k, g.group), value: g.median_expectancy_r,
              note: `${g.strategies} strateg${g.strategies === 1 ? "y" : "ies"} · ${pct(g.survivor_rate, 0)} survivors` }))}
              unit={`median ${basis === "net" ? "net" : "gross"} R per trade`} />}
          </Card>;
        })}
      </div>
      <div className="grid-cards">
        <Card title={<>Exit on the opposite signal vs a fixed target {tags}</>} testId="results-exit-comparison">
          <KeyValues rows={[
            ["Exit on the opposite signal", <>{ex.signal_exit.median_expectancy_r == null ? "—" : `${r(ex.signal_exit.median_expectancy_r)} per trade`}
              <span className="muted small"> · {ex.signal_exit.strategies} strateg{ex.signal_exit.strategies === 1 ? "y" : "ies"}</span></>],
            ["Fixed risk-multiple target", <>{ex.fixed_target.median_expectancy_r == null ? "—" : `${r(ex.fixed_target.median_expectancy_r)} per trade`}
              <span className="muted small"> · {ex.fixed_target.strategies} strateg{ex.fixed_target.strategies === 1 ? "y" : "ies"}{ex.fixed_target.multiples.length ? ` · targets ${ex.fixed_target.multiples.map((m) => `${m}R`).join(", ")}` : ""}</span></>],
            ["Difference", <span className={signCls(ex.difference_r)}>{ex.difference_r == null ? "—" : r(ex.difference_r)}</span>]]} />
          <p className="small muted">Median per strategy. Different strategies sit in each group, so this is a description, not a controlled test.</p>
        </Card>
        <Card title={<>Evaluation simulator <span className="labels"><Scope kind="sim" /><Scope kind="is" /></span></>} testId="results-eval-summary">
          {!ev.survivors ? <Empty>No survivors yet, so there is nothing to simulate.</Empty> : <>
            <TableWrap><table className="dense"><thead><tr><th>Prop account</th><th className="r">Typical chance to pass</th></tr></thead>
              <tbody>{Object.entries(ev.median_p_pass).map(([pid, v]) => <tr key={pid}><td>{profileLabel(pid, ev.profile_names?.[pid])}</td>
                <td className="r num">{v == null ? <span className="faint">not simulated yet</span> : pct(v, 0)}</td></tr>)}</tbody></table></TableWrap>
            <p className="small muted">{ev.survivors_simulated} of {ev.survivors} survivors simulated ({ev.defaults.replays.toLocaleString()} replays,
              blocks of {ev.defaults.block_days} trading days). Run a simulation from a survivor's panel. Simulated from historical trades
              under the stated rules (some are assumed defaults); not a forecast.</p></>}
        </Card>
      </div>
    </>
  );
}

// ------------------------------------------------------------------------------ strategy panel
export function StrategyPanel({ id }: { id: string }) {
  const { data: p, error } = useApi<StrategyPanelData>(`/api/results-view/strategies/${id}`, [id]);
  if (error) return <ErrorPanel error={error} />;
  if (!p) return <Loading label="Loading strategy…" />;
  const k = p.kpis;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }} data-testid="strategy-panel">
      <div>
        <div className="inline" style={{ flexWrap: "wrap", gap: 6 }}>
          {p.tested && <FavStar id={p.strategy_id} testId="panel-fav" />}<b style={{ fontSize: 16 }}>{p.display_name}</b>
          {p.survivor && <Badge tone="ok">Survivor</Badge>}
          {p.tested && <Scope kind={p.status === "OUT_OF_SAMPLE" ? "oos" : p.status === "WALK_FORWARD" ? "wf" : "is"} />}
          {p.tested && <Scope kind="net" />}{p.synthetic && <Scope kind="synthetic" />}
        </div>
        <p className="small muted" style={{ margin: "6px 0 0" }}>{p.explanation}</p>
        {p.dataset && <p className="small muted" style={{ margin: "4px 0 0" }}>{p.dataset.instrument ?? "Market"} · {facetLabel("timeframe", p.dataset.timeframe)} bars ·
          {" "}{p.dataset.start.slice(0, 10)} to {p.dataset.end.slice(0, 10)}{p.dataset.provider ? ` · ${humanProvider(p.dataset.provider)}` : ""}</p>}
      </div>
      {!p.tested || !k ? <Empty>No backtest with trades in this scope yet. <a href={href("/run")}>Run a backtest</a> for this strategy.</Empty> : <>
        <div className="kpis" data-testid="panel-kpis">
          <Kpi label="Net R per trade" value={r(k.expectancy_r)} tone={signCls(k.expectancy_r) as "pos" | "neg" | ""} accent
            sub={`before costs ${r(k.gross_r_per_trade)}`} />
          <Kpi label="Trades" value={k.trades.toLocaleString()} sub={`${n(k.trades_per_week, 1)} per week`} />
          <Kpi label="Win rate" value={pct(k.win_rate, 0)} sub="never used to rank" />
          <Kpi label="Average reward to risk" value={k.avg_rr == null ? "—" : `${n(k.avg_rr, 2)} : 1`} />
          <Kpi label="Total net R" value={n(k.net_r, 1)} tone={signCls(k.net_r) as "pos" | "neg" | ""}
            sub={`${usd(k.net_usd_at_risk)} at ${usd(p.risk_per_trade_usd)} risk per trade`} />
          <Kpi label="Max drawdown" value={`${n(k.max_drawdown_r, 1)} R`} sub={`${usd(k.max_drawdown_usd_at_risk)} · longest losing streak ${k.max_loss_streak ?? "—"}`} />
          <Kpi label="Cost per trade" value={`${n(k.cost_r_per_trade, 3)} R`} sub={p.cost_status ? `costs ${humanCost(p.cost_status)}` : undefined} />
          <Kpi label="Weeks with a trade" value={pct(k.pct_weeks_with_trade, 0)} sub={k.weeks_in_data ? `of ${k.weeks_in_data} weeks in the data` : undefined} />
          <Kpi label="Average hold" value={hold(k.avg_hold_minutes)} />
          <Kpi label="Profit factor" value={n(k.profit_factor, 2)} sub={k.sample_label ? humanize(k.sample_label) : undefined} />
        </div>
        <p className="small muted">Dollar figures are R multiplied by your risk per trade ({usd(p.risk_per_trade_usd)}, set in Settings); the
          backtest itself is unchanged.</p>
        {p.last_12_months && <Card title={<>Last 12 months of data <Scope kind="is" /></>} testId="panel-last-12">
          <div className="kpis">
            <Kpi label="Trades" value={p.last_12_months.trades} />
            <Kpi label="Net R per trade" value={r(p.last_12_months.expectancy_r)} tone={signCls(p.last_12_months.expectancy_r) as "pos" | "neg" | ""} />
            <Kpi label="Total net R" value={n(p.last_12_months.net_r, 1)} sub={usd(p.last_12_months.net_r == null ? null : p.last_12_months.net_r * p.risk_per_trade_usd)} />
            <Kpi label="Win rate" value={pct(p.last_12_months.win_rate, 0)} />
          </div>
          <p className="small muted">{p.last_12_months.from} to {p.last_12_months.to}. The window ends at the last trade in the data, not today.</p>
        </Card>}
        <Card title={<>Out-of-sample <Scope kind="oos" /></>} testId="panel-oos">
          {!p.out_of_sample?.length ? <p className="small muted" style={{ margin: 0 }}>No out-of-sample test of this strategy yet. In-sample results
            alone are not evidence of an edge.</p> : <TableWrap><table className="dense"><thead><tr><th>Test</th><th>Period</th>
              <th className="r">Trades</th><th className="r">Net R per trade</th><th className="r">Total net R</th></tr></thead>
            <tbody>{p.out_of_sample.map((x) => <tr key={x.run_id}><td>{statusLabel(x.status)}</td><td className="small">{x.start.slice(0, 10)} to {x.end.slice(0, 10)}</td>
              <td className="r num">{x.trades}</td><td className={`r num ${signCls(x.expectancy_r)}`}>{r(x.expectancy_r)}</td>
              <td className="r num">{n(x.net_r, 1)} <span className="muted small">{usd(x.net_usd_at_risk)}</span></td></tr>)}</tbody></table></TableWrap>}
          {!!p.holdout?.length && <p className="small" style={{ marginBottom: 0 }}><Scope kind="holdout" /> {p.holdout.length} holdout evaluation(s):
            {" "}{p.holdout.map((h) => `${r(h.expectancy_r)} per trade over ${h.trades} trades`).join("; ")}. Kept separate from research results.</p>}
        </Card>
        {p.rank?.position != null && <p className="small muted">Ranked {p.rank.position} of {p.rank.of} by total net R among the latest in-sample
          backtests (an ordering of past results, not a validation).</p>}
        <Card title={<>Prop firm evaluations <span className="labels"><Scope kind="sim" /></span></>} testId="panel-prop">
          {!p.prop?.length ? <Empty>No prop audit stored with this backtest.</Empty> : <PropRows rows={p.prop} runId={p.run_id!} />}
        </Card>
      </>}
      <Card title="Rules in plain English" testId="panel-rules">
        <TableWrap><table className="dense"><tbody>{p.rules.map((x) => <tr key={x.rule}><th style={{ width: 170 }}>{x.rule}</th><td>{x.text}</td></tr>)}</tbody></table></TableWrap>
      </Card>
      <TechDetails testId="panel-technical" rows={Object.entries(p.technical).filter(([, v]) => v).map(([key, v]) => [TECH_LABEL[key] ?? key, <Mono>{v}</Mono>])} />
    </div>
  );
}

const TECH_LABEL: Record<string, string> = { strategy_id: "Strategy ID", logic_hash: "Logic hash", definition_hash: "Definition hash",
  machine_name: "Machine name", run_id: "Backtest ID", dataset_id: "Dataset ID", trades_hash: "Trades hash", config_hash: "Config hash",
  control_id: "Control ID", candidate_strategy_id: "Matched strategy ID", validation_id: "Control batch ID" };
const humanProvider = (p: string) => ({ synthetic: "synthetic demo data", dukascopy: "Dukascopy", histdata: "HistData" } as Record<string, string>)[p] ?? facetLabel("provider", p);
const humanCost = (s: string) => ({ configured: "configured", unconfigured: "not configured", assumption: "assumed" } as Record<string, string>)[s] ?? facetLabel("cost", s).toLowerCase();

export function PropRows({ rows, runId }: { rows: PropSummaryRow[]; runId: string }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <>
      <TableWrap><table className="dense"><thead><tr><th>Account</th><th>Actual trade order</th><th className="r">Payouts</th>
        <th className="r">Chance to pass</th><th className="r">Chance of first payout</th><th className="r">Chance of breach</th><th className="r">Days to pass</th><th /></tr></thead>
        <tbody>{rows.map((x) => { const b = x.bootstrap; const pid = x.profile_id ?? "";
          return <tr key={pid}>
            <td>{profileLabel(x.profile_id, x.profile_name)}</td>
            <td><Badge tone={x.evaluation === "PASS" ? "ok" : x.evaluation === "FAIL" ? "warn" : "neutral"}>{statusLabel(x.evaluation ?? x.status)}</Badge>
              {x.evaluation === "PASS" && x.pass_days != null && <span className="small muted"> in {x.pass_days} days</span>}
              {x.evaluation === "FAIL" && x.failure_reason && <div className="small muted">{statusLabel(x.failure_reason)}</div>}</td>
            <td className="r num">{x.payouts}</td>
            <td className="r num">{b ? pct(b.p_pass, 0) : <span className="faint">—</span>}</td>
            <td className="r num">{b ? pct(b.p_first_payout, 0) : <span className="faint">—</span>}</td>
            <td className="r num">{b ? pct(b.p_evaluation_breach, 0) : <span className="faint">—</span>}</td>
            <td className="r num">{b?.median_days_to_pass != null ? n(b.median_days_to_pass, 0) : <span className="faint">—</span>}</td>
            <td><Button small kind="ghost" onClick={() => setOpen(open === pid ? null : pid)} testId={`simulate-${pid}`}>
              {open === pid ? "Close" : "Simulate"}</Button></td>
          </tr>; })}</tbody></table></TableWrap>
      <p className="small muted">"Actual trade order" replays this backtest's trades in the order they happened under each account's rules
        (assumed defaults where not verified). The chances come from the evaluation simulator, which reshuffles whole trading days.</p>
      {open && <BootstrapSimulator runId={runId} profileId={open} profileName={profileLabel(open, rows.find((x) => x.profile_id === open)?.profile_name)} />}
    </>
  );
}

const MODE_TEXT: Record<string, string> = { profile: "The account's own rules", eod_trailing: "End-of-day trailing drawdown", static: "Static drawdown" };

export function BootstrapSimulator({ runId, profileId, profileName }: { runId: string; profileId: string; profileName: string }) {
  const [n_, setN] = useState(2000);
  const [mode, setMode] = useState("profile");
  const [res, setRes] = useState<BootstrapResponse | null>(null);
  const [err, setErr] = useState<Error | null>(null);
  const [go, setGo] = useState(0);
  useEffect(() => {
    if (!go) return;
    let live = true, timer = 0;
    const tick = () => api.post<BootstrapResponse>("/api/prop/bootstrap", { run_id: runId, profile_id: profileId, n: n_, mode })
      .then((x) => { if (!live) return; setRes(x); setErr(null); if (x.state === "running") timer = window.setTimeout(tick, 1500); })
      .catch((e: Error) => { if (live) setErr(e); });
    tick();
    return () => { live = false; window.clearTimeout(timer); };
  }, [go, runId, profileId, n_, mode]);
  const out = res?.state === "done" ? res.result : undefined;
  const pctl = out?.paths?.percentiles;
  return (
    <Card title={<>Evaluation simulator · {profileName} <Scope kind="sim" /></>} testId="bootstrap-sim">
      <div className="inline" style={{ flexWrap: "wrap", gap: 8 }}>
        <label className="small muted">Drawdown rule</label>
        <select className="input input-sm" value={mode} aria-label="drawdown rule" onChange={(e: { target: HTMLSelectElement }) => setMode(e.target.value)}>
          {Object.keys(MODE_TEXT).map((m) => <option key={m} value={m}>{MODE_TEXT[m]}</option>)}
          <option value="intraday_trailing" disabled>Intraday trailing drawdown (not supported)</option>
        </select>
        <label className="small muted">Replays</label>
        <select className="input input-sm" value={n_} aria-label="replays" onChange={(e: { target: HTMLSelectElement }) => setN(Number(e.target.value))}>
          {[500, 1000, 2000].map((v) => <option key={v} value={v}>{v.toLocaleString()}</option>)}
        </select>
        <Button small kind="primary" onClick={() => setGo((g) => g + 1)} busy={res?.state === "running"} busyLabel="Simulating…" testId="bootstrap-run">
          Run simulation</Button>
      </div>
      <p className="small muted">An intraday trailing drawdown is not supported by the simulator and is never approximated.</p>
      {err && <ErrorPanel error={err} />}
      {res?.state === "running" && <div className="small" data-testid="bootstrap-progress">Simulating {res.done ?? 0} of {res.total ?? n_} replays…
        <div className="meter"><span style={{ width: `${((res.done ?? 0) / (res.total || 1)) * 100}%` }} /></div></div>}
      {res?.state === "error" && <Banner tone="error">The simulation failed: {res.error}</Banner>}
      {out && <>
        <div className="kpis" data-testid="bootstrap-result">
          <Kpi label="Chance to pass" value={pct(out.p_pass, 0)} accent />
          <Kpi label="Chance of first payout" value={pct(out.p_first_payout, 0)} />
          <Kpi label="Chance of breach" value={pct(out.p_evaluation_breach, 0)} />
          <Kpi label="Not passed by the end" value={pct(out.p_not_passed_by_end, 0)} />
          <Kpi label="Typical days to pass" value={out.median_days_to_pass == null ? "—" : n(out.median_days_to_pass, 0)}
            sub={out.days_to_pass_p10_p90 ? `most between ${out.days_to_pass_p10_p90[0]} and ${out.days_to_pass_p10_p90[1]}` : undefined} />
        </div>
        {pctl?.["50"] && <PathsChart paths={out.paths.samples ?? []} highlight={{ label: "Median balance", values: pctl["50"] }} unit="$ balance"
          xLabel="trading day" testId="bootstrap-paths"
          refLines={[...(out.paths.start_balance != null && out.paths.target != null ? [{ value: out.paths.start_balance + out.paths.target, label: "Profit target", tone: "ok" as const }] : []),
            ...(out.paths.start_balance != null && out.paths.max_loss != null ? [{ value: out.paths.start_balance - out.paths.max_loss, label: "Starting drawdown limit", tone: "warn" as const }] : [])]} />}
        {Object.keys(out.failure_reasons).length > 0 && <KeyValues rows={Object.entries(out.failure_reasons).map(([k2, v]) =>
          [statusLabel(k2), `${v.toLocaleString()} replays`])} />}
        <p className="small muted">{out.valid_replays.toLocaleString()} replays: whole trading days of this backtest drawn in blocks of {res!.block_days} days,
          fixed seed {res!.seed}. {MODE_TEXT[res!.mode] ?? res!.mode_label}. Simulated from historical trades under stated rules; not a forecast.</p>
      </>}
    </Card>
  );
}

// ------------------------------------------------------------------------------ control panel
export function ControlPanelView({ id }: { id: string }) {
  const { data: c, error } = useApi<ControlPanelData>(`/api/results-view/controls/${id}`, [id]);
  if (error) return <ErrorPanel error={error} />;
  if (!c) return <Loading label="Loading control…" />;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }} data-testid="control-panel">
      <div className="inline"><b style={{ fontSize: 16 }}>Random control</b><Scope kind="control" />{c.synthetic && <Scope kind="synthetic" />}</div>
      <Banner tone="info">{c.note}</Banner>
      <div className="kpis">
        <Kpi label="Net R per trade" value={r(c.expectancy_r)} tone={signCls(c.expectancy_r) as "pos" | "neg" | ""} />
        <Kpi label="Trades" value={c.trades} />
        <Kpi label="Win rate" value={pct(c.win_rate, 0)} />
        <Kpi label="Average reward to risk" value={c.avg_rr == null ? "—" : `${n(c.avg_rr, 2)} : 1`} />
        <Kpi label="Total net R" value={n(c.net_r, 1)} sub={`${usd(c.net_usd_at_risk)} at ${usd(c.risk_per_trade_usd)} risk per trade`} />
        <Kpi label="Max drawdown" value={`${n(c.max_drawdown_r, 1)} R`} sub={`longest losing streak ${c.max_loss_streak ?? "—"}`} />
      </div>
      <TechDetails rows={([["control_id", c.control_id], ["candidate_strategy_id", c.candidate_strategy_id], ["validation_id", c.validation_id],
        ["dataset_id", c.dataset_id]] as [string, string | null][]).filter(([, v]) => v).map(([key, v]) => [TECH_LABEL[key] ?? key, <Mono>{v}</Mono>])}>
        <p className="small muted">Realization {c.realization}, seed {c.seed ?? "—"}.</p>
      </TechDetails>
    </div>
  );
}
