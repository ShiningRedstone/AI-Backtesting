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
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <Loading label="Loading strategies…" />;
  const rows = data.filter((r) => !r.archived);
  return (
    <div className="page" data-testid="run-backtest-page">
      <header className="page-head"><div><h1>Run a backtest</h1>
        <div className="subtitle small">One strategy on one dataset, through the backtest engine with its causality check. The result is recorded
          as in-sample and appears under Backtest results.</div></div></header>
      {!rows.length ? <Empty>No saved strategies yet. <a href={href("/builder?new=1")}>Create one in the builder</a>.</Empty> : <>
        <div className="grid3">
          <Field label="Strategy">
            <Select value={sid} onChange={setSid} placeholder="Choose a strategy…" testId="run-strategy"
              options={rows.map((r) => ({ value: r.strategy_id, label: `${strategyLabel(r.name)} · ${r.timeframe ? facetLabel("timeframe", r.timeframe) : "timeframe not set"}` }))} />
          </Field>
        </div>
        {sid ? <BacktestPanel key={sid} strategy={sid} /> : <p className="muted small">Choose a strategy to see which datasets it can run on.</p>}
      </>}
    </div>
  );
}
