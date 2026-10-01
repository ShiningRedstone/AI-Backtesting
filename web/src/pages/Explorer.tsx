import { useEffect, useMemo, useState } from "react";
import type { ExplorerResponse, ExplorerRow } from "../api/types";
import { go, href, useRoute } from "../app/router";
import { useApi } from "../app/context";
import { StrategyDetail } from "../components/strategy/detail";
import { facetLabel, humanize } from "../app/labels";
import { Badge, Button, Card, Drawer, Empty, ErrorPanel, Loading, Pager, Scope, SortTh, TableWrap, n, pct, r, signCls,
  useDebounced } from "../components/ui";

const FILTERS: { key: string; label: string }[] = [
  { key: "family_id", label: "Family" }, { key: "instrument", label: "Market" }, { key: "timeframe", label: "Timeframe" },
  { key: "session", label: "Session" }, { key: "entry_type", label: "Entry" }, { key: "stop_type", label: "Stop" },
  { key: "target_type", label: "Target" }, { key: "trailing", label: "Trailing stop" }, { key: "signal_exit", label: "Signal exit" },
  { key: "direction", label: "Direction" }, { key: "source", label: "Source" }, { key: "state", label: "State" },
];
const KEYS = ["q", "strategy_id", "scope", "protocol", "min_trades", "max_trades_per_week", "tested_only", "survivors_only", "sort", "order", "page",
  "page_size", ...FILTERS.map((f) => f.key)];
const CHIP: Record<string, string> = { q: "Search", strategy_id: "Strategy ID", scope: "Scope", protocol: "Protocol",
  min_trades: "Minimum trades", max_trades_per_week: "Maximum trades per week", tested_only: "Tested only", survivors_only: "Survivors only" };
const SCOPE_TEXT: Record<string, string> = { in_sample: "In-sample", oos: "Out-of-sample", walk_forward: "Walk-forward", any: "Any" };
function optLabel(k: string, v: string, states?: Record<string, string>): string {
  if (k === "state") return states?.[v] ?? humanize(v);
  if (k === "scope") return SCOPE_TEXT[v] ?? humanize(v);
  if (k === "tested_only" || k === "survivors_only") return "yes";
  if (["q", "strategy_id", "min_trades", "max_trades_per_week", "protocol"].includes(k)) return v;
  return facetLabel(k, v);
}
const DEFAULTS: Record<string, string> = { scope: "in_sample", sort: "expectancy_r", order: "desc", page: "1", page_size: "50" };

function readQuery(q: URLSearchParams): Record<string, string> {
  const out: Record<string, string> = { ...DEFAULTS };
  for (const k of KEYS) { const v = q.get(k); if (v) out[k] = v; }
  return out;
}

