/* Market simulator → Market behaviour (ADR-110): how NQ behaves in the discovery period, in three views - days &
   sessions, NQ vs ES, news & shocks - each with charts first and the exact numbers in tables below. */
import type { PatternRow, Quant, Rate } from "../../api/market";
import { go, useRoute } from "../../app/router";
import { BarChart, LineChart } from "../../components/charts";
import { Badge, Banner, Card, Empty, Kpi, PageSkeleton, TableWrap, Tabs, pct } from "../../components/ui";
import { PageHead } from "../MyStrategy";
import { NotReady, TF_NAMES, dirWord, num, outside, q, r, useSection } from "./shared";

interface TrendData {
  day_types: Record<string, Rate>; day_type_next: Record<string, Record<string, Rate>>; time_of_high: number[]; time_of_low: number[]; time_bins: string[];
  power_of_3: Record<string, Rate>; sessions: { session: string; range_pts: Quant | null; share_of_day: Quant | null; makes_high: Rate; makes_low: Rate }[];
  m15_after_runs: { k: number; after: string; next_same: Rate }[]; m15_up_rate: Rate; m15_autocorr: { lag: number; corr: number; band: number; n: number }[];
  m15_profile: ({ t: string; up: Rate } & Quant)[]; weekdays: { weekday: string; range: Quant | null; up: Rate }[];
  news_days?: Record<string, { range: Quant | null; types: Record<string, Rate> }>;
  chance?: { method: string; real: Record<string, Rate>; chance: Record<string, Rate>; time_of_high_real: number[]; time_of_high_chance: number[] };
}
interface NqEsData {
  missing?: boolean; too_few?: boolean; paired_minutes?: number;
  relationship?: { tf: string; corr: number | null; beta: number | null; same_direction: Rate; by_year: Record<string, number> }[];
  lead_lag?: { lag_min: number; corr: number | null; band: number }[];
  divergence?: { episodes: number; per_day: number; closed: Rate; minutes_to_line_up: Quant | null; peak_z: Quant | null;
    nq_ahead: Rate; after_close_nq_keeps_reverting: Rate; by_session: { session: string; n: number; closed: Rate; minutes: Quant | null }[];
    nq_closed_it?: Rate; closed_by?: Record<"NQ" | "ES" | "both", Rate> };
}
interface NewsBlock { n: number; impacted: Rate; size15: Quant | null; first5_pts: Quant | null; surprise_direction: { positive_surprise_moves_nq_up: Rate };
  first5_continues_next_hour: Rate; pre30_same_as_first5: Rate; why_not: Record<string, number>; name?: string; impact?: number }
interface ShockBlock { n: number; per_day: number; changed_15m: Rate; m15_size: Quant | null; kept_at_15m_close: Quant | null; reversed_by_15m_close: Rate;
  next_15m_continues: Rate; returned_to_start: Rate; minutes_to_return: Quant | null; event?: string }
interface ShockData { all: ShockBlock; by_cause: Record<string, ShockBlock>; by_largest_timeframe: Record<string, ShockBlock>; by_event: ShockBlock[];
  recent: { start: number; main: string; move_pts: number; tfs: Record<string, number>; tags: { tag: string; detail?: string }[]; m15_kept: number | null; session: string }[] }

type View = "days" | "nqes" | "news";
const VIEWS: { id: View; label: string }[] = [{ id: "days", label: "Days & sessions" }, { id: "nqes", label: "NQ vs ES" }, { id: "news", label: "News & shocks" }];
const pc = (v: number | null | undefined) => (v == null ? null : v * 100);

export function MarketBehaviourPage({ initial }: { initial?: View }) {
  const route = useRoute();
  const v = (route.query.get("view") as View | null) ?? initial ?? "days";
  return (
    <div className="page mk" data-testid="market-behaviour">
      <PageHead title="Market behaviour" />
      <p className="mk-lead">How NQ behaved in your discovery period: what a day looks like, how ES moves with it, and what news and sudden moves
        did. Descriptive statistics, measured once; nothing here is a prediction.</p>
      <Tabs tabs={VIEWS} active={v} onChange={(x) => go(`/market-behaviour?view=${x}`)} />
      {v === "days" ? <DaysView /> : v === "nqes" ? <NqEsView /> : <NewsView />}
    </div>
  );
}

