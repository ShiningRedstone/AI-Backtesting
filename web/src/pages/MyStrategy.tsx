/* My strategy (ADR-93): BP Blake's model rebuilt with every rule as a setting, its own backtests (own protocol), every
   trade documented with charts, the holdout review with the trader's take / skip decisions, and GitHub uploads. */
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { ApiError, viewCache } from "../api/client";
import { my } from "../api/my";
import type { Candle, ChallengeSummary, Decision, EsStatus, ExportResult, Explanation, HoldoutAllowance, HoldoutSlot, MyJob, Overview, PlanResult, PlanVariant, Report, ReportRow, ReviewView,
  SettingDef, SettingsPayload, SetupReviews, SetupStats, SetupView, TradeDoc, TradeRow } from "../api/my";
import { useApi, useApp } from "../app/context";
import { go, href, useRoute } from "../app/router";
import { useMoney } from "../app/money";
import { profileLabel } from "../app/labels";
import { CandleChart, nyTime } from "../components/candles";
import type { Marker, PriceBox, PriceLine } from "../components/candles";
import { StepTimeChart } from "../components/charts";
import { Markdown } from "../components/markdown";
import { Badge, Banner, Button, Card, Checkbox, Confirm, Empty, ErrorPanel, Field, Kpi, NumberInput, PageSkeleton, Select,
  TableWrap, Tabs, TechDetails, TextInput, bytes, n, pct, r, signCls } from "../components/ui";
import summaryText from "../content/my_strategy.md";

const TF_MIN: Record<string, number> = { "1m": 1, "2m": 2, "3m": 3, "4m": 4, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1D": 1440 };
const sec = (iso: string | null | undefined) => (iso ? Math.floor(Date.parse(iso) / 1000) : NaN);
const px = (v: number | null | undefined) => (typeof v === "number" && Number.isFinite(v)
  ? v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "–");
const dirWord = (d: number) => (d > 0 ? "Long" : "Short");
const NY_DATE = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "America/New_York" });
const day = (iso: string | null | undefined) => (iso ? NY_DATE.format(Date.parse(iso)) : "–");

export const CHECK_LABEL: Record<string, string> = {
  bias_clear: "Clear bias", draw_on_liquidity: "Draw on liquidity", discount_premium: "Discount / premium",
  key_fvg: "Key level: fair value gap", key_cisd: "Key level: CISD", key_rejection_block: "Key level: rejection block",
  key_bpr: "Key level: BPR", fresh_gap_or_swept_inside: "Fresh gap or swept inside", several_key_levels: "Several key levels",
  liquidity_swept: "Liquidity swept", no_equal_lows_highs: "No equal lows / highs", open_manipulation: "9:30 open manipulated",
  inversion_gap: "Inversion gap (IFG)", highest_timeframe_gap: "Highest-timeframe gap", displacement: "Displacement",
  smt: "SMT with ES", target_at_liquidity: "Target at liquidity", stacked_liquidity_at_target: "Stacked liquidity at target",
  not_choppy: "Not choppy",
};
const STAT_LABEL: Record<string, string> = {
  days: "Trading days looked at", days_no_bias: "No clear bias", days_no_draw: "No draw on liquidity",
  days_with_bias_and_draw: "Bias and draw found", days_no_key_level: "No key level near price", days_choppy: "Skipped as choppy",
  days_skipped_weekday: "Skipped weekday", days_bias_direction_not_allowed: "Bias in a blocked direction",
  days_short_blocked_near_high: "Shorts blocked near the high", setups: "Manipulation into a key level",
  setup_rejected_leg_too_small: "Leg too small", setup_rejected_not_in_discount_premium: "Not in discount / premium",
  setup_rejected_no_sweep: "No liquidity swept", setup_rejected_equal_extremes: "Formed equal lows / highs",
  setup_rejected_no_open_manipulation: "Judas: no move beyond the 9:30 open", setups_broken: "Price broke lower / higher (setup reset)",
  setups_no_confirmation_in_time: "No confirmation in time", setups_level_invalidated: "Key level closed through",
  confirmation_rejected_no_displacement: "Confirmation without displacement", signal_rejected_stop_too_wide: "Stop too wide",
  signal_rejected_target: "No acceptable target", signal_rejected_quality: "Confluence score too low",
  signals: "Entry signals", signals_not_filled_in_own_tracking: "Limit order not filled",
  signals_declined_in_review: "Declined in the holdout review", signals_flipped: "Flipped (traded the other way)",
  setup_rejected_no_smt: "No SMT divergence with ES", setup_rejected_smt_unknown: "SMT unknown (ES minutes missing)",
};
export const MODEL = (m?: string) => (m === "judas" ? "Judas swing" : m === "ny_4step" ? "NY four-step" : m ?? "–");

export function useJob(onDone?: (j: MyJob) => void): [MyJob | null, (j: MyJob) => void] {
  const [job, setJob] = useState<MyJob | null>(null);
  useEffect(() => {
    if (!job || job.state !== "running") return;
    let live = true;
    const t = window.setTimeout(() => {
      my.job(job.job_id).then((j) => {
        if (!live) return;
        setJob(j);
        if (j.state !== "running") { viewCache.clear(); onDone?.(j); }
      }).catch(() => { if (live) setJob(null); });
    }, 1000);
    return () => { live = false; window.clearTimeout(t); };
  }, [job]); // eslint-disable-line react-hooks/exhaustive-deps
  return [job, setJob];
}

export function JobLine({ job }: { job: MyJob | null }) {
  if (!job) return null;
  if (job.state === "running") return <Banner tone="info"><span className="spinner" /> {job.step}…</Banner>;
  if (job.state === "failed") return <Banner tone="error">{job.error?.message ?? "Failed"}</Banner>;
  return null;
}

export function PageHead({ title, children }: { title: string; children?: ReactNode }) {
  return <header className="page-head"><div><h1>{title}</h1></div><div className="actions">{children}</div></header>;
}

// =============================================================================================== overview
export function MyOverviewPage() {
  const { data, error } = useApi<Overview>(my.overviewUrl);
  const latest = data?.backtests?.[0];
  return (
    <div className="page" data-testid="my-overview-page">
      <PageHead title="My strategy">
        <a className="btn btn-secondary" href={href("/my-settings")}>Settings</a>
        <a className="btn btn-primary" href={href("/my-backtest")}>Backtest</a>
      </PageHead>
      {error && <ErrorPanel error={error} />}
      {data && !data.protocol.ready && <Banner tone="warn">{data.protocol.problem}</Banner>}
      {data && <StatusKpis data={data} latest={latest} />}
      <Card title="BP Blake's strategy: summary" testId="my-summary">
        <Markdown text={summaryText} />
      </Card>
    </div>
  );
}

function StatusKpis({ data, latest }: { data: Overview; latest?: ReportRow }) {
  const p = data.protocol;
  const m = latest?.metrics;
  return (
    <div className="kpis" data-testid="my-status">
      <Kpi label="Discovery period" value={p.discovery ? `${day(p.discovery.start)} – ${day(p.discovery.end)}` : "–"} />
      <Kpi label="Holdout (locked)" value={p.holdout ? `${day(p.holdout.start)} – ${day(p.holdout.end)}` : "–"} />
      <Kpi label="Settings tried" value={`${p.trials_used ?? 0} of ${p.trial_budget ?? 0}`} meter={(p.trials_used ?? 0) / (p.trial_budget || 1)} />
      <Kpi label="Holdout looks used" value={`${p.holdout_looks_used ?? 0} of ${p.holdout_looks ?? 1}`} />
      {m && <Kpi label="Latest backtest: win rate" value={pct(m.win_rate)} sub={`${n(m.trades_per_week, 1)} trades / week`} accent />}
      {m && <Kpi label="Latest backtest: losing months" value={`${m.months_losing ?? 0} of ${m.months_total ?? 0}`}
        tone={(m.months_losing ?? 0) > 0 ? "neg" : "pos"} />}
    </div>
  );
}

// =============================================================================================== settings
/** ES reference prices for SMT divergence (ADR-95): status, and import of the Dukascopy ES file (never a dataset). */
function EsDataCard() {
  const { data, error, reload } = useApi<EsStatus>(my.esUrl);
  const { toast } = useApp();
  const [path, setPath] = useState(String.raw`C:\NQ_DATA\es_1min_5years.csv`);
  const [confirmed, setConfirmed] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [job, setJob] = useJob((j) => { if (j.state === "completed") { toast("ok", "ES data imported"); setConfirmed(false); } reload(); });
  const start = async () => {
    setErr(null);
    try { setJob(await my.importEs(path, confirmed)); } catch (e) { setErr(e as ApiError); }
  };
  const running = job?.state === "running";
  return (
    <Card title="ES data for SMT" testId="my-es" actions={data?.imported ? <Badge tone="ok">imported</Badge> : <Badge tone="warn">not imported</Badge>}>
      {error && <ErrorPanel error={error} />}
      {data?.imported && (
        <dl className="kv" data-testid="my-es-status">
          <div className="kv-row"><dt>Market</dt><dd>{data.identity.instrument} · {data.identity.description} · {data.identity.timeframe} {data.identity.price_basis.toUpperCase()}</dd></div>
          <div className="kv-row"><dt>Period</dt><dd>{day(data.first_bar_open_utc)} – {day(data.last_bar_open_utc)} · {(data.bars ?? 0).toLocaleString("en-US")} one-minute bars</dd></div>
          <div className="kv-row"><dt>File</dt><dd className="mono small">{data.source_file}</dd></div>
          <div className="kv-row"><dt>Imported</dt><dd>{nyTime(sec(data.imported_at))}{data.es_id ? ` · ${data.es_id}` : ""}</dd></div>
        </dl>)}
      <p className="muted small">Used only to compare highs and lows with NQ for SMT divergence; it is never traded and is not a
        research dataset. Each check uses ES prices up to the bar being decided, never later ones. If ES has no bar for a minute
        the check needs, SMT counts as unknown, never as yes.</p>
      <div className="inline">
        <Field label={data?.imported ? "Import a new file (replaces the current one)" : "ES file (Dukascopy download)"}>
          <TextInput value={path} onChange={setPath} mono ariaLabel="ES file path" testId="my-es-path" /></Field>
      </div>
      <Checkbox checked={confirmed} onChange={setConfirmed} testId="my-es-confirm"
        label="This file is Dukascopy USA500.IDX/USD (S&P 500 index CFD), 1-minute BID candles, timestamps in UTC at the bar open" />
      <div className="inline" style={{ marginTop: 8 }}>
        <Button kind={data?.imported ? "secondary" : "primary"} onClick={start} busy={running} busyLabel="Importing…"
          disabled={!confirmed || !path.trim()} testId="my-es-import">Import ES data</Button>
      </div>
      <JobLine job={job} />
      <ErrorPanel error={err} />
    </Card>
  );
}

