import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api, ApiError } from "../api/client";
import type { ComboHoldout, ComboJob, ComboPanel, ComboRegistration, ComboRow, ComboSearch, ComboSurvivors, SavedCombo } from "../api/types";
import { go, useRoute } from "../app/router";
import { useApi, useApp } from "../app/context";
import { useMoney } from "../app/money";
import { humanize, plainProse } from "../app/labels";
import { StepTimeChart } from "../components/charts";
import { PoolPicker, YearTable, useCriteriaName } from "../components/results";
import { Badge, Banner, Button, Card, Confirm, Empty, ErrorPanel, Kpi, Loading, Mono, PageSkeleton, Scope, TableWrap, TechDetails, n, r, signCls } from "../components/ui";

/** ADR-92 Strategies → Combinations: up to five surviving strategies traded in ONE prop account, one position at a time,
 *  from their RECORDED discovery trades (nothing is re-run). An automatic search adds survivors while the combination's
 *  ranked score improves; a combination can be registered (once per research protocol) and holdout-tested as a whole,
 *  within its own tests. Every number comes from the backend (/api/combinations/*); this page only lays it out. */
const OUTCOME: Record<string, string> = { HOLDOUT_CRITERIA_MET: "Criteria met", HOLDOUT_CRITERIA_NOT_MET: "Criteria not met" };
const FINAL = new Set(["completed", "failed", "cancelled"]);
const CHIP = ["#5b8def", "#e8a33d", "#3fb68b", "#c86bd8", "#e0675b"];
const CRIT_LABEL: Record<string, string> = { sample: "Enough trades", expectancy: "Net R per trade above 0", adjusted_confidence: "Confidence bound above 0",
  profit_factor: "Profit factor above 1", random_control: "Beats random entries", cost_stress: "Survives higher costs" };
const days = (v: number | null | undefined) => (v == null ? "—" : `${n(v, 0)} days`);
const idsKey = (ids: string[]) => [...ids].sort().join(",");

