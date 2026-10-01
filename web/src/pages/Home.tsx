import type { Overview } from "../api/types";
import { go, href } from "../app/router";
import { useApi } from "../app/context";
import { DatasetIdentity, ExecutionPanel, ProtocolPanel } from "../components/research";
import { UI_VERSION, useUpdates } from "../components/updates";
import { Badge, Banner, Button, Card, Empty, ErrorPanel, Kpi, Loading, Mono, Scope, ScopeOf, TableWrap, n, r, shortTime, signCls } from "../components/ui";
import { ChooseWorkspaceLink } from "../components/workspace";

export function HomePage() {
  const { data: o, error } = useApi<Overview>("/api/overview");
  const [upd] = useUpdates();
  if (error) return <div className="page"><header className="page-head"><h1>Home</h1></header><ErrorPanel error={error} title="Overview unavailable" /></div>;
  if (!o) return <div className="page"><header className="page-head"><h1>Home</h1></header><Loading label="Loading the research overview…" /></div>;
  const f = o.facts, st = f.runs_by_status;
  return (
    <div className="page" data-testid="home">
      <header className="page-head">
        <div><div className="eyebrow">Research terminal</div><h1>Home</h1>
          <div className="subtitle small">System facts and stored results. A positive backtest is a historical result under stated assumptions,
            never a verdict that a strategy is profitable.</div></div>
        <div className="actions">
          <Button kind="primary" onClick={() => go("/runs")} testId="qa-run-research">Run research</Button>
          <Button onClick={() => go("/explorer")}>Strategy explorer</Button>
          <Button onClick={() => go("/dashboard")}>Research dashboard</Button>
          <Button kind="primary" onClick={() => go("/builder?new=1")} testId="qa-create">New strategy</Button>
        </div>
      </header>

      {o.warnings.map((w) => <Banner key={w.text} tone={w.level === "warn" ? "warn" : "info"}>{w.text}</Banner>)}

      <section>
        <h3>System facts</h3>
        <div className="kpis" data-testid="home-facts">
          <Kpi label="Strategies" value={f.strategies.toLocaleString()} sub={`${f.families} families`} />
          <Kpi label="Stored runs" value={f.runs.toLocaleString()}
            sub={`${st.IN_SAMPLE ?? 0} in-sample · ${st.OUT_OF_SAMPLE ?? 0} OOS · ${st.WALK_FORWARD ?? 0} walk-fwd`} />
          <Kpi label="Research batches" value={f.searches.toLocaleString()} sub="search experiments" />
          <Kpi label="Variation batches" value={f.variation_batches.toLocaleString()} sub="controlled variations" />
          <Kpi label="AI generations" value={f.ai_generations.toLocaleString()} sub="proposal requests" />
          <Kpi label="Prop simulations" value={f.prop_simulations.toLocaleString()} sub={<Scope kind="sim" />} />
          <Kpi label="Datasets" value={f.datasets.toLocaleString()} sub={`store: ${f.store_backend}`} />
          <Kpi label="Version" value={`v${UI_VERSION}`} sub={upd?.available && !upd.skipped ? <span className="pos">update {upd.release?.version} available</span>
            : upd?.check.state === "error" ? `update check: ${upd.check.error?.code}` : "up to date or not checked"} />
        </div>
      </section>

      {o.protocols.length ? o.protocols.map((p) => <ProtocolPanel key={p.protocol_id} p={p} />)
        : <Banner tone="warn">No ACTIVE research protocol in this workspace ({o.protocol_records.length} protocol record(s)). Discovery evaluations
          are not governed by a locked holdout or a trial budget.</Banner>}

      <div className="grid-cards">
        <DatasetIdentity o={o} />
        <ExecutionPanel e={o.execution} />
      </div>

      <Card title={<>Latest research results <Scope kind="net" /><Scope kind="descriptive" /></>} testId="home-recent-runs"
        actions={<a className="small" href={href("/results")}>All results ›</a>}>
        {o.recent_runs.length ? <TableWrap><table className="dense">
          <thead><tr><th>Run</th><th>Scope</th><th>Strategy</th><th>Dataset</th><th className="r">Trades</th><th className="r">Net R/trade</th>
            <th className="r">Net R</th><th className="r">PF</th><th className="r">Max DD</th><th>Created</th></tr></thead>
          <tbody>{o.recent_runs.map((x) => (
            <tr key={x.run_id}>
              <td><a href={href(`/results/${x.run_id}`)}><Mono>{x.run_id}</Mono></a></td>
              <td><ScopeOf status={x.status} holdout={x.holdout} />{x.synthetic && <> <Scope kind="synthetic" /></>}</td>
              <td><a href={href(`/explorer?open=${x.strategy_id}`)}>{x.strategy_name ?? x.strategy_id}</a></td>
              <td className="small"><Mono>{x.dataset_id}</Mono></td>
              <td className="r num">{x.trade_count}</td>
              <td className={`r num ${signCls(x.expectancy_r)}`}>{r(x.expectancy_r)}</td>
              <td className={`r num ${signCls(x.net_r)}`}>{n(x.net_r, 1)}</td>
              <td className="r num">{n(x.profit_factor)}</td>
              <td className="r num">{n(x.max_drawdown_r, 1)}</td>
              <td className="small muted">{shortTime(x.created_at)}</td></tr>))}</tbody></table></TableWrap>
          : <Empty>No stored runs yet. <ChooseWorkspaceLink /></Empty>}
      </Card>

      <div className="grid-cards">
        <Card title="Recent experiments" actions={<a className="small" href={href("/research")}>Experiments ›</a>} testId="home-searches">
          {o.recent_searches.length ? <TableWrap><table className="dense">
            <thead><tr><th>Search</th><th>Status</th><th className="r">Trials</th><th className="r">Evaluated</th><th className="r">Failed</th><th>Protocol</th></tr></thead>
            <tbody>{o.recent_searches.map((s) => (
              <tr key={s.search_id}><td><a href={href(`/research/${s.search_id}`)}><Mono>{s.search_id}</Mono></a><div className="small muted">{shortTime(s.created_at)}</div></td>
                <td><Badge tone={s.status === "completed" ? "ok" : s.status === "failed" ? "error" : "neutral"}>{s.status}</Badge></td>
                <td className="r num">{s.n_trials}</td><td className="r num">{s.n_evaluated}</td><td className="r num">{s.n_failed}</td>
                <td className="small">{s.protocol_id ? <Mono>{s.protocol_id}</Mono> : <span className="muted">none</span>}</td></tr>))}</tbody></table></TableWrap>
            : <Empty>No research searches yet.</Empty>}
        </Card>
        <Card title="Candidates (shortlist tags and holdout ledger)" actions={<a className="small" href={href("/pipeline")}>Pipeline ›</a>} testId="home-candidates">
          {o.candidates.length ? <TableWrap><table className="dense">
            <thead><tr><th>Strategy</th><th>State</th><th>Protocol</th><th>Outcome</th></tr></thead>
            <tbody>{o.candidates.map((c, i) => (
              <tr key={i}><td><a href={href(`/explorer?open=${c.strategy_id}`)}><Mono>{c.strategy_id}</Mono></a></td>
                <td><Badge tone={c.status === "refused" || c.status === "failed" ? "error" : c.status === "completed" ? "info" : "neutral"}>{c.status}</Badge>
                  {c.reason_code && <div className="small muted">{c.reason_code}</div>}</td>
                <td className="small"><Mono>{c.protocol_id ?? "—"}</Mono></td>
                <td>{c.outcome ? <Badge tone={c.outcome === "HOLDOUT_CRITERIA_MET" ? "ok" : "warn"}>{c.outcome}</Badge> : <span className="muted small">—</span>}</td></tr>))}
            </tbody></table></TableWrap>
            : <Empty>No shortlisted or holdout candidates. A shortlist is a tag; holdout outcomes are "criteria met / not met", never "accepted".</Empty>}
        </Card>
      </div>

      <p className="small muted">{o.note}</p>
    </div>
  );
}
