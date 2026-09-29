import { useState } from "react";
import { api, ApiError } from "../api/client";
import type { DatasetRow } from "../api/types";
import { useRoute } from "../app/router";
import { useApi, useApp } from "../app/context";
import { SYNTHETIC_NOTICE } from "../components/strategy";
import { Badge, Banner, Button, Card, Empty, ErrorPanel, Field, KeyValues, Loading, Mono, Select, TableWrap, TextInput, fmt } from "../components/ui";

interface ConfigView {
  demo: boolean; root: string; config_hash: string; backtest: Record<string, unknown>;
  sessions: Record<string, { name: string; timezone: string; start: string; end: string }>;
  instruments: Record<string, { asset_class: string; tick_size: number; point_value: number; calendar: string }>;
  cost_profiles: Record<string, { status: string; profile?: string; reason?: string }>;
  import_profiles: string[]; web: { host: string; port: number; import_dirs: string[]; builder_timeframes: string[] };
  credentials: string;
}

// =========================================================================== datasets
export function DatasetsPage() {
  const route = useRoute();
  const { data, error, loading, reload } = useApi<DatasetRow[]>("/api/datasets");
  const [open, setOpen] = useState<string | null>(null);
  const [showImport, setShowImport] = useState(route.query.get("import") === "1");
  if (error) return <ErrorPanel error={error} />;
  return (
    <div className="page">
      <header className="page-head"><h1>Datasets</h1>
        <div className="actions"><Button kind="primary" onClick={() => setShowImport(!showImport)} testId="toggle-import">
          {showImport ? "Close import" : "Import Dataset"}</Button></div></header>
      {showImport && <ImportPanel onDone={() => { reload(); }} />}
      {loading && !data ? <Loading label="Loading datasets…" /> : !data?.length ? (
        <Empty>No datasets. Import one (existing Phase 2 pipeline) — EdgeLab never fabricates market data.</Empty>
      ) : (
        <TableWrap testId="datasets-table"><table>
          <thead><tr><th>Dataset</th><th>Instrument</th><th>Asset</th><th>Provider</th><th>TF</th><th>Start</th><th>End</th><th>Bars</th>
            <th>Validation</th><th>Price basis</th><th>Spread</th><th>Costs</th><th>Eligible</th><th /></tr></thead>
          <tbody>{data.map((d) => (
            <tr key={d.dataset_id}>
              <td><Mono>{d.dataset_id}</Mono>{d.synthetic && <> <Badge tone="demo">synthetic</Badge></>}</td>
              <td>{d.instrument}</td><td>{d.asset_type}</td><td>{d.provider}</td><td>{d.timeframe}</td>
              <td className="small">{d.start?.slice(0, 16)}</td><td className="small">{d.end?.slice(0, 16)}</td><td>{fmt(d.n_bars)}</td>
              <td><Badge tone={d.quality_status === "FAIL" ? "error" : d.quality_status === "WARN" ? "warn" : "ok"}>{d.quality_status}</Badge></td>
              <td>{d.price_basis}</td><td>{d.has_spread ? "per bar" : "none"}</td>
              <td><Badge tone={d.cost.status === "unconfigured" ? "error" : "neutral"} title={d.cost.reason}>{d.cost.status}</Badge></td>
              <td data-testid={`eligible-${d.dataset_id}`}>{d.runnable ? <Badge tone="ok">eligible</Badge>
                : <Badge tone="error" title={d.reasons.join("; ")}>not eligible</Badge>}
                {!d.runnable && <div className="small muted">{d.reasons.join("; ")}</div>}</td>
              <td className="row-actions"><button className="linklike" onClick={() => setOpen(open === d.dataset_id ? null : d.dataset_id)}
                data-testid={`inspect-${d.dataset_id}`}>{open === d.dataset_id ? "Close" : "Inspect"}</button></td>
            </tr>))}
          </tbody>
        </table></TableWrap>)}
      {open && <DatasetDetail id={open} />}
      <p className="muted small">Eligible = the stored dataset passed validation and has a configured cost profile (the strategy
        timeframe is checked when a backtest or search is set up). Source files refused at import (e.g. failed coverage) never
        become stored datasets; their reasons are in the import report. Inspect a dataset for its caveats.</p>
      <p className="muted small">Delete/archive: not supported — stored datasets are immutable and content-addressed; research runs reference them.</p>
    </div>
  );
}

