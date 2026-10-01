import { useEffect, useState } from "react";
import { ApiError } from "../api/client";
import { JOB_FINAL, POLL_MS, research } from "../api/research";
import type { BatchRow, DatasetRow, JobStatus, LibraryRow, Ranking, RankingMetric, SampleLabel, SearchBatch, SearchCell,
  SearchDetail, SearchPlan, SearchSpec, SearchValidation } from "../api/types";
import { useApi, useApp } from "../app/context";
import { datasetLabel, facetLabel, familyLabel, humanize, metricLabel, statusLabel, strategyLabel } from "../app/labels";
import { go, href, useRoute } from "../app/router";
import { plainText } from "../components/research";
import { Badge, Banner, Button, Card, Checkbox, Empty, ErrorPanel, Field, IssueList, KeyValues, Kpi, Loading, Mono,
  NumberInput, Select, TableWrap, TechDetails, TextInput, fmt, shortTime } from "../components/ui";
import type { ProtocolRecordRow, ProtocolStatus } from "../api/types";

// Phase 4 research: plan and run strategy x dataset searches over the /api/research service contracts.
// Every number shown here is an IN-SAMPLE measurement under stated assumptions; nothing is validated.

const METRICS: RankingMetric[] = ["expectancy_r", "profit_factor", "net_r"];
const SAMPLES: SampleLabel[] = ["LOW SAMPLE SIZE", "MODERATE SAMPLE", "ADEQUATE SAMPLE"];
const METRIC_OPTIONS = METRICS.map((m) => ({ value: m, label: metricLabel(m) }));
const SAMPLE_OPTIONS = SAMPLES.map((s) => ({ value: s, label: humanize(s) }));
const IN_SAMPLE = "IN-SAMPLE research results · NOT VALIDATED · not a forecast";

const tone = (s: string | undefined): "ok" | "warn" | "error" | "info" | "neutral" =>
  s === "completed" ? "ok" : s === "failed" ? "error" : s === "cancelled" || s === "interrupted" ? "warn"
    : s === "running" || s === "queued" ? "info" : "neutral";
// Plain one-word states (running, completed, cancelled …) are shown as they are; codes become words.
const StatusBadge = ({ s }: { s: string | undefined }) => <Badge tone={tone(s)}>{s && /^[a-z]+$/.test(s) ? s : statusLabel(s)}</Badge>;
/** Strategy machine names by id, for showing names instead of ids in tables. */
type Names = (sid: string) => string;
function useStrategyNames(): Names {
  const lib = useApi<LibraryRow[]>("/api/strategies");
  return (sid) => { const row = lib.data?.find((x) => x.strategy_id === sid); return row ? strategyLabel(row.name) : lib.data ? "Strategy not in the library" : "…"; };
}

export function ResearchPage() {
  const route = useRoute();
  return route.parts[1] ? <SearchPage key={route.parts[1]} id={route.parts[1]} /> : <ResearchHome />;
}

// =========================================================================== home: setup, job, list
function ResearchHome() {
  const route = useRoute();
  const jobId = route.query.get("job");
  const list = useApi<SearchBatch[]>(research.searchesUrl);
  return (
    <div className="page" data-testid="research-page">
      <header className="page-head"><div><h1>Experiments</h1></div>
        <Badge tone="info">batch search</Badge></header>
      <Banner tone="info" testId="research-in-sample">Searches run stored strategies on datasets (one strategy on one dataset per
        cell; datasets are never merged). Results are <b>{IN_SAMPLE}</b>. The top of a ranking is not a valid strategy: out-of-sample,
        controls and the protocol holdout come after a shortlist.</Banner>
      <SearchSetup />
      {jobId && <JobPanel key={jobId} jobId={jobId} onFinished={list.reload} />}
      <Card title="Searches" actions={<Button small onClick={list.reload}>Refresh</Button>}>
        {list.error ? <ErrorPanel error={list.error} /> : !list.data ? <Loading label="Loading searches…" />
          : <SearchList rows={list.data} />}
      </Card>
    </div>
  );
}

