import type { BatchDetail, BatchRow, FamilyDetail, RunAnalytics, RunDetail, RunRow } from "../api/types";
import { CostPanel, EquityPanels, MonteCarloPanel, PerformanceKpis, PerformancePanels, TradePanels } from "../components/analytics";
import { go, href, useRoute } from "../app/router";
import { useApi } from "../app/context";
import { LineageTable, LineageTree, MetricsView, SYNTHETIC_NOTICE, VariationResults } from "../components/strategy";
import { EquityChart, SCOPE, ScopeBadge } from "../components/strategy/lab";
import { ChooseWorkspaceLink } from "../components/workspace";
import { Badge, Banner, Button, Card, Empty, ErrorPanel, KeyValues, Loading, Mono, TableWrap, fmt, shortTime } from "../components/ui";

// =========================================================================== families
export function FamiliesPage() {
  const route = useRoute();
  return route.parts[1] ? <FamilyPage id={route.parts[1]} /> : <FamilyList />;
}

function FamilyList() {
  const { data, error } = useApi<Record<string, number>>("/api/families");
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <Loading label="Loading families…" />;
  const rows = Object.entries(data);
  return (
    <div className="page">
      <header className="page-head"><h1>Strategy Families</h1></header>
      <p className="muted">A family is a market hypothesis. Its instances are concrete canonical definitions testing that hypothesis.</p>
      {!rows.length ? <Empty>No families yet — families appear when strategies are saved.</Empty> : (
        <TableWrap testId="families-table"><table>
          <thead><tr><th>Family</th><th>Instances</th></tr></thead>
          <tbody>{rows.map(([f, n]) => <tr key={f}><td><a href={href(`/families/${f}`)}>{f}</a></td><td>{n}</td></tr>)}</tbody>
        </table></TableWrap>)}
    </div>
  );
}

function FamilyPage({ id }: { id: string }) {
  const { data, error } = useApi<FamilyDetail>(`/api/families/${id}`, [id]);
  if (error) return <ErrorPanel error={error} title={`Could not load family ${id}`} />;
  if (!data) return <Loading label="Loading family…" />;
  const nodes = data.instances.map((n) => ({ ...n, name: n.name }));
  return (
    <div className="page" data-testid="family-page">
      <header className="page-head"><div><h1>{data.family.name || data.family_id}</h1>
        <div className="subtitle">Family <Mono>{data.family_id}</Mono>{data.family.category ? <> · {data.family.category}</> : null}</div></div></header>
      <Card title="Hypothesis">
        <p data-testid="family-hypothesis">{data.family.hypothesis || <span className="muted">No hypothesis recorded.</span>}</p>
        <p className="muted small">A hypothesis to be tested, not a claim. {data.instances.length} instance(s).</p>
      </Card>
      <Card title="Lineage"><LineageTree nodes={nodes} /></Card>
      <Card title="Instances (table)"><LineageTable nodes={nodes} /></Card>
    </div>
  );
}

// =========================================================================== variation batches
export function VariationsPage() {
  const route = useRoute();
  return route.parts[1] ? <BatchPage id={route.parts[1]} /> : <BatchList />;
}

function BatchList() {
  const { data, error } = useApi<BatchRow[]>("/api/variation-batches");
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <Loading label="Loading batches…" />;
  return (
    <div className="page">
      <header className="page-head"><h1>Variation Batches</h1></header>
      <p className="muted">Generate variations from a saved strategy (Strategy → Generate Variations). Batches are reproducible from their records.</p>
      {!data.length ? <Empty>No variation batches yet. Open a <a href={href("/strategies")}>saved strategy</a> to generate one.</Empty> : (
        <TableWrap testId="batches-table"><table>
          <thead><tr><th>Batch</th><th>Base</th><th>Spec</th><th>Mode</th><th>Combinations</th><th>Unique</th><th>Duplicates</th><th>Same as base</th><th>Created</th></tr></thead>
          <tbody>{data.map((b) => (
            <tr key={b.batch_id}><td><a href={href(`/variations/${b.batch_id}`)}><Mono>{b.batch_id}</Mono></a></td>
              <td><a href={href(`/strategies/${b.base_strategy_id}`)}>{b.base_name}</a></td><td>{b.spec_name}</td><td>{b.mode}</td>
              <td>{b.combinations}</td><td>{b.generated}</td><td>{b.duplicates}</td><td>{b.same_as_base}</td><td className="small">{shortTime(b.created_at)}</td></tr>))}
          </tbody>
        </table></TableWrap>)}
    </div>
  );
}

