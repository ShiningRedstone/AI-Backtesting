import type { BatchDetail, BatchRow, FamilyDetail, RunAnalytics, RunDetail, RunRow } from "../api/types";
import { CostPanel, EquityPanels, MonteCarloPanel, PerformanceKpis, PerformancePanels, TradePanels } from "../components/analytics";
import { go, href, useRoute } from "../app/router";
import { useApi } from "../app/context";
import { datasetLabel, facetLabel, familyLabel, humanize, keyLabel, plainProse, statusLabel, strategyLabel, valueLabel } from "../app/labels";
import { LineageTable, LineageTree, MetricsView, SYNTHETIC_NOTICE, VariationResults } from "../components/strategy";
import { EquityChart, SCOPE, ScopeBadge } from "../components/strategy/lab";
import { ChooseWorkspaceLink } from "../components/workspace";
import { Badge, Banner, Button, Card, Empty, ErrorPanel, KeyValues, Loading, Mono, ObjectView, TableWrap, TechDetails, fmt, shortTime } from "../components/ui";

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
          <tbody>{rows.map(([f, n]) => <tr key={f}><td><a href={href(`/families/${f}`)} title={f}>{familyLabel(f)}</a></td><td>{n}</td></tr>)}</tbody>
        </table></TableWrap>)}
    </div>
  );
}

