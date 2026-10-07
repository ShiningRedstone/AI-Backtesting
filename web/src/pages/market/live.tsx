/* Market simulator → Live (new days) (ADR-110): the forward test - days after the research data, predicted with models
   frozen on discovery. Status and problems, scores per forecast, a chart over the days and a list that opens Day replay. */
import { useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import { market } from "../../api/market";
import type { MarketStatus, NewDayRow, Report } from "../../api/market";
import type { MyJob } from "../../api/my";
import { useApi } from "../../app/context";
import { href } from "../../app/router";
import { LineChart } from "../../components/charts";
import { Banner, Button, Card, Empty, ErrorPanel, Kpi, PageSkeleton, TableWrap, pct } from "../../components/ui";
import { PageHead } from "../MyStrategy";
import { SkillBars } from "./charts";
import { useMarketJob } from "./shared";

const better = (e: number, b: number) => { const v = ((b - e) / b) * 100; return `${v > 0.05 ? "+" : ""}${Math.abs(v) < 0.05 ? "0.0" : v.toFixed(1)} %`; };
const avg = (rows: NewDayRow[], k: keyof NewDayRow, w: keyof NewDayRow) => {
  let s = 0, n = 0;
  for (const r of rows) { const v = r[k] as number | undefined, c = (r[w] as number | undefined) ?? 0; if (v != null && c) { s += v * c; n += c; } }
  return n ? s / n : null;
};

export function MarketLivePage() {
  const { data, error, reload } = useApi<MarketStatus>(market.statusUrl);
  const rep = useApi<Report>(data?.analysis ? market.reportUrl : null, [data?.newdays.scores?.computed_at]);
  const [job, setJob] = useMarketJob(() => { reload(); rep.reload(); });
  const [err, setErr] = useState<ApiError | null>(null);
  useEffect(() => { if (data?.job && !job) setJob(data.job); }, [data?.job]); // eslint-disable-line react-hooks/exhaustive-deps
  if (error) return <div className="page mk"><PageHead title="Live (new days)" /><ErrorPanel error={error} /></div>;
  if (!data) return <div className="page mk"><PageHead title="Live (new days)" /><PageSkeleton layout="overview" label="Loading" /></div>;
  const nd = data.newdays, sc = nd.scores;
  const rows = sc?.by_day ?? [];
  const running = job?.state === "running";
  const start = async () => { setErr(null); try { setJob((await market.newdays()) as MyJob); } catch (e) { setErr(e as ApiError); } };
  const upR = avg(rows, "up_right", "candles"), upB = avg(rows, "up_base_right", "candles");
  const lvR = avg(rows, "level_right", "levels"), lvB = avg(rows, "level_base_right", "levels");
  const calls = rows.reduce((s, r) => s + (r.calls ?? 0), 0);
  const callsR = avg(rows, "calls_right", "calls");
  const szE = avg(rows, "size_error", "candles"), szB = avg(rows, "size_base_error", "candles");
  const tested = (rep.data?.targets ?? []).filter((t) => t.sources.new);
  return (
    <div className="page mk" data-testid="market-live">
      <PageHead title="Live (new days)" />
      <p className="mk-lead">The forward test: trading days after your research data, downloaded from Dukascopy and predicted with the models frozen on
        discovery, exactly as they would have been live. These days were never used for anything else, so this is the cleanest check there is.</p>
      <Card title="Status" testId="market-live-status" actions={data.analysis && <Button kind="primary" onClick={start} busy={running}
        busyLabel="Working…" disabled={running} testId="market-newdays-btn">Download and predict</Button>}>
        {!data.analysis && <Banner tone="warn">Run the analysis first (Start here, step 3).</Banner>}
        {running && <Banner tone="info"><span className="spinner" /> {job?.step}</Banner>}
        {job?.state === "failed" && <Banner tone="error">{job.error?.message}</Banner>}
        <ErrorPanel error={err} />
        {nd.last_problem && <Banner tone="warn" testId="market-live-problem">Nothing predicted: {nd.last_problem.problem}</Banner>}
        {nd.last_update && Object.keys(nd.last_update.errors).length > 0 && <Banner tone="warn">Download problem (retried next time):
          {" "}{Object.entries(nd.last_update.errors).map(([k, v]) => `${k.toUpperCase()} ${v}`).join(" · ")}</Banner>}
        <div className="kpis">
          <Kpi label="Days downloaded" value={nd.nq.days} sub={nd.nq.first ? `${nd.nq.first} – ${nd.nq.last}` : `from ${nd.first_date ?? "–"}`} />
          <Kpi label="Days predicted" value={sc ? sc.days.length : 0} sub={sc?.history === "holdout" ? "every new day (the holdout is the history)" :
            sc ? `after ${sc.warmup_days ?? 20} warm-up days` : "press Download and predict"} />
          <Kpi label="Last checked" value={nd.last_update?.checked_at ? new Date(nd.last_update.checked_at).toLocaleDateString() : "never"}
            sub={nd.last_update?.checked_at ? new Date(nd.last_update.checked_at).toLocaleTimeString() : undefined} />
        </div>
      </Card>
      {!rows.length ? <Card><Empty>No new day predicted yet.</Empty></Card> : <>
        <Card title="How the forecasts did on the new days" testId="market-live-kpis">
          <div className="kpis">
            <Kpi label="Next candle up / down: right" value={pct(upR, 1)} sub={`always-the-usual baseline ${pct(upB, 1)}`} accent={upR != null && upB != null && upR > upB} />
            <Kpi label="Candle size" value={szE != null && szB ? `${better(szE, szB)} vs usual` : "–"}
              sub="+ = smaller error than 'the usual size'" accent={szE != null && szB != null && szE < szB} />
            <Kpi label="Level traded in 2 h: right" value={pct(lvR, 1)} sub={`random-walk baseline ${pct(lvB, 1)}`} accent={lvR != null && lvB != null && lvR > lvB} />
            <Kpi label="Direction calls" value={calls ? pct(callsR, 0) : "none"} sub={calls ? `right on ${calls} confident calls` : "no confident call made"} />
          </div>
          {tested.length > 0 && <><h3 className="small-head">Skill on new days (with its 95 % range)</h3>
            <SkillBars rows={tested.map((t) => ({ id: t.id, label: t.name, skill: t.sources.new?.skill, ci: t.sources.new?.skill_ci, real: !!t.sources.new?.real }))} />
            <p className="small muted">With few new days the ranges are wide: a forecast needs weeks of new days before its range can sit above 0.</p></>}
        </Card>
        <Card title="Day by day" testId="market-live-chart">
          <LineChart x={rows.map((r) => r.date)} unit="% right" fmtX={(s) => s.slice(5)}
            series={[{ id: "up", label: "next candle up / down", values: rows.map((r) => (r.up_right == null ? null : r.up_right * 100)) },
              { id: "b", label: "baseline", values: rows.map((r) => (r.up_base_right == null ? null : r.up_base_right * 100)), dashed: true },
              { id: "lv", label: "level traded in 2 h", values: rows.map((r) => (r.level_right == null ? null : r.level_right * 100)) }]} />
          <div className="mk-scroll"><TableWrap><table className="dense" data-testid="market-live-days"><thead><tr><th>Day</th><th className="num">Up / down right (baseline)</th>
            <th className="num">Size: better than usual</th><th className="num">Levels right (baseline)</th><th className="num">Direction calls</th><th /></tr></thead>
            <tbody>{[...rows].reverse().map((r) => (
              <tr key={r.date}><td>{r.date}</td>
                <td className="num">{pct(r.up_right, 0)} <span className="faint">({pct(r.up_base_right, 0)})</span></td>
                <td className="num">{r.size_error != null && r.size_base_error ? better(r.size_error, r.size_base_error) : "–"}</td>
                <td className="num">{pct(r.level_right, 0)} <span className="faint">({pct(r.level_base_right, 0)})</span></td>
                <td className="num">{r.calls ? `${pct(r.calls_right, 0)} of ${r.calls}` : "–"}</td>
                <td><a href={href(`/market-day?src=new&day=${r.date}`)}>Replay</a></td></tr>))}</tbody></table></TableWrap></div>
        </Card></>}
    </div>
  );
}
