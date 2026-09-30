import { useState } from "react";
import { api, ApiError } from "../api/client";
import type { PropAccountResult, PropConfigRow, PropSimRow, PropSimulation, RunRow } from "../api/types";
import { href, useRoute } from "../app/router";
import { ChooseWorkspaceLink } from "../components/workspace";
import { useApi, useApp } from "../app/context";
import { Badge, Banner, Button, Card, Empty, ErrorPanel, Field, KeyValues, Kpi, Loading, Mono, Scope, Select, TableWrap, TextInput, fmt, shortTime } from "../components/ui";
import { BarChart, LineChart } from "../components/charts";

/** Phase 6: prop-account rules replayed over a STORED run's trades. The strategy result and the
 *  account result are shown separately; nothing here ranks, scores or promotes a strategy. */
export function PropPage() {
  const route = useRoute();
  return route.parts[1] ? <SavedSimulation id={route.parts[1]} /> : <PropWorkspace />;
}

interface AccountDraft { account_id: string; config: string; start: string }
const tone = (s: string) => s === "TARGET_REACHED" ? "ok" : s === "INCOMPLETE" || s === "ACTIVE" ? "info" : "error";
const yes = (b: boolean | null | undefined) => b == null ? "—" : b ? "yes" : "no";
const usd = (v: unknown) => typeof v === "number" ? v.toFixed(2) : fmt(v);