export function CombinationsPage() {
  const route = useRoute();
  const { toast } = useApp();
  const crit = useCriteriaName();
  const [pool, setPool] = useState("");
  const surv = useApi<ComboSurvivors>(`/api/combinations/survivors${pool ? `?campaign_run=${encodeURIComponent(pool)}` : ""}`, [pool]);
  const latest = useApi<{ search: ComboSearch | null; job: ComboJob | null }>(`/api/combinations/search${pool ? `?campaign_run=${encodeURIComponent(pool)}` : ""}`, [pool]);
  const reg = useApi<ComboRegistration>("/api/combinations/registration");
  const saved = useApi<SavedCombo[]>("/api/combinations/saved");
  const selected = (route.query.get("c") ?? "").split(",").filter(Boolean);
  const select = (ids: string[]) => go(`/combinations?c=${idsKey(ids)}`);
  const [jobId, setJobId] = useState<string | null>(null);
  const [size, setSize] = useState(0);
  const [building, setBuilding] = useState(false);
  const [pick, setPick] = useState<Set<string>>(new Set());
  const [regSel, setRegSel] = useState<Set<string>>(new Set());
  const [askReg, setAskReg] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => {                                  // a search started earlier in this app session: follow it
    const j = latest.data?.job;
    if (!jobId && j && !FINAL.has(j.state)) setJobId(j.job_id);
  }, [latest.data]);  // eslint-disable-line react-hooks/exhaustive-deps
  const names = useMemo(() => new Map((surv.data?.rows ?? []).map((s) => [s.strategy_id, s.display_name ?? "Unnamed strategy"])), [surv.data]);
  const nameOf = (sid: string) => names.get(sid) ?? "Not a current survivor";
  const search = latest.data?.search ?? null;
  const rows = (search?.rows ?? []).filter((x) => !size || x.members.length === size);
  const registered = new Map((reg.data?.combos ?? []).map((c) => [idsKey(c.members), c]));
  const canRegister = !!reg.data?.can_register;
  const startSearch = () => {
    setErr(null);
    api.post<ComboJob>("/api/combinations/search", pool ? { campaign_run: pool } : {})
      .then((j) => { setJobId(j.job_id); toast("info", "Looking for the best combinations…"); })
      .catch((e: ApiError) => setErr(e));
  };
  const register = () => {
    setBusy(true); setErr(null);
    api.post<{ n_registered: number }>("/api/combinations/registration", { combinations: [...regSel].map((k) => k.split(",")), confirm: typed })
      .then((o) => { toast("ok", `${o.n_registered} combination${o.n_registered === 1 ? "" : "s"} registered for holdout tests`); setRegSel(new Set());
        setAskReg(false); setTyped(""); reg.reload(); })
      .catch((e: ApiError) => { setErr(e); setAskReg(false); }).finally(() => setBusy(false));
  };
  const toggleReg = (k: string, on: boolean) => setRegSel((s) => { const x = new Set(s); if (on) x.add(k); else x.delete(k); return x; });
  const p = reg.data?.protocol ?? null;
  const nSurv = surv.data?.rows.length ?? 0;
  return (
    <div className="page" data-testid="combos-page">
      <header className="page-head"><div><h1>Combinations</h1></div></header>
      <Banner tone="info">Combine up to five survivors and trade them in <b>one prop account, one position at a time</b>. The search starts from every
        survivor and keeps adding the survivor that improves the combination most (fewer losing months, more trades per week up to your limit,
        higher net R per trade, faster evaluation pass and first payout) until nothing improves. Results come from the strategies' recorded
        research trades; nothing is backtested again.</Banner>
      {surv.error ? <ErrorPanel error={surv.error} /> : !surv.data ? <PageSkeleton layout="combos" label="Loading survivors…" /> : <>
        <div className="kpis">
          <Kpi label="Survivors" value={nSurv} sub={`under ${crit}`} accent />
          <Kpi label="Combinations checked" value={(reg.data?.evaluated_in_protocol ?? 0).toLocaleString()} sub="every one counts in the holdout's fairness correction" />
          <Kpi label="Combination holdout tests" value={p ? `${p.tests_left} of ${p.tests_budget} left` : "—"} testId="combos-tests"
            meter={p ? p.tests_used / Math.max(1, p.tests_budget) : null} sub={p ? "one per registered combination" : canRegister ? "register combinations first" : "no active research protocol"} />
          <Kpi label="Trades per week limit" value={n(surv.data.tpw_cap, 1)} sub="more earns no extra credit (Settings)" />
        </div>
        <div className="inline toolbar">
          <PoolPicker value={pool} onChange={setPool} testId="combos-pool" />
          <Button kind="primary" small onClick={startSearch} disabled={!!jobId || nSurv < 2} testId="combos-search">Find best combinations</Button>
          <Button small onClick={() => { setBuilding((b) => !b); setPick(new Set(selected)); }} testId="combos-build">{building ? "Close builder" : "Build your own"}</Button>
        </div>
        {jobId && <LiveSearch jobId={jobId} onDone={() => { setJobId(null); latest.reload(); reg.reload(); }} />}
        {err && <ErrorPanel error={err} title="Not done" testId="combos-error" />}
        <div className="combo-layout">
          <div className="combo-list">
            {building && <Card title="Build your own" testId="combos-builder"
              actions={<Button small kind="primary" disabled={pick.size < 2} onClick={() => { select([...pick]); setBuilding(false); }} testId="combos-show">
                Show combination ({pick.size})</Button>}>
              <p className="small muted" style={{ marginTop: 0 }}>Pick 2 to {surv.data.max_members} survivors.</p>
              <TableWrap className="fit"><table className="dense fit-table"><thead><tr><th style={{ width: 26 }} /><th>Strategy</th>
                <th className="r">Trades / week</th><th className="r">Net R per trade</th><th className="r">Losing months</th></tr></thead>
                <tbody>{surv.data.rows.map((s) => <tr key={s.strategy_id}>
                  <td><input type="checkbox" checked={pick.has(s.strategy_id)} aria-label={`pick ${s.display_name}`} data-testid={`cpick-${s.strategy_id}`}
                    disabled={!pick.has(s.strategy_id) && pick.size >= surv.data!.max_members}
                    onChange={(e: { target: HTMLInputElement }) => setPick((x) => { const y = new Set(x); if (e.target.checked) y.add(s.strategy_id); else y.delete(s.strategy_id); return y; })} /></td>
                  <td className="name-cell"><div className="cell-title">{s.display_name ?? "Unnamed strategy"}</div></td>
                  <td className="r num">{n(s.trades_per_week, 1)}</td><td className={`r num ${signCls(s.expectancy_r)}`}>{r(s.expectancy_r)}</td>
                  <td className="r num">{s.negative_months ?? "—"}</td></tr>)}</tbody></table></TableWrap>
            </Card>}
            <Card title="Best combinations" testId="combos-results"
              actions={canRegister && regSel.size > 0 ? <Button small kind="primary" onClick={() => setAskReg(true)} testId="combos-register">
                Register for holdout tests ({regSel.size})</Button> : undefined}>
              <div className="inline">
                <div className="segmented small" role="group" aria-label="size">
                  {[0, 2, 3, 4, 5].map((k) => <button key={k} className={size === k ? "on" : ""} onClick={() => setSize(k)} data-testid={`combos-size-${k}`}>
                    {k ? `${k} strategies` : "All"}</button>)}</div>
              </div>
              {!search ? <Empty>No search yet. Press <b>Find best combinations</b>{nSurv < 2 ? " (needs at least 2 survivors)" : ""}.</Empty>
                : !rows.length ? <Empty>No combination improved on its starting strategy{size ? ` with ${size} strategies` : ""}.</Empty> : (
                <TableWrap className="fit"><table className="dense fit-table" data-testid="combos-table">
                  <thead><tr>{canRegister && <th style={{ width: 26 }} />}<th>#</th><th>Strategies</th><th className="r">Losing months</th>
                    <th className="r">Trades / week</th><th className="r">Net R / trade</th><th className="r">Evals passed</th>
                    <th className="r" title="median trading days to pass the evaluation · to the first payout">Days: pass · payout</th></tr></thead>
                  <tbody>{rows.map((x) => <ComboTableRow key={idsKey(x.members)} x={x} nameOf={nameOf} active={idsKey(x.members) === idsKey(selected)}
                    onOpen={() => select(x.members)} canRegister={canRegister} checked={regSel.has(idsKey(x.members))}
                    onCheck={(on) => toggleReg(idsKey(x.members), on)} reg={registered.get(idsKey(x.members))?.holdout ?? (registered.has(idsKey(x.members)) ? null : undefined)} />)}
                  </tbody></table></TableWrap>)}
              {search && <p className="small muted">{search.n_survivors} survivors · {search.n_evaluated.toLocaleString()} combinations checked
                {search.n_survivors_other_protocols ? ` · ${search.n_survivors_other_protocols} survivors of another research protocol left out` : ""}
                {search.cancelled ? " · stopped early" : ""} · searched {new Date(search.created_at).toLocaleString()}. {search.ranking_rule}</p>}
            </Card>
            <SavedCard rows={saved.data} nameOf={nameOf} onOpen={select} onChange={() => saved.reload()} canRegister={canRegister}
              regSel={regSel} onCheck={toggleReg} />
            {p && <Card title="Registered for holdout tests" testId="combos-registered">
              <TableWrap><table className="dense"><thead><tr><th>Strategies</th><th>Holdout</th></tr></thead>
                <tbody>{reg.data!.combos.map((c) => <tr key={c.combo_id} className="clickable" onClick={() => select(c.members)}>
                  <td className="small combo-names">{c.members.map((sid) => <div key={sid}>{nameOf(sid)}</div>)}</td><td><HoldoutBadge h={c.holdout} /></td></tr>)}</tbody></table></TableWrap>
              <p className="small muted">{p.tests_used} of {p.tests_budget} combination holdout tests used. Holdout dates {p.holdout_trading_dates[0]} to
                {" "}{p.holdout_trading_dates[1]}. Registration happens once per research protocol.</p>
            </Card>}
          </div>
          <div className="combo-detail">
            {selected.length >= 2 ? <ComboDetail key={idsKey(selected)} ids={selected} nameOf={nameOf}
              onChanged={() => { reg.reload(); saved.reload(); }} />
              : <Card><Empty>Pick a combination on the left, or build your own, to see its results here.</Empty></Card>}
          </div>
        </div>
      </>}
      <Confirm open={askReg} title={`Register ${regSel.size} combination${regSel.size === 1 ? "" : "s"} for holdout tests?`} confirmLabel="Register" busy={busy}
        onConfirm={() => { if (typed === (reg.data?.confirm_word ?? "REGISTER")) register(); }} onCancel={() => { setAskReg(false); setTyped(""); }}>
        <p>This freezes the list. It happens <b>once</b> per research protocol: combinations cannot be added later. Each registered combination
          can then be tested once on the locked holdout, within 10 combination tests. The fairness correction counts all
          {" "}{(reg.data?.evaluated_in_protocol ?? 0).toLocaleString()} combinations checked so far.</p>
        <input className="input" style={{ width: 240 }} placeholder={`Type ${reg.data?.confirm_word ?? "REGISTER"} to confirm`} value={typed}
          aria-label="type REGISTER to confirm" data-testid="combos-confirm" onChange={(e: { target: HTMLInputElement }) => setTyped(e.target.value)} />
      </Confirm>
    </div>
  );
}

