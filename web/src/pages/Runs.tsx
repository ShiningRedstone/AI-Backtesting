import { useEffect, useMemo, useRef, useState } from "react";
import type { ApiError } from "../api/client";
import { CAMPAIGN_JOB_FINAL, LIVE_POLL_MS, campaigns, fmtDuration } from "../api/campaigns";
import type { CampaignDetail, CampaignJob, CampaignListRow, CampaignRunRecord, CampaignTree, CheckReport, Eta,
  FamilyResults, ScopeProgress, StrategyResult, TreeFamily } from "../api/campaigns";
import { useApi, useApp } from "../app/context";
import { href, useRoute } from "../app/router";
import { Badge, Banner, Button, Card, Empty, ErrorPanel, KeyValues, Kpi, Loading, Mono, TableWrap, fmt, n, r,
  shortTime } from "../components/ui";

/** Research Runs (ADR-69/70): a research browser (All strategies -> families -> strategies) over a FROZEN campaign.
 *  The page is a control and presentation layer: it starts the backend's own campaign runner (the same one as the CLI
 *  `research campaign-run`) as a background job and shows its persisted records. Selection is a run SCOPE made of the
 *  manifest's own frozen strategy ids; families and strategies appear in catalog / manifest order, never ranked. */
export function RunsPage() {
  const route = useRoute();
  const [cid, fid, sid] = route.parts.slice(1);
  if (cid && fid && sid) return <StrategyResultPage cid={cid} sid={sid} fid={fid} />;
  if (cid && fid) return <FamilyResultsPage cid={cid} fid={fid} />;
  if (cid) return <CampaignPage cid={cid} />;
  return <CampaignList />;
}

const pctOf = (done: number, total: number) => (total ? `${((100 * done) / total).toFixed(1)}%` : "—");
const elapsedS = (from: string | null | undefined, to?: string | null) =>
  from ? Math.max(0, ((to ? Date.parse(to) : Date.now()) - Date.parse(from)) / 1000) : null;
const statusTone = (s: string): "ok" | "warn" | "error" | "info" | "neutral" =>
  s === "completed" ? "ok" : s === "failed" || s === "stopped_on_failure" ? "error"
    : s === "running" || s === "preflight" ? "info" : s === "cancelled" || s === "interrupted" || s === "incomplete" ? "warn" : "neutral";

function Governance({ c }: { c: CampaignDetail | CampaignListRow }) {
  return (
    <KeyValues rows={[
      ["Campaign", <Mono>{c.campaign_id}</Mono>],
      ["Research protocol", <><Mono>{c.protocol.protocol_id}</Mono> v{c.protocol.protocol_version} · budget {fmt(c.protocol.max_unique_trials)} unique trials ·
        Bonferroni family = {c.protocol.family_size_rule === "declared_max_unique_trials" ? "declared budget" : c.protocol.family_size_rule}</>],
      ["Discovery window", <>{c.discovery.trading_dates.join(" → ")} <span className="muted small">(every run evaluates only these bars)</span></>],
      ["Holdout", <><Badge tone="warn">locked</Badge> <span className="small muted">never requested, loaded or evaluated by research runs</span></>],
      ["Data", <>Nasdaq / canonical BID-ASK research data · dataset chosen automatically from each strategy's frozen timeframe:{" "}
        {Object.entries(c.datasets).map(([tf, d]) => <span key={tf} className="chip" title={d.content_hash}>{tf} <Mono>{d.dataset_id}</Mono></span>)}</>],
      ["Frozen search / manifest", <><Mono>{c.search_id}</Mono> · <Mono>{c.manifest.manifest_id}</Mono> · {fmt(c.manifest.n_strategies)} strategies · {c.manifest.factory_version}</>],
      ["Execution", <>{c.execution.execution_contract} whole contracts (max {c.execution.max_quantity}) · ${fmt(c.execution.account.starting_equity)} research account ·
        workers {c.execution.workers} · one trial per strategy · {c.stage} only</>],
      ["Prop audit", <>{c.prop_simulation.profiles.map((p) => <span key={p.profile_id} className="chip" title={p.profile_hash}>{p.profile_id} v{p.version}</span>)}
        <span className="small muted"> downstream of the base result; never changes it</span></>],
    ]} />
  );
}

