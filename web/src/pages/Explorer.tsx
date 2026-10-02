import { useEffect, useMemo, useState } from "react";
import type { ExplorerResponse, ExplorerRow } from "../api/types";
import { go, href, useRoute } from "../app/router";
import { useApi, useApp } from "../app/context";
import { StrategyDetail } from "../components/strategy/detail";
import { RunPicker, useCriteriaName } from "../components/results";
import { facetLabel, humanize } from "../app/labels";
import { Badge, Button, Card, Drawer, FavStar, Empty, ErrorPanel, Loading, Pager, Scope, SortTh, TableWrap, n, pct, signCls,
  useDebounced } from "../components/ui";

const FILTERS: { key: string; label: string }[] = [
  { key: "family_id", label: "Family" }, { key: "instrument", label: "Market" }, { key: "timeframe", label: "Timeframe" },
  { key: "session", label: "Session" }, { key: "entry_type", label: "Entry" }, { key: "stop_type", label: "Stop" },
  { key: "target_type", label: "Target" }, { key: "trailing", label: "Trailing stop" }, { key: "signal_exit", label: "Signal exit" },
  { key: "direction", label: "Direction" }, { key: "source", label: "Source" }, { key: "state", label: "State" },
];
const KEYS = ["q", "strategy_id", "scope", "protocol", "min_trades", "max_trades_per_week", "tested_only", "survivors_only", "favorites_only",
  "prop", "campaign_run", "sort", "order", "page",
  "page_size", ...FILTERS.map((f) => f.key)];
const CHIP: Record<string, string> = { q: "Search", strategy_id: "Strategy ID", scope: "Scope", protocol: "Protocol",
  min_trades: "Minimum trades", max_trades_per_week: "Maximum trades per week", tested_only: "Tested only", survivors_only: "Survivors only",
  favorites_only: "Favorites only", prop: "Prop firm result", campaign_run: "Backtests" };
const PROP_TEXT: Record<string, string> = { eval: "passes evaluation", payout: "passes evaluation and payout" };
const SCOPE_TEXT: Record<string, string> = { in_sample: "In-sample", oos: "Out-of-sample", walk_forward: "Walk-forward", any: "Any" };
function optLabel(k: string, v: string, states?: Record<string, string>): string {
  if (k === "state") return states?.[v] ?? humanize(v);
  if (k === "scope") return SCOPE_TEXT[v] ?? humanize(v);
  if (k === "tested_only" || k === "survivors_only" || k === "favorites_only") return "yes";
  if (k === "prop") return PROP_TEXT[v] ?? v;
  if (k === "campaign_run") return "one research run";
  if (["q", "strategy_id", "min_trades", "max_trades_per_week", "protocol"].includes(k)) return v;
  return facetLabel(k, v);
}
const DEFAULTS: Record<string, string> = { scope: "in_sample", sort: "expectancy_r", order: "desc", page: "1", page_size: "50" };
/** ADR-85 Holdout results: the same explorer over each strategy's holdout-evaluation run. */
const HOLDOUT_DEFAULTS: Record<string, string> = { ...DEFAULTS, scope: "holdout", tested_only: "1" };
const OUTCOME_TEXT: Record<string, string> = { HOLDOUT_CRITERIA_MET: "Criteria met", HOLDOUT_CRITERIA_NOT_MET: "Criteria not met" };

function readQuery(q: URLSearchParams, defaults: Record<string, string> = DEFAULTS): Record<string, string> {
  const out: Record<string, string> = { ...defaults };
  for (const k of KEYS) { const v = q.get(k); if (v) out[k] = v; }
  return out;
}