function PropWorkspace() {
  const { toast } = useApp();
  const runs = useApi<RunRow[]>("/api/results");
  const cfgs = useApi<PropConfigRow[]>("/api/prop/configs");
  const sims = useApi<PropSimRow[]>("/api/prop/simulations");
  const route = useRoute();
  const [runId, setRunId] = useState(route.query.get("run") ?? "");
  const [accounts, setAccounts] = useState<AccountDraft[]>([{ account_id: "A1", config: "", start: "" }]);
  const [custom, setCustom] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [result, setResult] = useState<PropSimulation | null>(null);
  if (runs.error) return <ErrorPanel error={runs.error} />;
  if (cfgs.error) return <ErrorPanel error={cfgs.error} />;
  if (!runs.data || !cfgs.data) return <Loading label="Loading runs and prop rule sets…" />;
  const valid = cfgs.data.filter((c) => c.valid);
  const cfgOptions = [...valid.map((c) => ({ value: c.id as string, label: `${c.id}${c.synthetic_test_only ? " (synthetic test-only)" : ""}` })),
    { value: "__custom__", label: "Custom rule set (YAML below)" }];
  const setAcc = (i: number, patch: Partial<AccountDraft>) => setAccounts(accounts.map((a, j) => j === i ? { ...a, ...patch } : a));
  const run = () => {
    setBusy(true); setErr(null);
    const body = { run_id: runId, record: true, accounts: accounts.map((a) => ({
      account_id: a.account_id || undefined, config: a.config === "__custom__" ? custom : a.config, start: a.start || undefined })) };
    api.post<PropSimulation>("/api/prop/simulate", body)
      .then((r) => { setResult(r); sims.reload(); toast("ok", `Simulation ${r.simulation_id} recorded`); })
      .catch((e: ApiError) => setErr(e)).finally(() => setBusy(false));
  };
  const ready = runId && accounts.every((a) => a.config && (a.config !== "__custom__" || custom.trim()));
  return (
    <div className="page" data-testid="prop-page">
      <header className="page-head"><h1>Prop Simulation</h1></header>
      <Banner tone="info">Applies account rules to the <b>recorded trades</b> of a stored backtest. The backtest itself is read
        only and never changed. A passed evaluation here is a description of that trade sequence under those rules — not evidence
        that the strategy is profitable or deployable. No broker routing, no live trading.</Banner>
      <Card title="Setup">
        {runs.data.length === 0 && <Banner tone="warn" testId="prop-no-runs">No stored research runs in this workspace. Run a backtest first,
          or <ChooseWorkspaceLink /> that holds your runs.</Banner>}
        <Field label="Source result (stored run)" hint="Results page lists every stored run with its status and dataset.">
          <Select value={runId} onChange={setRunId} testId="prop-run" placeholder="Choose a run…"
            options={runs.data.map((r) => ({ value: r.run_id, label: `${r.run_id} · ${r.strategy_name ?? r.strategy_id} · ${r.dataset_id} · ${r.status}${r.synthetic ? " · SYNTHETIC" : ""}` }))} />
        </Field>
        <TableWrap testId="prop-accounts"><table>
          <thead><tr><th>Account</th><th>Rule set</th><th>Start (UTC, optional)</th><th /></tr></thead>
          <tbody>{accounts.map((a, i) => (
            <tr key={i}><td><TextInput value={a.account_id} onChange={(v) => setAcc(i, { account_id: v })} ariaLabel={`account ${i + 1} id`} /></td>
              <td><Select value={a.config} onChange={(v) => setAcc(i, { config: v })} options={cfgOptions} placeholder="Choose rules…" testId={`prop-config-${i}`} /></td>
              <td><TextInput value={a.start} onChange={(v) => setAcc(i, { start: v })} placeholder="e.g. 2024-02-01" ariaLabel={`account ${i + 1} start`} /></td>
              <td>{accounts.length > 1 && <button className="linklike danger" onClick={() => setAccounts(accounts.filter((_, j) => j !== i))}>Remove</button>}</td></tr>))}
          </tbody></table></TableWrap>
        <div className="actions">
          <Button small onClick={() => setAccounts([...accounts, { ...accounts[accounts.length - 1], account_id: `A${accounts.length + 1}` }])} testId="prop-add-account">Add account</Button>
        </div>
        {accounts.some((a) => a.config === "__custom__") && (
          <Field label="Custom rule set (YAML)" wide hint={<>Start from a shipped example below; see PROP_SIMULATION.md for every field. Validated by the backend.</>}>
            <textarea className="input mono" rows={16} value={custom} data-testid="prop-custom"
              onChange={(e: { target: HTMLTextAreaElement }) => setCustom(e.target.value)} />
            <div className="actions">{valid.map((c) => <Button key={c.file} small onClick={() => setCustom(c.text)}>Load {c.id}</Button>)}</div>
          </Field>)}
        <div className="actions"><Button kind="primary" onClick={run} busy={busy} busyLabel="Simulating…" disabled={!ready} testId="prop-run-btn">Run simulation</Button></div>
        {err && <ErrorPanel error={err} title="Simulation refused" testId="prop-error" />}
      </Card>
      {result && <SimulationView sim={result} />}
      <RuleSets rows={cfgs.data} />
      <Card title="Recorded simulations">
        {!sims.data?.length ? <Empty>None yet.</Empty> : (
          <TableWrap testId="prop-sims"><table>
            <thead><tr><th>Simulation</th><th>Created</th><th>Source run</th><th>Dataset</th><th>Accounts</th></tr></thead>
            <tbody>{sims.data.map((s) => (
              <tr key={s.simulation_id}><td><a href={href(`/prop/${s.simulation_id}`)}><Mono>{s.simulation_id}</Mono></a></td>
                <td className="small">{shortTime(s.created_at)}</td><td><a href={href(`/results/${s.source_run_id}`)}><Mono>{s.source_run_id}</Mono></a></td>
                <td><Mono>{s.dataset_id}</Mono></td>
                <td>{s.accounts.map((a) => <span key={a.account_id}>{a.account_id} <Badge tone={tone(a.status)}>{a.status}</Badge> </span>)}</td></tr>))}
            </tbody></table></TableWrap>)}
      </Card>
    </div>
  );
}

function RuleSets({ rows }: { rows: PropConfigRow[] }) {
  return (
    <Card title="Rule sets (configs/prop)">
      <p className="muted small">Versioned YAML rule sets. The shipped examples are SYNTHETIC and TEST-ONLY — they describe no real
        firm. Enter a real program's rules yourself from its current official documentation.</p>
      {!rows.length ? <Empty>No rule sets in configs/prop.</Empty> : (
        <TableWrap testId="prop-configs"><table>
          <thead><tr><th>File</th><th>Id</th><th>Name</th><th>Valid</th><th>Hash</th></tr></thead>
          <tbody>{rows.map((c) => (
            <tr key={c.file}><td><Mono>{c.file}</Mono></td><td>{c.id ?? "—"}{c.synthetic_test_only && <> <Badge tone="demo">synthetic test-only</Badge></>}</td>
              <td>{c.name}</td><td>{c.valid ? <Badge tone="ok">valid</Badge> : <Badge tone="error" title={c.errors.join("; ")}>invalid</Badge>}</td>
              <td><Mono>{c.config_hash?.slice(0, 12) ?? "—"}</Mono></td></tr>))}
          </tbody></table></TableWrap>)}
    </Card>
  );
}

