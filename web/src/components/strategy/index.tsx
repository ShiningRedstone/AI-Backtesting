import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, viewCache } from "../../api/client";
import type { ProtocolRecordRow, BacktestJob, BacktestResult, Change, DatasetRow, Readiness, VariantRow, VariationPreview, VariationResult } from "../../api/types";
import { href } from "../../app/router";
import { useApp } from "../../app/context";
import type { ParamDecl, StrategyDoc } from "../../dsl/types";
import { datasetLabel, facetLabel, humanize, metricLabel, plainProse, statusLabel, strategyLabel, valueLabel } from "../../app/labels";
import { Badge, Banner, Button, Checkbox, ErrorPanel, Field, IssueList, KeyValues, Loading, Mono, NumberInput, Select, TableWrap, TechDetails, TextInput, fmt } from "../ui";

export const SYNTHETIC_NOTICE = "Synthetic demonstration — not evidence of trading performance.";
export const METHOD_LABEL: Record<string, string> = {
  user: "Written by you", manual_edit: "Edited", duplicate: "Duplicate", mode_a_variation: "Variation", mode_b_proposal: "AI proposal",
  child: "Derived version", factory_variant: "Factory variant",
};
export const methodLabel = (m: string | null | undefined) => (m ? METHOD_LABEL[m] ?? humanize(m) : "—");
/** A stored scalar as words (objects stay as compact text). */
export const plainValue = (v: unknown) => (v !== null && typeof v === "object" && !Array.isArray(v) ? fmt(v) : typeof v === "number" ? fmt(v) : valueLabel(v));
export const changesText = (cs: Change[]) => cs.map((c) => `${humanize(c.parameter)}: ${plainValue(c.old)} → ${plainValue(c.new)}`).join(", ");
/** A parameter combination ({ema_period: 10, use_filter: true}) as words. */
export const comboText = (c: Record<string, unknown>) => Object.entries(c).map(([k, v]) => `${humanize(k)} ${plainValue(v)}`).join(", ") || "—";
/** Dataset quality verdict as words. */
export const qualityLabel = (q: string) => (q === "WARN" ? "Passed with warnings" : statusLabel(q));

// =========================================================================== lineage
export interface TreeNode { strategy_id: string; name: string | null; generation_method: string; parents: string[]; changes: Change[]; archived?: boolean }

export function LineageTree({ nodes, focus }: { nodes: TreeNode[]; focus?: string }) {
  const ids = new Set(nodes.map((n) => n.strategy_id));
  const kids = new Map<string, TreeNode[]>();
  const roots: TreeNode[] = [];
  for (const n of nodes) {
    const p = n.parents.find((x) => ids.has(x));             // placed under its first known parent
    if (p) kids.set(p, [...(kids.get(p) ?? []), n]); else roots.push(n);
  }
  const label = (id: string) => nodeLabel(nodes.find((x) => x.strategy_id === id), id, focus);
  const Node = ({ n, seen }: { n: TreeNode; seen: Set<string> }) => {
    const children = seen.has(n.strategy_id) ? [] : (kids.get(n.strategy_id) ?? []);
    const next = new Set(seen).add(n.strategy_id);
    const extra = n.parents.filter((p) => ids.has(p)).slice(1);
    return (
      <li>
        <div className={`tree-node${n.strategy_id === focus ? " focus" : ""}`} data-testid={`tree-${n.strategy_id}`}>
          <a href={href(`/strategies/${n.strategy_id}`)} title={n.strategy_id}>{label(n.strategy_id)}</a>
          <Badge tone={n.generation_method === "mode_a_variation" ? "info" : "neutral"}>{methodLabel(n.generation_method)}</Badge>
          {n.archived && <Badge tone="warn">archived</Badge>}
          {n.changes.length > 0 && <span className="muted small">{changesText(n.changes)}</span>}
          {extra.length > 0 && <span className="muted small">also derived from {extra.map(label).join(", ")}</span>}
        </div>
        {children.length > 0 && <ul>{children.map((c) => <Node key={c.strategy_id} n={c} seen={next} />)}</ul>}
      </li>
    );
  };
  return <ul className="tree" aria-label="lineage tree">{roots.map((r) => <Node key={r.strategy_id} n={r} seen={new Set()} />)}</ul>;
}