function BatchPage({ id }: { id: string }) {
  const { data: b, error } = useApi<BatchDetail>(`/api/variation-batches/${id}`, [id]);
  if (error) return <ErrorPanel error={error} />;
  if (!b) return <Loading label="Loading batch…" />;
  return (
    <div className="page">
      <header className="page-head"><h1>Batch <Mono>{b.batch_id}</Mono></h1></header>
      <Card title="Reproducibility">
        <KeyValues rows={[["Base", <a href={href(`/strategies/${b.base.strategy_id}`)}><Mono>{b.base.strategy_id}</Mono></a>],
          ["Mode", b.spec.mode], ["Max variants", fmt(b.spec.max_variants)], ["Generator", b.generator_version],
          ["Compiler", b.compiler_version], ["DSL version", fmt(b.dsl_version)], ["Created", shortTime(b.created_at)]]} />
      </Card>
      <VariationResults result={{ batch_id: b.batch_id, base_strategy_id: b.base.strategy_id, combinations: b.combinations,
        generated: b.generated, duplicates: b.duplicates, same_as_base: b.same_as_base,
        varied: b.spec.dimensions.map((d) => d.parameter), rows: b.children_detail }} />
    </div>
  );
}

// =========================================================================== run analytics
function RunAnalytics({ runId }: { runId: string }) {
  type Rows = { rows: Record<string, unknown>[]; note?: string; timezone?: string; breakeven_cost_multiplier?: number | null };
  const { data, error } = useApi<{ labels: string[]; sessions: Rows; hours: Rows; cost_sensitivity: Rows }>(`/api/results/report?run_ids=${runId}`, [runId]);
  if (error) return <ErrorPanel error={error} title="Analytics unavailable" />;
  if (!data) return <Loading label="Computing analytics…" />;
  const cells = (x: Record<string, unknown>) => ["trade_count", "net_r", "expectancy_r", "profit_factor", "win_rate"].map((k) =>
    <td key={k} className="mono">{typeof x[k] === "number" ? (x[k] as number).toFixed(k === "trade_count" ? 0 : 3) : fmt(x[k])}</td>);
  const table = (title: string, rows: Rows, first: (x: Record<string, unknown>) => string, testId: string) => (
    <div><h4>{title}</h4><TableWrap testId={testId}><table>
      <thead><tr><th /><th>Trades</th><th>Net R</th><th>Expectancy R</th><th>PF</th><th>Win rate</th></tr></thead>
      <tbody>{rows.rows.map((x, i) => <tr key={i}><td>{first(x)}</td>{cells(x)}</tr>)}</tbody></table></TableWrap>
      {rows.note && <p className="muted small">{rows.note}</p>}</div>);
  const be = data.cost_sensitivity.breakeven_cost_multiplier;
  return (
    <Card title="Breakdowns (Phase 5 analytics of this run)" testId="run-analytics">
      {data.labels.map((l) => <p key={l} className="muted small">{l}</p>)}
      <div className="grid-cards">
        {table("Sessions", data.sessions, (x) => `${fmt(x.session)} ${fmt(x.window)}`, "run-sessions")}
        {table(`Entry hour (${data.hours.timezone ?? ""})`, data.hours, (x) => fmt(x.bucket), "run-hours")}
        <div>{table("Cost sensitivity (× stated costs)", { ...data.cost_sensitivity,
          rows: data.cost_sensitivity.rows.map((x) => ({ ...x, trade_count: x.trades })) }, (x) => `${fmt(x.cost_multiplier)}×`, "run-costs")}
          <p className="small">Breakeven cost multiple: <b>{typeof be === "number" ? be.toFixed(3) : "—"}</b>
            {typeof be === "number" && be <= 0 ? " (gross R is not positive: no cost level makes it profitable)" : ""}</p></div>
      </div>
    </Card>
  );
}