export function ExplorerPage() {
  const route = useRoute();
  const [f, setF] = useState<Record<string, string>>(() => readQuery(route.query));
  const [open, setOpen] = useState<string | null>(route.query.get("open"));
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const q = useDebounced(f.q ?? "", 300);
  const params = useMemo(() => {
    const p = new URLSearchParams();
    for (const k of KEYS) { const v = k === "q" ? q : f[k]; if (v) p.set(k, v); }
    return p.toString();
  }, [f, q]);
  useEffect(() => {        // keep filters in the URL so Back from a strategy page restores them (no history spam)
    const qs = new URLSearchParams(params);
    if (open) qs.set("open", open);
    window.history.replaceState(null, "", `#/explorer${qs.toString() ? `?${qs}` : ""}`);
  }, [params, open]);
  const { data, error, loading } = useApi<ExplorerResponse>(`/api/explorer/strategies?${params}`, [params]);
  const set = (k: string, v: string) => setF((x) => ({ ...x, [k]: v, ...(k !== "page" ? { page: "1" } : {}) }));
  const active = Object.entries(f).filter(([k, v]) => v && !["sort", "order", "page", "page_size"].includes(k) && DEFAULTS[k] !== v);
  const reset = () => setF({ ...DEFAULTS });
  const onSort = (k: string, o: "asc" | "desc") => setF((x) => ({ ...x, sort: k, order: o, page: "1" }));
  const facet = (k: string) => data?.facets[k] ?? [];
  const toggle = (id: string) => setSelected((s) => { const nx = new Set(s); if (nx.has(id)) nx.delete(id); else nx.add(id); return nx; });
  const sortProps = { sort: f.sort, order: f.order as "asc" | "desc", onSort };
  return (
    <div className="page" data-testid="explorer">
      <header className="page-head">
        <div><div className="eyebrow">Strategies</div><h1>Strategy explorer</h1>
          <div className="subtitle small">Every stored strategy with the statistics of its latest run in the chosen scope. Ordered by what you pick;
            nothing here is a ranking of "best" strategies.</div></div>
        <div className="actions">
          <div className="segmented small" role="group" aria-label="scope">
            {[["in_sample", "In-sample"], ["oos", "Out-of-sample"], ["walk_forward", "Walk-forward"], ["any", "Any"]].map(([k, l]) =>
              <button key={k} className={f.scope === k ? "on" : ""} onClick={() => set("scope", k)} data-testid={`scope-${k}`}>{l}</button>)}
          </div>
          {selected.size >= 1 && <Button small onClick={() => go(`/compare?source=runs&id=${[...selected].map((s) => data?.rows.find((x) => x.strategy_id === s)?.ref_run?.run_id).filter(Boolean).join(",")}`)}
            disabled={![...selected].some((s) => data?.rows.find((x) => x.strategy_id === s)?.ref_run)}>Compare {selected.size} selected</Button>}
        </div>
      </header>

      <div className="filterbar" data-testid="explorer-filters">
        <input className={`input search-box${f.q ? " active" : ""}`} placeholder="Search name, ID, family, hypothesis…" value={f.q ?? ""}
          aria-label="search" data-testid="explorer-q" onChange={(e: { target: HTMLInputElement }) => set("q", e.target.value)} />
        <input className={`input${f.strategy_id ? " active" : ""}`} style={{ width: 150 }} placeholder="Strategy ID" value={f.strategy_id ?? ""}
          aria-label="strategy id" onChange={(e: { target: HTMLInputElement }) => set("strategy_id", e.target.value.toUpperCase())} />
        {FILTERS.map((fl) => (
          <select key={fl.key} className={`input${f[fl.key] ? " active" : ""}`} value={f[fl.key] ?? ""} aria-label={fl.label}
            data-testid={`filter-${fl.key}`} onChange={(e: { target: HTMLSelectElement }) => set(fl.key, e.target.value)}>
            <option value="">{fl.label}: all</option>
            {facet(fl.key).map((v) => <option key={v} value={v}>{optLabel(fl.key, v, data?.states)}</option>)}
          </select>))}
        <select className={`input${f.protocol ? " active" : ""}`} value={f.protocol ?? ""} aria-label="protocol"
          onChange={(e: { target: HTMLSelectElement }) => set("protocol", e.target.value)}>
          <option value="">Protocol: any</option>
          {(data?.protocols ?? []).map((p) => <option key={p.protocol_id} value={p.protocol_id}>{p.name ?? p.protocol_id} ({humanize(p.status)})</option>)}
        </select>
        <span className="fgroup">min trades</span>
        <input className={`input num-filter${f.min_trades ? " active" : ""}`} inputMode="numeric" value={f.min_trades ?? ""} aria-label="minimum trades"
          data-testid="filter-min-trades" onChange={(e: { target: HTMLInputElement }) => set("min_trades", e.target.value.replace(/[^0-9]/g, ""))} />
        <span className="fgroup">max /wk</span>
        <input className={`input num-filter${f.max_trades_per_week ? " active" : ""}`} inputMode="decimal" value={f.max_trades_per_week ?? ""}
          aria-label="maximum trades per week" onChange={(e: { target: HTMLInputElement }) => set("max_trades_per_week", e.target.value.replace(/[^0-9.]/g, ""))} />
        <label className="check small"><input type="checkbox" checked={f.tested_only === "1"}
          onChange={(e: { target: HTMLInputElement }) => set("tested_only", e.target.checked ? "1" : "")} />tested only</label>
        <label className="check small"><input type="checkbox" checked={f.survivors_only === "1"} data-testid="filter-survivors"
          onChange={(e: { target: HTMLInputElement }) => set("survivors_only", e.target.checked ? "1" : "")} />survivors only</label>
        <Button small kind="ghost" onClick={reset} disabled={!active.length} testId="explorer-reset">Reset</Button>
      </div>
      {active.length > 0 && <div className="active-filters" data-testid="active-filters">Active:
        {active.map(([k, v]) => <span key={k} className="fchip">{CHIP[k] ?? FILTERS.find((x) => x.key === k)?.label ?? humanize(k)}: {optLabel(k, v, data?.states)}
          <button aria-label={`clear ${k}`} onClick={() => set(k, k === "scope" ? "in_sample" : "")}>×</button></span>)}</div>}

      <Card className="flush" title={<>{data ? <><b className="num">{data.total.toLocaleString()}</b> of {data.library_total.toLocaleString()} strategies</> : "Strategies"}
        {data && <><Scope kind={f.scope === "oos" ? "oos" : f.scope === "walk_forward" ? "wf" : "is"}>{data.scope_label}</Scope><Scope kind="net" /></>}
        {loading && <span className="spinner" aria-label="loading" />}</>} testId="explorer-results">
        {error ? <div className="card-body"><ErrorPanel error={error} /></div> : !data ? <div className="card-body"><Loading label="Loading strategies…" /></div>
          : !data.rows.length ? <div className="card-body"><Empty>{data.library_total ? <>No strategy matches these filters.{" "}
              <button className="linklike" onClick={reset}>Clear all filters</button>{f.scope !== "any" && <> or switch the scope to <b>Any</b>
              (a strategy without a run in this scope shows empty metrics).</>}</> : <>The strategy library is empty. <a href={href("/builder?new=1")}>Create a strategy</a>.</>}</Empty></div>
          : <>
            <TableWrap testId="explorer-table"><table className="dense">
              <thead><tr>
                <th style={{ width: 28 }} />
                <SortTh k="strategy_id" label="Strategy" {...sortProps} width={210} />
                <SortTh k="family_id" label="Family" {...sortProps} />
                <th>Market</th><SortTh k="timeframe" label="Timeframe" {...sortProps} /><th>Session</th>
                <SortTh k="trade_count" label="Trades" right {...sortProps} />
                <SortTh k="trades_per_week" label="Per week" right {...sortProps} />
                <SortTh k="expectancy_r" label="Net R per trade" right {...sortProps} title="average result per trade after costs" />
                <SortTh k="gross_r_per_trade" label="Before costs" right {...sortProps} />
                <SortTh k="net_r" label="Total net R" right {...sortProps} />
                <SortTh k="avg_rr" label="Reward to risk" right {...sortProps} />
                <SortTh k="profit_factor" label="Profit factor" right {...sortProps} />
                <SortTh k="max_drawdown_r" label="Max drawdown R" right {...sortProps} />
                <SortTh k="cost_r_per_trade" label="Cost per trade" right {...sortProps} />
                <SortTh k="win_rate" label="Win rate" right {...sortProps} title="shown for completeness; never a ranking criterion" />
                <th className="r">Out-of-sample</th><th>Prop</th><th>State</th>
              </tr></thead>
              <tbody>{data.rows.map((x) => <Row key={x.strategy_id} x={x} sel={selected.has(x.strategy_id)} onSel={() => toggle(x.strategy_id)}
                onOpen={() => setOpen(x.strategy_id)} />)}</tbody>
            </table></TableWrap>
            <Pager page={data.page} pages={data.pages} total={data.total} pageSize={data.page_size}
              onPage={(p) => set("page", String(p))} onPageSize={(s) => set("page_size", String(s))} />
          </>}
      </Card>
      {data && <p className="small muted">{data.basis} {data.note}</p>}

      <Drawer open={!!open} onClose={() => setOpen(null)} testId="strategy-drawer"
        title={open ? humanize(data?.rows.find((x) => x.strategy_id === open)?.name ?? "Strategy") : ""}
        subtitle={<>Descriptive statistics of stored runs; formal acceptance happens only in a protocol holdout evaluation.</>}
        actions={open ? <Button small onClick={() => go(`/strategies/${open}`)}>Open strategy page</Button> : null}>
        {open && <StrategyDetail key={open} id={open} />}
      </Drawer>
    </div>
  );
}