export function MySettingsPage() {
  const { data, error, reload } = useApi<SettingsPayload>(my.settingsUrl);
  const { toast } = useApp();
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [filter, setFilter] = useState("");
  const [onlyChanged, setOnlyChanged] = useState(false);
  useEffect(() => { if (data) setDraft({ ...data.resolved }); }, [data]);
  if (error) return <div className="page"><ErrorPanel error={error} /></div>;
  if (!data || !draft) return <div className="page"><PageSkeleton layout="settings" label="Loading settings" /></div>;
  const defs = data.schema.settings;
  const dirty = defs.some((d) => draft[d.key] !== data.resolved[d.key]);
  const changedFromDefault = (d: SettingDef) => draft[d.key] !== d.default;
  const f = filter.trim().toLowerCase();
  const visible = (d: SettingDef) => (!f || `${d.label} ${d.help} ${d.key}`.toLowerCase().includes(f)) && (!onlyChanged || changedFromDefault(d));
  const save = async (values: Record<string, unknown>) => {
    setSaving(true); setErr(null);
    try {
      const ov: Record<string, unknown> = {};
      for (const d of defs) if (values[d.key] !== d.default) ov[d.key] = values[d.key];
      await my.saveSettings(ov);
      toast("ok", "Settings saved");
      reload();
    } catch (e) { setErr(e as ApiError); } finally { setSaving(false); }
  };
  return (
    <div className="page" data-testid="my-settings-page">
      <PageHead title="Strategy settings">
        <Button onClick={() => setDraft({ ...data.resolved })} disabled={!dirty}>Discard changes</Button>
        <Button onClick={() => { const d: Record<string, unknown> = {}; for (const x of defs) d[x.key] = x.default; setDraft(d); }}>
          Reset all to defaults</Button>
        <Button kind="primary" onClick={() => save(draft)} busy={saving} disabled={!dirty} testId="my-settings-save">Save settings</Button>
      </PageHead>
      <p className="muted">Every rule of the strategy is a setting. Changed settings are marked; a backtest always uses the saved
        settings. {defs.length} settings in {data.schema.groups.length} groups.</p>
      {data.problems.length > 0 && <Banner tone="warn">The saved settings file has problems: {data.problems.join("; ")}</Banner>}
      <ErrorPanel error={err} />
      <EsDataCard />
      <div className="filterbar">
        <TextInput value={filter} onChange={setFilter} placeholder="Find a setting…" ariaLabel="Find a setting" />
        <Checkbox checked={onlyChanged} onChange={setOnlyChanged} label="Only changed from default" />
      </div>
      <div className="my-settings-grid">
        {data.schema.groups.map((g) => {
          const rows = defs.filter((d) => d.group === g.id && visible(d));
          if (!rows.length) return null;
          return (
            <Card key={g.id} title={g.label} testId={`my-group-${g.id}`}>
              {rows.map((d) => <SettingRow key={d.key} d={d} value={draft[d.key]} changed={changedFromDefault(d)}
                onChange={(v) => setDraft({ ...draft, [d.key]: v })} />)}
            </Card>
          );
        })}
      </div>
      <TechDetails rows={[["Settings hash", data.settings_hash]]} />
    </div>
  );
}

function SettingRow({ d, value, changed, onChange }: { d: SettingDef; value: unknown; changed: boolean; onChange: (v: unknown) => void }) {
  let ctl: ReactNode;
  if (d.type === "bool") {
    const on = Boolean(value);
    ctl = <button type="button" role="switch" aria-checked={on} aria-label={d.label} disabled={!!d.unavailable}
      className={`switch${on ? " on" : ""}`} onClick={() => onChange(!on)} data-testid={`set-${d.key}`}><span /></button>;
  } else if (d.type === "choice") {
    ctl = <Select value={String(value)} onChange={(v) => onChange(v)} options={(d.options ?? []).map((o) => ({ value: o, label: o.replace(/_/g, " ") }))}
      ariaLabel={d.label} testId={`set-${d.key}`} />;
  } else if (d.type === "time") {
    ctl = <input className="input num" type="time" value={String(value)} aria-label={d.label} data-testid={`set-${d.key}`}
      onChange={(e: { target: HTMLInputElement }) => onChange(e.target.value)} />;
  } else {
    ctl = <NumberInput value={value as number} integer={d.type === "int"} onChange={(v) => onChange(v === undefined ? d.default : v)}
      ariaLabel={d.label} testId={`set-${d.key}`} />;
  }
  return (
    <div className={`my-setting${changed ? " changed" : ""}`}>
      <div className="my-setting-text">
        <div className="my-setting-label">{d.label}{changed && <Badge tone="info">changed</Badge>}
          {d.unavailable && <Badge tone="warn">{d.unavailable}</Badge>}</div>
        {d.help && <div className="muted small">{d.help}</div>}
        <div className="faint small">Default: {String(d.default)}{d.min !== undefined ? ` · ${d.min} to ${d.max}` : ""}
          {d.source ? ` · ${d.source}` : ""}</div>
      </div>
      <div className="my-setting-ctl">{ctl}</div>
    </div>
  );
}

// =============================================================================================== backtest
export function MyBacktestPage() {
  const route = useRoute();
  const { data, error, reload } = useApi<Overview>(my.overviewUrl);
  const selected = route.query.get("r") ?? data?.backtests?.[0]?.id ?? null;
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [label, setLabel] = useState("");
  const [err, setErr] = useState<ApiError | null>(null);
  const [job, setJob] = useJob((j) => {
    reload();
    const id = (j.result as { id?: string } | null)?.id;
    if (j.state === "completed" && id) go(`/my-backtest?r=${id}`);
  });
  useEffect(() => { if (data?.job && !job) setJob(data.job); }, [data?.job]); // eslint-disable-line react-hooks/exhaustive-deps
  const run = async () => {
    setErr(null);
    try { setJob(await my.startBacktest({ start: start || null, end: end || null, label })); } catch (e) { setErr(e as ApiError); }
  };
  const p = data?.protocol;
  const running = job?.state === "running";
  return (
    <div className="page" data-testid="my-backtest-page">
      <PageHead title="Backtest My strategy" />
      {error && <ErrorPanel error={error} />}
      {p && !p.ready && <Banner tone="warn">{p.problem}</Banner>}
      {p?.ready && p.config_ok === false && <Banner tone="error">The research settings differ from the protocol's. Restore them under
        Run backtest → Research runs before testing.</Banner>}
      <Card title="Run a backtest" testId="my-run-card">
        <p className="muted">Uses the saved settings on the discovery period ({p?.discovery ? `${day(p.discovery.start)} – ${day(p.discovery.end)}` : "–"}),
          the same engine, BID/ASK costs and MNQ sizing as every backtest, with the lookahead check. Each new settings combination
          counts as one try ({p?.trials_used ?? 0} of {p?.trial_budget ?? 300} used). Leave the dates empty for the whole period.</p>
        <div className="inline">
          <Field label="From (optional)"><input className="input" type="date" value={start} aria-label="From"
            onChange={(e: { target: HTMLInputElement }) => setStart(e.target.value)} /></Field>
          <Field label="To (optional)"><input className="input" type="date" value={end} aria-label="To"
            onChange={(e: { target: HTMLInputElement }) => setEnd(e.target.value)} /></Field>
          <Field label="Name (optional)"><TextInput value={label} onChange={setLabel} placeholder="e.g. defaults" /></Field>
          <Button kind="primary" onClick={run} busy={running} busyLabel="Running…" disabled={!p?.ready} testId="my-run">Run backtest</Button>
        </div>
        <JobLine job={job} />
        <ErrorPanel error={err} />
      </Card>
      {data && <ReportList rows={data.backtests} selected={selected} onOpen={(id) => go(`/my-backtest?r=${id}`)} onChanged={reload} editable
        title="Backtests" testId="my-reports" />}
      {selected ? <ReportView id={selected} title={data?.backtests.find((b) => b.id === selected)?.label || undefined} />
        : data && <Empty>No backtest yet. Run the first one above.</Empty>}
      {data && <PlansCard data={data} onChange={reload} />}
    </div>
  );
}

const BLAKE = { win_rate: 0.7, rr: "1:1 to 1:3", tpw: "3–5 (1 a day)" };

