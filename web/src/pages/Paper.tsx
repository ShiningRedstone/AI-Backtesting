import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api/client";
import type { PaperAccountDetail, PaperAccountRow, PaperAttempt, PaperCandidates, PaperFeedStatus, PaperSourceCheck, PaperState } from "../api/types";
import { go, href, useRoute } from "../app/router";
import { useApi, useApp } from "../app/context";
import { datasetLabel, facetLabel, humanize, plainProse, profileLabel } from "../app/labels";
import { Badge, Banner, Button, Card, Checkbox, Confirm, Drawer, Empty, ErrorPanel, KeyValues, Kpi, Loading, Mono, TableWrap, TechDetails, fmt,
  shortTime, signCls } from "../components/ui";
import { LineChart } from "../components/charts";

/** ADR-81 paper trading: strategies trade NEW Dukascopy days (downloaded after each completed trading day) in simulated
 *  prop accounts. Evaluations restart after a failure (fee charged), passed accounts keep trading funded and record every
 *  payout. Everything is recomputed by the backend from the feed; this page only shows it. Never a research result. */
export function PaperPage() {
  const route = useRoute();
  return route.parts[1] === "new" ? <StartPaper /> : <PaperAccounts />;
}

export const PAPER_LABEL = "Paper trading: simulated forward trading on Dukascopy USA 100 data (an index CFD stand-in for NQ), "
  + "under the prop account's default assumed rules. Not a research result, not a trial, not live trading.";
