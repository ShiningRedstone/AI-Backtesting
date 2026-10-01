import { useState, type ReactNode } from "react";
import { api, ApiError } from "../api/client";
import type { DatasetRow, GapReport, InstrumentIdentity, PreferredDataset, RiskPreference, WorkspaceState } from "../api/types";
import { ChooseWorkspaceLink, WorkspacePanel } from "../components/workspace";
import { href, useRoute } from "../app/router";
import { useApi, useApp } from "../app/context";
import { datasetLabel, facetLabel, humanize, plainProse, statusLabel, valueLabel } from "../app/labels";
import { SYNTHETIC_NOTICE } from "../components/strategy";
import { Badge, Banner, Button, Card, Empty, ErrorPanel, Field, KeyValues, Loading, Mono, ObjectView, Select, TableWrap, TechDetails, TextInput, fmt, shortTime } from "../components/ui";
import type { ProtocolRecordRow } from "../api/types";
import { UI_VERSION, UpdatePanel, useVersion } from "../components/updates";
import { SystemPanel } from "./Strategies";

/** Backend prose that mentions config keys ("spread_source", "evaluation.drawdown.mode") with the keys as words. */
const prose = plainProse;
/** IANA time zone without underscores ("America/New_York" -> "America/New York"). */
const tzLabel = (z: unknown) => (z ? String(z).replace(/_/g, " ") : "—");
/** Calendar / instrument ids as words, exchange names kept upper case ("CME_EQUITY" -> "CME equity"). */
const idWords = (v: unknown) => humanize(v).replace(/^(Cme|Cbot|Nymex|Comex|Eurex|Ice)\b/, (m) => m.toUpperCase());
const qLabel = (q: string | null | undefined) => (q === "WARN" ? "Passed with warnings" : statusLabel(q));
const storeLabel = (b: unknown) => (b === "sqlite" ? "SQLite" : b === "duckdb" ? "DuckDB" : humanize(b));
const countsLabel = (o: Record<string, number>) => Object.entries(o).map(([k, v]) => `${humanize(k)}: ${v}`).join(", ") || "none";

// =========================================================================== about
/* eslint-disable @typescript-eslint/no-explicit-any */
function AboutCard() {
  const { data: v } = useVersion();
  const { data: s, error } = useApi<Record<string, any>>("/api/status");
  const ws = useApi<WorkspaceState>("/api/workspace");
  const protos = useApi<ProtocolRecordRow[]>("/api/protocols");
  const { data: build } = useApi<{ built_at: string; react: string; app_version?: string }>("/build-info.json");
  const active = (protos.data ?? []).filter((p) => p.status === "ACTIVE");
  return (
    <Card title="About EdgeLab" testId="about">
      <KeyValues rows={[
        ["EdgeLab version", <><b data-testid="about-version">{v?.version ?? "…"}</b>{v && v.version !== UI_VERSION &&
          <Badge tone="error">UI bundle is {UI_VERSION}: rebuild the frontend</Badge>}</>],
        ["Build", v ? (v.packaged ? "packaged desktop build" : "development (running from source)") : "…"],
        ["Build date", v?.built_at ? shortTime(v.built_at) : build?.built_at ? `UI ${shortTime(build.built_at)}` : "—"],
        ["Source code", s?.code_version?.dirty ? "modified since the last commit" : "as committed"],
        ["Architecture", <span className="small">Python engine + Flask API (loopback only) · React UI{v?.packaged ? " · PyInstaller folder app with native WebView2 window" : ""}</span>],
        ["Workspace", ws.data?.current ? `folder “${ws.data.current.path.split(/[\\/]/).filter(Boolean).pop() ?? ws.data.current.path}”` : <Badge tone="warn">none selected</Badge>],
        ...(error ? [["System status", <span className="warn">{error.message}</span>] as [string, ReactNode]] : []),
        ["Backend", s ? <Badge tone="ok">{s.backend === "ok" ? "running" : valueLabel(s.backend)}</Badge> : error ? <Badge tone="error">unavailable</Badge> : "…"],
        ["Database", s ? `${storeLabel(s.store_backend)} · ${s.runs} runs · ${s.datasets} datasets` : "—"],
        ["Research protocol", active.length ? active.map((p) => <span key={p.protocol_id}>{p.name ? `${p.name} ` : ""}<Badge tone="ok">active</Badge> version {p.protocol_version}</span>)
          : <span className="muted">no active protocol</span>]]} />
      <TechDetails rows={[["Build id", v?.packaged ? <Mono>{v.build_id}</Mono> : null],
        ["Commit", <Mono>{v?.git_commit ?? s?.code_version?.git_commit ?? "—"}</Mono>],
        ["Workspace", ws.data?.current ? <Mono>{ws.data.current.path}</Mono> : null], ["Data root", s ? <Mono>{String(s.root)}</Mono> : null],
        ["Research protocol", active.length ? <Mono>{active.map((p) => p.protocol_id).join(", ")}</Mono> : null],
        ["Settings file", ws.data?.settings_path ? <Mono>{ws.data.settings_path}</Mono> : null]]} />
    </Card>
  );
}
/* eslint-enable @typescript-eslint/no-explicit-any */