function ComboTableRow({ x, nameOf, active, onOpen, canRegister, checked, onCheck, reg }: { x: ComboRow; nameOf: (s: string) => string; active: boolean;
  onOpen: () => void; canRegister: boolean; checked: boolean; onCheck: (on: boolean) => void; reg: ComboHoldout | null | undefined }) {
  return (
    <tr className={`clickable${active ? " selected" : ""}`} onClick={onOpen} data-testid={`crow-${x.position}`}>
      {canRegister && <td onClick={(e: { stopPropagation: () => void }) => e.stopPropagation()}>
        <input type="checkbox" checked={checked} aria-label="register this combination" data-testid={`creg-${x.position}`}
          onChange={(e: { target: HTMLInputElement }) => onCheck(e.target.checked)} /></td>}
      <td className="num">{x.position}</td>
      <td className="small combo-names">{x.members.map((s, i) => <div key={s} title={nameOf(s)}><span className="chip-dot" style={{ background: CHIP[i % CHIP.length] }} />{nameOf(s)}</div>)}
        {reg !== undefined && <div style={{ marginTop: 3 }}><HoldoutBadge h={reg} /></div>}</td>
      <td className="r num">{x.negative_months}</td><td className="r num">{n(x.trades_per_week, 1)}</td>
      <td className={`r num ${signCls(x.expectancy_r)}`}>{n(x.expectancy_r, 3)}</td><td className="r num">{x.pass_pct == null ? "—" : `${n(x.pass_pct, 0)}%`}</td>
      <td className="r num">{n(x.median_days_to_pass, 0)} · {n(x.median_days_to_payout, 0)}</td>
    </tr>
  );
}

