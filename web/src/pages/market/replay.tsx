/* Market simulator → Day replay (ADR-110): any predicted day (discovery walk-forward, new days, or the holdout after a
   look) with its 15-minute chart, the level map of a chosen moment, a day summary and every candle's predictions. */
import { useEffect, useMemo, useState } from "react";
import { market } from "../../api/market";
import type { DayCall, DayView, MapMoment } from "../../api/market";
import { useApi } from "../../app/context";
import { go, useRoute } from "../../app/router";
import { CandleChart } from "../../components/candles";
import type { Marker, PriceLine } from "../../components/candles";
import { Button, Card, Empty, ErrorPanel, Kpi, PageSkeleton, Select, TableWrap, pct } from "../../components/ui";
import { PageHead } from "../MyStrategy";
import { MODEL_WORDS, STAGE_NAMES, num, nyTime } from "./shared";

type Src = "discovery" | "new" | "holdout";
const SRC_WORDS: Record<Src, string> = { discovery: "Discovery (walk-forward)", new: "New days (live)", holdout: "Holdout (after a look)" };

export function MarketDayReplayPage() {
  const route = useRoute();
  const src = ((route.query.get("src") as Src | null) ?? "discovery");
  const days = useApi<{ days: string[] }>(market.daysUrl(src), [src]);
  const want = route.query.get("day") ?? "";
  const list = days.data?.days ?? [];
  const day = list.includes(want) ? want : list[list.length - 1] ?? "";
  const set = (s: Src, d: string) => go(`/market-day?src=${s}${d ? `&day=${d}` : ""}`);
  const idx = list.indexOf(day);
  return (
    <div className="page mk" data-testid="market-replay">
      <PageHead title="Day replay" />
      <p className="mk-lead">Pick a day and see what the forecasts said before each 15-minute candle, and what actually happened. Every prediction
        was made live, by models that never saw that day.</p>
      <Card testId="market-day-pick">
        <div className="row gap">
          <Select value={src} onChange={(v) => set(v as Src, "")} ariaLabel="Days" testId="market-src"
            options={(Object.keys(SRC_WORDS) as Src[]).map((s) => ({ value: s, label: SRC_WORDS[s] }))} />
          {list.length > 0 && <>
            <Button small kind="ghost" disabled={idx <= 0} onClick={() => set(src, list[idx - 1])}>← Previous</Button>
            <Select value={day} onChange={(d) => set(src, d)} ariaLabel="Day" testId="market-day-select"
              options={[...list].reverse().map((d) => ({ value: d, label: d }))} />
            <Button small kind="ghost" disabled={idx < 0 || idx >= list.length - 1} onClick={() => set(src, list[idx + 1])}>Next →</Button></>}
          {days.data && !list.length && <span className="muted small">{src === "new" ? "No new days predicted yet (Start here → step 5)." :
            src === "holdout" ? "Shown only after a holdout look (Holdout tests)." : "No predicted days."}</span>}
        </div>
      </Card>
      {day && <DayCard key={`${src}/${day}`} day={day} src={src} />}
    </div>
  );
}