interface ConfigView {
  demo: boolean; root: string; config_hash: string; backtest: Record<string, unknown>;
  sessions: Record<string, { name: string; timezone: string; start: string; end: string }>;
  instruments: Record<string, { asset_class: string; tick_size: number; point_value: number; calendar: string }>;
  cost_profiles: Record<string, { status: string; profile?: string; reason?: string }>;
  import_profiles: string[]; web: { host: string; port: number; import_dirs: string[]; builder_timeframes: string[] };
  credentials: string;
}

// =========================================================================== datasets
const qTone = (q: string) => (q === "FAIL" ? "error" : q === "WARN" ? "warn" : "ok");

/** "Research Dataset / Provider / Instrument / Timeframe" — the data identity new research will use. */
export function ResearchDatasetStrip({ row, testId = "research-dataset-strip" }: { row: DatasetRow | null | undefined; testId?: string }) {
  if (!row) return <Banner tone="warn" testId={testId}>No research dataset selected. Set a Preferred Research Dataset on the
    {" "}<a href={href("/datasets")}>Datasets</a> page.</Banner>;
  const id = row.identity;
  return (
    <div className="ds-strip" data-testid={testId}>
      <div><span className="muted small">Research Dataset</span>{datasetLabel(row.dataset_id)}{row.preferred && <> <Badge tone="info">preferred</Badge></>}</div>
      <div><span className="muted small">Provider</span>{humanize(row.provider)}</div>
      <div><span className="muted small">Instrument</span>{humanize(row.instrument)}
        {id?.identity_status === "provisional" && <> <Badge tone="warn" title={id.problem ?? ""}>provisional identity</Badge></>}
        {id?.research_proxy && <> <Badge tone="neutral">research proxy</Badge></>}</div>
      <div><span className="muted small">Timeframe</span>{facetLabel("timeframe", row.timeframe)}</div>
      <div><span className="muted small">Costs</span><Badge tone={row.cost.status === "unconfigured" ? "error" : "neutral"}>{valueLabel(row.cost.status)}</Badge></div>
      <TechDetails rows={[["Dataset", <Mono>{row.dataset_id}</Mono>], ["Provider / instrument", <Mono>{`${row.provider} / ${row.instrument}`}</Mono>]]} />
    </div>
  );
}

