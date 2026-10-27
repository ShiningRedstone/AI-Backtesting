/* ADR-116 Run backtest → Noise band: the user's noise band breakout as 10,000 frozen versions under their own protocol
   (own tries, own holdout tests; the research protocol's data, windows and costs). Create the set (design + library, no
   backtest), then backtest it with the normal research runner; results in Backtest results → "Noise band". */
import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import { href, useRoute } from "../app/router";
import { useApi, useApp } from "../app/context";
import { humanize } from "../app/labels";
import { Badge, Banner, Button, Card, Confirm, Empty, ErrorPanel, Kpi, Loading, Mono, TableWrap, TechDetails } from "../components/ui";

const FINAL = new Set(["completed", "failed", "cancelled"]);

interface NbJob {
  job_id: string; kind: string; state: string; error: string | null; processes?: number; search_id?: string;
  live: { phase?: string; current?: string | null; done?: number; total?: number; completed?: number; failed?: number; skipped?: number };
  result?: { count: number; by_timeframe: Record<string, number>; skipped_timeframes: string[] } | null;
}
interface NbView {
  name: string; ready: boolean; problem?: { code: string; message: string }; design_hash: string;
  design: { target_total: number; dimensions: Record<string, (string | number | boolean)[]>; fixed: string[]; sizing: Record<string, unknown> };
  parent?: { protocol_id: string; name: string | null }; discovery_period?: { start: string; end: string };
  datasets_by_timeframe?: Record<string, { dataset_id: string }>; dataset_problems?: { timeframe?: string; reason: string }[];
  set: null | { protocol_id: string; status: string; count: number; by_timeframe: Record<string, number>; trials_used: number;
    trial_budget: number; holdout_looks_used: number; holdout_looks: number };
  results?: { backtested: number; with_trades: number; net_positive: number; survivors: number };
}

const DIM_LABEL: Record<string, string> = {
  timeframe: "Candle timeframe", lookback: "Band: days averaged", mult: "Band: width multiplier", gap: "Band: with yesterday's close",
  trigger: "Entry trigger", entry_until: "Last entry (New York)", close_time: "Close time (New York)", direction: "Direction",
  stop: "Stop loss", target: "Take profit", trailing: "Trailing stop", max_trades: "Trades per day",
};
const VAL: Record<string, string> = {
  close_outside: "any close outside the band", fresh_break: "first close outside after being inside", session_end: "16:00 session end",
  both: "long and short", long: "long only", short: "short only", none: "none", band: "the band", opposite_band: "the opposite band",
  true: "yes", false: "no (9:30 open only)",
};
const word = (v: string | number | boolean) => {
  const s = String(v);
  if (VAL[s]) return VAL[s];
  const m = s.match(/^(atr|pts|rr)_(.+)$/);
  if (m) return m[1] === "atr" ? `${m[2]} × ATR` : m[1] === "pts" ? `${m[2]} points` : `1 : ${m[2]}`;
  return s;
};

export function NoiseBandPage() {
  const route = useRoute();
  const view = useApi<NbView>("/api/noise-band");
  const [jobId, setJobId] = useState<string | null>(route.query.get("job"));
  useEffect(() => {                                   // a job started earlier in this app session: show it
    if (jobId) return;
    api.get<{ job: NbJob | null }>("/api/campaigns/active-job").then((d) => {
      if (d.job && (d.job.kind === "noise_band" || (d.job.kind === "pool2" && (d.job as unknown as { action: string }).action === "noise_create"))
        && !FINAL.has(d.job.state)) setJobId(d.job.job_id);
    }).catch(() => undefined);
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!jobId) return;
    const t = window.setInterval(() => view.reload(), 8000);
    return () => window.clearInterval(t);
  }, [jobId]);  // eslint-disable-line react-hooks/exhaustive-deps
  const v = view.data;
  return (
    <div className="page" data-testid="noise-band-page">
      <header className="page-head"><div><h1>Noise band</h1></div></header>
      <Banner tone="info">Your noise band breakout, tested as <b>10,000 fixed versions</b>. Every version trades NQ only, starts at
        9:30 New York and only enters when a candle <b>closes outside the noise band</b> (above it long, below it short). What changes
        between versions: timeframe, band settings, last entry, close time, stop, target, trailing stop and 1–3 trades a day.
        The set has its <b>own</b> 10,000 tries and holdout tests; your research runs and their holdout tests are not touched.
        Testing 10,000 versions finds some that look good by luck; only the holdout tells.</Banner>
      {view.error ? <ErrorPanel error={view.error} />
        : !v ? <Loading label="Loading…" kind="table" />
        : !v.ready ? <Empty>{v.problem?.message ?? "Not available."} The noise band set uses your research protocol's data and dates
          (Run backtest → Research runs).</Empty>
        : <Body v={v} jobId={jobId} setJobId={setJobId} reload={view.reload} />}
    </div>
  );
}