function EtaText({ eta, preflight }: { eta: Eta | null | undefined; preflight?: number | null }) {
  if (!eta) return <span>Estimating…</span>;
  if (eta.remaining_strategies === 0) return <span data-testid="eta-done">nothing left to evaluate</span>;
  if (eta.state !== "estimate" || eta.remaining_seconds == null) {
    return <span data-testid="eta-estimating">Estimating… <span className="muted small">({eta.n_observations} of {eta.min_observations} timing observations needed)</span></span>;
  }
  return (
    <span data-testid="eta-estimate">≈ {fmtDuration(eta.remaining_seconds)}
      <span className="muted small"> estimate from {fmt(eta.n_observations)} observed durations (workers 1)
        {preflight != null && ` · preflight took ${fmtDuration(preflight)}`}</span></span>
  );
}

function CampaignList() {
  const list = useApi<CampaignListRow[]>(campaigns.listUrl);
  return (
    <div className="page" data-testid="runs-page">
      <header className="page-head"><div><div className="eyebrow">Research</div><h1>Research runs</h1>
        <p className="subtitle">Run frozen research campaigns from the desktop app. Runs continue in the background, are stored
          permanently in this workspace and can be resumed after the app is closed.</p></div></header>
      <ActiveJobBanner />
      {list.error && <ErrorPanel error={list.error} title="Could not load campaigns" />}
      {!list.data && !list.error && <Loading label="Loading campaigns…" />}
      {list.data && !list.data.length && <Empty>No frozen campaign in this workspace yet. Freeze one with
        <Mono> research campaign-freeze FM_…</Mono> (the manifest and protocol must exist first).</Empty>}
      {list.data?.map((c) => c.error
        ? <Card key={c.campaign_id} title={<Mono>{c.campaign_id}</Mono>}><Banner tone="error">{c.error.code}: {c.error.message}</Banner></Card>
        : (
          <Card key={c.campaign_id} testId={`campaign-${c.campaign_id}`}
            title={<><Mono>{c.campaign_id}</Mono> <Badge tone={c.progress.remaining === 0 ? "ok" : "info"}>
              {c.progress.remaining === 0 ? "complete" : `${pctOf(c.progress.completed, c.progress.strategies)} done`}</Badge></>}
            actions={<a className="btn btn-primary" href={href(`/runs/${c.campaign_id}`)} data-testid={`open-${c.campaign_id}`}>Open research browser</a>}>
            <div className="kpis">
              <Kpi label="Strategies" value={fmt(c.progress.strategies)} />
              <Kpi label="Completed" value={fmt(c.progress.completed)} meter={c.progress.fraction_done} />
              <Kpi label="Remaining" value={fmt(c.progress.remaining)} />
              <Kpi label="Failed (retried on resume)" value={fmt(c.progress.failed)} />
              <Kpi label="Runs" value={fmt(c.n_runs)} sub={c.latest_run ? `last: ${c.latest_run.status} · ${shortTime(c.latest_run.created_at)}` : "none yet"} />
            </div>
            <Governance c={c} />
          </Card>))}
    </div>
  );
}

function ActiveJobBanner() {
  const [job, setJob] = useState<CampaignJob | null>(null);
  useEffect(() => {
    let live = true;
    let t = 0;
    const poll = () => campaigns.active().then((d) => { if (live) setJob(d.job); }).catch(() => undefined)
      .finally(() => { if (live) t = window.setTimeout(poll, 3000); });
    poll();
    return () => { live = false; window.clearTimeout(t); };
  }, []);
  if (!job || job.kind !== "campaign" || CAMPAIGN_JOB_FINAL.has(job.state)) return null;
  return <Banner tone="info" testId="active-job">A research run is in progress: <Mono>{job.campaign_id}</Mono> — {job.live.phase}.{" "}
    <a href={href(`/runs/${job.campaign_id}?job=${job.job_id}`)}>Show progress ›</a></Banner>;
}