function toggle(list: string[], id: string, on: boolean): string[] {
  return on ? [...list.filter((x) => x !== id), id] : list.filter((x) => x !== id);
}

function SearchSetup() {
  const strategies = useApi<LibraryRow[]>("/api/strategies");
  const batches = useApi<BatchRow[]>("/api/variation-batches");
  const families = useApi<Record<string, number>>("/api/families");
  const datasets = useApi<DatasetRow[]>("/api/datasets");
  const [ids, setIds] = useState<string[]>([]);
  const [vbs, setVbs] = useState<string[]>([]);
  const [fams, setFams] = useState<string[]>([]);
  const [pbs, setPbs] = useState("");
  const [dsIds, setDsIds] = useState<string[]>([]);
  const protocols = useApi<ProtocolRecordRow[]>("/api/protocols");
  const activeProtocol = (protocols.data ?? []).find((p) => p.status === "ACTIVE") ?? null;
  const [periodMode, setPeriodMode] = useState<"none" | "common" | "explicit" | "protocol">("none");
  useEffect(() => { if (activeProtocol) setPeriodMode("protocol"); }, [activeProtocol?.protocol_id]);   // ADR-70 default
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [metric, setMetric] = useState<RankingMetric>("expectancy_r");
  const [minSample, setMinSample] = useState<SampleLabel>("MODERATE SAMPLE");
  const [maxCells, setMaxCells] = useState<number | undefined>(1000);
  const [seed, setSeed] = useState<number | undefined>(undefined);
  const [busy, setBusy] = useState<"" | "validate" | "plan" | "start">("");
  const [validation, setValidation] = useState<SearchValidation | null>(null);
  const [plan, setPlan] = useState<SearchPlan | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  const spec = (): SearchSpec => {
    const pb = pbs.split(/[\s,]+/).filter(Boolean);
    const s: SearchSpec = {
      strategies: { ...(ids.length ? { ids } : {}), ...(vbs.length ? { variation_batches: vbs } : {}),
        ...(pb.length ? { proposal_batches: pb } : {}), ...(fams.length ? { families: fams } : {}) },
      datasets: dsIds, ranking: { metric, min_sample_label: minSample },
    };
    if (maxCells !== undefined) s.max_cells = maxCells;
    if (seed !== undefined) s.seed = seed;
    if (periodMode === "common") s.period = "common";
    if (periodMode === "explicit") s.period = { start, end };
    if (periodMode === "protocol" && activeProtocol?.discovery_period) s.period = activeProtocol.discovery_period;
    return s;
  };
  const run = async (kind: "validate" | "plan" | "start") => {
    setBusy(kind); setError(null);
    if (kind !== "validate") setPlan(null);
    try {
      if (kind === "validate") setValidation(await research.validate(spec()));
      else if (kind === "plan") setPlan(await research.plan(spec()));
      else { const job = await research.startJob(spec()); go(`/research?job=${job.job_id}`); }
    } catch (e) { setError(e as ApiError); } finally { setBusy(""); }
  };

  const pick = (rows: { id: string; label: string; extra?: string }[], chosen: string[], set: (v: string[]) => void, testId: string) =>
    !rows.length ? <Empty>None stored.</Empty> : (
      <div className="checks" data-testid={testId}>{rows.map((r) => (
        <Checkbox key={r.id} checked={chosen.includes(r.id)} onChange={(on) => set(toggle(chosen, r.id, on))}
          testId={`${testId}-${r.id}`} label={<><span title={r.id}>{r.label}</span>{r.extra && <span className="muted small"> {r.extra}</span>}</>} />))}
      </div>);
  const nameOf = (sid: string) => { const row = strategies.data?.find((x) => x.strategy_id === sid); return row ? strategyLabel(row.name) : "Strategy not in the library"; };
  const loadErr = strategies.error ?? batches.error ?? families.error ?? datasets.error;
  return (
    <Card title="Search setup" testId="search-setup">
      {loadErr && <ErrorPanel error={loadErr} />}
      <div className="grid2">
        <Field label="Strategies" wide>{strategies.data ? pick(strategies.data.map((s) => ({ id: s.strategy_id, label: strategyLabel(s.name), extra: s.timeframe ? facetLabel("timeframe", s.timeframe) : "" })),
          ids, setIds, "rs-ids") : <Loading label="Loading strategies…" />}</Field>
        <Field label="Datasets" hint="Ineligible datasets stay in the plan with their reasons (e.g. unconfigured broker costs).">
          {datasets.data ? pick(datasets.data.map((d) => ({ id: d.dataset_id, label: datasetLabel(d.dataset_id),
            extra: `${humanize(d.asset_type)}${d.runnable ? "" : " · " + plainText(d.reasons.join("; "))}` })), dsIds, setDsIds, "rs-datasets")
            : <Loading label="Loading datasets…" />}</Field>
        <Field label="Variation batches">{batches.data ? pick(batches.data.map((b) => ({ id: b.batch_id, label: strategyLabel(b.base_name), extra: `${b.generated} variants · ${shortTime(b.created_at)}` })),
          vbs, setVbs, "rs-vbs") : <Loading label="Loading batches…" />}</Field>
        <Field label="Families">{families.data ? pick(Object.entries(families.data).map(([f, n]) => ({ id: f, label: familyLabel(f), extra: `${n} instance(s)` })),
          fams, setFams, "rs-families") : <Loading label="Loading families…" />}</Field>
        <Field label="Proposal batches" hint="Proposal batch ids, comma separated (AI proposal batches that were saved through the ingestion gate)">
          <TextInput value={pbs} onChange={setPbs} mono testId="rs-pbs" placeholder="PB_…" /></Field>
        <Field label="Period" hint="Explicit times need an offset, e.g. 2024-01-02T00:00:00Z (timezones are never guessed).">
          <Select value={periodMode} onChange={setPeriodMode} testId="rs-period"
            options={[...(activeProtocol ? [{ value: "protocol" as const, label: `Protocol discovery window (${activeProtocol.discovery_trading_dates?.join(" → ")})` }] : []),
              { value: "none", label: "Full datasets" }, { value: "common", label: "Period common to all datasets" },
              { value: "explicit", label: "Explicit start / end" }]} />
          {periodMode === "explicit" && <div className="inline">
            <TextInput value={start} onChange={setStart} mono ariaLabel="period start" testId="rs-start" placeholder="Start" />
            <TextInput value={end} onChange={setEnd} mono ariaLabel="period end" testId="rs-end" placeholder="End" /></div>}
        </Field>
        <Field label="Ranking metric"><Select value={metric} onChange={setMetric} options={METRIC_OPTIONS} testId="rs-metric" /></Field>
        <Field label="Minimum sample size"><Select value={minSample} onChange={setMinSample} options={SAMPLE_OPTIONS} testId="rs-min-sample" /></Field>
        <Field label="Max eligible cells" hint="A larger search is refused, never truncated.">
          <NumberInput value={maxCells} onChange={setMaxCells} integer testId="rs-max-cells" /></Field>
        <Field label="Seed" hint="Optional; part of the search identity."><NumberInput value={seed} onChange={setSeed} integer testId="rs-seed" /></Field>
      </div>
      <TechDetails summary="Technical details: search spec (exactly what is sent)">
        <pre className="code" data-testid="rs-spec-json">{JSON.stringify(spec(), null, 2)}</pre></TechDetails>
      <div className="actions">
        <Button onClick={() => run("validate")} busy={busy === "validate"} testId="rs-validate">Check spec</Button>
        <Button onClick={() => run("plan")} busy={busy === "plan"} testId="rs-plan">Preview plan</Button>
        <Button kind="primary" onClick={() => run("start")} busy={busy === "start"} testId="rs-start-job">Start search</Button>
      </div>
      <ErrorPanel error={error} testId="rs-error" />
      {validation && <div data-testid="rs-validation">
        {validation.valid
          ? <><Banner tone="ok">Search spec is well formed.</Banner>
            {validation.search_hash && <TechDetails rows={[["Search hash", <Mono>{validation.search_hash}</Mono>]]} />}</>
          : <Banner tone="error">Search spec has {validation.errors.length} problem(s)</Banner>}
        <IssueList issues={[...validation.errors, ...validation.warnings]} testId="rs-validation-issues" />
      </div>}
      {plan && <PlanView plan={plan} nameOf={nameOf} />}
    </Card>
  );
}

