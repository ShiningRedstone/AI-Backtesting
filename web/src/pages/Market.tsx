/* Market simulator (ADR-106): NQ and ES on the DISCOVERY period, analysed deeply (every ICT / SMC concept on every
   timeframe and its effect on the other timeframes and the 15-minute chart, NQ vs ES, shocks, news) and live-knowledge
   15-minute forecasts scored against baselines on months the models never saw. Nothing here is a backtest, a try or a
   holdout look; the holdout is never read. */
import { useEffect, useMemo, useState } from "react";
import { ApiError, viewCache } from "../api/client";
import { market } from "../api/market";
import type { DayView, EdgeCell, EdgeGroup, Edges, EffectRow, HoldoutStatus, MarketStatus, PatternRow, Quant, Rate, Score, Section, TargetEval } from "../api/market";
import type { MyJob } from "../api/my";
import { useApi } from "../app/context";
import { BarChart, LineChart } from "../components/charts";
import { CandleChart } from "../components/candles";
import type { Marker, PriceLine } from "../components/candles";
import { Badge, Banner, Button, Card, Confirm, Empty, ErrorPanel, Kpi, PageSkeleton, Select, TableWrap, TechDetails, TextInput, pct } from "../components/ui";
import { PageHead } from "./MyStrategy";

const TF_NAMES: Record<number, string> = { 1: "1m", 2: "2m", 3: "3m", 4: "4m", 5: "5m", 10: "10m", 15: "15m", 30: "30m", 60: "1h", 240: "4h", 1440: "1D" };
const MODEL_WORDS: Record<string, string> = { baseline: "Baseline (the usual)", logistic: "Logistic", boosting: "Gradient boosting", similar: "Similar situations" };
const OUTCOME_WORDS: Record<string, string> = { edge: "moved 1 ATR its way before 1 ATR against", next15: "next 15-min candle went its way",
  rest15: "rest of the 15-min candle went its way" };
const TARGET_WORDS: Record<string, string> = { up: "Next 15-min candle up or down", size: "Size of the next 15-min candle",
  bias: "Session closes above the current price", levels: "Level reached before the session ends" };
const r = (p: Rate | null | undefined, d = 0) => (p && p.p != null ? `${pct(p.p, d)} (${p.k}/${p.n})` : "–");
const q = (x: Quant | null | undefined, k: "q25" | "q50" | "q75" | "q10" | "q90" = "q50", d = 1) => (x && x[k] != null ? (x[k] as number).toFixed(d) : "–");
const num = (v: number | null | undefined, d = 2) => (v == null || !Number.isFinite(v) ? "–" : v.toFixed(d));
const dirWord = (d: number) => (d > 0 ? "bullish" : "bearish");
const kindName = (kinds: Record<string, string> | undefined, k: string) => kinds?.[k] ?? k;
const outside = (real: Rate | undefined, chance: number | null | undefined) =>
  !!(real?.ci && chance != null && (chance < real.ci[0] || chance > real.ci[1]));

function useMarketJob(onDone: () => void): [MyJob | null, (j: MyJob) => void] {
  const [job, setJob] = useState<MyJob | null>(null);
  useEffect(() => {
    if (!job || job.state !== "running") return;
    let live = true;
    const t = window.setTimeout(() => {
      market.job(job.job_id).then((j) => {
        if (!live) return;
        setJob(j);
        if (j.state !== "running") { viewCache.clear(); onDone(); }
      }).catch(() => { if (live) setJob(null); });
    }, 1200);
    return () => { live = false; window.clearTimeout(t); };
  }, [job]); // eslint-disable-line react-hooks/exhaustive-deps
  return [job, setJob];
}

function useSection<T>(name: string) {
  return useApi<Section<T>>(market.sectionUrl(name), [name]);
}

function NeedsAnalysis({ title, error }: { title: string; error: ApiError | null }) {
  return (
    <div className="page"><PageHead title={title} />
      {error && error.status === 404 ? <Empty>Run the analysis first (Market simulator → Overview).</Empty> : <ErrorPanel error={error} />}
    </div>
  );
}

// =============================================================================================== overview
export function MarketOverviewPage() {
  const { data, error, reload } = useApi<MarketStatus>(market.statusUrl);
  const [job, setJob] = useMarketJob(reload);
  const [err, setErr] = useState<ApiError | null>(null);
  const [key, setKey] = useState("");
  const [source, setSource] = useState("forex-factory");
  useEffect(() => { if (data?.job && !job) setJob(data.job); }, [data?.job]); // eslint-disable-line react-hooks/exhaustive-deps
  if (error) return <div className="page"><PageHead title="Market simulator" /><ErrorPanel error={error} /></div>;
  if (!data) return <div className="page"><PageHead title="Market simulator" /><PageSkeleton layout="overview" label="Loading" /></div>;
  const running = job?.state === "running";
  const go = async (fn: () => Promise<MyJob>) => { setErr(null); try { setJob(await fn()); } catch (e) { setErr(e as ApiError); } };
  const saveKey = async () => { setErr(null); try { await market.setKey(key || null); setKey(""); viewCache.clear(); reload(); } catch (e) { setErr(e as ApiError); } };
  const a = data.analysis, n = data.news;
  return (
    <div className="page" data-testid="market-overview">
      <PageHead title="Market simulator" />
      <Card title="What this does" testId="market-intro">
        <p>Reads every minute of NQ and ES in your <b>discovery period</b> (never the holdout) and measures how the market behaves: trend and range
          days, sessions, how ES follows and when the two diverge, every ICT / SMC concept on every timeframe from 1 minute to the daily chart and
          what each one did to the other timeframes and to the 15-minute chart, shocks and their causes, and what the news did.</p>
        <p className="small">Then it predicts every 15-minute candle using only what was known before that candle opened, with three different models,
          and scores them on months they never saw against the plain "usual" baseline. A model only counts as skilled if it beats the baseline
          by more than chance. Nothing here is a backtest, a try or a holdout look.</p>
      </Card>
      <ErrorPanel error={err} />
      {running && <Banner tone="info" testId="market-job"><span className="spinner" /> {job?.step}</Banner>}
      {job?.state === "failed" && <Banner tone="error">{job.error?.message}</Banner>}
      <div className="grid-cards">
        <Card title="1 · Price data" testId="market-data">
          {!data.protocol ? <Banner tone="warn">An active research protocol is needed (its discovery dates and data).</Banner> :
            <p className="small">NQ: your research data, discovery period only. ES: {data.es.imported ? "imported (USA500 1-minute)" :
              <b>not imported - import it under My strategy → Settings for the NQ vs ES analysis</b>}.</p>}
          {a && <p className="small muted">Window {a.source.window.start.slice(0, 10)} – {a.source.window.end.slice(0, 10)} · {a.days} trading dates ·
            holdout from {a.source.holdout_start.slice(0, 10)} is never read.</p>}
        </Card>
        <Card title="2 · News (JBlanked API)" testId="market-news">
          <p className="small">Economic calendar with Forex Factory's red / orange / yellow folders, forecasts and actual values. Free account and API key
            at jblanked.com. The key is stored only on this computer (app settings, not in your workspace).</p>
          <div className="row gap">
            <input className="input" type="password" style={{ width: 260 }} placeholder={n.key.set ? `saved (${n.key.hint})` : "API key"}
              value={key} onChange={(e: { target: HTMLInputElement }) => setKey(e.target.value)} aria-label="JBlanked API key" data-testid="market-key" />
            <Button small onClick={saveKey} disabled={!key} testId="market-key-save">Save key</Button>
          </div>
          <div className="row gap" style={{ marginTop: 8 }}>
            <Select value={source} onChange={setSource} ariaLabel="News source" options={n.sources.map((s) => ({ value: s, label: s }))} />
            <Button small onClick={() => go(() => market.downloadNews(source))} disabled={!n.key.set || running} testId="market-news-btn">
              {n.downloaded ? "Download again" : "Download news"}</Button>
          </div>
          {n.downloaded && (n.refused ? <Banner tone="warn">{n.refused.message} Nothing from this download is used.</Banner> :
            <p className="small" data-testid="market-news-status">{n.events?.toLocaleString()} USD events ({n.high?.toLocaleString()} high impact),
              {" "}{n.first?.slice(0, 10)} – {n.last?.slice(0, 10)}. Time zone proven: <b>{n.timezone?.zone}</b> ({pct(n.timezone?.match, 0)} of
              {" "}{n.timezone?.anchors} fixed-time releases on their known New York time).</p>)}
        </Card>
      </div>
      <Card title="3 · Analysis" testId="market-analysis" actions={<Button kind="primary" onClick={() => go(() => market.analyze(!!a))}
        busy={running && (job as MyJob & { what?: string })?.kind === "market_analysis"} busyLabel="Analysing…" disabled={running || !data.protocol} testId="market-run">
        {a ? "Run again" : "Run the analysis"}</Button>}>
        {!a ? <p className="muted">Not run yet. Takes a few minutes (about 1-2 per year of data); the result is kept and only recomputed when the data,
          the news or the settings change.</p> : <AnalysisHeadline a={a} />}
      </Card>
      {a && <Card title="4 · New days (after your research data)" testId="market-newdays"
        actions={<Button onClick={() => go(market.newdays)} disabled={running} testId="market-newdays-btn">Download new days and score</Button>}>
        <p className="small">Downloads NQ and ES for the trading dates after your research data ends (from {data.newdays.first_date ?? "–"}; the holdout is
          never used, not even as history). The first 20 new days only build history; every later day is predicted live by models trained on the
          whole discovery period, and scored once it is complete.</p>
        <p className="small muted">Stored: NQ {data.newdays.nq.days} days, ES {data.newdays.es.days} days{data.newdays.nq.last ? ` (up to ${data.newdays.nq.last})` : ""}.</p>
        {data.newdays.scores && <ScoreTable scores={data.newdays.scores.up} title={`Next 15-min candle up / down on ${data.newdays.scores.candles} new candles`} />}
      </Card>}
    </div>
  );
}

