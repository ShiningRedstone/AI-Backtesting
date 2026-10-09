/* Charts → Predictor (ADR-113): the Market simulator's 15-minute candle forecast and level map on today's live NQ prices,
   drawn on the chart, with the report-card verdict next to every forecast ("no proven skill" is shown faded and said so). */
import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { ChartCtl, PredOverlay } from "./engine";
import { fmtPrice } from "./model";

interface Skill { works: boolean; skill: number | null; name?: string }
interface PCandle { t: number; p_up: number | null; p_up_base: number; size_pts: number | null; range50: [number, number] | null;
  range80: [number, number] | null; usual_pts: number | null; complete: boolean; actual_up?: number; actual_pts?: number | null;
  o: number; h: number; l: number; c: number }
interface PLevel { label: string; price: number; side: number; dist: number; kind: string; stack: number; p_reach2h: number | null;
  p_reach: number | null; p_react: number | null; base_reach: number | null; turn_pick: boolean; touched_at: number | null; within2h: boolean | null }
interface PDecision { t: number; px: number; atr15: number; levels: PLevel[]; p_up_first: number | null; p_up_first_random_walk: number | null;
  turn_prob: Record<string, number>; land: Record<string, { median: number | null; band50: (number | null)[]; band80: (number | null)[]; actual: number | null }> }
export interface PredResult { date: string; last_minute: number; candles: PCandle[]; levelmaps: PDecision[]; analysis_key: string; computed_at: string;
  caveats: string[]; skill: Record<string, Skill>; has_levelmap: boolean; chosen: Record<string, unknown> }
interface PredAnswer { available: boolean; reason?: string; state?: string; step?: string | null; error?: { code: string; message: string } | null;
  result?: PredResult | null }

const pct = (v: number | null | undefined, d = 0) => (v == null ? "–" : `${(v * 100).toFixed(d)}%`);
const NY = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: "America/New_York" });
const nyTime = (sec: number) => NY.format(sec * 1000);

export function usePredictor(on: boolean, symbol: string) {
  const [ans, setAns] = useState<PredAnswer | null>(null);
  useEffect(() => {
    if (!on) { setAns(null); return; }
    let live = true, t = 0;
    const poll = async () => {
      let next = 15000;
      try {
        const a = await api.get<PredAnswer>(`/api/charts/predictor?symbol=${symbol}`);
        if (!live) return;
        setAns(a);
        if (a.available && !a.result) next = 3000;
      } catch (e) { if (live) setAns({ available: true, error: { code: "HTTP", message: (e as Error).message } }); }
      if (live) t = window.setTimeout(poll, document.hidden ? 60000 : next);
    };
    void poll();
    return () => { live = false; window.clearTimeout(t); };
  }, [on, symbol]);
  return ans;
}

/** The overlay drawn by the chart: candle badges + the chosen decision's levels and 2-hour landing band. */
export function overlayOf(r: PredResult | null | undefined, show: { candles: boolean; levels: boolean }, decT: number | null): PredOverlay | null {
  if (!r) return null;
  const sk = r.skill ?? {};
  const upOk = !!sk.up?.works, sizeOk = !!sk.size?.works, reachOk = !!sk.reach2h?.works, landOk = !!sk.land2h?.works;
  const last = r.candles[r.candles.length - 1];
  const candles = show.candles ? r.candles.map((k) => {
    const p = k.p_up ?? 0.5, up = p >= 0.5;
    const sizeTxt = k.size_pts == null ? "" : `≈${k.size_pts.toFixed(0)} pts${k.range50 ? ` (${k.range50[0].toFixed(0)}–${k.range50[1].toFixed(0)})` : ""}`;
    let mark = "";
    if (k.complete && k.actual_up != null) mark = k.actual_up === 0 ? "·" : (k.actual_up > 0) === up ? "✓" : "✗";
    return { t: k.t, up, high: k.h, current: k === last && !k.complete, faded: !upOk && !sizeOk,
      text: `${up ? "▲" : "▼"} ${pct(up ? p : 1 - p)}${upOk ? "" : " no skill"}`, sub: `${sizeTxt}${sizeOk ? "" : " · no skill"}`, mark };
  }) : [];
  const d = show.levels ? (r.levelmaps.find((x) => x.t === decT) ?? r.levelmaps[r.levelmaps.length - 1]) : undefined;
  const l2 = d?.land?.land2h;
  return {
    candles, levelsFrom: d ? d.t : null,
    levels: d ? d.levels.map((x) => ({ price: x.price, side: x.side, turn: x.turn_pick, faded: !reachOk,
      text: `${x.label} · ${pct(x.p_reach2h)} in 2 h${x.p_react != null ? ` · react ${pct(x.p_react)}` : ""}${x.touched_at ? " · touched" : ""}${reachOk ? "" : " · no skill"}` })) : [],
    land: d && l2 && l2.band50[0] != null && l2.band50[1] != null ? { lo: l2.band50[0], hi: l2.band50[1], mid: l2.median, to: d.t + 7200, faded: !landOk } : null,
  };
}

function SkillTag({ s, testId }: { s: Skill | undefined; testId?: string }) {
  if (!s) return <span className="pr-tag" data-testid={testId}>no verdict</span>;
  return <span className={`pr-tag${s.works ? " ok" : ""}`} data-testid={testId} title="Report card, discovery months the model choice never saw">
    {s.works ? "beats its baseline" : "no proven skill"}{s.skill != null ? ` (${s.skill > 0 ? "+" : ""}${(s.skill * 100).toFixed(1)} %)` : ""}</span>;
}