function PlanView({ plan, nameOf }: { plan: SearchPlan; nameOf: Names }) {
  const c = plan.counts;
  return (
    <div data-testid="rs-plan-result">
      <h3>Plan <span className="muted small">(nothing executed)</span></h3>
      <KeyValues rows={[["Cells", `${c.planned} planned · ${c.eligible} eligible · ${c.ineligible} ineligible`],
        ["Strategies", `${c.strategies} (${c.duplicate_references_collapsed} duplicate reference(s) collapsed, ${c.excluded_archived} archived excluded)`],
        ["Datasets", fmt(c.datasets)], ["Period", plan.period ? `${humanize(plan.period.mode)}: ${plan.period.start} → ${plan.period.end}` : "full datasets"]]} />
      <TechDetails rows={[["Search id", <Mono>{plan.search_id}</Mono>], ["Search hash", <Mono>{plan.search_hash}</Mono>], ["Plan hash", <Mono>{plan.plan_hash}</Mono>],
        ["Config hash", <Mono>{plan.config_hash}</Mono>]]} />
      {plan.warnings.map((w, i) => <Banner key={i} tone="warn">{plainText(w)}</Banner>)}
      <TableWrap testId="rs-plan-cells"><table>
        <thead><tr><th>#</th><th>Strategy</th><th>Dataset</th><th>Eligible</th><th>Reasons</th></tr></thead>
        <tbody>{plan.cells.map((x) => (
          <tr key={x.cell_id}><td>{x.plan_index}</td><td title={x.strategy_id}>{nameOf(x.strategy_id)}</td><td title={x.dataset_id}>{datasetLabel(x.dataset_id)}</td>
            <td>{x.eligible ? <Badge tone="ok">eligible</Badge> : <Badge tone="warn">ineligible</Badge>}</td>
            <td className="small">{plainText(x.reasons.join("; ")) || "—"}</td></tr>))}
        </tbody>
      </table></TableWrap>
    </div>
  );
}