/** A lineage node as words: its strategy name, else its place relative to the focused version (the id is a tooltip). */
export function nodeLabel(n: TreeNode | undefined, id: string, focus?: string): string {
  if (n?.name) return strategyLabel(n.name);
  if (id === focus) return "This version";
  return n?.generation_method === "child" ? "Derived version" : n ? "Earlier version" : "Version outside this view";
}

export function LineageTable({ nodes, focus }: { nodes: TreeNode[]; focus?: string }) {
  const label = (id: string) => nodeLabel(nodes.find((x) => x.strategy_id === id), id, focus);
  return (<>
    <TableWrap testId="lineage-table">
      <table>
        <caption className="sr-only">Lineage records</caption>
        <thead><tr><th>Strategy</th><th>Method</th><th>Parent(s)</th><th>Changes vs parent</th></tr></thead>
        <tbody>{nodes.map((n) => (
          <tr key={n.strategy_id}>
            <td><a href={href(`/strategies/${n.strategy_id}`)} title={n.strategy_id}>{label(n.strategy_id)}</a></td>
            <td>{methodLabel(n.generation_method)}</td>
            <td>{n.parents.length ? n.parents.map((p) => <div key={p} title={p}>{label(p)}</div>) : <span className="muted">root</span>}</td>
            <td className="small">{changesText(n.changes) || "—"}</td>
          </tr>))}
        </tbody>
      </table>
    </TableWrap>
    <TechDetails summary="Technical details (strategy IDs)">
      <table><tbody>{nodes.map((n) => <tr key={n.strategy_id}><td>{label(n.strategy_id)}</td><td><Mono>{n.strategy_id}</Mono></td></tr>)}</tbody></table>
    </TechDetails>
  </>);
}

// =========================================================================== metrics
const GROUPS: [string, string[]][] = [
  ["Sample", ["trade_count", "sample_label", "trades_per_week"]],
  ["Outcome (R multiples)", ["net_r", "gross_r", "cost_r", "expectancy_r", "expectancy_se", "expectancy_ci95", "median_r", "std_r",
    "win_rate", "win_rate_ci95", "loss_rate", "avg_winner_r", "avg_loser_r", "best_trade_r", "worst_trade_r",
    "profit_factor", "profit_factor_gross", "sharpe_like_per_trade", "sortino_like_per_trade"]],
  ["Money", ["net_usd", "max_drawdown_usd"]],
  ["Risk & streaks", ["max_drawdown_r", "max_win_streak", "max_loss_streak"]],
  ["Holding", ["avg_hold_minutes", "median_hold_minutes"]],
  ["Assumptions", ["cost_multiplier", "conflict_bars"]],
];

export function MetricsView({ metrics }: { metrics: Record<string, unknown> }) {
  const known = new Set(GROUPS.flatMap(([, ks]) => ks).concat(["exit_reasons"]));
  const other = Object.keys(metrics).filter((k) => !known.has(k));
  const label = String(metrics.sample_label ?? "");
  return (
    <div className="metrics" data-testid="metrics">
      <div className="inline"><Badge tone={label.startsWith("LOW") ? "warn" : "neutral"}>{label ? humanize(label) : "no sample label"}</Badge>
        <span className="muted small">Measurements under the stated assumptions. No ranking, significance verdict or edge label is implied.</span></div>
      <div className="metric-groups">
        {[...GROUPS, ["Other", other] as [string, string[]]].map(([g, ks]) => {
          const rows = ks.filter((k) => k in metrics);
          return rows.length ? (
            <div key={g} className="metric-group"><h4>{g}</h4>
              <KeyValues rows={rows.map((k) => [metricLabel(k), <span className="mono">{plainValue(metrics[k])}</span>])} />
            </div>) : null;
        })}
      </div>
    </div>
  );
}

