/* ADR-114: how a Fair price trade is shown: the chart with the fair price, the structure the entry broke and the
   opening candle, and the plain-English reasons + checklist. Reads only what the rules recorded at the signal. */
import { useState } from "react";
import type { Candle } from "../api/my";
import { CandleChart, nyTime } from "../components/candles";
import type { Marker, PriceBox, PriceLine } from "../components/candles";
import { Card, Checkbox, Tabs } from "../components/ui";

const TF_MIN: Record<string, number> = { "1m": 1, "2m": 2, "3m": 3, "4m": 4, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1D": 1440 };
const sec = (iso: string | null | undefined) => (iso ? Math.floor(Date.parse(iso) / 1000) : NaN);
const px = (v: number | null | undefined) => (typeof v === "number" && Number.isFinite(v)
  ? v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "–");

export interface FairExplanation {
  phase: "eval" | "funded"; session: string; session_key: string; session_open: string; model: string; setup: string;
  direction: number; signal_ts: string; planned_entry: number; stop: number; target_price: number; stop_points: number;
  target_points: number; risk_budget_usd: number; target: { points: number; r_planned: number };
  fair: { price: number; source: string; ts: string; news?: { names: string[]; z: number | null }; range?: { high: number; low: number; from: string; to: string } };
  distance_points?: number; share_needed?: number; streak_before: number; trade_in_session: number;
  structure?: { kind: string; price: number; ts: string };
  previous_candle?: { o: number; h: number; l: number; c: number; ts: string };
  opening_candle?: { o: number; h: number; l: number; c: number; ts: string };
  bias?: { hours: number; price_then: number; price_at_open: number; direction: number };
  big_opening_candle?: boolean;
  checklist: Record<string, boolean | null>;
  flip?: { setup_direction: number; setup_stop: number; setup_target: number; setup_stop_points: number; setup_target_points: number;
    setup_r_planned: number | null; contracts_kept: number };
}

export const PHASE_WORD: Record<string, string> = { eval: "Evaluation", funded: "Funded" };
export const SETUP_WORD: Record<string, string> = { continuation: "Opening-candle continuation", displacement: "Displacement",
  bos: "Break of structure" };
const FAIR_SOURCE: Record<string, string> = {
  session_open: "the session's opening price", ny_am_open: "the 9:30 New York opening price",
  pre_news: "the price just before the 8:30 news", pre_open_consolidation: "the middle of the narrow range before the open",
  consolidation_after_losses: "the middle of the range after the losing streak (fair price moved)" };

export function fairExplainText(e: FairExplanation): string[] {
  const up = (e.flip?.setup_direction ?? e.direction) > 0;           // the SETUP's direction (flipped trades: the original)
  const side = up ? "long" : "short";
  const out: string[] = [];
  out.push(`${PHASE_WORD[e.phase] ?? e.phase} rules, ${e.session} session (opened ${nyTime(sec(e.session_open))} New York); trade ${e.trade_in_session} of the session` +
    (e.streak_before ? `, after ${e.streak_before} loss${e.streak_before > 1 ? "es" : ""} in a row.` : "."));
  out.push(`Fair price ${px(e.fair.price)}: ${FAIR_SOURCE[e.fair.source] ?? e.fair.source}` +
    (e.fair.news ? ` (${e.fair.news.names.slice(0, 3).join(", ")}${e.fair.news.z != null ? `, surprise ${e.fair.news.z.toFixed(1)} sd` : ""})` : "") + ".");
  if (e.setup === "continuation") {
    const oc = e.opening_candle;
    if (oc) out.push(`The opening candle was ${oc.c > oc.o ? "green" : "red"} (${px(oc.o)} → ${px(oc.c)}, range ${(oc.h - oc.l).toFixed(1)} points)` +
      (e.big_opening_candle ? ", bigger than the limit, so stop and target were doubled and the size halved." : "."));
    if (e.bias) out.push(`Higher-timeframe bias: price ${e.bias.price_at_open < e.bias.price_then ? "fell" : "rose"} over the ${e.bias.hours} hours before the open ` +
      `(${px(e.bias.price_then)} → ${px(e.bias.price_at_open)}), so the bias was ${e.bias.direction > 0 ? "long" : "short"}.`);
    if (e.structure) out.push(`A candle closed ${up ? "above" : "below"} the structure at ${px(e.structure.price)} (${e.structure.kind === "swing" ? "the last 1-minute swing" : e.structure.kind}): ${side} continuation.`);
  } else {
    out.push(`Price was ${e.distance_points?.toFixed(1)} points ${up ? "below" : "above"} the fair price: a ${side} reversion back towards it.`);
    if (e.setup === "bos" && e.structure) out.push(`Break of structure: the candle closed ${up ? "above" : "below"} the 1-minute swing at ${px(e.structure.price)} (${nyTime(sec(e.structure.ts))}).`);
    if (e.setup === "displacement" && e.previous_candle) out.push(`Displacement: the candle's body was bigger than the previous candle's and it closed ${up ? "above" : "below"} its wick (${px(up ? e.previous_candle.h : e.previous_candle.l)}).`);
  }
  if (e.flip) out.push(`Flipped: the setup was a ${side} (stop ${px(e.flip.setup_stop)}, target ${px(e.flip.setup_target)}); the trade is a ` +
    `${up ? "short" : "long"} with its stop at the old target and its target at the old stop, ${e.flip.contracts_kept} MNQ like the setup.`);
  out.push(`Take profit ${e.target_points} points, stop ${e.stop_points} points (planned ${e.target.r_planned.toFixed(2)} R)` +
    (e.phase === "funded" ? `, sized for the funded dollar win (risk budget $${e.risk_budget_usd.toLocaleString("en-US")}).` : `, risk $${e.risk_budget_usd.toLocaleString("en-US")}.`));
  return out;
}

export function FairExplain({ e }: { e: FairExplanation }) {
  if (!e) return null;
  return (
    <div className="grid-cards">
      <Card title="Why it entered" testId="my-explain">
        <ul className="my-explain">{fairExplainText(e).map((t, k) => <li key={k}>{t}</li>)}</ul>
      </Card>
      <Card title={`${SETUP_WORD[e.setup] ?? e.setup} · ${PHASE_WORD[e.phase] ?? e.phase}`} testId="my-checklist">
        <div className="my-checks">{Object.entries(e.checklist).map(([k, v]) => (
          <div key={k} className={`my-check ${v === true ? "yes" : v === false ? "no" : "na"}`}>
            <span className="my-check-mark">{v === true ? "✓" : v === false ? "✗" : "–"}</span>
            <span>{k.charAt(0).toUpperCase() + k.slice(1)}</span><span className="muted small">{v === true ? "True" : v === false ? "False" : "not used"}</span>
          </div>))}</div>
      </Card>
    </div>
  );
}

export function FairTradeCharts({ doc, until }: { doc: { explanation: FairExplanation; charts: string[]; candles: Record<string, Candle[]>;
  entry_ts?: string; exit_ts?: string; stop_price?: number; target_price?: number; entry_price_theo?: number }; until?: boolean }) {
  const e = doc.explanation;
  const tfs = doc.charts.filter((t) => doc.candles[t]?.length);
  const [picked, setTf] = useState<string>(tfs[0]);
  const [show, setShow] = useState({ fair: true, structure: true, open: true });
  if (!e || !tfs.length) return null;
  const tf = tfs.includes(picked) ? picked : tfs[0];
  const sig = sec(e.signal_ts) + 60;
  const exitT = doc.exit_ts ? sec(doc.exit_ts) : undefined;
  const entry = doc.entry_price_theo ?? e.planned_entry;
  const stop = doc.stop_price ?? e.stop, target = doc.target_price ?? e.target_price;
  const lines: PriceLine[] = [
    { price: entry, label: `Entry ${px(entry)}`, color: "var(--accent)", from: sig - 60, to: exitT },
    { price: stop, label: `Stop ${px(stop)}`, color: "var(--c-neg)", from: sig - 60, to: exitT },
    { price: target, label: `Target ${px(target)}`, color: "var(--ok)", from: sig - 60, to: exitT },
  ];
  if (show.fair) lines.push({ price: e.fair.price, label: `Fair price ${px(e.fair.price)}`, color: "var(--c2)", dash: "6 4", noRange: true });
  if (show.structure && e.structure) lines.push({ price: e.structure.price, label: "Structure", color: "var(--warn)", dash: "2 4",
    from: sec(e.structure.ts), to: sig });
  const boxes: PriceBox[] = [];
  if (show.fair && e.fair.range) boxes.push({ top: e.fair.range.high, bottom: e.fair.range.low, from: sec(e.fair.range.from), to: sec(e.fair.range.to) + 60,
    label: "Consolidation", color: "var(--c2)" });
  const markers: Marker[] = [];
  if (show.open) markers.push({ t: sec(e.session_open), price: e.opening_candle?.o ?? e.fair.price, label: `${e.session} open`, color: "var(--text-2)" });
  const tfm = TF_MIN[tf] ?? 1;
  const from = Math.min(sec(e.session_open), sec(e.fair.ts), sig) - tfm * 60 * 5;
  return (
    <Card title="Charts" testId="my-charts" actions={
      <div className="inline small">
        {([["fair", "Fair price"], ["structure", "Structure"], ["open", "Session open"]] as [keyof typeof show, string][])
          .map(([k, l]) => <Checkbox key={k} checked={show[k]} onChange={(v) => setShow({ ...show, [k]: v })} label={l} />)}
      </div>}>
      <Tabs tabs={tfs.map((t) => ({ id: t, label: t }))} active={tf} onChange={setTf} />
      <CandleChart key={tf} candles={doc.candles[tf]} tfMinutes={tfm} lines={lines} boxes={boxes} markers={markers}
        focus={{ from, to: until ? sig : (exitT ?? sig) }} cut={until ? sig - 60 : undefined} testId={`my-chart-${tf}`} />
      {until && <p className="muted small">The chart stops at the entry signal: you see only what was known at that moment.</p>}
    </Card>
  );
}
