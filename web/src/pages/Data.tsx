import { useState } from "react";
import { api, ApiError } from "../api/client";
import type { DatasetRow, GapReport, InstrumentIdentity, PreferredDataset, WorkspaceState } from "../api/types";
import { ChooseWorkspaceLink, WorkspacePanel } from "../components/workspace";
import { href, useRoute } from "../app/router";
import { useApi, useApp } from "../app/context";
import { SYNTHETIC_NOTICE } from "../components/strategy";
import { Badge, Banner, Button, Card, Empty, ErrorPanel, Field, KeyValues, Loading, Mono, Select, TableWrap, TextInput, fmt, shortTime } from "../components/ui";
import type { ProtocolRecordRow } from "../api/types";
import { UI_VERSION, UpdatePanel, useVersion } from "../components/updates";
import { SystemPanel } from "./Strategies";

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
        ["Build", v ? (v.packaged ? <>packaged · build <Mono>{v.build_id}</Mono></> : "development (running from source)") : "…"],
        ["Build date", v?.built_at ? shortTime(v.built_at) : build?.built_at ? `UI ${shortTime(build.built_at)}` : "—"],
        ["Commit", <Mono>{(v?.git_commit ?? s?.code_version?.git_commit ?? "—").slice(0, 12)}{s?.code_version?.dirty ? " (modified)" : ""}</Mono>],
        ["Architecture", <span className="small">Python engine + Flask API (loopback only) · React UI{v?.packaged ? " · PyInstaller folder app with native WebView2 window" : ""}</span>],
        ["Workspace", ws.data?.current ? <Mono>{ws.data.current.path}</Mono> : <Badge tone="warn">none selected</Badge>],
        ["Data root", s ? <Mono>{String(s.root)}</Mono> : error ? <span className="warn">{error.message}</span> : "…"],
        ["Backend", s ? <Badge tone="ok">{String(s.backend)}</Badge> : error ? <Badge tone="error">unavailable</Badge> : "…"],
        ["Database", s ? `${s.store_backend} · ${s.runs} runs · ${s.datasets} datasets` : "—"],
        ["Research protocol", active.length ? active.map((p) => <span key={p.protocol_id}><Mono>{p.protocol_id}</Mono> <Badge tone="ok">ACTIVE</Badge> v{p.protocol_version}</span>)
          : <span className="muted">no ACTIVE protocol</span>],
        ["Settings file", ws.data?.settings_path ? <Mono>{ws.data.settings_path}</Mono> : "—"]]} />
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
      <div><span className="muted small">Research Dataset</span><Mono>{row.dataset_id}</Mono>{row.preferred && <> <Badge tone="info">preferred</Badge></>}</div>
      <div><span className="muted small">Provider</span>{row.provider}</div>
      <div><span className="muted small">Instrument</span>{row.instrument}
        {id?.identity_status === "provisional" && <> <Badge tone="warn" title={id.problem ?? ""}>provisional identity</Badge></>}
        {id?.research_proxy && <> <Badge tone="neutral">research proxy</Badge></>}</div>
      <div><span className="muted small">Timeframe</span>{row.timeframe}</div>
      <div><span className="muted small">Costs</span><Badge tone={row.cost.status === "unconfigured" ? "error" : "neutral"}>{row.cost.status}</Badge></div>
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
      {data?.state === "missing" && <Banner tone="warn">The preferred dataset {data.preferred?.dataset_id} is no longer stored.</Banner>}
      {row ? <ResearchDatasetStrip row={{ ...row, preferred: true }} testId="preferred-strip" />
        : <p className="muted small" data-testid="preferred-none">None set. Choose one below ("Set preferred"): only datasets that passed validation are eligible.</p>}
      {row && !row.runnable && <Banner tone="warn" testId="preferred-not-runnable">Research on this dataset is refused until: {row.reasons.join("; ")}.</Banner>}
      <p className="muted small wrap-any">The Strategy Lab and AI Discovery preselect it for NEW research. Changing it never alters stored runs, strategies or
        datasets. Stored in this workspace ({data?.stored_at ?? "workspace_preferences.json"}).</p>
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
    try { await api.post("/api/preferences/research-dataset", { dataset_id: id }); toast("ok", `Preferred research dataset: ${id}`); reload(); setTick((t) => t + 1); }
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
          <thead><tr><th>Dataset</th><th>Provider</th><th>Instrument</th><th>Identity</th><th>TF</th><th>Range</th><th>Bars</th>
            <th>Validation</th><th>Source hash</th><th>Price / volume</th><th>Costs</th><th>Eligible</th><th /></tr></thead>
          <tbody>{data.map((d) => (
            <tr key={d.dataset_id} data-testid={`dsrow-${d.dataset_id}`}>
              <td><Mono>{d.dataset_id}</Mono>{d.synthetic && <> <Badge tone="demo">synthetic</Badge></>}
                {d.preferred && <> <Badge tone="info">preferred</Badge></>}
                {d.parent_dataset_id && <div className="small muted">derived from <Mono>{d.parent_dataset_id}</Mono></div>}</td>
              <td>{d.provider}</td><td>{d.instrument}<div className="small muted">{d.asset_type}</div></td>
              <td data-testid={`identity-${d.dataset_id}`}>
                {d.identity?.identity_status === "provisional" ? <Badge tone="warn" title={d.identity.problem ?? ""}>provisional</Badge>
                  : <Badge tone="neutral">{d.identity?.identity_status ?? "?"}</Badge>}
                {d.identity?.research_proxy && <div className="small muted">research proxy</div>}
                {d.identity?.calendar_status === "provisional_unverified" && <div className="small muted">calendar unverified</div>}</td>
              <td>{d.timeframe}</td>
              <td className="small">{d.start?.slice(0, 10)} → {d.end?.slice(0, 10)}</td><td>{fmt(d.n_bars)}</td>
              <td><Badge tone={qTone(d.quality_status)}>{d.quality_status}</Badge>
                {d.missing_bars ? <div className="small muted">{fmt(d.missing_bars)} missing</div> : null}</td>
              <td className="small"><Mono>{d.content_hash?.slice(0, 12)}</Mono></td>
              <td className="small">{d.price_basis} · vol {d.volume_type}{d.has_spread ? " · spread" : ""}</td>
              <td><Badge tone={d.cost.status === "unconfigured" ? "error" : "neutral"} title={d.cost.reason}>{d.cost.status}</Badge></td>
              <td data-testid={`eligible-${d.dataset_id}`}>{d.runnable ? <Badge tone="ok">eligible</Badge>
                : <Badge tone="error" title={d.reasons.join("; ")}>not eligible</Badge>}
                {!d.runnable && <div className="small muted">{d.reasons.join("; ")}</div>}
                {d.limitations?.length ? <details><summary className="small">caveats ({d.limitations.length})</summary>
                  {d.limitations.map((l) => <div key={l} className="small">{l}</div>)}</details> : null}</td>
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
    <Card title={<>Metadata — <Mono>{id}</Mono></>} testId="dataset-detail">
      {data.limitations.map((l) => <Banner key={l} tone={l.startsWith("SYNTHETIC") ? "demo" : "warn"}>{l}</Banner>)}
      {idn?.identity_status === "provisional" && <Banner tone="warn" testId="identity-provisional"><b>Provisional source identity.</b> {idn.problem}
        {idn.missing_metadata?.length ? <> Missing: {idn.missing_metadata.join(", ")}.</> : null}</Banner>}
      {idn?.calendar_status === "provisional_unverified" && <Banner tone="warn" testId="calendar-unverified"><b>Session calendar not
        verified.</b> {idn.problem}</Banner>}
      <KeyValues rows={[["Instrument identity", <>{idn?.identity_status}{idn?.research_proxy ? " · research proxy" : ""}</>],
        ["Source provider / symbol", `${idn?.source_provider ?? "—"} / ${idn?.source_symbol ?? "not stated"}${idn?.source_feed_code ? ` (feed ${idn.source_feed_code})` : ""}`],
        ["Asset class", idn?.asset_class ?? "—"], ["Price basis", idn?.price_basis ?? "—"], ["Price source", idn?.price_source ?? "—"],
        ["Volume", idn?.volume_semantics ?? "—"],
        ["Calendar", `${idn?.calendar ?? "—"}${idn?.calendar_status ? ` (${idn.calendar_status})` : ""}`],
        ["Point value / tick", `${idn?.point_value ?? "?"} / ${idn?.tick_size ?? "?"} (${idn?.economics === "research_units" ? "research units, not contract economics" : "research units unless verified"})`]]} />
      <KeyValues rows={Object.entries(data.manifest).filter(([, v]) => typeof v !== "object" || v === null)
        .map(([k, v]) => [k, <span className="mono small">{fmt(v)}</span>])} />
      <KeyValues rows={[["Manifest hash", <Mono>{data.manifest_hash}</Mono>], ["Derived datasets", data.derived_datasets.join(", ") || "none"]]} />
      {checks.length > 0 && <TableWrap><table>
        <thead><tr><th>Validation check</th><th>Status</th><th>Count</th><th>Detail</th></tr></thead>
        <tbody>{checks.map((c) => <tr key={c.name}><td>{c.name}</td><td><Badge tone={qTone(c.status)}>{c.status}</Badge></td>
          <td>{fmt(c.count)}</td><td className="small">{c.detail ?? c.message}</td></tr>)}</tbody>
      </table></TableWrap>}
      <div className="actions"><Button onClick={loadGaps} busy={busy} busyLabel="Classifying gaps…" testId="load-gaps">Gap classification &amp; coverage</Button></div>
      <ErrorPanel error={gErr} />
      {gaps && <div data-testid="gap-report">
        <KeyValues rows={[["Calendar", gaps.calendar], ["Expected / present bars", `${fmt(gaps.summary.expected_bars)} / ${fmt(gaps.summary.present_in_session)}`],
          ["Missing", `${fmt(gaps.summary.missing_bars)} (${(gaps.summary.missing_ratio * 100).toFixed(3)}%) in ${gaps.summary.n_gaps} gaps`],
          ["By length", JSON.stringify(gaps.summary.by_length)], ["By position", JSON.stringify(gaps.summary.by_position)],
          ["Trading days", `${gaps.coverage.trading_days_with_bars} of ${gaps.coverage.expected_trading_days} have bars`],
          ["Missing trading days", gaps.coverage.missing_trading_days.slice(0, 20).join(", ") || "none"]]} />
        {gaps.largest_gaps.length > 0 && <TableWrap><table>
          <thead><tr><th>Start (UTC)</th><th>Bars</th><th>Trading date</th><th>Class</th><th>Position</th><th>Likely</th></tr></thead>
          <tbody>{gaps.largest_gaps.slice(0, 25).map((g) => <tr key={g.start}><td className="small">{g.start}</td><td>{g.missing_bars}</td>
            <td>{g.trading_date} {g.weekday}</td><td>{g.length_class}</td><td>{g.position}</td><td className="small">{g.likely}</td></tr>)}</tbody>
        </table></TableWrap>}
        <p className="muted small">{gaps.note}</p>
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
        <Field label="Asset type" hint="unspecified when the source does not state it"><Select value={o.asset_type} onChange={set("asset_type")}
          options={["FUTURE", "CFD", "INDEX", "unspecified", "SYNTHETIC"]} /></Field>
        <Field label="Timeframe of the file"><TextInput value={o.timeframe} onChange={set("timeframe")} /></Field>
        <Field label="Source timezone" hint="IANA zone, or IANA+Nh for broker server time"><TextInput value={o.source_timezone} onChange={set("source_timezone")} /></Field>
        <Field label="Layout profile"><Select value={o.profile} onChange={set("profile")} options={cfg?.import_profiles ?? ["generic_csv"]} /></Field>
        <Field label="Price basis"><Select value={o.price_basis} onChange={set("price_basis")} options={["unknown", "bid", "ask", "mid", "last"]} /></Field>
        <Field label="Dataset name" hint="optional, e.g. NQ_DUKASCOPY_2021_2026"><TextInput value={o.dataset_name ?? ""} onChange={set("dataset_name")} testId="import-name" /></Field>
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
      {inspect && <details open><summary>Inspection (nothing stored)</summary><pre className="code">{JSON.stringify(inspect, null, 2)}</pre></details>}
      {res && <Banner tone="ok"><b>Imported</b> <Mono>{String(res.dataset_id)}</Mono> — validation {String(res.quality_status ?? "")}</Banner>}
    </Card>
  );
}

// =========================================================================== settings
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
      {wsCard}
      <h3>System status</h3>
      <SystemPanel />
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