// =========================================================================== backtest
export function DatasetSummary({ d }: { d: DatasetRow }) {
  return <>
    <KeyValues rows={[
      ["Dataset", <span title={d.dataset_id}>{datasetLabel(d.dataset_id)}</span>], ["Instrument", d.instrument], ["Asset type", valueLabel(d.asset_type)],
      ["Provider", valueLabel(d.provider)], ["Timeframe", facetLabel("timeframe", d.timeframe)], ["Range", `${d.start?.slice(0, 10)} → ${d.end?.slice(0, 10)}`],
      ["Bars", fmt(d.n_bars)], ["Price basis", valueLabel(d.price_basis)], ["Validation", qualityLabel(d.quality_status)],
      ["Cost profile", <>{valueLabel(d.cost.status)}{d.cost.profile ? ` (${humanize(d.cost.profile)})` : ""}</>]]} />
    <TechDetails rows={[["Dataset ID", <Mono>{d.dataset_id}</Mono>], ["Cost profile ID", d.cost.profile ? <Mono>{d.cost.profile}</Mono> : null]]} />
  </>;
}

export function BacktestPanel({ strategy }: { strategy: string | StrategyDoc }) {
  const [ready, setReady] = useState<Readiness | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [pick, setPick] = useState<string>("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [runErr, setRunErr] = useState<ApiError | null>(null);
  const [protocol, setProtocol] = useState<ProtocolRecordRow | null>(null);
  const { reloadPrefs } = useApp();
  // ADR-76: the backtest runs as a background job; the job id is remembered for this strategy so leaving the page and
  // coming back picks the run (or its result) up again instead of losing it.
  const jobKey = `munyun.backtest-job.${typeof strategy === "string" ? strategy : JSON.stringify(strategy).length + ":" + (strategy as StrategyDoc).name}`;
  const [jobId, setJobId] = useState<string | null>(() => { try { return window.sessionStorage.getItem(jobKey); } catch { return null; } });
  useEffect(() => {
    if (!jobId) return;
    let live = true, t = 0;
    setRunning(true);
    const poll = () => api.get<BacktestJob>(`/api/backtests/jobs/${jobId}`).then((j) => {
      if (!live) return;
      if (j.state === "running") { t = window.setTimeout(poll, 1000); return; }
      setRunning(false);
      if (j.state === "completed" && j.result) { setResult(j.result); setRunErr(null); viewCache.clear(); reloadPrefs(); }
      else setRunErr(new ApiError(400, j.error?.kind ?? "failed", j.error?.message ?? "The backtest failed"));
      try { window.sessionStorage.removeItem(jobKey); } catch { /* ignore */ }
      setJobId(null);
    }).catch(() => {                                   // unknown after an app restart: forget it
      if (!live) return;
      setRunning(false); setJobId(null);
      try { window.sessionStorage.removeItem(jobKey); } catch { /* ignore */ }
    });
    poll();
    return () => { live = false; window.clearTimeout(t); };
  }, [jobId]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {                                   // ADR-70: the governed window comes from the protocol record (data)
    api.get<ProtocolRecordRow[]>("/api/protocols").then((rows) => setProtocol(rows.find((p) => p.status === "ACTIVE") ?? null))
      .catch(() => setProtocol(null));
  }, []);
  useEffect(() => {
    api.post<Readiness>("/api/backtests/readiness", { strategy }).then((r) => {
      setReady(r); setErr(null);
      const pref = r.datasets.find((d) => d.dataset_id === r.preferred_dataset_id);
      if (pref?.runnable) setPick((cur) => cur || pref.dataset_id);        // new research starts on the preferred dataset
    }).catch(setErr);
  }, [typeof strategy === "string" ? strategy : JSON.stringify(strategy)]); // eslint-disable-line react-hooks/exhaustive-deps
  if (err) return <ErrorPanel error={err} />;
  if (!ready) return <Loading label="Checking datasets…" />;
  const sel = ready.datasets.find((d) => d.dataset_id === pick);
  const anyRunnable = ready.datasets.some((d) => d.runnable);
  const run = async () => {
    setRunning(true); setRunErr(null); setResult(null);
    try {
      const period = protocol?.discovery_period;        // explicit discovery window under a protocol; never the holdout
      const j = await api.post<BacktestJob>("/api/backtests/jobs", { strategy, dataset_id: pick, ...(period ? { period } : {}) });
      try { window.sessionStorage.setItem(jobKey, j.job_id); } catch { /* per-tab convenience only */ }
      setJobId(j.job_id);
    }
    catch (e) { setRunErr(e as ApiError); setRunning(false); }
  };
  return (
    <div className="backtest" data-testid="backtest-panel">
      <p className="muted small">One causality-checked run through the existing engine (Phase 1), recorded in the run registry with status In-sample.
        Strategy timeframe: <b>{ready.strategy_timeframe ? facetLabel("timeframe", ready.strategy_timeframe) : "?"}</b>. Only validated, timeframe-compatible datasets with configured costs can be selected.</p>
      {protocol && <p className="small" data-testid="bt-protocol-window">Research protocol <b title={protocol.protocol_id}>{valueLabel(protocol.name) === "—" ? "(active)" : valueLabel(protocol.name)}</b> governs this instrument:
        the run uses its <b>discovery window</b> {protocol.discovery_trading_dates?.join(" → ")} and counts as a protocol trial;
        the holdout {protocol.holdout_trading_dates?.join(" → ")} stays locked.</p>}
      {!ready.datasets.length && (
        <Banner tone="warn" testId="no-datasets"><b>No compatible validated dataset available.</b> Import a dataset first
          (<a href={href("/datasets")}>Datasets</a>).</Banner>)}
      {ready.datasets.length > 0 && !anyRunnable && (
        <Banner tone="warn" testId="no-datasets"><b>No compatible validated dataset available.</b> See the reasons below, or import a dataset first.</Banner>)}
      {ready.datasets.length > 0 && (
        <TableWrap testId="dataset-select">
          <table>
            <thead><tr><th /><th>Dataset</th><th>Instrument</th><th>Asset</th><th>Provider</th><th>Timeframe</th><th>Range</th><th>Bars</th>
              <th>Price basis</th><th>Validation</th><th>Costs</th><th>Status</th></tr></thead>
            <tbody>{ready.datasets.map((d) => (
              <tr key={d.dataset_id} className={d.runnable ? "" : "disabled-row"} data-testid={`ds-${d.dataset_id}`}>
                <td><input type="radio" name="bt-dataset" aria-label={`select ${datasetLabel(d.dataset_id)}`} disabled={!d.runnable}
                  checked={pick === d.dataset_id} onChange={() => setPick(d.dataset_id)} /></td>
                <td title={d.dataset_id}>{datasetLabel(d.dataset_id)}{d.synthetic && <> <Badge tone="demo">synthetic</Badge></>}
                  {d.preferred && <> <Badge tone="info">preferred</Badge></>}</td>
                <td>{plainProse(d.instrument)}</td><td>{valueLabel(d.asset_type)}</td><td>{valueLabel(d.provider)}</td><td>{facetLabel("timeframe", d.timeframe)}</td>
                <td className="small">{d.start?.slice(0, 10)} → {d.end?.slice(0, 10)}</td><td>{fmt(d.n_bars)}</td>
                <td>{valueLabel(d.price_basis)}</td><td><Badge tone={d.quality_status === "FAIL" ? "error" : d.quality_status === "WARN" ? "warn" : "ok"}>{qualityLabel(d.quality_status)}</Badge></td>
                <td><Badge tone={d.cost.status === "unconfigured" ? "error" : "neutral"}>{valueLabel(d.cost.status)}</Badge></td>
                <td className="small">{d.runnable ? "ready" : d.reasons.map((r) => <div key={r}>{r}</div>)}</td>
              </tr>))}
            </tbody>
          </table>
        </TableWrap>
      )}
      {ready.datasets.filter((d) => d.asset_type === "CFD" && d.cost.status === "unconfigured").length > 0 && (
        <Banner tone="warn" testId="cfd-unavailable"><b>CFD backtest unavailable.</b> Reason: Broker/provider cost profile is unconfigured.
          Configure verified costs (configs/costs.yaml) before running research. Munyun Lab never assumes CFD spreads, commissions or slippage.</Banner>)}
      {sel && (
        <div className="bt-confirm">
          <h3>Run on</h3>
          <DatasetSummary d={sel} />
          {sel.synthetic && <Banner tone="demo">{SYNTHETIC_NOTICE}</Banner>}
          <Button kind="primary" onClick={run} busy={running} busyLabel="Running backtest…" testId="run-backtest">Run Backtest</Button>
          {running && <span className="small muted" data-testid="backtest-running">Runs in the background: you can use other pages and come back.</span>}
        </div>
      )}
      <ErrorPanel error={runErr} title="The backtest did not run" testId="backtest-error" />
      {result && <BacktestView result={result} />}
    </div>
  );
}

export function BacktestView({ result }: { result: BacktestResult }) {
  return (
    <div className="bt-result" data-testid="backtest-result">
      {result.synthetic && <Banner tone="demo" testId="synthetic-banner"><b>{SYNTHETIC_NOTICE}</b></Banner>}
      <KeyValues rows={[
        ["Run", result.run_id ? <a href={href(`/results/${result.run_id}`)} title={result.run_id}>Open the recorded run</a> : "not recorded"],
        ["Dataset", <span title={result.dataset_id}>{datasetLabel(result.dataset_id)}</span>],
        ["Cost profile status", valueLabel(result.cost_status)], ["Signals", fmt(result.n_signals)],
        ["Exit reasons", Object.entries(result.exit_reasons).map(([k, v]) => `${humanize(k)} ${v}`).join(" · ") || "—"],
        ["Skipped signals", Object.entries(result.skipped).map(([k, v]) => `${humanize(k)} ${v}`).join(" · ") || "—"]]} />
      <TechDetails rows={[["Run ID", result.run_id ? <Mono>{result.run_id}</Mono> : null], ["Strategy ID", <Mono>{result.strategy_id}</Mono>],
        ["Dataset ID", <Mono>{result.dataset_id}</Mono>], ["Trades hash", <Mono>{result.trades_hash}</Mono>]]} />
      <MetricsView metrics={result.metrics} />
      <p className="muted small">{result.note}</p>
    </div>
  );
}

// =========================================================================== variations
interface DimState { explore: boolean; how: "range" | "values"; min?: number; max?: number; step?: number; valuesText: string; picks: string[]; category: string }

const PARAM_TYPE_LABEL: Record<string, string> = {
  integer: "whole number", float: "decimal number", boolean: "on / off", choice: "choice", timeframe: "timeframe",
};
/** A parameter value as words (timeframes "5m" -> "5 min", booleans On / Off). */
function choiceLabel(d: ParamDecl, c: string): string {
  if (d.type === "boolean") return c === "true" ? "On" : c === "false" ? "Off" : c;
  if (d.type === "timeframe") return facetLabel("timeframe", c);
  if (d.type === "choice") return humanize(c);
  return c === "" ? "—" : c;
}

function initDim(name: string, d: ParamDecl, timeframes: string[]): DimState {
  const choices = d.type === "boolean" ? ["false", "true"] : d.type === "timeframe" ? (d.choices ?? timeframes).map(String)
    : (d.choices ?? []).map(String);
  return { explore: false, how: d.type === "integer" || d.type === "float" ? (d.step ? "range" : "values") : "values",
    min: d.min, max: d.max, step: d.step, valuesText: String(d.value ?? ""), picks: choices, category: name };
}

function dimValues(d: ParamDecl, s: DimState): unknown[] {
  if (d.type === "boolean") return s.picks.map((p) => p === "true");
  if (d.type === "choice" || d.type === "timeframe") return s.picks;
  return s.valuesText.split(",").map((x) => x.trim()).filter(Boolean).map(Number);
}

export function VariationBuilder({ baseId, base }: { baseId: string; base: StrategyDoc }) {
  const { options } = useApp();
  const params = base.parameters ?? {};
  const names = Object.keys(params);
  const [dims, setDims] = useState<Record<string, DimState>>(() =>
    Object.fromEntries(names.map((n) => [n, initDim(n, params[n], options?.timeframes ?? [])])));
  const [mode, setMode] = useState("grid");
  const [maxV, setMaxV] = useState<number | undefined>(100);
  const [seed, setSeed] = useState<number | undefined>(1);
  const [sample, setSample] = useState<number | undefined>(10);
  const [specName, setSpecName] = useState(`${base.name}_variations`);
  const [preview, setPreview] = useState<VariationPreview | null>(null);
  const [previewErr, setPreviewErr] = useState<ApiError | null>(null);
  const [generating, setGenerating] = useState(false);
  const [genErr, setGenErr] = useState<ApiError | null>(null);
  const [result, setResult] = useState<VariationResult | null>(null);
  const seq = useRef(0);

  const spec = useMemo(() => {
    const dimensions = names.filter((n) => dims[n]?.explore).map((n) => {
      const d = params[n], s = dims[n];
      const range = (d.type === "integer" || d.type === "float") && s.how === "range";
      return { parameter: n, category: s.category || "parameter",
        ...(range ? { range: { min: s.min, max: s.max, step: s.step } } : { values: dimValues(d, s) }) };
    });
    return { variation_spec_version: 1, name: specName, mode, max_variants: maxV, dimensions,
      ...(mode === "random_sample" ? { seed, sample_size: sample } : {}) };
  }, [dims, mode, maxV, seed, sample, specName]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const my = ++seq.current;
    const t = window.setTimeout(() => {
      api.post<VariationPreview>("/api/variations/preview", { base: baseId, spec })
        .then((p) => { if (my === seq.current) { setPreview(p); setPreviewErr(null); } })
        .catch((e: ApiError) => { if (my === seq.current) setPreviewErr(e); });
    }, 250);
    return () => window.clearTimeout(t);
  }, [spec, baseId]);

  if (!names.length) {
    return <Banner tone="info">This strategy declares no parameters, so there is nothing controlled variations may vary. Add parameters in the
      <a href={href(`/builder/${baseId}`)}> builder</a> and save the new instance.</Banner>;
  }
  const set = (n: string, patch: Partial<DimState>) => setDims((d) => ({ ...d, [n]: { ...d[n], ...patch } }));
  const generate = async () => {
    setGenerating(true); setGenErr(null); setResult(null);
    try { setResult(await api.post<VariationResult>("/api/variations", { base: baseId, spec, save: true })); }
    catch (e) { setGenErr(e as ApiError); }
    finally { setGenerating(false); }
  };
  return (
    <div className="variations" data-testid="variation-builder">
      <p className="muted small">Only parameters declared by the base strategy can vary, and only inside their declared domains. The backend
        validates and compiles every child, removes logic duplicates, and refuses (never truncates) batches above the cap.</p>
      <div className="grid3">
        <Field label="Mode">
          <Select value={mode} onChange={setMode} testId="var-mode" options={[
            { value: "grid", label: "Grid (all combinations)" }, { value: "one_at_a_time", label: "One-at-a-time" },
            { value: "random_sample", label: "Random sample (seeded)" }]} />
        </Field>
        <Field label="Maximum variants (cap)"><NumberInput value={maxV} integer onChange={setMaxV} testId="var-max" /></Field>
        <Field label="Batch name"><TextInput value={specName} onChange={setSpecName} /></Field>
        {mode === "random_sample" && <>
          <Field label="Seed (required)"><NumberInput value={seed} integer onChange={setSeed} testId="var-seed" /></Field>
          <Field label="Sample size"><NumberInput value={sample} integer onChange={setSample} testId="var-sample" /></Field>
        </>}
      </div>
      <div className="dims">
        {names.map((n) => {
          const d = params[n], s = dims[n];
          const numeric = d.type === "integer" || d.type === "float";
          const choices = d.type === "boolean" ? ["false", "true"] : d.type === "timeframe"
            ? (d.choices ?? options?.timeframes ?? []).map(String) : (d.choices ?? []).map(String);
          return (
            <div key={n} className={`dim${s.explore ? " on" : ""}`} data-testid={`dim-${n}`}>
              <div className="dim-head">
                <Checkbox checked={s.explore} onChange={(v) => set(n, { explore: v })} testId={`dim-${n}-explore`}
                  label={<><b title={`$${n}`}>{humanize(n)}</b> <span className="muted small">{PARAM_TYPE_LABEL[d.type] ?? humanize(d.type)}, base {choiceLabel(d, String(d.value ?? ""))}
                    {numeric && d.min !== undefined ? `, allowed ${d.min} to ${d.max} in steps of ${d.step ?? "—"}` : ""}</span></>} />
              </div>
              {s.explore && (
                <div className="dim-body">
                  {numeric && (
                    <div className="segmented small">
                      <button className={s.how === "range" ? "on" : ""} onClick={() => set(n, { how: "range" })}>Range</button>
                      <button className={s.how === "values" ? "on" : ""} onClick={() => set(n, { how: "values" })}>Values</button>
                    </div>)}
                  {numeric && s.how === "range" && <>
                    <Field label="Min"><NumberInput value={s.min} onChange={(v) => set(n, { min: v })} testId={`dim-${n}-min`} /></Field>
                    <Field label="Max"><NumberInput value={s.max} onChange={(v) => set(n, { max: v })} testId={`dim-${n}-max`} /></Field>
                    <Field label="Step"><NumberInput value={s.step} onChange={(v) => set(n, { step: v })} testId={`dim-${n}-step`} /></Field>
                  </>}
                  {numeric && s.how === "values" && (
                    <Field label="Values (comma-separated)"><TextInput value={s.valuesText} onChange={(v) => set(n, { valuesText: v })} testId={`dim-${n}-values`} /></Field>)}
                  {!numeric && (
                    <div className="checks">{choices.map((c) => (
                      <Checkbox key={c} label={choiceLabel(d, c)} checked={s.picks.includes(c)} testId={`dim-${n}-pick-${c}`}
                        onChange={(on) => set(n, { picks: on ? choices.filter((x) => x === c || s.picks.includes(x)) : s.picks.filter((x) => x !== c) })} />))}
                    </div>)}
                  <Field label="Category"><TextInput value={s.category} onChange={(v) => set(n, { category: v })} /></Field>
                </div>)}
            </div>
          );
        })}
      </div>
      <div className="var-preview" data-testid="variation-preview">
        {previewErr && <ErrorPanel error={previewErr} />}
        {preview && <>
          <KeyValues rows={[
            ["Estimated combinations", <b data-testid="var-count">{preview.combinations !== undefined ? fmt(preview.combinations) : "—"}</b>],
            ["Maximum allowed", fmt(preview.max_variants)], ["Mode", humanize(preview.mode ?? mode)],
            ...(preview.full_grid !== undefined && preview.full_grid !== preview.combinations ? [["Full grid", fmt(preview.full_grid)] as [string, string]] : [])]} />
          <IssueList issues={[...preview.errors, ...(preview.warnings ?? [])]} testId="variation-issues" />
          {preview.combinations_list && preview.combinations_list.length > 0 && (
            <details open={preview.combinations_list.length <= 30} data-testid="var-combos">
              <summary>Exact combinations to generate ({preview.combinations_list.length})</summary>
              <TableWrap><table><thead><tr><th>#</th>{Object.keys(preview.values ?? {}).map((p) => <th key={p}>{humanize(p)}</th>)}</tr></thead>
                <tbody>{preview.combinations_list.map((c, i) => (
                  <tr key={i}><td>{i + 1}</td>{Object.keys(preview.values ?? {}).map((p) => <td key={p} className="mono">{p in c ? fmt(c[p]) : <span className="muted">base</span>}</td>)}</tr>))}
                </tbody></table></TableWrap>
              <p className="muted small">Combinations identical to the base or to each other in logic are removed by the backend after compiling.</p>
            </details>)}
        </>}
        <Button kind="primary" onClick={generate} busy={generating} testId="generate"
          busyLabel={`Generating ${preview?.combinations ?? ""} variations…`}
          disabled={!preview?.ok || !spec.dimensions.length}
          title={!spec.dimensions.length ? "choose at least one parameter to explore" : !preview?.ok ? "fix the issues above" : undefined}>
          Generate Variations</Button>
      </div>
      <ErrorPanel error={genErr} title="Variation generation failed" testId="variation-error" />
      {result && <Banner tone="ok" testId="var-next">Variation batch saved. Next:{" "}
        <a href={href(`/strategies/${baseId}?tab=research&batch=${result.batch_id}`)} data-testid="var-run-batch">run it on datasets</a>
        {" "}(Research tab), then compare the results.</Banner>}
      {result && <VariationResults result={{
        batch_id: result.batch_id, base_strategy_id: result.base_strategy_id, combinations: result.combinations,
        generated: result.generated, duplicates: result.duplicates, same_as_base: result.same_as_base_combinations,
        varied: result.varied_parameters, rows: result.variants }} />}
    </div>
  );
}

