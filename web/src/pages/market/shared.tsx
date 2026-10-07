/* Market simulator (ADR-106 ... ADR-110): shared words, formatters and small pieces used by every tab. */
import { useEffect, useState } from "react";
import { ApiError, viewCache } from "../../api/client";
import { market } from "../../api/market";
import type { CallScore, Mistakes, Quant, Rate, Score, Section, TargetEval, TurnScore } from "../../api/market";
import type { MyJob } from "../../api/my";
import { useApi } from "../../app/context";
import { Badge, Empty, ErrorPanel, TableWrap, pct } from "../../components/ui";
import { PageHead } from "../MyStrategy";

export const TF_NAMES: Record<number, string> = { 1: "1m", 2: "2m", 3: "3m", 4: "4m", 5: "5m", 10: "10m", 15: "15m", 30: "30m", 60: "1h", 240: "4h", 1440: "1D" };

export const MODEL_WORDS: Record<string, string> = { baseline: "Baseline (the usual)", logistic: "Logistic", boosting: "Gradient boosting", similar: "Similar situations" };

export const OUTCOME_WORDS: Record<string, string> = { edge: "moved 1 ATR its way before 1 ATR against", next15: "next 15-min candle went its way",
  rest15: "rest of the 15-min candle went its way" };

export const TARGET_WORDS: Record<string, string> = { up: "Next 15-min candle up or down", size: "Size of the next 15-min candle",
  bias: "Session closes above the current price", levels: "Level reached before the session ends",
  reach2h: "Level traded within 2 hours", reach: "Level traded before the session ends",
  react: "Reacts at the level (1 ATR away before 1 ATR through)", first: "Nearest level above traded before the nearest below",
  land2h: "Where price is 2 hours later", land: "Where price is at the session end" };

export const LM_TARGETS = ["reach2h", "reach", "react", "first", "land2h", "land"] as const;

export const BASE_WORDS: Record<string, string> = { reach2h: "random walk: distance, time left, current volatility", reach: "random walk: distance, time left, current volatility",
  react: "the usual reaction rate", first: "random walk: the nearer side first (gambler's ruin)", land2h: "no change", land: "no change" };