function Body({ v, jobId, setJobId, reload }: { v: NbView; jobId: string | null; setJobId: (j: string | null) => void; reload: () => void }) {
  const { toast } = useApp();
  const [ask, setAsk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [running, setRunning] = useState(false);          // a job of this page is still working
  const [err, setErr] = useState<ApiError | null>(null);
  const s = v.set, res = v.results;
  const create = () => {
    setBusy(true); setErr(null);
    api.post<NbJob>("/api/noise-band", {}).then((j) => { setAsk(false); setJobId(j.job_id); toast("info", "Creating the 10,000 noise band versions"); })
      .catch((e: ApiError) => { setErr(e); setAsk(false); }).finally(() => setBusy(false));
  };
  const start = () => {
    setBusy(true); setErr(null);
    api.post<NbJob>("/api/noise-band/jobs", {}).then((j) => { setJobId(j.job_id); toast("info", "Backtesting the noise band set"); })
      .catch((e: ApiError) => setErr(e)).finally(() => setBusy(false));
  };
  const remaining = s ? s.count - s.trials_used : 0;
  const tfs = Object.keys(v.datasets_by_timeframe ?? {});
  return <>
    <div className="kpis">
      <Kpi label="Versions" value={(s?.count ?? v.design.target_total).toLocaleString()} accent
        sub={s ? Object.entries(s.by_timeframe).map(([k, x]) => `${k} ${x.toLocaleString()}`).join(" · ") : "not created yet"} />
      <Kpi label="Backtested" value={s ? s.trials_used.toLocaleString() : "0"} meter={s ? s.trials_used / Math.max(1, s.count) : 0}
        sub={s ? `of ${s.count.toLocaleString()} tries` : "—"} testId="nb-backtested" />
      <Kpi label="Net positive" value={res ? res.net_positive.toLocaleString() : "—"} sub="after costs, research dates" />
      <Kpi label="Survivors" value={res ? res.survivors.toLocaleString() : "—"} sub="net > 0 and a prop payout" testId="nb-survivors" />
      <Kpi label="Holdout tests left" value={s ? `${s.holdout_looks - s.holdout_looks_used} of ${s.holdout_looks}` : "—"} sub="this set's own" />
    </div>
    {s && s.status !== "ACTIVE" && <Banner tone="warn">This noise band set's protocol is retired (its research protocol was retired or
      replaced). Its results stay readable; nothing more can be backtested or holdout-tested under it.</Banner>}
    {err && <ErrorPanel error={err} title="Not started" testId="nb-error" />}
    {jobId && <LiveJob jobId={jobId} onFinished={reload} onClose={() => setJobId(null)} onRunning={setRunning} />}
    <Card title={s ? "Backtest the set" : "Create the set"} testId="nb-action" actions={
      !s ? <Button kind="primary" onClick={() => setAsk(true)} busy={busy} disabled={running || !tfs.length} testId="nb-create">Create the 10,000 versions</Button>
        : s.status === "ACTIVE" && remaining > 0 ? <Button kind="primary" onClick={start} busy={busy} disabled={running} testId="nb-start">
          {s.trials_used ? `Backtest the remaining ${remaining.toLocaleString()}` : `Backtest all ${s.count.toLocaleString()}`}</Button>
        : undefined}>
      {!s ? <p className="small">Creating designs the 10,000 versions and saves them to the strategy library (about a minute; no backtest yet).
        The list is frozen: it can never be changed or regenerated with other settings, so every version counts as a try.
        Timeframes with a dataset: {tfs.length ? tfs.join(", ") : "none"}.</p>
        : <p className="small">Each version is backtested once on the research dates ({v.discovery_period ? `${new Date(v.discovery_period.start).toLocaleDateString()} – ${new Date(v.discovery_period.end).toLocaleDateString()}` : "discovery"})
          through the same engine, BID/ASK costs, MNQ sizing and prop checks as your research runs, using the CPU cores from Settings.
          You can stop and continue later. Results: <a href={href("/explorer?campaign_run=set:noise")}>Backtest results → Noise band</a>;
          survivors appear in <a href={href("/holdout")}>Holdout backtest</a> with this set's own holdout tests.</p>}
      {(v.dataset_problems ?? []).length > 0 && <p className="small muted">Not used: {(v.dataset_problems ?? []).map((p) => `${p.timeframe ?? ""} ${p.reason}`).join("; ")}.</p>}
    </Card>
    <Card title="What every version shares and what varies" testId="nb-design">
      <ul className="small">{v.design.fixed.map((f) => <li key={f}>Fixed: {f}</li>)}
        <li>Fixed: $500 risk per trade in MNQ micros, at most 40.</li></ul>
      <TableWrap className="fit"><table className="dense fit-table">
        <thead><tr><th>Setting</th><th>Values tried</th></tr></thead>
        <tbody>{Object.entries(v.design.dimensions).map(([k, vals]) => <tr key={k}><td>{DIM_LABEL[k] ?? humanize(k)}</td>
          <td className="small">{vals.map(word).join(" · ")}</td></tr>)}</tbody>
      </table></TableWrap>
      <p className="small muted">Noise band (Zarattini, Aziz & Barbon 2024): for each time of day, the average move away from the 9:30 open over the
        previous days; the band = the 9:30 open (or yesterday's close, whichever is further out) ± that average × the width multiplier.
        Close time: the trade is closed at the open of the first candle at that time; "16:00 session end" uses the normal end-of-day close.</p>
    </Card>
    <Confirm open={ask} title="Create the 10,000 noise band versions?" confirmLabel="Create" busy={busy}
      onConfirm={create} onCancel={() => setAsk(false)}>
      <p>The 10,000 versions are designed once and frozen under their own protocol: 10,000 tries and their own holdout tests.
        Nothing is backtested yet. This cannot be undone or redone with other settings.</p>
    </Confirm>
    <TechDetails rows={[["Research protocol", <Mono>{v.parent?.protocol_id}</Mono>], ["Set protocol", <Mono>{s?.protocol_id ?? "—"}</Mono>],
      ["Design fingerprint", <Mono>{v.design_hash}</Mono>]]} />
  </>;
}

function LiveJob({ jobId, onFinished, onClose, onRunning }: { jobId: string; onFinished: () => void; onClose: () => void;
  onRunning: (r: boolean) => void }) {
  const [job, setJob] = useState<NbJob | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [cancelling, setCancelling] = useState(false);
  useEffect(() => {
    let live = true, t = 0;
    const poll = () => api.get<NbJob>(`/api/noise-band/jobs/${jobId}`).then((j) => {
      if (!live) return;
      setJob(j); setErr(null);
      onRunning(!FINAL.has(j.state));
      if (FINAL.has(j.state)) onFinished(); else t = window.setTimeout(poll, 2000);
    }).catch((e) => { if (live) { setErr(e as ApiError); onRunning(false); } });
    poll();
    return () => { live = false; window.clearTimeout(t); onRunning(false); };
  }, [jobId]);  // eslint-disable-line react-hooks/exhaustive-deps
  if (err && !job) return <Card title="Noise band job"><ErrorPanel error={err} title="This job is not known to this app session" />
    <Button small onClick={onClose}>Close</Button></Card>;
  if (!job) return <Card title="Noise band job"><Loading label="Connecting…" /></Card>;
  const final = FINAL.has(job.state);
  const L = job.live;
  const running = job.kind === "noise_band";
  return (
    <Card testId="nb-live" title={<>{running ? "Backtesting the noise band set" : "Creating the noise band set"} <Badge
      tone={job.state === "completed" ? "ok" : job.state === "failed" ? "error" : "info"}>{humanize(job.state)}</Badge></>}
      actions={final ? <Button small onClick={onClose}>Close</Button>
        : running ? <Button small kind="danger" busy={cancelling} busyLabel="Stopping…" testId="nb-cancel"
            onClick={async () => { setCancelling(true); try { setJob(await api.post<NbJob>(`/api/noise-band/jobs/${jobId}/cancel`, {})); } finally { setCancelling(false); } }}>
            Stop (backtests already running finish)</Button> : undefined}>
      {running ? <>
        <div className="progress" aria-label="noise band progress"><span style={{ width: `${(100 * (L.done ?? 0)) / Math.max(1, L.total ?? 0)}%` }} /></div>
        <p className="small">{(L.done ?? 0).toLocaleString()} of {(L.total ?? 0).toLocaleString()} done · {(L.completed ?? 0).toLocaleString()} backtested
          {L.skipped ? ` · ${L.skipped.toLocaleString()} already done` : ""}{L.failed ? ` · ${L.failed} failed` : ""}
          {!final && L.current ? <> · now <b>{L.current}</b></> : null} · {job.processes} CPU core{job.processes === 1 ? "" : "s"}</p>
      </> : <p className="small">{final && job.result ? `${job.result.count.toLocaleString()} versions saved (${Object.entries(job.result.by_timeframe).map(([k, x]) => `${k} ${x.toLocaleString()}`).join(", ")}).`
        : L.phase ?? "Starting…"}</p>}
      {job.error && <Banner tone="error">{job.error}</Banner>}
    </Card>
  );
}
