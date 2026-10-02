import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { FlipJob, FlipResults, FlipRow, FlipSelection, FlipView } from "../api/types";
import { href, useRoute } from "../app/router";
import { useApi, useApp } from "../app/context";
import { facetLabel, humanize, plainProse } from "../app/labels";
import { useCriteriaName } from "../components/results";
import { OUTCOME_LABEL } from "./Holdout";
import { Badge, Banner, Button, Card, Confirm, Empty, ErrorPanel, Field, Kpi, Loading, Mono, NumberInput, TableWrap, TechDetails, n, signCls } from "../components/ui";

/** ADR-87 Run backtest → Flip scan: the worst discovery results that lose clearly BEFORE costs are fully mirrored (every
 *  trade on the other side, stop and target swapped) and backtested again as new strategies under their own flip protocol.
 *  Nothing here negates a stored result: every flipped number is a new backtest through the same engine. */
const FINAL = new Set(["completed", "failed", "cancelled"]);
const yesNo = (v: boolean | null | undefined) => (v == null ? <span className="muted">—</span> : v ? <Badge tone="ok">Yes</Badge> : <span className="muted">No</span>);

export function FlipsPage() {
  const route = useRoute();
  const [cap, setCap] = useState<number | undefined>(undefined);
  const [asked, setAsked] = useState<number | undefined>(undefined);
  const view = useApi<FlipView>(`/api/flips${asked ? `?cap=${asked}` : ""}`, [asked]);
  const [jobId, setJobId] = useState<string | null>(route.query.get("job"));
  useEffect(() => {                                   // a flip job started earlier in this app session: show it
    if (jobId) return;
    api.get<{ job: FlipJob | null }>("/api/campaigns/active-job").then((d) => {
      if (d.job && d.job.kind === "flip" && !FINAL.has(d.job.state)) setJobId(d.job.job_id);
    }).catch(() => undefined);
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {                                   // refresh the table while a job runs
    if (!jobId) return;
    const t = window.setInterval(() => view.reload(), 6000);
    return () => window.clearInterval(t);
  }, [jobId]);  // eslint-disable-line react-hooks/exhaustive-deps
  const v = view.data;
  const noProtocol = v?.state === "no_protocol";
  return (
    <div className="page" data-testid="flips-page">
      <header className="page-head"><div><h1>Flip scan</h1></div></header>
      <Banner tone="info">The flip scan takes the strategies that lost <b>clearly before costs</b> in your research runs and tests the
        exact opposite: every trade on the other side, with the old profit target as the new stop and the old stop as the new target.
        The flipped strategies are new strategies with their own backtests (costs, spread and prop rules applied again), their own
        holdout tests, and a stricter significance bar that counts every strategy they were picked from. Picking the worst of many and
        flipping it is how luck gets mistaken for an edge, so treat a flipped survivor like any other until the holdout says otherwise.</Banner>
      {noProtocol ? <Empty>There is no active research protocol yet. The flip scan reads the results of your research runs
        (Run backtest → Research runs).</Empty>
        : view.error ? <ErrorPanel error={view.error} />
        : !v ? <Loading label="Scanning the research results…" kind="table" />
        : v.state === "preview" && v.selection ? <Preview v={v} sel={v.selection} cap={cap} setCap={setCap}
            onRefresh={() => setAsked(cap)} onCreated={() => { setAsked(undefined); view.reload(); }} busy={view.loading} />
        : v.flip ? <Created v={v} flip={v.flip} jobId={jobId} setJobId={setJobId} reload={view.reload} />
        : <Empty>Nothing to show.</Empty>}
    </div>
  );
}

function Preview({ v, sel, cap, setCap, onRefresh, onCreated, busy }: {
  v: FlipView; sel: FlipSelection; cap: number | undefined; setCap: (x: number | undefined) => void; onRefresh: () => void;
  onCreated: () => void; busy: boolean;
}) {
  const { toast } = useApp();
  const [ask, setAsk] = useState(false);
  const [creating, setCreating] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const create = () => {
    setCreating(true); setErr(null);
    api.post<{ n_flips: number }>("/api/flips", { cap: sel.cap })
      .then((r) => { setAsk(false); toast("info", `Flip scan created: ${r.n_flips} flipped strategies saved`); onCreated(); })
      .catch((e: ApiError) => { setErr(e); setAsk(false); }).finally(() => setCreating(false));
  };
  const skipped = Object.entries(sel.skipped);
  return <>
    <div className="kpis">
      <Kpi label="Research results scanned" value={sel.n_discovery_results.toLocaleString()} sub={`${sel.n_with_enough_trades.toLocaleString()} with ${v.min_trades}+ trades`} />
      <Kpi label="Lose clearly before costs" value={sel.n_clearly_negative.toLocaleString()} sub="95% sure the average trade loses before costs" />
      <Kpi label="Will be flipped" value={sel.n_selected} accent testId="flips-selected" sub={`scan size ${sel.cap}`} />
      <Kpi label="Significance family" value={(v.family_size ?? 0).toLocaleString()} sub={`${v.parent.trial_budget.toLocaleString()} research trials + ${sel.n_selected} flips`} />
    </div>
    <Card title="What the scan would flip, worst first" testId="flips-preview"
      actions={<Button kind="primary" onClick={() => setAsk(true)} disabled={!sel.n_selected || busy} testId="flips-create">
        Create flip scan ({sel.n_selected})</Button>}>
      <div className="inline">
        <Field label="Scan size (most flips)" hint={`1 to ${v.max_cap}; default ${v.default_cap}`}>
          <NumberInput value={cap ?? sel.cap} onChange={setCap} integer ariaLabel="scan size" testId="flips-cap" />
        </Field>
        <Button onClick={onRefresh} disabled={busy || !cap || cap === sel.cap} busy={busy} busyLabel="Scanning…">Update list</Button>
      </div>
      {!sel.rows.length ? <Empty>No research result loses clearly before costs (at least {v.min_trades} trades and 95% sure the average
        trade loses before costs). A strategy that only loses after costs would lose again when flipped, because the flip pays the same costs.</Empty> : (
        <TableWrap className="fit"><table className="dense fit-table">
          <thead><tr><th>Strategy (original)</th><th>Family</th><th>Time­frame</th><th className="r">Trades</th>
            <th className="r" title="average R per trade before costs">Before-cost R / trade</th>
            <th className="r" title="95% upper bound of the before-cost R per trade">95% upper bound</th>
            <th className="r">Net R / trade</th><th>Flip</th></tr></thead>
          <tbody>{sel.rows.map((r) => (
            <tr key={r.strategy_id} className={r.mirror ? "" : "faint-row"} data-testid={`frow-${r.strategy_id}`}>
              <td className="name-cell"><div className="cell-title">{r.display_name ?? "Unnamed strategy"}</div>
                {r.synthetic && <Badge tone="demo">synthetic</Badge>}</td>
              <td className="small wrap">{r.family_name ?? facetLabel("family_id", r.family_id)}</td>
              <td>{facetLabel("timeframe", r.timeframe ?? null)}</td>
              <td className="r num">{r.trades}</td>
              <td className={`r num ${signCls(r.gross_r_per_trade)}`}>{n(r.gross_r_per_trade, 3)}</td>
              <td className={`r num ${signCls(r.gross_upper_bound)}`}>{n(r.gross_upper_bound, 3)}</td>
              <td className={`r num ${signCls(r.net_r_per_trade)}`}>{n(r.net_r_per_trade, 3)}</td>
              <td className="small">{r.mirror ? <Badge tone="info">Will be flipped</Badge>
                : r.skip_label ? <span className="muted">Skipped: {r.skip_label}</span>
                : <span className="muted">Not examined (scan size reached)</span>}</td>
            </tr>))}</tbody></table></TableWrap>)}
      {skipped.length > 0 && <p className="small muted">Skipped: {skipped.map(([, x]) => `${x.count} ${x.label}`).join("; ")}.</p>}
      <p className="small muted">{plainProse(sel.rule)}</p>
      {err && <ErrorPanel error={err} title="Not created" testId="flips-error" />}
    </Card>
    <Confirm open={ask} title={`Create the flip scan with ${sel.n_selected} flipped strateg${sel.n_selected === 1 ? "y" : "ies"}?`}
      confirmLabel="Create flip scan" busy={creating} onConfirm={create} onCancel={() => setAsk(false)}>
      This creates the flip protocol for your research protocol. It is <b>permanent and happens once</b>: exactly these {sel.n_selected} flipped
      strategies are registered (each one backtest on the research dates, no holdout dates), with their own {v.default_holdout_looks} holdout
      tests. Their significance bar counts all {(v.family_size ?? 0).toLocaleString()} strategies, because the flips were picked from your
      research results. Nothing is backtested yet: you start that next.
    </Confirm>
  </>;
}

function Created({ v, flip, jobId, setJobId, reload }: { v: FlipView; flip: FlipResults; jobId: string | null; setJobId: (j: string | null) => void; reload: () => void }) {
  const { toast } = useApp();
  const crit = useCriteriaName();
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const c = flip.counts;
  const start = () => {
    setStarting(true); setErr(null);
    api.post<FlipJob>("/api/flips/jobs", {})
      .then((j) => { setJobId(j.job_id); toast("info", "Backtesting the flipped strategies"); })
      .catch((e: ApiError) => setErr(e)).finally(() => setStarting(false));
  };
  return <>
    <div className="kpis">
      <Kpi label="Flipped strategies" value={c.flips} sub={`backtested ${c.completed} of ${c.flips}`} meter={c.completed / Math.max(1, c.flips)} accent testId="flips-done" />
      <Kpi label="Net positive" value={c.net_positive} sub="after costs, research dates" />
      <Kpi label="Pass eval / payout" value={`${c.pass_eval} / ${c.payout}`} sub={crit} />
      <Kpi label="Survivors" value={c.survivors} sub="net > 0 and a payout" testId="flips-survivors" />
      <Kpi label="Flip holdout tests left" value={`${flip.holdout.looks_left} of ${flip.holdout.looks_budget}`} sub="separate from your research holdout tests" />
    </div>
    {flip.status !== "ACTIVE" && <Banner tone="warn">This flip protocol is retired (its research protocol was retired). Its results stay
      readable; nothing more can be backtested or holdout-tested under it.</Banner>}
    {jobId && <LiveFlip jobId={jobId} onFinished={reload} onClose={() => setJobId(null)} />}
    <Card title="Flipped strategies beside their originals" testId="flips-results"
      actions={c.remaining > 0 && flip.status === "ACTIVE" ? <Button kind="primary" onClick={start} busy={starting} disabled={!!jobId} testId="flips-start">
        {c.completed ? `Backtest the remaining ${c.remaining}` : `Backtest the ${c.flips} flipped strategies`}</Button> : undefined}>
      {err && <ErrorPanel error={err} title="Not started" testId="flips-start-error" />}
      <TableWrap className="fit"><table className="dense fit-table">
        <thead><tr><th>Original</th><th className="r">Net R / trade</th><th>Flipped</th><th>Status</th><th className="r">Trades</th>
          <th className="r">Before-cost R / trade</th><th className="r">Net R / trade</th><th className="r">Profit factor</th>
          <th>Pass eval</th><th>Payout</th><th>Holdout</th></tr></thead>
        <tbody>{flip.rows.map((r) => <FlipTr key={r.flip.strategy_id} r={r} />)}</tbody>
      </table></TableWrap>
      <p className="small muted">Results are backtests on the research dates under stated assumptions, never a forecast. Survivors of the flip
        scan appear in Run backtest → <a href={href("/holdout")}>Holdout backtest</a>, marked “Flipped”, and use this flip protocol's own
        holdout tests. Pass flags under {crit} (Settings).</p>
    </Card>
    <TechDetails rows={[["Flip protocol id", <Mono>{flip.protocol_id}</Mono>], ["Research protocol id", <Mono>{v.parent.protocol_id}</Mono>],
      ["Trials used", `${flip.trials.used} of ${flip.trials.budget}`], ["Significance family", flip.family_size.toLocaleString()],
      ["Per-test alpha", flip.per_test_alpha.toExponential(2)], ["Selection rule", plainProse(String(flip.selection.rule))]]} />
  </>;
}

function FlipTr({ r }: { r: FlipRow }) {
  const f = r.flip, o = r.original;
  return (
    <tr data-testid={`flip-${f.strategy_id}`} className={f.status === "completed" ? "" : "faint-row"}>
      <td className="name-cell"><div className="cell-title">{o.display_name ?? "Unnamed strategy"}</div>
        <div className="small muted">{facetLabel("timeframe", o.timeframe)}</div></td>
      <td className={`r num ${signCls(o.net_r_per_trade)}`}>{n(o.net_r_per_trade, 3)}</td>
      <td className="name-cell"><a href={href(`/explorer?open=${f.strategy_id}`)}>{f.display_name ?? "Flipped strategy"}</a>
        <div className="cell-badges">{f.survivor && <Badge tone="ok">Survivor</Badge>}{f.synthetic && <Badge tone="demo">synthetic</Badge>}</div></td>
      <td className="small">{f.status === "failed" ? <span title={f.error ?? ""}>Failed</span> : f.status === "not_started" ? <span className="muted">Not yet</span> : humanize(f.status)}</td>
      <td className="r num">{f.trades ?? "—"}</td>
      <td className={`r num ${signCls(f.gross_r_per_trade)}`}>{n(f.gross_r_per_trade, 3)}</td>
      <td className={`r num ${signCls(f.net_r_per_trade)}`}>{n(f.net_r_per_trade, 3)}</td>
      <td className="r num">{n(f.profit_factor)}</td>
      <td>{yesNo(f.prop_pass_eval)}</td><td>{yesNo(f.prop_pass_payout)}</td>
      <td className="small">{f.holdout?.outcome ? <Badge tone={f.holdout.outcome === "HOLDOUT_CRITERIA_MET" ? "ok" : "warn"}>{OUTCOME_LABEL[f.holdout.outcome] ?? humanize(f.holdout.outcome)}</Badge>
        : f.holdout ? humanize(f.holdout.status) : <span className="muted">—</span>}</td>
    </tr>
  );
}

function LiveFlip({ jobId, onFinished, onClose }: { jobId: string; onFinished: () => void; onClose: () => void }) {
  const [job, setJob] = useState<FlipJob | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [cancelling, setCancelling] = useState(false);
  useEffect(() => {
    let live = true, t = 0;
    const poll = () => api.get<FlipJob>(`/api/flips/jobs/${jobId}`).then((j) => {
      if (!live) return;
      setJob(j); setErr(null);
      if (FINAL.has(j.state)) onFinished(); else t = window.setTimeout(poll, 2000);
    }).catch((e) => { if (live) setErr(e as ApiError); });
    poll();
    return () => { live = false; window.clearTimeout(t); };
  }, [jobId]);  // eslint-disable-line react-hooks/exhaustive-deps
  if (err && !job) return <Card title="Flip scan backtests"><ErrorPanel error={err} title="This job is not known to this app session" />
    <Button small onClick={onClose}>Close</Button></Card>;
  if (!job) return <Card title="Flip scan backtests"><Loading label="Connecting…" /></Card>;
  const final = FINAL.has(job.state);
  const L = job.live;
  return (
    <Card testId="flips-live" title={<>Flip scan backtests <Badge tone={job.state === "completed" ? "ok" : job.state === "failed" ? "error" : "info"}>{humanize(job.state)}</Badge></>}
      actions={final ? <Button small onClick={onClose}>Close</Button>
        : <Button small kind="danger" busy={cancelling} busyLabel="Stopping…" testId="flips-cancel"
            onClick={async () => { setCancelling(true); try { setJob(await api.post<FlipJob>(`/api/flips/jobs/${jobId}/cancel`, {})); } finally { setCancelling(false); } }}>
            Stop (backtests already running finish)</Button>}>
      <div className="progress" aria-label="flip progress"><span style={{ width: `${(100 * L.done) / Math.max(1, L.total)}%` }} /></div>
      <p className="small">{L.done} of {L.total} done · {L.completed} backtested{L.skipped ? ` · ${L.skipped} already done` : ""}{L.failed ? ` · ${L.failed} failed` : ""}
        {!final && L.current ? <> · now <b>{L.current}</b></> : null} · {job.processes} CPU core{job.processes === 1 ? "" : "s"}</p>
      {job.error && <Banner tone="error">{job.error}</Banner>}
      <TechDetails rows={[["Job id", <Mono>{job.job_id}</Mono>], ["Search id", <Mono>{job.search_id}</Mono>]]} />
    </Card>
  );
}