export interface VariationView {
  batch_id: string; base_strategy_id: string; combinations: number; generated: number; varied: string[]; rows: VariantRow[];
  duplicates: { combination: Record<string, unknown>; duplicate_of: string }[];
  same_as_base: { combination: Record<string, unknown>; strategy_id: string }[];
}

export function VariationResults({ result }: { result: VariationView }) {
  const [compare, setCompare] = useState<string[]>([]);
  const cmpRows = result.rows.filter((r) => compare.includes(r.strategy_id));
  const sameAs = (id: string) => id === result.base_strategy_id ? "The base strategy"
    : strategyLabel(result.rows.find((r) => r.strategy_id === id)?.name ?? "another stored version");
  return (
    <div className="var-results" data-testid="variation-results">
      <h3>Variation batch</h3>
      <div className="stats">
        <div><b data-testid="stat-combinations">{result.combinations}</b><span>combinations</span></div>
        <div><b data-testid="stat-unique">{result.generated}</b><span>unique strategies</span></div>
        <div><b data-testid="stat-duplicates">{result.duplicates.length}</b><span>duplicates</span></div>
        <div><b data-testid="stat-same">{result.same_as_base.length}</b><span>identical to base</span></div>
      </div>
      <p className="muted small">Generated from <a href={href(`/strategies/${result.base_strategy_id}`)} title={result.base_strategy_id}>the base strategy</a>.
        Every listed child was validated and compiled by the backend. No performance columns: nothing has been run.</p>
      <TableWrap testId="variants-table">
        <table>
          <thead><tr><th>Compare</th><th>Strategy</th>{result.varied.map((p) => <th key={p}>{humanize(p)}</th>)}<th>Status</th><th>Actions</th></tr></thead>
          <tbody>{result.rows.map((r) => (
            <tr key={r.strategy_id}>
              <td><input type="checkbox" aria-label={`compare ${strategyLabel(r.name)}`} checked={compare.includes(r.strategy_id)}
                onChange={(e: { target: HTMLInputElement }) => setCompare((c) => e.target.checked ? [...c, r.strategy_id] : c.filter((x) => x !== r.strategy_id))} /></td>
              <td className="small"><a href={href(`/strategies/${r.strategy_id}`)} title={r.strategy_id}>{strategyLabel(r.name)}</a></td>
              {result.varied.map((p) => <td key={p} className="mono">{fmt(r.overrides?.[p])}</td>)}
              <td><Badge tone="ok">valid</Badge></td>
              <td className="row-actions">
                <a href={href(`/strategies/${r.strategy_id}`)}>Open</a>
                <a href={href(`/strategies/${r.strategy_id}?tab=lineage`)}>Lineage</a>
              </td>
            </tr>))}
          </tbody>
        </table>
      </TableWrap>
      {cmpRows.length >= 2 && (
        <div data-testid="compare-table"><h3>Compare</h3>
          <TableWrap><table>
            <thead><tr><th>Parameter</th>{cmpRows.map((r) => <th key={r.strategy_id} title={r.strategy_id}>{strategyLabel(r.name)}</th>)}</tr></thead>
            <tbody>{result.varied.map((p) => {
              const vals = cmpRows.map((r) => fmt(r.overrides?.[p]));
              return <tr key={p} className={new Set(vals).size > 1 ? "differs" : ""}><td>{humanize(p)}</td>{vals.map((v, i) => <td key={i} className="mono">{v}</td>)}</tr>;
            })}</tbody>
          </table></TableWrap>
        </div>)}
      {compare.length === 1 && <p className="muted small">Select at least two strategies to compare.</p>}
      {result.duplicates.length > 0 && (
        <details><summary>{result.duplicates.length} combinations removed as logic duplicates</summary>
          <TableWrap><table><thead><tr><th>Combination</th><th>Same logic as</th></tr></thead>
            <tbody>{result.duplicates.map((d, i) => <tr key={i}><td className="small">{comboText(d.combination)}</td><td title={d.duplicate_of}>{sameAs(d.duplicate_of)}</td></tr>)}</tbody>
          </table></TableWrap>
        </details>)}
      {result.same_as_base.length > 0 && (
        <details><summary>{result.same_as_base.length} combinations identical to the base</summary>
          <ul>{result.same_as_base.map((d, i) => <li key={i} className="small">{comboText(d.combination)}</li>)}</ul>
        </details>)}
      <TechDetails rows={[["Batch ID", <Mono>{result.batch_id}</Mono>], ["Base strategy ID", <Mono>{result.base_strategy_id}</Mono>]]}>
        <table><tbody>{result.rows.map((r) => <tr key={r.strategy_id}><td>{strategyLabel(r.name)}</td><td><Mono>{r.strategy_id}</Mono></td></tr>)}</tbody></table>
      </TechDetails>
    </div>
  );
}
