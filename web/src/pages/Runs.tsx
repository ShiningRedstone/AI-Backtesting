import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import type { ApiError } from "../api/client";
import { CAMPAIGN_JOB_FINAL, LIVE_POLL_MS, campaigns, fmtDuration } from "../api/campaigns";
import type { CampaignDetail, CampaignJob, CampaignListRow, CampaignRunRecord, CampaignTree, CheckReport,
  FamilyResults, ScopeProgress, StrategyResult, TreeFamily } from "../api/campaigns";
import { useApi, useApp } from "../app/context";
import { datasetLabel, facetLabel, familyLabel, humanize, keyLabel, metricLabel, profileLabel, statusLabel, valueLabel } from "../app/labels";
import { href, useRoute } from "../app/router";
import { plainText } from "../components/research";
import { Badge, Banner, Button, Card, Empty, ErrorPanel, KeyValues, Kpi, Loading, Mono, ObjectView, TableWrap, TechDetails, fmt, n, r,
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
const SOURCE: Record<string, string> = { cli: "Command line", desktop: "Desktop app" };
/** A stored value for display: numbers exactly as stored, codes and SHOUTING labels as words. */
const show = (v: unknown) => (typeof v === "number" ? String(v) : typeof v === "string" && /^[A-Z][A-Z ]+$/.test(v) ? humanize(v) : valueLabel(v));
/** A key parameter value in words (timeframe, session and direction through the facet labels). */
const paramValue = (k: string, v: unknown) => (["timeframe", "session", "direction"].includes(k) ? facetLabel(k, v)
  : typeof v === "string" ? plainText(v) : show(v));

function Governance({ c }: { c: CampaignDetail | CampaignListRow }) {
  return (
    <><KeyValues rows={[
      ["Research protocol", <>Version {c.protocol.protocol_version} · budget {fmt(c.protocol.max_unique_trials)} unique trials ·
        Bonferroni family = {c.protocol.family_size_rule === "declared_max_unique_trials" ? "declared budget" : humanize(c.protocol.family_size_rule)}</>],
      ["Discovery window", <>{c.discovery.trading_dates.join(" → ")} <span className="muted small">(every run evaluates only these bars)</span></>],
      ["Holdout", <><Badge tone="warn">locked</Badge> <span className="small muted">never requested, loaded or evaluated by research runs</span></>],
      ["Data", <>Nasdaq / canonical BID-ASK research data · dataset chosen automatically from each strategy's frozen timeframe:{" "}
        {Object.entries(c.datasets).map(([tf, d]) => <span key={tf} className="chip" title={datasetLabel(d.dataset_id)}>{facetLabel("timeframe", tf)}</span>)}</>],
      ["Frozen strategy list", <>{fmt(c.manifest.n_strategies)} strategies · strategy factory version {c.manifest.factory_version}</>],
      ["Execution", <>{c.execution.execution_contract} whole contracts (max {c.execution.max_quantity}) · ${fmt(c.execution.account.starting_equity)} research account ·
        workers {c.execution.workers} · one trial per strategy · {humanize(c.stage).toLowerCase()} only</>],
      ["Prop audit", <>{c.prop_simulation.profiles.map((p) => <span key={p.profile_id} className="chip">{profileLabel(p.profile_id)} v{p.version}</span>)}
        <span className="small muted"> downstream of the base result; never changes it</span></>],
    ]} />
    <TechDetails rows={[["Campaign id", <Mono>{c.campaign_id}</Mono>], ["Protocol id", <Mono>{c.protocol.protocol_id}</Mono>],
      ["Search id", <Mono>{c.search_id}</Mono>], ["Manifest id", <Mono>{c.manifest.manifest_id}</Mono>], ["Config hash", <Mono>{c.config_hash}</Mono>],
      ...Object.entries(c.datasets).map(([tf, d]): [string, ReactNode] => [`Dataset (${tf})`, <Mono>{d.dataset_id} · {d.content_hash}</Mono>]),
      ...c.prop_simulation.profiles.map((p): [string, ReactNode] => [`Prop profile ${p.profile_id}`, <Mono>v{p.version} · {p.profile_hash}</Mono>])]} /></>
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
      {list.data && !list.data.length && <Empty>No frozen campaign in this workspace yet. Freeze one from the command line with the
        <Mono>research campaign-freeze</Mono> command (the strategy manifest and research protocol must exist first).</Empty>}
      {list.data?.map((c) => c.error
        ? <Card key={c.campaign_id} title="Research campaign"><Banner tone="error">{c.error.message}</Banner>
          <TechDetails rows={[["Campaign id", <Mono>{c.campaign_id}</Mono>], ["Error code", <Mono>{c.error.code}</Mono>]]} /></Card>
        : (
          <Card key={c.campaign_id} testId={`campaign-${c.campaign_id}`}
            title={<><span title={c.campaign_id}>Research campaign · {fmt(c.manifest.n_strategies)} strategies</span> <Badge tone={c.progress.remaining === 0 ? "ok" : "info"}>
              {c.progress.remaining === 0 ? "complete" : `${pctOf(c.progress.completed, c.progress.strategies)} done`}</Badge></>}
            actions={<a className="btn btn-primary" href={href(`/runs/${c.campaign_id}`)} data-testid={`open-${c.campaign_id}`}>Open research browser</a>}>
            <div className="kpis">
              <Kpi label="Strategies" value={fmt(c.progress.strategies)} />
              <Kpi label="Completed" value={fmt(c.progress.completed)} meter={c.progress.fraction_done} />
              <Kpi label="Remaining" value={fmt(c.progress.remaining)} />
              <Kpi label="Failed (retried on resume)" value={fmt(c.progress.failed)} />
              <Kpi label="Runs" value={fmt(c.n_runs)} sub={c.latest_run ? `last: ${statusLabel(c.latest_run.status).toLowerCase()} · ${shortTime(c.latest_run.created_at)}` : "none yet"} />
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
  return <Banner tone="info" testId="active-job">A research run is in progress — {plainText(job.live.phase)}.{" "}
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
  if (det.error) return <div className="page"><ErrorPanel error={det.error} title="Could not load this campaign" /></div>;
  if (treeQ.error) return <div className="page"><ErrorPanel error={treeQ.error} title="Could not load the research tree of this campaign" /></div>;
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
        <h1>Research browser</h1>
        <p className="subtitle" title={d.campaign_id}>{plainText(d.data_line)}</p></div></header>
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
          <Kpi label="Run status" value={<Badge tone={statusTone(latest?.status ?? "none")}>{latest?.status ? statusLabel(latest.status) : "no run yet"}</Badge>}
            sub={latest ? shortTime(latest.updated_at ?? latest.created_at) : undefined} />
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
          {check.ready ? "Ready" : "Not ready"} — {plainText(check.note)}
          {!check.ready && <ul>{check.checks.filter((x) => !x.ok).map((x) => <li key={x.check}>{humanize(x.check)}: <ObjectView value={x.detail} /></li>)}</ul>}
          {!check.ready && <TechDetails><pre className="code">{JSON.stringify(check.checks.filter((x) => !x.ok), null, 2)}</pre></TechDetails>}
        </Banner>}
        <p className="small muted">{plainText(tree.note)}. Each selected strategy is evaluated once, on the dataset of its own frozen timeframe,
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
        <span className="small muted">{f.group ? ` · ${humanize(f.group)}` : ""}</span>
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
              <span className="muted"> · {facetLabel("timeframe", s.timeframe)}</span> <Badge tone={statusTone(s.status)}>{statusLabel(s.status)}</Badge>
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
  if (err && !job) return <Card title="Research run"><ErrorPanel error={err} title="This run is not known to this app session" />
    <p className="small muted">Jobs live in the running app; the durable record is in the run history below.</p>
    <Button small onClick={onDismiss}>Dismiss</Button></Card>;
  if (!job) return <Card title="Research run"><Loading label="Connecting to the background run…" /></Card>;
  const L = job.live;
  const tot: ScopeProgress | null | undefined = L.totals;
  const scopeN = L.n_scope ?? tot?.strategies ?? 0;
  const done = tot?.completed ?? 0;                             // from PERSISTED cells (survives restarts)
  const final = CAMPAIGN_JOB_FINAL.has(job.state);
  const el = elapsedS(job.started_at, job.finished_at);
  return (
    <Card testId="live-run"
      title={<>Research run <Badge tone={statusTone(L.status)}>{statusLabel(L.status)}</Badge></>}
      actions={final ? <Button small onClick={onDismiss}>Close</Button>
        : <Button small kind="danger" busy={cancelling} busyLabel="Stopping after the current strategy…" testId="cancel-run"
            onClick={async () => { setCancelling(true); try { setJob(await campaigns.cancel(jobId)); } finally { setCancelling(false); } }}>
            Cancel (keeps all completed results)</Button>}>
      <p data-testid="live-phase"><b>{plainText(L.phase)}</b>{job.cancel_requested && !final && " · cancel requested: the current strategy finishes, then the run stops"}</p>
      <div className="progress" aria-label="research progress"><span style={{ width: `${scopeN ? (100 * done) / scopeN : 0}%` }} /></div>
      <div className="kpis">
        <Kpi label="Progress" value={pctOf(done, scopeN)} accent />
        <Kpi label="Completed / selected" value={`${fmt(done)} / ${fmt(scopeN)}`}
          sub={`${fmt(L.counts?.completed_this_run ?? 0)} in this run · ${fmt(L.counts?.skipped_completed ?? 0)} done before`} />
        <Kpi label="Remaining" value={fmt(Math.max(0, scopeN - done))} />
        <Kpi label="Errors" value={fmt(L.counts?.failed_this_run ?? 0)} tone={(L.counts?.failed_this_run ?? 0) ? "neg" : ""} sub="never counted as trials; retried on resume" />
        <Kpi label="Elapsed" value={fmtDuration(el)} sub={L.preflight_seconds != null ? `preflight ${fmtDuration(L.preflight_seconds)}` : L.status === "preflight" ? "preflight running…" : undefined} />
      </div>
      {L.current && !final && <p className="small" data-testid="live-current">Now evaluating <b title={L.current.strategy_id}>{L.current.display_name ?? "a strategy"}</b>
        {" "}· family <b>{familyLabel(L.current.family_id)}</b> · {facetLabel("timeframe", L.current.timeframe)} on {datasetLabel(L.current.dataset_id)}
        {" "}({fmt(L.current.index)} of {fmt(L.current.of)} in this scope)</p>}
      <p className="small muted">{L.protocol_version != null ? `Research protocol version ${L.protocol_version} · ` : ""}workers 1 · holdout locked · results are stored as they finish.</p>
      <TechDetails rows={[["Job id", <Mono>{job.job_id}</Mono>], ["Campaign id", <Mono>{job.campaign_id}</Mono>], ["Search id", <Mono>{job.search_id}</Mono>],
        ["Protocol id", L.protocol_id ? <Mono>{L.protocol_id}</Mono> : null],
        ["Current strategy id", L.current && !final ? <Mono>{L.current.strategy_id}</Mono> : null], ["Current dataset id", L.current && !final ? <Mono>{L.current.dataset_id}</Mono> : null]]} />
      {job.error && <Banner tone="error">{job.error}</Banner>}
      {!!L.errors?.length && <details><summary>{L.errors.length} error(s)</summary><ul className="small">
        {L.errors.map((e, i) => <li key={i}>{e.check ? `${humanize(e.check)}: ` : ""}{e.error ? plainText(e.error) : e.detail != null ? <ObjectView value={e.detail} /> : "error"}</li>)}</ul>
        <TechDetails><pre className="code">{JSON.stringify(L.errors, null, 2)}</pre></TechDetails></details>}
    </Card>
  );
}

function RunHistory({ cid, runs, onRestoreScope }: { cid: string; runs: CampaignRunRecord[]; onRestoreScope: (ids: string[]) => void }) {
  const { toast } = useApp();
  return (
    <Card title={`Run history (${runs.length})`} testId="run-history">
      {!runs.length ? <Empty>No run yet.</Empty> : (
        <TableWrap><table>
          <thead><tr><th>Run (created)</th><th>Status</th><th>Scope</th><th className="num">Strategies</th>
            <th className="num">Completed in run</th><th className="num">Skipped</th><th className="num">Errors</th><th>Duration</th><th>Source</th><th></th></tr></thead>
          <tbody>{runs.map((x) => (
            <tr key={x.run_record_id}>
              <td title={x.run_record_id}>{shortTime(x.created_at)}</td>
              <td><Badge tone={statusTone(x.status)}>{statusLabel(x.status)}</Badge>{x.status === "interrupted" && <span className="small muted"> resumable</span>}</td>
              <td className="small">{x.all_families ? "all strategies" : `${x.scope_kind === "strategies" ? "selection" : "families"}: ${x.families.map((f) => familyLabel(f)).join(", ")}`}</td>
              <td className="num">{fmt(x.n_scope)}</td><td className="num">{fmt(x.counts.completed_this_run)}</td>
              <td className="num">{fmt(x.counts.skipped_completed)}</td><td className="num">{fmt(x.counts.failed_this_run)}</td>
              <td>{fmtDuration(elapsedS(x.started_at ?? x.created_at, x.finished_at ?? x.updated_at))}</td><td>{SOURCE[x.source] ?? humanize(x.source)}</td>
              <td>{x.scope_file && <button type="button" className="linklike small" data-testid={`restore-${x.run_record_id}`}
                onClick={() => campaigns.runScope(cid, x.run_record_id).then((s) => { onRestoreScope(s.strategy_ids); toast("info", `Selection restored from the run of ${shortTime(x.created_at)}`); })
                  .catch(() => toast("error", "Could not load that run's scope"))}>reselect scope</button>}</td>
            </tr>))}</tbody>
        </table>
        <TechDetails rows={runs.map((x): [string, ReactNode] => [shortTime(x.created_at), <Mono>{x.run_record_id}</Mono>])} /></TableWrap>)}
    </Card>
  );
}

function FamilyResultsPage({ cid, fid }: { cid: string; fid: string }) {
  const res = useApi<FamilyResults>(campaigns.familyUrl(cid, fid));
  if (res.error) return <div className="page"><ErrorPanel error={res.error} title="Could not load this family" /></div>;
  if (!res.data) return <div className="page"><Loading label="Loading stored results…" /></div>;
  const d = res.data;
  return (
    <div className="page" data-testid="family-results">
      <header className="page-head"><div><div className="eyebrow"><a href={href("/runs")}>Research runs</a> › <a href={href(`/runs/${cid}`)} title={cid}>Research browser</a></div>
        <h1>{d.name}</h1>
        <p className="subtitle">{d.hypothesis}</p></div></header>
      <div className="kpis">
        <Kpi label="Strategies" value={fmt(d.n)} />
        <Kpi label="Completed" value={fmt(d.completed)} />
        <Kpi label="Remaining" value={fmt(d.remaining)} />
        <Kpi label="Timeframes" value={Object.entries(d.timeframes).map(([tf, k]) => `${facetLabel("timeframe", tf)}: ${k}`).join(" · ")} />
      </div>
      <p className="small muted">{plainText(d.note)}. Data: Nasdaq / canonical BID-ASK research data, dataset chosen from each strategy's timeframe.</p>
      <TableWrap testId="family-strategies"><table>
        <thead><tr><th>Strategy</th><th>Timeframe</th><th>Status</th><th className="num">Trades</th><th className="num">Net R</th>
          <th className="num">Net R per trade</th><th className="num">Profit factor</th><th className="num">Max drawdown (R)</th><th>Sample size</th><th></th></tr></thead>
        <tbody>{d.strategies.map((s) => (
          <tr key={s.strategy_id}>
            <td><a href={href(`/runs/${cid}/${fid}/${s.strategy_id}`)} title={s.strategy_id}>{s.display_name}</a><div className="small muted">{s.explanation}</div></td>
            <td>{facetLabel("timeframe", s.timeframe)}</td>
            <td><Badge tone={statusTone(s.status)}>{statusLabel(s.status)}</Badge>{s.error && <span className="small muted" title={s.error}> error</span>}</td>
            <td className="num">{fmt(s.headline.trade_count)}</td><td className="num">{r(s.headline.net_r)}</td>
            <td className="num">{r(s.headline.expectancy_r)}</td><td className="num">{n(s.headline.profit_factor)}</td>
            <td className="num">{r(s.headline.max_drawdown_r)}</td><td className="small">{s.headline.sample_label ? humanize(s.headline.sample_label) : "—"}</td>
            <td className="row-actions">{s.run_id ? <a href={href(`/results/${s.run_id}`)}>Full report ›</a> : <span className="muted small">no result yet</span>}</td>
          </tr>))}</tbody>
      </table></TableWrap>
      <TechDetails rows={[["Campaign id", <Mono>{cid}</Mono>], ["Family id", <Mono>{d.family_id}</Mono>],
        ...d.strategies.map((s): [string, ReactNode] => [s.display_name, <Mono>{s.strategy_id}{s.run_id ? ` · ${s.run_id}` : ""}</Mono>])]} />
    </div>
  );
}

function StrategyResultPage({ cid, fid, sid }: { cid: string; fid: string; sid: string }) {
  const res = useApi<StrategyResult>(campaigns.strategyUrl(cid, sid));
  if (res.error) return <div className="page"><ErrorPanel error={res.error} title="Could not load this strategy result" /></div>;
  if (!res.data) return <div className="page"><Loading label="Loading stored result…" /></div>;
  const d = res.data;
  const p = d.presentation;
  return (
    <div className="page" data-testid="strategy-result">
      <header className="page-head"><div><div className="eyebrow"><a href={href(`/runs/${cid}`)} title={cid}>Research browser</a> › <a href={href(`/runs/${cid}/${fid}`)}>{d.family_name}</a></div>
        <h1>{p.display_name} <Badge tone={statusTone(d.status)}>{statusLabel(d.status)}</Badge></h1>
        <p className="subtitle" data-testid="strategy-explanation">{p.explanation}</p></div>
        <div className="actions">{d.run_id && <a className="btn btn-primary" href={href(`/results/${d.run_id}`)}>Full run report</a>}
          <a className="btn" href={href(`/strategies/${sid}`)}>Strategy definition</a></div></header>
      {d.error && <Banner tone="error">{d.error}</Banner>}
      <Card title="Identity and research design (read-only)" testId="strategy-identity">
        <KeyValues rows={[
          ["Family", d.family_name],
          ["Timeframe", <>{facetLabel("timeframe", d.timeframe)} <span className="muted small">(from the frozen definition; selects the dataset)</span></>],
          ["Dataset", <>{datasetLabel(d.dataset_id)} <span className="muted small">Nasdaq / canonical BID-ASK research data</span></>],
          ["Sizing / account", <>{Object.entries(d.sizing).map(([k, v]) => `${keyLabel(k)} ${k === "mode" ? humanize(v).toLowerCase() : show(v)}`).join(", ")} · {d.execution_contract} · ${fmt(d.account.starting_equity)} research account</>],
          ["Execution time", d.duration_s != null ? `${d.duration_s} seconds` : "—"],
        ]} />
        <TechDetails rows={[["Strategy id", <Mono>{p.strategy_id}</Mono>], ["Logic hash", <Mono>{p.logic_hash}</Mono>], ["Definition hash", <Mono>{p.definition_hash}</Mono>],
          ["Machine name", p.machine_name ? <Mono>{p.machine_name}</Mono> : null], ["Family id", <Mono>{d.family_id}</Mono>],
          ["Dataset id", d.dataset_id ? <Mono>{d.dataset_id}</Mono> : null], ["Campaign id", <Mono>{cid}</Mono>], ["Run id", d.run_id ? <Mono>{d.run_id}</Mono> : null],
          ["Trades hash", d.trades_hash ? <Mono>{d.trades_hash}</Mono> : null], ["Sizing (as stored)", <Mono>{JSON.stringify(d.sizing)}</Mono>]]} />
      </Card>
      <Card title="Key parameters"><KeyValues rows={Object.entries(p.key_parameters).map(([k, v]) => [keyLabel(k), paramValue(k, v)])} /></Card>
      {d.metrics && <Card title="Stored base result (historical result under stated assumptions)">
        <KeyValues rows={Object.entries(d.metrics).filter(([, v]) => typeof v !== "object").slice(0, 24).map(([k, v]) => [metricLabel(k), show(v)])} /></Card>}
      {d.prop && <Card title="Prop audit (downstream of the base result)" testId="strategy-prop">
        <TableWrap><table><thead><tr><th>Profile</th><th>Status</th><th>Result</th></tr></thead>
          <tbody>{d.prop.profiles.map((x) => <tr key={x.profile.profile_id}><td title={x.profile.profile_id}>{profileLabel(x.profile.profile_id)} v{x.profile.version}</td>
            <td><Badge tone={x.status === "PASS" ? "ok" : x.status === "NOT_APPLICABLE" ? "neutral" : "warn"}>{statusLabel(x.status)}</Badge></td>
            <td className="small">{statusLabel(x.final_status)}</td></tr>)}</tbody></table></TableWrap></Card>}
      <Card title="Provenance"><p className="small muted">Where this result came from: the frozen campaign, protocol, data and code that produced it.</p>
        <TechDetails rows={Object.entries(d.provenance).map(([k, v]): [string, ReactNode] => [keyLabel(k), typeof v === "object" ? <Mono>{JSON.stringify(v)}</Mono> : <Mono>{String(v)}</Mono>])} /></Card>
    </div>
  );
}