export function ReportView({ id, title }: { id: string; title?: string }) {
  const { data, error } = useApi<Report>(my.reportUrl(id), [id]);
  const money = useMoney();
  const curve = useMemoCurve(data?.trades ?? []);
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <PageSkeleton layout="overview" label="Loading the backtest" />;
  const m = data.metrics;
  const isHoldout = data.kind.startsWith("holdout");
  return (
    <>
      <Card title={title ?? (data.label || (isHoldout ? "Holdout" : "Backtest"))} testId="my-report"
        actions={<a className="btn btn-secondary btn-sm" href={href(`/my-trades/${data.id}`)}>All {data.trade_count} trades</a>}>
        <div className="muted small">{day(data.window.start)} – {day(data.window.end)} · {isHoldout ? "Holdout" : "Discovery"}
          {" "}· created {nyTime(sec(data.created_at))}{data.causality_passed ? " · lookahead check passed" : ""}</div>
        <div className="kpis">
          <Kpi label="Trades" value={data.trade_count} sub={`${n(m.trades_per_week, 2)} per week · ${m.sample_label ?? ""}`} />
          <Kpi label="Win rate" value={pct(m.win_rate)} sub={`Blake: ~${pct(BLAKE.win_rate, 0)}`} accent />
          <Kpi label="Average winner" value={r(m.avg_winner_r, 2)} sub={`planned ${n(m.avg_planned_rr, 2)} R · Blake ${BLAKE.rr}`} />
          <Kpi label="Expectancy" value={r(m.expectancy_r)} tone={signCls(m.expectancy_r)} sub="per trade, after costs" />
          <Kpi label="Net result" value={money.fmt(m.net_usd ?? null)} tone={signCls(m.net_usd)} sub={r(m.net_r, 1)} />
          <Kpi label="Losing months" value={`${m.months_losing ?? 0} of ${m.months_total ?? 0}`} tone={(m.months_losing ?? 0) > 0 ? "neg" : "pos"} />
          <Kpi label="Profit factor" value={n(m.profit_factor, 2)} sub={`max drawdown ${n(m.max_drawdown_r, 1)} R`} />
          <ChallengeKpi data={data} money={money.fmt} />
        </div>
        {curve.length > 0 && <StepTimeChart points={curve} start={data.window.start} end={data.window.end} label="Cumulative net R" testId="my-curve" />}
      </Card>
      <ChallengeCard data={data} money={money.fmt} />
      <div className="grid-cards">
        <Card title="Months" testId="my-months">
          <TableWrap><table className="dense">
            <thead><tr><th>Month</th><th className="num">Trades</th><th className="num">Wins</th><th className="num">Net R</th><th className="num">Net</th></tr></thead>
            <tbody>{data.monthly.map((x) => (
              <tr key={x.month}><td>{x.month}</td><td className="num">{x.trades}</td><td className="num">{x.wins}</td>
                <td className={`num ${signCls(x.net_r)}`}>{r(x.net_r, 2)}</td><td className={`num ${signCls(x.net_usd)}`}>{money.fmt(x.net_usd)}</td></tr>))}
            </tbody></table></TableWrap>
        </Card>
        <Card title="Trades per week" testId="my-weeks">
          {m.weekly && <dl className="kv">
            {([["Weeks tested", m.weekly.weeks], ["Average per week", n(m.weekly.mean, 2)], ["Weeks with 3–5 trades", m.weekly.weeks_3_to_5],
              ["Weeks with no trade", m.weekly.weeks_with_0], ["Weeks with more than 5", m.weekly.weeks_above_5], ["Most in one week", m.weekly.max]] as [string, ReactNode][])
              .map(([k, v]) => <div key={k} className="kv-row"><dt>{k}</dt><dd>{v}</dd></div>)}
          </dl>}
          <h3 className="small-head">Exits</h3>
          <dl className="kv">{Object.entries(m.exit_reasons ?? {}).map(([k, v]) => (
            <div key={k} className="kv-row"><dt>{exitWord(k)}</dt><dd>{v}</dd></div>))}</dl>
        </Card>
        <Card title="Why setups did or did not trade" testId="my-rule-stats">
          <dl className="kv">{Object.entries(data.rule_stats).sort((a, b) => b[1] - a[1]).map(([k, v]) => (
            <div key={k} className="kv-row"><dt>{STAT_LABEL[k] ?? k}</dt><dd>{v}</dd></div>))}</dl>
        </Card>
        <Card title="Settings of this backtest">
          {Object.keys(data.settings_changed).length === 0 ? <p className="muted">All defaults.</p> :
            <dl className="kv">{Object.entries(data.settings_changed).map(([k, v]) => (
              <div key={k} className="kv-row"><dt>{k}</dt><dd>{String(v)}</dd></div>))}</dl>}
          {data.decisions && <p className="muted small">{Object.values(data.decisions).filter((x) => x.take).length} taken,{" "}
            {Object.values(data.decisions).filter((x) => !x.take).length} skipped by you.</p>}
          <TechDetails rows={[["Report", data.id], ["Run", data.run_id ?? ""], ["Trial", data.trial_id ?? ""], ["Settings hash", data.settings_hash]]} />
        </Card>
      </div>
    </>
  );
}

/** ADR-101: the prop challenge chain of a report: fail -> next challenge, pass -> funded payouts, every fee deducted. */
export function ChainLine({ c, money }: { c: ChallengeSummary | { error: string }; money: (v: number | null) => string }) {
  if ("error" in c && c.error) return <span className="muted small">{c.error}</span>;
  const x = c as ChallengeSummary;
  return (
    <span className="small" title={`${x.challenges} challenges bought · ${x.payouts} payouts · fees ${money(x.fees_total)}`}>
      {x.passes} pass{x.passes === 1 ? "" : "es"} · {x.fails} fail{x.fails === 1 ? "" : "s"} ·{" "}
      {x.net == null ? <span className="muted">fees not set</span> : <b className={signCls(x.net)}>{money(x.net)}</b>}
    </span>
  );
}

function ChallengeKpi({ data, money }: { data: Report; money: (v: number | null) => string }) {
  const prof = data.criteria_profile ?? "LUCID_LUCIDFLEX_50K";
  const c = data.challenge?.profiles?.[prof] as ChallengeSummary | undefined;
  if (!c || "error" in c && c.error) {
    return <Kpi label={`Prop (${data.challenge?.names?.[prof] ?? profileLabel(prof)})`} value={data.prop?.evaluation ?? "–"}
      sub={data.challenge ? "challenge chain unavailable" : "run it again for the challenge chain"} />;
  }
  return <Kpi label={`Prop challenges, ${data.challenge?.names?.[prof] ?? profileLabel(prof)}`} value={`${c.passes} passed`}
    sub={`${c.fails} failed · ${c.net == null ? "set the fees in Settings for the net" : `net ${money(c.net)} after ${money(c.fees_total)} fees`}`}
    testId="my-chain-kpi" />;
}

function ChallengeCard({ data, money }: { data: Report; money: (v: number | null) => string }) {
  const v = data.challenge;
  if (!v) return null;
  if (v.error) return <Card title="Prop challenges"><p className="muted small">Not available: {v.error}</p></Card>;
  const rows = Object.entries(v.profiles ?? {});
  return (
    <Card title="Prop challenges, one after another" testId="my-chain">
      <p className="small muted">The backtest's trades go through challenge after challenge: a failed evaluation → the next challenge (reset fee, or a new
        evaluation when no reset fee is set); a passed one → activation fee, then the funded account collects every payout until it is lost or ends →
        a new evaluation. Each challenge is sized from its own starting balance. Fees from Settings → prop account fees (discount applied).
        Default assumed rules; a historical result, not a forecast.</p>
      <TableWrap><table className="dense">
        <thead><tr><th>Account</th><th className="num">Challenges</th><th className="num">Failed</th><th className="num">Passed</th>
          <th className="num">Funded lost</th><th className="num">Payouts</th><th className="num">Average payout per pass</th><th className="num">Your payouts</th>
          <th className="num">Fees</th><th className="num">Net</th></tr></thead>
        <tbody>{rows.map(([pid, c]) => {
          const name = v.names?.[pid] ?? profileLabel(pid);
          if ("error" in c && c.error) return <tr key={pid}><td>{name}</td><td colSpan={9} className="muted small">{c.error}</td></tr>;
          const x = c as ChallengeSummary;
          return (
            <tr key={pid} className={pid === data.criteria_profile ? "selected" : ""}>
              <td>{name}{x.open_at_end ? <span className="muted small"> · still open at the end</span> : null}
                {x.incompatible ? <span className="muted small"> · stopped: {x.stopped}</span> : null}</td>
              <td className="num">{x.challenges}</td><td className="num">{x.fails}</td><td className="num">{x.passes}</td>
              <td className="num">{x.funded_lost}</td><td className="num">{x.payouts}</td><td className="num">{money(x.avg_payout_per_pass)}</td>
              <td className="num">{money(x.trader_payouts)}</td><td className="num">{x.fees_complete ? money(x.fees_total) : <span className="muted">not set</span>}</td>
              <td className={`num ${signCls(x.net)}`}><b>{x.net == null ? "–" : money(x.net)}</b></td>
            </tr>);
        })}</tbody></table></TableWrap>
    </Card>
  );
}

function useMemoCurve(trades: TradeRow[]) {
  return useMemo(() => {
    let c = 0;
    return [...trades].sort((a, b) => a.exit_ts.localeCompare(b.exit_ts)).map((t) => { c += t.net_r; return { t: t.exit_ts, v: c }; });
  }, [trades]);
}

export const exitWord = (k: string) => ({ TARGET: "Take profit", TARGET_GAP: "Take profit (gap)", STOP: "Stop loss", STOP_GAP: "Stop loss (gap)",
  TRAIL_STOP: "Moved stop (breakeven / trailing)", TRAIL_STOP_GAP: "Moved stop (gap)", SIGNAL: "Closed at the set time",
  SESSION_CLOSE: "Session close", END_OF_DATA: "End of data" } as Record<string, string>)[k] ?? k;