export const nyTime = (t: number) => new Date(t / 1e6).toLocaleTimeString("en-GB", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit" });

export const r = (p: Rate | null | undefined, d = 0) => (p && p.p != null ? `${pct(p.p, d)} (${p.k}/${p.n})` : "–");

export const q = (x: Quant | null | undefined, k: "q25" | "q50" | "q75" | "q10" | "q90" = "q50", d = 1) => (x && x[k] != null ? (x[k] as number).toFixed(d) : "–");

export const num = (v: number | null | undefined, d = 2) => (v == null || !Number.isFinite(v) ? "–" : v.toFixed(d));

export const dirWord = (d: number) => (d > 0 ? "bullish" : "bearish");

export const kindName = (kinds: Record<string, string> | undefined, k: string) => kinds?.[k] ?? k;

export const outside = (real: Rate | undefined, chance: number | null | undefined) =>
  !!(real?.ci && chance != null && (chance < real.ci[0] || chance > real.ci[1]));

export function useMarketJob(onDone: () => void): [MyJob | null, (j: MyJob) => void] {
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

export function useSection<T>(name: string) {
  return useApi<Section<T>>(market.sectionUrl(name), [name]);
}

export function NeedsAnalysis({ title, error }: { title: string; error: ApiError | null }) {
  return (
    <div className="page"><PageHead title={title} />
      {error && error.status === 404 ? <Empty>Run the analysis first (Market simulator → Start here).</Empty> : <ErrorPanel error={error} />}
    </div>
  );
}

export const skill = (s: Score | null | undefined) => (s && s.skill != null ? `${(s.skill * 100).toFixed(1)} %${s.skill_ci ? ` (${(s.skill_ci[0] * 100).toFixed(1)} … ${(s.skill_ci[1] * 100).toFixed(1)})` : ""}` : "–");

export function RealBadge({ s }: { s: Score | null | undefined }) {
  if (!s || s.skill == null) return <span className="muted">–</span>;
  return s.real ? <Badge tone="ok">beats the baseline</Badge> : <Badge>no real skill</Badge>;
}

export function ScoreTable({ scores, title }: { scores: Record<string, Score | null>; title: string }) {
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

export function MistakesView({ m, holdout }: { m: Mistakes; holdout?: boolean }) {
  if (!m.groups) return <p className="small muted">Too few predictions ({m.n}).</p>;
  const real = m.target === "size" || (m.target ?? "").startsWith("land");
  return (
    <div data-testid="market-mistakes">
      <p className="small">{m.n.toLocaleString()} predictions · skill {m.skill != null ? `${(m.skill * 100).toFixed(1)} %` : "–"}
        {holdout && m.discovery_skill != null ? ` (discovery, later months: ${(m.discovery_skill * 100).toFixed(1)} %)` : ""}.
        {" "}Descriptive: buckets are not corrected for how many there are, and nothing here is fed back into the models.</p>
      {m.findings && m.findings.length > 0 ? <ul className="small edge-list">{m.findings.slice(0, 10).map((f, i) => <li key={i}>{f.text}</li>)}</ul> :
        <p className="small muted">No bucket of 50+ predictions was clearly worse than the baseline or badly calibrated.</p>}
      <details className="tech"><summary>Every bucket</summary>
        {m.groups.map((g) => (
          <div key={g.group}><h4 className="small-head">{g.group}</h4>
            <TableWrap><table className="dense"><thead><tr><th>Bucket</th><th className="num">Predictions</th><th className="num">Skill</th>
              {holdout && <th className="num">Skill on discovery</th>}<th className="num">Hit rate</th><th className="num">Said</th><th className="num">Happened</th></tr></thead>
              <tbody>{g.rows.map((x) => <tr key={x.bucket}><td>{x.bucket}</td><td className="num">{x.n.toLocaleString()}</td>
                <td className={`num ${x.skill != null && x.skill < 0 ? "neg" : ""}`}>{x.skill != null ? `${(x.skill * 100).toFixed(1)} %` : "–"}</td>
                {holdout && <td className="num">{x.discovery_skill != null ? `${(x.discovery_skill * 100).toFixed(1)} %` : "–"}</td>}
                <td className="num">{x.accuracy != null ? pct(x.accuracy, 0) : "–"}</td>
                <td className="num">{x.said != null ? pct(x.said, 0) : x.error != null ? num(x.error, 2) : "–"}</td>
                <td className="num">{x.happened != null ? pct(x.happened, 0) : x.error_baseline != null ? `baseline ${num(x.error_baseline, 2)}` : "–"}</td></tr>)}</tbody></table></TableWrap>
          </div>))}
      </details>
      {m.worst && m.worst.length > 0 && <details className="tech"><summary>The most confident misses</summary>
        <TableWrap><table className="dense"><thead><tr><th>When</th>{m.worst[0].level !== undefined && <th>Level</th>}<th className="num">Said</th>
          <th className="num">Happened</th><th>Situation</th></tr></thead>
          <tbody>{m.worst.map((w, i) => <tr key={i}><td>{new Date(w.t / 1e6).toLocaleString("en-GB", { timeZone: "America/New_York", dateStyle: "short", timeStyle: "short" })}</td>
            {w.level !== undefined && <td>{w.level}</td>}
            <td className="num">{real ? num(w.said, 2) : pct(w.said, 0)}</td>
            <td className="num">{real ? num(w.happened, 2) : w.happened > 0.5 ? "yes" : "no"}</td>
            <td className="small">{Object.values(w.context).join(" · ")}</td></tr>)}</tbody></table></TableWrap>
      </details>}
    </div>
  );
}

export function TargetScores({ t, e }: { t: string; e: TargetEval }) {
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

export function TurnRow({ t, label }: { t: TurnScore | undefined; label: string }) {
  if (!t || t.model == null) return <p className="small muted">{label}: too few cases.</p>;
  return (
    <p className="small">{label}: the map picked the right level (or "no turn") <b>{pct(t.model, 1)}</b> of the time; the random-walk baseline
      {" "}{pct(t.baseline, 1)}; "always the nearest level" {pct(t.nearest, 1)} ({t.n?.toLocaleString()} cases, no turn in {pct(t.none_share, 0)}).
      {" "}{t.real ? <Badge tone="ok">better than the baseline</Badge> : <Badge tone="neutral">not reliably better</Badge>}</p>
  );
}

export const STAGE_NAMES: Record<string, string> = { "0": "At the open", "5": "At minute 5", "10": "At minute 10" };

export const ci = (x: [number, number] | null | undefined) => (x ? ` (${pct(x[0], 1)} … ${pct(x[1], 1)})` : "");

export function CallCells({ c }: { c: CallScore | null | undefined }) {
  if (!c || !c.calls) return <><td className="num">no calls</td><td className="num">–</td><td className="num">–</td><td>–</td></>;
  return (
    <>
      <td className="num">{pct(c.share, 1)} <span className="muted">({c.calls.toLocaleString()})</span></td>
      <td className="num">{c.accuracy != null ? `${pct(c.accuracy, 1)}${ci(c.accuracy_ci)}` : "–"}</td>
      <td className="num">{c.baseline_accuracy != null ? pct(c.baseline_accuracy, 1) : "–"}</td>
      <td>{c.real ? <Badge tone="ok">better than the baseline</Badge> : <Badge tone="neutral">not reliably better</Badge>}</td>
    </>
  );
}

/** Inside a tab view: the analysis is missing (404) or something failed. */
export function NotReady({ error }: { error: ApiError | null }) {
  return error && error.status === 404 ? <Empty>Run the analysis first (Market simulator → Start here).</Empty> : <ErrorPanel error={error} />;
}