function AnalysisHeadline({ a }: { a: NonNullable<MarketStatus["analysis"]> }) {
  return (
    <>
      <div className="kpis">
        <Kpi label="Trading dates" value={a.days} sub={`computed ${new Date(a.computed_at).toLocaleString()}`} />
        <Kpi label="Pattern cells tested" value={a.edges.cells_tested.toLocaleString()} sub="pattern × timeframe × direction × condition × outcome" />
        <Kpi label="Edge candidates" value={a.edges.confirmed} accent sub={`${a.edges.passed_find} passed on the first 70 %, ${a.edges.confirmed} held on the last 30 %`} />
        <Kpi label="News used" value={a.news.used ? a.news.events.toLocaleString() : "no"} sub={a.news.used ? `${a.news.high} high impact` : "download news first"} />
      </div>
      <TableWrap><table className="dense"><thead><tr><th>Forecast</th><th>Best model (chosen on earlier months)</th><th className="num">Skill on later months</th><th>Real?</th></tr></thead>
        <tbody>{Object.entries(a.forecast).map(([t, v]) => (
          <tr key={t}><td>{TARGET_WORDS[t] ?? t}</td><td>{v.chosen ? MODEL_WORDS[v.chosen] : "–"}</td>
            <td className="num">{skill(v.late)}</td><td><RealBadge s={v.late} /></td></tr>))}</tbody></table></TableWrap>
      <p className="small muted">Skill = how much better than the baseline (0 % = no better than "the usual", below 0 = worse). "Real" only when the
        95 % range of the skill (resampling whole days) stays above 0.</p>
    </>
  );
}

const skill = (s: Score | null | undefined) => (s && s.skill != null ? `${(s.skill * 100).toFixed(1)} %${s.skill_ci ? ` (${(s.skill_ci[0] * 100).toFixed(1)} … ${(s.skill_ci[1] * 100).toFixed(1)})` : ""}` : "–");
function RealBadge({ s }: { s: Score | null | undefined }) {
  if (!s || s.skill == null) return <span className="muted">–</span>;
  return s.real ? <Badge tone="ok">beats the baseline</Badge> : <Badge>no real skill</Badge>;
}

function ScoreTable({ scores, title }: { scores: Record<string, Score | null>; title: string }) {
  return (
    <>
      <h3 className="small-head">{title}</h3>
      <TableWrap><table className="dense"><thead><tr><th>Model</th><th className="num">Skill</th><th className="num">Hit rate</th><th>Real?</th></tr></thead>
        <tbody>{Object.entries(scores).map(([k, s]) => (
          <tr key={k}><td>{MODEL_WORDS[k] ?? k}</td><td className="num">{skill(s)}</td><td className="num">{s?.accuracy != null ? pct(s.accuracy, 1) : "–"}</td>
            <td>{k === "baseline" ? <span className="muted">reference</span> : <RealBadge s={s} />}</td></tr>))}</tbody></table></TableWrap>
    </>
  );
}

// =============================================================================================== trend & sessions
interface TrendData {
  day_types: Record<string, Rate>; day_type_next: Record<string, Record<string, Rate>>; time_of_high: number[]; time_of_low: number[]; time_bins: string[];
  power_of_3: Record<string, Rate>; sessions: { session: string; range_pts: Quant | null; share_of_day: Quant | null; makes_high: Rate; makes_low: Rate }[];
  m15_after_runs: { k: number; after: string; next_same: Rate }[]; m15_up_rate: Rate; m15_autocorr: { lag: number; corr: number; band: number; n: number }[];
  m15_profile: ({ t: string; up: Rate } & Quant)[]; weekdays: { weekday: string; range: Quant | null; up: Rate }[];
  news_days?: Record<string, { range: Quant | null; types: Record<string, Rate> }>;
  chance?: { method: string; real: Record<string, Rate>; chance: Record<string, Rate>; time_of_high_real: number[]; time_of_high_chance: number[] };
}