// ------------------------------------------------------------------------------------------ selection (frozen ids only)
const storeKey = (cid: string) => `edgelab.runs.selection.${cid}`;
function loadSelection(cid: string, all: string[]): Set<string> {
  try {
    const raw = window.localStorage.getItem(storeKey(cid));
    if (raw) { const known = new Set(all); return new Set((JSON.parse(raw) as string[]).filter((x) => known.has(x))); }
  } catch { /* per-viewer convenience only */ }
  return new Set(all);
}
function saveSelection(cid: string, sel: Set<string>) {
  try { window.localStorage.setItem(storeKey(cid), JSON.stringify([...sel])); } catch { /* ignore */ }
}

function TriCheckbox({ checked, indeterminate, onChange, label, testId }: {
  checked: boolean; indeterminate?: boolean; onChange: (v: boolean) => void; label: string; testId?: string;
}) {
  const ref = useRef<HTMLInputElement | null>(null);
  useEffect(() => { if (ref.current) ref.current.indeterminate = !!indeterminate && !checked; }, [indeterminate, checked]);
  return <input ref={ref} type="checkbox" checked={checked} aria-label={label} data-testid={testId}
    data-state={checked ? "checked" : indeterminate ? "mixed" : "unchecked"}
    onChange={(e: { target: HTMLInputElement }) => onChange(e.target.checked)} />;
}