function JobPanel({ jobId, onFinished }: { jobId: string; onFinished: () => void }) {
  const [job, setJob] = useState<JobStatus | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [cancelling, setCancelling] = useState(false);
  useEffect(() => {                                   // ordinary polling; stops at a final state or an error
    let live = true, timer = 0;
    const poll = () => research.job(jobId).then((j) => {
      if (!live) return;
      setJob(j); setError(null);
      if (JOB_FINAL.has(j.state)) onFinished(); else timer = window.setTimeout(poll, POLL_MS);
    }).catch((e: ApiError) => { if (live) setError(e); });
    poll();
    return () => { live = false; window.clearTimeout(timer); };
  }, [jobId]); // eslint-disable-line react-hooks/exhaustive-deps
  const cancel = async () => {
    setCancelling(true);
    try { setJob(await research.cancelJob(jobId)); } catch (e) { setError(e as ApiError); } finally { setCancelling(false); }
  };
  const p = job?.progress ?? { stored: false };
  const active = job && !JOB_FINAL.has(job.state);
  return (
    <Card title="Search job" testId="rs-job"
      actions={active && !job?.cancel_requested
        ? <Button kind="danger" small onClick={cancel} busy={cancelling} testId="rs-cancel">Cancel search</Button> : null}>
      <ErrorPanel error={error} title={error?.status === 404 ? "This job is not known to the running server (job ids live in the server process)" : undefined}
        testId="rs-job-error" />
      {!job ? (!error && <Loading label="Loading job…" />) : <>
        <KeyValues rows={[["State", <span data-testid="rs-job-state"><StatusBadge s={job.state} /></span>],
          ["Search", <a href={href(`/research/${job.search_id}`)} data-testid="rs-job-search" title={job.search_id}>View this search</a>],
          ["Started", shortTime(job.started_at)], ["Finished", shortTime(job.finished_at)]]} />
        {job.cancel_requested && active && <Banner tone="warn" testId="rs-cancel-requested">Cancellation requested: the running cell
          finishes, no new cell starts. Completed cells stay stored; the rest become cancelled.</Banner>}
        <TechDetails rows={[["Job id", <Mono>{jobId}</Mono>], ["Search id", <Mono>{job.search_id}</Mono>]]} />
        {job.error && <Banner tone="error" testId="rs-job-failure">{job.error}</Banner>}
        {p.stored && <div data-testid="rs-progress">
          <progress max={1} value={p.fraction_done ?? 0} aria-label="search progress" />
          <KeyValues rows={[["Cells", `${fmt(p.planned)} planned · ${fmt(p.eligible)} eligible · ${fmt(p.ineligible)} ineligible`],
            ["Evaluated", fmt(p.evaluated)], ["Pending", fmt(p.pending)], ["Failed", fmt(p.failed)],
            ["Skipped (already completed)", fmt(p.skipped_resume)], ["Cancelled", fmt(p.cancelled)], ["Trials", fmt(p.trials)],
            ["Batch status", <StatusBadge s={p.batch_status} />]]} />
        </div>}
        {!active && <p><a href={href(`/research/${job.search_id}`)} data-testid="rs-open-results">Open search results →</a>
          {" · "}<a href={href(`/compare?source=search&id=${job.search_id}`)} data-testid="rs-open-compare">Compare these runs →</a></p>}
      </>}
    </Card>
  );
}

