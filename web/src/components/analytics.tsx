/* Analytics panels for ONE stored run (/api/results/<rid>/analytics). Used by the strategy drawer and the
   run-detail page. Every panel states its basis (net / gross) and sample scope; descriptive only. */
import type { ReactNode } from "react";
import type { BucketRow, RunAnalytics } from "../api/types";
import { BarChart, HBars, Histogram, LineChart, MonthHeatmap, PathsChart } from "./charts";
import { Badge, Card, Kpi, Scope, ScopeOf, TableWrap, n, pct, r, signCls } from "./ui";

function Head({ a, net = true, extra }: { a: RunAnalytics; net?: boolean; extra?: ReactNode }) {
  return <span className="labels"><ScopeOf status={a.run.status} holdout={a.run.holdout} /><Scope kind={net ? "net" : "gross"} />
    {a.run.synthetic && <Scope kind="synthetic" />}{extra}</span>;
}

export function Panel({ title, a, children, net = true, extra, testId, className }: {
  title: ReactNode; a: RunAnalytics; children?: ReactNode; net?: boolean; extra?: ReactNode; testId?: string; className?: string;
}) {
  return <Card className={className} title={<>{title} <Head a={a} net={net} extra={extra} /></>} testId={testId}>{children}</Card>;
}

export function BucketTable({ rows, first = "Bucket", testId }: { rows: BucketRow[]; first?: string; testId?: string }) {
  return (
    <TableWrap testId={testId}><table className="dense">
      <thead><tr><th>{first}</th><th className="r">Trades</th><th className="r">Net R</th><th className="r">Net R/trade</th>
        <th className="r">Gross R</th><th className="r">PF</th><th className="r">Win %</th><th>Sample</th></tr></thead>
      <tbody>{rows.map((b, i) => (
        <tr key={i}><td>{String(b.session ?? b.bucket)}</td><td className="r num">{b.trade_count}</td>
          <td className={`r num ${signCls(b.net_r)}`}>{n(b.net_r, 2)}</td><td className={`r num ${signCls(b.expectancy_r)}`}>{n(b.expectancy_r, 3)}</td>
          <td className="r num">{n(b.gross_r, 2)}</td><td className="r num">{n(b.profit_factor)}</td><td className="r num muted">{pct(b.win_rate, 0)}</td>
          <td className="small muted">{b.sample_label}</td></tr>))}</tbody></table></TableWrap>
  );
}

export function PerformanceKpis({ a }: { a: RunAnalytics }) {
  const m = a.metrics?.net ?? {}, g = a.metrics?.gross ?? {};
  return (
    <div className="kpis" data-testid="perf-kpis">
      <Kpi label="Trades" value={String(m.trade_count ?? 0)} sub={m.sample_label} />
      <Kpi label="Expectancy (net)" value={r(m.expectancy_r)} tone={signCls(m.expectancy_r) as "pos"} accent
        sub={Array.isArray(m.expectancy_ci95) ? `95% CI ${n(m.expectancy_ci95[0], 3)} … ${n(m.expectancy_ci95[1], 3)}` : undefined} />
      <Kpi label="Net R" value={n(m.net_r, 1)} tone={signCls(m.net_r) as "pos"} sub={`gross ${n(m.gross_r, 1)} · costs ${n(m.cost_r, 1)} R`} />
      <Kpi label="Profit factor (net)" value={n(m.profit_factor)} sub={`gross ${n(g.profit_factor)}`} />
      <Kpi label="Max drawdown" value={`${n(m.max_drawdown_r, 1)} R`} sub={m.max_drawdown_usd != null ? `$${n(m.max_drawdown_usd, 0)}` : undefined} />
      <Kpi label="Avg win / avg loss" value={`${n(m.avg_winner_r, 2)} / ${n(m.avg_loser_r, 2)}`} sub="R per trade" />
      <Kpi label="Win rate" value={pct(m.win_rate)} sub="shown, never used to rank" />
      <Kpi label="Cost per trade" value={m.trade_count ? `${n((m.cost_r ?? 0) / m.trade_count, 3)} R` : "—"}
        sub={a.breakeven_cost_multiplier == null ? undefined : a.breakeven_cost_multiplier <= 0 ? "gross R not positive: no break-even cost"
          : `break-even at ${n(a.breakeven_cost_multiplier, 2)}× costs`} />
    </div>
  );
}