export function PredictorPanel({ ans, ctl, show, setShow }: { ans: PredAnswer | null; ctl: ChartCtl | null;
  show: { candles: boolean; levels: boolean }; setShow: (s: { candles: boolean; levels: boolean }) => void }) {
  const [decT, setDecT] = useState<number | null>(null);
  const [open, setOpen] = useState(true);
  const [banner, setBanner] = useState<string | null>(null);
  const lastDec = useRef<number | null>(null);
  const r = ans?.result ?? null;
  useEffect(() => { ctl?.setPredictor(ans?.available ? overlayOf(r, show, decT) : null); }, [ctl, r, show, decT, ans?.available]);
  useEffect(() => () => ctl?.setPredictor(null), [ctl]);
  useEffect(() => {                                                    // a banner when a new level map is made
    const d = r?.levelmaps[r.levelmaps.length - 1];
    if (!d) return;
    if (lastDec.current != null && d.t > lastDec.current) {
      const up = d.levels.find((x) => x.turn_pick && x.side > 0), dn = d.levels.find((x) => x.turn_pick && x.side < 0);
      setBanner(`New level map at ${nyTime(d.t)} New York${up ? ` · turning level above ${fmtPrice(up.price, 2)} (${up.label})` : ""}` +
        `${dn ? ` · below ${fmtPrice(dn.price, 2)} (${dn.label})` : ""}`);
      setDecT(null);
      const t = window.setTimeout(() => setBanner(null), 20000);
      lastDec.current = d.t;
      return () => window.clearTimeout(t);
    }
    lastDec.current = d.t;
    return undefined;
  }, [r]);
  if (!ans) return null;
  const cur = r?.candles[r.candles.length - 1];
  const d = r ? (r.levelmaps.find((x) => x.t === decT) ?? r.levelmaps[r.levelmaps.length - 1]) : undefined;
  return (
    <>
      {banner && <div className="pr-banner" data-testid="pr-banner" onClick={() => setBanner(null)}>{banner}</div>}
      <div className={`pr-panel${open ? "" : " closed"}`} data-testid="pr-panel" onMouseDown={(e: { stopPropagation(): void }) => e.stopPropagation()}>
        <div className="pr-head"><button type="button" className="ch-link" onClick={() => setOpen(!open)}>{open ? "▾" : "▸"} Predictor</button>
          {ans.available && <span className="faint">{ans.state === "ready" ? (r ? `updated ${nyTime(Date.parse(r.computed_at) / 1000)} NY` : "") : ans.state}</span>}</div>
        {open && (!ans.available ? <p className="ch-note">{ans.reason}</p> : <>
          {ans.step && !r && <p className="ch-note"><span className="spinner" /> {ans.step}{ans.state === "training" ? " (once per app start, about a minute)" : ""}</p>}
          {ans.error && <div className="tr-error" data-testid="pr-error">{ans.error.message}</div>}
          {r && <>
            <div className="pr-row"><label className="ch-check"><input type="checkbox" checked={show.candles} onChange={(e: { target: HTMLInputElement }) => setShow({ ...show, candles: e.target.checked })} />
              15-min candle forecast</label><SkillTag s={r.skill.up} testId="pr-skill-up" /></div>
            {cur && <div className="pr-line" data-testid="pr-current">{nyTime(cur.t)} candle: <b>{(cur.p_up ?? 0.5) >= 0.5 ? `up ${pct(cur.p_up)}` : `down ${pct(1 - (cur.p_up ?? 0.5))}`}</b>
              <span className="faint"> (usual {pct(cur.p_up_base)} up)</span> · size <b>≈{cur.size_pts?.toFixed(0)} pts</b>
              {cur.range80 && <span className="faint"> (80 %: {cur.range80[0].toFixed(0)}–{cur.range80[1].toFixed(0)})</span>} <SkillTag s={r.skill.size} /></div>}
            <div className="pr-row"><label className="ch-check"><input type="checkbox" checked={show.levels} onChange={(e: { target: HTMLInputElement }) => setShow({ ...show, levels: e.target.checked })} />
              Level map</label><SkillTag s={r.skill.reach2h} testId="pr-skill-reach" /></div>
            {r.has_levelmap && !r.levelmaps.length && <p className="ch-note">The first level map of the day is made at 9:30 New York.</p>}
            {r.levelmaps.length > 0 && <div className="pr-decs">{r.levelmaps.map((x) => (
              <button type="button" key={x.t} className={x.t === d?.t ? "on" : ""} onClick={() => setDecT(x.t)} data-testid={`pr-dec-${nyTime(x.t).replace(":", "")}`}>{nyTime(x.t)}</button>))}</div>}
            {d && <div className="pr-line" data-testid="pr-levelmap">Map of {nyTime(d.t)}: nearest level above traded first <b>{pct(d.p_up_first)}</b>
              <span className="faint"> (random walk {pct(d.p_up_first_random_walk)})</span> <SkillTag s={r.skill.first} />
              {d.land?.land2h?.median != null && <> · in 2 h ≈ <b>{fmtPrice(d.land.land2h.median, 2)}</b> <SkillTag s={r.skill.land2h} /></>}</div>}
            {r.caveats.map((c, i) => <p key={i} className="ch-note">{c}</p>)}
            <p className="ch-note">Models trained on all discovery days and frozen (analysis {r.analysis_key.slice(0, 8)}); live inputs only. A forecast, not a
              signal: see the report card in Market simulator → Predictions.</p>
          </>}
        </>)}
      </div>
    </>
  );
}