const usd = (v: number | null | undefined) => v == null ? "—" : `${v < 0 ? "−" : ""}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
const ATTEMPT_LABEL: Record<PaperAttempt["status"], string> = {
  in_progress: "Evaluation in progress", failed: "Evaluation failed", incompatible: "Cannot trade this account", funded: "Funded, trading",
  funded_lost: "Funded account lost", funded_completed: "Funded cycle completed" };
const ATTEMPT_TONE: Record<PaperAttempt["status"], "ok" | "warn" | "error" | "info" | "neutral"> = {
  in_progress: "info", failed: "error", incompatible: "error", funded: "ok", funded_lost: "error", funded_completed: "ok" };
const REASON: Record<string, string> = { LIVE_TRANSITION_ELIGIBLE: "reached the live-account point", PAYOUT_COUNT_LIMIT_REACHED: "payout limit reached" };
const price = (v: number | null | undefined) => v == null ? "—" : v.toFixed(2);
const reasonLabel = (r: string | null | undefined) => r ? REASON[r] ?? humanize(r) : "—";
const FEE_LABEL = { eval_price: "Evaluation price", reset_fee: "Reset fee", activation_fee: "Activation fee" } as const;

function stateBadge(row: { state: PaperAccountRow["state"]; current_attempt: number; status?: string }) {
  if (row.status === "stopped" || row.state === "stopped") return <Badge tone="neutral">Stopped</Badge>;
  if (row.state === "funded") return <Badge tone="ok">Funded · attempt {row.current_attempt}</Badge>;
  if (row.state === "evaluation") return <Badge tone="info">Evaluation · attempt {row.current_attempt}</Badge>;
  return <Badge tone="neutral" title="Waiting for the first completed trading day with a trade">Waiting for first trade</Badge>;
}

/** Feed status + "Update now". Polls quickly while an update runs. */
function FeedBanner({ onUpdated }: { onUpdated: () => void }) {
  const { toast } = useApp();
  const feed = useApi<PaperFeedStatus>("/api/paper/feed");
  const running = feed.data?.manager.state === "running" || !!feed.data?.manager.checking_source;
  const [askContinue, setAskContinue] = useState(false);
  const lastRun = feed.data?.manager.last_run;
  useEffect(() => {
    const t = window.setInterval(feed.reload, running ? 3000 : 60000);
    return () => window.clearInterval(t);
  }, [running]);
  useEffect(() => { if (lastRun) onUpdated(); }, [lastRun]);
  if (feed.error) return <ErrorPanel error={feed.error} />;
  if (!feed.data) return null;
  const f = feed.data, errs = Object.entries(f.errors ?? {});
  const update = () => api.post("/api/paper/feed/update", {}).then(() => { toast("info", "Checking for new trading days…"); window.setTimeout(feed.reload, 800); })
    .catch((e: Error) => toast("error", e.message));
  const check = () => api.post("/api/paper/feed/check", {}).then(() => { toast("info", "Comparing downloaded days with your research data…");
    window.setTimeout(feed.reload, 800); }).catch((e: Error) => toast("error", e.message));
  const goOn = () => api.post("/api/paper/feed/continue-anyway", {}).then(() => { setAskContinue(false); toast("ok", "Paper accounts continue updating");
    feed.reload(); }).catch((e: Error) => toast("error", e.message));
  const c = f.source_check;
  return (
    <Card testId="paper-feed" title="Market data" actions={<>
      <Button small onClick={check} busy={!!f.manager.checking_source} busyLabel="Comparing…" disabled={running && !f.manager.checking_source}
        testId="paper-check">Check against my research data</Button>
      <Button small onClick={update} busy={f.manager.state === "running" && !f.manager.checking_source} busyLabel="Updating…" testId="paper-update">Update now</Button></>}>
      <KeyValues rows={[
        ["Source", f.source],
        ["Same as your research data", <span data-testid="paper-source-status">{sourceSummary(c, !!f.manager.checking_source)}</span>],
        ["Days downloaded", f.n_days ? `${f.n_days} (${f.first_day} to ${f.newest_day})` : "none yet (downloads start with the first paper account)"],
        ["Last check", f.manager.last_run ? shortTime(f.manager.last_run) : f.checked_at ? shortTime(f.checked_at) : "—"],
        ["Next automatic check", f.manager.next_check ? shortTime(f.manager.next_check) : "when the app is open: at start, then every 30 minutes"]]} />
      {c?.verdict === "mismatch" && <Banner tone="error" testId="paper-source-mismatch">
        <b>The downloaded data does not match your research data</b> ({datasetLabel(c.dataset_id)}): {(c.bars_different ?? 0).toLocaleString()} of
        {" "}{(c.compared ?? 0).toLocaleString()} one-minute bars have different prices (largest gap: BID {gap(c, false)}, ASK {gap(c, true)}).
        {f.paused ? <> Paper accounts are <b>paused</b> and keep their last results until a later check matches.{" "}
          <button className="linklike" onClick={() => setAskContinue(true)} data-testid="paper-continue">Continue anyway</button></>
          : <> You chose to continue anyway{c.continue_anyway ? ` (${shortTime(c.continue_anyway.at)})` : ""}.</>}</Banner>}
      {!f.downloader_available && <Banner tone="error">The Dukascopy downloader (dukascopy-python) is not installed, so no new days can be downloaded.</Banner>}
      {f.manager.last_error && <Banner tone="error" testId="paper-feed-error">Last update failed: {f.manager.last_error}. It is retried at the next check.</Banner>}
      {errs.length > 0 && <Banner tone="warn">Waiting on {errs.length === 1 ? "a day" : "days"} that could not be used yet: {errs.map(([d, m]) =>
        `${d} (${plainProse(m)})`).join("; ")}. Later days wait for {errs.length === 1 ? "it" : "them"}, so the data never has a hole.</Banner>}
      <p className="small muted">Only completed trading days are used (after the 16:15 New York close plus 45 minutes). Days are never filled in or
        changed after download; a day with no data from the source (a holiday) is skipped and listed under Technical details.</p>
      <TechDetails rows={[["Skipped days", Object.keys(f.skipped ?? {}).join(", ") || "none"], ["Last completed trading day", f.last_completed_date ?? "—"],
        ["Research dataset compared", <Mono>{c?.dataset_id ?? "—"}</Mono>],
        ["Days compared", (c?.days ?? []).map((d) => `${d.date}: ${d.error ? plainProse(d.error) : `${d.compared ?? 0} bars, ${d.bars_different ?? 0} different`
          + `${d.only_in_download ? `, ${d.only_in_download} only in the download` : ""}${d.only_in_research ? `, ${d.only_in_research} only in the research data` : ""}`}`).join("; ") || "—"]]} />
      <Confirm open={askContinue} title="Continue paper trading on this data?" confirmLabel="Continue anyway" danger onConfirm={goOn} onCancel={() => setAskContinue(false)}>
        The prices downloaded for paper trading differ from your research data, so paper results may not be comparable with your backtests.
        Accounts start updating again; the next check you run replaces this choice.</Confirm>
    </Card>
  );
}

const gap = (c: PaperSourceCheck, ask: boolean) => {
  const keys = ask ? ["ask_open", "ask_high", "ask_low", "ask_close"] : ["open", "high", "low", "close"];
  return Math.max(0, ...keys.map((k) => c.max_abs_diff?.[k] ?? 0)).toFixed(3);
};
function sourceSummary(c: PaperSourceCheck | null, checking: boolean) {
  if (checking) return "comparing…";
  if (!c) return "not checked yet (runs by itself with the first download)";
  switch (c.verdict) {
    case "match": return <><Badge tone="ok">same prices</Badge> {(c.compared ?? 0).toLocaleString()} one-minute bars over {c.days?.length ?? 0} days identical
      to {datasetLabel(c.dataset_id)} (checked {shortTime(c.checked_at)})</>;
    case "mismatch": return <><Badge tone="error">different prices</Badge> checked {shortTime(c.checked_at)}</>;
    case "no_overlap": return <><Badge tone="neutral">nothing to compare</Badge> {plainProse(c.note ?? "")}</>;
    case "no_dataset": return <><Badge tone="neutral">not checked</Badge> {plainProse(c.note ?? "")}</>;
    default: return <><Badge tone="warn">could not check</Badge> {plainProse((c.days ?? []).find((d) => d.error)?.error ?? c.note ?? "")}; tried again at the
      next update</>;
  }
}

function PaperAccounts() {
  const { prefs } = useApp();
  const route = useRoute();
  const accounts = useApi<PaperAccountRow[]>("/api/paper/accounts");
  const [open, setOpen] = useState<string | null>(route.query.get("account"));
  const name = (pid: string) => profileLabel(pid, prefs.profile_choices?.find((p) => p.profile_id === pid)?.name);
  const rows = accounts.data ?? [];
  const tot = rows.reduce((a, r) => ({ net: a.net + r.net, fees: a.fees + r.fees_total, pay: a.pay + r.trader_payouts, passes: a.passes + r.passes }),
    { net: 0, fees: 0, pay: 0, passes: 0 });
  return (
    <div className="page" data-testid="paper-page">
      <header className="page-head"><div><h1>Paper accounts</h1></div>
        <div className="actions"><Button kind="primary" onClick={() => go("/paper/new")} testId="paper-new">Start paper trading</Button></div></header>
      <Banner tone="info">{PAPER_LABEL}</Banner>
      <FeedBanner onUpdated={accounts.reload} />
      {accounts.error ? <ErrorPanel error={accounts.error} /> : !accounts.data ? <Loading label="Loading paper accounts…" kind="table" /> : !rows.length ? (
        <Card><Empty>No paper accounts yet. <a href={href("/paper/new")}>Start paper trading</a> with your survivors.</Empty></Card>) : <>
        <div className="kpis">
          <Kpi label="Accounts" value={rows.length} sub={`${rows.filter((r) => r.status === "running").length} running`} />
          <Kpi label="Passed evaluations" value={tot.passes} />
          <Kpi label="Payouts (your share)" value={usd(tot.pay)} />
          <Kpi label="Fees paid" value={usd(tot.fees)} />
          <Kpi label="Net (payouts − fees)" value={usd(tot.net)} tone={tot.net > 0 ? "pos" : tot.net < 0 ? "neg" : ""} accent />
        </div>
        <Card title="Accounts" testId="paper-accounts">
          <TableWrap><table className="dense hover">
            <thead><tr><th>Strategy</th><th>Prop account</th><th>Started</th><th>Now</th><th className="right">Balance</th><th className="right">Attempts</th>
              <th className="right">Passes</th><th className="right">Payouts</th><th className="right">Fees</th><th className="right">Net</th><th className="right">Trades</th>
              <th>Data up to</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.account_id} className="clickable" onClick={() => setOpen(r.account_id)} data-testid={`paper-row-${r.account_id}`}>
                <td>{r.display_name ?? "Unnamed strategy"}</td><td>{name(r.profile_id)}</td><td>{r.start_date}</td>
                <td>{stateBadge(r)}</td><td className="right">{usd(r.balance)}</td><td className="right">{r.attempts}</td><td className="right">{r.passes}</td>
                <td className="right">{usd(r.trader_payouts)}</td><td className="right">{usd(r.fees_total)}</td>
                <td className={`right ${signCls(r.net)}`}>{usd(r.net)}</td><td className="right">{r.n_trades}</td><td>{r.last_day ?? "—"}</td></tr>))}
            </tbody></table></TableWrap>
          <p className="small muted">Net = payouts you would have received (your share) minus every fee paid: the first evaluation, a reset (or a new
            evaluation if no reset fee is set) after each failed evaluation, an activation fee after each pass, and a new evaluation after a funded
            account is lost or completes. Click a row for its attempts, payouts and trades.</p>
        </Card></>}
      {open && <AccountDrawer id={open} onClose={() => setOpen(null)} onChanged={accounts.reload} profileName={name} />}
    </div>
  );
}

function AccountDrawer({ id, onClose, onChanged, profileName }: { id: string; onClose: () => void; onChanged: () => void; profileName: (p: string) => string }) {
  const { toast } = useApp();
  const d = useApi<PaperAccountDetail>(`/api/paper/accounts/${id}`, [id]);
  const [busy, setBusy] = useState(false);
  const [del, setDel] = useState(false);
  const act = (a: "stop" | "resume" | "delete") => {
    setBusy(true);
    api.post(`/api/paper/accounts/${id}/${a}`, {})
      .then(() => { toast("ok", a === "delete" ? "Paper account deleted" : a === "stop" ? "Paper account stopped" : "Paper account resumed: it catches up at the next update");
        onChanged(); if (a === "delete") onClose(); else d.reload(); })
      .catch((e: Error) => toast("error", e.message)).finally(() => { setBusy(false); setDel(false); });
  };
  const a = d.data?.account, st = d.data?.state;
  return (
    <Drawer open onClose={onClose} testId="paper-drawer" title={a?.display_name ?? "Paper account"}
      subtitle={a ? `${profileName(a.profile_id)} · started ${a.start_date}` : undefined}
      actions={a && <>{a.status === "running"
        ? <Button small onClick={() => act("stop")} busy={busy} testId="paper-stop">Stop</Button>
        : <Button small onClick={() => act("resume")} busy={busy} testId="paper-resume">Resume</Button>}
        <Button small kind="danger" onClick={() => setDel(true)} testId="paper-delete">Delete</Button></>}>
      {d.error ? <ErrorPanel error={d.error} /> : !a ? <Loading label="Loading the account…" /> : <>
        {a.status === "stopped" && <Banner tone="warn">Stopped{a.stop_reason ? `: ${plainProse(a.stop_reason)}` : ""}. A stopped account is not updated.</Banner>}
        {!st ? <Empty>No trading day has been processed yet. The account starts trading on {a.start_date} (the next trading day after it was started)
          and is updated after that day closes.</Empty> : <AccountState st={st} />}
        <TechDetails rows={[["Paper account", <Mono>{a.account_id}</Mono>], ["Strategy", <Mono>{a.strategy_id}</Mono>],
          ["Logic hash", <Mono>{a.logic_hash ?? "—"}</Mono>], ["Rule profile", <Mono>{`${a.profile_id} v${a.profile_version}`}</Mono>],
          ["Start (UTC)", <Mono>{a.start_ts}</Mono>], ["Fees at start", <Mono>{JSON.stringify(a.fees)}</Mono>],
          ["Data hash (1-minute feed)", <Mono>{st?.feed.content_hash ?? "—"}</Mono>], ["Computed", <Mono>{st?.computed_at ?? "—"}</Mono>]]} />
      </>}
      <Confirm open={del} title="Delete this paper account?" confirmLabel="Delete" danger busy={busy} onConfirm={() => act("delete")} onCancel={() => setDel(false)}>
        Its attempts, payouts and trades are removed. The downloaded market data is kept.</Confirm>
    </Drawer>
  );
}

function AccountState({ st }: { st: PaperState }) {
  const curve = useMemo(() => {
    let c = 0;
    return st.trades.map((t) => (c += t.net_usd));
  }, [st.trades]);
  const cur = st.attempts[st.attempts.length - 1];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }} data-testid="paper-state">
      <div className="kpis">
        <Kpi label="Now" value={stateBadge({ state: st.state, current_attempt: st.current_attempt })} sub={cur?.balance != null ? `balance ${usd(cur.balance)}` : undefined} />
        <Kpi label="Attempts" value={st.attempts.length} sub={`${st.passes} passed`} />
        <Kpi label="Payouts (your share)" value={usd(st.trader_payouts)} sub={`${st.payouts.length} payout${st.payouts.length === 1 ? "" : "s"}`} />
        <Kpi label="Fees paid" value={usd(st.fees_total)} />
        <Kpi label="Net" value={usd(st.net)} tone={st.net > 0 ? "pos" : st.net < 0 ? "neg" : ""} accent />
      </div>
      {st.stopped && <Banner tone="error">{plainProse(st.stopped)}</Banner>}
      <Card title="Attempts" testId="paper-attempts">
        <TableWrap><table className="dense">
          <thead><tr><th>#</th><th>Started</th><th>Ended</th><th>Result</th><th>Why it ended</th><th className="right">Trades</th>
            <th className="right">Eval P&L</th><th className="right">Eval days</th><th className="right">Fee</th><th className="right">Payouts</th></tr></thead>
          <tbody>{st.attempts.map((x) => (
            <tr key={x.n}><td>{x.n}</td><td className="small">{shortTime(x.start)}</td><td className="small">{x.end ? shortTime(x.end) : "—"}</td>
              <td><Badge tone={ATTEMPT_TONE[x.status]}>{ATTEMPT_LABEL[x.status]}</Badge></td>
              <td className="small" title={x.detail ?? undefined}>{x.end ? reasonLabel(x.reason) : "—"}</td>
              <td className="right">{x.n_trades}</td><td className={`right ${signCls(x.eval_profit)}`}>{usd(x.eval_profit)}</td>
              <td className="right">{fmt(x.eval_trading_days)}</td><td className="right">{usd(x.fee)}</td>
              <td className="right">{x.payouts ? `${x.payouts} · ${usd(x.trader_payout)}` : "—"}</td></tr>))}
          </tbody></table></TableWrap>
      </Card>
      {st.trades.length > 0 && <Card title="Cumulative trade P&L across all attempts (USD)">
        <LineChart x={st.trades.map((t) => t.exit_ts.slice(0, 10))} unit="USD" testId="paper-curve"
          series={[{ id: "pnl", label: "Cumulative P&L", values: curve, area: true }]} /></Card>}
      <div className="grid-cards">
        <Card title="Payouts">{!st.payouts.length ? <Empty>No payouts yet.</Empty> : (
          <TableWrap><table className="dense"><thead><tr><th>Attempt</th><th>Date</th><th className="right">Gross</th><th className="right">Your share</th></tr></thead>
            <tbody>{st.payouts.map((p) => <tr key={`${p.attempt}-${p.n}`}><td>{p.attempt}</td><td>{p.date}</td><td className="right">{usd(p.gross)}</td>
              <td className="right pos">{usd(p.trader_share)}</td></tr>)}</tbody></table></TableWrap>)}</Card>
        <Card title="Fees">
          <TableWrap><table className="dense"><thead><tr><th>Attempt</th><th>Fee</th><th>Date</th><th className="right">Amount</th></tr></thead>
            <tbody>{st.fees.map((f, i) => <tr key={i}><td>{f.attempt}</td><td>{FEE_LABEL[f.kind]}</td><td className="small nowrap">{f.at?.slice(0, 10) ?? "—"}</td>
              <td className="right">{usd(f.amount)}</td></tr>)}</tbody></table></TableWrap></Card>
      </div>
      <Card title={`Trades (${st.n_trades})`}>
        {!st.trades.length ? <Empty>No trades yet.</Empty> : <TableWrap><table className="dense">
          <thead><tr><th>Entry time</th><th>Side</th><th className="right">MNQ</th><th className="right">Entry fill</th><th className="right">Exit fill</th>
            <th>Exit</th><th className="right">Net (USD)</th></tr></thead>
          <tbody>{st.trades.slice(-300).reverse().map((t, i) => (
            <tr key={i}><td className="small" title={`exit ${shortTime(t.exit_ts)}`}>{shortTime(t.entry_ts)}</td><td>{t.direction > 0 ? "Long" : "Short"}</td>
              <td className="right">{t.contracts}</td><td className="right">{price(t.entry_price_eff)}</td><td className="right">{price(t.exit_price_eff)}</td>
              <td>{humanize(t.exit_reason)}</td><td className={`right nowrap ${signCls(t.net_usd)}`}>{usd(t.net_usd)}</td></tr>))}
          </tbody></table></TableWrap>}
        {st.trades.length > 300 && <p className="small muted">Showing the latest 300 trades.</p>}
      </Card>
      <p className="small muted">Data used: {st.feed.first_day} to {st.feed.last_day} (the first 40 trading days only warm up indicators; trades count from the
        start date). Accounts with percentage-of-balance sizing size from their own balance, which restarts at each attempt; payout withdrawals do not
        lower that sizing balance inside a funded cycle.</p>
    </div>
  );
}

/** Batch start: one paper account per chosen strategy, all with the same prop account and fees. */
function StartPaper() {
  const { prefs, toast } = useApp();
  const [profile, setProfile] = useState(prefs.prop_criteria_profile);
  const [showAll, setShowAll] = useState(false);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const cands = useApi<PaperCandidates>(`/api/paper/candidates?profile_id=${encodeURIComponent(profile)}&show_all=${showAll ? 1 : 0}`, [profile, showAll]);
  const feed = useApi<PaperFeedStatus>("/api/paper/feed");
  useEffect(() => { setSel(new Set()); }, [profile, showAll]);
  const fees = (prefs.prop_fees ?? {})[profile];
  const rows = (cands.data?.strategies ?? []).filter((s) => !q || (s.display_name ?? "").toLowerCase().includes(q.toLowerCase()));
  const selectable = rows.filter((s) => !s.already_running);
  const allOn = selectable.length > 0 && selectable.every((s) => sel.has(s.strategy_id));
  const toggle = (sid: string, on: boolean) => setSel((p) => { const n = new Set(p); if (on) n.add(sid); else n.delete(sid); return n; });
  const start = () => {
    setBusy(true); setErr(null);
    api.post<{ accounts: string[]; start_date: string }>("/api/paper/batches", { strategy_ids: [...sel], profile_id: profile })
      .then((r) => { toast("ok", `${r.accounts.length} paper account${r.accounts.length === 1 ? "" : "s"} started; trading begins ${r.start_date}`); go("/paper"); })
      .catch((e: ApiError) => setErr(e)).finally(() => setBusy(false));
  };
  return (
    <div className="page" data-testid="paper-new-page">
      <header className="page-head"><div><h1>Start paper trading</h1></div></header>
      <Banner tone="info">{PAPER_LABEL}</Banner>
      <Card title="Settings for every chosen strategy" testId="paper-settings">
        <KeyValues rows={[
          ["Prop account", <select className="input" value={profile} aria-label="prop account" data-testid="paper-profile"
            onChange={(e: { target: HTMLSelectElement }) => setProfile(e.target.value)}>
            {(prefs.profile_choices ?? []).map((p) => <option key={p.profile_id} value={p.profile_id}>{p.name}</option>)}</select>],
          ["Starting balance", "the account's own starting balance (50K accounts)"],
          ["First trading day", feed.data ? `${feed.data.next_start_date} (the next trading day: only data that does not exist yet)` : "…"],
          ["Fees", fees?.eval_price != null ? `evaluation ${usd(fees.eval_price)} · reset ${fees.reset_fee != null ? usd(fees.reset_fee) : "not set (a new evaluation is charged)"} · activation ${fees.activation_fee != null ? usd(fees.activation_fee) : "none"}`
            : "not entered yet"]]} />
        {fees?.eval_price == null && <Banner tone="warn" testId="paper-fees-missing">Enter this prop account's fees in <a href={href("/settings")}>Settings → Prop account fees</a> first;
          Munyun Lab never guesses prices.</Banner>}
        <p className="small muted">After a failed evaluation a new attempt starts right away (reset fee, or a new evaluation if no reset fee is set). After a pass the
          account trades funded and records every payout; when a funded account is lost, or reaches the live-account point or the payout limit, a new
          evaluation starts. Strategies must be sized in MNQ contracts.</p>
      </Card>
      <Card title={showAll ? "Tested strategies" : "Survivors"} testId="paper-candidates"
        actions={<Checkbox checked={showAll} onChange={setShowAll} label="Show all tested strategies" testId="paper-show-all" />}>
        <div className="inline">
          <input className="input" style={{ width: 260 }} placeholder="Search…" value={q} aria-label="search strategies"
            onChange={(e: { target: HTMLInputElement }) => setQ(e.target.value)} />
          <span className="muted small">{sel.size} selected</span>
        </div>
        {cands.error ? <ErrorPanel error={cands.error} /> : !cands.data ? <Loading label="Loading strategies…" kind="table" /> : !rows.length ? (
          <Empty>{showAll ? "No tested strategies yet." : "No survivors under this prop account. Turn on “Show all tested strategies” to pick others."}</Empty>) : (
          <TableWrap><table className="dense">
            <thead><tr><th><Checkbox checked={allOn} onChange={(v) => setSel(v ? new Set(selectable.map((s) => s.strategy_id)) : new Set())} label="" testId="paper-select-all" /></th>
              <th>Strategy</th><th>Survivor</th><th>Timeframe</th><th className="right">Expectancy (R)</th><th className="right">Backtest trades</th></tr></thead>
            <tbody>{rows.map((s) => (
              <tr key={s.strategy_id}>
                <td><Checkbox checked={sel.has(s.strategy_id)} disabled={s.already_running} onChange={(v) => toggle(s.strategy_id, v)} label=""
                  testId={`paper-pick-${s.strategy_id}`} /></td>
                <td>{s.display_name ?? "Unnamed strategy"}{s.already_running && <> <Badge tone="info">already paper trading</Badge></>}</td>
                <td>{s.survivor ? <Badge tone="ok">survivor</Badge> : <span className="muted">no</span>}</td>
                <td>{s.timeframe ? facetLabel("timeframe", s.timeframe) : "—"}</td>
                <td className={`right ${signCls(s.expectancy_r)}`}>{fmt(s.expectancy_r)}</td><td className="right">{s.trades}</td></tr>))}
            </tbody></table></TableWrap>)}
        <div className="actions">
          <Button kind="primary" onClick={start} busy={busy} busyLabel="Starting…" disabled={!sel.size || fees?.eval_price == null} testId="paper-start">
            Start {sel.size || ""} paper account{sel.size === 1 ? "" : "s"}</Button>
          <Button onClick={() => go("/paper")}>Cancel</Button>
        </div>
        {err && <ErrorPanel error={err} title="Not started" testId="paper-start-error" />}
      </Card>
    </div>
  );
}