function DayCard({ day, src }: { day: string; src: string }) {
  const { data, error } = useApi<DayView>(market.dayUrl(day, src), [day, src]);
  const [mi, setMi] = useState(0);
  useEffect(() => setMi(0), [day, src]);
  const moments = data?.levelmap ?? [];
  const mo: MapMoment | undefined = moments[Math.min(mi, Math.max(0, moments.length - 1))];
  const candles = useMemo(() => (data?.candles ?? []).map((c) => [Math.floor(c.t / 1e9), c.o, c.h, c.l, c.c] as [number, number, number, number, number]), [data]);
  const lines = useMemo<PriceLine[]>(() => {
    if (!mo || !candles.length) return [];
    const hi = Math.max(...candles.map((c) => c[2])), lo = Math.min(...candles.map((c) => c[3]));
    const span = Math.max(hi - lo, 1);
    const near = (side: number) => mo.levels.filter((l) => l.side === side).sort((a, b) => Math.abs(a.dist) - Math.abs(b.dist)).slice(0, 3);
    const pick = new Set([...near(1), ...near(-1), ...mo.levels.filter((l) => l.turn_pick)]);
    const ls: PriceLine[] = mo.levels.filter((l) => pick.has(l) && l.price <= hi + 0.3 * span && l.price >= lo - 0.3 * span)
      .map((l) => ({ price: l.price, label: `${l.label} · ${pct(l.p_reach2h, 0)} in 2 h${l.turn_pick ? " · likely turn" : ""}`,
        color: l.turn_pick ? "var(--c-survivor)" : "var(--muted)", dash: l.turn_pick ? undefined : "4 3" }));
    const b = mo.land.land2h.band50;
    if (b[0] != null && b[1] != null) {
      ls.push({ price: b[0] as number, label: "2 h landing range (50 %)", color: "var(--c2)", dash: "2 2" });
      ls.push({ price: b[1] as number, label: "", color: "var(--c2)", dash: "2 2" });
    }
    ls.push({ price: mo.px, label: `price at ${nyTime(mo.t)}`, color: "var(--c1)" });
    return ls;
  }, [mo, candles]);
  const markers = useMemo<Marker[]>(() => {
    if (!data) return [];
    const ms: Marker[] = data.news.filter((n) => n.impact >= 2).map((n) => {
      const c = data.candles.find((x) => x.t <= n.t && n.t < x.t + 15 * 60e9) ?? data.candles[0];
      return { t: Math.floor(n.t / 1e9), price: c ? c.h : 0, label: n.impact === 3 ? `★ ${n.name}` : n.name, color: n.impact === 3 ? "var(--c-neg)" : "var(--c-survivor)" };
    });
    for (const s of data.shocks) {
      const c = data.candles.find((x) => x.t <= s.start && s.start < x.t + 15 * 60e9);
      if (c) ms.push({ t: Math.floor(s.start / 1e9), price: s.move_pts > 0 ? c.h : c.l, label: "shock", color: "var(--c2)" });
    }
    return ms;
  }, [data]);
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <PageSkeleton layout="overview" label="Loading the day" />;
  const up = data.chosen.up ?? "logistic";
  const rth = data.candles.filter((c) => { const h = Number(new Date(c.t / 1e6).toLocaleString("en-GB", { timeZone: "America/New_York", hour: "2-digit", hour12: false }));
    return h >= 8 && h < 17; });
  // day summary (official models)
  const withUp = data.candles.filter((c) => c.p_up?.[up] != null && c.c !== c.o);
  const upRight = withUp.filter((c) => (c.p_up![up]! > 0.5) === (c.c > c.o)).length;
  const withBand = data.candles.filter((c) => c.size_q && c.size_q[0] != null && c.actual_size != null);
  const inBand = withBand.filter((c) => c.actual_size! >= (c.size_q![0] as number) && c.actual_size! <= (c.size_q![3] as number)).length;
  const calls = Object.values(data.direction ?? {}).flatMap((x) => Object.values(x)).filter((c) => c.called && c.actual !== 0);
  const callsRight = calls.filter((c) => ((c.p ?? 0.5) > 0.5) === (c.actual > 0)).length;
  return (
    <>
      <Card title={`${day} · summary`} testId="market-day-summary">
        <div className="kpis">
          <Kpi label="Next candle up / down" value={withUp.length ? pct(upRight / withUp.length, 0) : "–"} sub={`right on ${upRight} of ${withUp.length} candles (${MODEL_WORDS[up]})`} />
          <Kpi label="Candle size inside the 80 % range" value={withBand.length ? pct(inBand / withBand.length, 0) : "–"} sub={`${inBand} of ${withBand.length} · honest ≈ 80 %`} />
          <Kpi label="Direction calls" value={calls.length ? pct(callsRight / calls.length, 0) : "none"} sub={calls.length ? `right on ${callsRight} of ${calls.length} calls` : "no confident call that day"} />
          <Kpi label="News" value={data.news.filter((n) => n.impact >= 3).length} sub={`${data.news.length} releases in total`} />
        </div>
      </Card>
      <Card title="15-minute chart with the level map" testId="market-levelmap"
        actions={moments.length > 0 && <Select value={String(Math.min(mi, moments.length - 1))} onChange={(v) => setMi(Number(v))} ariaLabel="Moment"
          testId="market-levelmap-time" options={moments.map((m, i) => ({ value: String(i), label: `Levels at ${nyTime(m.t)}` }))} />}>
        <CandleChart candles={candles} tfMinutes={15} lines={lines} markers={markers} height={440} testId="market-day-chart" />
        <p className="small muted">{mo ? <>Lines: the 3 nearest levels on each side at {nyTime(mo.t)} with their chance of being traded within 2 hours;
          amber = the most likely turning level; blue dashes = where price should be 2 hours later (50 % range). </> : "No level map for this day. "}
          Markers: red / orange news and shocks.</p>
      </Card>
      {mo && <LevelLadder mo={mo} />}
      <Card title="Candle by candle (8:00 – 17:00 New York)" testId="market-day-table">
        <TableWrap><table className="dense"><thead><tr><th>Time</th><th className="num">Chance up</th><th>Actual</th><th className="num">Size range (× usual)</th>
          <th className="num">Actual size</th><th>Direction calls (rest of the candle)</th></tr></thead>
          <tbody>{rth.map((c) => {
            const p = c.p_up?.[up];
            const act = c.c > c.o ? "up" : c.c < c.o ? "down" : "flat";
            const hit = p != null && act !== "flat" ? ((p > 0.5) === (act === "up")) : null;
            const band = c.size_q && c.size_q[0] != null ? `${Math.exp(c.size_q[0] as number).toFixed(2)} – ${Math.exp(c.size_q[3] as number).toFixed(2)}` : "–";
            return (
              <tr key={c.t} title={(c.why ?? []).map((w) => `${w.input} ${w.push != null && w.push > 0 ? "↑" : "↓"}`).join(" · ")}>
                <td>{nyTime(c.t)}</td><td className="num">{p != null ? pct(p, 0) : "–"}</td>
                <td>{act}{hit == null ? "" : hit ? " ✓" : " ✗"}</td><td className="num">{band}</td>
                <td className="num">{c.actual_size != null ? Math.exp(c.actual_size).toFixed(2) : "–"}</td>
                <td className="small"><DayCalls calls={data.direction?.[String(Math.round(c.t / 1e9))]} /></td></tr>);
          })}</tbody></table></TableWrap>
        <p className="small muted">"Chance up" = the official model's probability that the candle closes above its open; "Size range" = the 80 % range of its
          high–low size against the usual size at that time. Hover a row to see what pushed the up / down forecast most.</p>
      </Card>
      {data.bias.length > 0 && <Card title="Does the session close above the price at that moment?">
        <TableWrap><table className="dense"><thead><tr><th>Time</th>{["logistic", "boosting", "similar", "baseline"].map((m) => <th key={m} className="num">{MODEL_WORDS[m]}</th>)}</tr></thead>
          <tbody>{data.bias.map((b) => <tr key={b.t}><td>{nyTime(b.t)}</td>
            {["logistic", "boosting", "similar", "baseline"].map((m) => <td key={m} className="num">{pct(b[m], 0)}</td>)}</tr>)}</tbody></table></TableWrap>
      </Card>}
      {data.news.length > 0 && <Card title="News that day">
        <TableWrap><table className="dense"><thead><tr><th>Time</th><th>Event</th><th className="num">Forecast</th><th className="num">Actual</th><th className="num">Surprise</th></tr></thead>
          <tbody>{data.news.map((n, i) => <tr key={i}><td>{nyTime(n.t)}</td>
            <td>{n.name} <span className="faint">({["none", "yellow", "orange", "red"][n.impact]})</span></td><td className="num">{n.forecast ?? "–"}</td>
            <td className="num">{n.actual ?? "–"}</td><td className="num">{n.surprise_z != null ? `${n.surprise_z.toFixed(1)}σ` : "–"}</td></tr>)}</tbody></table></TableWrap>
      </Card>}
    </>
  );
}