function SearchList({ rows }: { rows: SearchBatch[] }) {
  if (!rows.length) return <Empty>No searches yet.</Empty>;
  return (
    <TableWrap testId="rs-searches"><table>
      <thead><tr><th>Search (created)</th><th>Status</th><th>Finished</th><th>Planned</th><th>Eligible</th><th>Evaluated</th>
        <th>Failed</th><th>Cancelled</th><th>Trials</th><th>Shortlist</th><th>Protocol</th></tr></thead>
      <tbody>{[...rows].reverse().map((r) => (
        <tr key={r.search_id}><td><a href={href(`/research/${r.search_id}`)} title={r.search_id}>{shortTime(r.created_at)}</a></td>
          <td><StatusBadge s={r.status} /></td><td className="small">{shortTime(r.finished_at)}</td><td>{r.n_planned}</td>
          <td>{r.n_eligible}</td><td>{r.n_evaluated}</td><td>{r.n_failed}</td><td>{r.n_cancelled}</td><td>{r.n_trials}</td>
          <td>{r.shortlist?.strategy_ids.length ?? 0}</td>
          <td className="small">{r.protocol_id ? <span title={r.protocol_id}>attributed</span> : <span className="muted">none</span>}</td></tr>))}
      </tbody>
    </table></TableWrap>
  );
}