function PreferredCard({ rows, onChange }: { rows: DatasetRow[]; onChange: () => void }) {
  const { data, error, reload } = useApi<PreferredDataset>("/api/preferences/research-dataset");
  const [err, setErr] = useState<ApiError | null>(null);
  const clear = async () => {
    try { await api.post("/api/preferences/research-dataset", { dataset_id: null }); reload(); onChange(); }
    catch (e) { setErr(e as ApiError); }
  };
  if (error) return <ErrorPanel error={error} />;
  const row = rows.find((d) => d.dataset_id === data?.preferred?.dataset_id);
  return (
    <Card title="Preferred Research Dataset" testId="preferred-card"
      actions={data?.preferred ? <Button small onClick={clear} testId="preferred-clear">Clear</Button> : undefined}>
      {data?.state === "missing" && <Banner tone="warn">The preferred dataset {datasetLabel(data.preferred?.dataset_id)} is no longer stored.</Banner>}
      {row ? <ResearchDatasetStrip row={{ ...row, preferred: true }} testId="preferred-strip" />
        : <p className="muted small" data-testid="preferred-none">None set. Choose one below ("Set preferred"): only datasets that passed validation are eligible.</p>}
      {row && !row.runnable && <Banner tone="warn" testId="preferred-not-runnable">Research on this dataset is refused until: {row.reasons.map(prose).join("; ")}.</Banner>}
      <p className="muted small wrap-any">The Strategy Lab and AI Discovery preselect it for NEW research. Changing it never alters stored runs, strategies or
        datasets. Stored in this workspace's preferences file.</p>
      {data?.stored_at && <TechDetails rows={[["Preferences file", <Mono>{data.stored_at}</Mono>]]} />}
      <ErrorPanel error={err} />
    </Card>
  );
}

