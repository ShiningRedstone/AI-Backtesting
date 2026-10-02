import { useState } from "react";
import type { FieldPoint, GroupRow, ResearchDashboard } from "../api/types";
import { href } from "../app/router";
import { useApi } from "../app/context";
import { BarChart, HBars, Histogram } from "../components/charts";
import { ClusterList, ControlPanelView, ResultsOverviewSection, StrategyPanel } from "../components/results";
import { Banner, Button, Card, Drawer, Empty, ErrorPanel, Kpi, Loading, Scope, TableWrap, n, pct } from "../components/ui";
import { go } from "../app/router";
import { datasetLabel, facetLabel } from "../app/labels";
import type { ScopeKind } from "../components/ui";

const DIMS: [string, string][] = [["instrument", "Performance by market"], ["timeframe", "By timeframe"], ["session", "By session"],
  ["entry_type", "By entry type"], ["stop_type", "By stop methodology"], ["target_type", "By target methodology"],
  ["direction", "By direction"], ["family", "By strategy family"], ["source", "By discovery source"]];
const SCOPE_KIND: Record<string, ScopeKind> = { in_sample: "is", oos: "oos", walk_forward: "wf", any: "descriptive" };

export function DashboardPage() {
  const [scope, setScope] = useState("in_sample");
  const [inst, setInst] = useState(""), [ds, setDs] = useState(""), [fam, setFam] = useState("");
  const [synth, setSynth] = useState(false);
  const [table, setTable] = useState<Record<string, boolean>>({});
  const [open, setOpen] = useState<{ kind: "strategy" | "control"; id: string } | null>(null);
  const [cluster, setCluster] = useState<FieldPoint[] | null>(null);          // ADR-86: a grouped circle's strategies
  const qs = new URLSearchParams({ scope, ...(inst ? { instrument: inst } : {}), ...(ds ? { dataset_id: ds } : {}),
    ...(fam ? { family_id: fam } : {}), ...(synth ? { include_synthetic: "1" } : {}) }).toString();
  const { data: d, error, loading } = useApi<ResearchDashboard>(`/api/research/dashboard?${qs}`, [qs]);
  const sk = SCOPE_KIND[scope];
  const tags = <><Scope kind={sk} /><Scope kind="net" />{d?.includes_synthetic && <Scope kind="synthetic" />}</>;
  const panel = (key: string, title: string, rows: GroupRow[]) => (
    <Card key={key} className="resizable" title={<>{title} {tags}</>} testId={`dash-${key}`}
      actions={<button className="linklike small" onClick={() => setTable((t) => ({ ...t, [key]: !t[key] }))}>{table[key] ? "chart" : "table"}</button>}>
      {!rows.length ? <Empty>No runs in this scope.</Empty> : table[key] ? <TableWrap><table className="dense">
        <thead><tr><th>Group</th><th className="r">Runs</th><th className="r">Trades</th><th className="r">Net R/trade</th><th className="r">Gross R/trade</th>
          <th className="r">Median run exp.</th><th className="r">% runs &gt; 0</th></tr></thead>
        <tbody>{rows.map((g) => <tr key={g.group}><td>{facetLabel(key === "family" ? "family_id" : key, g.group)}</td><td className="r num">{g.runs}</td><td className="r num">{g.trades}</td>
          <td className="r num">{n(g.net_r_per_trade, 3)}</td><td className="r num">{n(g.gross_r_per_trade, 3)}</td>
          <td className="r num">{n(g.median_run_expectancy_r, 3)}</td><td className="r num">{pct(g.pct_runs_positive_net, 0)}</td></tr>)}</tbody></table></TableWrap>
        : <HBars rows={rows.map((g) => ({ label: facetLabel(key === "family" ? "family_id" : key, g.group), value: g.net_r_per_trade, note: `${g.runs} runs · ${g.trades} tr` }))} unit="net R per trade (trade-weighted)" />}
    </Card>);
  const cal = (key: "weekday" | "month" | "year", title: string) => {
    const rows = d?.calendar[key] ?? [];
    return <Card key={key} title={<>{title} {tags}<Scope kind="gross" /></>} testId={`dash-cal-${key}`}>
      {rows.length ? <BarChart categories={rows.map((x) => x.bucket)} unit="R/trade" sub={(i) => `${rows[i].trades} trades`}
        series={[{ id: "net", label: "Net R per trade", values: rows.map((x) => x.net_r_per_trade) },
          { id: "gross", label: "Gross R per trade", values: rows.map((x) => x.gross_r_per_trade) }]} /> : <Empty>No trades.</Empty>}
    </Card>;
  };
  return (
    <div className="page" data-testid="research-dashboard">
      <header className="page-head">
        <div><h1>Overview</h1></div>
      </header>
      <ResultsOverviewSection onOpen={(id) => { setCluster(null); setOpen({ kind: "strategy", id }); }}
        onOpenControl={(id) => { setCluster(null); setOpen({ kind: "control", id }); }} onOpenMany={(pts) => { setOpen(null); setCluster(pts); }} />
      <Drawer open={!!open || !!cluster} onClose={() => { setOpen(null); setCluster(null); }} testId="results-drawer"
        title={open?.kind === "control" ? "Random control" : open ? "Strategy" : `${cluster?.length ?? 0} strategies`}
        actions={<>{open && cluster && <Button small kind="ghost" onClick={() => setOpen(null)} testId="cluster-back">‹ Back to the list</Button>}
          {open?.kind === "strategy" ? <Button small onClick={() => go(`/strategies/${open.id}`)}>Open strategy page</Button> : null}</>}>
        {open ? (open.kind === "control" ? <ControlPanelView key={open.id} id={open.id} /> : <StrategyPanel key={open.id} id={open.id} />)
          : cluster && <ClusterList points={cluster} onOpen={(id) => setOpen({ kind: "strategy", id })} />}
      </Drawer>
      <header className="page-head" style={{ marginTop: 18 }}>
        <div><h2 style={{ margin: 0 }}>All stored backtests</h2></div>
        <div className="actions">
          <div className="segmented small" role="group" aria-label="scope">
            {[["in_sample", "In-sample"], ["oos", "Out-of-sample"], ["walk_forward", "Walk-forward"], ["any", "All"]].map(([k, l]) =>
              <button key={k} className={scope === k ? "on" : ""} onClick={() => setScope(k)}>{l}</button>)}
          </div>
        </div>
      </header>
      <div className="filterbar">
        <select className={`input${inst ? " active" : ""}`} value={inst} onChange={(e: { target: HTMLSelectElement }) => setInst(e.target.value)} aria-label="market">
          <option value="">Market: all</option>{d?.filters.instruments.map((x) => <option key={x}>{x}</option>)}</select>
        <select className={`input${ds ? " active" : ""}`} value={ds} onChange={(e: { target: HTMLSelectElement }) => setDs(e.target.value)} aria-label="dataset" style={{ maxWidth: 280 }}>
          <option value="">Dataset: all</option>{d?.filters.datasets.map((x) => <option key={x} value={x}>{datasetLabel(x)}</option>)}</select>
        <select className={`input${fam ? " active" : ""}`} value={fam} onChange={(e: { target: HTMLSelectElement }) => setFam(e.target.value)} aria-label="family">
          <option value="">Family: all</option>{d?.filters.families.map((x) => <option key={x} value={x}>{facetLabel("family_id", x)}</option>)}</select>
        <label className="check small"><input type="checkbox" checked={synth} onChange={(e: { target: HTMLInputElement }) => setSynth(e.target.checked)} />include synthetic runs</label>
        {loading && <span className="spinner" />}
      </div>
      {error ? <ErrorPanel error={error} /> : !d ? <Loading label="Aggregating stored runs…" /> : <>
        {d.holdout_runs_excluded > 0 && <Banner tone="info">{d.holdout_runs_excluded} protocol holdout-evaluation run(s) are excluded from these
          aggregates (they are shown per candidate, labelled Holdout, never pooled with discovery or OOS research).</Banner>}
        {d.synthetic_excluded > 0 &&<Banner tone="info">{d.synthetic_excluded} synthetic run(s) excluded (tick “include synthetic runs” to see them, labelled).</Banner>}
        {!d.n_runs ? <Empty>No stored runs with trades in this scope. <a href={href("/runs")}>Start a research run</a> or change the scope.</Empty> : <>
          <div className="kpis">
            <Kpi label="Runs" value={d.n_runs.toLocaleString()} sub={`${d.n_strategies} strategies`} />
            <Kpi label="Trades" value={d.n_trades.toLocaleString()} />
            <Kpi label="Runs with positive net" value={pct(d.pct_runs_positive_net, 0)} sub="share of runs, not a hit rate of trades" />
            <Kpi label="Gross R" value={n(d.cost_share.gross_r, 1)} sub={<Scope kind="gross" />} />
            <Kpi label="Costs" value={`${n(d.cost_share.cost_r, 1)} R`} sub={`${n(d.cost_share.cost_r_per_trade, 3)} R per trade`} />
            <Kpi label="Net R" value={n(d.cost_share.net_r, 1)} tone={d.cost_share.net_r > 0 ? "pos" : "neg"} sub={<Scope kind="net" />} />
          </div>
          <h3>Breakdowns</h3>
          <div className="panel-grid">{DIMS.map(([k, t]) => panel(k, t, d.breakdowns[k] ?? []))}</div>
          <h3>Distributions across runs</h3>
          <div className="panel-grid dense">
            <Card title={<>Expectancy distribution {tags}</>} testId="dash-dist-exp"><Histogram hist={d.distributions.expectancy_r} unit="net R per trade" signed color="var(--c1)" /></Card>
            <Card title={<>Profit-factor distribution {tags}</>}><Histogram hist={d.distributions.profit_factor} unit="profit factor (clipped at 4)" /></Card>
            <Card title={<>Drawdown distribution {tags}</>}><Histogram hist={d.distributions.max_drawdown_r} unit="max drawdown (R)" color="var(--c3)" /></Card>
            <Card title={<>Trade-count distribution <Scope kind={sk} /></>}><Histogram hist={d.distributions.trade_count} unit="trades per run" /></Card>
            <Card title={<>Win-rate distribution <Scope kind={sk} /></>}><Histogram hist={d.distributions.win_rate} unit="win rate (never a ranking criterion)" color="var(--c-neutral)" /></Card>
          </div>
          <h3>Calendar effects (pooled trades)</h3>
          <div className="panel-grid">{cal("weekday", "By weekday")}{cal("month", "By month")}{cal("year", "By year")}</div>
          <p className="small muted">{d.calendar.note}</p>
          <h3>Out-of-sample, walk-forward, controls</h3>
          <Banner tone="info">Switch the scope to <b>OOS</b> or <b>Walk-forward</b> to aggregate those runs. Random-entry control results are not
            stored as runs; see <a href={href("/controls")}>Controls</a> and each strategy's Robustness tab. Monte Carlo summaries are per run
            (Results → run).</Banner>
          <p className="small muted">{d.note} Basis: {d.basis}.</p>
        </>}
      </>}
    </div>
  );
}