function CampaignPage({ cid }: { cid: string }) {
  const route = useRoute();
  const { toast } = useApp();
  const det = useApi<CampaignDetail>(campaigns.detailUrl(cid));
  const treeQ = useApi<CampaignTree>(campaigns.treeUrl(cid));
  const [sel, setSel] = useState<Set<string> | null>(null);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [jobId, setJobId] = useState<string | null>(route.query.get("job"));
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [check, setCheck] = useState<CheckReport | null>(null);
  const [checking, setChecking] = useState(false);
  const d = det.data;
  const tree = treeQ.data;
  const allIds = useMemo(() => (tree ? tree.families.flatMap((f) => f.strategies.map((s) => s.strategy_id)) : []), [tree]);
  useEffect(() => { if (tree && sel === null) setSel(loadSelection(cid, allIds)); }, [tree]);
  useEffect(() => { if (sel) saveSelection(cid, sel); }, [sel]);
  useEffect(() => {                                    // re-attach to a run started earlier (or from another page)
    if (jobId) return;
    campaigns.active().then((a) => { if (a.job && a.job.campaign_id === cid && !CAMPAIGN_JOB_FINAL.has(a.job.state)) setJobId(a.job.job_id); })
      .catch(() => undefined);
  }, [cid]);
  const reloadAll = () => { det.reload(); treeQ.reload(); };
  const scope = useMemo(() => {
    if (!tree || !sel) return { strategies: 0, completed: 0, remaining: 0, families: 0 };
    let completed = 0, families = 0;
    for (const f of tree.families) {
      const mine = f.strategies.filter((s) => sel.has(s.strategy_id));
      if (mine.length) families += 1;
      completed += mine.filter((s) => s.status === "completed").length;
    }
    return { strategies: sel.size, completed, remaining: sel.size - completed, families };
  }, [tree, sel]);
  if (det.error) return <div className="page"><ErrorPanel error={det.error} title={`Could not load campaign ${cid}`} /></div>;
  if (treeQ.error) return <div className="page"><ErrorPanel error={treeQ.error} title={`Could not load the research tree of ${cid}`} /></div>;
  if (!d || !tree || !sel) return <div className="page"><Loading label="Loading the frozen campaign and its research tree…" /></div>;
  const allSelected = sel.size === allIds.length;
  const setMany = (ids: string[], on: boolean) => {
    const s = new Set(sel);
    for (const id of ids) { if (on) s.add(id); else s.delete(id); }
    setSel(s);
  };
  const start = async () => {
    setStarting(true); setErr(null);
    try {
      const j = await campaigns.start(cid, allSelected ? null : [...sel]);
      setJobId(j.job_id);
      toast("info", `Research run started — ${fmt(sel.size)} strategies selected; preflight first.`);
    } catch (e) { setErr(e as ApiError); } finally { setStarting(false); }
  };
  const runCheck = async () => {
    setChecking(true); setErr(null);
    try { setCheck(await campaigns.check(cid)); } catch (e) { setErr(e as ApiError); } finally { setChecking(false); }
  };
  const latest = d.latest_run;
  return (
    <div className="page" data-testid="campaign-page">
      <header className="page-head"><div><div className="eyebrow"><a href={href("/runs")}>Research runs</a></div>
        <h1>Research browser <Mono>{d.campaign_id}</Mono></h1>
        <p className="subtitle">{d.data_line}</p></div></header>
      {jobId && <LiveRun jobId={jobId} onFinished={reloadAll} onDismiss={() => setJobId(null)} />}
      {/* ---------------- All strategies view */}
      <Card title={<>All strategies ({fmt(tree.n_strategies)})</>} testId="all-strategies"
        actions={<>
          <Button small onClick={() => setMany(allIds, true)} testId="select-all">Select all</Button>
          <Button small onClick={() => setSel(new Set())} testId="clear-all">Clear all</Button>
        </>}>
        <div className="kpis" data-testid="scope-summary">
          <Kpi label="Total" value={fmt(tree.n_strategies)} />
          <Kpi label="Selected" value={fmt(sel.size)} sub={`${scope.families} of ${tree.families.length} families`} accent />
          <Kpi label="Completed (campaign)" value={fmt(d.progress.completed)} meter={d.progress.fraction_done} sub={pctOf(d.progress.completed, d.progress.strategies)} />
          <Kpi label="Remaining (campaign)" value={fmt(d.progress.remaining)} />
          <Kpi label="Run status" value={<Badge tone={statusTone(latest?.status ?? "none")}>{latest?.status ?? "no run yet"}</Badge>}
            sub={latest ? shortTime(latest.updated_at ?? latest.created_at) : undefined} />
          <Kpi label="Estimated remaining (campaign)" value={<EtaText eta={d.eta} preflight={latest?.preflight_seconds} />} />
        </div>
        <div className="actions">
          <Button kind="primary" onClick={start} busy={starting} busyLabel="Starting…" testId="run-research"
            disabled={!sel.size || !!jobId || scope.remaining === 0}
            title={jobId ? "A run is in progress" : scope.remaining === 0 ? "Every selected strategy is already evaluated" : undefined}>
            Run selected ({fmt(scope.remaining)} to evaluate{scope.completed ? `, ${fmt(scope.completed)} already done` : ""})
          </Button>
          <Button onClick={runCheck} busy={checking} busyLabel="Checking (read-only)…" testId="run-check">Preflight check (read-only)</Button>
        </div>
        {err && <ErrorPanel error={err} title="The run could not start" />}
        {check && <Banner tone={check.ready ? "ok" : "error"} testId="check-result">
          {check.ready ? "READY" : "NOT READY"} — {check.note}
          {!check.ready && <ul>{check.checks.filter((x) => !x.ok).map((x) => <li key={x.check}>{x.check}: <Mono>{JSON.stringify(x.detail)}</Mono></li>)}</ul>}
        </Banner>}
        <p className="small muted">{tree.note}. Each selected strategy is evaluated once, on the dataset of its own frozen timeframe,
          inside the campaign's frozen search; strategies already evaluated are skipped (never re-run, never a second trial).</p>
        {/* ---------------- family tree */}
        <div className="tree" data-testid="family-tree">
          {tree.families.map((f) => (
            <FamilyNode key={f.family_id} cid={cid} f={f} sel={sel} open={open.has(f.family_id)}
              onToggleOpen={() => { const o = new Set(open); if (o.has(f.family_id)) o.delete(f.family_id); else o.add(f.family_id); setOpen(o); }}
              onSelectFamily={(on) => setMany(f.strategies.map((s) => s.strategy_id), on)}
              onSelectStrategy={(id, on) => setMany([id], on)} />))}
        </div>
      </Card>
      <Card title="Governance — fixed research design (not editable here)" testId="campaign-governance"><Governance c={d} /></Card>
      <RunHistory cid={cid} runs={d.runs} onRestoreScope={(ids) => setSel(new Set(ids.filter((x) => allIds.includes(x))))} />
    </div>
  );
}

