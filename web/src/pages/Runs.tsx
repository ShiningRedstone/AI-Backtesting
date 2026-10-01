import { useEffect, useMemo, useState } from "react";
import type { ApiError } from "../api/client";
import { CAMPAIGN_JOB_FINAL, LIVE_POLL_MS, campaigns } from "../api/campaigns";
import type { CampaignDetail, CampaignJob, CampaignListRow, CampaignRunRecord, CheckReport, FamilyResults,
  StrategyResult } from "../api/campaigns";
import { useApi, useApp } from "../app/context";
import { href, useRoute } from "../app/router";
import { Badge, Banner, Button, Card, Checkbox, Empty, ErrorPanel, KeyValues, Kpi, Loading, Mono, TableWrap, fmt, n, r,
  shortTime } from "../components/ui";

/** Research Runs (ADR-69): launch and monitor frozen-campaign research from the desktop app.
 *  The page never evaluates anything itself: it starts the backend's own campaign runner (the same one as the CLI
 *  `research campaign-run`) as a background job and shows its durable records. Family selection is a run SCOPE,
 *  not a quality filter: families and strategies are always listed in the manifest catalog's order. */
export function RunsPage() {
  const route = useRoute();
  const [cid, fid, sid] = route.parts.slice(1);
  if (cid && fid && sid) return <StrategyResultPage cid={cid} sid={sid} fid={fid} />;
  if (cid && fid) return <FamilyResultsPage cid={cid} fid={fid} />;
  if (cid) return <CampaignPage cid={cid} />;
  return <CampaignList />;
}

