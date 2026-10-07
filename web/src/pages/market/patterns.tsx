/* Market simulator → Patterns (ADR-106, reorganised in ADR-110): every ICT / SMC concept per chart and the edge scan. */
import { useState } from "react";
import type { EdgeCell, EdgeGroup, Edges, EffectRow, PatternRow } from "../../api/market";
import { BarChart } from "../../components/charts";
import { Badge, Banner, Card, Empty, Kpi, PageSkeleton, Select, TableWrap, pct } from "../../components/ui";
import { PageHead } from "../MyStrategy";
import { NeedsAnalysis, OUTCOME_WORDS, TF_NAMES, dirWord, kindName, useSection } from "./shared";

export function MarketPatternsPage() {
  const pats = useSection<PatternRow[]>("patterns");
  const eff = useSection<EffectRow[]>("effect_matrix");
  const edges = useSection<Edges>("edges");
  const [kind, setKind] = useState<string>("FVG");
  const [tf, setTf] = useState<string>("15");
  if (pats.error) return <NeedsAnalysis title="Patterns" error={pats.error} />;
  if (!pats.data) return <div className="page"><PageHead title="Patterns" /><PageSkeleton layout="overview" label="Loading" /></div>;
  const rows = pats.data.data;
  const kinds = pats.data.kinds;
  const kindOpts = [...new Set(rows.map((x) => x.kind))].map((k) => ({ value: k, label: kindName(kinds, k) }));
  const tfs = [...new Set(rows.filter((x) => x.kind === kind).map((x) => x.tf))].sort((a, b) => a - b);
  const sel = rows.filter((x) => x.kind === kind && String(x.tf) === tf);
  const effRows = (eff.data?.data ?? []).filter((e) => (e.kind === kind || e.kind === `${kind}_FORMED`) && e.tf === TF_NAMES[Number(tf)]);
  return (
    <div className="page mk" data-testid="market-patterns">
      <PageHead title="Patterns (ICT / SMC)" />
      <p className="mk-lead">Every ICT / SMC concept on every chart from 1 minute to the daily: how often it appears, whether price came back to
        it, and what the next candles of every timeframe did afterwards. The edge scan at the bottom looks for any of them doing better than usual.</p>
      <Card title="Pick a concept and a chart" testId="market-pattern-pick">
        <div className="row gap">
          <Select value={kind} onChange={(v) => { setKind(v); const t2 = rows.filter((x) => x.kind === v).map((x) => x.tf); if (!t2.includes(Number(tf))) setTf(String(t2[0] ?? 15)); }}
            options={kindOpts} ariaLabel="Concept" testId="market-kind" />
          <Select value={tf} onChange={setTf} options={tfs.map((t) => ({ value: String(t), label: TF_NAMES[t] ?? String(t) }))} ariaLabel="Chart" testId="market-tf" />
        </div>
        <p className="small muted">FVG = three bars with a gap. Touched / 50 % (CE) / filled = price entered the zone / reached its middle / traded through it. Held = price moved 1 ATR away from the zone before trading through it. Left behind = never filled by the end of the data.</p>
      </Card>
      {sel.map((p) => <PatternCard key={`${p.kind}${p.tf}${p.dir}`} p={p} kinds={kinds} eff={effRows.filter((e) => e.dir === p.dir)} />)}
      {!sel.length && <Empty>No instances of this concept on this chart.</Empty>}
      <EdgeScan edges={edges.data?.data} kinds={kinds} />
    </div>
  );
}

function PatternCard({ p, kinds, eff }: { p: PatternRow; kinds: Record<string, string>; eff: EffectRow[] }) {
  const zone = p.median_fill_min != null || p.filled > 0;
  return (
    <Card title={`${kindName(kinds, p.kind)} · ${TF_NAMES[p.tf]} · ${dirWord(p.dir)}`} testId={`market-pattern-${p.dir > 0 ? "bull" : "bear"}`}>
      <div className="kpis">
        <Kpi label="How often" value={p.n.toLocaleString()} sub={`${p.per_day.toFixed(2)} per day`} />
        {zone && <Kpi label="Filled" value={pct(p.filled, 0)}
          sub={`touched ${pct(p.touched, 0)} · 50 % ${pct(p.ce, 0)} · within 1h ${pct(p.filled_1h, 0)}, 1 day ${pct(p.filled_1d, 0)}, 5 days ${pct(p.filled_5d, 0)}`} />}
        {zone && <Kpi label="Left behind" value={p.left_behind.toLocaleString()} sub={p.left_behind_median_age_days != null ? `median ${p.left_behind_median_age_days.toFixed(0)} days old` : "none"} />}
        {zone && <Kpi label="Time to fill (median)" value={p.median_fill_min != null ? `${Math.round(p.median_fill_min)} min` : "–"}
          sub={p.median_touch_min != null ? `first touch after ${Math.round(p.median_touch_min)} min` : undefined} />}
        <Kpi label="Held / reacted" value={p.held != null ? pct(p.held, 0) : "–"} sub={`of ${p.held_n.toLocaleString()} with a result`} />
        <Kpi label="1 ATR its way first" value={p.edge != null ? pct(p.edge, 1) : "–"} sub={`${p.edge_n.toLocaleString()} cases · chance ≈ 50 %`} accent />
      </div>
      {eff.map((e) => (
        <div key={e.kind}>
          <h3 className="small-head">{e.kind.endsWith("_FORMED") ? "Effect on every timeframe right after it FORMS" : "Effect on every timeframe after price TOUCHES it (or after it happens)"}</h3>
          <TableWrap><table className="dense"><thead><tr><th>Next candle of</th>{e.effects.map((x) => <th key={x.tf} className="num">{x.tf}</th>)}</tr></thead>
            <tbody>
              <tr><td>went its way</td>{e.effects.map((x) => <td key={x.tf} className="num">{x.same_way != null ? pct(x.same_way, 0) : "–"}</td>)}</tr>
              <tr><td>size vs usual (median)</td>{e.effects.map((x) => <td key={x.tf} className="num">{x.size_median != null ? `${x.size_median.toFixed(2)}×` : "–"}</td>)}</tr>
            </tbody></table></TableWrap>
          <p className="small muted">On the 15-minute chart: a 15-min break of structure its way within the next 2 hours {pct(e.m15_bos, 0)}; the next four 15-min candles moved
            {" "}{e.m15_4_atr ? `${e.m15_4_atr.q25.toFixed(2)} / ${e.m15_4_atr.q50.toFixed(2)} / ${e.m15_4_atr.q75.toFixed(2)}` : "–"} 15-min ATRs its way (q25 / median / q75).</p>
        </div>))}
      <h3 className="small-head">By session: moved 1 ATR its way first (chance ≈ 50 %)</h3>
      <BarChart categories={p.by_session.map((s) => s.session)} unit="%" signed={false} height={160}
        series={[{ id: "e", label: "moved 1 ATR its way first, by session", values: p.by_session.map((s) => (s.edge == null ? null : s.edge * 100)) }]}
        sub={(i) => `${p.by_session[i].n} cases`} />
    </Card>
  );
}