/** Backtests / reports as a table: tick boxes, a green check for reports already saved for Claude, one save button. */
function ReportList({ rows: allRows, selected, onOpen, title, testId, onChanged, editable }: {
  rows: ReportRow[]; selected?: string | null; onOpen: (id: string) => void; title: string; testId?: string; onChanged?: () => void;
  editable?: boolean;
}) {
  const { toast } = useApp();
  const money = useMoney();
  const [favOnly, setFavOnly] = useState(false);
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null);
  const [meta, setMeta] = useState<Record<string, { favorite?: boolean; label?: string }>>({});
  const rows = allRows.map((b) => ({ ...b, ...(meta[b.id] ?? {}) })).filter((b) => !favOnly || b.favorite);
  const saveMeta = async (id: string, patch: { favorite?: boolean; label?: string }) => {
    setMeta((m) => ({ ...m, [id]: { ...m[id], ...patch } }));
    try { await my.setMeta(id, patch); onChanged?.(); } catch (e) { setErr(e as ApiError); setMeta((m) => { const c = { ...m }; delete c[id]; return c; }); }
  };
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [candles, setCandles] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [saved, setSaved] = useState<ExportResult | null>(null);
  const [savedNow, setSavedNow] = useState<Set<string>>(new Set());
  const toggle = (id: string) => { const t = new Set(ticked); if (t.has(id)) t.delete(id); else t.add(id); setTicked(t); };
  const save = async () => {
    setBusy(true); setErr(null);
    try {
      const res = await my.exportReports([...ticked], candles);
      setSaved(res);
      setSavedNow(new Set([...savedNow, ...res.reports]));
      setTicked(new Set());
      toast("ok", `Saved ${res.file}`);
    } catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  };
  const open = async () => { try { await my.openExports(); } catch (e) { setErr(e as ApiError); } };
  const all = rows.length > 0 && rows.every((b) => ticked.has(b.id));
  return (
    <Card title={title} testId={testId} actions={
      <div className="inline">
        {editable && <Checkbox checked={favOnly} onChange={setFavOnly} label="Favourites only" testId="my-fav-only" />}
        <Checkbox checked={candles} onChange={setCandles} label="Include candles (bigger file)" />
        <Button kind="primary" onClick={save} busy={busy} busyLabel="Saving…" disabled={!ticked.size} testId="my-save-selected">
          Save selected for Claude{ticked.size ? ` (${ticked.size})` : ""}</Button>
      </div>}>
      {saved && (
        <Banner tone="ok" testId="my-saved">Saved <b>{saved.file}</b> ({bytes(saved.bytes)}) in {saved.folder}. Attach this file in the
          chat with Claude. <Button small kind="ghost" onClick={open}>Open folder</Button></Banner>)}
      <ErrorPanel error={err} />
      {!rows.length ? <Empty>No backtest yet.</Empty> : (
        <TableWrap className="my-report-scroll" testId={`${testId}-scroll`}><table className="dense" data-testid={`${testId}-table`}>
          <thead><tr>
            <th style={{ width: 32 }}><input type="checkbox" aria-label="Select all" checked={all}
              onChange={() => setTicked(all ? new Set() : new Set(rows.map((b) => b.id)))} /></th>
            {editable && <th title="Favourite">★</th>}<th title="Saved for Claude">Saved</th><th>When</th><th>Name</th><th>Period</th><th className="num">Trades</th>
            <th className="num">Per week</th><th className="num">Win rate</th><th className="num">Net R</th>
            <th className="num">Losing months</th><th>Prop challenges</th></tr></thead>
          <tbody>{rows.map((b) => {
            const done = !!b.exported || savedNow.has(b.id);
            return (
              <tr key={b.id} className={b.id === selected ? "selected" : ""} onClick={() => onOpen(b.id)} style={{ cursor: "pointer" }}>
                <td onClick={(e: { stopPropagation: () => void }) => e.stopPropagation()}>
                  <input type="checkbox" aria-label={`Select ${b.label || b.id}`} checked={ticked.has(b.id)} onChange={() => toggle(b.id)}
                    data-testid={`my-tick-${b.id}`} /></td>
                {editable && <td onClick={(e: { stopPropagation: () => void }) => e.stopPropagation()}>
                  <button type="button" className={`fav-star${b.favorite ? " on" : ""}`} aria-pressed={!!b.favorite} data-testid={`my-fav-${b.id}`}
                    title={b.favorite ? "Remove from favourites" : "Add to favourites"} aria-label={b.favorite ? "remove from favourites" : "add to favourites"}
                    onClick={() => void saveMeta(b.id, { favorite: !b.favorite })}>
                    <svg width="15" height="15" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.8l2.8 5.9 6.4.8-4.7 4.4 1.2 6.4L12 17.2l-5.7 3.1 1.2-6.4-4.7-4.4 6.4-.8z"
                      fill={b.favorite ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" /></svg>
                  </button></td>}
                <td>{done ? <span className="my-saved-check" title={b.exported ? `Saved ${nyTime(sec(b.exported.at))} · ${b.exported.file}` : "Saved"}
                  data-testid={`my-saved-${b.id}`}>✓</span> : ""}</td>
                <td>{nyTime(sec(b.created_at))}</td>
                {editing?.id === b.id ? (
                  <td onClick={(e: { stopPropagation: () => void }) => e.stopPropagation()}>
                    <form className="inline" onSubmit={(e: { preventDefault: () => void }) => { e.preventDefault(); void saveMeta(b.id, { label: editing.text }); setEditing(null); }}>
                      <TextInput value={editing.text} onChange={(t) => setEditing({ id: b.id, text: t })} ariaLabel="New name" testId={`my-rename-input-${b.id}`} />
                      <Button small kind="primary" type="submit" testId={`my-rename-save-${b.id}`}>Save</Button>
                      <Button small kind="ghost" onClick={() => setEditing(null)}>Cancel</Button>
                    </form></td>
                ) : (
                  <td>{b.label || (b.kind?.startsWith("holdout") ? (b.kind === "holdout_mechanical" ? "Holdout - every signal" : "Holdout - your decisions") : "–")}
                    {editable && <button type="button" className="my-rename" title="Rename" aria-label={`Rename ${b.label || b.id}`} data-testid={`my-rename-${b.id}`}
                      onClick={(e: { stopPropagation: () => void }) => { e.stopPropagation(); setEditing({ id: b.id, text: b.label || "" }); }}>✎</button>}</td>
                )}
                <td>{day(b.window.start)} – {day(b.window.end)}</td>
                <td className="num">{b.trade_count}</td><td className="num">{n(b.metrics.trades_per_week, 2)}</td>
                <td className="num">{pct(b.metrics.win_rate)}</td><td className={`num ${signCls(b.metrics.net_r)}`}>{r(b.metrics.net_r, 1)}</td>
                <td className="num">{b.metrics.months_losing ?? 0} / {b.metrics.months_total ?? 0}</td>
                <td>{(() => { const c = b.challenge?.profiles ? Object.values(b.challenge.profiles)[0] : null;
                  return c ? <ChainLine c={c} money={money.fmt} /> : <span className="muted small">{b.prop?.evaluation ?? "–"}</span>; })()}</td>
              </tr>);
          })}</tbody></table></TableWrap>)}
    </Card>
  );
}

function PlansCard({ data, onChange }: { data: Overview; onChange: () => void }) {
  const [text, setText] = useState("");
  const [plan, setPlan] = useState<{ raw: unknown; name?: string; note?: string; variants: PlanVariant[] } | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [job, setJob] = useJob((j) => {
    onChange();
    const id = (j.result as { id?: string } | null)?.id;
    if (j.state === "completed" && id) go(`/my-plan/${id}`);
  });
  const check = async () => {
    setErr(null); setPlan(null);
    let raw: unknown;
    try { raw = JSON.parse(text); } catch { setErr(new ApiError(0, "plan", "That is not a valid test plan (copy the whole block Claude gave you).")); return; }
    try { const d = await my.checkPlan(raw); setPlan({ raw, ...d }); } catch (e) { setErr(e as ApiError); }
  };
  const run = async () => {
    if (!plan) return;
    setErr(null);
    try { setJob(await my.runPlan(plan.raw)); } catch (e) { setErr(e as ApiError); }
  };
  return (
    <Card title="Test plans from Claude" testId="my-plans">
      <p className="muted">Claude can give you a test plan (a list of settings variations) in the chat. Paste it here, check it and run it:
        every variation is backtested on the discovery period and counts as a try. Then tick the plan in the list and save it for Claude.</p>
      <TextInput multiline mono value={text} onChange={(v) => { setText(v); setPlan(null); }} placeholder='{"name": "...", "variants": [...]}'
        ariaLabel="Test plan" testId="my-plan-text" />
      <div className="inline" style={{ marginTop: 8 }}>
        <Button onClick={check} disabled={!text.trim()} testId="my-plan-check">Check plan</Button>
        {plan && <Button kind="primary" onClick={run} busy={job?.state === "running"} busyLabel="Running…" testId="my-plan-run">
          Run {plan.variants.length} backtests</Button>}
      </div>
      {plan && (
        <div className="my-plan">
          <h3>{plan.name ?? "Test plan"}</h3>
          {plan.note && <p className="muted">{plan.note}</p>}
          <ol>{plan.variants.map((v, k) => <li key={k}>{v.label}: {Object.entries(v.overrides).map(([a, b]) => `${a} = ${String(b)}`).join(", ") || "defaults"}</li>)}</ol>
        </div>
      )}
      <JobLine job={job} />
      <ErrorPanel error={err} />
      {data.plans.length > 0 && (
        <TableWrap><table className="dense"><thead><tr><th>Saved</th><th>Plan</th><th>When</th><th className="num">Variations</th></tr></thead>
          <tbody>{data.plans.map((p) => <tr key={p.id} onClick={() => go(`/my-plan/${p.id}`)} style={{ cursor: "pointer" }}>
            <td>{p.exported ? <span className="my-saved-check">✓</span> : ""}</td><td>{p.name}</td><td>{nyTime(sec(p.created_at))}</td>
            <td className="num">{p.variants}</td></tr>)}</tbody>
        </table></TableWrap>)}
    </Card>
  );
}

export function MyPlanResultPage() {
  const route = useRoute();
  const id = route.parts[1];
  const { data, error } = useApi<PlanResult>(id ? my.planResultUrl(id) : null, [id]);
  return (
    <div className="page" data-testid="my-plan-page">
      <PageHead title={data ? `Test plan: ${data.name}` : "Test plan"}>
        {data && <SaveOne id={data.id} done={!!data.exported} />}
        <a className="btn btn-secondary" href={href("/my-backtest")}>Back</a></PageHead>
      {error && <ErrorPanel error={error} />}
      {data && <Card title="Variations">
        <TableWrap><table className="dense"><thead><tr><th>Variation</th><th className="num">Trades</th><th className="num">Per week</th>
          <th className="num">Win rate</th><th className="num">Expectancy R</th><th className="num">Net R</th><th className="num">Losing months</th><th>Prop</th></tr></thead>
          <tbody>{data.variants.map((v, k) => <tr key={k} onClick={() => v.backtest_id && go(`/my-backtest?r=${v.backtest_id}`)} style={{ cursor: "pointer" }}>
            <td>{v.label}{v.error && <span className="neg"> {v.error.message}</span>}</td><td className="num">{v.trade_count ?? "–"}</td>
            <td className="num">{n(v.metrics?.trades_per_week, 2)}</td><td className="num">{pct(v.metrics?.win_rate)}</td>
            <td className={`num ${signCls(v.metrics?.expectancy_r)}`}>{r(v.metrics?.expectancy_r)}</td>
            <td className={`num ${signCls(v.metrics?.net_r)}`}>{r(v.metrics?.net_r, 1)}</td>
            <td className="num">{v.metrics ? `${v.metrics.months_losing ?? 0} / ${v.metrics.months_total ?? 0}` : "–"}</td>
            <td>{v.prop?.evaluation ?? "–"}</td></tr>)}</tbody></table></TableWrap>
      </Card>}
    </div>
  );
}

