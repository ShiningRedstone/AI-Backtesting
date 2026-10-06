/* Edge lab (ADR-104/105): does a signal know the direction BEFORE a strategy is built on it? The edge check measures a frozen
   set of ideas on NQ (9:30-11:00 New York and, since version 2, the last half hour) on the discovery period or on the days
   before it from another dataset, with strict statistics; the trade anatomy shows what the trades of a My strategy report
   really did, corrected for the number of tries. Nothing here is a run, a try or a holdout look. */
import { useEffect, useState } from "react";
import { ApiError, viewCache } from "../api/client";
import { edge } from "../api/edge";
import type { Anatomy, Block, EdgeResult, EdgeSource, EdgeStatus, HypResult, HypothesisDef, Selection, Verdict } from "../api/edge";
import type { MyJob } from "../api/my";
import { useApi } from "../app/context";
import { go, useRoute } from "../app/router";
import { Histogram } from "../components/charts";
import { Badge, Banner, Button, Card, Empty, ErrorPanel, Kpi, PageSkeleton, Select, TableWrap, TechDetails, pct, signCls } from "../components/ui";
import { PageHead, exitWord } from "./MyStrategy";

const VERDICT: Record<Verdict, { label: string; tone: "neutral" | "ok" | "warn" | "error" | "info" }> = {
  TOO_FEW: { label: "Too few days", tone: "neutral" },
  NO_EVIDENCE: { label: "No evidence", tone: "neutral" },
  NOT_TRADEABLE: { label: "Information, not tradeable", tone: "warn" },
  INCONSISTENT: { label: "Not consistent", tone: "warn" },
  CANDIDATE: { label: "Candidate", tone: "ok" },
};
const pts = (v: number | null | undefined, d = 1) => (v == null || !Number.isFinite(v) ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(d)} pts`);
const rng = (ci: [number, number] | null | undefined, d = 1) => (ci ? `${ci[0].toFixed(d)} … ${ci[1].toFixed(d)}` : "–");
const pv = (p: number | null | undefined) => (p == null ? "–" : p < 0.001 ? "< 0.001" : p.toFixed(3));
const dayFmt = (iso: string) => new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });

function useEdgeJob(onDone: () => void): [MyJob | null, (j: MyJob) => void] {
  const [job, setJob] = useState<MyJob | null>(null);
  useEffect(() => {
    if (!job || job.state !== "running") return;
    let live = true;
    const t = window.setTimeout(() => {
      edge.job(job.job_id).then((j) => {
        if (!live) return;
        setJob(j);
        if (j.state !== "running") { viewCache.clear(); onDone(); }
      }).catch(() => { if (live) setJob(null); });
    }, 1000);
    return () => { live = false; window.clearTimeout(t); };
  }, [job]); // eslint-disable-line react-hooks/exhaustive-deps
  return [job, setJob];
}

const Head = ({ title }: { title: string }) => <PageHead title={title} />;

// =============================================================================================== edge check
export function EdgeCheckPage() {
  const { data, error, reload } = useApi<EdgeStatus>(edge.statusUrl);
  const [job, setJob] = useEdgeJob(reload);
  const [err, setErr] = useState<ApiError | null>(null);
  const [src, setSrc] = useState<string>("discovery");
  useEffect(() => { if (data?.job && !job) { setJob(data.job); if (data.job.source) setSrc(data.job.source); } }, [data?.job]); // eslint-disable-line react-hooks/exhaustive-deps
  if (error) return <div className="page"><Head title="Edge check" /><ErrorPanel error={error} /></div>;
  if (!data) return <div className="page"><Head title="Edge check" /><PageSkeleton layout="overview" label="Loading the edge check" /></div>;
  const res: EdgeResult | null = data.results?.[src] ?? (src === "discovery" ? data.latest : null);
  const running = job?.state === "running";
  const start = async () => { setErr(null); try { setJob(await edge.run(src)); } catch (e) { setErr(e as ApiError); } };
  const old = res != null && res.set.version < data.set.version;
  const missing = old ? data.set.hypotheses.filter((h) => !res.results.some((x) => x.id === h.id)).map((h) => h.id) : [];
  return (
    <div className="page" data-testid="edge-check-page">
      <Head title="Edge check · NQ" />
      <Card title="What this answers" testId="edge-intro">
        <p>Does a simple, fixed idea know where NQ goes next? It is measured BEFORE any strategy is built on it: if an idea does not know the
          direction, no choice of stops and targets can make it profitable. H1 – H4 trade the first 90 minutes after the New York open (9:30 – 11:00);
          H5 and H6 are the two published end-of-day ideas (15:30 – 16:00).</p>
        <ul className="small edge-list">
          <li><b>Discovery period, or days before it.</b> Either your research protocol's discovery period, or the days BEFORE it from another
            dataset you imported (for example older USATECH data): a fresh sample the ideas have never seen. The holdout is never read. Nothing
            here is a backtest run, a try or a holdout look.</li>
          <li><b>{data.set.family} ideas, frozen before any result was seen</b>, each with the reasons it may NOT work. Changing them
            is a new version; every idea ever tested stays in the count.</li>
          <li><b>Shuffle test:</b> the idea's long / short days are shuffled 10,000 times over the same days. If the real result is not
            unusual among the shuffles, the idea does not know the direction. The p-value is then multiplied by {data.set.family}
            (one chance per idea) before anything counts.</li>
          <li><b>After costs:</b> buy on ASK, sell on BID, plus the configured commission and slippage of one MNQ contract.</li>
          <li><b>Consistency:</b> the same sign in at least 3 of 4 years with 20+ signals, and the same signals on ES (if imported).</li>
        </ul>
      </Card>
      <Card title="Run" testId="edge-run" actions={<Button kind="primary" onClick={start} busy={running} busyLabel="Measuring…" testId="edge-run-btn"
        disabled={!data.sources.some((x) => x.key === src && x.usable)}>
        {res && !old ? "Run again" : "Run the edge check"}</Button>}>
        <SourcePicker sources={data.sources} value={src} onChange={setSrc} disabled={running} />
        {running && <Banner tone="info"><span className="spinner" /> {job?.step}</Banner>}
        {job?.state === "failed" && <Banner tone="error">{job.error?.message}</Banner>}
        <ErrorPanel error={err} />
        {old && <Banner tone="warn" testId="edge-old-version">This result was measured with version {res.set.version} of the ideas
          ({res.results.map((x) => x.id).join(", ")}). Run again to add {missing.join(" and ")}; the earlier ideas give the same numbers, but the
          correction becomes × {data.set.family}.</Banner>}
        {res ? <RunFacts r={res} /> : <p className="muted">Not measured on this data yet. It takes about a minute on four years of data; the same data
          and settings always give the same numbers (the result is kept).</p>}
      </Card>
      {res && <SummaryTable r={res} />}
      {data.set.hypotheses.map((h) => <HypCard key={h.id} h={h} r={res?.results.find((x) => x.id === h.id)} family={res?.family ?? data.set.family} />)}
    </div>
  );
}

function SourcePicker({ sources, value, onChange, disabled }: { sources: EdgeSource[]; value: string; onChange: (v: string) => void; disabled: boolean }) {
  const usable = sources.filter((x) => x.usable);
  const other = sources.filter((x) => !x.usable && x.key !== "discovery");
  const cur = sources.find((x) => x.key === value);
  if (!sources.length) return <Banner tone="warn">The edge check needs the workspace's active research protocol.</Banner>;
  return (
    <div className="edge-source" data-testid="edge-source">
      <label className="small-head" htmlFor="edge-source-select">Data</label>
      {disabled ? <p className="small">{cur?.label}</p> :
        <Select value={value} onChange={onChange} ariaLabel="Data" testId="edge-source-select"
          options={usable.map((x) => ({ value: x.key, label: x.key === "discovery" ? `Discovery period · ${x.first_day} – ${x.last_day}`
            : `${x.name} · days before discovery · ${x.first_day} – ${x.last_day}` }))} />}
      <p className="small muted">{value === "discovery"
        ? "Your research protocol's discovery period: the same days your strategies were built on."
        : "Only this dataset's days BEFORE your discovery period are used: data the ideas and your strategies never saw. A real effect should show here too; if it only shows on the discovery period, it was probably chance."}</p>
      {usable.length < 2 && <p className="small muted">To test on older data, import it under Settings → Data: a Dukascopy USATECH.IDX/USD 1-minute
        file with BID and ASK, the same instrument as your research data (for example 2013 – 2020). It then appears here.</p>}
      {other.length > 0 && <details className="small"><summary>{other.length} other 1-minute dataset{other.length > 1 ? "s" : ""} cannot be used</summary>
        <ul>{other.map((x) => <li key={x.key}>{x.name}: {x.reason}</li>)}</ul></details>}
    </div>
  );
}

function RunFacts({ r }: { r: EdgeResult }) {
  return (
    <div className="kpis" data-testid="edge-facts">
      <Kpi label="Days measured" value={r.days.usable.toLocaleString()}
        sub={`${dayFmt(r.window.start)} – ${dayFmt(r.window.end)} · ${r.days.skipped_missing_minutes} skipped (missing minutes)`} />
      <Kpi label="Data" value={r.source?.independent ? "Earlier days" : "Discovery"}
        sub={r.source?.independent ? `${r.dataset.name ?? r.dataset.instrument}: days before the discovery period` : "the protocol's discovery period"} />
      <Kpi label="ES cross-check" value={r.es ? "on" : "off"} sub={r.es ? `${r.es.usable_days} ES days` : "import ES under My strategy → Settings"} />
      <Kpi label="Measured" value={dayFmt(r.computed_at)} sub={`${new Date(r.computed_at).toLocaleTimeString()} · set version ${r.set.version} · ${r.dataset.instrument}`} />
    </div>
  );
}

function SummaryTable({ r }: { r: EdgeResult }) {
  return (
    <Card title="Results" testId="edge-summary">
      <TableWrap><table className="dense">
        <thead><tr><th>Idea</th><th className="num">Signal days</th><th className="num">Before costs, per trade</th><th className="num">Chance (corrected)</th>
          <th className="num">After costs, per trade</th><th className="num">Years same way</th><th>ES</th><th>Verdict</th></tr></thead>
        <tbody>{r.results.map((x) => {
          const def = r.set.hypotheses.find((h) => h.id === x.id);
          return (
            <tr key={x.id} data-testid={`edge-row-${x.id}`}>
              <td><a href={`#edge-${x.id}`} onClick={(e: { preventDefault: () => void }) => { e.preventDefault(); document.getElementById(`edge-${x.id}`)?.scrollIntoView({ behavior: "smooth" }); }}>
                {x.id} · {def?.name}</a></td>
              <td className="num">{x.n}</td>
              <td className={`num ${signCls(x.gross_pts?.mean)}`}>{pts(x.gross_pts?.mean)}</td>
              <td className="num">{pv(x.p_bonf)}</td>
              <td className={`num ${signCls(x.net_pts?.mean)}`}>{x.net_pts ? pts(x.net_pts.mean) : "–"}{x.direction === "opposite" ? " (opposite trade)" : ""}</td>
              <td className="num">{x.years_counted ? `${x.years_same_sign} of ${x.years_counted}` : "–"}</td>
              <td>{!x.es ? "–" : x.es.too_few ? "too few" : x.es.same_sign ? `same way (p ${pv(x.es.p)})` : `opposite (p ${pv(x.es.p)})`}</td>
              <td><Badge tone={VERDICT[x.verdict].tone}>{VERDICT[x.verdict].label}</Badge></td>
            </tr>);
        })}</tbody></table></TableWrap>
      <p className="small muted">"Chance (corrected)" = how often shuffled directions do at least as well, times {r.family}. Below 0.05 means the idea
        probably knows something; above it means the result is what luck alone gives. A verdict is never "profitable": at best a candidate for one
        holdout or forward test.</p>
    </Card>
  );
}

