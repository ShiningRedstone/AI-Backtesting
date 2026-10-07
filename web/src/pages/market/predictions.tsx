/* Market simulator → Predictions (ADR-110): the report card of every forecast - what beats its baseline first, then what
   does not - with skill bars and ranges, month-by-month skill (discovery, holdout, new days) and calibration. */
import { useState } from "react";
import { market } from "../../api/market";
import type { DirectionStatus, LevelMapSummary, Report, ReportScore, ReportTarget } from "../../api/market";
import { useApi } from "../../app/context";
import { href } from "../../app/router";
import { Badge, Banner, Card, Kpi, PageSkeleton, Select, TableWrap, pct } from "../../components/ui";
import { PageHead } from "../MyStrategy";
import { CalibrationChart, MonthlyChart, PctBars, SkillBars } from "./charts";
import { CallCells, MODEL_WORDS, MistakesView, NeedsAnalysis, STAGE_NAMES, useSection } from "./shared";

const sk = (s: { skill: number | null } | null | undefined) => (s && s.skill != null ? `${s.skill > 0 ? "+" : ""}${(s.skill * 100).toFixed(1)} %` : "–");
const SRC = { discovery: "Discovery (months never seen)", holdout: "Holdout", new: "New days" } as const;

function Verdict({ s }: { s: ReportScore | null | undefined }) {
  if (!s || s.skill == null) return <span className="faint">–</span>;
  return s.real ? <Badge tone="ok">beats it</Badge> : <Badge>no real skill</Badge>;
}

export function MarketPredictionsPage() {
  const { data, error } = useApi<Report>(market.reportUrl);
  const [pick, setPick] = useState<string>("");
  if (error) return <NeedsAnalysis title="Predictions" error={error} />;
  if (!data) return <div className="page mk"><PageHead title="Predictions" /><PageSkeleton layout="overview" label="Loading" /></div>;
  const works = data.targets.filter((t) => t.works), none = data.targets.filter((t) => !t.works);
  const cur = data.targets.find((t) => t.id === pick) ?? works[0] ?? data.targets[0];
  return (
    <div className="page mk" data-testid="market-predictions">
      <PageHead title="Predictions" />
      <p className="mk-lead">Every forecast against its own baseline (the simple rule it has to beat). Skill = how much smaller its error is than the
        baseline's: +10 % = 10 % better, 0 = no better, below 0 = worse. It counts as real only when the 95 % range (from resampling whole days)
        stays above 0. All numbers come from months, holdout days and new days the models never saw.</p>
      {!data.direction_ready && <Banner tone="info">The direction calls are not computed yet: Start here → step 4.</Banner>}
      <Card title={`Works (${works.length})`} testId="market-works">
        {works.length ? <SkillBars testId="market-works-bars" rows={works.map((t) => ({ id: t.id, label: t.name, skill: t.discovery?.skill,
          ci: t.discovery?.skill_ci, real: true }))} /> : <p className="small">No forecast beats its baseline beyond chance.</p>}
      </Card>
      <Card title={`No skill (${none.length})`} testId="market-noskill">
        <p className="small muted">About as good as their baseline, or worse. Kept here so you can see what was tried.</p>
        <SkillBars rows={none.map((t) => ({ id: t.id, label: t.name, skill: t.discovery?.skill, ci: t.discovery?.skill_ci, real: false }))} />
      </Card>
      <Card title="Every test at a glance" testId="market-score-table">
        <TableWrap><table className="dense"><thead><tr><th>Forecast</th><th className="num">Discovery</th><th className="num">Holdout</th>
          <th className="num">New days</th><th>Verdict (discovery)</th></tr></thead>
          <tbody>{data.targets.map((t) => (
            <tr key={t.id} className={t.id === cur?.id ? "row-hl" : ""} onClick={() => setPick(t.id)} style={{ cursor: "pointer" }}>
              <td>{t.name}</td><td className="num">{sk(t.discovery)}</td><td className="num">{sk(t.sources.holdout)}</td>
              <td className="num">{sk(t.sources.new)}</td><td><Verdict s={t.discovery} /></td></tr>))}</tbody></table></TableWrap>
        <p className="small muted">Click a row to look closer. Holdout: {data.holdout.first_used ? "tested" : "not tested yet"}{data.holdout.second_used ?
          " (direction calls in a second, labelled look)" : ""}. New days: {data.new_days.days ? `${data.new_days.days} days` : "none predicted yet"}.</p>
      </Card>
      {cur && <TargetDetail t={cur} all={data.targets} onPick={setPick} />}
      <LevelKinds />
    </div>
  );
}