function HoldoutBadge({ h }: { h: ComboHoldout | null }) {
  if (!h) return <Badge tone="info">Registered</Badge>;
  if (h.outcome) return <Badge tone={h.outcome === "HOLDOUT_CRITERIA_MET" ? "ok" : "warn"}>{OUTCOME[h.outcome] ?? humanize(h.outcome)}</Badge>;
  return <Badge tone={h.status === "failed" ? "error" : "neutral"}>{humanize(h.status)}</Badge>;
}

function LiveSearch({ jobId, onDone }: { jobId: string; onDone: () => void }) {
  const [job, setJob] = useState<ComboJob | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => {
    let live = true, t = 0;
    const poll = () => api.get<ComboJob>(`/api/combinations/jobs/${jobId}`).then((j) => {
      if (!live) return;
      setJob(j);
      if (FINAL.has(j.state)) onDone(); else t = window.setTimeout(poll, 1500);
    }).catch((e) => { if (live) setErr(e as ApiError); });
    poll();
    return () => { live = false; window.clearTimeout(t); };
  }, [jobId]);  // eslint-disable-line react-hooks/exhaustive-deps
  if (err) return <ErrorPanel error={err} title="The search is not known to this app session" />;
  if (!job) return <Card title="Finding combinations"><Loading label="Starting…" /></Card>;
  const done = job.live.done ?? 0, total = job.live.total ?? 0;
  return (
    <Card testId="combos-live" title={<>Finding combinations <Badge tone="info">{humanize(job.state)}</Badge></>}
      actions={<Button small kind="danger" onClick={() => api.post(`/api/combinations/jobs/${jobId}/cancel`, {})} testId="combos-cancel">Stop</Button>}>
      <div className="progress" aria-label="search progress"><span style={{ width: `${total ? (100 * done) / total : 2}%` }} /></div>
      <p className="small muted" style={{ marginBottom: 0 }}>{total ? `${done} of ${total} starting strategies done` : "Loading the survivors' trades…"}
        {job.processes ? ` · ${job.processes} CPU core${job.processes === 1 ? "" : "s"}` : ""}. Nothing is backtested again; this only reads stored trades.</p>
      {job.error && <Banner tone="error">{plainProse(job.error)}</Banner>}
    </Card>
  );
}