// =============================================================================================== days & sessions
function DaysView() {
  const { data, error } = useSection<TrendData>("trend");
  if (error) return <NotReady error={error} />;
  if (!data) return <PageSkeleton layout="overview" label="Loading" />;
  const t = data.data;
  const types = Object.keys(t.day_types);
  return (
    <>
      <div className="grid-cards">
        <Card title="What kind of day is it?" testId="market-daytypes">
          <BarChart categories={types} unit="% of days" signed={false} height={190}
            series={[{ id: "p", label: "share of regular sessions", values: types.map((x) => pc(t.day_types[x]?.p)) }]} />
          <p className="small muted">Regular session 9:30–16:00. Trend day = closes in its top (bottom) 20 % with at least the usual range of the
            last 20 sessions; range day = closes in the middle 30–70 % with a smaller range; everything else is "normal".</p>
        </Card>
        <Card title="What follows each kind of day">
          <TableWrap><table className="dense"><thead><tr><th>Today</th>{types.map((x) => <th key={x} className="num">Next: {x}</th>)}</tr></thead>
            <tbody>{types.map((x) => (
              <tr key={x}><td>{x} <span className="faint">({t.day_types[x]?.n ? `${t.day_types[x].k} days` : ""})</span></td>
                {types.map((y) => <td key={y} className="num">{pct(t.day_type_next[x]?.[y]?.p, 0)}</td>)}</tr>))}
              <tr className="mk-ref"><td>any day (usual)</td>{types.map((y) => <td key={y} className="num">{pct(t.day_types[y]?.p, 0)}</td>)}</tr></tbody></table></TableWrap>
          <p className="small muted">Read across a row and compare it with the last row: a clear difference means the day before matters.</p>
        </Card>
      </div>
      <Card title="When the regular session's high and low form" testId="market-chance">
        <BarChart categories={t.time_bins} unit="days" signed={false}
          series={[{ id: "h", label: "high", values: t.time_of_high }, { id: "l", label: "low", values: t.time_of_low },
            ...(t.chance ? [{ id: "c", label: "high, if moves were random", values: t.chance.time_of_high_chance }] : [])]} />
        {t.chance && <TableWrap><table className="dense"><thead><tr><th>Pattern</th><th className="num">Real</th><th className="num">If moves were random</th></tr></thead>
          <tbody>{([["up_days_low_first", "Up days: low before high (Power of 3)"], ["down_days_high_first", "Down days: high before low"],
            ["high_or_low_in_first_hour", "High or low inside the first hour"]] as const).map(([k, w]) => (
            <tr key={k}><td>{w}</td><td className="num">{r(t.chance!.real[k])}</td><td className="num">{pct(t.chance!.chance[k]?.p, 1)}
              {outside(t.chance!.real[k], t.chance!.chance[k]?.p) ? " *" : ""}</td></tr>))}</tbody></table></TableWrap>}
        <p className="small muted">"If moves were random" uses the day's real 1-minute moves with random signs (same volatility through the day, no
          direction pattern). Equal numbers mean the shape comes from how prices move, not from a pattern; * = clearly different.</p>
      </Card>
      <div className="grid-cards">
        <Card title="Sessions: how much each one moves">
          <BarChart categories={t.sessions.map((s) => s.session)} unit="pts" signed={false} height={190}
            series={[{ id: "r", label: "median range (points)", values: t.sessions.map((s) => s.range_pts?.q50 ?? null) }]} />
        </Card>
        <Card title="Sessions: who makes the day's high and low">
          <BarChart categories={t.sessions.map((s) => s.session)} unit="%" signed={false} height={190}
            series={[{ id: "h", label: "makes the high", values: t.sessions.map((s) => pc(s.makes_high.p)) },
              { id: "l", label: "makes the low", values: t.sessions.map((s) => pc(s.makes_low.p)) }]} />
        </Card>
      </div>
      <Card title="15-minute candle size through the day (New York time)">
        <LineChart x={t.m15_profile.map((p) => p.t)} unit="pts" fmtX={(s) => s}
          series={[{ id: "q50", label: "median", values: t.m15_profile.map((p) => p.q50 ?? null) },
            { id: "q90", label: "1 in 10 candles is bigger than", values: t.m15_profile.map((p) => p.q90 ?? null), dashed: true }]} />
      </Card>
      <Card title="15-minute candles: does direction carry on?">
        <BarChart categories={[1, 2, 3, 4, 5].map((k) => `after ${k}`)} unit="%" signed={false} height={190}
          series={["up", "down"].map((d) => ({ id: d, label: `next candle ${d} again, after ${d} candles in a row`,
            values: [1, 2, 3, 4, 5].map((k) => pc(t.m15_after_runs.find((y) => y.k === k && y.after === d)?.next_same.p)) }))} />
        <p className="small">All 15-minute candles: {r(t.m15_up_rate)} up. Around 50 % in every bar means a run of candles says nothing about the
          next one. Autocorrelation (lag 1–8): {t.m15_autocorr.map((a) => `${a.corr.toFixed(3)}${Math.abs(a.corr) > a.band ? "*" : ""}`).join(" · ")}
          {" "}(* = outside the ±{t.m15_autocorr[0]?.band.toFixed(3)} band chance gives).</p>
      </Card>
      <div className="grid-cards">
        <Card title="Weekdays">
          <BarChart categories={t.weekdays.map((w) => w.weekday)} unit="pts" signed={false} height={170}
            series={[{ id: "r", label: "median day range (points)", values: t.weekdays.map((w) => w.range?.q50 ?? null) }]} />
          <TableWrap><table className="dense"><thead><tr><th>Day</th><th className="num">Range q25 / median / q75</th><th className="num">Up days</th></tr></thead>
            <tbody>{t.weekdays.map((w) => (
              <tr key={w.weekday}><td>{w.weekday}</td><td className="num">{q(w.range, "q25", 0)} / {q(w.range, "q50", 0)} / {q(w.range, "q75", 0)}</td>
                <td className="num">{r(w.up)}</td></tr>))}</tbody></table></TableWrap>
        </Card>
        {t.news_days ? <Card title="Days with and without high-impact news">
          <BarChart categories={types} unit="% of days" signed={false} height={170}
            series={Object.entries(t.news_days).map(([k, v]) => ({ id: k, label: k === "without" ? "no high-impact news" : "high-impact news",
              values: types.map((x) => pc(v.types[x]?.p)) }))} />
          <p className="small muted">Median day range: {Object.entries(t.news_days).map(([k, v]) => `${k === "without" ? "without news" : "with news"} ${q(v.range, "q50", 0)} pts`).join(" · ")}.</p>
        </Card> : <Card title="Days with and without high-impact news"><Empty>Download the news (Start here, step 2) and run the analysis again.</Empty></Card>}
      </div>
    </>
  );
}