function FamilyNode({ cid, f, sel, open, onToggleOpen, onSelectFamily, onSelectStrategy }: {
  cid: string; f: TreeFamily; sel: Set<string>; open: boolean; onToggleOpen: () => void;
  onSelectFamily: (on: boolean) => void; onSelectStrategy: (id: string, on: boolean) => void;
}) {
  const nSel = f.strategies.filter((s) => sel.has(s.strategy_id)).length;
  const all = nSel === f.strategies.length && nSel > 0;
  return (
    <div className="tree-node" data-testid={`family-${f.family_id}`} data-selected={nSel} data-total={f.n_strategies}>
      <div className="tree-name">
        <button type="button" className="linklike" onClick={onToggleOpen} aria-expanded={open} data-testid={`toggle-${f.family_id}`}>{open ? "▾" : "▸"}</button>
        <TriCheckbox checked={all} indeterminate={nSel > 0 && !all} onChange={onSelectFamily} label={`select family ${f.name}`} testId={`fam-${f.family_id}`} />
        <b>{f.name}</b> <span className="muted">({fmt(f.n_strategies)})</span>
        <span className="small muted"> · selected {fmt(nSel)} / {fmt(f.n_strategies)} · completed {fmt(f.completed)}{f.failed ? ` · failed ${fmt(f.failed)}` : ""}</span>
        <span className="small muted"> · {f.group ?? ""}</span>
        <a className="small" href={href(`/runs/${cid}/${f.family_id}`)}> details ›</a>
      </div>
      {open && (
        <ul className="tree-children small" data-testid={`children-${f.family_id}`}>
          {f.hypothesis && <li className="muted">{f.hypothesis}</li>}
          {f.strategies.map((s, i) => (
            <li key={s.strategy_id}>
              <TriCheckbox checked={sel.has(s.strategy_id)} onChange={(v) => onSelectStrategy(s.strategy_id, v)}
                label={`select ${s.display_name}`} testId={`str-${s.strategy_id}`} />
              <span className="muted">{String(i + 1).padStart(4, "0")}</span>{" "}
              <a href={href(`/runs/${cid}/${f.family_id}/${s.strategy_id}`)}>{s.display_name}</a>
              <span className="muted"> · {s.timeframe}</span> <Badge tone={statusTone(s.status)}>{s.status.replace("_", " ")}</Badge>
            </li>))}
        </ul>)}
    </div>
  );
}

