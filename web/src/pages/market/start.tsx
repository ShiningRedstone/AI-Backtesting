/* Market simulator → Start here (ADR-110): the five setup steps in order, each with its state, and the headline of what
   the forecasts can and cannot do. */
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { ApiError, viewCache } from "../../api/client";
import { market } from "../../api/market";
import type { MarketStatus, Report } from "../../api/market";
import type { MyJob } from "../../api/my";
import { useApi } from "../../app/context";
import { href } from "../../app/router";
import { Badge, Banner, Button, Card, ErrorPanel, Kpi, PageSkeleton, Select, pct } from "../../components/ui";
import { PageHead } from "../MyStrategy";
import { SkillBars } from "./charts";
import { useMarketJob } from "./shared";

function Step({ n, title, done, optional, children, actions, testId }: { n: number; title: string; done: boolean; optional?: boolean;
  children?: ReactNode; actions?: ReactNode; testId?: string }) {
  return (
    <Card testId={testId} title={<><span className={`mk-step-n${done ? " done" : ""}`}>{done ? "✓" : n}</span>{title}
      {done ? <Badge tone="ok">done</Badge> : optional ? <Badge>optional</Badge> : <Badge tone="warn">to do</Badge>}</>} actions={actions}>
      {children}
    </Card>
  );
}