// =============================================================================================== NQ vs ES
function NqEsView() {
  const { data, error } = useSection<NqEsData>("nqes");
  const pats = useSection<PatternRow[]>("patterns");
  if (error) return <NotReady error={error} />;
  if (!data) return <PageSkeleton layout="overview" label="Loading" />;
  const x = data.data;
  if (x.missing || x.too_few) return <Empty>Import ES (My strategy → Settings) and run the analysis again.</Empty>;
  const smt = (pats.data?.data ?? []).filter((p) => p.kind === "SMT");
  const dv = x.divergence;
  const rel = x.relationship ?? [];
  return (
    <div className="mk-stack" data-testid="market-nqes">
      <div className="grid-cards">
        <Card title="How closely they move">
          <BarChart categories={rel.map((rr) => rr.tf)} unit="corr" signed={false} height={180}
            series={[{ id: "c", label: "correlation of NQ and ES moves", values: rel.map((rr) => rr.corr) }]} />
          <TableWrap><table className="dense"><thead><tr><th>Chart</th><th className="num">Correlation</th><th className="num">NQ move per ES move</th>
            <th className="num">Same colour</th></tr></thead>
            <tbody>{rel.map((rr) => (
              <tr key={rr.tf}><td>{rr.tf}</td><td className="num">{num(rr.corr)}</td><td className="num">{num(rr.beta)}×</td><td className="num">{r(rr.same_direction)}</td></tr>))}</tbody></table></TableWrap>
          <p className="small muted">Only minutes both markets have are compared ({x.paired_minutes?.toLocaleString()}).</p>
        </Card>
        <Card title="Who moves first?">
          <BarChart categories={(x.lead_lag ?? []).map((l) => `${l.lag_min > 0 ? "+" : ""}${l.lag_min} min`)} unit="corr" signed height={180}
            series={[{ id: "c", label: "NQ now vs ES k minutes later", values: (x.lead_lag ?? []).map((l) => l.corr) }]} />
          <p className="small muted">A bar at +1 clearly above the noise band (±{x.lead_lag?.[0]?.band.toFixed(3)}) would mean NQ moves first and ES follows;
            at −1, ES leads. Bars inside the band: neither leads.</p>
        </Card>
      </div>
      {dv && <Card title="When NQ and ES drift apart" testId="market-divergence">
        <div className="kpis">
          <Kpi label="Per day" value={dv.per_day.toFixed(1)} sub={`${dv.episodes} episodes (gap at least 2× its usual size)`} />
          <Kpi label="Lined up again" value={pct(dv.closed.p, 0)} sub={`median ${q(dv.minutes_to_line_up, "q50", 0)} min`} />
          <Kpi label="NQ closed the gap" value={dv.closed_by ? pct(dv.closed_by.NQ.p, 0) : pct(dv.nq_closed_it?.p, 0)} accent
            sub={dv.closed_by ? `ES caught up ${pct(dv.closed_by.ES.p, 0)} · both ${pct(dv.closed_by.both.p, 0)}` : undefined} />
          <Kpi label="NQ keeps going back next hour" value={pct(dv.after_close_nq_keeps_reverting.p, 0)} sub="toward ES, after the gap closed" />
        </div>
        <BarChart categories={dv.by_session.map((s) => s.session)} unit="%" signed={false} height={170}
          series={[{ id: "c", label: "gaps that closed", values: dv.by_session.map((s) => pc(s.closed.p)) }]}
          sub={(i) => `${dv.by_session[i].n} episodes · median ${q(dv.by_session[i].minutes, "q50", 0)} min`} />
      </Card>}
      <Card title="SMT divergences at swings">
        {!smt.length ? <Empty>No SMT events.</Empty> :
          <TableWrap><table className="dense"><thead><tr><th>Chart</th><th>Type</th><th className="num">Count</th><th className="num">Per day</th>
            <th className="num">Moved 1 ATR its way first</th></tr></thead>
            <tbody>{smt.map((p) => <tr key={`${p.tf}${p.dir}`}><td>{TF_NAMES[p.tf]}</td><td>{dirWord(p.dir)}</td><td className="num">{p.n}</td>
              <td className="num">{p.per_day.toFixed(2)}</td><td className="num">{pct(p.edge, 1)}</td></tr>)}</tbody></table></TableWrap>}
        <p className="small muted">Chance for "moved 1 ATR its way first" is about 50 %.</p>
      </Card>
    </div>
  );
}