function TargetDetail({ t, all, onPick }: { t: ReportTarget; all: ReportTarget[]; onPick: (id: string) => void }) {
  const lm = useSection<LevelMapSummary>("levelmap");
  const dir = useApi<DirectionStatus>(t.id.startsWith("dir") ? market.directionUrl : null, [t.id]);
  const mistakes = t.id.startsWith("dir") ? dir.data?.summary?.mistakes : lm.data?.data?.mistakes?.[t.id];
  const monthly = (["discovery", "holdout", "new"] as const).filter((k) => t.monthly[k]?.length)
    .map((k) => ({ id: k, label: SRC[k], points: t.monthly[k] ?? [] }));
  const cal = [{ id: "discovery", label: SRC.discovery, points: t.calibration ?? [] },
    ...(["holdout", "new"] as const).filter((k) => t.calibration_by?.[k]?.length).map((k) => ({ id: k, label: SRC[k], points: t.calibration_by?.[k] ?? [] }))]
    .filter((s) => s.points.length);
  return (
    <Card title="Look closer" testId="market-detail" actions={<Select value={t.id} onChange={onPick} ariaLabel="Forecast"
      options={all.map((x) => ({ value: x.id, label: x.name }))} testId="market-detail-pick" />}>
      <p className="small"><b>{t.name}.</b> Baseline: {t.baseline}. Official model: {MODEL_WORDS[t.model] ?? t.model}, chosen on the earlier discovery
        months ({t.chosen_on ?? "–"}) and scored on {t.reported_on ?? "the later ones"}.</p>
      <div className="kpis">
        {(["discovery", "holdout", "new"] as const).map((k) => {
          const s = k === "discovery" ? t.discovery : t.sources[k];
          return <Kpi key={k} label={SRC[k]} value={sk(s)} accent={!!s?.real}
            sub={s ? `${s.skill_ci ? `range ${(s.skill_ci[0] * 100).toFixed(1)} … ${(s.skill_ci[1] * 100).toFixed(1)} %` : ""}${s.accuracy != null ? ` · right ${pct(s.accuracy, 1)}` : ""}` : "not tested"} />;
        })}
      </div>
      <div className="grid-cards">
        <div className="mk-panel"><h3 className="small-head">Skill month by month</h3>
          <MonthlyChart sources={monthly} testId="market-monthly" />
          <p className="small muted">Above 0 = better than the baseline that month. A forecast that works stays above 0 in most months, also on the holdout
            and new days.</p></div>
        <div className="mk-panel"><h3 className="small-head">{t.kind === "binary" ? "Said vs happened" : "Ranges"}</h3>
          {t.kind === "binary" ? <><CalibrationChart series={cal} testId="market-calibration" />
            <p className="small muted">Each dot: predictions with about the same probability. On the dashed line = honest (said 60 %, happened 60 %);
              all dots near 50 % = it rarely dares to lean either way.</p></> :
            t.bands ? <p className="small">{pct(t.bands.inside_50, 0)} of outcomes fell inside the 50 % range and {pct(t.bands.inside_80, 0)} inside
              the 80 % range ({t.bands.n.toLocaleString()} predictions; honest ranges give 50 % and 80 %).</p> : <p className="small muted">No ranges.</p>}
        </div>
      </div>
      {t.calls && <DirectionCalls t={t} />}
      {mistakes && <><h3 className="small-head">Where it goes wrong (discovery, months the model choice never saw)</h3><MistakesView m={mistakes} /></>}
      {t.id.startsWith("dir") && dir.data?.summary && <>
        <h3 className="small-head">What the direction model leans on most</h3>
        <p className="small">{dir.data.summary.top_inputs.slice(0, 10).map((x) => `${x.words} ${pct(x.share, 0)}`).join(" · ")}</p></>}
    </Card>
  );
}

function DirectionCalls({ t }: { t: ReportTarget }) {
  const c = t.calls!;
  return (
    <>
      <h3 className="small-head">Confident calls ({STAGE_NAMES[t.id.slice(3)] ?? ""})</h3>
      <TableWrap><table className="dense"><thead><tr><th>Test</th><th className="num">Calls (share of candles)</th><th className="num">Right when calling</th>
        <th className="num">Baseline on the same candles</th><th>Real?</th></tr></thead>
        <tbody>{(["discovery", "holdout", "new"] as const).map((k) => (
          <tr key={k}><td>{SRC[k]}</td><CallCells c={c[k]} /></tr>))}</tbody></table></TableWrap>
      <p className="small muted">{c.rule?.tau != null ? `A call = the model says at least ${pct(0.5 + c.rule.tau, 0)} one way.` :
        `No calls: ${c.rule?.why ?? "no threshold was clearly better than a coin flip on the earlier months"}.`}</p>
    </>
  );
}

function LevelKinds() {
  const lm = useSection<LevelMapSummary>("levelmap");
  const rows = (lm.data?.data?.by_kind ?? []).filter((x) => x.n >= 200);
  if (!rows.length) return null;
  const lab = (x: (typeof rows)[number]) => `${x.kind_name}${x.chart ? ` ${x.chart}` : ""}`;
  return (
    <Card title="Which levels get traded, and does price react there?" testId="market-level-kinds">
      <PctBars rows={rows.map((x) => ({ id: `${x.kind}${x.chart}`, label: lab(x), values: [x.reach2h, x.react],
        note: `${x.n.toLocaleString()} levels, median ${x.median_dist.toFixed(1)} ATR away` }))}
        series={[{ label: "traded within 2 hours", color: "var(--c1)" }, { label: "reacted when touched", color: "var(--c2)" }]} testId="market-level-kinds-bars" />
      <p className="small muted">Discovery, all levels the map listed at 9:30 … 15:30. About 50 % is chance for a reaction. Nearer levels are traded
        more often, so compare kinds at similar distances (shown next to each name). Day replay shows the map on any day: <a href={href("/market-day")}>open it</a>.</p>
    </Card>
  );
}