// =========================================================================== integrity / provenance
/* eslint-disable @typescript-eslint/no-explicit-any */
function ProvenanceSection({ r, id }: { r: Record<string, any>; id: string }) {
  const d = r.dataset ?? {}, a = r.assumptions ?? {}, c = a.costs ?? {}, s = r.strategy ?? {}, cv = r.code_version ?? {};
  const quotes = c.spread_source === "quotes";
  return (
    <Card title="Integrity / provenance" testId="run-provenance">
      <p className="small muted" style={{ marginTop: 0 }}>Everything that produced this result. Re-running the same strategy definition on the
        same dataset content with the same config and code reproduces the same trades hash.</p>
      <div className="grid2">
        <KeyValues rows={[["Run", <Mono>{id}</Mono>], ["Status", <Badge>{r.status}</Badge>],
          ["Strategy", <><Mono>{s.strategy_id}</Mono> {s.dsl?.name ? <span className="small muted">{s.dsl.name}</span> : null}</>],
          ["Logic hash", <Mono>{s.dsl?.logic_hash ?? "—"}</Mono>], ["Definition hash", <Mono>{s.dsl?.definition_hash ?? "—"}</Mono>],
          ["Parent strategy", s.parent_strategy_id ? <Mono>{s.parent_strategy_id}</Mono> : "—"],
          ["Dataset", <Mono>{d.dataset_id}</Mono>], ["Parent dataset", d.parent_dataset_id ? <Mono>{d.parent_dataset_id}</Mono> : "—"],
          ["Dataset content hash", <Mono title={String(d.content_hash ?? "—")}>{String(d.content_hash ?? "—").slice(0, 24)}</Mono>],
          ["Period", `${String(d.start ?? "").slice(0, 16)} → ${String(d.end ?? "").slice(0, 16)}`],
          ["Instrument / provider / TF", `${d.instrument} / ${d.provider} / ${d.timeframe}`]]} />
        <KeyValues rows={[["Config hash", <Mono title={String(r.config_hash)}>{String(r.config_hash).slice(0, 24)}</Mono>],
          ["Code", <Mono>{cv.app_version ? `v${cv.app_version} · ` : ""}{String(cv.git_commit ?? "—").slice(0, 12)}{cv.dirty ? " (modified)" : ""}</Mono>],
          ["Source hash", <Mono title={String(cv.source_sha256 ?? "—")}>{String(cv.source_sha256 ?? "—").slice(0, 16)}</Mono>],
          ["Trades hash", <Mono title={String(r.trades_hash)}>{String(r.trades_hash).slice(0, 24)}</Mono>],
          ["Causality check", r.causality_check ? `passed=${r.causality_check.passed}, cuts=${r.causality_check.cuts_tested}` : "—"],
          ["Execution", quotes ? "directional BID/ASK quotes (long ASK→BID, short BID→ASK)" : `single series, spread ${c.spread_source ?? "fixed"}`],
          ["Cost scenario", <Mono>{c.scenario || c.profile || "—"}</Mono>], ["Cost status", <Badge tone={a.cost_status === "assumed" ? "warn" : "neutral"}>{a.cost_status}</Badge>],
          ["Cost basis", <span className="small">{c.basis || "—"}</span>], ["Seed", String(r.seed ?? "—")]]} />
      </div>
    </Card>
  );
}
/* eslint-enable @typescript-eslint/no-explicit-any */

// =========================================================================== results
export function ResultsPage() {
  const route = useRoute();
  return route.parts[1] ? <RunPage id={route.parts[1]} /> : <RunList />;
}

function RunList() {
  const { data, error } = useApi<RunRow[]>("/api/results");
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <Loading label="Loading runs…" />;
  const real = data.filter((r) => !r.synthetic), demo = data.filter((r) => r.synthetic);
  const table = (rows: RunRow[], testId: string) => (
    <TableWrap testId={testId}><table>
      <thead><tr><th>Run</th><th>Created</th><th>Strategy</th><th>Dataset</th><th>Status</th><th>Trades</th><th>Sample</th></tr></thead>
      <tbody>{rows.map((r) => (
        <tr key={r.run_id}><td><a href={href(`/results/${r.run_id}`)}><Mono>{r.run_id}</Mono></a></td><td className="small">{shortTime(r.created_at)}</td>
          <td><a href={href(`/strategies/${r.strategy_id}`)}>{r.strategy_name ?? r.strategy_id}</a></td><td><Mono>{r.dataset_id}</Mono></td>
          <td><Badge>{r.status}</Badge></td><td>{fmt(r.headline_metrics.trade_count)}</td><td>{fmt(r.headline_metrics.sample_label)}</td></tr>))}
      </tbody>
    </table></TableWrap>);
  return (
    <div className="page">
      <header className="page-head"><h1>Results</h1></header>
      <Banner tone="info">Runs recorded in the run registry, each with its own status (in-sample, out-of-sample, walk-forward). Listed
        chronologically — no ranking, no “best strategy”, no significance verdict. Open a run for analytics, provenance and robustness.</Banner>
      <Card title="Research runs">{real.length ? table(real, "runs-real") : <Empty>No stored research runs in this workspace. <ChooseWorkspaceLink /></Empty>}</Card>
      <Card title="Synthetic demonstrations">
        <p className="muted small">{SYNTHETIC_NOTICE} Kept separate from research runs.</p>
        {demo.length ? table(demo, "runs-demo") : <Empty>None.</Empty>}
      </Card>
    </div>
  );
}

