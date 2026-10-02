import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api/client";
import type { HoldoutCandidate, HoldoutCandidates, HoldoutHistoryRow, HoldoutJob } from "../api/types";
import { href, useRoute } from "../app/router";
import { useApi, useApp } from "../app/context";
import { facetLabel, humanize, plainProse } from "../app/labels";
import { useCriteriaName } from "../components/results";
import { Badge, Banner, Button, Card, Confirm, Empty, ErrorPanel, Kpi, Loading, Mono, TableWrap, TechDetails, n, shortTime, signCls } from "../components/ui";

/** ADR-85 Run backtest → Holdout backtest: pick SURVIVORS only and test them once each on the locked holdout through the
 *  protocol gate (random-entry comparison, cost stress, pre-registered criteria). The list order is a display ranking
 *  from discovery numbers only; nothing here changes a strategy, a result or the gate's rules. */
export const OUTCOME_LABEL: Record<string, string> = { HOLDOUT_CRITERIA_MET: "Criteria met", HOLDOUT_CRITERIA_NOT_MET: "Criteria not met" };
const FINAL = new Set(["completed", "failed", "cancelled"]);
type SortKey = "position" | "trades_per_week" | "negative_months" | "avg_rr" | "profit_factor" | "max_drawdown_r" | "expectancy_r" | "net_r" | "max_loss_streak" | "display_name" | "timeframe";
const LOWER_FIRST: Record<string, boolean> = { position: true, negative_months: true, max_drawdown_r: true, max_loss_streak: true, display_name: true, timeframe: true };