function HypCard({ h, r, family }: { h: HypothesisDef; r?: HypResult; family: number }) {
  return (
    <Card title={<span id={`edge-${h.id}`}>{h.id} · {h.name}</span>} testId={`edge-card-${h.id}`}
      actions={r ? <Badge tone={VERDICT[r.verdict].tone}>{VERDICT[r.verdict].label}</Badge> : undefined}>
      <p><b>Idea.</b> {h.idea}</p>
      {h.added_in === 2 && <p className="small muted">Added in version 2, after H1 – H4 found nothing: a published idea, tested with the same rules.</p>}
      <p className="small"><b>Exact rule.</b> {h.rule}</p>
      <details className="edge-against" open={!r}><summary>Reasons it may NOT work (written before the test)</summary>
        <ul className="small">{h.against.map((a, i) => <li key={i}>{a}</li>)}</ul></details>
      {r && <HypResultView r={r} h={h} family={family} />}
    </Card>
  );
}

function HypResultView({ r, h, family }: { r: HypResult; h: HypothesisDef; family: number }) {
  if (r.n < 2 || !r.gross_pts) return <Banner tone="info">{r.verdict_text}</Banner>;
  const hist = r.null ? { ...r.null, clipped: 0 } : null;
  return (
    <>
      <Banner tone={r.verdict === "CANDIDATE" ? "ok" : r.verdict === "NO_EVIDENCE" || r.verdict === "TOO_FEW" ? "info" : "warn"}
        testId={`edge-verdict-${r.id}`}>{r.verdict_text}</Banner>
      <div className="kpis">
        <Kpi label="Signal days" value={r.n} sub={`${r.longs} long · ${r.shorts} short`} />
        <Kpi label="Before costs, per trade" value={pts(r.gross_pts.mean)} tone={signCls(r.gross_pts.mean) as "pos" | "neg" | undefined}
          sub={`95 %: ${rng(r.gross_pts_ci95)} pts · ${h.direction_meaning.split(";")[r.direction === "opposite" ? 1 : 0]?.trim() ?? ""}`} />
        <Kpi label="Chance (corrected)" value={pv(r.p_bonf)} sub={`raw ${pv(r.p)} × ${family} ideas`} accent />
        <Kpi label="After costs, per trade" value={r.net_pts ? pts(r.net_pts.mean) : "–"} tone={signCls(r.net_pts?.mean) as "pos" | "neg" | undefined}
          sub={r.net_pts ? `95 %: ${rng(r.net_pts_ci95)} · costs ${pts(r.cost_pts)} · wins ${pct(r.net_win_share, 0)}` : undefined} />
      </div>
      <div className="grid-cards">
        <div>
          <h3 className="small-head">The real result against 10,000 shuffled directions</h3>
          <Histogram hist={hist} unit="average move per day, in typical ranges" marker={{ value: r.observed_norm ?? 0, label: "real" }} height={180}
            testId={`edge-null-${r.id}`} />
          <p className="small muted">Each bar counts shuffles that reached that average. The real result (line) must sit far out in a tail to count.
            {r.detectable_pts != null ? ` With ${r.n} days the test would find an effect of about ${r.detectable_pts.toFixed(1)} points per trade or more (80 % of the time).` : ""}</p>
        </div>
        <div>
          <h3 className="small-head">By year (before costs)</h3>
          <TableWrap><table className="dense"><thead><tr><th>Year</th><th className="num">Days</th><th className="num">Per trade</th></tr></thead>
            <tbody>{(r.by_year ?? []).map((y) => (
              <tr key={y.year} className={y.n < 20 ? "muted" : ""}><td>{y.year}</td><td className="num">{y.n}</td>
                <td className={`num ${signCls(y.gross_pts)}`}>{pts(y.gross_pts)}</td></tr>))}</tbody></table></TableWrap>
          <p className="small muted">{r.years_same_sign} of {r.years_counted} years with 20+ days went the same way. Win share of the signal: {pct(r.win_share, 0)}
            {" "}(moves were up on {pct(r.win_share_all_up, 0)} of these days).</p>
          {r.es && !r.es.too_few && <p className="small">ES, same rule: {r.es.n} days, {r.es.same_sign ? "same" : "opposite"} direction,
            chance {pv(r.es.p)} (uncorrected).</p>}
        </div>
      </div>
    </>
  );
}

