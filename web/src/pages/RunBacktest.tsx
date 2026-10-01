import { useState } from "react";
import type { LibraryRow } from "../api/types";
import { useApi } from "../app/context";
import { facetLabel, strategyLabel } from "../app/labels";
import { href } from "../app/router";
import { BacktestPanel } from "../components/strategy";
import { Empty, ErrorPanel, Field, Loading, Select } from "../components/ui";

/** Run backtest -> Single backtest: pick a saved strategy; the existing backtest panel lists the datasets with their
 *  readiness (validation, timeframe, costs; CFD refused while costs are unconfigured) and runs one causality-checked
 *  backtest through the engine (recorded as in-sample). */
export function RunBacktestPage() {
  const { data, error } = useApi<LibraryRow[]>("/api/strategies");
  const [sid, setSid] = useState("");
  const [q, setQ] = useState("");
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <Loading label="Loading strategies…" />;
  const rows = data.filter((r) => !r.archived);
  const match = rows.filter((r) => !q || `${r.display_name ?? ""} ${r.name} ${r.family_id}`.toLowerCase().includes(q.toLowerCase()));
  const shown = match.slice(0, 300);                                     // a long list stays fast; search narrows it
  return (
    <div className="page" data-testid="run-backtest-page">
      <header className="page-head"><div><h1>Run a backtest</h1></div></header>
      {!rows.length ? <Empty>No saved strategies yet. <a href={href("/builder?new=1")}>Create one in the builder</a>.</Empty> : <>
        <div className="grid3">
          <Field label="Search" hint={match.length > shown.length ? `showing ${shown.length} of ${match.length.toLocaleString()}; type to narrow` : undefined}>
            <input className="input" value={q} placeholder="Search strategies…" aria-label="search strategies"
              onChange={(e: { target: HTMLInputElement }) => setQ(e.target.value)} />
          </Field>
          <Field label="Strategy">
            <Select value={sid} onChange={setSid} placeholder="Choose a strategy…" testId="run-strategy"
              options={shown.map((r) => ({ value: r.strategy_id, label: `${r.display_name ?? strategyLabel(r.name)}${r.timeframe && !r.display_name ? ` · ${facetLabel("timeframe", r.timeframe)}` : ""}` }))} />
          </Field>
        </div>
        {sid ? <BacktestPanel key={sid} strategy={sid} /> : <p className="muted small">Choose a strategy to see which datasets it can run on.</p>}
      </>}
    </div>
  );
}
