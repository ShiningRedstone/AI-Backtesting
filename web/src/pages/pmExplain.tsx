/* ADR-115: how a Premarket breakout trade is shown: the chart with the premarket high / low and the breakout candle,
   and the plain-English reasons + checklist. Reads only what the rules recorded at the signal. */
import { useState } from "react";
import type { Candle } from "../api/my";
import { CandleChart, nyTime } from "../components/candles";
import type { Marker, PriceBox, PriceLine } from "../components/candles";
import { Card, Checkbox, Tabs } from "../components/ui";

const TF_MIN: Record<string, number> = { "1m": 1, "3m": 3, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1D": 1440 };
const sec = (iso: string | null | undefined) => (iso ? Math.floor(Date.parse(iso) / 1000) : NaN);
const px = (v: number | null | undefined) => (typeof v === "number" && Number.isFinite(v)
  ? v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "–");

export interface PmExplanation {
  model: "breakout"; direction: number; signal_ts: string; level: number; planned_entry: number; trade_of_day: number;
  premarket: { high: number; low: number; range: number; from: string; to: string; bars: number; high_ts: string; low_ts: string };
  candle: { tf: string; t0: string; o: number; h: number; l: number; c: number };
  stop: { price: number; points: number; wick: number };
  target: { price: number; r_planned: number; points: number };
  checklist: Record<string, boolean | null>;
}

export function pmExplainText(e: PmExplanation): string[] {
  const up = e.direction > 0;
  const pm = e.premarket;
  return [
    `Premarket ${nyTime(sec(pm.from))} – ${nyTime(sec(pm.to) + 60)} New York: high ${px(pm.high)} (${nyTime(sec(pm.high_ts))}), ` +
      `low ${px(pm.low)} (${nyTime(sec(pm.low_ts))}), a ${pm.range.toFixed(1)}-point range.`,
    `The ${e.candle.tf} candle of ${nyTime(sec(e.candle.t0))} closed at ${px(e.candle.c)}, ${up ? "above the premarket high" : "below the premarket low"} ` +
      `(${px(e.level)}): a ${up ? "long" : "short"}` + (e.trade_of_day > 1 ? ` (trade ${e.trade_of_day} of the day).` : "."),
    `Entry at the next minute's open (planned ${px(e.planned_entry)}, the ${up ? "ASK" : "BID"} close). Stop ${px(e.stop.price)} at the candle's ` +
      `${up ? "low" : "high"} (wick ${px(e.stop.wick)}), ${e.stop.points.toFixed(2)} points.`,
    `Target ${px(e.target.price)} = ${e.target.r_planned} × the stop distance (${e.target.points.toFixed(1)} points).`,
  ];
}

export function PmExplain({ e }: { e: PmExplanation }) {
  if (!e) return null;
  return (
    <div className="grid-cards">
      <Card title="Why it entered" testId="my-explain">
        <ul className="my-explain">{pmExplainText(e).map((t, k) => <li key={k}>{t}</li>)}</ul>
      </Card>
      <Card title="Checklist" testId="my-checklist">
        <div className="my-checks">{Object.entries(e.checklist).map(([k, v]) => (
          <div key={k} className={`my-check ${v === true ? "yes" : v === false ? "no" : "na"}`}>
            <span className="my-check-mark">{v === true ? "✓" : v === false ? "✗" : "–"}</span>
            <span>{k.charAt(0).toUpperCase() + k.slice(1)}</span><span className="muted small">{v === true ? "True" : v === false ? "False" : "not used"}</span>
          </div>))}</div>
      </Card>
    </div>
  );
}

export function PmTradeCharts({ doc, until }: { doc: { explanation: PmExplanation; charts: string[]; candles: Record<string, Candle[]>;
  entry_ts?: string; exit_ts?: string; stop_price?: number; target_price?: number; entry_price_theo?: number }; until?: boolean }) {
  const e = doc.explanation;
  const tfs = doc.charts.filter((t) => doc.candles[t]?.length);
  const [picked, setTf] = useState<string>(e?.candle?.tf && tfs.includes(e.candle.tf) ? e.candle.tf : tfs[0]);
  const [show, setShow] = useState({ range: true, candle: true });
  if (!e || !tfs.length) return null;
  const tf = tfs.includes(picked) ? picked : tfs[0];
  const sig = sec(e.signal_ts) + 60;
  const exitT = doc.exit_ts ? sec(doc.exit_ts) : undefined;
  const entry = doc.entry_price_theo ?? e.planned_entry;
  const stop = doc.stop_price ?? e.stop.price, target = doc.target_price ?? e.target.price;
  const pm = e.premarket;
  const lines: PriceLine[] = [
    { price: entry, label: `Entry ${px(entry)}`, color: "var(--accent)", from: sig - 60, to: exitT },
    { price: stop, label: `Stop ${px(stop)}`, color: "var(--c-neg)", from: sig - 60, to: exitT },
    { price: target, label: `Target ${px(target)}`, color: "var(--ok)", from: sig - 60, to: exitT },
  ];
  if (show.range) {
    lines.push({ price: pm.high, label: `PM high ${px(pm.high)}`, color: "var(--c2)", dash: "6 4", from: sec(pm.from), to: exitT ?? sig });
    lines.push({ price: pm.low, label: `PM low ${px(pm.low)}`, color: "var(--c2)", dash: "6 4", from: sec(pm.from), to: exitT ?? sig });
  }
  const boxes: PriceBox[] = [];
  if (show.range) boxes.push({ top: pm.high, bottom: pm.low, from: sec(pm.from), to: sec(pm.to) + 60, label: "Premarket", color: "var(--c2)" });
  if (show.candle) boxes.push({ top: e.candle.h, bottom: e.candle.l, from: sec(e.candle.t0), to: sig, label: `Breakout ${e.candle.tf}`, color: "var(--warn)" });
  const markers: Marker[] = [];
  const tfm = TF_MIN[tf] ?? 1;
  const from = Math.min(sec(pm.from), sig) - tfm * 60 * 5;
  return (
    <Card title="Charts" testId="my-charts" actions={
      <div className="inline small">
        {([["range", "Premarket range"], ["candle", "Breakout candle"]] as [keyof typeof show, string][])
          .map(([k, l]) => <Checkbox key={k} checked={show[k]} onChange={(v) => setShow({ ...show, [k]: v })} label={l} />)}
      </div>}>
      <Tabs tabs={tfs.map((t) => ({ id: t, label: t }))} active={tf} onChange={setTf} />
      <CandleChart key={tf} candles={doc.candles[tf]} tfMinutes={tfm} lines={lines} boxes={boxes} markers={markers}
        focus={{ from, to: until ? sig : (exitT ?? sig) }} cut={until ? sig - 60 : undefined} testId={`my-chart-${tf}`} />
      {until && <p className="muted small">The chart stops at the entry signal: you see only what was known at that moment.</p>}
    </Card>
  );
}