function SavedSimulation({ id }: { id: string }) {
  const { data, error } = useApi<PropSimulation>(`/api/prop/simulations/${id}`, [id]);
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <Loading label="Loading simulation…" />;
  return <div className="page"><header className="page-head"><h1>Prop simulation <Mono>{id}</Mono></h1></header>
    <p><a href={href("/prop")}>Back to prop simulation</a></p><SimulationView sim={data} /></div>;
}

function SimulationView({ sim }: { sim: PropSimulation }) {
  const L = sim.lineage as Record<string, any>, S = sim.strategy_result as Record<string, unknown>;
  return (
    <div data-testid="prop-result" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {sim.labels.map((l) => <Banner key={l} tone={l.startsWith("SYNTHETIC") ? "demo" : "warn"}>{l}</Banner>)}
      <PropOverview sim={sim} />
      <Card title="Strategy result (source run, unchanged)" testId="prop-strategy-result">
        <KeyValues rows={[["Run", <a href={href(`/results/${L.source_run_id}`)}><Mono>{L.source_run_id}</Mono></a>], ["Run status", <Badge>{L.source_run_status}</Badge>],
          ["Trades", fmt(S.trade_count)], ["Net R", fmt(S.net_r)], ["Net USD", usd(S.net_usd)], ["Expectancy (R)", fmt(S.expectancy_r)],
          ["Profit factor", fmt(S.profit_factor)], ["Max drawdown (R)", fmt(S.max_drawdown_r)], ["Max drawdown (USD)", usd(S.max_drawdown_usd)],
          ["Sample", fmt(S.sample_label)]]} />
      </Card>
      <Card title="Prop-account results" testId="prop-account-results">
        <TableWrap><table>
          <thead><tr><th>Account</th><th>Rules</th><th>Outcome</th><th>Start bal.</th><th>Target</th><th>End bal.</th><th>Net P&L</th><th>Net R</th>
            <th>Trades</th><th>Days</th><th>Target reached</th><th>DD breach</th><th>Daily-loss breach</th><th>Max acct DD</th><th>Max daily loss</th>
            <th>Time to target</th><th>Time to breach</th><th>Violation</th></tr></thead>
          <tbody>{sim.accounts.map(({ summary: a }) => (
            <tr key={a.account_id} data-testid={`prop-acc-${a.account_id}`}>
              <td>{a.account_id}{a.account_start && <div className="small muted">from {a.account_start.slice(0, 16)}</div>}</td>
              <td><Mono>{a.prop_config_id}</Mono></td><td><Badge tone={tone(a.status)}>{a.status}</Badge></td>
              <td>{usd(a.starting_balance)}</td><td>{usd(a.target_usd)}</td><td>{usd(a.ending_balance)}</td><td>{usd(a.net_pnl_usd)}</td><td>{fmt(a.net_r)}</td>
              <td>{a.trade_count}{a.trades_not_processed ? <span className="small muted"> (+{a.trades_not_processed} after end)</span> : null}</td>
              <td>{a.trading_days}</td><td>{yes(a.profit_target_reached)}</td><td>{yes(a.drawdown_breach)}</td><td>{yes(a.daily_loss_breach)}</td>
              <td>{usd(a.max_drawdown_usd_closed)}{a.max_drawdown_usd_intratrade_bound != null && <div className="small muted">bound {usd(a.max_drawdown_usd_intratrade_bound)}</div>}</td>
              <td>{usd(a.max_daily_loss_usd_closed)}{a.max_daily_loss_usd_intratrade_bound != null && <div className="small muted">bound {usd(a.max_daily_loss_usd_intratrade_bound)}</div>}</td>
              <td className="small">{a.time_to_target ? `${a.time_to_target.trading_days} d · ${a.time_to_target.at.slice(0, 16)}` : "—"}</td>
              <td className="small">{a.time_to_breach ? `${a.time_to_breach.trading_days} d · ${a.time_to_breach.at.slice(0, 16)}` : "—"}</td>
              <td className="small">{a.violation_reason ?? (a.incomplete_reasons.join("; ") || "—")}</td></tr>))}
          </tbody></table></TableWrap>
      </Card>
      {sim.accounts.map((acc) => <AccountDetail key={acc.summary.account_id} acc={acc} />)}
      <Card title="Lineage"><KeyValues rows={[["Simulation", <Mono>{sim.simulation_id}</Mono>], ["Recorded", yes(sim.recorded)],
        ["Strategy", <a href={href(`/strategies/${L.strategy_id}`)}><Mono>{L.strategy_id}</Mono></a>], ["Definition hash", <Mono>{L.definition_hash ?? L.definition_hash_note}</Mono>],
        ["Dataset", <Mono>{L.dataset_id}</Mono>], ["Provider / instrument / TF", `${L.provider} / ${L.instrument} / ${L.timeframe}`],
        ["Source period", `${L.source_period?.dataset_start} → ${L.source_period?.dataset_end}`], ["Cost profile", `${L.cost_profile} (${L.cost_status})`],
        ["Trades hash (verified)", <Mono>{String(L.trades_hash).slice(0, 16)}</Mono>], ["Ordering", L.ordering], ["Simulator", L.simulator_version]]} /></Card>
    </div>
  );
}