export function DatasetsPage() {
  const route = useRoute();
  const { toast } = useApp();
  const { data, error, loading, reload } = useApi<DatasetRow[]>("/api/datasets");
  const [open, setOpen] = useState<string | null>(null);
  const [showImport, setShowImport] = useState(route.query.get("import") === "1");
  const [err, setErr] = useState<ApiError | null>(null);
  const [tick, setTick] = useState(0);
  if (error) return <ErrorPanel error={error} />;
  const prefer = async (id: string) => {
    setErr(null);
    try { await api.post("/api/preferences/research-dataset", { dataset_id: id }); toast("ok", `Preferred research dataset: ${datasetLabel(id)}`); reload(); setTick((t) => t + 1); }
    catch (e) { setErr(e as ApiError); }
  };
  return (
    <div className="page">
      <header className="page-head"><h1>Datasets</h1>
        <div className="actions"><Button kind="primary" onClick={() => setShowImport(!showImport)} testId="toggle-import">
          {showImport ? "Close import" : "Import Dataset"}</Button></div></header>
      {showImport && <ImportPanel onDone={() => { reload(); }} />}
      {data && data.length > 0 && <PreferredCard key={tick} rows={data} onChange={() => { reload(); }} />}
      <ErrorPanel error={err} title="Not set as preferred" testId="preferred-error" />
      {loading && !data ? <Loading label="Loading datasets…" /> : !data?.length ? (
        <Empty><span data-testid="datasets-empty">No datasets in this workspace.</span> Open the research workspace that holds your datasets
          (<ChooseWorkspaceLink />), or import one into this workspace (existing Phase 2 pipeline). EdgeLab never fabricates market data.</Empty>
      ) : (
        <TableWrap testId="datasets-table"><table>
          <thead><tr><th>Dataset</th><th>Provider</th><th>Instrument</th><th>Identity</th><th>Timeframe</th><th>Range</th><th>Bars</th>
            <th>Validation</th><th>Price / volume</th><th>Costs</th><th>Eligible</th><th /></tr></thead>
          <tbody>{data.map((d) => (
            <tr key={d.dataset_id} data-testid={`dsrow-${d.dataset_id}`}>
              <td>{datasetLabel(d.dataset_id)}{d.synthetic && <> <Badge tone="demo">synthetic</Badge></>}
                {d.preferred && <> <Badge tone="info">preferred</Badge></>}
                {d.parent_dataset_id && <div className="small muted">derived from {datasetLabel(d.parent_dataset_id)}</div>}
                <TechDetails rows={[["Dataset", <Mono>{d.dataset_id}</Mono>], ["Source hash", <Mono>{d.content_hash}</Mono>],
                  ["Derived from", d.parent_dataset_id ? <Mono>{d.parent_dataset_id}</Mono> : null]]} /></td>
              <td>{humanize(d.provider)}</td><td>{humanize(d.instrument)}<div className="small muted">{humanize(d.asset_type)}</div></td>
              <td data-testid={`identity-${d.dataset_id}`}>
                {d.identity?.identity_status === "provisional" ? <Badge tone="warn" title={d.identity.problem ?? ""}>provisional</Badge>
                  : <Badge tone="neutral">{d.identity?.identity_status ? valueLabel(d.identity.identity_status) : "?"}</Badge>}
                {d.identity?.research_proxy && <div className="small muted">research proxy</div>}
                {d.identity?.calendar_status === "provisional_unverified" && <div className="small muted">calendar unverified</div>}</td>
              <td>{facetLabel("timeframe", d.timeframe)}</td>
              <td className="small">{d.start?.slice(0, 10)} → {d.end?.slice(0, 10)}</td><td>{fmt(d.n_bars)}</td>
              <td><Badge tone={qTone(d.quality_status)}>{qLabel(d.quality_status)}</Badge>
                {d.missing_bars ? <div className="small muted">{fmt(d.missing_bars)} missing</div> : null}</td>
              <td className="small">{humanize(d.price_basis)} prices · volume: {humanize(d.volume_type).toLowerCase()}{d.has_spread ? " · spread" : ""}</td>
              <td><Badge tone={d.cost.status === "unconfigured" ? "error" : "neutral"} title={d.cost.reason ? prose(d.cost.reason) : undefined}>{valueLabel(d.cost.status)}</Badge></td>
              <td data-testid={`eligible-${d.dataset_id}`}>{d.runnable ? <Badge tone="ok">eligible</Badge>
                : <Badge tone="error">not eligible</Badge>}
                {!d.runnable && <div className="small muted">{d.reasons.map(prose).join("; ")}</div>}
                {d.limitations?.length ? <details><summary className="small">caveats ({d.limitations.length})</summary>
                  {d.limitations.map((l) => <div key={l} className="small">{prose(l)}</div>)}</details> : null}</td>
              <td className="row-actions">
                <button className="linklike" onClick={() => setOpen(open === d.dataset_id ? null : d.dataset_id)}
                  data-testid={`inspect-${d.dataset_id}`}>{open === d.dataset_id ? "Close" : "Inspect"}</button>
                {!d.preferred && <button className="linklike" disabled={d.quality_status === "FAIL"} onClick={() => prefer(d.dataset_id)}
                  data-testid={`prefer-${d.dataset_id}`} title="Default for new research in the Strategy Lab and AI Discovery">Set preferred</button>}
              </td>
            </tr>))}
          </tbody>
        </table></TableWrap>)}
      {open && <DatasetDetail id={open} />}
      <p className="muted small">Eligible = the stored dataset passed validation, its instrument identity is established and it has a configured
        cost profile (the strategy timeframe is checked when a backtest or search is set up). A PROVISIONAL instrument identity (e.g. the Dukascopy
        source until you state its symbol and economics) imports and validates, but research on it is refused. Source files refused at import never
        become stored datasets; their reasons are in the import report.</p>
      <p className="muted small">Delete/archive: not supported — stored datasets are immutable and content-addressed; research runs reference them.</p>
    </div>
  );
}

/** The dataset manifest's descriptive fields in words; ids, hashes and file details stay under Technical details. */
const MANIFEST_HIDDEN = /(_id|hash|sha256|fingerprint|^source_detail|^calendar|^import_version)$|^(dataset_id|parent_dataset_id|source_detail|calendar)$/;
function manifestWords(m: Record<string, unknown>) {
  return Object.fromEntries(Object.entries(m).filter(([k, v]) => !MANIFEST_HIDDEN.test(k) && (typeof v !== "object" || v === null))
    .map(([k, v]) => [k, /timezone$/.test(k) ? tzLabel(v) : typeof v === "string" && /\s/.test(v) ? prose(v) : v]));
}