function LiveRun({ jobId, onFinished, onDismiss }: { jobId: string; onFinished: () => void; onDismiss: () => void }) {
  const [job, setJob] = useState<CampaignJob | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    let t = 0;
    const poll = () => campaigns.job(jobId).then((j) => {
      if (!live) return;
      setJob(j); setErr(null);
      if (CAMPAIGN_JOB_FINAL.has(j.state)) onFinished(); else t = window.setTimeout(poll, LIVE_POLL_MS);
    }).catch((e) => { if (live) { setErr(e as ApiError); t = window.setTimeout(poll, 3 * LIVE_POLL_MS); } });
    poll();
    const clock = window.setInterval(() => setTick((x) => x + 1), 1000);
    return () => { live = false; window.clearTimeout(t); window.clearInterval(clock); };
  }, [jobId]);
  if (err && !job) return <Card title="Research run"><ErrorPanel error={err} title={`Run ${jobId} is not known to this app session`} />
    <p className="small muted">Jobs live in the running app; the durable record is in the run history below.</p>
    <Button small onClick={onDismiss}>Dismiss</Button></Card>;
  if (!job) return <Card title="Research run"><Loading label="Connecting to the background run…" /></Card>;
  const L = job.live;
  const tot: ScopeProgress | null | undefined = L.totals;
  const scopeN = L.n_scope ?? tot?.strategies ?? 0;
  const done = tot?.completed ?? 0;                             // from PERSISTED cells (survives restarts)
  const final = CAMPAIGN_JOB_FINAL.has(job.state);
  const el = elapsedS(job.started_at, job.finished_at);
  const eta = L.eta ?? null;
  const totalEst = eta?.state === "estimate" && eta.remaining_seconds != null && el != null ? el + eta.remaining_seconds : null;
  return (
    <Card testId="live-run"
      title={<>Research run <Mono>{job.job_id}</Mono> <Badge tone={statusTone(L.status)}>{L.status}</Badge></>}
      actions={final ? <Button small onClick={onDismiss}>Close</Button>
        : <Button small kind="danger" busy={cancelling} busyLabel="Stopping after the current strategy…" testId="cancel-run"
            onClick={async () => { setCancelling(true); try { setJob(await campaigns.cancel(jobId)); } finally { setCancelling(false); } }}>
            Cancel (keeps all completed results)</Button>}>
      <p data-testid="live-phase"><b>{L.phase}</b>{job.cancel_requested && !final && " · cancel requested: the current strategy finishes, then the run stops"}</p>
      <div className="progress" aria-label="research progress"><span style={{ width: `${scopeN ? (100 * done) / scopeN : 0}%` }} /></div>
      <div className="kpis">
        <Kpi label="Progress" value={pctOf(done, scopeN)} accent />
        <Kpi label="Completed / selected" value={`${fmt(done)} / ${fmt(scopeN)}`}
          sub={`${fmt(L.counts?.completed_this_run ?? 0)} in this run · ${fmt(L.counts?.skipped_completed ?? 0)} done before`} />
        <Kpi label="Remaining" value={fmt(Math.max(0, scopeN - done))} />
        <Kpi label="Errors" value={fmt(L.counts?.failed_this_run ?? 0)} tone={(L.counts?.failed_this_run ?? 0) ? "neg" : ""} sub="never counted as trials; retried on resume" />
        <Kpi label="Elapsed" value={fmtDuration(el)} sub={L.preflight_seconds != null ? `preflight ${fmtDuration(L.preflight_seconds)}` : L.status === "preflight" ? "preflight running…" : undefined} />
        <Kpi label="Estimated remaining" value={<EtaText eta={eta} />} testId="live-eta"
          sub={totalEst != null ? `estimated total ≈ ${fmtDuration(totalEst)}` : "estimate; not an exact completion time"} />
      </div>
      {L.current && !final && <p className="small" data-testid="live-current">Now evaluating <b>{L.current.display_name ?? L.current.strategy_id}</b>
        {" "}<Mono>{L.current.strategy_id}</Mono> · family <b>{L.current.family_id}</b> · {L.current.timeframe} on <Mono>{L.current.dataset_id}</Mono>
        {" "}({fmt(L.current.index)} of {fmt(L.current.of)} in this scope)</p>}
      <p className="small muted">Campaign <Mono>{job.campaign_id}</Mono> · search <Mono>{job.search_id}</Mono> · protocol <Mono>{L.protocol_id ?? "…"}</Mono>
        {L.protocol_version != null && ` v${L.protocol_version}`} · workers 1 · holdout locked · results are stored as they finish.</p>
      {job.error && <Banner tone="error">{job.error}</Banner>}
      {!!L.errors?.length && <details><summary>{L.errors.length} error(s)</summary><ul className="small">
        {L.errors.map((e, i) => <li key={i}>{e.strategy_id && <Mono>{e.strategy_id}</Mono>} {e.check ?? ""} {e.error ?? JSON.stringify(e.detail ?? "")}</li>)}</ul></details>}
    </Card>
  );
}