// =========================================================================== one search
function SearchPage({ id }: { id: string }) {
  const detail = useApi<SearchDetail>(research.searchUrl(id), [id]);
  const nameOf = useStrategyNames();
  if (detail.error) return <div className="page"><ErrorPanel error={detail.error} title="Could not load search results" testId="rs-search-error" />
    <p><a href={href("/research?setup=1")}>Back to research</a></p></div>;
  const d = detail.data;
  if (!d) return <Loading label="Loading search…" />;
  return (
    <div className="page" data-testid="rs-search-page">
      <header className="page-head"><div><h1>Search of {shortTime(d.created_at)}</h1>
        <div className="head-meta"><StatusBadge s={d.status} /></div></div>
        <div className="actions"><a href={href(`/compare?source=search&id=${d.search_id}`)} data-testid="rs-compare">Compare runs</a>
          {" · "}<a href={href("/research?setup=1")}>All searches</a></div></header>
      <Banner tone="info" testId="rs-search-in-sample"><b>{IN_SAMPLE}.</b> {plainText(d.note)}</Banner>
      {d.protocol_id ? <ProtocolBudget pid={d.protocol_id} search={d} />
        : <Banner tone="warn">This search is not attributed to a research protocol (it ran without an active protocol): its trials are counted
          only per search.</Banner>}
      <Card title="Accounting">
        <KeyValues rows={[["Last invocation", `${d.n_planned} planned · ${d.n_eligible} eligible · ${d.n_ineligible} ineligible · `
          + `${d.n_evaluated} evaluated · ${d.n_skipped_resume} skipped (already completed) · ${d.n_failed} failed · ${d.n_cancelled} cancelled`],
          ["All invocations (current plan)", Object.entries(d.cumulative).map(([k, v]) => `${humanize(k).toLowerCase()} ${v}`).join(" · ")],
          ["Trials (distinct cells evaluated)", <b data-testid="rs-trials">{d.cumulative.trials}</b>]]} />
        {d.warnings.map((w, i) => <Banner key={i} tone="warn">{plainText(w)}</Banner>)}
        <TechDetails rows={[["Search id", <Mono>{d.search_id}</Mono>], ["Config hash", <Mono>{d.config_hash}</Mono>], ["Search hash", <Mono>{d.search_hash}</Mono>],
          ["Protocol id", d.protocol_id ? <Mono>{d.protocol_id}</Mono> : null]]}>
          <h4>Search spec</h4><pre className="code">{JSON.stringify(d.spec, null, 2)}</pre></TechDetails>
      </Card>
      <Card title={`Current cells (${d.cells.length})`}><CellTable cells={d.cells} testId="rs-cells" nameOf={nameOf} /></Card>
      <Card title={`Historical cells (${d.historical_cells.length})`} testId="rs-historical">
        <p className="muted small">Cells from an earlier plan of this search (e.g. a family whose membership has changed). Kept for the
          research record; not counted in the current accounting or trials, and never ranked.</p>
        {d.historical_cells.length ? <CellTable cells={d.historical_cells} testId="rs-historical-cells" nameOf={nameOf} /> : <Empty>None.</Empty>}
      </Card>
      <RankingCard search={d} nameOf={nameOf} />
      <ShortlistCard search={d} onSaved={detail.reload} nameOf={nameOf} />
    </div>
  );
}

function ProtocolBudget({ pid, search }: { pid: string; search: SearchDetail }) {
  const { data, error } = useApi<{ status: ProtocolStatus }>(`/api/protocols/${pid}`, [pid]);
  if (error) return <ErrorPanel error={error} title="Research protocol" />;
  if (!data) return <Loading label="Loading protocol budget…" />;
  const t = data.status.trials, h = data.status.holdout;
  return (
    <Card title={<>Research protocol <Badge tone={data.status.status === "ACTIVE" ? "ok" : "neutral"}>{statusLabel(data.status.status)}</Badge></>} testId="rs-protocol">
      <div className="kpis">
        <Kpi label="This search: trials" value={String(search.cumulative.trials ?? search.n_trials)} sub={`${search.n_failed} failed cells`} />
        <Kpi label="Program: unique trials" value={`${t.unique_numerical_trials} / ${t.budget}`} sub={`${t.remaining} remaining · ${t.duplicate_events} duplicate events`}
          meter={t.unique_numerical_trials / Math.max(1, t.budget)} />
        <Kpi label="Failed evaluations" value={String(t.failed_events)} sub="recorded, not counted" />
        <Kpi label="Holdout looks" value={`${h.looks_used} / ${h.budget}`} sub="one per shortlisted candidate" meter={h.looks_used / Math.max(1, h.budget)} />
      </div>
      <p className="small muted">A shortlist is a tag carrying this protocol id; it is never acceptance. Holdout access is only through the backend gate.</p>
      <TechDetails rows={[["Protocol id", <Mono>{pid}</Mono>]]} />
    </Card>
  );
}