// ------------------------------------------------------------------ simulated paths + distributions (display only)
function PropOverview({ sim }: { sim: PropSimulation }) {
  const accs = sim.accounts.map((x) => x.summary);
  const nA = accs.length || 1;
  const statuses: Record<string, number> = {};
  for (const a of accs) statuses[a.status] = (statuses[a.status] ?? 0) + 1;
  const keys = Object.keys(statuses);
  const days = accs.map((a) => a.time_to_target?.trading_days).filter((x): x is number => typeof x === "number");
  return (
    <Card title={<>Simulated evaluation summary <Scope kind="sim">Simulation</Scope></>} testId="prop-overview">
      <Banner tone="warn">A replay of one stored backtest's trades through the stated rule set. A simulated pass is not a prediction of passing a real
        evaluation or of being funded; real accounts face fills, rules and discretion this replay does not model.</Banner>
      <div className="kpis" style={{ marginTop: 10 }}>
        <Kpi label="Accounts simulated" value={String(accs.length)} />
        <Kpi label="Survived (no breach)" value={`${accs.filter((a) => a.survived).length} / ${accs.length}`} meter={accs.filter((a) => a.survived).length / nA} />
        <Kpi label="Profit target reached" value={`${accs.filter((a) => a.profit_target_reached).length} / ${accs.length}`}
          meter={accs.filter((a) => a.profit_target_reached).length / nA} />
        <Kpi label="Drawdown breaches" value={String(accs.filter((a) => a.drawdown_breach).length)} tone={accs.some((a) => a.drawdown_breach) ? "neg" : ""} />
        <Kpi label="Daily-loss breaches" value={String(accs.filter((a) => a.daily_loss_breach).length)} tone={accs.some((a) => a.daily_loss_breach) ? "neg" : ""} />
        <Kpi label="Payout eligible (per rules)" value={String(accs.filter((a) => a.payout_eligible).length)}
          sub={accs.some((a) => a.payout_eligible == null) ? "not defined by some rule sets" : undefined} />
        <Kpi label="Days to target (median)" value={days.length ? String(days.sort((x, y) => x - y)[Math.floor(days.length / 2)]) : "—"} sub="trading days" />
      </div>
      {accs.length > 1 && <div style={{ marginTop: 10 }}><h4>Outcome distribution</h4>
        <BarChart categories={keys} unit="accounts" signed={false} series={[{ id: "n", label: "Accounts", values: keys.map((k) => statuses[k]), color: "var(--c2)" }]} /></div>}
    </Card>
  );
}

