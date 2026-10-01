import { useState } from "react";
import { api, ApiError } from "../api/client";
import type { WorkspaceInfo, WorkspaceState } from "../api/types";
import { href } from "../app/router";
import { humanize } from "../app/labels";
import { Badge, Banner, Button, Card, ErrorPanel, KeyValues, Mono, TechDetails, TextInput } from "./ui";

/** Research Workspace: which folder (configs/ + data/) EdgeLab works on. Choosing one is a pointer
 *  change: the backend validates it read-only and never copies, migrates, imports or deletes data. */
const storeLabel = (b: string) => (b === "sqlite" ? "SQLite store" : b === "duckdb" ? "DuckDB store" : `${humanize(b)} store`);
/** How the workspace was chosen ("--data-root", an environment variable, "saved selection") in words. */
const sourceLabel = (s: string) => (s.startsWith("--") ? "start-up option" : /^[A-Z0-9]+(_[A-Z0-9]+)+$/.test(s) ? "environment variable"
  : s === "none" ? "nothing (not selected)" : humanize(s));

export function WorkspaceSummary({ w, testId }: { w: WorkspaceInfo; testId?: string }) {
  return (
    <div data-testid={testId}>
      <KeyValues rows={[["Location", <Mono>{w.path}</Mono>],
        ["Status", <>{w.valid ? <Badge tone="ok">valid workspace</Badge> : <Badge tone="error">not usable</Badge>}{" "}
          {w.store_backend && <Badge>{storeLabel(w.store_backend)}</Badge>}{" "}
          {w.writable ? <Badge tone="neutral">read/write</Badge> : <Badge tone="warn">read-only</Badge>}
          {w.has_store ? "" : <> <Badge tone="info">no store yet</Badge></>}</>],
        ["Datasets", <b data-testid={testId ? `${testId}-datasets` : undefined}>{w.datasets}</b>],
        ["Research runs", <b data-testid={testId ? `${testId}-runs` : undefined}>{w.runs}</b>],
        ["Strategies", String(w.strategies)], ["Prop simulations", String(w.prop_simulations)],
        ["Feature cache", w.has_feature_cache ? "present" : "none"],
        ...(w.source ? [["Selected by", sourceLabel(w.source)] as [string, string]] : [])]} />
      {w.source && sourceLabel(w.source) !== w.source && <TechDetails rows={[["Selected by", <Mono>{w.source}</Mono>]]} />}
      {w.problems.map((p) => <Banner key={p} tone="error">{p}</Banner>)}
    </div>
  );
}

export function WorkspacePanel({ state, welcome }: { state: WorkspaceState; welcome?: boolean }) {
  const [path, setPath] = useState("");
  const [check, setCheck] = useState<WorkspaceInfo | null>(null);
  const [newPath, setNewPath] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState<ApiError | null>(null);
  const done = () => { window.location.hash = "#/datasets"; window.location.reload(); };
  const act = async (what: string, fn: () => Promise<unknown>) => {
    setBusy(what); setErr(null);
    try { await fn(); } catch (e) { setErr(e as ApiError); } finally { setBusy(""); }
  };
  const inspect = (p = path) => act("inspect", async () => setCheck(await api.post<WorkspaceInfo>("/api/workspace/inspect", { path: p })));
  const select = (p: string) => act("select", async () => { await api.post("/api/workspace/select", { path: p }); done(); });
  const create = (p: string) => act("create", async () => { await api.post("/api/workspace/create", { path: p }); done(); });
  const browse = () => act("browse", async () => {
    const r = await api.post<{ path: string | null }>("/api/workspace/browse");
    if (r.path) { setPath(r.path); await inspect(r.path); }
  });
  const d = state.default;
  return (
    <div data-testid="workspace-panel">
      {state.notice && <Banner tone="warn" testId="ws-notice">{state.notice}</Banner>}
      {state.current ? <>
        <p data-testid="ws-connected">Connected to workspace: <Mono>{state.current.path}</Mono></p>
        <WorkspaceSummary w={state.current} testId="ws-current" />
      </> : <Banner tone="info" testId="ws-none">{welcome ? "No research workspace is selected yet." : "No research workspace is selected."}</Banner>}
      {!state.switchable ? <p className="muted small">Development server: the workspace is the root folder the server was started with.
        Restart it with another root folder to change it; the desktop app switches workspaces here.</p> : <>
        <h3>{welcome ? "Open an existing workspace" : "Open another workspace"}</h3>
        <p className="muted small">An EdgeLab workspace is a folder with <code>configs/</code> and <code>data/</code> — for example your
          development folder (<code>…\AI-Backtesting</code>). Its datasets, strategies, runs, feature cache and prop simulations are used where
          they are: nothing is copied, imported, migrated or deleted, and both workspaces stay unchanged.</p>
        <div className="inline">
          <TextInput value={path} onChange={(v) => { setPath(v); setCheck(null); }} placeholder="C:\Users\you\Documents\AI-Backtesting" testId="ws-path" ariaLabel="workspace folder" />
          {state.browse_available && <Button onClick={browse} busy={busy === "browse"} testId="ws-browse">Browse…</Button>}
          <Button onClick={() => inspect()} busy={busy === "inspect"} disabled={!path.trim()} testId="ws-inspect">Check folder</Button>
        </div>
        {check && <Card title="Folder check" testId="ws-check">
          <WorkspaceSummary w={check} testId="ws-candidate" />
          <Button kind="primary" disabled={!check.valid} busy={busy === "select"} onClick={() => select(check.path)} testId="ws-select">
            Use this workspace</Button>
          {check.valid && !check.has_store && <p className="muted small">This workspace has no store yet; it is created on first use.</p>}
        </Card>}
        <h3>Create a new workspace</h3>
        <div className="inline">
          <TextInput value={newPath} onChange={setNewPath} placeholder="an empty or new folder" testId="ws-new-path" ariaLabel="new workspace folder" />
          <Button onClick={() => create(newPath)} busy={busy === "create"} disabled={!newPath.trim()} testId="ws-create">Create new workspace</Button>
        </div>
        <p className="muted small">Only in an empty or new folder: default configuration is copied in, the data store starts empty.</p>
        {d && <><h3>Default workspace</h3>
          <p className="small"><Mono>{d.path}</Mono> — {d.info.valid ? `${d.info.datasets} datasets, ${d.info.runs} runs` : "not created yet"}</p>
          <Button onClick={() => (d.info.valid ? select(d.path) : create(d.path))} busy={busy === "select" || busy === "create"} testId="ws-default">
            {d.info.valid ? "Open default workspace" : "Create default workspace"}</Button></>}
        {state.settings_path && <><p className="muted small">Your choice is remembered in this computer's EdgeLab settings file (not inside any workspace).</p>
          <TechDetails rows={[["Settings file", <Mono>{state.settings_path}</Mono>]]} /></>}
      </>}
      <ErrorPanel error={err} title="The workspace was not changed" testId="ws-error" />
    </div>
  );
}

export function WelcomePage({ state }: { state: WorkspaceState }) {
  return (
    <div className="page" data-testid="welcome-page">
      <header className="page-head"><h1>Welcome to EdgeLab</h1></header>
      <p>Choose where your research workspace should live. Open an existing workspace (for example the folder that already holds your
        datasets and research runs), or create a new one.</p>
      <Card title="Research Workspace"><WorkspacePanel state={state} welcome /></Card>
    </div>
  );
}

export const ChooseWorkspaceLink = () => <a href={href("/settings")} data-testid="choose-workspace">Choose Research Workspace</a>;