function CellTable({ cells, testId, nameOf }: { cells: SearchCell[]; testId: string; nameOf: Names }) {
  if (!cells.length) return <Empty>No cells.</Empty>;
  return (
    <TableWrap testId={testId}><table>
      <thead><tr><th>#</th><th>Strategy</th><th>Dataset</th><th>Status</th><th>Run</th><th>Trades</th><th>Sample size</th>
        <th>Net R per trade</th><th>Profit factor</th><th>Net R</th><th>Reasons / error</th></tr></thead>
      <tbody>{cells.map((c) => {
        const h = c.headline ?? {};
        return (
          <tr key={c.cell_id} data-status={c.status}><td>{c.plan_index}</td>
            <td><a href={href(`/strategies/${c.strategy_id}`)} title={c.strategy_id}>{nameOf(c.strategy_id)}</a></td><td title={c.dataset_id}>{datasetLabel(c.dataset_id)}</td>
            <td><StatusBadge s={c.status} /></td>
            <td>{c.run_id ? <a href={href(`/results/${c.run_id}`)} title={c.run_id}>open run</a> : "—"}</td>
            <td>{fmt(h.trade_count)}</td><td className="small">{h.sample_label ? humanize(h.sample_label) : "—"}</td><td>{fmt(h.expectancy_r)}</td>
            <td>{fmt(h.profit_factor)}</td><td>{fmt(h.net_r)}</td>
            <td className="small">{c.error ?? (plainText((c.reasons ?? []).join("; ")) || "—")}</td></tr>);
      })}</tbody>
    </table>
    <TechDetails>{cells.map((c) => <div key={c.cell_id} className="small">#{c.plan_index}: <Mono>{c.strategy_id}</Mono> · <Mono>{c.dataset_id}</Mono>
      {c.run_id && <> · <Mono>{c.run_id}</Mono></>}</div>)}</TechDetails></TableWrap>
  );
}