export function HoldoutPage() {
  const { toast } = useApp();
  const route = useRoute();
  const crit = useCriteriaName();
  const cands = useApi<HoldoutCandidates>("/api/holdout/candidates");
  const hist = useApi<HoldoutHistoryRow[]>("/api/holdout/history");
  const [jobId, setJobId] = useState<string | null>(route.query.get("job"));
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [fam, setFam] = useState("");
  const [tf, setTf] = useState("");
  const [sort, setSort] = useState<{ k: SortKey; desc: boolean }>({ k: "position", desc: false });
  const [ask, setAsk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => {                                   // a holdout job started earlier in this app session: show it
    if (jobId) return;
    api.get<{ job: HoldoutJob | null }>("/api/campaigns/active-job").then((d) => {
      if (d.job && d.job.kind === "holdout" && !FINAL.has(d.job.state)) setJobId(d.job.job_id);
    }).catch(() => undefined);
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  const rows = cands.data?.rows ?? [];
  const p = cands.data?.protocol ?? null;
  const fams = useMemo(() => [...new Map(rows.map((r) => [r.family_id ?? "", r.family_name ?? facetLabel("family_id", r.family_id)])).entries()], [rows]);
  const tfs = useMemo(() => [...new Set(rows.map((r) => r.timeframe ?? ""))].filter(Boolean), [rows]);
  const shown = useMemo(() => {
    const v = rows.filter((r) => (!fam || r.family_id === fam) && (!tf || r.timeframe === tf));
    const key = (r: HoldoutCandidate) => sort.k === "max_drawdown_r" ? Math.abs(r.max_drawdown_r ?? NaN) : r[sort.k];
    return [...v].sort((a, b) => {
      const x = key(a), y = key(b);
      if (x == null || (typeof x === "number" && isNaN(x))) return 1;
      if (y == null || (typeof y === "number" && isNaN(y))) return -1;
      const c = typeof x === "string" ? x.localeCompare(String(y)) : (x as number) - (y as number);
      return sort.desc ? -c : c;
    });
  }, [rows, fam, tf, sort]);
  const left = p?.looks_left ?? 0;
  const running = !!jobId;
  const toggle = (sid: string, on: boolean) => setSel((s) => { const nx = new Set(s); if (on) nx.add(sid); else nx.delete(sid); return nx; });
  const start = () => {
    setBusy(true); setErr(null);
    api.post<HoldoutJob>("/api/holdout/jobs", { strategy_ids: [...sel] })
      .then((j) => { setJobId(j.job_id); setSel(new Set()); setAsk(false); toast("info", `Holdout backtest started (${j.n_items} strateg${j.n_items === 1 ? "y" : "ies"})`); })
      .catch((e: ApiError) => { setErr(e); setAsk(false); }).finally(() => setBusy(false));
  };
  const onSort = (k: SortKey) => setSort((s) => (s.k === k ? { k, desc: !s.desc } : { k, desc: !LOWER_FIRST[k] }));
  const th = (k: SortKey, label: string, right = true, title?: string) => (
    <th className={`sortable${right ? " r" : ""}`} title={title} onClick={() => onSort(k)} data-testid={`hsort-${k}`}>
      {label}{sort.k === k ? (sort.desc ? " ▾" : " ▴") : ""}</th>);
  return (
    <div className="page" data-testid="holdout-page">
      <header className="page-head"><div><h1>Holdout backtest</h1></div></header>
      <Banner tone="info">The holdout is the locked final part of your data that research runs never touch. Each strategy can be tested on it
        <b> once</b>, within your protocol's limit, and a used test never comes back. A test is a backtest on exactly the holdout dates plus
        100 random-entry comparisons and a cost stress test, judged by the rules your protocol fixed in advance: “criteria met” or “not met”,
        never “approved”. Only survivors can be tested.</Banner>
      {cands.error ? <ErrorPanel error={cands.error} /> : !cands.data ? <Loading label="Loading survivors…" kind="table" /> : <>
        <div className="kpis">
          <Kpi label="Holdout tests left" value={p ? `${p.looks_left} of ${p.looks_budget}` : "—"} accent testId="holdout-left"
            meter={p ? p.looks_used / Math.max(1, p.looks_budget) : null} sub={p ? `${p.looks_used} used` : "no active research protocol"} />
          <Kpi label="Holdout dates" value={p ? p.holdout_trading_dates[0] : "—"} sub={p ? `to ${p.holdout_trading_dates[1]} · locked for research runs` : undefined} />
          <Kpi label="Survivors" value={cands.data.n_survivors} sub={`${cands.data.n_eligible} can be tested`} />
          <Kpi label="Selected" value={sel.size} />
        </div>
        {cands.data.protocols.length > 1 && <Banner tone="warn">Survivors come from more than one research protocol; select survivors of one
          protocol at a time.</Banner>}
        {jobId && <LiveHoldout jobId={jobId} onFinished={() => { cands.reload(); hist.reload(); }} onClose={() => setJobId(null)} />}
        <Card title="Survivors, best to worst for prop trading" testId="holdout-candidates"
          actions={<Button kind="primary" onClick={() => setAsk(true)} disabled={!sel.size || running || sel.size > left} testId="holdout-start">
            Start holdout backtest{sel.size ? ` (${sel.size})` : ""}</Button>}>
          <div className="inline">
            <select className="input" value={fam} aria-label="family" onChange={(e: { target: HTMLSelectElement }) => setFam(e.target.value)}>
              <option value="">Family: all</option>{fams.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select>
            <select className="input" value={tf} aria-label="timeframe" onChange={(e: { target: HTMLSelectElement }) => setTf(e.target.value)}>
              <option value="">Timeframe: all</option>{tfs.map((x) => <option key={x} value={x}>{facetLabel("timeframe", x)}</option>)}</select>
            <span className="small muted">{sel.size} selected{sel.size > left ? ` · only ${left} test${left === 1 ? "" : "s"} left` : ""}</span>
          </div>
          {!rows.length ? <Empty>No survivors yet under {crit}. Survivors come from research runs (Run backtest → Research runs).</Empty> : (
            <TableWrap className="fit"><table className="dense fit-table">
              <thead><tr><th style={{ width: 26 }} />{th("position", "#", false, "overall rank (lower is better)")}{th("display_name", "Strategy", false)}
                <th>Family</th>{th("timeframe", "Time­frame", false)}
                {th("trades_per_week", "Trades / week")}{th("negative_months", "Negative months", true, "counts double")}
                {th("avg_rr", "Reward to risk")}{th("profit_factor", "Profit factor")}{th("max_drawdown_r", "Max draw­down (R)", true, "counts double")}
                {th("expectancy_r", "Net R per trade")}{th("net_r", "Total net R")}{th("max_loss_streak", "Longest losing streak")}
                <th>Holdout</th></tr></thead>
              <tbody>{shown.map((r) => (
                <tr key={r.strategy_id} className={r.eligible ? "" : "faint-row"} data-testid={`hrow-${r.strategy_id}`}>
                  <td><input type="checkbox" checked={sel.has(r.strategy_id)} disabled={!r.eligible || running} aria-label={`select ${r.display_name}`}
                    data-testid={`hpick-${r.strategy_id}`} onChange={(e: { target: HTMLInputElement }) => toggle(r.strategy_id, e.target.checked)} /></td>
                  <td className="num">{r.position}</td>
                  <td className="name-cell"><div className="cell-title">{r.display_name ?? "Unnamed strategy"}</div>
                    {r.synthetic && <Badge tone="demo">synthetic</Badge>}</td>
                  <td className="small wrap">{r.family_name ?? facetLabel("family_id", r.family_id)}</td><td>{facetLabel("timeframe", r.timeframe)}</td>
                  <td className="r num">{n(r.trades_per_week, 1)}</td><td className="r num">{r.negative_months ?? "—"}</td>
                  <td className="r num">{n(r.avg_rr, 2)}</td><td className="r num">{n(r.profit_factor)}</td>
                  <td className="r num">{n(r.max_drawdown_r, 1)}</td>
                  <td className={`r num ${signCls(r.expectancy_r)}`}>{n(r.expectancy_r, 3)}</td><td className={`r num ${signCls(r.net_r)}`}>{n(r.net_r, 1)}</td>
                  <td className="r num">{r.max_loss_streak ?? "—"}</td>
                  <td className="small">{r.tested ? <Badge tone={r.tested.outcome === "HOLDOUT_CRITERIA_MET" ? "ok" : r.tested.outcome ? "warn" : "neutral"}>
                    {r.tested.outcome ? OUTCOME_LABEL[r.tested.outcome] ?? humanize(r.tested.outcome) : humanize(r.tested.status)}</Badge>
                    : r.eligible ? <span className="muted">not tested</span> : <span className="muted" title={r.reason ?? ""}>{plainProse(r.reason ?? "")}</span>}</td>
                </tr>))}
              </tbody></table></TableWrap>)}
          <p className="small muted">{cands.data.ranking_rule} Survivors under {crit} (Settings → Prop firm pass criteria). Click a column to sort by it.</p>
          {err && <ErrorPanel error={err} title="Not started" testId="holdout-error" />}
        </Card>
        <HoldoutHistory rows={hist.data} error={hist.error} />
      </>}
      <Confirm open={ask} title={`Use ${sel.size} of your ${left} holdout test${left === 1 ? "" : "s"}?`} confirmLabel="Start holdout backtest" busy={busy}
        onConfirm={start} onCancel={() => setAsk(false)}>
        Each selected strategy is tested once on the locked holdout dates. This is permanent: the tests are used up, a strategy can never be
        holdout-tested again, and the result is recorded whatever it is. Strategies are shortlisted from the research run that tested them.
      </Confirm>
    </div>
  );
}

function LiveHoldout({ jobId, onFinished, onClose }: { jobId: string; onFinished: () => void; onClose: () => void }) {
  const [job, setJob] = useState<HoldoutJob | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [cancelling, setCancelling] = useState(false);
  useEffect(() => {
    let live = true, t = 0;
    const poll = () => api.get<HoldoutJob>(`/api/holdout/jobs/${jobId}`).then((j) => {
      if (!live) return;
      setJob(j); setErr(null);
      if (FINAL.has(j.state)) onFinished(); else t = window.setTimeout(poll, 2000);
    }).catch((e) => { if (live) setErr(e as ApiError); });
    poll();
    return () => { live = false; window.clearTimeout(t); };
  }, [jobId]);  // eslint-disable-line react-hooks/exhaustive-deps
  if (err && !job) return <Card title="Holdout backtest"><ErrorPanel error={err} title="This job is not known to this app session" />
    <Button small onClick={onClose}>Close</Button></Card>;
  if (!job) return <Card title="Holdout backtest"><Loading label="Connecting…" /></Card>;
  const final = FINAL.has(job.state);
  const done = job.items.filter((x) => ["completed", "failed", "refused"].includes(x.state)).length;
  return (
    <Card testId="holdout-live" title={<>Holdout backtest <Badge tone={job.state === "completed" ? "ok" : job.state === "failed" ? "error" : "info"}>{humanize(job.state)}</Badge></>}
      actions={final ? <Button small onClick={onClose}>Close</Button>
        : <Button small kind="danger" busy={cancelling} busyLabel="Stopping after the current strategy…" testId="holdout-cancel"
            onClick={async () => { setCancelling(true); try { setJob(await api.post<HoldoutJob>(`/api/holdout/jobs/${jobId}/cancel`, {})); } finally { setCancelling(false); } }}>
            Cancel (the strategy being tested finishes)</Button>}>
      <div className="progress" aria-label="holdout progress"><span style={{ width: `${(100 * done) / Math.max(1, job.n_items)}%` }} /></div>
      {!final && job.live.current && <p className="small">Now testing <b>{job.live.current}</b> ({done + 1} of {job.n_items}): backtest on the holdout dates,
        100 random-entry comparisons, cost stress and the verdict. This can take several minutes per strategy.</p>}
      <TableWrap><table className="dense"><thead><tr><th>Strategy</th><th>Status</th><th>Result</th></tr></thead>
        <tbody>{job.items.map((x) => <tr key={x.strategy_id}><td>{x.display_name ?? "Unnamed strategy"}</td><td>{humanize(x.state)}</td>
          <td className="small">{x.outcome ? <Badge tone={x.outcome === "HOLDOUT_CRITERIA_MET" ? "ok" : "warn"}>{OUTCOME_LABEL[x.outcome] ?? humanize(x.outcome)}</Badge>
            : x.error ? plainProse(x.error) : "—"}{x.run_id && <> · <a href={href(`/holdout-results?open=${x.strategy_id}`)}>see result ›</a></>}</td></tr>)}</tbody>
      </table></TableWrap>
      {job.error && <Banner tone="error">{job.error}</Banner>}
      <TechDetails rows={[["Job id", <Mono>{job.job_id}</Mono>], ["Protocol id", <Mono>{job.protocol_id}</Mono>]]} />
    </Card>
  );
}

function HoldoutHistory({ rows, error }: { rows: HoldoutHistoryRow[] | null; error: ApiError | null }) {
  return (
    <Card title="Holdout run history" testId="holdout-history">
      {error ? <ErrorPanel error={error} /> : !rows ? <Loading label="Loading…" /> : !rows.length ? <Empty>No holdout tests yet.</Empty> : (
        <TableWrap><table className="dense"><thead><tr><th>When</th><th>Strategy</th><th>Status</th><th>Result</th><th className="r">Trades</th><th /></tr></thead>
          <tbody>{rows.map((h) => <tr key={h.access_id}><td className="small">{shortTime(h.created_at)}</td><td>{h.display_name ?? "Unnamed strategy"}</td>
            <td>{h.status === "refused" ? <span title={h.reason ?? ""}>Refused (no test used)</span> : humanize(h.status)}</td>
            <td>{h.outcome ? <Badge tone={h.outcome === "HOLDOUT_CRITERIA_MET" ? "ok" : "warn"}>{OUTCOME_LABEL[h.outcome] ?? humanize(h.outcome)}</Badge>
              : h.status === "refused" ? <span className="small muted">{plainProse(h.reason ?? "")}</span> : "—"}</td>
            <td className="r num">{h.trade_count ?? "—"}</td>
            <td>{h.run_id && <a href={href(`/holdout-results?open=${h.strategy_id}`)}>Open ›</a>}</td></tr>)}</tbody></table></TableWrap>)}
      <p className="small muted">Every attempt is recorded, including refused ones (a refusal never uses a test).</p>
    </Card>
  );
}
