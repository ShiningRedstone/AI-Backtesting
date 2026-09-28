import type { BatchDetail, BatchRow, FamilyDetail, RunDetail, RunRow } from "../api/types";
import { href, useRoute } from "../app/router";
import { useApi } from "../app/context";
import { LineageTable, LineageTree, MetricsView, SYNTHETIC_NOTICE, VariationResults } from "../components/strategy";
import { Badge, Banner, Card, Empty, ErrorPanel, KeyValues, Loading, Mono, TableWrap, fmt, shortTime } from "../components/ui";

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
      <Banner tone="info">Single backtests recorded in the run registry (status IN_SAMPLE). Listed chronologically — no ranking, no
        “best strategy”, no significance verdict. Analytics, OOS and robustness arrive in Phases 5–6.</Banner>
      <Card title="Research runs">{real.length ? table(real, "runs-real") : <Empty>No research runs yet.</Empty>}</Card>
      <Card title="Synthetic demonstrations">
        <p className="muted small">{SYNTHETIC_NOTICE} Kept separate from research runs.</p>
        {demo.length ? table(demo, "runs-demo") : <Empty>None.</Empty>}
      </Card>
    </div>
  );
}

function RunPage({ id }: { id: string }) {
  const { data, error } = useApi<RunDetail>(`/api/results/${id}`, [id]);
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <Loading label="Loading run…" />;
  const r = data.record as Record<string, any>;
  const cols = data.trades.length ? Object.keys(data.trades[0]).filter((c) => c !== "run_id") : [];
  return (
    <div className="page">
      <header className="page-head"><h1>Run <Mono>{id}</Mono></h1></header>
      {data.synthetic && <Banner tone="demo" testId="synthetic-banner"><b>{SYNTHETIC_NOTICE}</b></Banner>}
      <Card title="Record">
        <KeyValues rows={[["Status", <Badge>{r.status}</Badge>], ["Strategy", <Mono>{r.strategy?.strategy_id}</Mono>],
          ["Dataset", <Mono>{r.dataset?.dataset_id}</Mono>], ["Created", shortTime(r.created_at)],
          ["Causality check", r.causality_check ? `passed=${r.causality_check.passed}, cuts=${r.causality_check.cuts_tested}` : "—"],
          ["Trades hash", <Mono>{r.trades_hash}</Mono>], ["Code", <Mono>{r.code_version?.git_commit?.slice(0, 10)}</Mono>],
          ["Config hash", <Mono>{String(r.config_hash).slice(0, 12)}</Mono>], ["Notes", r.notes], ["Disclaimer", r.disclaimer]]} />
      </Card>
      <Card title="Headline metrics"><MetricsView metrics={r.headline_metrics ?? {}} /></Card>
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