function RankingCard({ search, nameOf }: { search: SearchDetail; nameOf: Names }) {
  const [metric, setMetric] = useState<RankingMetric>(search.spec.ranking?.metric ?? "expectancy_r");
  const [minSample, setMinSample] = useState<SampleLabel>(search.spec.ranking?.min_sample_label ?? "MODERATE SAMPLE");
  const [ranking, setRanking] = useState<Ranking | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  useEffect(() => {
    let live = true;
    setError(null);
    research.ranking(search.search_id, metric, minSample).then((r) => { if (live) setRanking(r); })
      .catch((e: ApiError) => { if (live) { setRanking(null); setError(e); } });
    return () => { live = false; };
  }, [search.search_id, metric, minSample]);
  return (
    <Card title="Ranking (in-sample)" testId="rs-ranking">
      <div className="inline">
        <Field label="Metric"><Select value={metric} onChange={setMetric} options={METRIC_OPTIONS} testId="rk-metric" /></Field>
        <Field label="Minimum sample size"><Select value={minSample} onChange={setMinSample} options={SAMPLE_OPTIONS} testId="rk-min-sample" /></Field>
      </div>
      <ErrorPanel error={error} testId="rk-error" />
      {ranking && <>
        <Banner tone="warn" testId="rk-label"><b>{ranking.label}</b></Banner>
        <p className="muted small" data-testid="rk-meta">Ranked by {metricLabel(ranking.metric).toLowerCase()} ({ranking.direction}) · minimum sample size {humanize(ranking.min_sample_label).toLowerCase()} ·
          {" "}{ranking.n_trials} trial(s) · status {statusLabel(ranking.status).toLowerCase()} · excluded: {Object.entries(ranking.excluded).filter(([, n]) => n)
            .map(([k, n]) => `${humanize(k).toLowerCase()} ${n}`).join(", ") || "none"}</p>
        {ranking.ranked.length ? <TableWrap testId="rk-table"><table>
          <thead><tr><th>Rank</th><th>Strategy</th><th>Dataset</th><th>Ranked by: {metricLabel(ranking.metric)}</th><th>Trades</th><th>Sample size</th>
            <th>Net R per trade</th><th>Profit factor</th><th>Net R</th><th>Max drawdown (R)</th><th>Run</th></tr></thead>
          <tbody>{ranking.ranked.map((r) => (
            <tr key={r.cell_id}><td>{r.rank}</td><td title={r.strategy_id}>{nameOf(r.strategy_id)}</td><td title={r.dataset_id}>{datasetLabel(r.dataset_id)}</td>
              <td>{r.value_infinite ? "+∞ (no losing trade)" : fmt(r.value)}</td><td>{fmt(r.metrics.trade_count)}</td>
              <td className="small">{r.metrics.sample_label ? humanize(r.metrics.sample_label) : "—"}</td><td>{fmt(r.metrics.expectancy_r)}</td>
              <td>{r.value_infinite && ranking.metric === "profit_factor" ? "+∞" : fmt(r.metrics.profit_factor)}</td>
              <td>{fmt(r.metrics.net_r)}</td><td>{fmt(r.metrics.max_drawdown_r)}</td>
              <td>{r.run_id ? <a href={href(`/results/${r.run_id}`)} title={r.run_id}>open run</a> : "—"}</td></tr>))}
          </tbody>
        </table></TableWrap> : <Empty>No cell meets the ranking filters (see the excluded counts above).</Empty>}
        <p className="muted small" data-testid="rk-note">{plainText(ranking.note)}</p>
      </>}
    </Card>
  );
}

function ShortlistCard({ search, onSaved, nameOf }: { search: SearchDetail; onSaved: () => void; nameOf: Names }) {
  const { toast } = useApp();
  const ids = [...new Set(search.cells.map((c) => c.strategy_id))];
  const [chosen, setChosen] = useState<string[]>(search.shortlist?.strategy_ids ?? []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const save = async () => {
    setBusy(true); setError(null);
    try {
      const r = await research.shortlist(search.search_id, chosen);
      toast("ok", `Shortlist saved (${r.strategy_ids.length} strateg${r.strategy_ids.length === 1 ? "y" : "ies"}) — a research tag only`);
      onSaved();
    } catch (e) { setError(e as ApiError); } finally { setBusy(false); }
  };
  return (
    <Card title="Shortlist" testId="rs-shortlist">
      <p className="muted small">A shortlist is a research tag on this search: it changes no run status and implies no validation.
        Candidates still need the Phase 6 out-of-sample and walk-forward checks.</p>
      <div className="checks">{ids.map((sid) => (
        <Checkbox key={sid} checked={chosen.includes(sid)} onChange={(on) => setChosen(toggle(chosen, sid, on))}
          testId={`sl-${sid}`} label={<span title={sid}>{nameOf(sid)}</span>} />))}
      </div>
      <div className="actions"><Button kind="primary" onClick={save} busy={busy} testId="sl-save">Save shortlist</Button></div>
      <ErrorPanel error={error} testId="sl-error" />
      <div data-testid="sl-current">{search.shortlist
        ? <>Saved shortlist ({shortTime(search.shortlist.selected_at)}): {search.shortlist.strategy_ids.length
          ? search.shortlist.strategy_ids.map((s) => nameOf(s)).join(", ") : <span className="muted">empty</span>}
          <div className="muted small">{plainText(search.shortlist.note)}</div>
          {search.shortlist.strategy_ids.length > 0 && <TechDetails rows={[["Strategy ids", <Mono>{search.shortlist.strategy_ids.join(", ")}</Mono>]]} />}</>
        : <span className="muted">No shortlist saved.</span>}</div>
    </Card>
  );
}