export function SaveOne({ id, done }: { id: string; done: boolean }) {
  const { toast } = useApp();
  const [res, setRes] = useState<ExportResult | null>(null);
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try { const x = await my.exportReports([id], false); setRes(x); toast("ok", `Saved ${x.file} in ${x.folder}`); }
    catch (e) { toast("error", (e as ApiError).message); } finally { setBusy(false); }
  };
  return <>{(done || res) && <span className="my-saved-check" title="Saved for Claude">✓</span>}
    <Button onClick={save} busy={busy} busyLabel="Saving…">Save for Claude</Button></>;
}

// =============================================================================================== trades
export function MyTradesPage() {
  const route = useRoute();
  return route.parts[1] ? <MyTradeListPage id={route.parts[1]} /> : <MyReportsPage />;
}

function MyReportsPage() {
  const { data, error } = useApi<Overview>(my.overviewUrl);
  return (
    <div className="page" data-testid="my-reports-page">
      <PageHead title="Trades" />
      <p className="muted">Pick a backtest to see all its trades. Holdout results appear here once the holdout review is finished.</p>
      {error && <ErrorPanel error={error} />}
      {!data ? <PageSkeleton layout="table" label="Loading backtests" /> :
        <ReportList rows={data.reports} onOpen={(id) => go(`/my-trades/${id}`)} title="All backtests" testId="my-all-reports" />}
    </div>
  );
}

function MyTradeListPage({ id }: { id: string }) {
  const { data, error } = useApi<Report>(my.reportUrl(id), [id]);
  const money = useMoney();
  const [side, setSide] = useState<"all" | "win" | "loss">("all");
  const rows = (data?.trades ?? []).filter((t) => side === "all" || (side === "win" ? t.net_r > 0 : t.net_r <= 0));
  return (
    <div className="page" data-testid="my-trades-page">
      <PageHead title={data ? `Trades: ${data.label || (data.kind.startsWith("holdout") ? "Holdout" : "Backtest")}` : "Trades"}>
        <a className="btn btn-secondary" href={href("/my-trades")}>All backtests</a>
        <div className="segmented small" role="group">
          {(["all", "win", "loss"] as const).map((k) => <button key={k} className={side === k ? "on" : ""} onClick={() => setSide(k)}>
            {k === "all" ? "All" : k === "win" ? "Winners" : "Losers / breakeven"}</button>)}
        </div>
      </PageHead>
      {error && <ErrorPanel error={error} />}
      {!data ? <PageSkeleton layout="table" label="Loading trades" /> : (
        <Card title={`${rows.length} trades · ${day(data.window.start)} – ${day(data.window.end)}`}>
          <p className="muted small">Click a trade for its charts, levels and checklist.</p>
          <TableWrap><table className="dense" data-testid="my-trades-table">
            <thead><tr><th className="num">#</th><th>Entry (New York)</th><th>Side</th><th>Model</th><th>Confirmation</th>
              <th className="num">Entry</th><th className="num">Stop</th><th className="num">Target</th><th className="num">Planned R</th>
              <th>Exit</th><th className="num">Result R</th><th className="num">Result</th><th className="num">Score</th></tr></thead>
            <tbody>{rows.map((t) => (
              <tr key={t.trade_no} onClick={() => go(`/my-trades/${data.id}/${t.trade_no}`)} style={{ cursor: "pointer" }}>
                <td className="num">{t.trade_no}</td><td>{nyTime(sec(t.entry_ts))}</td><td>{dirWord(t.direction)}</td><td>{MODEL(t.model)}</td>
                <td>{t.confirmation_tf ?? "–"}</td><td className="num">{px(t.entry_price_theo)}</td><td className="num">{px(t.stop_price)}</td>
                <td className="num">{px(t.target_price)}</td><td className="num">{n(t.r_planned, 2)}</td><td>{exitWord(t.exit_reason)}</td>
                <td className={`num ${signCls(t.net_r)}`}>{r(t.net_r, 2)}</td><td className={`num ${signCls(t.net_usd)}`}>{money.fmt(t.net_usd)}</td>
                <td className="num">{t.quality ?? "–"}</td></tr>))}</tbody></table></TableWrap>
        </Card>)}
    </div>
  );
}

export function MyTradePage() {
  const route = useRoute();
  const id = route.parts[1], no = Number(route.parts[2]);
  const { data, error } = useApi<TradeDoc>(id && no ? my.tradeUrl(id, no) : null, [id, no]);
  const money = useMoney();
  return (
    <div className="page" data-testid="my-trade-page">
      <PageHead title={data ? `Trade ${data.trade_no}: ${dirWord(data.direction)} ${nyTime(sec(data.entry_ts))}` : "Trade"}>
        <a className="btn btn-secondary" href={href(`/my-trades/${id}`)}>All trades</a>
        <Button disabled={!data || no <= 1} onClick={() => go(`/my-trades/${id}/${no - 1}`)}>Previous</Button>
        <Button disabled={!data || no >= (data?.count ?? 0)} onClick={() => go(`/my-trades/${id}/${no + 1}`)}>Next</Button>
      </PageHead>
      {error && <ErrorPanel error={error} />}
      {!data ? <PageSkeleton layout="overview" label="Loading the trade" /> : (
        <>
          <div className="kpis">
            <Kpi label="Result" value={r(data.net_r, 2)} tone={signCls(data.net_r)} sub={money.fmt(data.net_usd)} accent />
            <Kpi label="Exit" value={exitWord(data.exit_reason)} sub={nyTime(sec(data.exit_ts))} />
            <Kpi label="Entry / stop / target" value={px(data.entry_price_theo)} sub={`${px(data.stop_price)} / ${px(data.target_price)}`} />
            <Kpi label="Model" value={MODEL(data.explanation?.model)} sub={`${data.contracts} MNQ · risk ${n(data.risk_points, 1)} pts`} />
          </div>
          <TradeCharts doc={data} />
          <TradeExplain e={data.explanation} />
        </>
      )}
    </div>
  );
}

/** Why the strategy entered, in plain words (built from the recorded explanation; nothing recomputed). */
function smtText(s: NonNullable<Explanation["smt"]>, up: boolean): string {
  const w = up ? "low" : "high";
  if (s.divergence === null) return `SMT with ES: unknown (${s.reason}).`;
  if (s.nq_ref === undefined) return `SMT with ES: no (${s.reason}).`;
  const ref = s.ref_ts ? ` of ${nyTime(sec(s.ref_ts))} (${s.tf})` : "";
  return `SMT with ES: ${s.divergence ? "yes" : "no"}, ${s.reason}. Swing ${w}${ref}: NQ ${px(s.nq_ref)} → extreme ${px(s.nq_extreme)}; `
    + `ES ${px(s.es_ref)} → ${s.es_extreme === null || s.es_extreme === undefined ? "no later bar" : `${up ? "lowest" : "highest"} since ${px(s.es_extreme)}`}.`;
}

export function explainText(e: Explanation): string[] {
  const up = (e.flip ? e.flip.setup_direction : e.direction) > 0;     // the setup's direction (ADR-97: a flip trades the other way)
  const out: string[] = [];
  if (e.flip) out.push(`Flipped trade: the setup was a ${up ? "long" : "short"} (stop ${px(e.flip.setup_stop)}, target ${px(e.flip.setup_target)}, `
    + `${n(e.flip.setup_r_planned, 2)} R); it was traded the other way, as a ${up ? "short" : "long"} with the setup's target as its stop and its stop as its target.`);
  const b = e.bias;
  const tfs = Object.entries(b.per_tf ?? {}).map(([k, v]) => `${k} ${v.score >= 0 ? "+" : ""}${n(v.score, 2)}`).join(", ");
  out.push(`${up ? "Bullish" : "Bearish"} bias (${b.method}${b.score !== null ? `, score ${n(b.score, 2)}` : ""}${tfs ? `: ${tfs}` : ""}).`);
  if (e.draw) out.push(`Draw on liquidity: ${e.draw.kind.replace(/_/g, " ")}${e.draw.tf ? ` (${e.draw.tf})` : ""} at ${px(e.draw.price)}, ${n(e.draw.distance, 0)} points away.`);
  const kl = e.key_levels.map((z) => `${z.tf} ${z.kind.replace(/_/g, " ")} ${px(z.bottom)}–${px(z.top)}${z.swept_inside_level ? ` (inside ${up ? "low" : "high"} ${px(z.swept_inside_level)} swept)` : ""}`).join("; ");
  out.push(`Manipulation leg from ${px(e.leg.start_price)} (${nyTime(sec(e.leg.start_ts))}) to ${px(e.leg.end_price)} (${nyTime(sec(e.leg.end_ts))}), ${n(e.leg.size_points, 1)} points, into: ${kl}.`);
  if (e.eq) out.push(`Equilibrium: the extreme sat at ${pct(e.eq.position, 0)} of the dealing range ${px(e.eq.low)}–${px(e.eq.high)} (${e.eq.ok ? (up ? "discount" : "premium") : "not in " + (up ? "discount" : "premium")}).`);
  if (e.leg.swept) out.push(`It took ${up ? "a prior low" : "a prior high"} at ${px(e.leg.swept.price)} (${e.leg.swept.tf}).`);
  const c = e.confirmation;
  out.push(`Confirmation: a ${c.tf} candle closed ${up ? "above" : "below"} ${px(c.level)}, through the ${c.gaps.length} ${up ? "bearish" : "bullish"} gap(s) of the leg (rule: ${c.rule} timeframe; gaps per timeframe ${Object.entries(c.gaps_per_tf).map(([k, v]) => `${k}: ${v}`).join(", ")}). Body ${pct(c.displacement.body_ratio, 0)} of the candle, range ${n(c.displacement.range_x_avg, 2)}× average.`);
  out.push(`Entry ${e.entry.type === "market" ? "at the next 1-minute open" : "with a limit order"} (planned ${px(e.entry.reference_price)}), stop ${px(e.stop.price)} (${e.stop.mode.replace(/_/g, " ")}${e.stop.widened_to_minimum ? ", widened to the minimum" : ""}, ${n(e.stop.risk_points, 1)} points), target ${px(e.target.price)} = ${n(e.target.r_planned, 2)} R (${e.target.source ?? ""}).`);
  if (e.smt) out.push(smtText(e.smt, up));
  if (e.breakeven.level !== null) out.push(`Breakeven once price reaches ${px(e.breakeven.level)} (the leg's swing point).`);
  else if (e.breakeven.mode !== "off") out.push(`Breakeven rule: ${e.breakeven.mode.replace(/_/g, " ")}.`);
  return out;
}