// =============================================================================================== trade anatomy
export function TradeAnatomyPage() {
  const route = useRoute();
  const { data, error } = useApi<EdgeStatus>(edge.statusUrl);
  const id = route.query.get("r");
  if (error) return <div className="page"><Head title="Trade anatomy" /><ErrorPanel error={error} /></div>;
  if (!data) return <div className="page"><Head title="Trade anatomy" /><PageSkeleton layout="overview" label="Loading" /></div>;
  const opts = data.reports.map((r) => ({ value: r.id, label: `${r.favorite ? "★ " : ""}${r.label || r.id} · ${r.trade_count} trades` }));
  return (
    <div className="page" data-testid="edge-anatomy-page">
      <Head title="Trade anatomy" />
      <Card title="Which report" testId="edge-anatomy-pick">
        <p className="small muted">What the trades of one of your My strategy reports really did: the result BEFORE costs (does the setup know the
          direction?), what costs take, and how far trades went for and against you before they ended (an exit problem or a signal problem?).</p>
        {!opts.length ? <Empty>No My strategy report yet.</Empty> :
          <Select value={id ?? ""} onChange={(v) => go(`/edge-anatomy?r=${v}`)} options={opts} placeholder="Pick a report…"
            ariaLabel="Report" testId="edge-anatomy-select" />}
      </Card>
      {id && <AnatomyView key={id} id={id} />}
    </div>
  );
}