function DatasetDetail({ id }: { id: string }) {
  const { data, error } = useApi<{ manifest: Record<string, unknown>; manifest_hash: string; validation_report: Record<string, any> | null;
    derived_datasets: string[]; limitations: string[] }>(`/api/datasets/${id}`, [id]);
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <Loading label="Loading metadata…" />;
  const checks = (data.validation_report?.checks ?? []) as { name: string; status: string; count?: number; detail?: string }[];
  return (
    <Card title={<>Metadata — <Mono>{id}</Mono></>} testId="dataset-detail">
      {data.limitations.map((l) => <Banner key={l} tone={l.startsWith("SYNTHETIC") ? "demo" : "warn"}>{l}</Banner>)}
      <KeyValues rows={Object.entries(data.manifest).filter(([, v]) => typeof v !== "object" || v === null)
        .map(([k, v]) => [k, <span className="mono small">{fmt(v)}</span>])} />
      <KeyValues rows={[["Manifest hash", <Mono>{data.manifest_hash}</Mono>], ["Derived datasets", data.derived_datasets.join(", ") || "none"]]} />
      {checks.length > 0 && <TableWrap><table>
        <thead><tr><th>Validation check</th><th>Status</th><th>Count</th><th>Detail</th></tr></thead>
        <tbody>{checks.map((c) => <tr key={c.name}><td>{c.name}</td><td><Badge tone={c.status === "FAIL" ? "error" : c.status === "WARN" ? "warn" : "ok"}>{c.status}</Badge></td>
          <td>{fmt(c.count)}</td><td className="small">{c.detail}</td></tr>)}</tbody>
      </table></TableWrap>}
    </Card>
  );
}

function ImportPanel({ onDone }: { onDone: () => void }) {
  const { toast } = useApp();
  const { data: files, error } = useApi<{ import_dirs: string[]; files: { path: string; bytes: number }[] }>("/api/import/files");
  const { data: cfg } = useApi<ConfigView>("/api/config");
  const [path, setPath] = useState("");
  const [o, setO] = useState<Record<string, string>>({ instrument: "", provider: "", asset_type: "FUTURE", timeframe: "1m",
    source_timezone: "UTC", profile: "generic_csv", price_basis: "unknown" });
  const [busy, setBusy] = useState<"" | "inspect" | "import">("");
  const [inspect, setInspect] = useState<Record<string, unknown> | null>(null);
  const [res, setRes] = useState<Record<string, unknown> | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const set = (k: string) => (v: string) => setO((x) => ({ ...x, [k]: v }));
  const run = async (what: "inspect" | "import") => {
    setBusy(what); setErr(null);
    try {
      const options = Object.fromEntries(Object.entries(o).filter(([, v]) => v !== ""));
      const r = await api.post<Record<string, unknown>>(what === "inspect" ? "/api/import/inspect" : "/api/import", { path, options });
      if (what === "inspect") setInspect(r); else { setRes(r); toast("ok", `Imported ${String(r.dataset_id)}`); onDone(); }
    } catch (e) { setErr(e as ApiError); } finally { setBusy(""); }
  };
  return (
    <Card title="Import a dataset (existing Phase 2 import pipeline)" testId="import-panel">
      <ErrorPanel error={error} />
      <p className="muted small">Files are read only from the configured import folder(s): <b>{files?.import_dirs.join(", ")}</b> (relative to the
        EdgeLab root). Copy a CSV there, then pick it below. Provider, timezone and asset type must describe the real file — nothing is assumed.</p>
      <div className="grid3">
        <Field label="File">
          <Select value={path} onChange={setPath} testId="import-file" placeholder={files?.files.length ? "choose a file…" : "no files in the import folder"}
            options={(files?.files ?? []).map((f) => ({ value: f.path, label: `${f.path} (${Math.round(f.bytes / 1024)} KB)` }))} />
        </Field>
        <Field label="Instrument"><Select value={o.instrument} onChange={set("instrument")} placeholder="choose…"
          options={Object.keys(cfg?.instruments ?? {})} testId="import-instrument" /></Field>
        <Field label="Provider"><TextInput value={o.provider} onChange={set("provider")} placeholder="e.g. DUKASCOPY" testId="import-provider" /></Field>
        <Field label="Asset type"><Select value={o.asset_type} onChange={set("asset_type")} options={["FUTURE", "CFD", "INDEX", "SYNTHETIC"]} /></Field>
        <Field label="Timeframe of the file"><TextInput value={o.timeframe} onChange={set("timeframe")} /></Field>
        <Field label="Source timezone" hint="IANA zone, or IANA+Nh for broker server time"><TextInput value={o.source_timezone} onChange={set("source_timezone")} /></Field>
        <Field label="Layout profile"><Select value={o.profile} onChange={set("profile")} options={cfg?.import_profiles ?? ["generic_csv"]} /></Field>
        <Field label="Price basis"><Select value={o.price_basis} onChange={set("price_basis")} options={["unknown", "bid", "ask", "mid", "last"]} /></Field>
      </div>
      <div className="inline">
        <Button onClick={() => run("inspect")} busy={busy === "inspect"} busyLabel="Inspecting…" disabled={!path}>Inspect file</Button>
        <Button kind="primary" onClick={() => run("import")} busy={busy === "import"} busyLabel="Importing & validating…"
          disabled={!path || !o.instrument || !o.provider}>Import</Button>
      </div>
      <ErrorPanel error={err} title="Import refused" />
      {inspect && <details open><summary>Inspection (nothing stored)</summary><pre className="code">{JSON.stringify(inspect, null, 2)}</pre></details>}
      {res && <Banner tone="ok"><b>Imported</b> <Mono>{String(res.dataset_id)}</Mono> — validation {String(res.quality_status ?? "")}</Banner>}
    </Card>
  );
}