function RunHistory({ cid, runs, onRestoreScope }: { cid: string; runs: CampaignRunRecord[]; onRestoreScope: (ids: string[]) => void }) {
  const { toast } = useApp();
  return (
    <Card title={`Run history (${runs.length})`} testId="run-history">
      {!runs.length ? <Empty>No run yet.</Empty> : (
        <TableWrap><table>
          <thead><tr><th>Run</th><th>Created</th><th>Status</th><th>Scope</th><th className="num">Strategies</th>
            <th className="num">Completed in run</th><th className="num">Skipped</th><th className="num">Errors</th><th>Duration</th><th>Source</th><th></th></tr></thead>
          <tbody>{runs.map((x) => (
            <tr key={x.run_record_id}>
              <td><Mono>{x.run_record_id}</Mono></td><td>{shortTime(x.created_at)}</td>
              <td><Badge tone={statusTone(x.status)}>{x.status}</Badge>{x.status === "interrupted" && <span className="small muted"> resumable</span>}</td>
              <td className="small">{x.all_families ? "all strategies" : `${x.scope_kind === "strategies" ? "selection" : "families"}: ${x.families.join(", ")}`}</td>
              <td className="num">{fmt(x.n_scope)}</td><td className="num">{fmt(x.counts.completed_this_run)}</td>
              <td className="num">{fmt(x.counts.skipped_completed)}</td><td className="num">{fmt(x.counts.failed_this_run)}</td>
              <td>{fmtDuration(elapsedS(x.started_at ?? x.created_at, x.finished_at ?? x.updated_at))}</td><td>{x.source}</td>
              <td>{x.scope_file && <button type="button" className="linklike small" data-testid={`restore-${x.run_record_id}`}
                onClick={() => campaigns.runScope(cid, x.run_record_id).then((s) => { onRestoreScope(s.strategy_ids); toast("info", `Selection restored from ${x.run_record_id}`); })
                  .catch(() => toast("error", "Could not load that run's scope"))}>reselect scope</button>}</td>
            </tr>))}</tbody>
        </table></TableWrap>)}
    </Card>
  );
}

function FamilyResultsPage({ cid, fid }: { cid: string; fid: string }) {
  const res = useApi<FamilyResults>(campaigns.familyUrl(cid, fid));
  if (res.error) return <div className="page"><ErrorPanel error={res.error} title={`Could not load ${fid}`} /></div>;
  if (!res.data) return <div className="page"><Loading label="Loading stored results…" /></div>;
  const d = res.data;
  return (
    <div className="page" data-testid="family-results">
      <header className="page-head"><div><div className="eyebrow"><a href={href("/runs")}>Research runs</a> › <a href={href(`/runs/${cid}`)}><Mono>{cid}</Mono></a></div>
        <h1>{d.name} <span className="muted small"><Mono>{d.family_id}</Mono></span></h1>
        <p className="subtitle">{d.hypothesis}</p></div></header>
      <div className="kpis">
        <Kpi label="Strategies" value={fmt(d.n)} />
        <Kpi label="Completed" value={fmt(d.completed)} />
        <Kpi label="Remaining" value={fmt(d.remaining)} />
        <Kpi label="Timeframes" value={Object.entries(d.timeframes).map(([tf, k]) => `${tf}: ${k}`).join(" · ")} />
      </div>
      <p className="small muted">{d.note}. Data: Nasdaq / canonical BID-ASK research data, dataset chosen from each strategy's timeframe.</p>
      <TableWrap testId="family-strategies"><table>
        <thead><tr><th>Strategy</th><th>TF</th><th>Status</th><th className="num">Trades</th><th className="num">Net R</th>
          <th className="num">Expectancy R</th><th className="num">PF</th><th className="num">Max DD R</th><th>Sample</th><th></th></tr></thead>
        <tbody>{d.strategies.map((s) => (
          <tr key={s.strategy_id}>
            <td><a href={href(`/runs/${cid}/${fid}/${s.strategy_id}`)}>{s.display_name}</a><div className="small muted">{s.explanation}</div>
              <div className="small muted"><Mono>{s.strategy_id}</Mono></div></td><td>{s.timeframe}</td>
            <td><Badge tone={statusTone(s.status)}>{s.status.replace("_", " ")}</Badge>{s.error && <span className="small muted" title={s.error}> error</span>}</td>
            <td className="num">{fmt(s.headline.trade_count)}</td><td className="num">{r(s.headline.net_r)}</td>
            <td className="num">{r(s.headline.expectancy_r)}</td><td className="num">{n(s.headline.profit_factor)}</td>
            <td className="num">{r(s.headline.max_drawdown_r)}</td><td className="small">{fmt(s.headline.sample_label)}</td>
            <td className="row-actions">{s.run_id ? <a href={href(`/results/${s.run_id}`)}>Full report ›</a> : <span className="muted small">no result yet</span>}</td>
          </tr>))}</tbody>
      </table></TableWrap>
    </div>
  );
}