function DatasetDetail({ id }: { id: string }) {
  const { data, error } = useApi<{ manifest: Record<string, unknown>; manifest_hash: string; validation_report: Record<string, any> | null;
    derived_datasets: string[]; limitations: string[]; identity: InstrumentIdentity; preferred: boolean }>(`/api/datasets/${id}`, [id]);
  const [gaps, setGaps] = useState<GapReport | null>(null);
  const [gErr, setGErr] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <Loading label="Loading metadata…" />;
  const checks = (data.validation_report?.checks ?? []) as { name: string; status: string; count?: number; message?: string; detail?: string }[];
  const idn = data.identity;
  const loadGaps = async () => {
    setBusy(true); setGErr(null);
    try { setGaps(await api.get<GapReport>(`/api/datasets/${id}/quality`)); } catch (e) { setGErr(e as ApiError); } finally { setBusy(false); }
  };
  return (
    <Card title={<>Metadata — {datasetLabel(id)}</>} testId="dataset-detail">
      {data.limitations.map((l) => <Banner key={l} tone={l.startsWith("SYNTHETIC") ? "demo" : "warn"}>{prose(l)}</Banner>)}
      {idn?.identity_status === "provisional" && <Banner tone="warn" testId="identity-provisional"><b>Provisional source identity.</b> {prose(idn.problem)}
        {idn.missing_metadata?.length ? <> Missing: {idn.missing_metadata.map((m) => humanize(m).toLowerCase()).join(", ")}.</> : null}</Banner>}
      {idn?.calendar_status === "provisional_unverified" && <Banner tone="warn" testId="calendar-unverified"><b>Session calendar not
        verified.</b> {prose(idn.problem)}</Banner>}
      <KeyValues rows={[["Instrument identity", <>{valueLabel(idn?.identity_status)}{idn?.research_proxy ? " · research proxy" : ""}</>],
        ["Source provider / symbol", `${idn?.source_provider ? humanize(idn.source_provider) : "—"} / ${idn?.source_symbol ?? "not stated"}${idn?.source_feed_code ? " (feed code under Technical details)" : ""}`],
        ["Asset class", idn?.asset_class ? humanize(idn.asset_class) : "—"], ["Price basis", idn?.price_basis ? humanize(idn.price_basis) : "—"],
        ["Price source", idn?.price_source ? prose(idn.price_source) : "—"], ["Volume", idn?.volume_semantics ? prose(idn.volume_semantics) : "—"],
        ["Calendar", `${idn?.calendar ? idWords(idn.calendar) : "—"}${idn?.calendar_status ? ` (${valueLabel(idn.calendar_status).toLowerCase()})` : ""}`],
        ["Point value / tick", `${idn?.point_value ?? "?"} / ${idn?.tick_size ?? "?"} (${idn?.economics === "research_units" ? "research units, not contract economics" : "research units unless verified"})`]]} />
      <ObjectView value={manifestWords(data.manifest)} />
      <KeyValues rows={[["Derived datasets", data.derived_datasets.map(datasetLabel).join(", ") || "none"]]} />
      <TechDetails rows={[["Dataset", <Mono>{id}</Mono>], ["Manifest hash", <Mono>{data.manifest_hash}</Mono>],
        ["Feed code", idn?.source_feed_code ? <Mono>{idn.source_feed_code}</Mono> : null], ["Calendar", idn?.calendar ? <Mono>{idn.calendar}</Mono> : null],
        ["Derived datasets", data.derived_datasets.length ? <Mono>{data.derived_datasets.join(", ")}</Mono> : null]]}>
        <pre className="code">{JSON.stringify(data.manifest, null, 2)}</pre></TechDetails>
      {checks.length > 0 && <TableWrap><table>
        <thead><tr><th>Validation check</th><th>Status</th><th>Count</th><th>Detail</th></tr></thead>
        <tbody>{checks.map((c) => <tr key={c.name}><td>{humanize(c.name)}</td><td><Badge tone={qTone(c.status)}>{qLabel(c.status)}</Badge></td>
          <td>{fmt(c.count)}</td><td className="small">{prose(c.detail ?? c.message)}</td></tr>)}</tbody>
      </table></TableWrap>}
      <div className="actions"><Button onClick={loadGaps} busy={busy} busyLabel="Classifying gaps…" testId="load-gaps">Gap classification &amp; coverage</Button></div>
      <ErrorPanel error={gErr} />
      {gaps && <div data-testid="gap-report">
        <KeyValues rows={[["Calendar", idWords(gaps.calendar)], ["Expected / present bars", `${fmt(gaps.summary.expected_bars)} / ${fmt(gaps.summary.present_in_session)}`],
          ["Missing", `${fmt(gaps.summary.missing_bars)} (${(gaps.summary.missing_ratio * 100).toFixed(3)}%) in ${gaps.summary.n_gaps} gaps`],
          ["By length", countsLabel(gaps.summary.by_length)], ["By position", countsLabel(gaps.summary.by_position)],
          ["Trading days", `${gaps.coverage.trading_days_with_bars} of ${gaps.coverage.expected_trading_days} have bars`],
          ["Missing trading days", gaps.coverage.missing_trading_days.slice(0, 20).join(", ") || "none"]]} />
        {gaps.largest_gaps.length > 0 && <TableWrap><table>
          <thead><tr><th>Start (UTC)</th><th>Bars</th><th>Trading date</th><th>Class</th><th>Position</th><th>Likely</th></tr></thead>
          <tbody>{gaps.largest_gaps.slice(0, 25).map((g) => <tr key={g.start}><td className="small">{g.start}</td><td>{g.missing_bars}</td>
            <td>{g.trading_date} {g.weekday}</td><td>{humanize(g.length_class)}</td><td>{humanize(g.position)}</td><td className="small">{prose(g.likely)}</td></tr>)}</tbody>
        </table></TableWrap>}
        <p className="muted small">{prose(gaps.note)}</p>
      </div>}
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
      const { derive, ...rest } = o;
      const options: Record<string, unknown> = Object.fromEntries(Object.entries(rest).filter(([, v]) => v !== ""));
      if (derive?.trim()) options.derive_timeframes = derive.split(",").map((x) => x.trim()).filter(Boolean);
      const r = await api.post<Record<string, unknown>>(what === "inspect" ? "/api/import/inspect" : "/api/import", { path, options });
      if (what === "inspect") setInspect(r); else { setRes(r); toast("ok", `Imported ${datasetLabel(String(r.dataset_id))}`); onDone(); }
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
          options={Object.keys(cfg?.instruments ?? {}).map((k) => ({ value: k, label: humanize(k) }))} testId="import-instrument" /></Field>
        <Field label="Provider"><TextInput value={o.provider} onChange={set("provider")} placeholder="e.g. DUKASCOPY" testId="import-provider" /></Field>
        <Field label="Asset type" hint="unspecified when the source does not state it"><Select value={o.asset_type} onChange={set("asset_type")}
          options={["FUTURE", "CFD", "INDEX", "unspecified", "SYNTHETIC"].map((v) => ({ value: v, label: humanize(v) }))} /></Field>
        <Field label="Timeframe of the file"><TextInput value={o.timeframe} onChange={set("timeframe")} /></Field>
        <Field label="Source timezone" hint="IANA zone, or IANA+Nh for broker server time"><TextInput value={o.source_timezone} onChange={set("source_timezone")} /></Field>
        <Field label="Layout profile"><Select value={o.profile} onChange={set("profile")} options={(cfg?.import_profiles ?? ["generic_csv"]).map((v) => ({ value: v, label: humanize(v) }))} /></Field>
        <Field label="Price basis"><Select value={o.price_basis} onChange={set("price_basis")} options={["unknown", "bid", "ask", "mid", "last"].map((v) => ({ value: v, label: humanize(v) }))} /></Field>
        <Field label="Dataset name" hint="optional; by default the instrument and provider names"><TextInput value={o.dataset_name ?? ""} onChange={set("dataset_name")} testId="import-name" /></Field>
        <Field label="Source symbol" hint="only if you know it; never guessed"><TextInput value={o.symbol ?? ""} onChange={set("symbol")} /></Field>
        <Field label="Calendar" hint="default: the instrument's calendar"><TextInput value={o.calendar ?? ""} onChange={set("calendar")} /></Field>
        <Field label="Derive timeframes" hint="comma-separated, e.g. 5m"><TextInput value={o.derive ?? ""} onChange={set("derive")} testId="import-derive" /></Field>
      </div>
      <div className="inline">
        <Button onClick={() => run("inspect")} busy={busy === "inspect"} busyLabel="Inspecting…" disabled={!path}>Inspect file</Button>
        <Button kind="primary" onClick={() => run("import")} busy={busy === "import"} busyLabel="Importing & validating…"
          disabled={!path || !o.instrument || !o.provider}>Import</Button>
      </div>
      <ErrorPanel error={err} title="Import refused" />
      {inspect && <details open><summary>Inspection (nothing stored)</summary><ObjectView value={inspect} />
        <TechDetails><pre className="code">{JSON.stringify(inspect, null, 2)}</pre></TechDetails></details>}
      {res && <Banner tone="ok"><b>Imported</b> {datasetLabel(String(res.dataset_id))} — validation {qLabel(String(res.quality_status ?? "")).toLowerCase()}
        <TechDetails rows={[["Dataset", <Mono>{String(res.dataset_id)}</Mono>]]} /></Banner>}
    </Card>
  );
}

