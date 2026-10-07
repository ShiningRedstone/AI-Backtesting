/* Market simulator → Holdout tests (ADR-107 / 109, reorganised in ADR-110): the one recorded look of the forecaster and
   the second, labelled look of the direction calls, each with its mistakes report. */
import { useState } from "react";
import { ApiError } from "../../api/client";
import { market } from "../../api/market";
import type { DirectionStatus, HoldoutModelScore, HoldoutStatus, Mistakes, TurnScore } from "../../api/market";
import { useApi } from "../../app/context";
import { href } from "../../app/router";
import { Banner, Button, Card, Confirm, ErrorPanel, Select, TableWrap, TextInput, pct } from "../../components/ui";
import { PageHead } from "../MyStrategy";
import { SkillBars } from "./charts";
import { BASE_WORDS, CallCells, LM_TARGETS, MODEL_WORDS, MistakesView, RealBadge, STAGE_NAMES, TARGET_WORDS, TurnRow, skill, useMarketJob } from "./shared";

export function MarketHoldoutPage() {
  return (
    <div className="page mk" data-testid="market-holdout-page">
      <PageHead title="Holdout tests" />
      <p className="mk-lead">The holdout is the locked last part of your research data. Each test here is ONE recorded look: models are frozen on
        discovery first, the look is written to the protocol ledger, and only then is the holdout read. It cannot be repeated. Afterwards a
        mistakes report shows where the predictions failed; nothing is ever retrained from it.</p>
      <HoldoutTestCard />
      <DirectionHoldoutCard />
    </div>
  );
}

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
            <li>The level map is tested too (traded in 2 h / by the close, reacts, which side first, where price lands, the turning level),
              followed by a mistakes report: where and why the predictions failed (time of day, news, volatility, level kind, chart, distance,
              trend). Nothing is retrained from the holdout, so the look stays clean.</li>
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
          <SkillBars testId="market-holdout-bars" rows={[...HOLDOUT_TARGETS.filter(([k]) => res.targets[k]?.official).map(([k, w]) => ({ id: k, label: w,
            skill: res.targets[k].official?.skill, ci: res.targets[k].official?.skill_ci, real: !!res.targets[k].official?.real })),
            ...LM_TARGETS.filter((k) => res.levelmap?.[k]?.official).map((k) => ({ id: `lm_${k}`, label: `${TARGET_WORDS[k]} (level map)`,
              skill: res.levelmap![k].official?.skill, ci: res.levelmap![k].official?.skill_ci, real: !!res.levelmap![k].official?.real }))]} />
          {res.targets.size?.bands && <p className="small">Candle-size ranges on the holdout: {pct(res.targets.size.bands.inside_50, 0)} of candles inside the
            50 % range and {pct(res.targets.size.bands.inside_80, 0)} inside the 80 % range (honest ranges give 50 % and 80 %).</p>}
          {res.levelmap?.turn && <TurnRow t={res.levelmap.turn} label="Turning level on the holdout" />}
          <details className="tech"><summary>Exact numbers, every model</summary>
            <TableWrap><table className="dense" data-testid="market-holdout-result"><thead><tr><th>Forecast</th><th>Official model</th>
              <th className="num">Skill on the holdout</th><th className="num">Hit rate</th><th>Real?</th><th>Other models (not chosen in advance)</th></tr></thead>
              <tbody>{HOLDOUT_TARGETS.map(([k, w]) => { const tg = res.targets[k]; if (!tg) return null; const s = tg.official;
                return (
                  <tr key={k}><td>{w}</td><td>{MODEL_WORDS[tg.official_model]}</td><td className="num">{skill(s)}</td>
                    <td className="num">{s?.accuracy != null ? pct(s.accuracy, 1) : "–"}</td><td><RealBadge s={s} /></td>
                    <td className="small">{Object.entries(tg.models).filter(([m]) => m !== tg.official_model && m !== "baseline")
                      .map(([m, x]) => `${MODEL_WORDS[m]} ${x?.skill != null ? (x.skill * 100).toFixed(1) + " %" : "–"}`).join(" · ")}</td></tr>);
              })}</tbody></table></TableWrap>
            {res.levelmap && <HoldoutLevelMap lm={res.levelmap} />}
          </details>
          {(res.mistakes || res.levelmap?.mistakes) && <HoldoutMistakes res={res} />}
          <p className="small muted">Every holdout day can be replayed candle by candle with its level map: <a href={href("/market-day?src=holdout")}>Day replay → Holdout</a>.</p>
        </>)}
    </Card>
  );
}

function HoldoutLevelMap({ lm }: { lm: Record<string, HoldoutModelScore> & { turn?: TurnScore } }) {
  return (
    <>
      <h3 className="small-head">Level map</h3>
      <TableWrap><table className="dense" data-testid="market-holdout-levelmap"><thead><tr><th>Forecast</th><th>Official model</th><th className="num">Skill</th>
        <th className="num">Hit rate</th><th>Real?</th><th>Baseline</th></tr></thead>
        <tbody>{LM_TARGETS.map((k) => { const tg = lm[k]; if (!tg) return null; const s = tg.official;
          return (<tr key={k}><td>{TARGET_WORDS[k]}</td><td>{MODEL_WORDS[tg.official_model]}</td><td className="num">{skill(s)}</td>
            <td className="num">{s?.accuracy != null && !k.startsWith("land") ? pct(s.accuracy, 1) : "–"}</td><td><RealBadge s={s} /></td>
            <td className="small muted">{BASE_WORDS[k]}</td></tr>); })}</tbody></table></TableWrap>
    </>
  );
}