function FamilyPage({ id }: { id: string }) {
  const { data, error } = useApi<FamilyDetail>(`/api/families/${id}`, [id]);
  if (error) return <ErrorPanel error={error} title="Could not load this family" />;
  if (!data) return <Loading label="Loading family…" />;
  const nodes = data.instances.map((n) => ({ ...n, name: n.name }));
  return (
    <div className="page" data-testid="family-page">
      <header className="page-head"><div><h1 title={data.family_id}>{familyLabel(data.family_id, data.family.name)}</h1>
        <div className="subtitle">Strategy family{data.family.category ? <> · {humanize(data.family.category)}</> : null}</div></div></header>
      <Card title="Hypothesis">
        <p data-testid="family-hypothesis">{data.family.hypothesis || <span className="muted">No hypothesis recorded.</span>}</p>
        <p className="muted small">A hypothesis to be tested, not a claim. {data.instances.length} instance(s).</p>
        <TechDetails rows={[["Family ID", <Mono>{data.family_id}</Mono>]]} />
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
          <thead><tr><th>Batch</th><th>Base</th><th>Mode</th><th>Combinations</th><th>Unique</th><th>Duplicates</th><th>Same as base</th><th>Created</th></tr></thead>
          <tbody>{data.map((b) => (
            <tr key={b.batch_id}><td><a href={href(`/variations/${b.batch_id}`)} title={b.batch_id}>{humanize(b.spec_name || "Variation batch")}</a></td>
              <td><a href={href(`/strategies/${b.base_strategy_id}`)} title={b.base_strategy_id}>{strategyLabel(b.base_name)}</a></td><td>{humanize(b.mode)}</td>
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
      <header className="page-head"><h1 title={b.batch_id}>Variation batch{b.spec.name ? `: ${humanize(b.spec.name)}` : ""}</h1></header>
      <Card title="Reproducibility">
        <KeyValues rows={[["Base", <a href={href(`/strategies/${b.base.strategy_id}`)} title={b.base.strategy_id}>
            {strategyLabel(typeof b.base.definition?.name === "string" ? b.base.definition.name : "Base strategy")}</a>],
          ["Mode", humanize(b.spec.mode)], ["Maximum variants", fmt(b.spec.max_variants)], ["Generator version", b.generator_version],
          ["Compiler version", b.compiler_version], ["Strategy language version", fmt(b.dsl_version)], ["Created", shortTime(b.created_at)]]} />
        <TechDetails rows={[["Batch ID", <Mono>{b.batch_id}</Mono>], ["Base strategy ID", <Mono>{b.base.strategy_id}</Mono>],
          ["Base logic hash", <Mono>{b.base.logic_hash}</Mono>], ["Base definition hash", <Mono>{b.base.definition_hash}</Mono>]]} />
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
    <td key={k} className="mono">{typeof x[k] === "number" ? (x[k] as number).toFixed(k === "trade_count" ? 0 : 3) : valueLabel(x[k])}</td>);
  const table = (title: string, rows: Rows, first: (x: Record<string, unknown>) => string, testId: string) => (
    <div><h4>{title}</h4><TableWrap testId={testId}><table>
      <thead><tr><th /><th>Trades</th><th>Net R</th><th>Net R per trade</th><th>Profit factor</th><th>Win rate</th></tr></thead>
      <tbody>{rows.rows.map((x, i) => <tr key={i}><td>{first(x)}</td>{cells(x)}</tr>)}</tbody></table></TableWrap>
      {rows.note && <p className="muted small">{rows.note}</p>}</div>);
  const be = data.cost_sensitivity.breakeven_cost_multiplier;
  return (
    <Card title="Breakdowns (Phase 5 analytics of this run)" testId="run-analytics">
      {data.labels.map((l) => <p key={l} className="muted small">{l}</p>)}
      <div className="grid-cards">
        {table("Sessions", data.sessions, (x) => `${facetLabel("session", x.session)} ${fmt(x.window)}`, "run-sessions")}
        {table(`Entry hour (${(data.hours.timezone ?? "").replace(/_/g, " ")})`, data.hours, (x) => fmt(x.bucket), "run-hours")}
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
const causality = (cc: any) => (cc ? `${cc.passed ? "Passed" : "Failed"} (${fmt(cc.cuts_tested)} truncation points tested)` : "—");
/** A trade cell: enum-like lower-case values as words; numbers, times and ids as stored. */
const tradeCell = (v: unknown) => (typeof v === "string" && /^[A-Za-z0-9]+(_[A-Za-z0-9]+)+$/.test(v) ? valueLabel(v) : fmt(v));

function ProvenanceSection({ r, id }: { r: Record<string, any>; id: string }) {
  const d = r.dataset ?? {}, a = r.assumptions ?? {}, c = a.costs ?? {}, s = r.strategy ?? {}, cv = r.code_version ?? {};
  const quotes = c.spread_source === "quotes";
  return (
    <Card title="Integrity / provenance" testId="run-provenance">
      <p className="small muted" style={{ marginTop: 0 }}>Everything that produced this result. Re-running the same strategy definition on the
        same dataset content with the same config and code reproduces the same trades hash.</p>
      <div className="grid2">
        <KeyValues rows={[["Status", <Badge>{statusLabel(r.status)}</Badge>],
          ["Strategy", s.dsl?.name ? strategyLabel(s.dsl.name) : "—"],
          ["Parent strategy", s.parent_strategy_id ? <a href={href(`/strategies/${s.parent_strategy_id}`)} title={s.parent_strategy_id}>Open the parent version</a> : "—"],
          ["Dataset", <span title={d.dataset_id}>{datasetLabel(d.dataset_id)}</span>],
          ["Parent dataset", d.parent_dataset_id ? <span title={d.parent_dataset_id}>{datasetLabel(d.parent_dataset_id)}</span> : "—"],
          ["Period", `${String(d.start ?? "").slice(0, 16)} → ${String(d.end ?? "").slice(0, 16)}`],
          ["Instrument / provider / timeframe", `${plainProse(d.instrument)} / ${valueLabel(d.provider)} / ${facetLabel("timeframe", d.timeframe)}`]]} />
        <KeyValues rows={[["Software version", cv.app_version ? `${cv.app_version}${cv.dirty ? " (modified)" : ""}` : cv.dirty ? "modified" : "—"],
          ["Causality check", causality(r.causality_check)],
          ["Execution", quotes ? "directional BID/ASK quotes (long ASK→BID, short BID→ASK)" : `single series, spread ${humanize(c.spread_source ?? "fixed").toLowerCase()}`],
          ["Cost scenario", c.scenario || c.profile ? <span title={c.scenario || c.profile}>{humanize(c.scenario || c.profile)}</span> : "—"],
          ["Cost status", <Badge tone={a.cost_status === "assumed" ? "warn" : "neutral"}>{valueLabel(a.cost_status)}</Badge>],
          ["Cost basis", <span className="small">{c.basis || "—"}</span>], ["Seed", String(r.seed ?? "—")]]} />
      </div>
      <TechDetails testId="run-provenance-tech" rows={[["Run ID", <Mono>{id}</Mono>], ["Strategy ID", <Mono>{s.strategy_id}</Mono>],
        ["Logic hash", <Mono>{s.dsl?.logic_hash ?? "—"}</Mono>], ["Definition hash", <Mono>{s.dsl?.definition_hash ?? "—"}</Mono>],
        ["Parent strategy ID", s.parent_strategy_id ? <Mono>{s.parent_strategy_id}</Mono> : null],
        ["Dataset ID", <Mono>{d.dataset_id}</Mono>], ["Parent dataset ID", d.parent_dataset_id ? <Mono>{d.parent_dataset_id}</Mono> : null],
        ["Dataset content hash", <Mono>{String(d.content_hash ?? "—")}</Mono>], ["Config hash", <Mono>{String(r.config_hash)}</Mono>],
        ["Code commit", <Mono>{String(cv.git_commit ?? "—")}{cv.dirty ? " (modified)" : ""}</Mono>],
        ["Source hash", <Mono>{String(cv.source_sha256 ?? "—")}</Mono>], ["Trades hash", <Mono>{String(r.trades_hash)}</Mono>],
        ["Cost scenario ID", c.scenario || c.profile ? <Mono>{c.scenario || c.profile}</Mono> : null]]} />
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
        <tr key={r.run_id}><td><a href={href(`/results/${r.run_id}`)} title={r.run_id}>Open run</a></td><td className="small">{shortTime(r.created_at)}</td>
          <td><a href={href(`/strategies/${r.strategy_id}`)} title={r.strategy_id}>{strategyLabel(r.strategy_name ?? "Unnamed strategy")}</a></td>
          <td title={r.dataset_id}>{datasetLabel(r.dataset_id)}</td>
          <td><Badge>{statusLabel(r.status)}</Badge></td><td>{fmt(r.headline_metrics.trade_count)}</td><td>{valueLabel(r.headline_metrics.sample_label)}</td></tr>))}
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
      <header className="page-head"><div><h1 title={id}>Run of {r.strategy?.dsl?.name ? strategyLabel(r.strategy.dsl.name) : "a strategy"}
        <span className="muted small"> · {shortTime(r.created_at)}</span></h1>
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
        <KeyValues rows={[["Status", <Badge>{statusLabel(r.status)}</Badge>],
          ["Strategy", r.strategy?.dsl?.name ? <span title={r.strategy?.strategy_id}>{strategyLabel(r.strategy.dsl.name)}</span> : "—"],
          ["Dataset", <span title={r.dataset?.dataset_id}>{datasetLabel(r.dataset?.dataset_id)}</span>], ["Created", shortTime(r.created_at)],
          ["Causality check", causality(r.causality_check)], ["Notes", r.notes ? valueLabel(r.notes) : null], ["Disclaimer", r.disclaimer]]} />
        <TechDetails rows={[["Run ID", <Mono>{id}</Mono>], ["Strategy ID", <Mono>{r.strategy?.strategy_id}</Mono>],
          ["Dataset ID", <Mono>{r.dataset?.dataset_id}</Mono>], ["Trades hash", <Mono>{r.trades_hash}</Mono>],
          ["Code commit", <Mono>{String(r.code_version?.git_commit ?? "—")}</Mono>], ["Config hash", <Mono>{String(r.config_hash)}</Mono>]]} />
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
      <Card title="Assumptions"><ObjectView value={r.assumptions} />
        <TechDetails summary="Assumptions (technical)"><pre className="code">{JSON.stringify(r.assumptions, null, 2)}</pre></TechDetails></Card>
      <Card title={`Trades (${data.trades_shown} of ${data.n_trades})`}>
        {data.trades.length ? <TableWrap><table>
          <thead><tr>{cols.map((c) => <th key={c} title={c}>{keyLabel(c)}</th>)}</tr></thead>
          <tbody>{data.trades.map((t, i) => <tr key={i}>{cols.map((c) => <td key={c} className="mono small">{tradeCell(t[c])}</td>)}</tr>)}</tbody>
        </table></TableWrap> : <Empty>No trades.</Empty>}
      </Card>
    </div>
  );
}