// =============================================================================================== news & shocks
function NewsView() {
  const nf = useSection<{ releases: number; by_impact: Record<string, NewsBlock>; by_event: NewsBlock[] } | null>("newsfx");
  const sh = useSection<ShockData>("shocks");
  if (sh.error) return <NotReady error={sh.error} />;
  if (!sh.data) return <PageSkeleton layout="overview" label="Loading" />;
  const n = nf.data?.data;
  const s = sh.data.data;
  const top = (n?.by_event ?? []).slice(0, 12);
  const causes = Object.entries(s.by_cause);
  return (
    <div className="mk-stack" data-testid="market-news-page">
      {!n ? <Banner tone="info">No news in this analysis: save your JBlanked key and download the news (Start here, step 2), then run the analysis again.</Banner> : <>
        <Card title={`What the news did (${n.releases} USD releases)`} testId="market-newsfx">
          <BarChart categories={top.map((e) => e.name ?? "")} unit="%" signed={false} height={200}
            series={[{ id: "m", label: "release moved the 15-min chart", values: top.map((e) => pc(e.impacted.p)) },
              { id: "c", label: "first 5 min carried on for an hour", values: top.map((e) => pc(e.first5_continues_next_hour.p)) }]}
            sub={(i) => `${top[i].n} releases`} />
          <p className="small muted">"Moved the 15-min chart" = that 15-minute candle was at least 1.5× its usual size for the time of day. The most
            frequent events are shown; all are in the table.</p>
        </Card>
        <Card title="Every event">
          <TableWrap><table className="dense"><thead><tr><th>Event</th><th className="num">Releases</th><th className="num">Moved the 15-min chart</th>
            <th className="num">15-min candle vs usual</th><th className="num">Higher number → NQ up</th><th className="num">First 5 min carried on</th></tr></thead>
            <tbody>{n.by_event.slice(0, 40).map((e) => (
              <tr key={e.name}><td>{e.impact === 3 ? <Badge tone="error">red</Badge> : e.impact === 2 ? <Badge tone="warn">orange</Badge> : <Badge>yellow</Badge>} {e.name}</td>
                <td className="num">{e.n}</td><td className="num">{r(e.impacted)}</td><td className="num">{q(e.size15, "q50", 2)}×</td>
                <td className="num">{r(e.surprise_direction.positive_surprise_moves_nq_up)}</td><td className="num">{r(e.first5_continues_next_hour)}</td></tr>))}</tbody></table></TableWrap>
          <p className="small muted">"Higher number → NQ up": of the releases with a clear surprise, how often a higher-than-forecast number pushed NQ up
            in the first 5 minutes (below 50 % = higher numbers push NQ down).</p>
        </Card></>}
      <Card title="Sudden moves (shocks)" testId="market-shocks">
        <p className="small">A shock is a bar in the top 0.1 % of its size for that time of day, on any chart from 1 minute to 4 hours. Overlapping
          shock bars count once. {s.all.n} shocks, {s.all.per_day.toFixed(2)} per day.</p>
        <BarChart categories={causes.map(([k]) => k)} unit="shocks" signed={false} height={180}
          series={[{ id: "n", label: "shocks by cause", values: causes.map(([, v]) => v.n) }]}
          sub={(i) => `kept ${pct(causes[i][1].kept_at_15m_close?.q50, 0)} at the 15-min close`} />
        <ShockTable rows={causes.map(([k, v]) => ({ name: k, ...v }))} first="Cause" />
        <ShockTable rows={Object.entries(s.by_largest_timeframe).map(([k, v]) => ({ name: `biggest on ${k}`, ...v }))} first="Seen up to" />
      </Card>
      <Card title="Most recent shocks">
        <TableWrap><table className="dense"><thead><tr><th>When (New York)</th><th>Why</th><th className="num">Move (pts)</th><th>Seen on</th>
          <th className="num">Kept at the 15-min close</th></tr></thead>
          <tbody>{s.recent.slice(-25).reverse().map((x) => (
            <tr key={x.start}><td>{new Date(x.start / 1e6).toLocaleString("en-GB", { timeZone: "America/New_York", dateStyle: "short", timeStyle: "short" })}</td>
              <td className="small">{x.main}{x.tags.filter((t) => t.detail).slice(0, 1).map((t) => ` · ${t.detail}`).join("")}</td>
              <td className="num">{x.move_pts.toFixed(1)}</td><td className="small">{Object.keys(x.tfs).join(" ")}</td>
              <td className="num">{x.m15_kept != null ? pct(x.m15_kept, 0) : "–"}</td></tr>))}</tbody></table></TableWrap>
      </Card>
    </div>
  );
}

function ShockTable({ rows, first }: { rows: (ShockBlock & { name: string })[]; first: string }) {
  return (
    <TableWrap><table className="dense"><thead><tr><th>{first}</th><th className="num">Count</th><th className="num">Changed the 15-min candle</th>
      <th className="num">Kept at the close (median)</th><th className="num">Reversed by the close</th><th className="num">Next 15 min carried on</th>
      <th className="num">Back to the start</th></tr></thead>
      <tbody>{rows.map((x) => (
        <tr key={x.name}><td>{x.name}</td><td className="num">{x.n}</td><td className="num">{r(x.changed_15m)}</td><td className="num">{pct(x.kept_at_15m_close?.q50, 0)}</td>
          <td className="num">{r(x.reversed_by_15m_close)}</td><td className="num">{r(x.next_15m_continues)}</td>
          <td className="num">{pct(x.returned_to_start.p, 0)} · {q(x.minutes_to_return, "q50", 0)} min</td></tr>))}</tbody></table></TableWrap>
  );
}
