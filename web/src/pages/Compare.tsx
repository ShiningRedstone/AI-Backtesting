import { useState } from "react";
import type { BatchRow, Comparison, LibraryRow, SearchBatch } from "../api/types";
import { go, useRoute } from "../app/router";
import { useApi } from "../app/context";
import { facetLabel, statusLabel, strategyLabel } from "../app/labels";
import { plainText } from "../components/research";
import { CompareTable } from "../components/strategy/lab";
import { Banner, Button, Card, Empty, ErrorPanel, Field, Loading, Mono, Select, TechDetails, shortTime } from "../components/ui";

type Source = "lineage" | "strategy" | "batch" | "search" | "runs";
const SOURCES: { value: Source; label: string }[] = [
  { value: "lineage", label: "A strategy and all its descendants (versions + variations)" },
  { value: "strategy", label: "One strategy version" },
  { value: "batch", label: "A variation batch (base + children)" },
  { value: "search", label: "A research search (its current cells)" },
];

/** Phase 8: transparent side-by-side metrics of stored runs. Sorting and filtering are views; the
 *  backend returns rows unranked and nothing here scores, ranks or recommends. */
export function ComparePage() {
  const route = useRoute();
  const source = (route.query.get("source") as Source) || "";
  const id = route.query.get("id") || "";
  return (
    <div className="page" data-testid="compare-page">
      <header className="page-head"><h1>Compare research results</h1></header>
      <Banner tone="info">Transparent metrics of stored runs. No best-strategy score, no winner, no recommendation. Each row keeps its sample
        scope: in-sample rows are exploratory; out-of-sample and walk-forward rows evaluate a fixed definition on held-out time.</Banner>
      <SourcePicker source={source} id={id} />
      {source && id ? <CompareResults key={`${source}:${id}`} source={source} id={id} /> : <Empty>Choose what to compare.</Empty>}
    </div>
  );
}

function SourcePicker({ source, id }: { source: Source | ""; id: string }) {
  const [kind, setKind] = useState<Source>(source && source !== "runs" ? source : "lineage");
  const [pick, setPick] = useState(source === kind ? id : "");
  const strategies = useApi<LibraryRow[]>("/api/strategies");
  const batches = useApi<BatchRow[]>("/api/variation-batches");
  const searches = useApi<SearchBatch[]>("/api/research/searches");
  const opts = kind === "batch" ? (batches.data ?? []).map((b) => ({ value: b.batch_id, label: `${strategyLabel(b.base_name)} · ${b.generated} variants · ${shortTime(b.created_at)}` }))
    : kind === "search" ? (searches.data ?? []).map((s) => ({ value: s.search_id, label: `Search of ${shortTime(s.created_at)} · ${statusLabel(s.status)} · ${s.n_evaluated ?? 0} evaluated` }))
    : (strategies.data ?? []).map((s) => ({ value: s.strategy_id, label: `${strategyLabel(s.name)}${s.timeframe ? ` · ${facetLabel("timeframe", s.timeframe)}` : ""}${s.created_at ? ` · ${shortTime(s.created_at)}` : ""}` }));
  return (
    <Card title="Source">
      <div className="grid3">
        <Field label="Compare runs of"><Select value={kind} onChange={(v) => { setKind(v); setPick(""); }} options={SOURCES} testId="cmp-source" /></Field>
        <Field label="Choose"><Select value={pick} onChange={setPick} options={opts} placeholder="Choose…" testId="cmp-id" /></Field>
        <div className="actions"><Button kind="primary" disabled={!pick} onClick={() => go(`/compare?source=${kind}&id=${pick}`)} testId="cmp-go">Compare</Button></div>
      </div>
      {source === "runs" && <><p className="small">Showing {id.split(",").filter(Boolean).length} selected runs.</p>
        <TechDetails rows={[["Run ids", <Mono>{id}</Mono>]]} /></>}
      {source !== "runs" && pick && <TechDetails rows={[["Selected id", <Mono>{pick}</Mono>]]} />}
    </Card>
  );
}

function CompareResults({ source, id }: { source: Source; id: string }) {
  const { data, error } = useApi<Comparison>(`/api/compare?source=${source}&id=${encodeURIComponent(id)}`, [source, id]);
  const [sel, setSel] = useState<string[]>([]);
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <Loading label="Loading runs…" />;
  const one = sel.length === 1 ? data.rows.find((r) => r.run_id === sel[0]) : undefined;
  return (
    <Card title={`Runs (${data.n_runs})`} testId="compare-results">
      {data.labels.map((l) => <Banner key={l} tone={l.startsWith("SYNTHETIC") ? "demo" : "warn"}>{plainText(l)}</Banner>)}
      {!data.rows.length ? <Empty>No stored runs for this source yet. Run a backtest or a research job first.</Empty> : <>
        <CompareTable rows={data.rows} selected={sel} onSelect={setSel} />
        <div className="actions" data-testid="cmp-actions">
          <span className="muted small">{sel.length} selected</span>
          <Button small disabled={sel.length < 2} onClick={() => go(`/compare?source=runs&id=${sel.join(",")}`)}>Compare only selected</Button>
          <Button small disabled={!one} testId="cmp-validate"
            onClick={() => one && go(`/strategies/${one.strategy_id}?tab=validate&dataset=${one.parent_dataset_id ?? one.dataset_id}`)}>
            Validate selected (out-of-sample · walk-forward · control)</Button>
          <Button small disabled={!one} onClick={() => one && go(`/prop?run=${one.run_id}`)} testId="cmp-prop">Prop simulation on selected run</Button>
        </div>
      </>}
    </Card>
  );
}