function EdgeGroups({ groups, kinds, tradeable, total }: { groups: EdgeGroup[]; kinds: Record<string, string>; tradeable: number; total: number }) {
  return (
    <>
      <Banner tone={tradeable ? "ok" : "info"} testId="market-edge-groups">The {total} confirmed combinations are <b>{groups.length} distinct effects</b> (the
        same effect under many conditions counts once here). Tradeable after costs: <b>{tradeable}</b>.</Banner>
      <TableWrap><table className="dense"><thead><tr><th>Effect</th><th>Outcome</th><th className="num">Best: first 70 % → last 30 % (usual)</th>
        <th className="num">Conditions</th><th>After costs</th></tr></thead>
        <tbody>{groups.slice(0, 40).map((g, i) => (
          <tr key={i}><td>{kindName(kinds, g.kind)} · {g.tf} · {g.more_often ? "works MORE often than usual" : "works LESS often (the opposite happens)"}</td>
            <td className="small">{OUTCOME_WORDS[g.outcome]}</td>
            <td className="num">{pct(g.best.rate_find, 1)} → {g.best.rate_confirm != null ? pct(g.best.rate_confirm, 1) : "–"} ({pct(g.best.base_find, 1)})</td>
            <td className="num" title={g.conditions.join(" | ")}>{g.cells}</td>
            <td>{g.outcome !== "edge" ? <span className="muted small">not a trade outcome</span> : g.tradeable ? <Badge tone="ok">clears costs</Badge> :
              <Badge>below break-even</Badge>}</td></tr>))}</tbody></table></TableWrap>
    </>
  );
}

function EdgeScan({ edges, kinds }: { edges?: Edges; kinds: Record<string, string> }) {
  if (!edges) return null;
  const line = (c: EdgeCell) => `${kindName(kinds, c.kind)} · ${c.tf} · ${dirWord(c.dir)} · ${c.condition}`;
  return (
    <Card title="Edge scan: found early, confirmed late" testId="market-edges">
      <p className="small">{edges.cells_tested.toLocaleString()} combinations were tested on the first {pct(edges.find_share, 0)} of the discovery period
        (only the first event per 15 minutes counts, each compared with the usual rate at the same time of day). {edges.passed_find} survived the
        false-discovery correction; <b>{edges.confirmed}</b> then went the same way on the last {pct(1 - edges.find_share, 0)}, which the scan never saw.</p>
      {edges.groups && edges.candidates.length > 0 && <EdgeGroups groups={edges.groups} kinds={kinds} tradeable={edges.tradeable ?? 0} total={edges.confirmed} />}
      {!edges.candidates.length ? <Banner tone="info">No edge candidates: nothing in these patterns did better than the usual rate by more than chance
        explains, once the number of combinations is taken into account.</Banner> :
        <TableWrap><table className="dense"><thead><tr><th>Pattern and condition</th><th>Outcome</th><th className="num">First 70 %</th><th className="num">Usual</th>
          <th className="num">Last 30 %</th><th className="num">Needed after costs</th></tr></thead>
          <tbody>{edges.candidates.slice(0, 60).map((c, i) => (
            <tr key={i}><td>{line(c)}</td><td className="small">{OUTCOME_WORDS[c.outcome]}</td>
              <td className="num">{pct(c.rate_find, 1)} ({c.n_find})</td><td className="num">{pct(c.base_find, 1)}</td>
              <td className="num">{c.rate_confirm != null ? `${pct(c.rate_confirm, 1)} (${c.n_confirm})` : "–"}</td>
              <td className="num">{c.breakeven != null ? pct(c.breakeven, 1) : "–"}{c.tradeable === true ? " ✓" : ""}</td></tr>))}</tbody></table></TableWrap>}
      <p className="small muted">"Needed after costs": the win rate a 1:1 trade at one ATR of that chart needs to cover the spread of the session the events
        happened in plus commission and slippage of one MNQ contract (✓ = the better side clears it). A candidate is not a strategy: it is something to test once, forward.</p>
    </Card>
  );
}