const STATE_TONE: Record<string, "ok" | "warn" | "error" | "info" | "neutral"> = {
  untested: "neutral", tested: "neutral", oos_tested: "info", shortlisted: "info", holdout_granted: "info",
  holdout_criteria_met: "ok", holdout_criteria_not_met: "warn",
};

function Row({ x, sel, onSel, onOpen }: { x: ExplorerRow; sel: boolean; onSel: () => void; onOpen: () => void }) {
  return (
    <tr className={`clickable${sel ? " selected" : ""}`} onClick={onOpen} data-testid={`xrow-${x.strategy_id}`}>
      <td onClick={(e: { stopPropagation: () => void }) => e.stopPropagation()}><input type="checkbox" checked={sel} onChange={onSel}
        aria-label={`select ${x.strategy_id}`} /></td>
      <td><div style={{ color: "var(--text)", fontWeight: 560 }}>{humanize(x.name ?? "—")}</div>
        {x.survivor && <Badge tone="ok">Survivor</Badge>}{x.synthetic && <> <Scope kind="synthetic">synthetic</Scope></>}</td>
      <td className="small">{x.family_name ?? facetLabel("family_id", x.family_id)}</td>
      <td className="small">{x.instrument ?? "—"}</td><td>{facetLabel("timeframe", x.timeframe)}</td><td className="small">{facetLabel("session", x.session)}</td>
      <td className="r num">{x.trade_count ?? "—"}</td><td className="r num">{n(x.trades_per_week, 1)}</td>
      <td className={`r num ${signCls(x.expectancy_r)}`} style={{ fontWeight: 650 }}>{x.expectancy_r == null ? "—" : r(x.expectancy_r)}</td>
      <td className="r num muted">{n(x.gross_r_per_trade, 3)}</td>
      <td className={`r num ${signCls(x.net_r)}`}>{n(x.net_r, 1)}</td>
      <td className="r num">{x.avg_rr == null ? "—" : `${n(x.avg_rr, 2)}`}</td>
      <td className="r num">{n(x.profit_factor)}</td><td className="r num">{n(x.max_drawdown_r, 1)}</td>
      <td className="r num muted">{n(x.cost_r_per_trade, 3)}</td><td className="r num faint">{pct(x.win_rate, 0)}</td>
      <td className={`r num ${signCls(x.oos_expectancy_r)}`}>{x.oos_expectancy_r == null ? <span className="faint">—</span> : n(x.oos_expectancy_r, 3)}</td>
      <td className="small">{x.prop_pass_payout == null ? <span className="faint">—</span> : x.prop_pass_payout ? "Pass and payout" : "No payout"}</td>
      <td><Badge tone={STATE_TONE[x.state] ?? "neutral"}>{x.state_label}</Badge></td>
    </tr>
  );
}