function AnatomyView({ id }: { id: string }) {
  const { data, error } = useApi<Anatomy>(edge.anatomyUrl(id), [id]);
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <PageSkeleton layout="overview" label="Reading the trades" />;
  const a = data.all, ex = data.excursion;
  return (
    <>
      <Card title={data.report.label || data.report.id} testId="edge-anatomy">
        <Banner tone={data.verdict.code === "POSITIVE" ? "ok" : data.verdict.code === "TOO_FEW" ? "info" : "warn"} testId="edge-anatomy-verdict">
          {(data.verdict.lines ?? [data.verdict.text]).map((l, i) => <div key={i}>{l}</div>)}</Banner>
        <div className="kpis">
          <Kpi label="Trades" value={a.n} />
          <Kpi label="Before costs, per trade" value={rr(a.gross_r?.mean)} tone={signCls(a.gross_r?.mean) as "pos" | "neg" | undefined}
            sub={`95 %: ${rng(a.gross_ci95, 2)} R · wins ${pct(a.gross_win, 0)}`} accent />
          <Kpi label="Costs, per trade" value={rr(a.cost_r != null ? -a.cost_r : null)} sub="spread is in the fill prices" />
          <Kpi label="After costs, per trade" value={rr(a.net_r?.mean)} tone={signCls(a.net_r?.mean) as "pos" | "neg" | undefined}
            sub={`95 %: ${rng(a.net_ci95, 2)} R · wins ${pct(a.net_win, 0)}`} />
        </div>
      </Card>
      {data.selection && <SelectionCard s={data.selection} />}
      {ex && <Card title="How far trades went while they were open" testId="edge-anatomy-excursion">
        <TableWrap><table className="dense"><thead><tr><th>Went this far in your favour</th><th className="num">All trades</th><th className="num">Losing trades</th></tr></thead>
          <tbody>{Object.entries(ex.reached).map(([lv, v]) => (
            <tr key={lv}><td>{lv} R or more</td><td className="num">{pct(v.all, 0)}</td><td className="num">{v.losers == null ? "–" : pct(v.losers, 0)}</td></tr>))}
            <tr><td>Never 0.25 R in favour</td><td className="num">{pct(ex.never_moved, 0)}</td><td className="num">{ex.losers_never_moved == null ? "–" : pct(ex.losers_never_moved, 0)}</td></tr>
          </tbody></table></TableWrap>
        <div className="grid-cards">
          <div><h3 className="small-head">Best point reached (R)</h3><Histogram hist={{ ...ex.mfe_hist, clipped: 0 }} unit="R in favour" height={160} /></div>
          <div><h3 className="small-head">Worst point reached (R)</h3><Histogram hist={{ ...ex.mae_hist, clipped: 0 }} unit="R against" height={160} color="var(--c-neg)" /></div>
        </div>
        <p className="small muted">Bar resolution: "reached" means at some minute while the trade was open (the order inside a minute is unknown).
          {ex.winners_heat_half_r != null ? ` ${pct(ex.winners_heat_half_r, 0)} of the winners first went 0.5 R or more against you.` : ""}</p>
      </Card>}
      {data.by && <Card title="Split" testId="edge-anatomy-split">
        <div className="grid-cards">{(["direction", "model", "exit"] as const).map((k) => (
          <div key={k}><h3 className="small-head">{k === "direction" ? "Direction" : k === "model" ? "Model" : "Exit"}</h3>
            <BlockTable rows={k === "exit" ? data.by![k].map((b) => ({ ...b, group: exitWord(b.group) })) : data.by![k]} /></div>))}</div>
      </Card>}
      <TechDetails rows={[["Report", data.report.id]]} />
    </>
  );
}