function TradeExplain({ e }: { e: Explanation }) {
  if (!e) return null;
  return (
    <div className="grid-cards">
      <Card title="Why it entered" testId="my-explain">
        <ul className="my-explain">{explainText(e).map((t, k) => <li key={k}>{t}</li>)}</ul>
      </Card>
      <Card title={`Checklist (score ${e.quality})`} testId="my-checklist">
        <div className="my-checks">{Object.entries(e.checklist).map(([k, v]) => (
          <div key={k} className={`my-check ${v === true ? "yes" : v === false ? "no" : "na"}`}>
            <span className="my-check-mark">{v === true ? "✓" : v === false ? "✗" : "–"}</span>
            <span>{CHECK_LABEL[k] ?? k}</span><span className="muted small">{v === true ? "True" : v === false ? "False" : "not checked"}</span>
          </div>))}</div>
      </Card>
    </div>
  );
}

/** Charts of every timeframe a trade used, with entry / stop / target and the levels. ``until`` = holdout review before the
    decision: the candles stop at the signal and nothing after it is drawn. */
export function TradeCharts({ doc, until }: { doc: { explanation: Explanation; charts: string[]; candles: Record<string, Candle[]>;
  entry_ts?: string; exit_ts?: string; exit_price_theo?: number; final_stop_price?: number; stop_price?: number; target_price?: number;
  entry_price_theo?: number; direction?: number }; until?: boolean }) {
  const e = doc.explanation;
  const tfs = doc.charts.filter((t) => doc.candles[t]?.length);
  const preferred = e?.confirmation?.tf && tfs.includes(e.confirmation.tf) ? e.confirmation.tf : tfs[0];
  const [picked, setTf] = useState<string>(preferred);
  const [show, setShow] = useState({ levels: true, conf: true, leg: true, draw: true, bias: true });
  if (!e || !tfs.length) return null;
  const tf = tfs.includes(picked) ? picked : preferred;            // a later trade may not have the previous tab
  const sig = sec(e.signal_ts) + 60;               // the signal bar's close
  const exitT = doc.exit_ts ? sec(doc.exit_ts) : undefined;
  const entry = doc.entry_price_theo ?? e.entry.reference_price;
  const stop = doc.stop_price ?? e.stop.price, target = doc.target_price ?? e.target.price;
  const lines: PriceLine[] = [
    { price: entry, label: `Entry ${px(entry)}`, color: "var(--accent)", from: sig - 60, to: exitT },
    { price: stop, label: `Stop ${px(stop)}`, color: "var(--c-neg)", from: sig - 60, to: exitT },
    { price: target, label: `Target ${px(target)}`, color: "var(--ok)", from: sig - 60, to: exitT },
  ];
  if (e.breakeven.level !== null) lines.push({ price: e.breakeven.level, label: "Breakeven trigger", color: "var(--c3)", dash: "5 4", from: sig - 60, to: exitT });
  if (!until && doc.final_stop_price !== undefined && doc.final_stop_price !== null && doc.final_stop_price !== stop)
    lines.push({ price: doc.final_stop_price, label: "Moved stop", color: "var(--c-neg)", dash: "3 3", from: sig, to: exitT });
  if (show.draw && e.draw) lines.push({ price: e.draw.price, label: `Draw ${px(e.draw.price)}`, color: "var(--c2)", dash: "6 4", noRange: true });
  if (show.draw && e.eq) lines.push({ price: e.eq.eq, label: `EQ ${px(e.eq.eq)}`, color: "var(--muted)", dash: "2 3", noRange: true });
  const boxes: PriceBox[] = [];
  const endT = until ? sig : exitT ?? sig;
  if (show.levels) for (const z of e.key_levels) {
    boxes.push({ top: z.top, bottom: z.bottom, from: sec(z.t_from), to: endT, label: `${z.tf} ${z.kind.replace(/_/g, " ")}`,
      color: z.kind === "fvg" ? "var(--c2)" : z.kind === "cisd" ? "var(--c3)" : "var(--info)" });
    if (z.from_gap) boxes.push({ top: z.from_gap.top, bottom: z.from_gap.bottom, from: sec(z.t_from) - TF_MIN[z.from_gap.tf] * 120, to: endT,
      label: `${z.from_gap.tf} gap`, color: "var(--c2)" });
  }
  if (show.conf) for (const g of e.confirmation.gaps)
    boxes.push({ top: g.top, bottom: g.bottom, from: sec(g.t_from), to: sig, label: `${e.confirmation.tf} gap (inverted)`, color: "var(--warn)" });
  if (show.bias && e.bias.per_tf?.[tf]?.gaps) for (const g of e.bias.per_tf[tf].gaps ?? [])
    boxes.push({ top: g.top, bottom: g.bottom, from: sec(g.t_from), to: sig, label: `${g.side} gap ${g.state}`,
      color: g.state === "respected" ? "var(--c1)" : "var(--muted)" });
  const markers: Marker[] = [];
  if (show.leg) {
    markers.push({ t: sec(e.leg.start_ts), price: e.leg.start_price, label: "Leg start", color: "var(--text-2)" });
    markers.push({ t: sec(e.leg.end_ts), price: e.leg.end_price, label: "Manipulation", color: "var(--warn)" });
    if (e.leg.swept) lines.push({ price: e.leg.swept.price, label: "Swept", color: "var(--warn)", dash: "2 4", from: sec(e.leg.start_ts) - 3600, to: sig });
  }
  const tfm = TF_MIN[tf] ?? 1;
  const focus = { from: Math.min(sec(e.leg.start_ts), sig) - tfm * 60 * 5, to: until ? sig : (exitT ?? sig) };
  return (
    <Card title="Charts" testId="my-charts" actions={
      <div className="inline small">
        {([["levels", "Key levels"], ["conf", "Inverted gap"], ["leg", "Leg"], ["draw", "Draw / EQ"], ["bias", "Bias gaps"]] as [keyof typeof show, string][])
          .map(([k, l]) => <Checkbox key={k} checked={show[k]} onChange={(v) => setShow({ ...show, [k]: v })} label={l} />)}
      </div>}>
      <Tabs tabs={tfs.map((t) => ({ id: t, label: t }))} active={tf} onChange={setTf} />
      <CandleChart key={tf} candles={doc.candles[tf]} tfMinutes={tfm} lines={lines} boxes={boxes} markers={markers} focus={focus}
        cut={until ? sig - 60 : undefined} testId={`my-chart-${tf}`} />
      {until && <p className="muted small">The chart stops at the entry signal: you see only what was known at that moment.</p>}
    </Card>
  );
}