export function MarketTrendPage() {
  const { data, error } = useSection<TrendData>("trend");
  if (error) return <NeedsAnalysis title="Trend & sessions" error={error} />;
  if (!data) return <div className="page"><PageHead title="Trend & sessions" /><PageSkeleton layout="overview" label="Loading" /></div>;
  const t = data.data;
  const types = Object.keys(t.day_types);
  return (
    <div className="page" data-testid="market-trend">
      <PageHead title="Trend & sessions" />
      <Card title="Trend days and what comes next" testId="market-daytypes">
        <p className="small muted">Regular session 9:30-16:00. Trend day = closes in its top (bottom) 20 % with a range at least as big as the median of the
          previous 20 sessions; range day = closes in the middle 30-70 % with a smaller range.</p>
        <TableWrap><table className="dense"><thead><tr><th>Today</th><th className="num">How often</th>{types.map((x) => <th key={x} className="num">Next: {x}</th>)}</tr></thead>
          <tbody>{types.map((x) => (
            <tr key={x}><td>{x}</td><td className="num">{r(t.day_types[x])}</td>
              {types.map((y) => <td key={y} className="num">{pct(t.day_type_next[x]?.[y]?.p, 0)}</td>)}</tr>))}</tbody></table></TableWrap>
        <p className="small muted">Read across: after a trend-up day, how often the next day is each type. Compare with "How often" to see whether the day
          before changes anything.</p>
      </Card>
      <div className="grid-cards">
        <Card title="When the regular session's high and low form" testId="market-chance">
          <BarChart categories={t.time_bins} unit="days" signed={false}
            series={[{ id: "h", label: "high", values: t.time_of_high }, { id: "l", label: "low", values: t.time_of_low },
              ...(t.chance ? [{ id: "c", label: "high by chance", values: t.chance.time_of_high_chance }] : [])]} />
          {t.chance ? <TableWrap><table className="dense"><thead><tr><th></th><th className="num">Real</th><th className="num">By chance</th></tr></thead>
            <tbody>{([["up_days_low_first", "Up days: low before high (Power of 3)"], ["down_days_high_first", "Down days: high before low"],
              ["high_or_low_in_first_hour", "High or low inside the first hour"]] as const).map(([k, w]) => (
              <tr key={k}><td>{w}</td><td className="num">{r(t.chance!.real[k])}</td><td className="num">{pct(t.chance!.chance[k]?.p, 1)}
                {outside(t.chance!.real[k], t.chance!.chance[k]?.p) ? " *" : ""}</td></tr>))}</tbody></table></TableWrap> :
            <p className="small">Up days with the low before the high (Power of 3): {r(t.power_of_3.up_days_low_first)}. High or low inside the first hour:
              {" "}{r(t.power_of_3.high_or_low_in_first_hour)}.</p>}
          <p className="small muted">"By chance": {t.chance?.method ?? "run the analysis again"}. Only a clear gap between the two columns is a pattern
            (* = the chance value lies outside the real number's 95 % range; with three rows, one * in about 7 runs is itself luck); equal numbers mean the
            shape comes from how prices move, not from the time of day or a model.</p>
        </Card>
        <Card title="Sessions">
          <TableWrap><table className="dense"><thead><tr><th>Session</th><th className="num">Range (median pts)</th><th className="num">Share of day</th>
            <th className="num">Makes the day's high</th><th className="num">… low</th></tr></thead>
            <tbody>{t.sessions.map((s) => (
              <tr key={s.session}><td>{s.session}</td><td className="num">{q(s.range_pts)}</td><td className="num">{pct(s.share_of_day?.q50, 0)}</td>
                <td className="num">{pct(s.makes_high.p, 0)}</td><td className="num">{pct(s.makes_low.p, 0)}</td></tr>))}</tbody></table></TableWrap>
        </Card>
      </div>
      <Card title="15-minute candles: does direction continue?">
        <p className="small">All 15-min candles up: {r(t.m15_up_rate)}. After k candles in a row one way (same session), how often the next goes the same way:</p>
        <TableWrap><table className="dense"><thead><tr><th>After</th>{[1, 2, 3, 4, 5].map((k) => <th key={k} className="num">{k} in a row</th>)}</tr></thead>
          <tbody>{["up", "down"].map((d) => (
            <tr key={d}><td>{d}</td>{[1, 2, 3, 4, 5].map((k) => { const x = t.m15_after_runs.find((y) => y.k === k && y.after === d);
              return <td key={k} className="num">{x ? r(x.next_same.p != null ? x.next_same : null, 1) : "–"}</td>; })}</tr>))}</tbody></table></TableWrap>
        <p className="small muted">Autocorrelation of 15-min moves (lag 1-8): {t.m15_autocorr.map((a) => `${a.lag}: ${a.corr.toFixed(3)}${Math.abs(a.corr) > a.band ? "*" : ""}`).join(" · ")}
          {" "}(* = outside the ±{t.m15_autocorr[0]?.band.toFixed(3)} band chance gives).</p>
      </Card>
      <Card title="15-minute candle size through the day (New York time)">
        <LineChart x={t.m15_profile.map((p) => p.t)} unit="pts"
          series={[{ id: "q50", label: "median", values: t.m15_profile.map((p) => p.q50 ?? null) },
            { id: "q90", label: "1 in 10 candles is bigger than", values: t.m15_profile.map((p) => p.q90 ?? null), dashed: true }]} />
      </Card>
      <div className="grid-cards">
        <Card title="Weekdays">
          <TableWrap><table className="dense"><thead><tr><th>Day</th><th className="num">Range q25 / median / q75</th><th className="num">Up days</th></tr></thead>
            <tbody>{t.weekdays.map((w) => (
              <tr key={w.weekday}><td>{w.weekday}</td><td className="num">{q(w.range, "q25", 0)} / {q(w.range, "q50", 0)} / {q(w.range, "q75", 0)}</td>
                <td className="num">{r(w.up)}</td></tr>))}</tbody></table></TableWrap>
        </Card>
        {t.news_days && <Card title="Days with high-impact news vs without">
          <TableWrap><table className="dense"><thead><tr><th></th><th className="num">Range median</th>{types.map((x) => <th key={x} className="num">{x}</th>)}</tr></thead>
            <tbody>{Object.entries(t.news_days).map(([k, v]) => (
              <tr key={k}><td>{k === "without" ? "no high-impact news" : "high-impact news"}</td><td className="num">{q(v.range, "q50", 0)}</td>
                {types.map((x) => <td key={x} className="num">{pct(v.types[x]?.p, 0)}</td>)}</tr>))}</tbody></table></TableWrap>
        </Card>}
      </div>
    </div>
  );
}

// =============================================================================================== NQ vs ES
interface NqEsData {
  missing?: boolean; too_few?: boolean; paired_minutes?: number;
  relationship?: { tf: string; corr: number | null; beta: number | null; same_direction: Rate; by_year: Record<string, number> }[];
  lead_lag?: { lag_min: number; corr: number | null; band: number }[];
  divergence?: { episodes: number; per_day: number; closed: Rate; minutes_to_line_up: Quant | null; peak_z: Quant | null;
    nq_ahead: Rate; after_close_nq_keeps_reverting: Rate; by_session: { session: string; n: number; closed: Rate; minutes: Quant | null }[];
    nq_closed_it?: Rate; closed_by?: Record<"NQ" | "ES" | "both", Rate> };
}

export function MarketNqEsPage() {
  const { data, error } = useSection<NqEsData>("nqes");
  const pats = useSection<PatternRow[]>("patterns");
  if (error) return <NeedsAnalysis title="NQ vs ES" error={error} />;
  if (!data) return <div className="page"><PageHead title="NQ vs ES" /><PageSkeleton layout="overview" label="Loading" /></div>;
  const x = data.data;
  if (x.missing || x.too_few) return <div className="page"><PageHead title="NQ vs ES" /><Empty>Import ES (My strategy → Settings) and run the analysis again.</Empty></div>;
  const smt = (pats.data?.data ?? []).filter((p) => p.kind === "SMT");
  const dv = x.divergence;
  return (
    <div className="page" data-testid="market-nqes">
      <PageHead title="NQ vs ES" />
      <Card title="How closely they move">
        <TableWrap><table className="dense"><thead><tr><th>Chart</th><th className="num">Correlation</th><th className="num">NQ moves × ES</th>
          <th className="num">Candles the same colour</th><th>Correlation by year</th></tr></thead>
          <tbody>{(x.relationship ?? []).map((rr) => (
            <tr key={rr.tf}><td>{rr.tf}</td><td className="num">{num(rr.corr)}</td><td className="num">{num(rr.beta)}</td><td className="num">{r(rr.same_direction)}</td>
              <td className="small">{Object.entries(rr.by_year).map(([y, c]) => `${y}: ${c.toFixed(2)}`).join(" · ")}</td></tr>))}</tbody></table></TableWrap>
        <p className="small muted">"NQ moves × ES": NQ's usual move for a 1 % ES move (beta). Only minutes both markets have are compared ({x.paired_minutes?.toLocaleString()}).</p>
      </Card>
      <Card title="Who moves first (1-minute returns)">
        <BarChart categories={(x.lead_lag ?? []).map((l) => `${l.lag_min > 0 ? "+" : ""}${l.lag_min}`)} unit="corr" signed
          series={[{ id: "c", label: "correlation of NQ now with ES k minutes later", values: (x.lead_lag ?? []).map((l) => l.corr) }]} />
        <p className="small muted">A bar at +1 clearly above the noise band (±{x.lead_lag?.[0]?.band.toFixed(3)}) would mean NQ moves first and ES follows a minute later;
          at -1, ES leads.</p>
      </Card>
      {dv && <Card title="Divergences (move gap) and how they close" testId="market-divergence">
        <div className="kpis">
          <Kpi label="Per day" value={dv.per_day.toFixed(1)} sub={`${dv.episodes} episodes (gap ≥ 2× its usual size)`} />
          <Kpi label="Lined up again" value={pct(dv.closed.p, 0)} sub={`median ${q(dv.minutes_to_line_up, "q50", 0)} min (q25 ${q(dv.minutes_to_line_up, "q25", 0)}, q75 ${q(dv.minutes_to_line_up, "q75", 0)})`} />
          <Kpi label="Who closed the gap" value={dv.closed_by ? `NQ ${pct(dv.closed_by.NQ.p, 0)}` : pct(dv.nq_closed_it?.p, 0)} accent
            sub={dv.closed_by ? `ES caught up ${pct(dv.closed_by.ES.p, 0)} · both ${pct(dv.closed_by.both.p, 0)}` : "run the analysis again"} />
          <Kpi label="After closing, NQ keeps going back" value={pct(dv.after_close_nq_keeps_reverting.p, 0)} sub="next hour, toward ES" />
        </div>
        <TableWrap><table className="dense"><thead><tr><th>Session</th><th className="num">Episodes</th><th className="num">Closed</th><th className="num">Minutes (median)</th></tr></thead>
          <tbody>{dv.by_session.map((s) => <tr key={s.session}><td>{s.session}</td><td className="num">{s.n}</td><td className="num">{r(s.closed)}</td>
            <td className="num">{q(s.minutes, "q50", 0)}</td></tr>)}</tbody></table></TableWrap>
      </Card>}
      <Card title="SMT divergences at swings (NQ vs ES)">
        {!smt.length ? <Empty>No SMT events.</Empty> :
          <TableWrap><table className="dense"><thead><tr><th>Chart</th><th>Type</th><th className="num">Count</th><th className="num">Per day</th>
            <th className="num">1 ATR its way first</th></tr></thead>
            <tbody>{smt.map((p) => <tr key={`${p.tf}${p.dir}`}><td>{TF_NAMES[p.tf]}</td><td>{dirWord(p.dir)}</td><td className="num">{p.n}</td>
              <td className="num">{p.per_day.toFixed(2)}</td><td className="num">{pct(p.edge, 1)}</td></tr>)}</tbody></table></TableWrap>}
      </Card>
    </div>
  );
}

// =============================================================================================== patterns + edge scan
export function MarketPatternsPage() {
  const pats = useSection<PatternRow[]>("patterns");
  const eff = useSection<EffectRow[]>("effect_matrix");
  const edges = useSection<Edges>("edges");
  const [kind, setKind] = useState<string>("FVG");
  const [tf, setTf] = useState<string>("15");
  if (pats.error) return <NeedsAnalysis title="Patterns" error={pats.error} />;
  if (!pats.data) return <div className="page"><PageHead title="Patterns" /><PageSkeleton layout="overview" label="Loading" /></div>;
  const rows = pats.data.data;
  const kinds = pats.data.kinds;
  const kindOpts = [...new Set(rows.map((x) => x.kind))].map((k) => ({ value: k, label: kindName(kinds, k) }));
  const tfs = [...new Set(rows.filter((x) => x.kind === kind).map((x) => x.tf))].sort((a, b) => a - b);
  const sel = rows.filter((x) => x.kind === kind && String(x.tf) === tf);
  const effRows = (eff.data?.data ?? []).filter((e) => (e.kind === kind || e.kind === `${kind}_FORMED`) && e.tf === TF_NAMES[Number(tf)]);
  return (
    <div className="page" data-testid="market-patterns">
      <PageHead title="Patterns (ICT / SMC)" />
      <Card title="Pick a concept and a chart" testId="market-pattern-pick">
        <div className="row gap">
          <Select value={kind} onChange={(v) => { setKind(v); const t2 = rows.filter((x) => x.kind === v).map((x) => x.tf); if (!t2.includes(Number(tf))) setTf(String(t2[0] ?? 15)); }}
            options={kindOpts} ariaLabel="Concept" testId="market-kind" />
          <Select value={tf} onChange={setTf} options={tfs.map((t) => ({ value: String(t), label: TF_NAMES[t] ?? String(t) }))} ariaLabel="Chart" testId="market-tf" />
        </div>
        <TechDetails summary="How each concept is defined" rows={[["Definitions", "See the module documentation: FVG = 3 bars with a gap; touched / 50 % (CE) / filled = price entered / reached the middle / traded through; held = 1 ATR away from the zone before trading through it; left behind = never filled by the end of the data."]]} />
      </Card>
      {sel.map((p) => <PatternCard key={`${p.kind}${p.tf}${p.dir}`} p={p} kinds={kinds} eff={effRows.filter((e) => e.dir === p.dir)} />)}
      {!sel.length && <Empty>No instances of this concept on this chart.</Empty>}
      <EdgeScan edges={edges.data?.data} kinds={kinds} />
    </div>
  );
}

function PatternCard({ p, kinds, eff }: { p: PatternRow; kinds: Record<string, string>; eff: EffectRow[] }) {
  const zone = p.median_fill_min != null || p.filled > 0;
  return (
    <Card title={`${kindName(kinds, p.kind)} · ${TF_NAMES[p.tf]} · ${dirWord(p.dir)}`} testId={`market-pattern-${p.dir > 0 ? "bull" : "bear"}`}>
      <div className="kpis">
        <Kpi label="How often" value={p.n.toLocaleString()} sub={`${p.per_day.toFixed(2)} per day`} />
        {zone && <Kpi label="Filled" value={pct(p.filled, 0)}
          sub={`touched ${pct(p.touched, 0)} · 50 % ${pct(p.ce, 0)} · within 1h ${pct(p.filled_1h, 0)}, 1 day ${pct(p.filled_1d, 0)}, 5 days ${pct(p.filled_5d, 0)}`} />}
        {zone && <Kpi label="Left behind" value={p.left_behind.toLocaleString()} sub={p.left_behind_median_age_days != null ? `median ${p.left_behind_median_age_days.toFixed(0)} days old` : "none"} />}
        {zone && <Kpi label="Time to fill (median)" value={p.median_fill_min != null ? `${Math.round(p.median_fill_min)} min` : "–"}
          sub={p.median_touch_min != null ? `first touch after ${Math.round(p.median_touch_min)} min` : undefined} />}
        <Kpi label="Held / reacted" value={p.held != null ? pct(p.held, 0) : "–"} sub={`of ${p.held_n.toLocaleString()} with a result`} />
        <Kpi label="1 ATR its way first" value={p.edge != null ? pct(p.edge, 1) : "–"} sub={`${p.edge_n.toLocaleString()} cases · chance ≈ 50 %`} accent />
      </div>
      {eff.map((e) => (
        <div key={e.kind}>
          <h3 className="small-head">{e.kind.endsWith("_FORMED") ? "Effect on every timeframe right after it FORMS" : "Effect on every timeframe after price TOUCHES it (or after it happens)"}</h3>
          <TableWrap><table className="dense"><thead><tr><th>Next candle of</th>{e.effects.map((x) => <th key={x.tf} className="num">{x.tf}</th>)}</tr></thead>
            <tbody>
              <tr><td>went its way</td>{e.effects.map((x) => <td key={x.tf} className="num">{x.same_way != null ? pct(x.same_way, 0) : "–"}</td>)}</tr>
              <tr><td>size vs usual (median)</td>{e.effects.map((x) => <td key={x.tf} className="num">{x.size_median != null ? `${x.size_median.toFixed(2)}×` : "–"}</td>)}</tr>
            </tbody></table></TableWrap>
          <p className="small muted">On the 15-minute chart: a 15-min break of structure its way within the next 2 hours {pct(e.m15_bos, 0)}; the next four 15-min candles moved
            {" "}{e.m15_4_atr ? `${e.m15_4_atr.q25.toFixed(2)} / ${e.m15_4_atr.q50.toFixed(2)} / ${e.m15_4_atr.q75.toFixed(2)}` : "–"} 15-min ATRs its way (q25 / median / q75).</p>
        </div>))}
      <TechDetails summary="By session" rows={p.by_session.map((s) => [s.session, `${s.n} · 1 ATR its way ${s.edge != null ? pct(s.edge, 1) : "–"} · filled ${pct(s.filled, 0)}`])} />
    </Card>
  );
}

function EdgeGroups({ groups, kinds, tradeable, total }: { groups: EdgeGroup[]; kinds: Record<string, string>; tradeable: number; total: number }) {
  return (
    <>
      <Banner tone={tradeable ? "ok" : "info"} testId="market-edge-groups">The {total} confirmed combinations are <b>{groups.length} distinct effects</b> (the
        same effect under many conditions counts once here). Tradeable after costs: <b>{tradeable}</b>.</Banner>
      <TableWrap><table className="dense"><thead><tr><th>Effect</th><th>Outcome</th><th className="num">Best: first 70 % → last 30 % (usual)</th>
        <th className="num">Conditions</th><th>After costs</th></tr></thead>
        <tbody>{groups.slice(0, 40).map((g, i) => (
          <tr key={i}><td>{kindName(kinds, g.kind)} · {g.tf} · {g.more_often ? "works MORE often than usual" : "works LESS often (the opposite happens)"}</td>
            <td className="small">{OUTCOME_WORDS[g.outcome]}</td>
            <td className="num">{pct(g.best.rate_find, 1)} → {g.best.rate_confirm != null ? pct(g.best.rate_confirm, 1) : "–"} ({pct(g.best.base_find, 1)})</td>
            <td className="num" title={g.conditions.join(" | ")}>{g.cells}</td>
            <td>{g.outcome !== "edge" ? <span className="muted small">not a trade outcome</span> : g.tradeable ? <Badge tone="ok">clears costs</Badge> :
              <Badge>below break-even</Badge>}</td></tr>))}</tbody></table></TableWrap>
    </>
  );
}

function EdgeScan({ edges, kinds }: { edges?: Edges; kinds: Record<string, string> }) {
  if (!edges) return null;
  const line = (c: EdgeCell) => `${kindName(kinds, c.kind)} · ${c.tf} · ${dirWord(c.dir)} · ${c.condition}`;
  return (
    <Card title="Edge scan: found early, confirmed late" testId="market-edges">
      <p className="small">{edges.cells_tested.toLocaleString()} combinations were tested on the first {pct(edges.find_share, 0)} of the discovery period
        (only the first event per 15 minutes counts, each compared with the usual rate at the same time of day). {edges.passed_find} survived the
        false-discovery correction; <b>{edges.confirmed}</b> then went the same way on the last {pct(1 - edges.find_share, 0)}, which the scan never saw.</p>
      {edges.groups && edges.candidates.length > 0 && <EdgeGroups groups={edges.groups} kinds={kinds} tradeable={edges.tradeable ?? 0} total={edges.confirmed} />}
      {!edges.candidates.length ? <Banner tone="info">No edge candidates: nothing in these patterns did better than the usual rate by more than chance
        explains, once the number of combinations is taken into account.</Banner> :
        <TableWrap><table className="dense"><thead><tr><th>Pattern and condition</th><th>Outcome</th><th className="num">First 70 %</th><th className="num">Usual</th>
          <th className="num">Last 30 %</th><th className="num">Needed after costs</th></tr></thead>
          <tbody>{edges.candidates.slice(0, 60).map((c, i) => (
            <tr key={i}><td>{line(c)}</td><td className="small">{OUTCOME_WORDS[c.outcome]}</td>
              <td className="num">{pct(c.rate_find, 1)} ({c.n_find})</td><td className="num">{pct(c.base_find, 1)}</td>
              <td className="num">{c.rate_confirm != null ? `${pct(c.rate_confirm, 1)} (${c.n_confirm})` : "–"}</td>
              <td className="num">{c.breakeven != null ? pct(c.breakeven, 1) : "–"}{c.tradeable === true ? " ✓" : ""}</td></tr>))}</tbody></table></TableWrap>}
      <p className="small muted">"Needed after costs": the win rate a 1:1 trade at one ATR of that chart needs to cover the spread of the session the events
        happened in plus commission and slippage of one MNQ contract (✓ = the better side clears it). A candidate is not a strategy: it is something to test once, forward.</p>
    </Card>
  );
}

// =============================================================================================== news + shocks
interface NewsBlock { n: number; impacted: Rate; size15: Quant | null; first5_pts: Quant | null; surprise_direction: { positive_surprise_moves_nq_up: Rate };
  first5_continues_next_hour: Rate; pre30_same_as_first5: Rate; why_not: Record<string, number>; name?: string; impact?: number }
interface ShockBlock { n: number; per_day: number; changed_15m: Rate; m15_size: Quant | null; kept_at_15m_close: Quant | null; reversed_by_15m_close: Rate;
  next_15m_continues: Rate; returned_to_start: Rate; minutes_to_return: Quant | null; event?: string }
interface ShockData { all: ShockBlock; by_cause: Record<string, ShockBlock>; by_largest_timeframe: Record<string, ShockBlock>; by_event: ShockBlock[];
  recent: { start: number; main: string; move_pts: number; tfs: Record<string, number>; tags: { tag: string; detail?: string }[]; m15_kept: number | null; session: string }[] }

export function MarketNewsPage() {
  const nf = useSection<{ releases: number; by_impact: Record<string, NewsBlock>; by_event: NewsBlock[] } | null>("newsfx");
  const sh = useSection<ShockData>("shocks");
  if (sh.error) return <NeedsAnalysis title="News & shocks" error={sh.error} />;
  if (!sh.data) return <div className="page"><PageHead title="News & shocks" /><PageSkeleton layout="overview" label="Loading" /></div>;
  const n = nf.data?.data;
  const s = sh.data.data;
  return (
    <div className="page" data-testid="market-news-page">
      <PageHead title="News & shocks" />
      {!n ? <Banner tone="info">No news in this analysis: save your JBlanked key and download the news (Overview), then run the analysis again.</Banner> :
        <Card title={`What the news did (${n.releases} USD releases)`} testId="market-newsfx">
          <TableWrap><table className="dense"><thead><tr><th>Event</th><th className="num">Releases</th><th className="num">Moved the 15-min chart</th>
            <th className="num">15-min candle vs usual</th><th className="num">Surprise up → NQ up</th><th className="num">First 5 min continued</th><th>Why not (no reaction)</th></tr></thead>
            <tbody>{n.by_event.slice(0, 40).map((e) => (
              <tr key={e.name}><td>{e.impact === 3 ? <Badge tone="error">red</Badge> : e.impact === 2 ? <Badge tone="warn">orange</Badge> : <Badge>yellow</Badge>} {e.name}</td>
                <td className="num">{e.n}</td><td className="num">{r(e.impacted)}</td><td className="num">{q(e.size15, "q50", 2)}×</td>
                <td className="num">{r(e.surprise_direction.positive_surprise_moves_nq_up)}</td><td className="num">{r(e.first5_continues_next_hour)}</td>
                <td className="small">{Object.entries(e.why_not).filter(([, v]) => v > 0).map(([k, v]) => `${k}: ${v}`).join(" · ") || "–"}</td></tr>))}</tbody></table></TableWrap>
          <p className="small muted">"Moved the 15-min chart" = the release's 15-min candle was at least 1.5× its usual size for that time. "Surprise up → NQ up": of the
            releases with a clear surprise, how often a higher-than-forecast number pushed NQ up in the first 5 minutes (below 50 % = higher numbers push NQ down).</p>
        </Card>}
      <Card title="Shocks on every timeframe" testId="market-shocks">
        <p className="small">A shock = a bar at least 3× its usual size for that time of day, on any chart from 1 minute to 4 hours. Overlapping shock bars are one
          shock. {s.all.n} shocks, {s.all.per_day.toFixed(2)} per day.</p>
        <ShockTable rows={Object.entries(s.by_cause).map(([k, v]) => ({ name: k, ...v }))} first="Why" />
        <ShockTable rows={Object.entries(s.by_largest_timeframe).map(([k, v]) => ({ name: `biggest on ${k}`, ...v }))} first="Seen up to" />
        {s.by_event.length > 0 && <ShockTable rows={s.by_event.map((v) => ({ name: v.event ?? "", ...v }))} first="Scheduled event" />}
      </Card>
      <Card title="Recent shocks">
        <TableWrap><table className="dense"><thead><tr><th>When (New York)</th><th>Why</th><th className="num">Move</th><th>Seen on</th><th className="num">Kept at the 15-min close</th></tr></thead>
          <tbody>{s.recent.slice(-40).reverse().map((x) => (
            <tr key={x.start}><td>{new Date(x.start / 1e6).toLocaleString("en-GB", { timeZone: "America/New_York" })}</td>
              <td className="small">{x.main}{x.tags.filter((t) => t.detail).slice(0, 2).map((t) => ` · ${t.detail}`).join("")}</td>
              <td className="num">{x.move_pts.toFixed(1)}</td><td className="small">{Object.keys(x.tfs).join(" ")}</td>
              <td className="num">{x.m15_kept != null ? pct(x.m15_kept, 0) : "–"}</td></tr>))}</tbody></table></TableWrap>
      </Card>
    </div>
  );
}

function ShockTable({ rows, first }: { rows: (ShockBlock & { name: string })[]; first: string }) {
  return (
    <TableWrap><table className="dense"><thead><tr><th>{first}</th><th className="num">Count</th><th className="num">Changed the 15-min candle</th>
      <th className="num">Kept at 15-min close (median)</th><th className="num">Reversed by the close</th><th className="num">Next 15 min continued</th>
      <th className="num">Back to start (median min)</th></tr></thead>
      <tbody>{rows.map((x) => (
        <tr key={x.name}><td>{x.name}</td><td className="num">{x.n}</td><td className="num">{r(x.changed_15m)}</td><td className="num">{pct(x.kept_at_15m_close?.q50, 0)}</td>
          <td className="num">{r(x.reversed_by_15m_close)}</td><td className="num">{r(x.next_15m_continues)}</td>
          <td className="num">{pct(x.returned_to_start.p, 0)} · {q(x.minutes_to_return, "q50", 0)}</td></tr>))}</tbody></table></TableWrap>
  );
}

// =============================================================================================== simulator (day viewer)
export function MarketSimulatorPage() {
  const fc = useSection<Record<string, TargetEval>>("forecast");
  const [src, setSrc] = useState<"discovery" | "new" | "holdout">("discovery");
  const days = useApi<{ days: string[] }>(fc.data ? market.daysUrl(src) : null, [src, fc.data?.key]);
  const [day, setDay] = useState<string>("");
  useEffect(() => { if (days.data && !days.data.days.includes(day)) setDay(days.data.days[days.data.days.length - 1] ?? ""); }, [days.data]); // eslint-disable-line react-hooks/exhaustive-deps
  if (fc.error) return <NeedsAnalysis title="Simulator" error={fc.error} />;
  if (!fc.data) return <div className="page"><PageHead title="Simulator" /><PageSkeleton layout="overview" label="Loading" /></div>;
  const f = fc.data.data;
  return (
    <div className="page" data-testid="market-sim">
      <PageHead title="Simulator · 15-minute forecasts" />
      <Card title="How good are the forecasts? (months the models never saw)" testId="market-scores">
        {(["up", "size", "bias", "levels"] as const).map((t) => f[t] && <TargetScores key={t} t={t} e={f[t]} />)}
        <p className="small muted">Every month was predicted by models trained only on the months before it. The highlighted model was chosen on the earlier
          70 % of those months; its score on the later 30 % is the honest one. Bands for the candle size: {f.size?.bands ? `${pct(f.size.bands.inside_50, 0)} of candles
          fell inside the 50 % band and ${pct(f.size.bands.inside_80, 0)} inside the 80 % band` : "–"} (well calibrated = 50 % and 80 %).</p>
      </Card>
      <Card title="Pick a day" testId="market-day-pick">
        <div className="row gap">
          <Select value={src} onChange={(v) => setSrc(v as "discovery" | "new" | "holdout")} ariaLabel="Days" testId="market-src"
            options={[{ value: "discovery", label: "Discovery (walk-forward predictions)" }, { value: "new", label: "New days (live)" },
              { value: "holdout", label: "Holdout (after the one look)" }]} />
          {days.data && days.data.days.length > 0 ?
            <Select value={day} onChange={setDay} ariaLabel="Day" testId="market-day-select"
              options={[...days.data.days].reverse().map((d) => ({ value: d, label: d }))} /> :
            <span className="muted small">{src === "new" ? "No new days scored yet (Overview → New days)." : src === "holdout" ?
              "The holdout test has not been run." : "No predicted days."}</span>}
        </div>
      </Card>
      {day && <DayCard key={`${src}/${day}`} day={day} src={src} />}
      <HoldoutTestCard />
    </div>
  );
}

function TargetScores({ t, e }: { t: string; e: TargetEval }) {
  const sc = e.scores ?? {};
  return (
    <>
      <h3 className="small-head">{TARGET_WORDS[t]}</h3>
      <TableWrap><table className="dense"><thead><tr><th>Model</th><th className="num">Skill, all months</th><th className="num">Skill, later months</th>
        {t !== "size" && <th className="num">Hit rate</th>}<th>Real?</th></tr></thead>
        <tbody>{Object.entries(sc).map(([k, s]) => (
          <tr key={k} className={k === e.chosen ? "row-hl" : ""}><td>{MODEL_WORDS[k] ?? k}{k === e.chosen ? " ★" : ""}</td>
            <td className="num">{skill(s.all)}</td><td className="num">{skill(s.late)}</td>
            {t !== "size" && <td className="num">{s.all?.accuracy != null ? pct(s.all.accuracy, 1) : "–"}</td>}
            <td>{k === "baseline" ? <span className="muted">reference</span> : <RealBadge s={s.late} />}</td></tr>))}</tbody></table></TableWrap>
    </>
  );
}

function DayCard({ day, src }: { day: string; src: string }) {
  const { data, error } = useApi<DayView>(market.dayUrl(day, src), [day, src]);
  const [model, setModel] = useState<string>("");
  const chosen = data?.chosen.up ?? "logistic";
  const mdl = model || chosen;
  const lines = useMemo<PriceLine[]>(() => {
    if (!data) return [];
    const first = data.levels.length ? Math.min(...data.levels.map((l) => l.t)) : 0;
    const hi = Math.max(...data.candles.map((c) => c.h)), lo = Math.min(...data.candles.map((c) => c.l));
    const span = Math.max(hi - lo, 1);
    return data.levels.filter((l) => l.t === first && l.price <= hi + 0.5 * span && l.price >= lo - 0.5 * span).slice(0, 10).map((l) => ({ price: l.price, label: `${l.name} ${pct(l[mdl] as number, 0)}`,
      color: l.reached ? "var(--c-pos)" : "var(--muted)", dash: "4 3" }));
  }, [data, mdl]);
  const markers = useMemo<Marker[]>(() => {
    if (!data) return [];
    const ms: Marker[] = data.news.filter((n) => n.impact >= 2).map((n) => {
      const c = data.candles.find((x) => x.t <= n.t && n.t < x.t + 15 * 60e9) ?? data.candles[0];
      return { t: Math.floor(n.t / 1e9), price: c ? c.h : 0, label: n.impact === 3 ? `★ ${n.name}` : n.name, color: n.impact === 3 ? "var(--c-neg)" : "var(--c-survivor)" };
    });
    for (const s of data.shocks) {
      const c = data.candles.find((x) => x.t <= s.start && s.start < x.t + 15 * 60e9);
      if (c) ms.push({ t: Math.floor(s.start / 1e9), price: s.move_pts > 0 ? c.h : c.l, label: "shock", color: "var(--c2)" });
    }
    return ms;
  }, [data]);
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <PageSkeleton layout="overview" label="Loading the day" />;
  const candles = data.candles.map((c) => [Math.floor(c.t / 1e9), c.o, c.h, c.l, c.c] as [number, number, number, number, number]);
  const rth = data.candles.filter((c) => { const h = Number(new Date(c.t / 1e6).toLocaleString("en-GB", { timeZone: "America/New_York", hour: "2-digit", hour12: false }));
    return h >= 8 && h < 17; });
  const models = ["logistic", "boosting", "similar", "baseline"];
  return (
    <>
      <Card title={`${day} · 15-minute chart`} testId="market-day" actions={<Select value={mdl} onChange={setModel} ariaLabel="Model"
        options={models.map((m) => ({ value: m, label: `${MODEL_WORDS[m]}${m === chosen ? " ★" : ""}` }))} />}>
        <CandleChart candles={candles} tfMinutes={15} lines={lines} markers={markers} height={420} testId="market-day-chart" />
        <p className="small muted">Dashed lines: the levels at 9:30 with the model's chance of being reached before the close (green = it was). Markers: red /
          orange news, shocks (their causes are in the News tab). Levels far outside the day's range are left off the chart. Predictions were made
          before each candle opened, by models that never saw this day.</p>
      </Card>
      <Card title="Candle by candle (8:00 - 17:00 New York)" testId="market-day-table">
        <TableWrap><table className="dense"><thead><tr><th>Time</th><th className="num">Chance up</th><th>Actual</th><th className="num">Size band (×usual)</th>
          <th className="num">Actual size</th><th>Why (logistic: biggest pushes)</th></tr></thead>
          <tbody>{rth.map((c) => {
            const p = c.p_up?.[mdl];
            const act = c.c > c.o ? "up" : c.c < c.o ? "down" : "flat";
            const hit = p != null && act !== "flat" ? ((p > 0.5) === (act === "up")) : null;
            const band = c.size_q && c.size_q[1] != null ? `${Math.exp(c.size_q[1] as number).toFixed(2)} – ${Math.exp(c.size_q[2] as number).toFixed(2)}` : "–";
            return (
              <tr key={c.t}><td>{new Date(c.t / 1e6).toLocaleTimeString("en-GB", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit" })}</td>
                <td className="num">{p != null ? pct(p, 0) : "–"}</td>
                <td>{act}{hit == null ? "" : hit ? " ✓" : " ✗"}</td><td className="num">{band}</td>
                <td className="num">{c.actual_size != null ? `${Math.exp(c.actual_size).toFixed(2)}` : "–"}</td>
                <td className="small">{(c.why ?? []).map((w) => `${w.input} ${w.push != null && w.push > 0 ? "↑" : "↓"}`).join(" · ")}</td></tr>);
          })}</tbody></table></TableWrap>
      </Card>
      {data.bias.length > 0 && <Card title="Daily bias: does the session close above the price at that moment?">
        <TableWrap><table className="dense"><thead><tr><th>Time</th>{models.map((m) => <th key={m} className="num">{MODEL_WORDS[m]}</th>)}</tr></thead>
          <tbody>{data.bias.map((b) => <tr key={b.t}><td>{new Date(b.t / 1e6).toLocaleTimeString("en-GB", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit" })}</td>
            {models.map((m) => <td key={m} className="num">{pct(b[m], 0)}</td>)}</tr>)}</tbody></table></TableWrap>
      </Card>}
      {data.news.length > 0 && <Card title="News that day">
        <ul className="small">{data.news.map((n, i) => <li key={i}>{new Date(n.t / 1e6).toLocaleTimeString("en-GB", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit" })}
          {" "}{n.name} ({["none", "yellow", "orange", "red"][n.impact]}) · forecast {n.forecast ?? "–"} · actual {n.actual ?? "–"}{n.surprise_z != null ? ` · surprise ${n.surprise_z.toFixed(1)}σ` : ""}</li>)}</ul>
      </Card>}
    </>
  );
}


// =============================================================================================== holdout prediction test (ADR-107)
const HOLDOUT_TARGETS: [string, string][] = [["size", "Size of the next 15-min candle"], ["levels", "Level reached before the session ends"],
  ["up", "Next 15-min candle up or down"], ["bias", "Session closes above the current price"]];

function HoldoutTestCard() {
  const { data, error, reload } = useApi<HoldoutStatus>(market.holdoutUrl);
  const [job, setJob] = useMarketJob(reload);
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [err, setErr] = useState<ApiError | null>(null);
  if (error) return <ErrorPanel error={error} />;
  if (!data) return null;
  const running = job?.state === "running";
  const start = async () => { setErr(null); try { setJob(await market.runHoldout(typed)); setOpen(false); setTyped(""); } catch (e) { setErr(e as ApiError); } };
  const res = data.result;
  return (
    <Card title="Holdout prediction test (one look)" testId="market-holdout">
      {!data.available ? <Banner tone="warn">{data.problem}</Banner> : !data.used ? (
        <>
          <p>Freezes the models trained on the whole discovery period, then predicts every 15-minute candle of your holdout
            ({data.holdout?.start.slice(0, 10)} – {data.holdout?.end.slice(0, 10)}) with live knowledge only and scores them against the same baselines.
            For each forecast the model chosen on discovery is the official one; the others are shown, marked as not chosen in advance.</p>
          <ul className="small edge-list">
            <li>It is <b>one recorded look</b> at the holdout (its own entry in the protocol ledger). It cannot be repeated or redone with changes.</li>
            <li>Run the analysis again first if anything changed (data, news, settings): the test refuses an outdated analysis.</li>
            <li>What discovery says to expect: the candle SIZE well (about +24 % better than usual), levels a little (+4 %), direction not at all.</li>
          </ul>
          {running && <Banner tone="info"><span className="spinner" /> {job?.step}</Banner>}
          {job?.state === "failed" && <Banner tone="error">{job.error?.message}</Banner>}
          <ErrorPanel error={err} />
          <Button kind="primary" onClick={() => setOpen(true)} disabled={running} testId="market-holdout-btn">Run the holdout test</Button>
          <Confirm open={open} title="Use the one holdout look of the Market simulator?" confirmLabel="Start" danger busy={running}
            onCancel={() => { setOpen(false); setTyped(""); }} onConfirm={() => void start()}>
            <p>This spends the Market simulator's single holdout look. It cannot be undone. Type <b>HOLDOUT</b> to confirm.</p>
            <TextInput value={typed} onChange={setTyped} ariaLabel="Type HOLDOUT" testId="market-holdout-typed" />
          </Confirm>
        </>) : !res ? (
          <Banner tone={data.look?.status === "failed" ? "error" : "info"}>
            {data.look?.status === "failed" ? "The look was recorded but the test failed; the look stays spent." : <><span className="spinner" /> Running…</>}</Banner>
        ) : (
        <>
          <p className="small">Look {res.access_id} · {res.days} holdout days · {res.candles.toLocaleString()} candles · models frozen on discovery
            (fingerprint {res.fingerprint}). Skill = better than the baseline; "real" = its 95 % range (resampling whole days) stays above 0.</p>
          <TableWrap><table className="dense" data-testid="market-holdout-result"><thead><tr><th>Forecast</th><th>Official model</th>
            <th className="num">Skill on the holdout</th><th className="num">Hit rate</th><th>Real?</th><th>Other models (not chosen in advance)</th></tr></thead>
            <tbody>{HOLDOUT_TARGETS.map(([k, w]) => { const tg = res.targets[k]; if (!tg) return null; const s = tg.official;
              return (
                <tr key={k}><td>{w}</td><td>{MODEL_WORDS[tg.official_model]}</td><td className="num">{skill(s)}</td>
                  <td className="num">{s?.accuracy != null ? pct(s.accuracy, 1) : "–"}</td><td><RealBadge s={s} /></td>
                  <td className="small">{Object.entries(tg.models).filter(([m]) => m !== tg.official_model && m !== "baseline")
                    .map(([m, x]) => `${MODEL_WORDS[m]} ${x?.skill != null ? (x.skill * 100).toFixed(1) + " %" : "–"}`).join(" · ")}</td></tr>);
            })}</tbody></table></TableWrap>
          {res.targets.size?.bands && <p className="small">Size bands on the holdout: {pct(res.targets.size.bands.inside_50, 0)} of candles inside the 50 % band,
            {" "}{pct(res.targets.size.bands.inside_80, 0)} inside the 80 % band (calibrated = 50 % and 80 %).</p>}
          <TechDetails summary="Skill by month (official models)" rows={HOLDOUT_TARGETS.filter(([k]) => res.targets[k]).map(([k, w]) =>
            [w, res.targets[k].by_month.map((x) => `${x.month}: ${x.skill != null ? (x.skill * 100).toFixed(1) : "–"} %`).join(" · ")])} />
          <p className="small muted">Pick "Holdout (after the one look)" above to see each holdout day candle by candle.</p>
        </>)}
    </Card>
  );
}
