import { useState } from "react";
import { api, ApiError } from "../api/client";
import type { ControlReport, DatasetRow, LibraryRow, PipelineBoard, StrategyPipeline } from "../api/types";
import { href } from "../app/router";
import { useApi } from "../app/context";
import { datasetLabel, facetLabel, metricLabel, statusLabel, strategyLabel } from "../app/labels";
import { ControlPresentation, plainText } from "../components/research";
import { Badge, Banner, Button, Card, Empty, ErrorPanel, Field, Loading, Mono, NumberInput, Scope, Select, TableWrap, TechDetails, TextInput, n } from "../components/ui";

export function ControlsPage() {
  return (
    <div className="page" data-testid="controls-page">
      <header className="page-head"><div><h1>Random controls</h1></div></header>
      <Card title="What a random-entry control is">
        <div className="grid2 small">
          <div><b>Preserved</b> — the candidate's compiled exits (stop, target, time stop), costs and execution quotes, sizing, cooldown,
            engine rules and the exact data window. <b>Randomized</b> — only entry timing and direction, drawn among the candidate's own eligible
            bars at its own signal rate and long/short mix (a conditional null).</div>
          <div><b>Reading it</b> — the exact Monte-Carlo p-value is (1 + #controls ≥ candidate, or non-finite) / (N + 1). A small value says random
            entries rarely matched the candidate on this window. It is a <b>robustness filter</b>, not a familywise significance test, and it is
            part of the formal acceptance criteria only inside a protocol holdout evaluation.</div>
        </div>
      </Card>
      <HoldoutControls />
      <RunControl />
    </div>
  );
}

function HoldoutControls() {
  const { data, error } = useApi<PipelineBoard>("/api/pipeline");
  const cands = (data?.candidates ?? []).filter((c) => c.stages.some((s) => s.id === "holdout_result" && (s.state === "done" || s.state === "failed")));
  return (
    <Card title={<>Formal controls from holdout evaluations <Scope kind="holdout" /></>} testId="holdout-controls">
      {error ? <ErrorPanel error={error} /> : !data ? <Loading label="Loading…" /> : cands.length
        ? cands.map((c) => <HoldoutControl key={c.strategy_id} id={c.strategy_id} name={c.name} />)
        : <Empty>No holdout evaluation has been made. Each protocol holdout evaluation runs 100 matched controls on the holdout window and stores
          the exact p-value in the holdout ledger.</Empty>}
    </Card>
  );
}

function HoldoutControl({ id, name }: { id: string; name: string | null }) {
  const { data } = useApi<StrategyPipeline>(`/api/strategies/${id}/pipeline`, [id]);
  if (!data) return <Loading label={`Loading ${name ? strategyLabel(name) : "strategy"}…`} />;
  return <>{data.holdout.filter((h) => h.random_control).map((h) => {
    const rc = h.random_control as Record<string, any>;  // eslint-disable-line @typescript-eslint/no-explicit-any
    return <div key={h.access_id} style={{ borderBottom: "1px solid var(--border)", paddingBottom: 12, marginBottom: 12 }}>
      <p><a href={href(`/explorer?open=${id}`)} title={id}>{name ? strategyLabel(name) : "Unnamed strategy"}</a> ·{" "}
        <Badge tone={h.outcome === "HOLDOUT_CRITERIA_MET" ? "ok" : "warn"}>{statusLabel(h.outcome)}</Badge></p>
      <ControlPresentation c={{ candidate: rc.candidate ?? null, controls: [], pValue: rc.p_value, nControls: rc.n_controls ?? 0, formal: true,
        scope: <Scope kind="holdout" /> }} testId={`holdout-control-${id}`} />
      <TechDetails rows={[["Strategy id", <Mono>{id}</Mono>], ["Protocol id", <Mono>{h.protocol_id}</Mono>], ["Access id", <Mono>{h.access_id}</Mono>],
        ["Outcome code", h.outcome ? <Mono>{h.outcome}</Mono> : null]]} />
    </div>;
  })}</>;
}

function RunControl() {
  const strategies = useApi<LibraryRow[]>("/api/strategies");
  const datasets = useApi<DatasetRow[]>("/api/datasets");
  const [sid, setSid] = useState(""), [did, setDid] = useState(""), [split, setSplit] = useState("");
  const [nCtl, setN] = useState<number | undefined>(100), [seed, setSeed] = useState<number | undefined>(0);
  const [busy, setBusy] = useState(false), [err, setErr] = useState<ApiError | null>(null), [rep, setRep] = useState<ControlReport | null>(null);
  const run = async () => {
    setBusy(true); setErr(null); setRep(null);
    try {
      setRep(await api.post<ControlReport>("/api/validation/control", { strategy: sid, dataset_id: did, n_controls: nCtl ?? 100, seed: seed ?? 0,
        ...(split ? { split_at: split } : {}) }));
    } catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  };
  const exp = rep?.comparison?.expectancy_r;
  const frac = typeof exp?.fraction_of_controls_exceeding_candidate === "number" ? exp.fraction_of_controls_exceeding_candidate : null;
  return (
    <Card title={<>Run a random-entry control <Scope kind="control" /></>} testId="run-control">
      <Banner tone="warn">This evaluates the candidate once on the chosen data through the normal engine path. Under an ACTIVE research protocol
        it is a discovery evaluation (counted in the trial ledger), and it is refused if its window touches the locked holdout.
        Control realizations are returned, not stored as runs.</Banner>
      <div className="grid3" style={{ marginTop: 10 }}>
        <Field label="Strategy"><Select value={sid} onChange={setSid} placeholder="choose…" testId="ctl-strategy"
          options={(strategies.data ?? []).map((s) => ({ value: s.strategy_id, label: `${strategyLabel(s.name)}${s.timeframe ? ` · ${facetLabel("timeframe", s.timeframe)}` : ""}` }))} /></Field>
        <Field label="Dataset"><Select value={did} onChange={setDid} placeholder="choose…" testId="ctl-dataset"
          options={(datasets.data ?? []).map((d) => ({ value: d.dataset_id, label: `${datasetLabel(d.dataset_id)} · ${String(d.start ?? "").slice(0, 10)} → ${String(d.end ?? "").slice(0, 10)}` }))} /></Field>
        <Field label="Only the out-of-sample window after (optional)" hint="YYYY-MM-DD"><TextInput value={split} onChange={setSplit} placeholder="e.g. 2024-01-01" /></Field>
        <Field label="Number of controls"><NumberInput value={nCtl} onChange={setN} integer /></Field>
        <Field label="Seed"><NumberInput value={seed} onChange={setSeed} integer /></Field>
      </div>
      <div className="actions" style={{ marginTop: 10 }}>
        <Button kind="primary" onClick={run} busy={busy} busyLabel="Running controls…" disabled={!sid || !did} testId="ctl-run">Run control</Button>
      </div>
      {(sid || did) && <TechDetails rows={[["Strategy id", sid ? <Mono>{sid}</Mono> : null], ["Dataset id", did ? <Mono>{did}</Mono> : null]]} />}
      <ErrorPanel error={err} />
      {rep && <div style={{ marginTop: 14 }}>
        {rep.labels.map((l) => <p key={l} className="small muted">{plainText(l)}</p>)}
        <ControlPresentation c={{ candidate: (rep.candidate.metrics.expectancy_r as number) ?? null,
          controls: rep.realizations.map((x) => x.expectancy_r as number | null), pValue: null,
          percentile: frac == null ? null : 1 - frac, nControls: rep.control_config.n_controls, formal: false,
          rule: "An ad-hoc control reports a descriptive rank within the conditional null (share of controls with a strictly larger value), not a p-value; the exact Monte-Carlo p-value is computed only by a protocol holdout evaluation",
          scope: <Scope kind={rep.sample_status === "OUT_OF_SAMPLE" ? "oos" : "is"} /> }} />
        <TableWrap><table className="dense"><thead><tr><th>Statistic</th><th className="r">Candidate</th><th className="r">Control median</th>
          <th className="r">5th / 95th percentile</th><th className="r">Controls exceeding</th></tr></thead>
          <tbody>{["expectancy_r", "net_r", "profit_factor", "max_drawdown_r"].map((k) => { const x = rep.comparison[k] ?? {};
            return <tr key={k}><td>{metricLabel(k)}</td><td className="r num">{n(x.candidate, 3)}</td><td className="r num">{n(x.median, 3)}</td>
              <td className="r num">{n(x.percentiles?.["5"], 2)} / {n(x.percentiles?.["95"], 2)}</td>
              <td className="r num">{x.fraction_of_controls_exceeding_candidate == null ? "—" : `${(x.fraction_of_controls_exceeding_candidate * 100).toFixed(0)}%`}</td></tr>; })}
          </tbody></table></TableWrap>
        <p className="small muted">{plainText(rep.comparison.note)}</p>
      </div>}
    </Card>
  );
}