export function ExplorerPage({ holdout = false }: { holdout?: boolean } = {}) {
  const route = useRoute();
  const { prefs } = useApp();
  const crit = useCriteriaName();
  const defs = holdout ? HOLDOUT_DEFAULTS : DEFAULTS;
  const base = holdout ? "/holdout-results" : "/explorer";
  const [f, setF] = useState<Record<string, string>>(() => readQuery(route.query, defs));
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
    window.history.replaceState(null, "", `#${base}${qs.toString() ? `?${qs}` : ""}`);
  }, [params, open]);
  const { data, error, loading } = useApi<ExplorerResponse>(`/api/explorer/strategies?${params}`, [params]);
  const set = (k: string, v: string) => setF((x) => ({ ...x, [k]: v, ...(k !== "page" ? { page: "1" } : {}) }));
  const active = Object.entries(f).filter(([k, v]) => v && !["sort", "order", "page", "page_size"].includes(k) && defs[k] !== v);
  const reset = () => setF({ ...defs });
  const onSort = (k: string, o: "asc" | "desc") => setF((x) => ({ ...x, sort: k, order: o, page: "1" }));
  const facet = (k: string) => data?.facets[k] ?? [];
  const toggle = (id: string) => setSelected((s) => { const nx = new Set(s); if (nx.has(id)) nx.delete(id); else nx.add(id); return nx; });
  const sortProps = { sort: f.sort, order: f.order as "asc" | "desc", onSort };
  return (
    <div className="page" data-testid={holdout ? "holdout-results" : "explorer"}>
      <header className="page-head">
        <div><h1>{holdout ? "Holdout results" : "Strategy explorer"}</h1></div>
        <div className="actions">
          {!holdout && <div className="segmented small" role="group" aria-label="scope">
            {[["in_sample", "In-sample"], ["oos", "Out-of-sample"], ["walk_forward", "Walk-forward"], ["any", "Any"]].map(([k, l]) =>
              <button key={k} className={f.scope === k ? "on" : ""} onClick={() => set("scope", k)} data-testid={`scope-${k}`}>{l}</button>)}
          </div>}
          {selected.size >= 1 && <Button small onClick={() => go(`/compare?source=runs&id=${[...selected].map((s) => data?.rows.find((x) => x.strategy_id === s)?.ref_run?.run_id).filter(Boolean).join(",")}`)}
            disabled={![...selected].some((s) => data?.rows.find((x) => x.strategy_id === s)?.ref_run)}>Compare {selected.size} selected</Button>}
        </div>
      </header>

      {holdout && <p className="small muted" data-testid="holdout-results-note">Each strategy's one holdout test: a backtest on exactly the locked
        holdout dates, judged by the protocol's pre-registered criteria. These results are kept apart from the discovery results under
        Strategies. Run tests under Run backtest → <a href={href("/holdout")}>Holdout backtest</a>.</p>}
      <div className="filterbar" data-testid="explorer-filters">
        <input className={`input search-box${f.q ? " active" : ""}`} placeholder="Search name, ID, family, hypothesis…" value={f.q ?? ""}
          aria-label="search" data-testid="explorer-q" onChange={(e: { target: HTMLInputElement }) => set("q", e.target.value)} />
        {prefs.show_ids && <input className={`input${f.strategy_id ? " active" : ""}`} style={{ width: 150 }} placeholder="Strategy ID" value={f.strategy_id ?? ""}
          aria-label="strategy id" onChange={(e: { target: HTMLInputElement }) => set("strategy_id", e.target.value.toUpperCase())} />}
        <RunPicker value={f.campaign_run ?? ""} onChange={(v) => set("campaign_run", v)} />
        <select className={`input${f.prop ? " active" : ""}`} value={f.prop ?? ""} aria-label="prop firm result" data-testid="filter-prop"
          title={`Prop firm result under ${crit} (change the account in Settings)`} onChange={(e: { target: HTMLSelectElement }) => set("prop", e.target.value)}>
          <option value="">Prop firm: all</option>
          <option value="eval">Passes evaluation</option>
          <option value="payout">Passes evaluation and payout</option>
        </select>
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
        <span className="fgroup">max per week</span>
        <input className={`input num-filter${f.max_trades_per_week ? " active" : ""}`} inputMode="decimal" value={f.max_trades_per_week ?? ""}
          aria-label="maximum trades per week" onChange={(e: { target: HTMLInputElement }) => set("max_trades_per_week", e.target.value.replace(/[^0-9.]/g, ""))} />
        <label className="check small"><input type="checkbox" checked={f.tested_only === "1"}
          onChange={(e: { target: HTMLInputElement }) => set("tested_only", e.target.checked ? "1" : "")} />tested only</label>
        <label className="check small"><input type="checkbox" checked={f.favorites_only === "1"} data-testid="filter-favorites"
          onChange={(e: { target: HTMLInputElement }) => set("favorites_only", e.target.checked ? "1" : "")} />favorites only</label>
        <label className="check small"><input type="checkbox" checked={f.survivors_only === "1"} data-testid="filter-survivors"
          onChange={(e: { target: HTMLInputElement }) => set("survivors_only", e.target.checked ? "1" : "")} />survivors only</label>
        <Button small kind="ghost" onClick={reset} disabled={!active.length} testId="explorer-reset">Reset</Button>
      </div>
      {active.length > 0 && <div className="active-filters" data-testid="active-filters">Active:
        {active.map(([k, v]) => <span key={k} className="fchip">{CHIP[k] ?? FILTERS.find((x) => x.key === k)?.label ?? humanize(k)}: {optLabel(k, v, data?.states)}
          <button aria-label={`clear ${k}`} onClick={() => set(k, k === "scope" ? defs.scope : (defs[k] ?? ""))}>×</button></span>)}</div>}

      <Card className="flush" title={<>{data ? <><b className="num">{data.total.toLocaleString()}</b> of {data.library_total.toLocaleString()} strategies</> : "Strategies"}
        {data && <><Scope kind={holdout ? "holdout" : f.scope === "oos" ? "oos" : f.scope === "walk_forward" ? "wf" : "is"}>{data.scope_label}</Scope><Scope kind="net" /></>}
        {loading && <span className="spinner" aria-label="loading" />}</>} testId="explorer-results">
        {error ? <div className="card-body"><ErrorPanel error={error} /></div> : !data ? <div className="card-body"><Loading label="Loading strategies…" /></div>
          : !data.rows.length && holdout && active.length === 0 ? <div className="card-body"><Empty>No holdout tests yet. Pick survivors under Run backtest →
              {" "}<a href={href("/holdout")}>Holdout backtest</a>.</Empty></div>
          : !data.rows.length ? <div className="card-body"><Empty>{data.library_total ? <>No strategy matches these filters.{" "}
              <button className="linklike" onClick={reset}>Clear all filters</button>{f.scope !== "any" && <> or switch the scope to <b>Any</b>
              (a strategy without a run in this scope shows empty metrics).</>}</> : <>The strategy library is empty. <a href={href("/builder?new=1")}>Create a strategy</a>.</>}</Empty></div>
          : <>
            <TableWrap testId="explorer-table" className={holdout ? "" : "fit"}><table className={holdout ? "dense" : "dense fit-table"}>
              <thead><tr>
                <th style={{ width: 26 }} /><th style={{ width: 30 }} aria-label="favorite" />
                <SortTh k="short_name" label="Strategy" {...sortProps} />
                <SortTh k="family_id" label="Family" {...sortProps} />
                {!holdout && <th>Market</th>}<SortTh k="timeframe" label="Time­frame" {...sortProps} />{!holdout && <th>Session</th>}
                {holdout && <th title="the protocol's pre-registered criteria, all must be met">Verdict</th>}
                <SortTh k="trade_count" label="Trades" right {...sortProps} />
                <SortTh k="trades_per_week" label="Per week" right {...sortProps} />
                <SortTh k="expectancy_r" label="Net R per trade" right {...sortProps} title="average result per trade after costs" />
                <SortTh k="net_r" label="Total net R" right {...sortProps} />
                <SortTh k="avg_rr" label="Reward to risk" right {...sortProps} />
                <SortTh k="profit_factor" label="Profit factor" right {...sortProps} />
                {!holdout && <SortTh k="win_rate" label="Win rate" right {...sortProps} title="shown for completeness; never a ranking criterion" />}
                <SortTh k="max_drawdown_r" label="Max draw­down (R)" right {...sortProps} />
                <th title={`Prop firm evaluation under ${crit} (change the account in Settings)`}>Eval</th>
                <th title={`First payout under ${crit} (change the account in Settings)`}>Payout</th>
                {holdout && <>
                  <SortTh k="holdout_random_control_p" label="Random comparison" right {...sortProps}
                    title="share of 100 random-entry runs doing at least as well (p-value); lower is better, required ≤ 0.05" />
                  <th title="net R stays at or above zero with costs × 1.5 and × 2">Cost stress</th>
                  <SortTh k="discovery_expectancy_r" label="Discovery net R per trade" right {...sortProps}
                    title="the same strategy on the discovery period (before the holdout), for comparison" />
                </>}
              </tr></thead>
              <tbody>{data.rows.map((x) => <Row key={x.strategy_id} x={x} sel={selected.has(x.strategy_id)} onSel={() => toggle(x.strategy_id)}
                onOpen={() => setOpen(x.strategy_id)} holdout={holdout} />)}</tbody>
            </table></TableWrap>
            <Pager page={data.page} pages={data.pages} total={data.total} pageSize={data.page_size}
              onPage={(p) => set("page", String(p))} onPageSize={(s) => set("page_size", String(s))} />
          </>}
      </Card>
      {data && <p className="small muted">{data.basis} {data.note}</p>}

      <Drawer open={!!open} onClose={() => setOpen(null)} testId="strategy-drawer"
        title={open ? (data?.rows.find((x) => x.strategy_id === open)?.display_name ?? "Strategy") : ""}
        actions={open ? <Button small onClick={() => go(`/strategies/${open}`)}>Open strategy page</Button> : null}>
        {open && <StrategyDetail key={open} id={open} scope={holdout ? "holdout" : undefined} />}
      </Drawer>
    </div>
  );
}