function AccountChart({ acc }: { acc: PropAccountResult }) {
  const p = acc.progression as Record<string, number | string | null>[];
  if (!p.length) return null;
  const a = acc.summary;
  const target = a.target_usd != null ? a.starting_balance + a.target_usd : null;
  const x = p.map((row, i) => String(row.exit_ts ?? `#${i + 1}`));
  return (
    <div className="grid2" style={{ marginBottom: 10 }}>
      <div><h4>Simulated balance path <Scope kind="sim" /></h4>
        <LineChart x={x} unit="USD" height={200} series={[
          { id: "bal", label: "Balance", values: p.map((r) => (typeof r.balance === "number" ? r.balance : null)) },
          { id: "floor", label: "Drawdown floor", values: p.map((r) => (typeof r.drawdown_floor === "number" ? r.drawdown_floor : null)), color: "var(--c-neg)", dashed: true },
          ...(target != null ? [{ id: "tgt", label: "Profit target", values: p.map(() => target), color: "var(--c3)", dashed: true }] : [])]} /></div>
      <div><h4>Drawdown trajectory <Scope kind="sim" /></h4>
        <LineChart x={x} unit="USD" height={200} series={[
          { id: "dd", label: "Drawdown from peak", values: p.map((r) => (typeof r.drawdown === "number" ? -Math.abs(r.drawdown) : null)), area: true, color: "var(--c-neg)" },
          { id: "head", label: "Drawdown headroom", values: p.map((r) => (typeof r.drawdown_headroom === "number" ? r.drawdown_headroom : null)), color: "var(--c2)" }]} /></div>
    </div>
  );
}

function AccountDetail({ acc }: { acc: PropAccountResult }) {
  const [open, setOpen] = useState(false);
  const a = acc.summary;
  const cols = ["trade_no", "day", "entry_ts", "exit_ts", "contracts", "net_usd", "balance", "peak_balance", "drawdown", "intratrade_low_bound",
    "day_pnl", "daily_loss_headroom", "drawdown_floor", "drawdown_headroom", "target_progress", "trading_days", "status", "skipped"];
  return (
    <Card title={<>Account {a.account_id} · <Badge tone={tone(a.status)}>{a.status}</Badge></>}
      actions={<Button small onClick={() => setOpen(!open)} testId={`prop-detail-${a.account_id}`}>{open ? "Hide progression" : "Show progression"}</Button>}>
      <AccountChart acc={acc} />
      {a.violations.length ? (
        <TableWrap testId={`prop-violations-${a.account_id}`}><table>
          <thead><tr><th>Rule</th><th>At</th><th>Trade</th><th>Detection</th><th>Detail</th></tr></thead>
          <tbody>{a.violations.map((v, i) => <tr key={i}><td><Badge tone="error">{v.rule}</Badge></td><td className="small">{v.at}</td>
            <td>{v.trade_no}</td><td>{v.detection}</td><td className="small">{v.detail}</td></tr>)}</tbody></table></TableWrap>
      ) : <p className="muted small">No rule violations.</p>}
      <p className="muted small">Detection: {a.detection} · trades crossing the daily reset: {a.trades_crossing_reset} · force-closed at end of data:
        {" "}{a.trades_end_of_data} · skipped after a daily-loss pause: {a.trades_skipped_daily_loss_pause}
        {a.payout_eligible != null && <> · payout eligible (per rule set): {yes(a.payout_eligible)}</>}
        {a.best_day_share_of_profit != null && <> · best-day share of profit: {a.best_day_share_of_profit.toFixed(3)}</>}</p>
      {open && <>
        <h3>Days</h3>
        <TableWrap><table><thead><tr><th>Day</th><th>Start bal.</th><th>P&L</th><th>Worst P&L bound</th><th>Trades</th><th>End bal.</th></tr></thead>
          <tbody>{acc.days.map((d, i) => <tr key={i}><td>{fmt(d.day)}</td><td>{usd(d.start_balance)}</td><td>{usd(d.pnl)}</td>
            <td>{usd(d.worst_pnl_bound)}</td><td>{fmt(d.trades)}</td><td>{usd(d.end_balance)}</td></tr>)}</tbody></table></TableWrap>
        <h3>Per-trade progression</h3>
        <TableWrap testId={`prop-progression-${a.account_id}`}><table><thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
          <tbody>{acc.progression.map((p, i) => <tr key={i}>{cols.map((c) => <td key={c} className="mono small">
            {typeof p[c] === "number" && !["trade_no", "trading_days", "contracts"].includes(c) ? (p[c] as number).toFixed(c === "target_progress" ? 3 : 2) : fmt(p[c])}</td>)}</tr>)}</tbody></table></TableWrap>
      </>}
    </Card>
  );
}