function SelectionCard({ s }: { s: Selection }) {
  if (!s.applies) return <Card title="Correcting for your tries" testId="edge-anatomy-tries"><p className="small">{s.reason}</p></Card>;
  const g = s.gross_r, n = s.net_r;
  return (
    <Card title="Correcting for your tries" testId="edge-anatomy-tries">
      {s.tries > 1
        ? <p className="small">Your My strategy and autotuner tries on this discovery period: <b>{s.tries.toLocaleString()}</b>. You picked this backtest
          because it looked good among them, and among that many tries some look good by luck alone. So the chance must be multiplied by the number of
          tries.</p>
        : <p className="small">Only one try is counted on this discovery period, so there is nothing to correct for yet.</p>}
      <div className="kpis">
        <Kpi label="Tries on this period" value={s.tries.toLocaleString()} sub={s.by.map((b) => `${b.name} ${b.tries.toLocaleString()}`).join(" · ") || "none counted"} />
        <Kpi label="Before costs: luck alone does this well" value={g ? pv(g.p_corrected) : "–"} accent
          sub={g ? `${pv(g.p)} for one try (t = ${g.t.toFixed(1)})${s.tries > 1 ? ` × ${s.tries.toLocaleString()} tries` : ""}` : undefined} />
        <Kpi label="After costs: luck alone does this well" value={n ? pv(n.p_corrected) : "–"}
          sub={n ? `${pv(n.p)} for one try (t = ${n.t.toFixed(1)})${s.tries > 1 ? ` × ${s.tries.toLocaleString()}` : ""}` : undefined} />
        <Kpi label="Needed to count" value={`t ≥ ${s.t_needed.toFixed(1)}`} sub={`one try alone would need t ≥ ${s.t_needed_one.toFixed(1)}`} />
      </div>
      <p className="small muted">Below 0.05 after the correction would mean the result is unlikely to be luck even as the best of many. The correction
        (Bonferroni) is strict because many tries are similar to each other; the true chance lies between the one-try and the corrected number.
        A one-time holdout or forward test of the chosen settings needs no correction.</p>
    </Card>
  );
}

const rr = (v: number | null | undefined) => (v == null || !Number.isFinite(v) ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(3)} R`);

function BlockTable({ rows }: { rows: (Block & { group: string })[] }) {
  return (
    <TableWrap><table className="dense"><thead><tr><th></th><th className="num">Trades</th><th className="num">Before costs</th><th className="num">After costs</th></tr></thead>
      <tbody>{rows.map((b) => (
        <tr key={b.group}><td>{b.group}</td><td className="num">{b.n}</td>
          <td className={`num ${signCls(b.gross_r?.mean)}`}>{rr(b.gross_r?.mean)}</td>
          <td className={`num ${signCls(b.net_r?.mean)}`}>{rr(b.net_r?.mean)}</td></tr>))}</tbody></table></TableWrap>
  );
}