/** Passed / Failed under the Settings account; "—" when that account has no stored audit for the backtest. */
const PassFail = ({ v }: { v: boolean | null | undefined }) => (v == null ? <span className="faint" title="not checked">—</span>
  : <span className={`pf ${v ? "pf-pass" : "pf-fail"}`}>{v ? "Passed" : "Failed"}</span>);


function Row({ x, sel, onSel, onOpen, holdout }: { x: ExplorerRow; sel: boolean; onSel: () => void; onOpen: () => void; holdout?: boolean }) {
  return (
    <tr className={`clickable${sel ? " selected" : ""}`} onClick={onOpen} data-testid={`xrow-${x.strategy_id}`}>
      <td onClick={(e: { stopPropagation: () => void }) => e.stopPropagation()}><input type="checkbox" checked={sel} onChange={onSel}
        aria-label={`select ${x.strategy_id}`} /></td>
      <td onClick={(e: { stopPropagation: () => void }) => e.stopPropagation()}><FavStar id={x.strategy_id} /></td>
      <td className="name-cell"><div className="cell-title">{x.short_name ?? humanize(x.name ?? "—")}</div>
        <div className="cell-badges">{x.survivor && <Badge tone="ok">Survivor</Badge>}{x.synthetic && <Scope kind="synthetic">synthetic</Scope>}</div></td>
      <td className="small wrap">{x.family_name ?? facetLabel("family_id", x.family_id)}</td>
      {!holdout && <td className="small">{facetLabel("instrument", x.instrument)}</td>}<td>{facetLabel("timeframe", x.timeframe)}</td>
      {!holdout && <td className="small wrap">{facetLabel("session", x.session)}</td>}
      {holdout && <td data-testid={`verdict-${x.strategy_id}`}>{x.holdout_outcome ? <Badge tone={x.holdout_outcome === "HOLDOUT_CRITERIA_MET" ? "ok" : "warn"}>
        {OUTCOME_TEXT[x.holdout_outcome] ?? humanize(x.holdout_outcome)}</Badge> : <span className="faint">—</span>}</td>}
      <td className="r num">{x.trade_count ?? "—"}</td><td className="r num">{n(x.trades_per_week, 1)}</td>
      <td className={`r num ${signCls(x.expectancy_r)}`} style={{ fontWeight: 650 }}>{x.expectancy_r == null ? "—" : n(x.expectancy_r, 3)}</td>
      <td className={`r num ${signCls(x.net_r)}`}>{n(x.net_r, 1)}</td>
      <td className="r num">{x.avg_rr == null ? "—" : `${n(x.avg_rr, 2)}`}</td>
      <td className="r num">{n(x.profit_factor)}</td>{!holdout && <td className="r num faint">{pct(x.win_rate, 0)}</td>}
      <td className="r num">{n(x.max_drawdown_r, 1)}</td>
      <td data-testid={`pass-eval-${x.strategy_id}`}><PassFail v={x.prop_pass_eval} /></td>
      <td data-testid={`pass-payout-${x.strategy_id}`}><PassFail v={x.prop_pass_payout} /></td>
      {holdout && <>
        <td className="r num">{x.holdout_random_control_p == null ? "—" : n(x.holdout_random_control_p, 3)}</td>
        <td><PassFail v={x.holdout_cost_stress_met} /></td>
        <td className={`r num ${signCls(x.discovery_expectancy_r)}`}>{x.discovery_expectancy_r == null ? "—" : n(x.discovery_expectancy_r, 3)}</td>
      </>}
    </tr>
  );
}
