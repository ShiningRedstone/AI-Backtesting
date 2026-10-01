/* Strategy detail (Explorer drawer): identity, rules in plain English, performance, equity, trade behaviour,
   robustness and pipeline. Descriptive backtest statistics are kept visually separate from the protocol's
   formal acceptance criteria (only a holdout evaluation applies those). */
import { useEffect, useMemo, useState } from "react";
import type { ExplainResult, RunAnalytics, StoredStrategy, StrategyPipeline, StrategyResearch } from "../../api/types";
import { href } from "../../app/router";
import { useApi } from "../../app/context";
import { CostPanel, EquityPanels, MonteCarloPanel, PerformanceKpis, PerformancePanels, TradePanels } from "../analytics";
import { datasetLabel, facetLabel, familyLabel, statusLabel, strategyLabel } from "../../app/labels";
import { ControlPresentation, PipelineStrip, RulesTable } from "../research";
import { StrategyPanel } from "../results";
import { Badge, Banner, Card, Empty, ErrorPanel, KeyValues, Loading, Mono, Scope, ScopeOf, TableWrap, Tabs, TechDetails, n, r, shortTime, signCls } from "../ui";

type Tab = "summary" | "overview" | "performance" | "equity" | "trades" | "robustness" | "pipeline";

export function StrategyDetail({ id, tab: initial = "summary" }: { id: string; tab?: Tab }) {
  const [tab, setTab] = useState<Tab>(initial);
  const s = useApi<StoredStrategy>(`/api/strategies/${id}`, [id]);
  const ex = useApi<ExplainResult>(`/api/strategies/${id}/explain`, [id]);
  const sr = useApi<StrategyResearch>(`/api/strategies/${id}/research`, [id]);
  const pl = useApi<StrategyPipeline>(`/api/strategies/${id}/pipeline`, [id]);
  const runs = sr.data?.runs ?? [];
  const defaultRun = useMemo(() => {
    const is = runs.filter((x) => x.status === "IN_SAMPLE");
    return (is[is.length - 1] ?? runs[runs.length - 1])?.run_id ?? null;
  }, [sr.data]);  // eslint-disable-line react-hooks/exhaustive-deps
  const [runId, setRunId] = useState<string | null>(null);
  useEffect(() => { setRunId(defaultRun); }, [defaultRun]);
  const an = useApi<RunAnalytics>(runId && tab !== "summary" && tab !== "overview" && tab !== "pipeline" ? `/api/results/${runId}/analytics` : null, [runId]);
  if (s.error) return <ErrorPanel error={s.error} />;
  if (!s.data) return <Loading label="Loading strategy…" />;
  const d = s.data, first = d.lineage[0];
  const def = d.definition as Record<string, any>;  // eslint-disable-line @typescript-eslint/no-explicit-any
  const protocols = pl.data ? [...new Set([...pl.data.shortlists.map((x) => x.protocol_id), ...pl.data.holdout.map((x) => x.protocol_id)].filter(Boolean))] as string[] : [];
  const runPicker = runs.length ? (
    <div className="inline small" data-testid="run-picker">
      <span className="muted">Statistics of stored run</span>
      <select className="input input-sm" value={runId ?? ""} onChange={(e: { target: HTMLSelectElement }) => setRunId(e.target.value)}>
        {runs.map((x) => <option key={x.run_id} value={x.run_id} title={x.run_id}>{shortTime(x.created_at)} · {statusLabel(x.status)} · {datasetLabel(x.dataset_id)}{x.synthetic ? " · SYNTHETIC" : ""}</option>)}
      </select>
      {runId && <a href={href(`/results/${runId}`)}>open run ›</a>}
    </div>) : null;
  const needRun = (body: (a: RunAnalytics) => unknown) => {
    if (!runs.length) return <Empty>No stored runs for this strategy yet. Backtest it from the <a href={href(`/strategies/${id}?tab=backtest`)}>strategy page</a>.</Empty>;
    if (an.error) return <ErrorPanel error={an.error} />;
    if (!an.data || an.data.run_id !== runId) return <Loading label="Computing analytics from the stored trades…" />;
    if (!an.data.n_trades) return <Empty>This run has no trades.</Empty>;
    return body(an.data);
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }} data-testid="strategy-detail">
      <Tabs<Tab> active={tab} onChange={setTab} tabs={[{ id: "summary", label: "Summary" }, { id: "overview", label: "Identity & rules" }, { id: "performance", label: "Performance" },
        { id: "equity", label: "Equity" }, { id: "trades", label: "Trade behaviour" }, { id: "robustness", label: "Robustness" },
        { id: "pipeline", label: "Pipeline" }]} />
      {tab !== "summary" && tab !== "overview" && tab !== "pipeline" && runPicker}
      {tab === "summary" && <StrategyPanel id={id} />}
      {tab === "overview" && <>
        <Card title="Identity" testId="detail-identity">
          <KeyValues rows={[["Name", strategyLabel(d.name)], ["Family", familyLabel(d.family_id, def.family?.name)],
            ["Source", <>{facetLabel("source", first?.generation_method)}{first?.generation_parameters?.proposal_id ? " · from an AI proposal" : null}</>],
            ["Parent", first?.parent_strategy_id ? "A variation of another strategy (see technical details)" : "—"],
            ["Created", shortTime(first?.generation_timestamp)],
            ["Protocols", pl.data ? (protocols.length ? `${protocols.length} (see technical details)` : "none") : "…"],
            ["Datasets used", runs.length ? [...new Set(runs.map((x) => x.dataset_id))].map(datasetLabel).join(", ") : "none yet"]]} />
          <TechDetails rows={[["Strategy id", <Mono>{d.strategy_id}</Mono>], ["Machine name", <Mono>{d.name}</Mono>], ["Family id", <Mono>{d.family_id}</Mono>],
            ["Generation method", first?.generation_method ? <Mono>{first.generation_method}</Mono> : null],
            ["Proposal id", first?.generation_parameters?.proposal_id ? <Mono>{String(first.generation_parameters.proposal_id)}</Mono> : null],
            ["Parent strategy id", first?.parent_strategy_id ? <Mono>{first.parent_strategy_id}</Mono> : null],
            ["Logic hash", <Mono>{d.logic_hash}</Mono>], ["Definition hash", <Mono>{d.definition_hash}</Mono>],
            ["Protocol ids", protocols.length ? <Mono>{protocols.join(", ")}</Mono> : null],
            ["Dataset ids", runs.length ? <Mono>{[...new Set(runs.map((x) => x.dataset_id))].join(", ")}</Mono> : null]]} />
        </Card>
        <Card title="Rules in plain English">
          {ex.error ? <ErrorPanel error={ex.error} /> : <RulesTable definition={def} explain={ex.data?.explain} />}
          <details className="tech"><summary>Technical details: definition (canonical DSL)</summary><pre className="code">{JSON.stringify(def, null, 2)}</pre></details>
        </Card>
      </>}
      {tab === "performance" && needRun((a) => <>
        <Banner tone="info">Descriptive backtest statistics of one stored run under its stated costs. They are not the protocol's formal
          acceptance criteria, which apply only to a holdout evaluation (see Robustness).</Banner>
        <PerformanceKpis a={a} /><PerformancePanels a={a} /></>)}
      {tab === "equity" && needRun((a) => <EquityPanels a={a} />)}
      {tab === "trades" && needRun((a) => <TradePanels a={a} />)}
      {tab === "robustness" && <Robustness sr={sr.data} pl={pl.data} a={an.data?.run_id === runId ? an.data : null} hasRuns={runs.length > 0} />}
      {tab === "pipeline" && (pl.error ? <ErrorPanel error={pl.error} /> : pl.data ? <>
        <PipelineStrip stages={pl.data.stages} /><p className="small muted">{pl.data.note}</p></> : <Loading label="Loading pipeline…" />)}
    </div>
  );
}