const pctOf = (done: number, total: number) => (total ? `${((100 * done) / total).toFixed(1)}%` : "—");
const elapsed = (from: string | null | undefined, to?: string | null) => {
  if (!from) return "—";
  const s = Math.max(0, Math.round(((to ? Date.parse(to) : Date.now()) - Date.parse(from)) / 1000));
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m ${String(s % 60).padStart(2, "0")}s`;
};
const statusTone = (s: string): "ok" | "warn" | "error" | "info" | "neutral" =>
  s === "completed" ? "ok" : s === "failed" || s === "stopped_on_failure" ? "error"
    : s === "running" || s === "preflight" ? "info" : s === "cancelled" || s === "interrupted" || s === "incomplete" ? "warn" : "neutral";

function Governance({ c }: { c: CampaignDetail | CampaignListRow }) {
  return (
    <KeyValues rows={[
      ["Campaign", <Mono>{c.campaign_id}</Mono>],
      ["Research protocol", <><Mono>{c.protocol.protocol_id}</Mono> v{c.protocol.protocol_version} · budget {fmt(c.protocol.max_unique_trials)} unique trials ·
        Bonferroni family = {c.protocol.family_size_rule === "declared_max_unique_trials" ? "declared budget" : c.protocol.family_size_rule}</>],
      ["Frozen search", <Mono>{c.search_id}</Mono>],
      ["Manifest", <><Mono>{c.manifest.manifest_id}</Mono> · {fmt(c.manifest.n_strategies)} strategies · {c.manifest.factory_version}</>],
      ["Stage", <>{c.stage} only · discovery {c.discovery.trading_dates.join(" → ")} · holdout <b>locked</b> (never read by these runs)</>],
      ["Datasets", <>{Object.entries(c.datasets).map(([tf, d]) => <span key={tf} className="chip" title={d.content_hash}>{tf} <Mono>{d.dataset_id}</Mono></span>)}</>],
      ["Execution", <>workers {c.execution.workers} · {c.execution.execution_contract} whole contracts (max {c.execution.max_quantity}) ·
        ${fmt(c.execution.account.starting_equity)} research account · one trial per strategy</>],
      ["Prop audit", <>{c.prop_simulation.profiles.map((p) => <span key={p.profile_id} className="chip" title={p.profile_hash}>{p.profile_id} v{p.version}</span>)}
        <span className="small muted"> downstream of the base result; never changes it</span></>],
    ]} />
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
            actions={<a className="btn btn-primary" href={href(`/runs/${c.campaign_id}`)} data-testid={`open-${c.campaign_id}`}>Open · Run research</a>}>
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

function CampaignPage({ cid }: { cid: string }) {
  const route = useRoute();
  const { toast } = useApp();
  const det = useApi<CampaignDetail>(campaigns.detailUrl(cid));
  const [sel, setSel] = useState<Set<string> | null>(null);
  const [jobId, setJobId] = useState<string | null>(route.query.get("job"));
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [check, setCheck] = useState<CheckReport | null>(null);
  const [checking, setChecking] = useState(false);
  const d = det.data;
  useEffect(() => { if (d && sel === null) setSel(new Set(d.families.map((f) => f.family_id))); }, [d]);
  useEffect(() => {                                    // re-attach to a run started earlier (or from another page)
    if (jobId) return;
    campaigns.active().then((a) => { if (a.job && a.job.campaign_id === cid && !CAMPAIGN_JOB_FINAL.has(a.job.state)) setJobId(a.job.job_id); })
      .catch(() => undefined);
  }, [cid]);
  const scope = useMemo(() => {
    if (!d || !sel) return { strategies: 0, completed: 0, remaining: 0, families: 0 };
    const fs = d.families.filter((f) => sel.has(f.family_id));
    return { families: fs.length, strategies: fs.reduce((a, f) => a + f.n_strategies, 0),
             completed: fs.reduce((a, f) => a + f.completed, 0), remaining: fs.reduce((a, f) => a + f.remaining, 0) };
  }, [d, sel]);
  if (det.error) return <div className="page"><ErrorPanel error={det.error} title={`Could not load campaign ${cid}`} /></div>;
  if (!d || !sel) return <div className="page"><Loading label="Loading campaign…" /></div>;
  const all = sel.size === d.families.length;
  const toggle = (fid: string, on: boolean) => { const s = new Set(sel); if (on) s.add(fid); else s.delete(fid); setSel(s); };
  const start = async () => {
    setStarting(true); setErr(null);
    try {
      const j = await campaigns.start(cid, all ? null : d.families.filter((f) => sel.has(f.family_id)).map((f) => f.family_id));
      setJobId(j.job_id);
      toast("info", `Research run started (${all ? "all families" : `${sel.size} families`}) — preflight first.`);
    } catch (e) { setErr(e as ApiError); } finally { setStarting(false); }
  };
  const runCheck = async () => {
    setChecking(true); setErr(null);
    try { setCheck(await campaigns.check(cid)); } catch (e) { setErr(e as ApiError); } finally { setChecking(false); }
  };
  return (
    <div className="page" data-testid="campaign-page">
      <header className="page-head"><div><div className="eyebrow"><a href={href("/runs")}>Research runs</a></div>
        <h1>Campaign <Mono>{d.campaign_id}</Mono></h1>
        <p className="subtitle">{fmt(d.progress.completed)} of {fmt(d.progress.strategies)} strategies evaluated
          ({pctOf(d.progress.completed, d.progress.strategies)}) · {fmt(d.progress.remaining)} remaining</p></div></header>
      <Card title="Governance — what controls every run of this campaign" testId="campaign-governance"><Governance c={d} /></Card>
      {jobId && <LiveRun jobId={jobId} onFinished={() => det.reload()} onDismiss={() => setJobId(null)} />}
      <Card title="Run scope — families" testId="family-selector"
        actions={<>
          <Button small onClick={() => setSel(new Set(d.families.map((f) => f.family_id)))} testId="select-all">Select all</Button>
          <Button small onClick={() => setSel(new Set())} testId="clear-all">Clear all</Button>
        </>}>
        <p className="small muted">{d.note}. Each selected strategy is evaluated once, on its own timeframe's frozen dataset, in the
          campaign's frozen search; strategies already evaluated are skipped (never re-run, never a second trial).</p>
        <div className="kpis" data-testid="scope-summary">
          <Kpi label="Families selected" value={`${scope.families} / ${d.families.length}`} accent />
          <Kpi label="Strategies (= cells = trials)" value={fmt(scope.strategies)} />
          <Kpi label="Already evaluated" value={fmt(scope.completed)} />
          <Kpi label="To evaluate now" value={fmt(scope.remaining)} />
        </div>
        <div className="actions">
          <Button kind="primary" onClick={start} busy={starting} busyLabel="Starting…" testId="run-research"
            disabled={!sel.size || !!jobId || scope.remaining === 0}
            title={jobId ? "A run is in progress" : scope.remaining === 0 ? "Every selected strategy is already evaluated" : undefined}>
            {scope.completed > 0 && scope.remaining > 0 ? "Resume research run" : "Run research"} ({fmt(scope.remaining)} to evaluate)
          </Button>
          <Button onClick={runCheck} busy={checking} busyLabel="Checking (read-only)…" testId="run-check">Preflight check (read-only)</Button>
        </div>
        {err && <ErrorPanel error={err} title="The run could not start" />}
        <TableWrap testId="family-table"><table>
          <thead><tr><th></th><th>Family</th><th>Group</th><th className="num">Strategies</th><th>Timeframes</th>
            <th className="num">Evaluated</th><th className="num">Failed</th><th className="num">Remaining</th><th></th></tr></thead>
          <tbody>{d.families.map((f) => (
            <tr key={f.family_id}>
              <td><Checkbox checked={sel.has(f.family_id)} onChange={(v) => toggle(f.family_id, v)} label="" testId={`fam-${f.family_id}`} /></td>
              <td title={f.hypothesis ?? ""}><b>{f.name}</b> <span className="small muted"><Mono>{f.family_id}</Mono></span></td>
              <td>{f.group ?? "—"}</td><td className="num">{fmt(f.n_strategies)}</td>
              <td className="small">{Object.entries(f.timeframes).map(([tf, k]) => `${tf}:${k}`).join(" ")}</td>
              <td className="num">{fmt(f.completed)}</td><td className="num">{fmt(f.failed)}</td><td className="num">{fmt(f.remaining)}</td>
              <td>{f.completed > 0 ? <a href={href(`/runs/${cid}/${f.family_id}`)}>Results ›</a> : <span className="muted small">no results yet</span>}</td>
            </tr>))}</tbody>
        </table></TableWrap>
        {check && <Banner tone={check.ready ? "ok" : "error"} testId="check-result">
          {check.ready ? "READY" : "NOT READY"} — {check.note}
          {!check.ready && <ul>{check.checks.filter((x) => !x.ok).map((x) => <li key={x.check}>{x.check}: <Mono>{JSON.stringify(x.detail)}</Mono></li>)}</ul>}
        </Banner>}
      </Card>
      <RunHistory runs={d.runs} />
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
  const tot = L.totals;
  const done = (tot?.completed ?? 0) + (job.state === "running" ? (L.counts?.completed_this_run ?? 0) : 0);
  const scopeN = L.n_scope ?? tot?.strategies ?? 0;
  const final = CAMPAIGN_JOB_FINAL.has(job.state);
  const shownDone = final && tot ? tot.completed : done;
  return (
    <Card testId="live-run"
      title={<>Research run <Mono>{job.job_id}</Mono> <Badge tone={statusTone(L.status)}>{L.status}</Badge></>}
      actions={final ? <Button small onClick={onDismiss}>Close</Button>
        : <Button small kind="danger" busy={cancelling} busyLabel="Stopping after the current strategy…" testId="cancel-run"
            onClick={async () => { setCancelling(true); try { setJob(await campaigns.cancel(jobId)); } finally { setCancelling(false); } }}>
            Cancel (keeps all completed results)</Button>}>
      <p data-testid="live-phase"><b>{L.phase}</b>{job.cancel_requested && !final && " · cancel requested: the current strategy finishes, then the run stops"}</p>
      <div className="progress" aria-label="research progress"><span style={{ width: `${scopeN ? (100 * shownDone) / scopeN : 0}%` }} /></div>
      <div className="kpis">
        <Kpi label="Progress" value={pctOf(shownDone, scopeN)} accent />
        <Kpi label="Scope (strategies = cells)" value={fmt(scopeN)} sub={job.families ? `${job.families.length} families` : "all families"} />
        <Kpi label="Completed" value={fmt(shownDone)} sub={`${fmt(L.counts?.completed_this_run ?? 0)} in this run · ${fmt(L.counts?.skipped_completed ?? 0)} skipped (done before)`} />
        <Kpi label="Remaining" value={fmt(Math.max(0, scopeN - shownDone))} />
        <Kpi label="Errors" value={fmt(L.counts?.failed_this_run ?? 0)} tone={(L.counts?.failed_this_run ?? 0) ? "neg" : ""} sub="never counted as trials; retried on resume" />
        <Kpi label="Elapsed" value={elapsed(job.started_at, job.finished_at)} />
      </div>
      {L.current && !final && <p className="small" data-testid="live-current">Now evaluating <Mono>{L.current.strategy_id}</Mono> · family <b>{L.current.family_id}</b> ·
        {" "}{L.current.timeframe} on <Mono>{L.current.dataset_id}</Mono> ({fmt(L.current.index)} of {fmt(L.current.of)} in this scope)</p>}
      <p className="small muted">Campaign <Mono>{job.campaign_id}</Mono> · search <Mono>{job.search_id}</Mono> · protocol <Mono>{L.protocol_id ?? "…"}</Mono>
        {L.protocol_version != null && ` v${L.protocol_version}`} · workers 1 · holdout locked · results are stored as they finish.</p>
      {job.error && <Banner tone="error">{job.error}</Banner>}
      {!!L.errors?.length && <details><summary>{L.errors.length} error(s)</summary><ul className="small">
        {L.errors.map((e, i) => <li key={i}>{e.strategy_id && <Mono>{e.strategy_id}</Mono>} {e.check ?? ""} {e.error ?? JSON.stringify(e.detail ?? "")}</li>)}</ul></details>}
    </Card>
  );
}

function RunHistory({ runs }: { runs: CampaignRunRecord[] }) {
  return (
    <Card title={`Run history (${runs.length})`} testId="run-history">
      {!runs.length ? <Empty>No run yet.</Empty> : (
        <TableWrap><table>
          <thead><tr><th>Run</th><th>Created</th><th>Status</th><th>Scope</th><th className="num">Strategies</th>
            <th className="num">Completed in run</th><th className="num">Skipped</th><th className="num">Errors</th><th>Duration</th><th>Source</th></tr></thead>
          <tbody>{runs.map((x) => (
            <tr key={x.run_record_id}>
              <td><Mono>{x.run_record_id}</Mono></td><td>{shortTime(x.created_at)}</td>
              <td><Badge tone={statusTone(x.status)}>{x.status}</Badge>{x.status === "interrupted" && <span className="small muted"> resumable</span>}</td>
              <td className="small">{x.all_families ? "all families" : x.families.join(", ")}</td>
              <td className="num">{fmt(x.n_scope)}</td><td className="num">{fmt(x.counts.completed_this_run)}</td>
              <td className="num">{fmt(x.counts.skipped_completed)}</td><td className="num">{fmt(x.counts.failed_this_run)}</td>
              <td>{elapsed(x.started_at ?? x.created_at, x.finished_at ?? x.updated_at)}</td><td>{x.source}</td>
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
        <h1>{fid}</h1><p className="subtitle">{d.n} strategies · {d.note}</p></div></header>
      <TableWrap testId="family-strategies"><table>
        <thead><tr><th>Strategy</th><th>TF</th><th>Status</th><th className="num">Trades</th><th className="num">Net R</th>
          <th className="num">Expectancy R</th><th className="num">PF</th><th className="num">Max DD R</th><th>Sample</th><th></th></tr></thead>
        <tbody>{d.strategies.map((s) => (
          <tr key={s.strategy_id}>
            <td><a href={href(`/runs/${cid}/${fid}/${s.strategy_id}`)}><Mono>{s.strategy_id}</Mono></a></td><td>{s.timeframe}</td>
            <td><Badge tone={statusTone(s.status)}>{s.status}</Badge>{s.error && <span className="small muted" title={s.error}> error</span>}</td>
            <td className="num">{fmt(s.headline.trade_count)}</td><td className="num">{r(s.headline.net_r)}</td>
            <td className="num">{r(s.headline.expectancy_r)}</td><td className="num">{n(s.headline.profit_factor)}</td>
            <td className="num">{r(s.headline.max_drawdown_r)}</td><td className="small">{fmt(s.headline.sample_label)}</td>
            <td className="row-actions">{s.run_id && <a href={href(`/results/${s.run_id}`)}>Full report ›</a>}</td>
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
  return (
    <div className="page" data-testid="strategy-result">
      <header className="page-head"><div><div className="eyebrow"><a href={href(`/runs/${cid}`)}><Mono>{cid}</Mono></a> › <a href={href(`/runs/${cid}/${fid}`)}>{fid}</a></div>
        <h1><Mono>{sid}</Mono> <Badge tone={statusTone(d.status)}>{d.status}</Badge></h1>
        <p className="subtitle">Stored result — nothing is re-run. Historical result under the campaign's stated assumptions.</p></div>
        <div className="actions">{d.run_id && <a className="btn btn-primary" href={href(`/results/${d.run_id}`)}>Full run report</a>}
          <a className="btn" href={href(`/strategies/${sid}`)}>Strategy definition</a></div></header>
      {d.error && <Banner tone="error">{d.error}</Banner>}
      <Card title="Provenance"><KeyValues rows={Object.entries(d.provenance).map(([k, v]) => [k, typeof v === "object" ? <Mono>{JSON.stringify(v)}</Mono> : <Mono>{String(v)}</Mono>])} /></Card>
      {d.prop && <Card title="Prop audit (downstream of the base result)" testId="strategy-prop">
        <TableWrap><table><thead><tr><th>Profile</th><th>Status</th><th>Result</th></tr></thead>
          <tbody>{d.prop.profiles.map((p) => <tr key={p.profile.profile_id}><td>{p.profile.profile_id} v{p.profile.version}</td>
            <td><Badge tone={p.status === "PASS" ? "ok" : p.status === "NOT_APPLICABLE" ? "neutral" : "warn"}>{p.status}</Badge></td>
            <td className="small">{p.final_status}</td></tr>)}</tbody></table></TableWrap></Card>}
    </div>
  );
}
