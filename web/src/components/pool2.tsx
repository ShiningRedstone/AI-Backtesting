import { useEffect, useState } from "react";
import type { ApiError } from "../api/client";
import { pool2 } from "../api/pool2";
import type { Pool2Job, Pool2Status } from "../api/pool2";
import { useApi, useApp } from "../app/context";
import { Banner, Button, Card, ErrorPanel, KeyValues, Mono, Spinner, TechDetails, fmt } from "./ui";

const FINAL = new Set(["completed", "failed", "cancelled"]);

/** Strategy pool 2 (ADR-86) on Run backtest → Research runs. Step 1 creates the second 10,000 strategies (safe: nothing
 *  is run and nothing else changes). Step 2 moves research to one protocol for 20,000 strategies (irreversible: the
 *  current protocol is retired, unchanged, and both pools become research campaigns); it needs a typed confirmation. */
export function Pool2Panel({ onChanged }: { onChanged?: () => void }) {
  const { toast } = useApp();
  const st = useApi<Pool2Status>(pool2.statusUrl);
  const [job, setJob] = useState<Pool2Job | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [text, setText] = useState("");
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!job || FINAL.has(job.state)) return;
    const t = window.setTimeout(() => pool2.job(job.job_id).then((j) => {
      setJob(j);
      if (FINAL.has(j.state)) {
        if (j.state === "completed") toast("ok", j.action === "generate" ? "Strategy pool 2 is ready (10,000 strategies)"
          : "Research now uses the 20,000-strategy protocol");
        st.reload();
        onChanged?.();
      }
    }).catch((e: ApiError) => setErr(e)), 1500);
    return () => window.clearTimeout(t);
  }, [job, st, toast, onChanged]);

  const start = (fn: () => Promise<Pool2Job>) => {
    setErr(null);
    fn().then(setJob).catch((e: ApiError) => setErr(e));
  };
  const s = st.data;
  if (st.error) return <ErrorPanel error={st.error} title="Could not load strategy pool 2" />;
  if (!s) return null;
  const running = job && !FINAL.has(job.state);
  const progress = running ? <Banner tone="info" testId="pool2-progress"><Spinner /> {job!.live.phase}</Banner> : null;
  const failed = job && job.state === "failed" ? <Banner tone="error" testId="pool2-failed">{job.error}</Banner> : null;

  if (s.switched) {
    if (!open) return (
      <div className="small muted" data-testid="pool2-done">Research uses one protocol for {fmt(s.total_budget)} strategies (strategy pool 1 + strategy pool 2).{" "}
        <a href="#" onClick={(e: { preventDefault: () => void }) => { e.preventDefault(); setOpen(true); }}>Details</a></div>);
  }
  const p = s.protocol;
  return (
    <Card title="Strategy pool 2" testId="pool2-panel" actions={s.switched ? <Button small kind="ghost" onClick={() => setOpen(false)}>Hide</Button> : undefined}>
      {s.switched ? (
        <KeyValues rows={[["Protocol", <>{fmt(p?.trial_budget)} strategies · {fmt(p?.trials_used)} tested so far</>],
          ["Holdout looks", <>{fmt(p?.holdout_looks_used)} used of {fmt(p?.holdout_looks_budget)}</>],
          ["Research campaigns", <>{s.campaigns_under_active.length} (strategy pool 1 and strategy pool 2)</>]]} />
      ) : !s.pool2 ? (
        <>
          <p className="small">A second set of <b>10,000 strategies</b>: 6,000 from 25 new families (pivot points, gap trading, Turtle Soup, Keltner,
            Supertrend, Ichimoku, divergence, Heikin-Ashi and more) and 4,000 from your 30 existing families using new sessions, filters, stops,
            targets and entries. Pool 1 is not changed, and no strategy of pool 1 is repeated. Creating the pool runs nothing.</p>
          {s.blockers.filter((b) => !b.startsWith("Create strategy pool 2")).map((b) => <Banner key={b} tone="warn">{b}</Banner>)}
          {progress}{failed}
          <div className="actions"><Button kind="primary" onClick={() => start(pool2.generate)} busy={!!running} busyLabel="Creating…"
            disabled={!s.pool1} testId="pool2-generate">Create strategy pool 2</Button></div>
        </>
      ) : (
        <>
          <p className="small"><b>Step 1 done:</b> strategy pool 2 holds {fmt(s.pool2.n_strategies)} strategies in {s.pool2.n_families} families.</p>
          <p className="small"><b>Step 2:</b> {s.switch_incomplete ? "finish the switch: freeze the missing research campaign(s)." : <>
            switch research to one protocol for {fmt(s.total_budget)} strategies (pool 1 + pool 2). Same data, discovery and holdout dates,
            costs and execution; every strategy is then judged against 20,000 tests instead of 10,000. The current protocol is retired and kept
            unchanged as evidence. This cannot be undone.</>}</p>
          {p && !s.switch_incomplete && <KeyValues rows={[
            ["Current protocol", <>{fmt(p.trial_budget)} strategies · {fmt(p.trials_used)} tested · holdout looks {fmt(p.holdout_looks_used)} used of {fmt(p.holdout_looks_budget)}</>],
            ["New protocol", <>{fmt(s.total_budget)} strategies · holdout looks {fmt(p.holdout_looks_budget - p.holdout_looks_used)} (looks already used are carried over)</>],
            ["Pool 1 now", s.old_campaign ? <>{fmt(s.old_campaign.completed)} with results · {fmt(s.old_campaign.remaining)} without</> : "—"]]} />}
          {s.blockers.map((b) => <Banner key={b} tone="warn">{b}</Banner>)}
          {s.warnings.map((w) => <Banner key={w} tone="warn" testId="pool2-warning">{w}</Banner>)}
          {progress}{failed}
          <div className="inline">
            <input className="input" style={{ width: 220 }} placeholder={`Type ${s.confirm_word} to confirm`} value={text}
              aria-label={`type ${s.confirm_word} to confirm`} data-testid="pool2-confirm"
              onChange={(e: { target: HTMLInputElement }) => setText(e.target.value)} />
            <Button kind="primary" onClick={() => start(() => pool2.switchProtocol(text))} busy={!!running} busyLabel="Switching…"
              disabled={text !== s.confirm_word || s.blockers.length > 0} testId="pool2-switch">
              {s.switch_incomplete ? "Finish the switch" : "Switch to the 20,000-strategy protocol"}</Button>
          </div>
        </>
      )}
      {err && <ErrorPanel error={err} />}
      <TechDetails rows={[["Pool 1 manifest", <Mono>{s.pool1?.manifest_id ?? "—"}</Mono>], ["Pool 2 manifest", <Mono>{s.pool2?.manifest_id ?? "—"}</Mono>],
        ["Pool 2 seed", <Mono>{s.pool2?.seed ?? "—"}</Mono>], ["Active protocol", <Mono>{p?.protocol_id ?? "—"}</Mono>]]} />
    </Card>
  );
}