function LevelLadder({ mo }: { mo: MapMoment }) {
  const [all, setAll] = useState(false);
  const shown = all ? mo.levels : mo.levels.filter((l) => l.turn_pick || Math.abs(l.dist) <= 4);
  const landRow = (k: "land2h" | "land", w: string) => {
    const L = mo.land[k];
    const inside = (b: (number | null)[]) => b[0] != null && b[1] != null && L.actual >= (b[0] as number) && L.actual <= (b[1] as number);
    return (
      <tr key={k}><td>{w}</td><td className="num">{num(L.median, 2)}</td><td className="num">{num(L.band50[0], 2)} – {num(L.band50[1], 2)}</td>
        <td className="num">{num(L.band80[0], 2)} – {num(L.band80[1], 2)}</td><td className="num">{num(L.actual, 2)}</td>
        <td>{inside(L.band50) ? "inside the 50 % range" : inside(L.band80) ? "inside the 80 % range" : "outside"}</td></tr>);
  };
  return (
    <Card title={`Levels ahead at ${nyTime(mo.t)}`} testId="market-levelmap-table-card"
      actions={<Button small kind="ghost" onClick={() => setAll(!all)}>{all ? "Fewer levels" : "All levels"}</Button>}>
      <p className="small">Price {num(mo.px, 2)} (15-min ATR {num(mo.atr15, 1)} pts). Nearest level above traded before the nearest below:
        {" "}<b>{pct(mo.p_up_first, 0)}</b> (random walk {pct(mo.p_up_first_random_walk, 0)}) → {mo.up_first == null ? "neither / unknown" :
        mo.up_first > 0.5 ? "above first" : "below first"}. Most likely turn: above {pct(mo.turn_prob.above, 0)}, below {pct(mo.turn_prob.below, 0)}.</p>
      <TableWrap><table className="dense" data-testid="market-levelmap-table"><thead><tr><th>Level</th><th className="num">Price</th><th className="num">ATR away</th>
        <th className="num">Traded in 2 h</th><th className="num">Traded by the close</th><th className="num">Reacts if touched</th><th>What happened</th></tr></thead>
        <tbody>{shown.map((l, i) => (
          <tr key={i} className={l.turn_pick ? "row-hl" : ""}><td>{l.label}{l.stack > 1 ? ` (stack of ${l.stack})` : ""}{l.turn_pick ? " · likely turn" : ""}</td>
            <td className="num">{l.price.toFixed(2)}</td><td className="num">{l.dist > 0 ? "+" : ""}{l.dist.toFixed(1)}</td>
            <td className="num">{pct(l.p_reach2h, 0)}</td><td className="num">{pct(l.p_reach, 0)} <span className="faint">({pct(l.base_reach, 0)})</span></td>
            <td className="num">{l.p_react != null ? pct(l.p_react, 0) : "–"} <span className="faint">({pct(l.base_react, 0)})</span></td>
            <td className="small">{!l.reached ? "not traded" : `traded ${l.touch_ns ? nyTime(l.touch_ns) : ""}${l.reached2h ? "" : " (after 2 h)"} · ${l.reacted == null ?
              "reaction unknown" : l.reacted > 0.5 ? "reacted ✓" : "went through"}`}</td></tr>))}</tbody></table></TableWrap>
      <p className="small muted">Grey numbers in brackets = the baseline. {all ? "" : "Levels within 4 ATR and the likely turns are listed."}</p>
      {!shown.length && <Empty>No levels within 4 ATR.</Empty>}
      <TableWrap><table className="dense"><thead><tr><th>Where price lands</th><th className="num">Middle guess</th><th className="num">50 % range</th>
        <th className="num">80 % range</th><th className="num">Actual</th><th /></tr></thead>
        <tbody>{landRow("land2h", "2 hours later")}{landRow("land", "Session end")}</tbody></table></TableWrap>
    </Card>
  );
}

function DayCalls({ calls }: { calls?: Record<string, DayCall> }) {
  if (!calls) return <span className="faint">–</span>;
  const made = ["0", "5", "10"].filter((s) => calls[s]?.called);
  if (!made.length) return <span className="faint">no confident call</span>;
  return <>{made.map((s, i) => {
    const c = calls[s];
    const up = (c.p ?? 0.5) > 0.5;
    const hit = c.actual === 0 ? null : (c.actual > 0) === up;
    return <span key={s} title={`${STAGE_NAMES[s]}: ${pct(c.p, 0)} up, from ${c.ref.toFixed(2)}`}>{i ? " · " : ""}{STAGE_NAMES[s].replace("At ", "at ")} {up ? "↑" : "↓"}
      {" "}{pct(up ? c.p : 1 - (c.p ?? 0.5), 0)}{hit == null ? "" : hit ? " ✓" : " ✗"}</span>;
  })}</>;
}