function RunPage({ id }: { id: string }) {
  const { data, error } = useApi<RunDetail>(`/api/results/${id}`, [id]);
  const an = useApi<RunAnalytics>(`/api/results/${id}/analytics`, [id]);
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <Loading label="Loading run…" />;
  const r = data.record as Record<string, any>;
  const cols = data.trades.length ? Object.keys(data.trades[0]).filter((c) => c !== "run_id") : [];
  return (
    <div className="page">
      <header className="page-head"><div><h1>Run <Mono>{id}</Mono></h1>
        <div className="subtitle"><ScopeBadge status={r.status} /> {SCOPE[r.status]?.note}</div></div>
        <div className="actions">
          <Button small onClick={() => go(`/strategies/${r.strategy?.strategy_id}?tab=research`)}>Open strategy in Lab</Button>
          <Button small onClick={() => go(`/compare?source=lineage&id=${r.strategy?.strategy_id}`)}>Compare lineage</Button>
          <Button small onClick={() => go(`/strategies/${r.strategy?.strategy_id}?tab=validate&dataset=${r.dataset?.parent_dataset_id ?? r.dataset?.dataset_id}`)}>Validate</Button>
          <Button small onClick={() => go(`/prop?run=${id}`)} testId="run-prop">Prop simulation</Button>
        </div></header>
      {data.synthetic && <Banner tone="demo" testId="synthetic-banner"><b>{SYNTHETIC_NOTICE}</b></Banner>}
      {an.data?.run.holdout && <Banner tone="info" testId="holdout-run-banner"><b>Protocol holdout evaluation.</b> This run is the one
        permitted look at the locked holdout for this candidate. Its formal outcome (criteria met / not met, never “accepted”) is in the holdout
        ledger; the statistics below are descriptive.</Banner>}
      {an.data && an.data.n_trades > 0 && <PerformanceKpis a={an.data} />}
      <ProvenanceSection r={r} id={id} />
      <Card title="Record">
        <KeyValues rows={[["Status", <Badge>{r.status}</Badge>], ["Strategy", <Mono>{r.strategy?.strategy_id}</Mono>],
          ["Dataset", <Mono>{r.dataset?.dataset_id}</Mono>], ["Created", shortTime(r.created_at)],
          ["Causality check", r.causality_check ? `passed=${r.causality_check.passed}, cuts=${r.causality_check.cuts_tested}` : "—"],
          ["Trades hash", <Mono>{r.trades_hash}</Mono>], ["Code", <Mono title={String(r.code_version?.git_commit ?? "") || undefined}>{r.code_version?.git_commit?.slice(0, 10)}</Mono>],
          ["Config hash", <Mono title={String(r.config_hash)}>{String(r.config_hash).slice(0, 12)}</Mono>], ["Notes", r.notes], ["Disclaimer", r.disclaimer]]} />
      </Card>
      <Card title="Headline metrics"><MetricsView metrics={r.headline_metrics ?? {}} /></Card>
      <Card title="Equity and drawdown (net R)"><EquityChart runId={id} /></Card>
      <RunAnalytics runId={id} />
      {an.error ? <ErrorPanel error={an.error} title="Detailed analytics unavailable" /> : !an.data ? <Loading label="Computing detailed analytics…" />
        : an.data.n_trades > 0 && <>
          <h3>Performance breakdowns</h3><PerformancePanels a={an.data} />
          <h3>Equity, drawdown and rolling expectancy</h3><EquityPanels a={an.data} />
          <h3>Trade behaviour</h3><TradePanels a={an.data} />
          <h3>Robustness (descriptive)</h3>
          <div className="panel-grid"><CostPanel a={an.data} /><MonteCarloPanel a={an.data} /></div>
        </>}
      <Card title="Assumptions"><pre className="code">{JSON.stringify(r.assumptions, null, 2)}</pre></Card>
      <Card title={`Trades (${data.trades_shown} of ${data.n_trades})`}>
        {data.trades.length ? <TableWrap><table>
          <thead><tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr></thead>
          <tbody>{data.trades.map((t, i) => <tr key={i}>{cols.map((c) => <td key={c} className="mono small">{fmt(t[c])}</td>)}</tr>)}</tbody>
        </table></TableWrap> : <Empty>No trades.</Empty>}
      </Card>
    </div>
  );
}