function StrategyResultPage({ cid, fid, sid }: { cid: string; fid: string; sid: string }) {
  const res = useApi<StrategyResult>(campaigns.strategyUrl(cid, sid));
  if (res.error) return <div className="page"><ErrorPanel error={res.error} title={`Could not load ${sid}`} /></div>;
  if (!res.data) return <div className="page"><Loading label="Loading stored result…" /></div>;
  const d = res.data;
  const p = d.presentation;
  return (
    <div className="page" data-testid="strategy-result">
      <header className="page-head"><div><div className="eyebrow"><a href={href(`/runs/${cid}`)}><Mono>{cid}</Mono></a> › <a href={href(`/runs/${cid}/${fid}`)}>{d.family_name}</a></div>
        <h1>{p.display_name} <Badge tone={statusTone(d.status)}>{d.status.replace("_", " ")}</Badge></h1>
        <p className="subtitle" data-testid="strategy-explanation">{p.explanation}</p></div>
        <div className="actions">{d.run_id && <a className="btn btn-primary" href={href(`/results/${d.run_id}`)}>Full run report</a>}
          <a className="btn" href={href(`/strategies/${sid}`)}>Strategy definition</a></div></header>
      {d.error && <Banner tone="error">{d.error}</Banner>}
      <Card title="Identity and research design (read-only)" testId="strategy-identity">
        <KeyValues rows={[
          ["Strategy ID", <Mono>{p.strategy_id}</Mono>], ["Logic hash", <Mono>{p.logic_hash}</Mono>], ["Definition hash", <Mono>{p.definition_hash}</Mono>],
          ["Machine name", <Mono>{p.machine_name ?? "—"}</Mono>], ["Family", <>{d.family_name} <Mono>{d.family_id}</Mono></>],
          ["Timeframe", <>{d.timeframe} <span className="muted small">(from the frozen definition; selects the dataset)</span></>],
          ["Dataset", <><Mono>{d.dataset_id ?? "—"}</Mono> <span className="muted small">Nasdaq / canonical BID-ASK research data</span></>],
          ["Sizing / account", <>{JSON.stringify(d.sizing)} · {d.execution_contract} · ${fmt(d.account.starting_equity)} research account</>],
          ["Execution time", d.duration_s != null ? `${d.duration_s}s` : "—"],
        ]} />
      </Card>
      <Card title="Key parameters"><KeyValues rows={Object.entries(p.key_parameters).map(([k, v]) => [k, String(v)])} /></Card>
      {d.metrics && <Card title="Stored base result (historical result under stated assumptions)">
        <KeyValues rows={Object.entries(d.metrics).filter(([, v]) => typeof v !== "object").slice(0, 24).map(([k, v]) => [k, String(v)])} /></Card>}
      {d.prop && <Card title="Prop audit (downstream of the base result)" testId="strategy-prop">
        <TableWrap><table><thead><tr><th>Profile</th><th>Status</th><th>Result</th></tr></thead>
          <tbody>{d.prop.profiles.map((x) => <tr key={x.profile.profile_id}><td>{x.profile.profile_id} v{x.profile.version}</td>
            <td><Badge tone={x.status === "PASS" ? "ok" : x.status === "NOT_APPLICABLE" ? "neutral" : "warn"}>{x.status}</Badge></td>
            <td className="small">{x.final_status}</td></tr>)}</tbody></table></TableWrap></Card>}
      <Card title="Provenance"><KeyValues rows={Object.entries(d.provenance).map(([k, v]) => [k, typeof v === "object" ? <Mono>{JSON.stringify(v)}</Mono> : <Mono>{String(v)}</Mono>])} /></Card>
    </div>
  );
}
