import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { CompareRow, ControlReport, DatasetRow, Readiness, RunCurve, RunSummary, StrategyResearch, ValidationReport } from "../../api/types";
import { research } from "../../api/research";
import { go, href } from "../../app/router";
import { Badge, Banner, Button, Card, Checkbox, Empty, ErrorPanel, Field, KeyValues, Loading, Mono, NumberInput, Select, TableWrap, TextInput, fmt, shortTime } from "../ui";
import { METHOD_LABEL, SYNTHETIC_NOTICE, changesText } from "./index";

/** Research-scope vocabulary shared by every Strategy Lab view: in-sample results never look like
 *  out-of-sample ones, controls are never runs, prop outcomes are never strategy results. */
export const SCOPE: Record<string, { label: string; tone: "warn" | "info" | "ok" | "neutral"; note: string }> = {
  IN_SAMPLE: { label: "In-sample · exploratory", tone: "warn", note: "Exploratory result on the data it was built/tuned on. Not validated." },
  OUT_OF_SAMPLE: { label: "Out-of-sample", tone: "info", note: "Fixed definition evaluated on a held-out time window." },
  WALK_FORWARD: { label: "Walk-forward test window", tone: "info", note: "Fixed definition on consecutive held-out windows." },
};
export const ScopeBadge = ({ status }: { status: string }) => {
  const s = SCOPE[status] ?? { label: status, tone: "neutral" as const, note: "" };
  return <Badge tone={s.tone} title={s.note}>{s.label}</Badge>;
};
const num = (v: unknown, d = 3) => (typeof v === "number" && Number.isFinite(v) ? v.toFixed(d) : fmt(v));