export function PerformancePanels({ a }: { a: RunAnalytics }) {
  const years = a.year ?? [];
  return (
    <div className="panel-grid">
      <Panel a={a} title="By year" testId="an-year">
        <BarChart categories={years.map((y) => y.bucket)} unit="net R" series={[{ id: "net", label: "Net R", values: years.map((y) => y.net_r ?? null) }]}
          sub={(i) => `${years[i].trade_count} trades`} /></Panel>
      <Panel a={a} title="Long vs short" testId="an-direction"><BucketTable rows={a.direction ?? []} first="Direction" /></Panel>
      <Panel a={a} title="By session (entry time)" testId="an-session">
        <HBars rows={(a.session?.rows ?? []).map((s) => ({ label: `${s.session}`, value: s.expectancy_r ?? null, note: `${s.trade_count} tr` }))} />
        <p className="small muted">{a.session?.note}</p></Panel>
      <Panel a={a} title="By weekday (New York)" testId="an-weekday">
        <BarChart categories={(a.weekday ?? []).map((w) => w.bucket)} unit="R/trade"
          series={[{ id: "e", label: "Net R per trade", values: (a.weekday ?? []).map((w) => (w.trade_count ? w.expectancy_r ?? null : null)) }]}
          sub={(i) => `${(a.weekday ?? [])[i].trade_count} trades`} /></Panel>
      <Panel a={a} title="By month of year" testId="an-month">
        <BarChart categories={(a.month ?? []).map((w) => w.bucket)} unit="R/trade"
          series={[{ id: "e", label: "Net R per trade", values: (a.month ?? []).map((w) => (w.trade_count ? w.expectancy_r ?? null : null)) }]}
          sub={(i) => `${(a.month ?? [])[i].trade_count} trades`} /></Panel>
      <Panel a={a} title="Entry hour (New York)" testId="an-hour">
        <BarChart categories={(a.hour?.rows ?? []).map((w) => w.bucket)} unit="R/trade"
          series={[{ id: "e", label: "Net R per trade", values: (a.hour?.rows ?? []).map((w) => w.expectancy_r ?? null) }]} /></Panel>
      <Panel a={a} title="Monthly returns heatmap (net R)" className="span2" testId="an-heatmap">
        {a.monthly_heatmap && <MonthHeatmap years={a.monthly_heatmap.years} months={a.monthly_heatmap.months} cells={a.monthly_heatmap.cells} />}</Panel>
    </div>
  );
}

export function EquityPanels({ a }: { a: RunAnalytics }) {
  const pts = a.curve?.points ?? [];
  const roll = a.rolling_expectancy?.points ?? [];
  return (
    <div className="panel-grid">
      <Panel a={a} title="Cumulative net R" className="span2" testId="an-equity">
        <LineChart x={pts.map((p) => p.exit_ts)} unit="R" xLabel="exit time" height={240}
          series={[{ id: "eq", label: "Cumulative net R", values: pts.map((p) => p.equity_r), area: true }]} />
        {a.curve?.thinned && <p className="chart-foot">Thinned for display to {pts.length} points; endpoints and values are exact.</p>}</Panel>
      <Panel a={a} title="Underwater (drawdown from peak)" testId="an-underwater">
        <LineChart x={pts.map((p) => p.exit_ts)} unit="R" height={180}
          series={[{ id: "dd", label: "Drawdown (R)", values: pts.map((p) => -p.drawdown_r), area: true, color: "var(--c-neg)" }]} /></Panel>
      <Panel a={a} title={`Rolling expectancy (${a.rolling_expectancy?.window ?? 50} trades)`} testId="an-rolling">
        <LineChart x={roll.map((p) => p.exit_ts)} unit="R/trade" height={180}
          series={[{ id: "roll", label: "Rolling net R per trade", values: roll.map((p) => p.value) }]}
          emptyText={`Needs at least ${a.rolling_expectancy?.window ?? 50} trades.`} /></Panel>
    </div>
  );
}