function HoldoutMistakes({ res }: { res: NonNullable<HoldoutStatus["result"]> }) {
  const all: Record<string, Mistakes> = { ...(res.levelmap?.mistakes ?? {}), ...(res.mistakes ?? {}) };
  const keys = Object.keys(all);
  const [k, setK] = useState(keys[0] ?? "");
  if (!keys.length) return null;
  return (
    <div data-testid="market-holdout-mistakes">
      <h3 className="small-head">Mistakes report: where and why the predictions failed</h3>
      <Select value={k} onChange={setK} ariaLabel="Forecast" options={keys.map((x) => ({ value: x, label: TARGET_WORDS[x] ?? x }))} />
      {all[k] && <MistakesView m={all[k]} holdout />}
    </div>
  );
}

function DirectionHoldoutCard() {
  const { data, error, reload } = useApi<DirectionStatus>(market.directionUrl);
  const [job, setJob] = useMarketJob(reload);
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [err, setErr] = useState<ApiError | null>(null);
  if (error) return <ErrorPanel error={error} />;
  if (!data) return null;
  if (!data.summary) return <Card title="Second holdout look: direction calls only"><p className="small muted">Run the direction analysis first
    (Start here → step 4).</p></Card>;
  const h = data.holdout;
  const running = job?.state === "running";
  const start = async () => { setErr(null); try { setJob(await market.runDirectionHoldout(typed)); setOpen(false); setTyped(""); } catch (e) { setErr(e as ApiError); } };
  const res = h.result;
  return (
    <Card title="Second holdout look: direction calls only" testId="market-direction-holdout">
      <Banner tone="warn">Not a clean first look: {h.first_look ? `the first holdout look (${h.first_look.created_at.slice(0, 10)}) was used and its results were seen` :
        "this forecaster was designed after earlier results"} before this forecaster was built. Its result is labelled as a second look in the
        protocol ledger and here; new days after your data stay the clean test.</Banner>
      {!h.available ? <Banner tone="warn">{h.problem}</Banner> : !h.used ? (
        <>
          <p className="small">Freezes the direction models and the call thresholds trained on discovery, records ONE look in its own protocol
            entry, then calls every holdout 15-minute candle at the open, minute 5 and minute 10 with live knowledge only.</p>
          {running && <Banner tone="info"><span className="spinner" /> {job?.step}</Banner>}
          {job?.state === "failed" && <Banner tone="error">{job.error?.message}</Banner>}
          <ErrorPanel error={err} />
          <Button kind="primary" onClick={() => setOpen(true)} disabled={running} testId="market-direction-holdout-btn">Run the second look</Button>
          <Confirm open={open} title="Use the second holdout look (direction calls)?" confirmLabel="Start" danger busy={running}
            onCancel={() => { setOpen(false); setTyped(""); }} onConfirm={() => void start()}>
            <p>This spends the one second look. It cannot be undone or repeated. Type <b>HOLDOUT</b> to confirm.</p>
            <TextInput value={typed} onChange={setTyped} ariaLabel="Type HOLDOUT" testId="market-direction-holdout-typed" />
          </Confirm>
        </>) : !res ? (
          <Banner tone={h.look?.status === "failed" ? "error" : "info"}>
            {h.look?.status === "failed" ? "The look was recorded but the test failed; the look stays spent." : <><span className="spinner" /> Running…</>}</Banner>
        ) : (
        <>
          <p className="small">Look {res.access_id} · {res.days} holdout days · {res.candles.toLocaleString()} candles · models and thresholds frozen on
            discovery (fingerprint {res.fingerprint}).</p>
          <TableWrap><table className="dense" data-testid="market-direction-holdout-result"><thead><tr><th>When</th><th className="num">Skill</th>
            <th className="num">Calls (share)</th><th className="num">Right when calling</th><th className="num">Baseline on the same candles</th><th>Real?</th>
            <th className="num">Discovery (later months): right when calling</th></tr></thead>
            <tbody>{["0", "5", "10"].map((s) => { const st = res.stages[s]; if (!st) return null; const d = res.discovery[s]?.calls;
              return (<tr key={s}><td>{STAGE_NAMES[s]}</td><td className="num">{skill(st.official)}</td><CallCells c={st.calls} />
                <td className="num">{d?.accuracy != null ? pct(d.accuracy, 1) : "no calls"}</td></tr>); })}</tbody></table></TableWrap>
          <h3 className="small-head">Mistakes report (descriptive; nothing is retrained)</h3>
          <MistakesView m={res.mistakes} holdout />
        </>)}
    </Card>
  );
}