// =============================================================================================== holdout review
export function MyHoldoutPage() {
  const { data, error, reload } = useApi<HoldoutAllowance>(my.holdoutUrl);
  const [confirm, setConfirm] = useState<null | { kind: "automatic" } | { kind: "manual"; n: number }>(null);
  const [typed, setTyped] = useState("");
  const [pick, setPick] = useState<string | null>(null);
  const [favOnly, setFavOnly] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [last, setLast] = useState<Decision | null>(null);
  const [lastCandidate, setLastCandidate] = useState<ReviewView["candidate"]>(null);
  const [job, setJob] = useJob(() => reload());
  useEffect(() => { if (data?.job && !job) setJob(data.job); }, [data?.job]); // eslint-disable-line react-hooks/exhaustive-deps
  const closeConfirm = () => { setConfirm(null); setTyped(""); };
  const start = async () => {
    if (typed !== "HOLDOUT" || !confirm) return;
    setErr(null);
    try {
      if (confirm.kind === "automatic" && pick) { setJob(await my.holdoutAutomatic(pick)); setPick(null); }
      else if (confirm.kind === "manual") { setBusy(true); await my.holdoutManual(confirm.n); reload(); }
    } catch (e) { setErr(e as ApiError); } finally { setBusy(false); closeConfirm(); }
  };
  const decide = async (take: boolean) => {
    if (!data?.candidate) return;
    setBusy(true); setErr(null);
    try {
      const d = await my.decide(data.candidate.signal_bar, take);
      setLastCandidate(data.candidate);
      setLast(d);
      reload();
    } catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  };
  if (error) return <div className="page"><PageHead title="Holdout review" /><ErrorPanel error={error} /></div>;
  if (!data) return <div className="page"><PageHead title="Holdout review" /><PageSkeleton layout="overview" label="Loading the holdout" /></div>;
  const running = job?.state === "running";
  const slots = data.strategies;
  const reviewing = data.review_strategy ? slots.find((x) => x.n === data.review_strategy) : undefined;
  const looksUsed = slots.reduce((k, x) => k + 1 + (x.manual ? 1 : 0), 0);
  const rows = favOnly ? data.candidates.filter((c) => c.favorite) : data.candidates;
  const chosen = data.candidates.find((c) => c.ref === pick) ?? null;
  const confirmLabel = confirm?.kind === "manual" ? slots.find((x) => x.n === confirm.n)?.source_label : chosen?.label;
  return (
    <div className="page" data-testid="my-holdout-page">
      <PageHead title="Holdout review" />
      {!data.ready && <Banner tone="warn">{data.problem}</Banner>}
      {data.ready && data.config_ok === false && <Banner tone="error">The research settings differ from the protocol's. Restore them under
        Run backtest → Research runs first.</Banner>}
      <ErrorPanel error={err} />
      <JobLine job={job} />
      <div className="kpis" data-testid="my-holdout-looks">
        <Kpi label="Holdout period" value={data.holdout ? day(data.holdout.start) : "–"} sub={data.holdout ? `to ${day(data.holdout.end)}` : undefined} />
        <Kpi label="Strategies tested" value={`${slots.length} of ${data.max_strategies}`} sub="each: automatic, then manual" accent={slots.length < data.max_strategies} />
        <Kpi label="Holdout looks used" value={`${looksUsed} of ${data.max_strategies * 2}`} />
      </div>
      {reviewing && (
        <Card title={`Manual holdout · Strategy ${reviewing.n}: ${reviewing.source_label}`} testId="my-manual"
          actions={data.progress ? <span className="small muted" data-testid="my-manual-progress">{data.progress.decided} decided · {data.progress.taken} taken ·{" "}
            <span className={signCls(data.progress.taken_net_r)}>{r(data.progress.taken_net_r, 2)}</span> on your trades</span> : undefined}>
          {last ? <TradeResult d={last} cand={lastCandidate} onNext={() => { setLast(null); setLastCandidate(null); }} />
            : data.candidate ? (
              <>
                <div className="inline" style={{ justifyContent: "space-between" }}>
                  <div>
                    <b>{dirWord(data.candidate.explanation.direction)} at {nyTime(sec(data.candidate.signal_ts) + 60)}</b>
                    <div className="muted small">Planned: entry about {px(data.candidate.explanation.entry.reference_price)}, stop {px(data.candidate.explanation.stop.price)},
                      target {px(data.candidate.explanation.target.price)} (1 : {n(data.candidate.explanation.target.r_planned, 1)}).</div>
                  </div>
                  <div className="inline">
                    <Button kind="primary" onClick={() => decide(true)} busy={busy} testId="my-take">Take trade</Button>
                    <Button onClick={() => decide(false)} disabled={busy} testId="my-skip">Skip</Button>
                  </div>
                </div>
                <TradeCharts key={data.candidate.signal_bar} doc={data.candidate} until />
                <TradeExplain e={data.candidate.explanation} />
              </>
            ) : <p className="muted">Finishing…</p>}
        </Card>
      )}
      {!reviewing && last && <Card title="Manual holdout" testId="my-manual"><TradeResult d={last} cand={lastCandidate}
        onNext={() => { setLast(null); setLastCandidate(null); }} nextLabel="Done" /></Card>}
      {slots.map((x) => <HoldoutSlotCard key={x.n} slot={x} busy={busy || !!reviewing} onManual={() => setConfirm({ kind: "manual", n: x.n })} />)}
      {slots.length < data.max_strategies && !reviewing && (
        <Card title={`Pick strategy ${slots.length + 1} of ${data.max_strategies}`} testId="my-holdout-pick" actions={<div className="inline">
          <Checkbox checked={favOnly} onChange={setFavOnly} label="Favourites only" testId="my-holdout-fav-only" />
          <Button kind="primary" onClick={() => setConfirm({ kind: "automatic" })} disabled={!data.ready || !pick || running}
            testId="my-holdout-open">Run the automatic holdout…</Button></div>}>
          <p className="muted small">Your discovery backtests (favourites first) and every autotuner result that passed the lookahead check. The strategy
            is tested twice on the holdout: first automatically (result shown right away), then manually with your take / skip decisions.</p>
          {!rows.length ? <Empty>{favOnly ? "No favourite. Star a backtest in the Backtest tab." : "No strategy yet: run a backtest or the autotuner first."}</Empty> : (
            <TableWrap className="my-report-scroll" testId="my-holdout-candidates"><table className="dense">
              <thead><tr><th style={{ width: 28 }} /><th>★</th><th>Strategy</th><th>From</th><th className="num">Trades</th><th className="num">Per week</th>
                <th className="num">Win rate</th><th className="num">Net R</th><th className="num">Losing months</th></tr></thead>
              <tbody>{rows.map((c) => (
                <tr key={c.ref} className={c.ref === pick ? "selected" : ""} onClick={() => setPick(c.ref)} style={{ cursor: "pointer" }}>
                  <td><input type="radio" name="my-holdout-pick" checked={c.ref === pick} onChange={() => setPick(c.ref)}
                    aria-label={`Test ${c.label}`} data-testid={`my-holdout-pick-${c.ref.replace(/[:]/g, "-")}`} /></td>
                  <td>{c.favorite ? <span className="at-star on" aria-label="favourite">★</span> : ""}</td>
                  <td>{c.label || "–"}</td>
                  <td><Badge tone={c.source === "autotuner" ? "info" : "neutral"}>{c.source === "autotuner" ? "Autotuner" : "Backtest"}</Badge></td>
                  <td className="num">{c.trade_count ?? "–"}</td><td className="num">{n(c.metrics?.trades_per_week, 2)}</td>
                  <td className="num">{pct(c.metrics?.win_rate)}</td><td className={`num ${signCls(c.metrics?.net_r)}`}>{r(c.metrics?.net_r, 1)}</td>
                  <td className="num">{c.metrics?.months_losing ?? 0} / {c.metrics?.months_total ?? 0}</td>
                </tr>))}</tbody></table></TableWrap>)}
        </Card>
      )}
      <Confirm open={confirm !== null} title={confirm?.kind === "manual" ? "Use the manual holdout look?" : "Use the automatic holdout look?"}
        confirmLabel="Start" danger busy={running || busy} onCancel={closeConfirm} onConfirm={() => void start()}>
        <p>This spends the {confirm?.kind === "manual" ? "manual" : "automatic"} holdout look of <b>{confirmLabel}</b>. It cannot be undone.
          {" "}Type <b>HOLDOUT</b> to confirm.</p>
        <TextInput value={typed} onChange={setTyped} ariaLabel="Type HOLDOUT" testId="my-holdout-typed" />
      </Confirm>
    </div>
  );
}

/** ADR-103: the result of one decision, short: R, how it ended, the planned reward : risk; details on demand; Next. */
function TradeResult({ d, cand, onNext, nextLabel = "Next" }: { d: Decision; cand: ReviewView["candidate"]; onNext: () => void; nextLabel?: string }) {
  const o = d.outcome;
  const planned = d.r_planned ?? cand?.explanation.target.r_planned;
  return (
    <div className="my-result" data-testid="my-result">
      <div className="my-result-line">
        {d.take ? null : <span className="muted">Skipped · it would have been </span>}
        {o ? <b className={`my-result-r ${signCls(o.net_r)}`} data-testid="my-result-r">{o.net_r > 0 ? "+" : ""}{r(o.net_r, 2)}</b>
          : <b className="muted">no trade (the engine would not have filled it)</b>}
        {o && <span> · {exitWord(o.exit_reason)}</span>}
      </div>
      {planned != null && <div className="muted small">planned 1 : {n(planned, 1)}</div>}
      {o && cand && <details className="my-result-details" data-testid="my-result-details">
        <summary>Show details (chart, prices, times)</summary>
        <p className="small">{dirWord(o.direction)} · entry {px(o.entry_price_theo)} at {nyTime(sec(o.entry_ts))} · stop {px(o.stop_price)} · target {px(o.target_price)}
          {" "}· exit {px(o.exit_price_theo)} at {nyTime(sec(o.exit_ts))} · {o.contracts} MNQ</p>
        <TradeCharts doc={{ ...cand, candles: o.candles, entry_ts: o.entry_ts, exit_ts: o.exit_ts, entry_price_theo: o.entry_price_theo,
          stop_price: o.stop_price, target_price: o.target_price }} />
      </details>}
      <div className="inline" style={{ justifyContent: "flex-end" }}>
        <Button kind="primary" onClick={onNext} testId="my-next">{nextLabel} →</Button>
      </div>
    </div>
  );
}

function HoldoutSlotCard({ slot, busy, onManual }: { slot: HoldoutSlot; busy: boolean; onManual: () => void }) {
  const [open, setOpen] = useState<null | "automatic" | "manual">(null);
  const manualDone = slot.review_status === "complete" && slot.final_report;
  return (
    <Card title={`Strategy ${slot.n}: ${slot.source_label}`} testId={`my-holdout-slot-${slot.n}`}>
      <div className="inline" style={{ flexWrap: "wrap", gap: 8 }}>
        <Badge tone="ok">Automatic: done</Badge>
        <Button small kind={open === "automatic" ? "primary" : "secondary"} onClick={() => setOpen(open === "automatic" ? null : "automatic")}
          testId={`my-holdout-auto-${slot.n}`}>{open === "automatic" ? "Hide" : "Show"} automatic result</Button>
        {!slot.manual ? <Button small kind="primary" onClick={onManual} disabled={busy} testId={`my-holdout-manual-${slot.n}`}>Start the manual holdout…</Button>
          : manualDone ? <><Badge tone="ok">Manual: done</Badge>
            <Button small kind={open === "manual" ? "primary" : "secondary"} onClick={() => setOpen(open === "manual" ? null : "manual")}
              testId={`my-holdout-manres-${slot.n}`}>{open === "manual" ? "Hide" : "Show"} manual result</Button></>
          : <Badge tone="info">Manual: in progress</Badge>}
      </div>
      <details className="small" style={{ marginTop: 8 }}><summary>Settings</summary>
        {Object.keys(slot.settings_changed).length ? <dl className="kv">{Object.entries(slot.settings_changed).map(([k, v]) => (
          <div key={k} className="kv-row"><dt>{k}</dt><dd>{String(v)}</dd></div>))}</dl> : <p className="muted">All defaults.</p>}
      </details>
      {open === "automatic" && <ReportView id={slot.automatic.report} title={`Strategy ${slot.n} - automatic (every signal)`} />}
      {open === "manual" && slot.final_report && <ReportView id={slot.final_report} title={`Strategy ${slot.n} - with your decisions`} />}
    </Card>
  );
}

// =============================================================================================== setup review (ADR-96)
/** Blind take / skip on a fixed sample of a DISCOVERY backtest's setups; outcomes revealed at the end. No holdout, no try. */
export function MySetupReviewPage() {
  const route = useRoute();
  const id = route.parts[1];
  return id ? <SetupReviewRun key={id} id={id} /> : <SetupReviewStart />;
}