// =========================================================================== equity / drawdown
export function EquityChart({ runId }: { runId: string }) {
  const [c, setC] = useState<RunCurve | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => { api.get<RunCurve>(`/api/results/${runId}/curve`).then(setC).catch(setErr); }, [runId]);
  if (err) return <ErrorPanel error={err} />;
  if (!c) return <Loading label="Loading equity curve…" />;
  if (!c.points.length) return <Empty>No trades: no curve.</Empty>;
  const W = 760, H = 180, DH = 70, P = 36;
  const eq = c.points.map((p) => p.equity_r), dd = c.points.map((p) => p.drawdown_r);
  const lo = Math.min(0, ...eq), hi = Math.max(0, ...eq), mdd = Math.max(1e-9, ...dd);
  const x = (i: number) => P + (i / Math.max(1, c.points.length - 1)) * (W - P - 8);
  const y = (v: number) => 8 + (1 - (v - lo) / Math.max(1e-9, hi - lo)) * (H - 16);
  const yd = (v: number) => H + 12 + (v / mdd) * (DH - 8);
  const line = c.points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.equity_r).toFixed(1)}`).join("");
  const area = `M${x(0)},${H + 12}` + c.points.map((p, i) => `L${x(i).toFixed(1)},${yd(p.drawdown_r).toFixed(1)}`).join("") + `L${x(c.points.length - 1)},${H + 12}Z`;
  return (
    <figure className="equity" data-testid="equity-chart">
      <svg viewBox={`0 0 ${W} ${H + DH + 20}`} width="100%" role="img"
        aria-label={`Cumulative net R over ${c.n_trades} trades, ending ${num(c.final_net_r, 2)} R; maximum drawdown ${num(c.max_drawdown_r, 2)} R`}>
        <line x1={P} x2={W - 8} y1={y(0)} y2={y(0)} stroke="var(--border-strong)" strokeDasharray="3 3" />
        <path d={line} fill="none" stroke="var(--accent)" strokeWidth={1.5} />
        <text x={2} y={y(hi) + 4} fontSize={10} fill="var(--muted)">{hi.toFixed(1)}R</text>
        <text x={2} y={y(lo)} fontSize={10} fill="var(--muted)">{lo.toFixed(1)}R</text>
        <path d={area} fill="var(--error-weak)" stroke="var(--error)" strokeWidth={0.8} />
        <text x={2} y={H + 20} fontSize={10} fill="var(--muted)">DD</text>
        <text x={2} y={H + DH + 10} fontSize={10} fill="var(--muted)">{mdd.toFixed(1)}R</text>
      </svg>
      <figcaption className="muted small">Cumulative net R by trade exit (top) and drawdown from the running peak (bottom), from the stored
        trades. {c.n_trades} trades{c.thinned ? " (thinned for display)" : ""}; final {num(c.final_net_r, 2)} R, max drawdown {num(c.max_drawdown_r, 2)} R.
        Historical, under the run's stated costs; not a forecast.</figcaption>
    </figure>
  );
}

// =========================================================================== dataset picker
/** Validated datasets for a strategy with the backend's eligibility verdict. Ineligible datasets
 *  are shown with their reasons and can never be selected. */
export function DatasetPicker({ strategy, multi, value, onChange, testId = "lab-datasets" }: {
  strategy: string; multi?: boolean; value: string[]; onChange: (ids: string[]) => void; testId?: string;
}) {
  const [ready, setReady] = useState<Readiness | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [f, setF] = useState({ provider: "", instrument: "", timeframe: "", eligibleOnly: false });
  useEffect(() => { api.post<Readiness>("/api/backtests/readiness", { strategy }).then(setReady).catch(setErr); }, [strategy]);
  const pref = ready?.datasets.find((d) => d.dataset_id === ready.preferred_dataset_id);
  useEffect(() => {                  // NEW research starts on the workspace's Preferred Research Dataset (when eligible)
    if (pref?.runnable && !value.length) onChange([pref.dataset_id]);
  }, [pref?.dataset_id]); // eslint-disable-line react-hooks/exhaustive-deps
  if (err) return <ErrorPanel error={err} />;
  if (!ready) return <Loading label="Checking datasets…" />;
  const uniq = (k: keyof DatasetRow) => [...new Set(ready.datasets.map((d) => String(d[k])))].sort();
  const rows = ready.datasets.filter((d) => (!f.provider || d.provider === f.provider) && (!f.instrument || d.instrument === f.instrument)
    && (!f.timeframe || d.timeframe === f.timeframe) && (!f.eligibleOnly || d.runnable));
  const pick = (d: DatasetRow, on: boolean) => {
    if (!d.runnable) return;
    onChange(multi ? (on ? [...value.filter((x) => x !== d.dataset_id), d.dataset_id] : value.filter((x) => x !== d.dataset_id)) : [d.dataset_id]);
  };
  const opt = (k: keyof DatasetRow, label: string) => [{ value: "", label: `All ${label}` }, ...uniq(k).map((v) => ({ value: v, label: v }))];
  return (
    <div data-testid={testId}>
      <div className="filters">
        <Select value={f.provider} onChange={(v) => setF({ ...f, provider: v })} options={opt("provider", "providers")} ariaLabel="provider filter" />
        <Select value={f.instrument} onChange={(v) => setF({ ...f, instrument: v })} options={opt("instrument", "instruments")} ariaLabel="instrument filter" />
        <Select value={f.timeframe} onChange={(v) => setF({ ...f, timeframe: v })} options={opt("timeframe", "timeframes")} ariaLabel="timeframe filter" />
        <Checkbox checked={f.eligibleOnly} onChange={(v) => setF({ ...f, eligibleOnly: v })} label="Eligible only" />
      </div>
      <p className="muted small">Strategy timeframe <b>{ready.strategy_timeframe ?? "?"}</b>. A dataset is eligible when it passed validation, matches the
        timeframe and has a configured cost profile. Ineligible datasets cannot be selected.</p>
      <PreferredNotice ready={ready} testId={`${testId}-preferred`} />
      {!rows.length ? <Empty>No datasets match. Import one on the <a href={href("/datasets")}>Datasets</a> page.</Empty> : (
        <TableWrap><table>
          <thead><tr><th /><th>Dataset</th><th>Provider</th><th>Instrument</th><th>TF</th><th>Range</th><th>Validation</th><th>Costs</th><th>Eligibility</th></tr></thead>
          <tbody>{rows.map((d) => (
            <tr key={d.dataset_id} className={d.runnable ? "" : "disabled-row"} data-testid={`${testId}-${d.dataset_id}`}>
              <td><input type={multi ? "checkbox" : "radio"} name={testId} disabled={!d.runnable} aria-label={`select ${d.dataset_name ?? d.dataset_id}`}
                checked={value.includes(d.dataset_id)} onChange={(e: { target: HTMLInputElement }) => pick(d, e.target.checked)} /></td>
              <td>{d.dataset_name}<div className="small"><Mono>{d.dataset_id}</Mono></div>{d.synthetic && <Badge tone="demo">synthetic</Badge>}
                {d.preferred && <> <Badge tone="info">preferred</Badge></>}{d.identity?.identity_status === "provisional" && <> <Badge tone="warn">provisional identity</Badge></>}</td>
              <td>{d.provider}</td><td>{d.instrument}</td><td>{d.timeframe}</td>
              <td className="small">{d.start?.slice(0, 10)} → {d.end?.slice(0, 10)}</td>
              <td><Badge tone={d.quality_status === "FAIL" ? "error" : d.quality_status === "WARN" ? "warn" : "ok"}>{d.quality_status}</Badge></td>
              <td><Badge tone={d.cost.status === "unconfigured" ? "error" : "neutral"} title={d.cost.reason}>{d.cost.status}</Badge>
                {d.cost.profile && <div className="small muted">{d.cost.profile}</div>}</td>
              <td className="small">{d.runnable ? <Badge tone="ok">eligible</Badge> : <><Badge tone="error">not eligible</Badge>{d.reasons.map((r) => <div key={r}>{r}</div>)}</>}
                {d.limitations?.length ? <details><summary>caveats ({d.limitations.length})</summary>{d.limitations.map((l) => <div key={l}>{l}</div>)}</details> : null}</td>
            </tr>))}
          </tbody></table></TableWrap>)}
    </div>
  );
}

/** Where the preselection comes from, and why it did not happen when the preferred dataset is not eligible. */
export function PreferredNotice({ ready, testId }: { ready: Readiness; testId?: string }) {
  const pref = ready.datasets.find((d) => d.dataset_id === ready.preferred_dataset_id);
  if (!ready.preferred_dataset_id) return <p className="muted small" data-testid={testId}>No Preferred Research Dataset is set (Datasets page).</p>;
  if (!pref) return null;
  return pref.runnable
    ? <p className="muted small" data-testid={testId}>Preselected: the workspace's Preferred Research Dataset <Mono>{pref.dataset_id}</Mono>
        ({pref.provider} · {pref.instrument} · {pref.timeframe}). A default for new research only; stored runs are unchanged.</p>
    : <Banner tone="warn" testId={testId}>The Preferred Research Dataset <Mono>{pref.dataset_id}</Mono> is not eligible here and was not
        preselected: {pref.reasons.join("; ")}.</Banner>;
}

// =========================================================================== provenance + runs
export function ProvenanceCard({ sr }: { sr: StrategyResearch }) {
  const L = sr.lineage, S = sr.strategy;
  return (
    <Card title="Version and provenance" testId="lab-provenance">
      <KeyValues rows={[["Strategy ID", <Mono>{S.strategy_id}</Mono>], ["Definition hash", <Mono>{S.definition_hash}</Mono>],
        ["Logic hash", <Mono>{S.logic_hash}</Mono>],
        ["Created from", <>{METHOD_LABEL[L.generation_method ?? ""] ?? L.generation_method ?? "—"}{L.generation_timestamp ? ` · ${shortTime(L.generation_timestamp)}` : ""}</>],
        ["Parent", L.parent_strategy_id ? <><a href={href(`/strategies/${L.parent_strategy_id}`)}><Mono>{L.parent_strategy_id}</Mono></a>
          <div className="small muted">parent definition hash <Mono>{L.parent_definition_hash?.slice(0, 16) ?? "—"}</Mono></div></> : <span className="muted">root (no parent)</span>],
        ["Changes vs parent", changesText(L.changes) || "—"],
        ["Variation batch", L.generation_batch ? <a href={href(`/variations/${L.generation_batch.batch_id}`)}><Mono>{L.generation_batch.batch_id}</Mono></a> : "—"],
        ["Ancestry depth", String(L.ancestry.length)], ["Children", String(L.children.length)],
        ["Parameters", Object.keys(S.parameters).length ? Object.entries(S.parameters).map(([k, v]) => `${k}=${fmt(v)}`).join(", ") : "none declared"],
        ["Research state", <>{sr.validation_state.run_statuses.length ? sr.validation_state.run_statuses.map((s) => <ScopeBadge key={s} status={s} />) : <span className="muted">no stored runs</span>}
          {" "}<Badge tone="neutral">not validated</Badge></>]]} />
      <p className="muted small">{sr.validation_state.note} Stored versions are immutable: edits always create a new version with lineage.</p>
      <details><summary>Machine-readable record (what an AI layer receives)</summary><pre className="code" data-testid="lab-provenance-json">{JSON.stringify(sr, null, 2)}</pre></details>
    </Card>
  );
}

export function RunsTable({ runs, testId = "lab-runs" }: { runs: RunSummary[]; testId?: string }) {
  if (!runs.length) return <Empty>No stored runs for this version yet. Run a backtest or a validation.</Empty>;
  return (
    <TableWrap testId={testId}><table>
      <thead><tr><th>Run</th><th>Scope</th><th>Dataset</th><th>Provider / TF</th><th>Costs</th><th>Trades</th><th>Net R</th><th>Expectancy</th><th>PF</th><th>Max DD R</th><th>Prop sims</th><th /></tr></thead>
      <tbody>{runs.map((r) => (
        <tr key={r.run_id}>
          <td><a href={href(`/results/${r.run_id}`)}><Mono>{r.run_id}</Mono></a><div className="small muted">{shortTime(r.created_at)}</div></td>
          <td><ScopeBadge status={r.status} />{r.synthetic && <Badge tone="demo">synthetic</Badge>}</td>
          <td className="small">{r.dataset_name ?? r.dataset_id}{r.parent_dataset_id && <div className="muted">window of {r.parent_dataset_id}</div>}</td>
          <td className="small">{r.provider} · {r.timeframe}</td><td className="small" title={r.cost_basis ?? ""}>{r.cost_status}{r.cost_scenario ? ` · ${r.cost_scenario}` : ""}</td>
          <td>{fmt(r.metrics.trade_count)}</td><td className="mono">{num(r.metrics.net_r, 2)}</td><td className="mono">{num(r.metrics.expectancy_r)}</td>
          <td className="mono">{num(r.metrics.profit_factor)}</td><td className="mono">{num(r.metrics.max_drawdown_r, 2)}</td><td>{r.prop_simulations}</td>
          <td className="row-actions"><a href={href(`/results/${r.run_id}`)}>Open</a><a href={href(`/prop?run=${r.run_id}`)}>Prop simulation</a></td>
        </tr>))}
      </tbody></table></TableWrap>
  );
}

// =========================================================================== batch research
/** Runs the base strategy + one variation batch on selected datasets through the existing Phase 4
 *  search job (the same pipeline as the Research page), then links to the comparison. */
export function BatchResearch({ strategyId, batches, preselect }: { strategyId: string; batches: string[]; preselect?: string | null }) {
  const [batch, setBatch] = useState(preselect && batches.includes(preselect) ? preselect : batches[batches.length - 1] ?? "");
  const [ds, setDs] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const spec = useMemo(() => ({ search_spec_version: 1, strategies: { ids: [strategyId], variation_batches: batch ? [batch] : [] },
    datasets: ds, max_cells: 1000, seed: 1, workers: 1 }), [strategyId, batch, ds]);
  const start = async () => {
    setBusy(true); setErr(null);
    try { const j = await research.startJob(spec); go(`/research?job=${j.job_id}`); }
    catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  };
  return (
    <Card title="Run this version and a variation batch on datasets" testId="lab-batch">
      <p className="muted small">Uses the existing background search: one cell per strategy × dataset, datasets never merged, results stored as
        normal IN_SAMPLE runs with progress and cancellation on the Research page. Trials are counted honestly per search.</p>
      <Field label="Variation batch">
        <Select value={batch} onChange={setBatch} testId="lab-batch-select" options={[{ value: "", label: "This version only (no batch)" },
          ...batches.map((b) => ({ value: b, label: b }))]} />
      </Field>
      {!batches.length && <p className="muted small">No variation batch from this version yet (Generate Variations tab).</p>}
      <DatasetPicker strategy={strategyId} multi value={ds} onChange={setDs} testId="lab-batch-ds" />
      <div className="actions"><Button kind="primary" onClick={start} busy={busy} busyLabel="Starting…" disabled={!ds.length} testId="lab-batch-start">
        Start research job ({ds.length} dataset{ds.length === 1 ? "" : "s"})</Button></div>
      <ErrorPanel error={err} title="The research job was refused" />
    </Card>
  );
}

// =========================================================================== validation
type VKind = "oos" | "walkforward" | "control";

export function ValidationPanel({ strategyId, initialDataset }: { strategyId: string; initialDataset?: string | null }) {
  const [ds, setDs] = useState<string[]>(initialDataset ? [initialDataset] : []);
  const [kind, setKind] = useState<VKind>("oos");
  const [split, setSplit] = useState("");
  const [trainM, setTrainM] = useState<number | undefined>(6);
  const [testM, setTestM] = useState<number | undefined>(1);
  const [anchored, setAnchored] = useState(false);
  const [record, setRecord] = useState(true);
  const [nCtl, setNCtl] = useState<number | undefined>(20);
  const [seed, setSeed] = useState<number | undefined>(0);
  const [oosCtl, setOosCtl] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [rep, setRep] = useState<ValidationReport | null>(null);
  const [ctl, setCtl] = useState<ControlReport | null>(null);
  const run = async () => {
    setBusy(true); setErr(null); setRep(null); setCtl(null);
    const base = { strategy: strategyId, dataset_id: ds[0] };
    try {
      if (kind === "oos") setRep(await api.post<ValidationReport>("/api/validation/oos", { ...base, split_at: split, record }));
      else if (kind === "walkforward") setRep(await api.post<ValidationReport>("/api/validation/walkforward",
        { ...base, train_months: trainM, test_months: testM, anchored, record }));
      else setCtl(await api.post<ControlReport>("/api/validation/control", { ...base, n_controls: nCtl, seed, ...(oosCtl && split ? { split_at: split } : {}) }));
    } catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  };
  const needsSplit = kind === "oos" || (kind === "control" && oosCtl);
  return (
    <div data-testid="lab-validate">
      <Banner tone="info">Validation runs this <b>fixed</b> version unchanged. OOS and walk-forward windows are recorded as runs with status
        OUT_OF_SAMPLE / WALK_FORWARD (their train windows stay IN_SAMPLE). The random-entry control is a conditional null; its realizations are
        never stored as runs. Nothing here promotes or approves a strategy.</Banner>
      <Card title="1 · Dataset"><DatasetPicker strategy={strategyId} value={ds} onChange={setDs} testId="lab-val-ds" /></Card>
      <Card title="2 · Method">
        <div className="segmented" role="tablist">
          {(["oos", "walkforward", "control"] as VKind[]).map((k) => (
            <button key={k} className={kind === k ? "on" : ""} onClick={() => setKind(k)} data-testid={`lab-val-${k}`}>
              {k === "oos" ? "Out-of-sample split" : k === "walkforward" ? "Walk-forward" : "Random-entry control"}</button>))}
        </div>
        <div className="grid3">
          {needsSplit && <Field label="OOS split date (first out-of-sample day, UTC)" hint="Train = before this date; OOS = from it to the dataset end.">
            <TextInput value={split} onChange={setSplit} placeholder="YYYY-MM-DD" testId="lab-val-split" /></Field>}
          {kind === "walkforward" && <>
            <Field label="Train months"><NumberInput value={trainM} integer onChange={setTrainM} testId="lab-val-train" /></Field>
            <Field label="Test months"><NumberInput value={testM} integer onChange={setTestM} testId="lab-val-test" /></Field>
            <Checkbox checked={anchored} onChange={setAnchored} label="Anchored (train from dataset start)" /></>}
          {kind !== "control" && <Checkbox checked={record} onChange={setRecord} label="Record windows as runs (needed for prop simulation)" />}
          {kind === "control" && <>
            <Field label="Realizations"><NumberInput value={nCtl} integer onChange={setNCtl} testId="lab-val-n" /></Field>
            <Field label="Base seed"><NumberInput value={seed} integer onChange={setSeed} /></Field>
            <Checkbox checked={oosCtl} onChange={setOosCtl} label="Restrict to the OOS window (needs the split date)" /></>}
        </div>
        <Button kind="primary" onClick={run} busy={busy} busyLabel="Running…" testId="lab-val-run"
          disabled={!ds.length || (needsSplit && !/^\d{4}-\d{2}-\d{2}$/.test(split))}>Run</Button>
        {busy && <p className="muted small">This runs synchronously through the engine; larger datasets or many realizations take a while.</p>}
      </Card>
      <ErrorPanel error={err} title="The validation was refused" testId="lab-val-error" />
      {rep && <ValidationView rep={rep} />}
      {ctl && <ControlView rep={ctl} />}
    </div>
  );
}

function Labels({ labels }: { labels: string[] }) {
  return <>{labels.map((l) => <Banner key={l} tone={l.startsWith("SYNTHETIC") ? "demo" : "warn"}>{l}</Banner>)}</>;
}

function ValidationView({ rep }: { rep: ValidationReport }) {
  const mc = rep.monte_carlo_oos?.bootstrap as Record<string, any> | undefined; // eslint-disable-line @typescript-eslint/no-explicit-any
  return (
    <Card title={<>{rep.validation === "oos" ? "Out-of-sample evaluation" : "Walk-forward evaluation"} <Mono>{rep.validation_id}</Mono></>} testId="lab-val-result">
      <Labels labels={rep.labels} />
      <TableWrap><table>
        <thead><tr><th>Window</th><th>Scope</th><th>Period</th><th>Run</th><th>Trades</th><th>Net R</th><th>Expectancy</th><th>PF</th><th>Max DD R</th><th /></tr></thead>
        <tbody>{rep.windows.map((w, i) => (
          <tr key={i}><td>{w.window.role}{w.segment !== undefined ? ` ${w.segment}` : ""}{w.partial ? " (partial)" : ""}</td><td><ScopeBadge status={w.status} /></td>
            <td className="small">{w.window.start.slice(0, 10)} → {w.window.end.slice(0, 10)}</td>
            <td>{w.run_id ? <a href={href(`/results/${w.run_id}`)}><Mono>{w.run_id}</Mono></a> : <span className="muted">not recorded</span>}</td>
            <td>{fmt(w.metrics.trade_count)}</td><td className="mono">{num(w.metrics.net_r, 2)}</td><td className="mono">{num(w.metrics.expectancy_r)}</td>
            <td className="mono">{num(w.metrics.profit_factor)}</td><td className="mono">{num(w.metrics.max_drawdown_r, 2)}</td>
            <td>{w.run_id && w.status !== "IN_SAMPLE" && <a href={href(`/prop?run=${w.run_id}`)}>Prop simulation</a>}</td></tr>))}
        </tbody></table></TableWrap>
      {rep.oos_pooled && <p className="small">Pooled walk-forward test windows: {fmt(rep.oos_pooled.trade_count)} trades, net {num(rep.oos_pooled.net_r, 2)} R,
        expectancy {num(rep.oos_pooled.expectancy_r)} R.</p>}
      {mc && <p className="small muted">Seeded bootstrap of the out-of-sample trades ({fmt(mc.n_sims)} resamples): total R percentiles
        {" "}{Object.entries(mc.total_r_percentiles ?? {}).map(([p, v]) => `p${p} ${num(v, 2)}`).join(" · ")}. Resampling observed trades; not a market simulation.</p>}
    </Card>
  );
}

function ControlView({ rep }: { rep: ControlReport }) {
  const keys = ["net_r", "expectancy_r", "profit_factor", "max_drawdown_r", "trade_count"];
  return (
    <Card title={<>Random-entry control <Mono>{rep.validation_id}</Mono></>} testId="lab-ctl-result">
      <Labels labels={rep.labels} />
      <KeyValues rows={[["Scope", <ScopeBadge status={rep.sample_status} />], ["Method", rep.control_config.method],
        ["Realizations", String(rep.control_config.n_controls)], ["Base seed", String(rep.control_config.base_seed)],
        ["Period", rep.control_config.period ? rep.control_config.period.join(" → ") : "whole dataset"],
        ["Candidate signals (pre-cooldown / final)", `${rep.candidate.pre_cooldown_signals} / ${rep.candidate.signals}`],
        ["Stored as runs", rep.stored_as_runs ? "yes" : "no (controls are never runs)"]]} />
      <TableWrap><table>
        <thead><tr><th>Metric</th><th>Candidate</th><th>Control median</th><th>Control p5 … p95</th><th>Fraction of controls exceeding candidate</th></tr></thead>
        <tbody>{keys.map((k) => { const c = rep.comparison[k] ?? {}; const p = c.percentiles ?? {};
          return <tr key={k}><td>{k}</td><td className="mono">{num(c.candidate)}</td><td className="mono">{num(c.median)}</td>
            <td className="mono">{num(p["5"])} … {num(p["95"])}</td><td className="mono">{fmt(c.fraction_of_controls_exceeding_candidate)}</td></tr>; })}
        </tbody></table></TableWrap>
      <p className="muted small">{String(rep.comparison.note ?? "")} Descriptive ranks within a conditional null; not p-values, not evidence for or against an edge.</p>
    </Card>
  );
}

// =========================================================================== comparison table
type Col = { key: string; label: string; get: (r: CompareRow) => unknown; numeric?: boolean };
export const COMPARE_COLS: Col[] = [
  { key: "trade_count", label: "Trades", get: (r) => r.metrics.trade_count, numeric: true },
  { key: "gross_r", label: "Gross R", get: (r) => r.metrics.gross_r, numeric: true },
  { key: "net_r", label: "Net R", get: (r) => r.metrics.net_r, numeric: true },
  { key: "cost_r", label: "Cost R", get: (r) => r.metrics.cost_r, numeric: true },
  { key: "expectancy_r", label: "Expectancy R", get: (r) => r.metrics.expectancy_r, numeric: true },
  { key: "profit_factor", label: "Profit factor", get: (r) => r.metrics.profit_factor, numeric: true },
  { key: "max_drawdown_r", label: "Max DD R", get: (r) => r.metrics.max_drawdown_r, numeric: true },
  { key: "breakeven", label: "Breakeven cost ×", get: (r) => r.breakeven_cost_multiplier, numeric: true },
];

export function CompareTable({ rows, selected, onSelect }: { rows: CompareRow[]; selected: string[]; onSelect: (ids: string[]) => void }) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 }>({ key: "run_id", dir: 1 });
  const [scope, setScope] = useState("");
  const [dataset, setDataset] = useState("");
  const [minTrades, setMinTrades] = useState<number | undefined>(undefined);
  const [text, setText] = useState("");
  const params = [...new Set(rows.flatMap((r) => Object.keys(r.parameters)))].sort();
  const col = COMPARE_COLS.find((c) => c.key === sort.key);
  const val = (r: CompareRow): unknown => col ? col.get(r) : sort.key.startsWith("p:") ? r.parameters[sort.key.slice(2)] : (r as unknown as Record<string, unknown>)[sort.key];
  const shown = rows.filter((r) => (!scope || r.status === scope) && (!dataset || r.dataset_id === dataset)
    && (minTrades === undefined || Number(r.metrics.trade_count ?? 0) >= minTrades)
    && (!text || `${r.strategy_id} ${r.strategy_name} ${r.run_id}`.toLowerCase().includes(text.toLowerCase())))
    .sort((a, b) => { const x = val(a), y = val(b);
      if (x === y) return 0; if (x === null || x === undefined) return 1; if (y === null || y === undefined) return -1;
      return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))) * sort.dir; });
  const th = (key: string, label: string) => (
    <th key={key}><button className="linklike" data-testid={`cmp-sort-${key}`} onClick={() => setSort({ key, dir: sort.key === key ? (sort.dir === 1 ? -1 : 1) : 1 })}>
      {label}{sort.key === key ? (sort.dir === 1 ? " ▲" : " ▼") : ""}</button></th>);
  const toggle = (id: string, on: boolean) => onSelect(on ? [...selected.filter((x) => x !== id), id] : selected.filter((x) => x !== id));
  return (
    <div data-testid="compare-view">
      <div className="filters">
        <Select value={scope} onChange={setScope} ariaLabel="scope filter" testId="cmp-scope" options={[{ value: "", label: "All scopes" },
          ...Object.entries(SCOPE).map(([k, v]) => ({ value: k, label: v.label }))]} />
        <Select value={dataset} onChange={setDataset} ariaLabel="dataset filter" options={[{ value: "", label: "All datasets" },
          ...[...new Set(rows.map((r) => r.dataset_id))].map((d) => ({ value: d, label: rows.find((r) => r.dataset_id === d)?.dataset_name ?? d }))]} />
        <NumberInput value={minTrades} integer onChange={setMinTrades} ariaLabel="minimum trades" testId="cmp-min-trades" />
        <TextInput value={text} onChange={setText} placeholder="Filter strategy / run" ariaLabel="text filter" />
      </div>
      <p className="muted small">{shown.length} of {rows.length} runs shown. Click a column to sort. Sorting is a view, not a ranking: no row is a
        recommendation, and in-sample rows are not comparable in standing to out-of-sample ones.</p>
      <TableWrap testId="compare-table"><table>
        <thead><tr><th />{th("run_id", "Run")}{th("strategy_id", "Strategy")}{th("status", "Scope")}{th("dataset_id", "Dataset")}
          {params.map((p) => th(`p:${p}`, p))}{COMPARE_COLS.map((c) => th(c.key, c.label))}<th>Sample</th><th>Costs</th><th>Prop</th></tr></thead>
        <tbody>{shown.map((r) => (
          <tr key={r.run_id} data-testid={`cmp-row-${r.run_id}`}>
            <td><input type="checkbox" aria-label={`select ${r.run_id}`} checked={selected.includes(r.run_id)}
              onChange={(e: { target: HTMLInputElement }) => toggle(r.run_id, e.target.checked)} /></td>
            <td><a href={href(`/results/${r.run_id}`)}><Mono>{r.run_id}</Mono></a></td>
            <td><a href={href(`/strategies/${r.strategy_id}`)}><Mono>{r.strategy_id}</Mono></a><div className="small muted">{r.strategy_name}
              {r.generation_method ? ` · ${METHOD_LABEL[r.generation_method] ?? r.generation_method}` : ""}</div></td>
            <td><ScopeBadge status={r.status} />{r.synthetic && <Badge tone="demo">synthetic</Badge>}</td>
            <td className="small">{r.dataset_name ?? r.dataset_id}</td>
            {params.map((p) => <td key={p} className="mono">{fmt(r.parameters[p])}</td>)}
            {COMPARE_COLS.map((c) => <td key={c.key} className="mono" title={c.key === "breakeven" ? r.breakeven_note ?? undefined : undefined}>
              {num(c.get(r), c.key === "trade_count" ? 0 : 3)}</td>)}
            <td className="small">{fmt(r.metrics.sample_label)}</td><td className="small" title={r.cost_basis ?? ""}>{r.cost_status}{r.cost_scenario ? ` · ${r.cost_scenario}` : ""}</td><td>{r.prop_simulations}</td>
          </tr>))}
        </tbody></table></TableWrap>
      {shown.some((r) => r.synthetic) && <Banner tone="demo">{SYNTHETIC_NOTICE}</Banner>}
    </div>
  );
}