// =========================================================================== settings
/** Risk per trade ($): the dollar amount one R stands for in the results views (display only; backtests unchanged). */
function RiskPerTradeCard() {
  const { toast } = useApp();
  const { data, error, setData } = useApi<RiskPreference>("/api/preferences/risk-per-trade");
  const [val, setVal] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const shown = val ?? (data ? String(data.risk_per_trade_usd) : "");
  const save = () => {
    const x = Number(shown);
    setBusy(true);
    api.post<RiskPreference>("/api/preferences/risk-per-trade", { risk_per_trade_usd: x })
      .then((d) => { setData(d); setVal(null); toast("ok", `Risk per trade set to $${d.risk_per_trade_usd.toLocaleString()}`); })
      .catch((e: Error) => toast("error", e.message)).finally(() => setBusy(false));
  };
  return (
    <Card title="Risk per trade" testId="settings-risk">
      {error ? <ErrorPanel error={error} /> : !data ? <Loading label="Loading…" /> : <>
        <div className="inline" style={{ gap: 8 }}>
          <span className="muted">$</span>
          <input className="input" style={{ width: 120 }} inputMode="decimal" aria-label="risk per trade in dollars" value={shown}
            data-testid="risk-input" onChange={(e: { target: HTMLInputElement }) => setVal(e.target.value.replace(/[^0-9.]/g, ""))} />
          <Button small kind="primary" onClick={save} busy={busy} disabled={!shown || Number(shown) <= 0 || shown === String(data.risk_per_trade_usd)}
            testId="risk-save">Save</Button>
        </div>
        <p className="small muted">Dollar figures in Backtest results are results in R multiplied by this amount (default $250). It only changes
          how results are displayed: backtests, position sizing and stored results stay exactly as they are.</p></>}
    </Card>
  );
}