export function MarketStartPage() {
  const { data, error, reload } = useApi<MarketStatus>(market.statusUrl);
  const rep = useApi<Report>(data?.analysis ? market.reportUrl : null, [data?.analysis?.key, data?.direction, data?.newdays.scores?.computed_at]);
  const [job, setJob] = useMarketJob(() => { reload(); rep.reload(); });
  const [err, setErr] = useState<ApiError | null>(null);
  const [key, setKey] = useState("");
  const [source, setSource] = useState("forex-factory");
  useEffect(() => { if (data?.job && !job) setJob(data.job); }, [data?.job]); // eslint-disable-line react-hooks/exhaustive-deps
  if (error) return <div className="page mk"><PageHead title="Market simulator" /><ErrorPanel error={error} /></div>;
  if (!data) return <div className="page mk"><PageHead title="Market simulator" /><PageSkeleton layout="overview" label="Loading" /></div>;
  const running = job?.state === "running";
  const what = (job as (MyJob & { kind?: string }) | null)?.kind;
  const go = async (fn: () => Promise<MyJob>) => { setErr(null); try { setJob(await fn()); } catch (e) { setErr(e as ApiError); } };
  const saveKey = async () => { setErr(null); try { await market.setKey(key || null); setKey(""); viewCache.clear(); reload(); } catch (e) { setErr(e as ApiError); } };
  const a = data.analysis, n = data.news, nd = data.newdays;
  const newOk = !!nd.scores && !nd.last_problem;
  const works = (rep.data?.targets ?? []).filter((t) => t.works);
  const noSkill = (rep.data?.targets ?? []).filter((t) => !t.works);
  return (
    <div className="page mk" data-testid="market-overview">
      <PageHead title="Market simulator" />
      <p className="mk-lead">Studies how NQ and ES behave in your <b>discovery period</b>, then predicts every 15-minute candle using only what was
        known before it, and checks those predictions on months, holdout days and new days the models never saw. Nothing here is a backtest,
        a try or a strategy. Work through the steps once; afterwards the other tabs show the results.</p>
      <ErrorPanel error={err} />
      {running && <Banner tone="info" testId="market-job"><span className="spinner" /> {job?.step}</Banner>}
      {job?.state === "failed" && <Banner tone="error">{job.error?.message}</Banner>}
      <div className="grid-cards">
        <Step n={1} title="Price data" done={data.protocol && data.es.imported} testId="market-data">
          {!data.protocol ? <Banner tone="warn">An active research protocol is needed (it sets the discovery dates and the data).</Banner> : <>
            <p className="small">NQ comes from your research data (discovery period only).</p>
            <p className="small">ES: {data.es.imported ? "imported (USA500, 1-minute)." :
              <b>not imported yet. Import it under My strategy → Settings to get the NQ vs ES analysis.</b>}</p>
            {a && <p className="small muted">Discovery {a.source.window.start.slice(0, 10)} – {a.source.window.end.slice(0, 10)} · {a.days} trading
              dates · holdout from {a.source.holdout_start.slice(0, 10)}.</p>}</>}
        </Step>
        <Step n={2} title="News" done={!!n.downloaded && !n.refused} optional testId="market-news">
          <p className="small">Economic calendar (red / orange / yellow), forecasts and actual numbers from the JBlanked API. Without it the
            analysis still runs, just without news.</p>
          <div className="row gap">
            <input className="input" type="password" style={{ width: 240 }} placeholder={n.key.set ? `saved (${n.key.hint})` : "API key"}
              value={key} onChange={(e: { target: HTMLInputElement }) => setKey(e.target.value)} aria-label="JBlanked API key" data-testid="market-key" />
            <Button small onClick={saveKey} disabled={!key} testId="market-key-save">Save key</Button>
          </div>
          <div className="row gap">
            <Select value={source} onChange={setSource} ariaLabel="News source" options={n.sources.map((s) => ({ value: s, label: s }))} />
            <Button small onClick={() => go(() => market.downloadNews(source))} disabled={!n.key.set || running} testId="market-news-btn">
              {n.downloaded ? "Download again" : "Download news"}</Button>
          </div>
          {n.downloaded && (n.refused ? <Banner tone="warn">{n.refused.message} Nothing from this download is used.</Banner> :
            <p className="small muted" data-testid="market-news-status">{n.events?.toLocaleString()} USD events ({n.high?.toLocaleString()} high impact),
              {" "}{n.first?.slice(0, 10)} – {n.last?.slice(0, 10)}. Time zone proven: {n.timezone?.zone} ({pct(n.timezone?.match, 0)} of
              {" "}{n.timezone?.anchors} fixed-time releases).</p>)}
        </Step>
      </div>
      <Step n={3} title="Analysis" done={!!a} testId="market-analysis"
        actions={<Button kind={a ? "secondary" : "primary"} onClick={() => go(() => market.analyze(!!a))} busy={running && what === "market_analysis"}
          busyLabel="Analysing…" disabled={running || !data.protocol} testId="market-run">{a ? "Run again" : "Run the analysis"}</Button>}>
        {!a ? <p className="small">Reads the discovery period, measures every pattern, trend, session, shock and news reaction, and scores the
          candle forecasts month by month. Takes a few minutes per year of data; the result is kept until the data, news or settings change.</p> :
          <div className="kpis">
            <Kpi label="Trading dates" value={a.days} sub={`computed ${new Date(a.computed_at).toLocaleString()}`} />
            <Kpi label="Patterns measured" value={a.patterns} sub={`${a.edges.cells_tested.toLocaleString()} combinations in the edge scan`} />
            <Kpi label="Edge candidates" value={a.edges.confirmed} sub={`found on the first 70 %, held on the last 30 %`} />
            <Kpi label="News events used" value={a.news.used ? a.news.events.toLocaleString() : "none"} sub={a.news.used ? `${a.news.high} high impact` : "optional"} />
          </div>}
      </Step>
      <div className="grid-cards">
        <Step n={4} title="Direction analysis" done={data.direction} testId="market-direction-step"
          actions={a && <Button onClick={() => go(() => market.runDirection(data.direction))} busy={running && what === "market_direction"}
            busyLabel="Working…" disabled={running} testId="market-direction-run">{data.direction ? "Run again" : "Run"}</Button>}>
          <p className="small">Direction calls for the rest of each 15-minute candle, made at the open, minute 5 and minute 10, and only when the
            model is confident. Needs step 3.</p>
        </Step>
        <Step n={5} title="New days (live test)" done={newOk} optional testId="market-newdays"
          actions={a && <Button onClick={() => go(market.newdays)} busy={running && what === "market_newdays"} busyLabel="Working…" disabled={running}
            testId="market-newdays-btn">Download and predict</Button>}>
          <p className="small">Downloads the NQ and ES days after your research data and predicts them with models frozen on discovery: the clean,
            forward test. Results: <a href={href("/market-live")}>Live (new days)</a>.</p>
          <p className="small muted">Stored: {nd.nq.days} NQ days{nd.nq.last ? ` (up to ${nd.nq.last})` : ""}.
            {nd.scores ? ` Predicted: ${nd.scores.days.length} days.` : ""}</p>
          {nd.last_problem && <Banner tone="warn">{nd.last_problem.problem}</Banner>}
        </Step>
      </div>
      {rep.data && <Card title="What the forecasts can do" testId="market-headline"
        actions={<a className="btn btn-ghost btn-sm" href={href("/market-predictions")}>Open predictions</a>}>
        {works.length ? <>
          <p className="small">Beat their baseline on discovery months the models never saw (95 % range above 0):</p>
          <SkillBars rows={works.map((t) => ({ id: t.id, label: t.name, skill: t.discovery?.skill, ci: t.discovery?.skill_ci, real: true }))} />
        </> : <p className="small">No forecast beats its baseline yet.</p>}
        {noSkill.length > 0 && <p className="small muted">No skill (about as good as the baseline): {noSkill.map((t) => t.name).join(" · ")}.</p>}
      </Card>}
    </div>
  );
}