function Robustness({ sr, pl, a, hasRuns }: { sr: StrategyResearch | null; pl: StrategyPipeline | null; a: RunAnalytics | null; hasRuns: boolean }) {
  const looks = (pl?.holdout ?? []).filter((h) => h.status !== "refused");
  const holdoutRuns = new Set(looks.map((h) => h.run_id).filter(Boolean));
  const oos = (sr?.runs ?? []).filter((x) => (x.status === "OUT_OF_SAMPLE" || x.status === "WALK_FORWARD") && !holdoutRuns.has(x.run_id));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }} data-testid="detail-robustness">
      <Card title={<>Out-of-sample & walk-forward runs <Scope kind="oos" /><Scope kind="net" /></>}>
        {oos.length ? <TableWrap><table className="dense"><thead><tr><th>Run</th><th>Scope</th><th>Period</th><th className="r">Trades</th>
          <th className="r">Net R per trade</th><th className="r">Net R</th><th className="r">Profit factor</th><th className="r">Max drawdown (R)</th></tr></thead>
          <tbody>{oos.map((x) => <tr key={x.run_id}><td><a href={href(`/results/${x.run_id}`)} title={x.run_id}>{shortTime(x.created_at)}</a></td><td><ScopeOf status={x.status} /></td>
            <td className="small">{x.period.start.slice(0, 10)} → {x.period.end.slice(0, 10)}</td><td className="r num">{String(x.metrics.trade_count ?? "—")}</td>
            <td className={`r num ${signCls(x.metrics.expectancy_r)}`}>{r(x.metrics.expectancy_r)}</td><td className="r num">{n(x.metrics.net_r as number, 1)}</td>
            <td className="r num">{n(x.metrics.profit_factor as number)}</td><td className="r num">{n(x.metrics.max_drawdown_r as number, 1)}</td></tr>)}</tbody></table></TableWrap>
          : <Empty>No out-of-sample or walk-forward runs. Run them from the strategy's <b>Validate</b> tab (inside the discovery window).</Empty>}
      </Card>
      <Card title={<>Holdout evaluations <Scope kind="holdout" /> <Badge tone="info">formal acceptance criteria</Badge></>} testId="detail-holdout">
        {looks.length ? looks.map((h) => {
          const rc = (h.random_control ?? {}) as Record<string, any>;  // eslint-disable-line @typescript-eslint/no-explicit-any
          return <div key={h.access_id} style={{ marginBottom: 12 }}>
            <p><Badge tone={h.outcome === "HOLDOUT_CRITERIA_MET" ? "ok" : "warn"}>{statusLabel(h.outcome ?? h.status)}</Badge> <span className="small muted">{shortTime(h.created_at)}</span> ·
              {" "}{h.run_id ? <a href={href(`/results/${h.run_id}`)} title={h.run_id}>open the evaluation run ›</a> : "no run stored"} · <span className="small muted">never "accepted"</span></p>
            {rc.p_value !== undefined && <ControlPresentation c={{ candidate: rc.candidate ?? null, controls: [], pValue: rc.p_value, nControls: rc.n_controls ?? 0,
              formal: true, scope: <Scope kind="holdout" />, rule: "p = (1 + #controls ≥ candidate or non-finite) / (N + 1); required ≤ " + String(rc.max_p_value ?? 0.05) }} />}
            <TechDetails rows={[["Protocol id", <Mono>{h.protocol_id}</Mono>], ["Access id", <Mono>{h.access_id}</Mono>], ["Run id", h.run_id ? <Mono>{h.run_id}</Mono> : null],
              ["Outcome code", <Mono>{h.outcome ?? h.status}</Mono>]]} />
          </div>;
        }) : <Empty>No holdout evaluation. Only a shortlisted, discovery-evaluated candidate can use one of the protocol's holdout looks,
          through the backend gate (Experiments → search → shortlist).</Empty>}
      </Card>
      {!hasRuns ? <Empty>No stored runs: nothing to resample.</Empty> : !a ? <Loading label="Computing analytics…" /> : <>
        <CostPanel a={a} />
        <MonteCarloPanel a={a} />
        <Card title={<>Stability by year <ScopeOf status={a.run.status} holdout={a.run.holdout} /><Scope kind="net" /></>}>
          <p className="small">{(a.year ?? []).filter((y) => (y.net_r ?? 0) > 0).length} of {(a.year ?? []).length} calendar years with positive net R.
            {" "}Random-entry controls for this strategy can be run from the <a href={href("/controls")}>Controls</a> page (a discovery-window evaluation, counted as a trial under an active protocol).</p>
        </Card>
      </>}
    </div>
  );
}