function SavedCard({ rows, nameOf, onOpen, onChange, canRegister, regSel, onCheck }: { rows: SavedCombo[] | null; nameOf: (s: string) => string;
  onOpen: (ids: string[]) => void; onChange: () => void; canRegister: boolean; regSel: Set<string>; onCheck: (k: string, on: boolean) => void }) {
  if (!rows?.length) return null;
  return (
    <Card title="Saved combinations" testId="combos-saved">
      <TableWrap><table className="dense"><tbody>{rows.map((s) => { const k = idsKey(s.members);
        return <tr key={k} className="clickable" onClick={() => onOpen(s.members)}>
          {canRegister && <td style={{ width: 26 }} onClick={(e: { stopPropagation: () => void }) => e.stopPropagation()}>
            <input type="checkbox" checked={regSel.has(k)} aria-label="register this combination" onChange={(e: { target: HTMLInputElement }) => onCheck(k, e.target.checked)} /></td>}
          <td className="combo-names"><b>{s.name}</b>{s.members.map((sid) => <div key={sid} className="small muted">{nameOf(sid)}</div>)}</td>
          <td className="r"><button type="button" className="linklike small" onClick={(e: { stopPropagation: () => void }) => {
            e.stopPropagation(); api.post("/api/combinations/saved/delete", { strategy_ids: s.members }).then(onChange); }}>Remove</button></td></tr>; })}
      </tbody></table></TableWrap>
    </Card>
  );
}