// =========================================================================== placeholders
export function DiscoveryPage() {
  return (
    <div className="page" data-testid="discovery-page">
      <header className="page-head"><h1>AI Strategy Discovery</h1><Badge tone="info">planned · later phase</Badge></header>
      <Banner tone="info">Coming in a later phase. No AI model is connected, and none is called anywhere in EdgeLab.</Banner>
      <Card title="How AI proposals will be handled">
        <div className="flow">
          <div className="flow-step">AI proposal<span>data only</span></div><div className="flow-arrow">→</div>
          <div className="flow-step done">DSL validation<span>exists (Phase 3)</span></div><div className="flow-arrow">→</div>
          <div className="flow-step done">Compiler<span>exists (Phase 3)</span></div><div className="flow-arrow">→</div>
          <div className="flow-step">Numerical backtest<span>engine measures; AI never judges</span></div>
        </div>
        <p className="muted small">The Phase 3 Mode B gate already rejects proposals that carry performance fields or claim language
          (CLI: <code>python -m edgelab.cli strategy proposals FILE</code>).</p>
      </Card>
    </div>
  );
}

// =========================================================================== settings
export function SettingsPage() {
  const { data: c, error } = useApi<ConfigView>("/api/config");
  if (error) return <ErrorPanel error={error} />;
  if (!c) return <Loading label="Loading configuration…" />;
  return (
    <div className="page">
      <header className="page-head"><h1>Settings</h1></header>
      <Banner tone="info">Read-only view. Configuration lives in <code>configs/*.yaml</code> (see CONFIG.md); edit the files and restart.
        {" "}{c.credentials}.</Banner>
      <div className="grid-cards">
        <Card title="Workspace"><KeyValues rows={[["Mode", c.demo ? <Badge tone="demo">demo</Badge> : "research"], ["Root", <Mono>{c.root}</Mono>],
          ["Config hash", <Mono>{c.config_hash}</Mono>], ["Web", `${c.web.host}:${c.web.port}`], ["Import folders", c.web.import_dirs.join(", ")],
          ["Builder timeframes", c.web.builder_timeframes.join(", ")]]} /></Card>
        <Card title="Cost profiles">
          <TableWrap><table><thead><tr><th>Instrument</th><th>Status</th><th>Note</th></tr></thead>
            <tbody>{Object.entries(c.cost_profiles).map(([k, v]) => (
              <tr key={k}><td>{k}</td><td><Badge tone={v.status === "unconfigured" ? "error" : "neutral"}>{v.status}</Badge></td>
                <td className="small">{v.reason ?? v.profile ?? ""}</td></tr>))}</tbody></table></TableWrap>
          <p className="muted small">Unconfigured profiles block backtests. EdgeLab never invents broker spreads, commissions or slippage.</p>
        </Card>
        <Card title="Backtest engine"><pre className="code">{JSON.stringify(c.backtest, null, 2)}</pre></Card>
        <Card title="Sessions"><TableWrap><table><thead><tr><th>Name</th><th>Timezone</th><th>Start</th><th>End</th></tr></thead>
          <tbody>{Object.values(c.sessions).map((s) => <tr key={s.name}><td>{s.name}</td><td>{s.timezone}</td><td>{s.start}</td><td>{s.end}</td></tr>)}</tbody>
        </table></TableWrap></Card>
        <Card title="Instruments"><TableWrap><table><thead><tr><th>Symbol</th><th>Class</th><th>Tick</th><th>Point value</th><th>Calendar</th></tr></thead>
          <tbody>{Object.entries(c.instruments).map(([k, v]) => <tr key={k}><td>{k}</td><td>{v.asset_class}</td><td>{v.tick_size}</td><td>{v.point_value}</td><td>{v.calendar}</td></tr>)}</tbody>
        </table></TableWrap></Card>
      </div>
      {c.demo && <p className="muted small">{SYNTHETIC_NOTICE}</p>}
    </div>
  );
}