export function TradePanels({ a }: { a: RunAnalytics }) {
  const wl = a.win_loss;
  const streakRows = (s: Record<string, number>) => Object.entries(s).map(([k, v]) => ({ label: `${k} in a row`, value: v }));
  return (
    <div className="panel-grid">
      <Panel a={a} title="R-multiple distribution" testId="an-rhist">
        <Histogram hist={a.r_histogram?.net} unit="net R per trade" color="var(--c1)" signed /></Panel>
      <Panel a={a} title="Holding time" testId="an-hold"><Histogram hist={a.holding_minutes} unit="minutes held" /></Panel>
      <Panel a={a} title="Win / loss" testId="an-winloss">
        {wl && <div className="kpis"><Kpi label="Winners" value={String(wl.wins)} /><Kpi label="Losers" value={String(wl.losses)} />
          <Kpi label="Flat" value={String(wl.flat)} /></div>}
        <div className="grid2" style={{ marginTop: 10 }}>
          <div><h4>Winning streaks</h4><HBars rows={streakRows(a.streaks?.wins ?? {})} unit="count" digits={0} /></div>
          <div><h4>Losing streaks</h4><HBars rows={streakRows(a.streaks?.losses ?? {})} unit="count" digits={0} /></div>
        </div></Panel>
      <Panel a={a} title="Exits by type" testId="an-exits"><BucketTable rows={a.exit_reason ?? []} first="Exit" /></Panel>
      <Panel a={a} title="Trade frequency (per month)" testId="an-freq">
        <BarChart categories={(a.trades_per_month ?? []).map((m) => m.month)} unit="trades" signed={false}
          series={[{ id: "t", label: "Trades", values: (a.trades_per_month ?? []).map((m) => m.trades), color: "var(--c2)" }]} /></Panel>
      <Panel a={a} title="Execution quote sides" testId="an-quotes">
        {a.quote_sides ? <>
          <TableWrap><table className="dense"><thead><tr><th>Direction / sides</th><th className="r">Trades</th></tr></thead>
            <tbody>{Object.entries(a.quote_sides.counts).map(([k, v]) => <tr key={k}><td>{k}</td><td className="r num">{v}</td></tr>)}</tbody></table></TableWrap>
          <p className="small muted">{a.quote_sides.rule}</p></> : <p className="muted small">This run records no quote sides (single price series).</p>}
        {a.mfe_mae && <p className="small">Average excursion: MFE <b>{n(a.mfe_mae.avg_mfe_r, 2)} R</b> · MAE <b>{n(a.mfe_mae.avg_mae_r, 2)} R</b></p>}
      </Panel>
    </div>
  );
}

export function CostPanel({ a }: { a: RunAnalytics }) {
  const rows = (a.cost_sensitivity?.rows ?? []) as Record<string, number>[];
  return (
    <Panel a={a} title="Cost sensitivity" testId="an-costs" extra={<Badge>exact: net(k) = gross − k × cost</Badge>}>
      <BarChart categories={rows.map((x) => `${x.cost_multiplier}×`)} unit="net R"
        series={[{ id: "net", label: "Net R at cost multiple", values: rows.map((x) => x.net_r ?? null) }]} />
      <p className="small">Break-even cost multiple: <b>{n(a.breakeven_cost_multiplier, 3)}</b>
        {typeof a.breakeven_cost_multiplier === "number" && a.breakeven_cost_multiplier <= 0 ? " (gross R is not positive: no cost level makes it profitable)" : ""}</p>
    </Panel>
  );
}

export function MonteCarloPanel({ a }: { a: RunAnalytics }) {
  const mc = a.monte_carlo_paths;
  const b = a.monte_carlo?.bootstrap ?? {}, s = a.monte_carlo?.shuffle ?? {};
  const fan = mc?.fan ?? {};
  return (
    <Panel a={a} title="Monte Carlo resampling" testId="an-mc" extra={<Scope kind="sim">Resampled trades</Scope>}>
      {mc && mc.paths.length > 0 && <PathsChart paths={mc.paths} highlight={mc.observed ? { label: "Observed order", values: mc.observed } : null}
        refLines={fan["5"] ? [{ value: fan["5"][fan["5"].length - 1], label: "5th pct final" }, { value: fan["95"][fan["95"].length - 1], label: "95th pct final", tone: "ok" }] : []} />}
      <TableWrap><table className="dense"><thead><tr><th>Method</th><th>Tests</th><th className="r">P(total &lt; 0)</th><th className="r">Total R p5 / p50 / p95</th>
        <th className="r">Max DD p50 / p95</th></tr></thead>
        <tbody>{[["bootstrap", b], ["shuffle", s]].map(([k, v]) => { const x = v as Record<string, any>;  // eslint-disable-line @typescript-eslint/no-explicit-any
          const tp = x.total_r_percentiles ?? {}, dp = x.max_drawdown_r_percentiles ?? {};
          return <tr key={String(k)}><td>{String(k)}</td><td className="small muted">{x.tests}</td><td className="r num">{pct(x.p_total_r_negative)}</td>
            <td className="r num">{n(tp["5"], 1)} / {n(tp["50"], 1)} / {n(tp["95"], 1)}</td><td className="r num">{n(dp["50"], 1)} / {n(dp["95"], 1)}</td></tr>; })}
        </tbody></table></TableWrap>
      <p className="small muted">{mc?.note ?? "Resampling of observed trades, not a market simulation."}</p>
    </Panel>
  );
}