export function SettingsPage() {
  const { data: c, error } = useApi<ConfigView>("/api/config");
  const ws = useApi<WorkspaceState>("/api/workspace");
  const wsCard = <Card title="Research Workspace" testId="settings-workspace">
    {ws.error ? <ErrorPanel error={ws.error} /> : ws.data ? <WorkspacePanel state={ws.data} /> : <Loading label="Loading workspace…" />}</Card>;
  const about = <AboutCard />;
  if (error?.kind === "no_workspace") return <div className="page"><header className="page-head"><h1>Settings</h1></header>{about}<UpdatePanel />{wsCard}</div>;
  if (error) return <div className="page"><header className="page-head"><h1>Settings</h1></header>{about}<UpdatePanel />{wsCard}<ErrorPanel error={error} /></div>;
  if (!c) return <Loading label="Loading configuration…" />;
  return (
    <div className="page">
      <header className="page-head"><div><div className="eyebrow">System</div><h1>Settings</h1>
        <div className="subtitle small">About this build, application updates, the research workspace and the (read-only) configuration.</div></div></header>
      <div className="grid-cards">{about}<UpdatePanel /></div>
      <div className="grid-cards"><RiskPerTradeCard />{wsCard}</div>
      <h3>System status</h3>
      <SystemPanel />
      <Banner tone="info">Read-only view. Configuration lives in the YAML files of the workspace's configs folder (described in the configuration
        guide); edit the files and restart.
        {" "}{c.credentials}.</Banner>
      <div className="grid-cards">
        <Card title="Workspace"><KeyValues rows={[["Mode", c.demo ? <Badge tone="demo">demo</Badge> : "research"],
          ["Web address", `${c.web.host}:${c.web.port}`], ["Import folders", c.web.import_dirs.join(", ")],
          ["Builder timeframes", c.web.builder_timeframes.map((t) => facetLabel("timeframe", t)).join(", ")]]} />
          <TechDetails rows={[["Root", <Mono>{c.root}</Mono>], ["Config hash", <Mono>{c.config_hash}</Mono>]]} /></Card>
        <Card title="Cost profiles">
          <TableWrap><table><thead><tr><th>Instrument</th><th>Status</th><th>Note</th></tr></thead>
            <tbody>{Object.entries(c.cost_profiles).map(([k, v]) => (
              <tr key={k}><td>{humanize(k)}</td><td><Badge tone={v.status === "unconfigured" ? "error" : "neutral"}>{valueLabel(v.status)}</Badge></td>
                <td className="small">{v.reason ? prose(v.reason) : v.profile ? humanize(v.profile) : ""}</td></tr>))}</tbody></table></TableWrap>
          <p className="muted small">Unconfigured profiles block backtests. EdgeLab never invents broker spreads, commissions or slippage.</p>
          <TechDetails rows={Object.entries(c.cost_profiles).map(([k, v]) => [k, <Mono>{`${v.status}${v.profile ? ` · ${v.profile}` : ""}`}</Mono>])} />
        </Card>
        <Card title="Backtest engine"><ObjectView value={c.backtest} />
          <TechDetails><pre className="code">{JSON.stringify(c.backtest, null, 2)}</pre></TechDetails></Card>
        <Card title="Sessions"><TableWrap><table><thead><tr><th>Name</th><th>Time zone</th><th>Start</th><th>End</th></tr></thead>
          <tbody>{Object.values(c.sessions).map((s) => <tr key={s.name}><td>{facetLabel("session", s.name)}</td><td>{tzLabel(s.timezone)}</td><td>{s.start}</td><td>{s.end}</td></tr>)}</tbody>
        </table></TableWrap>
          <TechDetails rows={Object.values(c.sessions).map((s) => [facetLabel("session", s.name), <Mono>{`${s.name} · ${s.timezone}`}</Mono>])} /></Card>
        <Card title="Instruments"><TableWrap><table><thead><tr><th>Symbol</th><th>Class</th><th>Tick size</th><th>Point value</th><th>Calendar</th></tr></thead>
          <tbody>{Object.entries(c.instruments).map(([k, v]) => <tr key={k}><td>{humanize(k)}</td><td>{humanize(v.asset_class)}</td><td>{v.tick_size}</td><td>{v.point_value}</td><td>{idWords(v.calendar)}</td></tr>)}</tbody>
        </table></TableWrap>
          <TechDetails rows={Object.entries(c.instruments).map(([k, v]) => [humanize(k), <Mono>{`${k} · ${v.asset_class} · ${v.calendar}`}</Mono>])} /></Card>
      </div>
      {c.demo && <p className="muted small">{SYNTHETIC_NOTICE}</p>}
    </div>
  );
}