function ComboDetail({ ids, nameOf, onChanged }: { ids: string[]; nameOf: (s: string) => string; onChanged: () => void }) {
  const { toast, prefs } = useApp();
  const critName = useCriteriaName();
  const usd = useMoney().fmt;
  const { data: c, error, reload } = useApi<ComboPanel>(`/api/combinations/panel?ids=${idsKey(ids)}`, [idsKey(ids)]);
  const [name, setName] = useState("");
  const [ask, setAsk] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  if (error) return <ErrorPanel error={error} />;
  if (!c) return <Card><Loading label="Combining the recorded trades…" kind="chart" /></Card>;
  const s = c.stats, k = c.kpis;
  const color = new Map(c.member_ids.map((sid, i) => [sid, CHIP[i % CHIP.length]]));
  const p = c.registration.protocol;
  const h = c.holdout;
  const hv = c.holdout_view;
  const canTest = c.registered && !h && !!p && p.tests_left > 0 && p.status === "ACTIVE";
  const start = () => {
    setErr(null);
    api.post<ComboJob>("/api/combinations/holdout", { protocol_id: p!.protocol_id, combo_id: c.combo_id })
      .then((j) => { setJobId(j.job_id); setAsk(false); toast("info", "Combination holdout test started"); })
      .catch((e: ApiError) => { setErr(e); setAsk(false); });
  };
  const crits = c.criteria;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }} data-testid="combo-panel">
      <Card>
        <div className="inline" style={{ flexWrap: "wrap", gap: 6 }}>
          <b style={{ fontSize: 16 }}>{c.member_ids.length} strategies</b>
          {s.survivor ? <Badge tone="ok">Survivor</Badge> : <Badge tone="warn">Not a survivor</Badge>}
          {c.registered && <HoldoutBadge h={h} />}
        </div>
        <div className="small" style={{ marginTop: 6 }}>{c.member_ids.map((sid) => <div key={sid}><span className="chip-dot" style={{ background: color.get(sid) }} />{nameOf(sid)}</div>)}</div>
        {c.window.start && <p className="small muted" style={{ margin: "6px 0 0" }}>Research period {c.window.start.slice(0, 10)} to {c.window.end?.slice(0, 10)} · one prop account, one position at a time</p>}
      </Card>
      <div className="kpis" data-testid="combo-kpis">
        <Kpi label="Net R per trade" value={r(k.expectancy_r)} tone={signCls(k.expectancy_r) as "pos" | "neg" | ""} accent />
        <Kpi label="Trades per week" value={n(k.trades_per_week, 1)} sub={`${k.trade_count.toLocaleString()} trades · ${c.skipped_total.toLocaleString()} skipped (overlap)`} />
        <Kpi label="Losing months" value={s.negative_months} />
        <Kpi label="Total net R" value={n(k.net_r, 1)} tone={signCls(k.net_r) as "pos" | "neg" | ""} sub={`${usd(k.net_usd)} recorded`} />
        <Kpi label="Max drawdown" value={usd(k.max_drawdown_usd)} sub={`${n(k.max_drawdown_r, 1)} R · longest losing streak ${k.max_loss_streak ?? "—"}`} />
        <Kpi label="Evaluations passed" value={s.pass_pct == null ? "—" : `${n(s.pass_pct, 0)}%`}
          sub={`${s.passed} of ${s.passed + s.failed} monthly starts${s.undecided ? ` · ${s.undecided} undecided at the end` : ""}`} />
        <Kpi label="Days to pass" value={days(s.median_days_to_pass)} sub="median trading days" />
        <Kpi label="Days to first payout" value={days(s.median_days_to_payout)} sub={`median · ${s.paid} start${s.paid === 1 ? "" : "s"} reached one`} />
      </div>
      <Card title="Combination vs each strategy alone" testId="combo-compare">
        <TableWrap><table className="dense"><thead><tr><th>Measure</th><th className="r">Combination</th>
          {c.members.map((m) => <th key={m.strategy_id} className="r" title={nameOf(m.strategy_id)}><span className="chip-dot" style={{ background: color.get(m.strategy_id) }} />
            {m.strategy_id === c.best_single ? "Best alone" : "Alone"}</th>)}</tr></thead>
          <tbody>{crits.map((x) => <tr key={x.key}><td>{x.label}{x.weight > 1 && <span className="muted small"> ×{x.weight}</span>}</td>
            <td className="r num"><b>{fmtCrit(x.key, (s as unknown as Record<string, number | null>)[x.key], usd)}</b></td>
            {c.members.map((m) => <td key={m.strategy_id} className="r num">{fmtCrit(x.key, m.own[x.key], usd)}</td>)}</tr>)}</tbody></table></TableWrap>
        <p className="small muted" style={{ marginBottom: 0 }}>Trades per week counts up to {n(c.tpw_cap, 1)} (Settings). Evaluation speed: one simulated
          evaluation from the start of every month, under {critName} (Settings → Prop firm pass criteria).</p>
      </Card>
      <Card title={<>Equity curve {hv && <Scope kind="holdout" />}</>} testId="combo-equity">
        <StepTimeChart points={c.curve} start={c.window.start ?? undefined} end={hv?.window.end ?? c.window.end ?? undefined} testId="combo-equity-chart"
          band={hv && hv.window.start && hv.window.end ? { from: hv.window.start, to: hv.window.end, label: "Holdout test", seriesLabel: "Holdout",
            points: hv.curve } : undefined} />
        <p className="small muted" style={{ marginBottom: 0 }}>Running total of net R of the combined trades. {c.merge_note}</p>
      </Card>
      {!!c.years.length && <Card title="Results by year" testId="combo-years">
        <YearTable years={c.years} dataset={c.window.start && c.window.end ? { start: c.window.start, end: c.window.end } as never : undefined}
          holdout={hv && p ? { from: hv.window.start ?? "", to: hv.window.end ?? "", trading_dates: p.holdout_trading_dates, evaluated: true, run_id: null,
            years: hv.years, curve: null } : undefined} />
        <p className="small muted" style={{ marginBottom: 0 }}>Click a year to see its months (New York exit dates).</p>
      </Card>}
      <Card title="Members" testId="combo-members">
        <TableWrap><table className="dense"><thead><tr><th>Strategy</th><th className="r">Trades</th><th className="r">Taken</th><th className="r">Skipped</th>
          <th className="r">Net R of taken trades</th></tr></thead>
          <tbody>{c.members.map((m) => <tr key={m.strategy_id}><td><span className="chip-dot" style={{ background: color.get(m.strategy_id) }} />{nameOf(m.strategy_id)}</td>
            <td className="r num">{m.trades}</td><td className="r num">{m.kept}</td><td className="r num">{m.skipped}</td>
            <td className={`r num ${signCls(m.net_r_kept)}`}>{n(m.net_r_kept, 1)}</td></tr>)}</tbody></table></TableWrap>
      </Card>
      <Card title={<>Holdout test <Scope kind="holdout" /></>} testId="combo-holdout">
        {h ? <>
          <p><HoldoutBadge h={h} />{h.trade_count != null && <span className="small"> · {h.trade_count} combined trades, {n(h.net_r, 1)} R</span>}</p>
          {h.criteria && <ul className="small">{Object.entries(h.criteria).map(([key, v]) => <li key={key} className={v.met ? "pos" : "neg"}>
            {v.met ? "✓" : "✗"} {CRIT_LABEL[key] ?? humanize(key)}</li>)}</ul>}
          <p className="small muted" style={{ marginBottom: 0 }}>Judged by the research protocol's rules fixed in advance: “criteria met” or “not met”, never “approved”.
            The members' holdout runs are kept separate and are not their own holdout results.</p>
        </> : c.registered ? <>
          <p className="small">Registered. Testing uses one of the {p?.tests_budget ?? 10} combination holdout tests ({p?.tests_left ?? 0} left): every
            member is backtested on exactly the holdout dates, with 100 random-entry comparisons each, and the combined trades are judged once.
            Afterwards its members can no longer be holdout-tested on their own.</p>
          <Button kind="primary" small disabled={!canTest || !!jobId} onClick={() => setAsk(true)} testId="combo-holdout-start">Holdout-test this combination</Button>
        </> : <p className="small muted" style={{ margin: 0 }}>{c.registration.can_register ? "Not registered. Tick it in the list on the left and press Register to make it holdout-testable."
          : c.registration.protocol ? "Not registered: this research protocol's combinations are already registered." : "No active research protocol."}</p>}
        {jobId && <LiveComboHoldout jobId={jobId} onDone={() => { setJobId(null); reload(); onChanged(); }} />}
        {err && <ErrorPanel error={err} title="Not started" />}
      </Card>
      <Card title="Save this combination">
        <div className="inline">
          <input className="input" placeholder="Name" value={name} maxLength={80} aria-label="combination name" data-testid="combo-name"
            onChange={(e: { target: HTMLInputElement }) => setName(e.target.value)} />
          <Button small disabled={!name.trim()} testId="combo-save"
            onClick={() => api.post("/api/combinations/saved", { name, strategy_ids: c.member_ids }).then(() => { toast("ok", "Saved"); setName(""); onChanged(); })
              .catch((e: ApiError) => setErr(e))}>Save</Button>
        </div>
      </Card>
      <TechDetails testId="combo-technical" rows={[["Combination ID", <Mono>{c.combo_id ?? "—"}</Mono>], ["Merge rule", <Mono>{c.merge_rule}</Mono>],
        ["Research protocol", <Mono>{c.protocol_id ?? "—"}</Mono>], ...(prefs.show_ids ? c.member_ids.map((sid) => [nameOf(sid), <Mono>{sid}</Mono>] as [string, ReactNode]) : [])]} />
      <Confirm open={ask} title="Use one combination holdout test?" confirmLabel="Start holdout test" onConfirm={start} onCancel={() => setAsk(false)}>
        This is permanent: the test is used up, this combination can never be holdout-tested again, its members can no longer be
        holdout-tested on their own, and the result is recorded whatever it is.
      </Confirm>
    </div>
  );
}