function SetupReviewStart() {
  const { data, error } = useApi<SetupReviews>(my.setupReviewsUrl);
  const [base, setBase] = useState<string | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const pick = base ?? data?.backtests?.[0]?.id ?? null;
  const chosen = data?.backtests.find((b) => b.id === pick);
  const start = async () => {
    if (!pick) return;
    setBusy(true); setErr(null);
    try { const st = await my.startSetupReview(pick); viewCache.clear(); go(`/my-setup/${st.id}`); }
    catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  };
  return (
    <div className="page" data-testid="my-setup-start-page">
      <PageHead title="Setup review" />
      {error && <ErrorPanel error={error} />}
      {data?.open && <Banner tone="info">A setup review is in progress ({data.open.decided} of {data.open.size} decided).{" "}
        <a href={href(`/my-setup/${data.open.id}`)} data-testid="my-setup-continue">Continue it</a></Banner>}
      {data && !data.open && (
        <Card title="Judge the strategy's setups yourself" testId="my-setup-new">
          <p>You see {data.sample_size} setups, picked at random from a discovery backtest (always the same ones for the same backtest).
            Each chart stops at the entry signal. Decide <b>Take</b> or <b>Skip</b>; for a skip, tick why. The results stay hidden until
            you have decided every setup, so they cannot steer your decisions. Then Claude can compare your skips with the outcomes and
            turn them into rules.</p>
          <p className="muted small">This uses the discovery period only: no holdout look, no try, not a backtest run. Please don't open
            this backtest's trades while you review.</p>
          {!data.backtests.length ? <Empty>Run a backtest first (Backtest tab).</Empty> : (
            <div className="inline">
              <Field label="Backtest to review">
                <Select value={pick} onChange={setBase} ariaLabel="Backtest to review" testId="my-setup-base"
                  options={data.backtests.map((b) => ({ value: b.id, label: `${b.label || "Backtest"} · ${nyTime(sec(b.created_at))} · ${b.trade_count} trades · ${pct(b.metrics.win_rate)} · ${n(b.metrics.trades_per_week, 2)} / week` }))} />
              </Field>
              <Button kind="primary" onClick={start} busy={busy} busyLabel="Starting…" disabled={!pick || !chosen?.trade_count} testId="my-setup-go">
                Start the review ({Math.min(data.sample_size, chosen?.trade_count ?? 0)} setups)</Button>
            </div>)}
          <ErrorPanel error={err} />
        </Card>)}
      {data && data.reviews.length > 0 && (
        <Card title="Setup reviews" testId="my-setup-list">
          <TableWrap><table className="dense"><thead><tr><th title="Saved for Claude">Saved</th><th>Started</th><th>Backtest</th>
            <th className="num">Decided</th><th>Status</th><th /></tr></thead>
            <tbody>{data.reviews.map((v) => (
              <tr key={v.id} onClick={() => go(`/my-setup/${v.id}`)} style={{ cursor: "pointer" }}>
                <td>{v.exported ? <span className="my-saved-check" title={`Saved · ${v.exported.file}`}>✓</span> : ""}</td>
                <td>{nyTime(sec(v.created_at))}</td><td>{v.base_label || v.base_report}</td>
                <td className="num">{v.decided} / {v.size}</td>
                <td>{v.status === "complete" ? <Badge tone="ok">Finished</Badge> : <Badge tone="info">In progress</Badge>}</td>
                <td onClick={(e: { stopPropagation: () => void }) => e.stopPropagation()}>
                  {v.status === "complete" && <SaveOne id={v.id} done={!!v.exported} />}</td>
              </tr>))}</tbody></table></TableWrap>
        </Card>)}
    </div>
  );
}

function SetupReviewRun({ id }: { id: string }) {
  const { data, error, reload } = useApi<SetupView>(my.setupReviewUrl(id), [id]);
  const [skipping, setSkipping] = useState(false);
  const [reasons, setReasons] = useState<Set<string>>(new Set());
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<ApiError | null>(null);
  const c = data?.candidate;
  const rv = data?.review;
  const reset = () => { setSkipping(false); setReasons(new Set()); setNote(""); };
  const decide = async (take: boolean) => {
    if (!c) return;
    setBusy(true); setErr(null);
    try { await my.setupDecide(id, c.trade_no, take, take ? [] : [...reasons], take ? "" : note); reset(); reload(); window.scrollTo(0, 0); }
    catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  };
  const undo = async () => {
    setBusy(true); setErr(null);
    try { await my.setupUndo(id); reset(); reload(); } catch (e) { setErr(e as ApiError); } finally { setBusy(false); }
  };
  const toggle = (k: string) => { const t = new Set(reasons); if (t.has(k)) t.delete(k); else t.add(k); setReasons(t); };
  const pr = data?.progress;
  return (
    <div className="page" data-testid="my-setup-page">
      <PageHead title="Setup review">
        {rv?.status === "complete" && <SaveOne id={id} done={!!rv.exported} />}
        <a className="btn btn-secondary" href={href("/my-setup")}>All setup reviews</a>
      </PageHead>
      {error && <ErrorPanel error={error} />}
      <ErrorPanel error={err} />
      {!data && !error && <PageSkeleton layout="overview" label="Loading the setup (the first one takes a moment)" />}
      {rv && pr && rv.status === "in_progress" && (
        <div className="kpis">
          <Kpi label="Decided" value={`${pr.decided} of ${pr.size}`} meter={pr.decided / (pr.size || 1)} sub={`${pr.taken} taken · ${pr.skipped} skipped`} />
          <Kpi label="Backtest" value={rv.base_label || "Backtest"} sub={`${rv.sample.size} of its ${rv.sample.of} trades, at random`} />
        </div>)}
      {rv?.status === "in_progress" && c && (
        <>
          <Card title={`Setup ${c.position} of ${pr?.size}: ${dirWord(c.explanation.direction)} · ${MODEL(c.explanation.model)} · ${nyTime(sec(c.signal_ts) + 60)}`}
            testId="my-setup-candidate" actions={<div className="inline">
              <Button small kind="ghost" onClick={undo} disabled={busy || !pr?.decided} testId="my-setup-undo">Undo last</Button>
              <Button kind="primary" onClick={() => decide(true)} busy={busy && !skipping} disabled={busy} testId="my-setup-take">Take</Button>
              <Button onClick={() => setSkipping(!skipping)} disabled={busy} testId="my-setup-skip">Skip…</Button></div>}>
            <p className="muted">Planned: entry about {px(c.explanation.entry.reference_price)}, stop {px(c.explanation.stop.price)},
              target {px(c.explanation.target.price)} ({n(c.explanation.target.r_planned, 2)} R). Score {c.explanation.quality}.</p>
            {skipping && (
              <div className="my-skip-box" data-testid="my-setup-reasons">
                <p><b>Why skip it?</b> Tick one or more.</p>
                <div className="my-reason-chips">{Object.entries(data.reasons).map(([k, label]) => (
                  <button key={k} type="button" className={`my-chip${reasons.has(k) ? " on" : ""}`} onClick={() => toggle(k)}
                    data-testid={`my-reason-${k}`}>{label}</button>))}</div>
                <TextInput value={note} onChange={setNote} placeholder="Note (optional)" ariaLabel="Note" testId="my-setup-note" />
                <div className="inline"><Button kind="primary" onClick={() => decide(false)} busy={busy} disabled={!reasons.size}
                  testId="my-setup-confirm-skip">Skip this setup</Button>
                  <Button kind="ghost" onClick={reset}>Cancel</Button></div>
              </div>)}
          </Card>
          <TradeCharts key={c.trade_no} doc={c} until />
          <TradeExplain e={c.explanation} />
          <p className="muted small">Results stay hidden until you have decided all {pr?.size} setups.</p>
        </>)}
      {rv?.status === "complete" && data?.results && <SetupResults data={data} />}
    </div>
  );
}

function SetupResults({ data }: { data: SetupView }) {
  const res = data.results!;
  const rv = data.review;
  const [side, setSide] = useState<"all" | "take" | "skip">("all");
  const rows = res.rows.filter((x) => side === "all" || (side === "take" ? x.take : !x.take));
  const stat = (label: string, s: SetupStats, accent?: boolean) => (
    <Kpi label={label} value={pct(s.win_rate)} tone={signCls(s.net_r)} accent={accent}
      sub={`${s.trades} trades · ${s.wins} wins · ${r(s.net_r, 1)} net · ${r(s.avg_r, 2)} / trade`} />);
  return (
    <>
      <Banner tone="ok" testId="my-setup-done">Review finished: the outcomes are shown below. Save it for Claude to turn your skips into rules.</Banner>
      <div className="kpis" data-testid="my-setup-kpis">
        {stat("Every reviewed setup", res.all)}
        {stat("The setups you took", res.taken, true)}
        {stat("The setups you skipped", res.skipped)}
      </div>
      <Card title="By skip reason" testId="my-setup-by-reason">
        {!res.by_reason.length ? <Empty>You took every setup.</Empty> : (
          <TableWrap><table className="dense"><thead><tr><th>Reason</th><th className="num">Skipped</th><th className="num">Would have won</th>
            <th className="num">Win rate</th><th className="num">Net R of these</th></tr></thead>
            <tbody>{res.by_reason.map((b) => (
              <tr key={b.reason}><td>{b.label}</td><td className="num">{b.trades}</td><td className="num">{b.wins}</td>
                <td className="num">{pct(b.win_rate)}</td><td className={`num ${signCls(b.net_r)}`}>{r(b.net_r, 1)}</td></tr>))}</tbody></table></TableWrap>)}
        <p className="muted small">A negative net R means skipping those setups helped. {res.note}</p>
      </Card>
      <Card title={`Reviewed setups · ${rv.base_label || "Backtest"}`} testId="my-setup-rows" actions={
        <div className="segmented small" role="group">
          {(["all", "take", "skip"] as const).map((k) => <button key={k} className={side === k ? "on" : ""} onClick={() => setSide(k)}>
            {k === "all" ? "All" : k === "take" ? "Taken" : "Skipped"}</button>)}
        </div>}>
        <p className="muted small">Click a setup to open the full trade.</p>
        <TableWrap className="my-setup-scroll"><table className="dense"><thead><tr><th className="num">#</th><th>Entry (New York)</th><th>Side</th>
          <th>Model</th><th>You</th><th>Why</th><th className="num">Score</th><th>Exit</th><th className="num">Result R</th></tr></thead>
          <tbody>{rows.map((x) => (
            <tr key={x.trade_no} onClick={() => go(`/my-trades/${rv.base_report}/${x.trade_no}`)} style={{ cursor: "pointer" }}>
              <td className="num">{x.trade_no}</td><td>{nyTime(sec(x.entry_ts))}</td><td>{dirWord(x.direction)}</td><td>{MODEL(x.model)}</td>
              <td>{x.take ? <Badge tone="ok">Took</Badge> : <Badge>Skipped</Badge>}</td>
              <td>{x.reasons.map((k) => data.reasons[k] ?? k).join(", ")}{x.note ? ` · ${x.note}` : ""}</td>
              <td className="num">{x.quality ?? "–"}</td><td>{exitWord(x.exit_reason ?? "")}</td>
              <td className={`num ${signCls(x.net_r)}`}>{r(x.net_r, 2)}</td></tr>))}</tbody></table></TableWrap>
      </Card>
    </>
  );
}