function fmtCrit(key: string, v: number | null | undefined, usd: (x: number | null | undefined) => string): string {
  if (v == null) return "—";
  if (key === "max_drawdown_usd") return usd(v);
  if (key === "pass_pct") return `${n(v, 0)}%`;
  if (key === "expectancy_r") return r(v);
  if (key.startsWith("median_days")) return n(v, 0);
  if (key === "tpw_scored") return n(v, 1);
  return n(v, 0);
}

function LiveComboHoldout({ jobId, onDone }: { jobId: string; onDone: () => void }) {
  const [job, setJob] = useState<ComboJob | null>(null);
  useEffect(() => {
    let live = true, t = 0;
    const poll = () => api.get<ComboJob>(`/api/combinations/jobs/${jobId}`).then((j) => {
      if (!live) return;
      setJob(j);
      if (FINAL.has(j.state)) onDone(); else t = window.setTimeout(poll, 2000);
    }).catch(() => undefined);
    poll();
    return () => { live = false; window.clearTimeout(t); };
  }, [jobId]);  // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div data-testid="combo-holdout-live" style={{ marginTop: 8 }}>
      <div className="progress" aria-label="holdout test progress"><span style={{ width: job && FINAL.has(job.state) ? "100%" : "40%" }} /></div>
      <p className="small muted">{job ? humanize(job.state) : "Starting"}: backtesting every member on the holdout dates with its random-entry
        comparisons. This can take several minutes per strategy.</p>
      {job?.error && <Banner tone="error">{plainProse(job.error)}</Banner>}
    </div>
  );
}

