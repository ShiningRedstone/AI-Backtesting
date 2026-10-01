import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { LibraryRow, ProtocolRecordRow } from "../api/types";
import { AppProvider, useApp } from "./context";
import { RouteContext, go, href, useHashRoute, useRoute } from "./router";
import { SYNTHETIC_NOTICE } from "../components/strategy";
import { Badge, Icon, Mono } from "../components/ui";
import { BuilderPage } from "../pages/Builder";
import { LibraryPage, StrategyPage } from "../pages/Strategies";
import { FamiliesPage, ResultsPage, VariationsPage } from "../pages/Research";
import { DatasetsPage, SettingsPage } from "../pages/Data";
import { DiscoveryPage } from "../pages/Discovery";
import { ResearchPage } from "../pages/ResearchEngine";
import { PropPage } from "../pages/Prop";
import { ComparePage } from "../pages/Compare";
import { HomePage } from "../pages/Home";
import { DashboardPage } from "../pages/Dashboard";
import { ExplorerPage } from "../pages/Explorer";
import { ControlsPage } from "../pages/Controls";
import { PipelinePage } from "../pages/Pipeline";
import { PaperPage } from "../pages/Paper";
import { RunsPage } from "../pages/Runs";
import { UpdateDialog, VersionChip } from "../components/updates";
import { WelcomePage } from "../components/workspace";
import type { WorkspaceState } from "../api/types";
import { useApi } from "./context";

interface NavItem { path: string; label: string; match: string; icon: string; planned?: boolean }
const NAV: { section: string; items: NavItem[] }[] = [
  { section: "Overview", items: [
    { path: "/", label: "Home", match: "", icon: "home" },
    { path: "/dashboard", label: "Research dashboard", match: "dashboard", icon: "chart" },
  ] },
  { section: "Strategies", items: [
    { path: "/explorer", label: "Explorer", match: "explorer", icon: "search" },
    { path: "/strategies", label: "Strategy library", match: "strategies", icon: "layers" },
    { path: "/builder", label: "Strategy builder", match: "builder", icon: "build" },
    { path: "/families", label: "Families", match: "families", icon: "tree" },
    { path: "/variations", label: "Variations", match: "variations", icon: "grid" },
  ] },
  { section: "Research", items: [
    { path: "/runs", label: "Research runs ▶", match: "runs", icon: "flask" },
    { path: "/research", label: "Experiments", match: "research", icon: "flask" },
    { path: "/results", label: "Results", match: "results", icon: "list" },
    { path: "/compare", label: "Compare", match: "compare", icon: "compare" },
    { path: "/controls", label: "Controls", match: "controls", icon: "shuffle" },
    { path: "/pipeline", label: "Candidate pipeline", match: "pipeline", icon: "flow" },
    { path: "/discovery", label: "AI Discovery", match: "discovery", icon: "sparkle" },
  ] },
  { section: "Simulation", items: [
    { path: "/prop", label: "Prop simulator", match: "prop", icon: "shield" },
    { path: "/paper", label: "Paper trading", match: "paper", icon: "pause", planned: true },
  ] },
  { section: "System", items: [
    { path: "/datasets", label: "Data", match: "datasets", icon: "db" },
    { path: "/settings", label: "Settings & about", match: "settings", icon: "gear" },
  ] },
];

function Search() {
  const [q, setQ] = useState("");
  const [rows, setRows] = useState<LibraryRow[] | null>(null);
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);
  const hits = (rows ?? []).filter((r) => q && `${r.name} ${r.strategy_id} ${r.family_id}`.toLowerCase().includes(q.toLowerCase())).slice(0, 8);
  return (
    <div className="search" ref={box}>
      <input className="input" type="search" placeholder="Search strategies…  (Enter: open · ↵ in Explorer for all)" aria-label="search strategies" value={q}
        data-testid="global-search"
        onFocus={() => { setOpen(true); api.get<LibraryRow[]>("/api/strategies").then(setRows).catch(() => setRows([])); }}
        onChange={(e: { target: HTMLInputElement }) => { setQ(e.target.value); setOpen(true); }}
        onKeyDown={(e: KeyboardEvent) => {
          if (e.key !== "Enter") return;
          if (hits[0]) go(`/strategies/${hits[0].strategy_id}`); else if (q) go(`/explorer?q=${encodeURIComponent(q)}`);
          setOpen(false); setQ("");
        }} />
      {open && q && (
        <ul className="search-results" role="listbox">
          {hits.length ? hits.map((h) => (
            <li key={h.strategy_id}><a href={href(`/strategies/${h.strategy_id}`)} onClick={() => { setOpen(false); setQ(""); }}>
              <span>{h.name}</span> <Mono>{h.strategy_id}</Mono></a></li>))
            : <li className="muted">{rows ? "No matches" : "Searching…"}</li>}
          {q && <li><a href={href(`/explorer?q=${encodeURIComponent(q)}`)} onClick={() => { setOpen(false); setQ(""); }}>
            <span>Search “{q}” in the Explorer</span><span className="muted small">all fields ›</span></a></li>}
        </ul>)}
    </div>
  );
}

function StatusDot() {
  const [ok, setOk] = useState<boolean | null>(null);
  useEffect(() => {
    const check = () => api.get("/api/health").then(() => setOk(true)).catch(() => setOk(false));
    check();
    const t = window.setInterval(check, 10000);
    return () => window.clearInterval(t);
  }, []);
  return (
    <span className={`status-dot ${ok === null ? "" : ok ? "ok" : "down"}`} role="status" data-testid="backend-status"
      title={ok ? "Backend connected" : ok === false ? "Backend unavailable" : "Checking backend…"}>
      <span className="dot" />{ok ? "Backend OK" : ok === false ? "Backend unavailable" : "…"}
    </span>
  );
}

function ProtocolChip() {
  const { data } = useApi<ProtocolRecordRow[]>("/api/protocols");
  const active = (data ?? []).filter((p) => p.status === "ACTIVE");
  if (!data) return null;
  if (!active.length) return <a className="chip warn" href={href("/")} title="No ACTIVE research protocol"><span className="dot" />No protocol</a>;
  const p = active[0];
  return (
    <a className="chip ok" href={href("/")} data-testid="protocol-chip"
      title={`ACTIVE research protocol ${p.protocol_id} (${p.name}), v${p.protocol_version ?? "?"}: locked holdout, trial budget`}>
      <span className="dot" />{p.protocol_id}<span className="muted">v{p.protocol_version ?? "?"}</span></a>
  );
}

function Toasts() {
  const { toasts, dismiss } = useApp();
  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((t) => <div key={t.id} className={`toast toast-${t.kind}`} onClick={() => dismiss(t.id)}>{t.text}</div>)}
    </div>
  );
}

function Page() {
  const route = useRoute();
  switch (route.parts[0] ?? "") {
    case "": return <HomePage />;
    case "dashboard": return <DashboardPage />;
    case "explorer": return <ExplorerPage />;
    case "strategies": return route.parts[1] ? <StrategyPage key={route.parts[1]} /> : <LibraryPage />;
    case "builder": return <BuilderPage />;
    case "families": return <FamiliesPage />;
    case "variations": return <VariationsPage />;
    case "datasets": return <DatasetsPage />;
    case "research": return <ResearchPage />;
    case "runs": return <RunsPage />;
    case "results": return <ResultsPage />;
    case "prop": return <PropPage />;
    case "compare": return <ComparePage />;
    case "controls": return <ControlsPage />;
    case "pipeline": return <PipelinePage />;
    case "paper": return <PaperPage />;
    case "discovery": return <DiscoveryPage />;
    case "settings": return <SettingsPage />;
    default: return <div className="page"><h1>Not found</h1><p><a href={href("/")}>Back to Home</a></p></div>;
  }
}

function Shell() {
  const route = useHashRoute();
  return <RouteContext.Provider value={route}><ShellBody /></RouteContext.Provider>;
}

const BrandMark = () => (
  <span className="brand-mark" aria-hidden="true">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#03130c" strokeWidth={2.6} strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 17l5-6 4 4 8-9" /></svg>
  </span>
);

function ShellBody() {
  const { demo } = useApp();
  const route = useRoute();
  const [menu, setMenu] = useState(false);
  useEffect(() => setMenu(false), [route.parts.join("/")]);
  const active = route.parts[0] ?? "";
  const ws = useApi<WorkspaceState>("/api/workspace");
  const firstRun = !!ws.data && ws.data.switchable && !ws.data.current && active !== "settings";
  const wsName = ws.data?.current?.path.split(/[\\/]/).filter(Boolean).pop();
  return (
    <div className={`shell${menu ? " menu-open" : ""}`}>
      <header className="topbar">
        <button className="hamburger" aria-label="menu" aria-expanded={menu} onClick={() => setMenu(!menu)} data-testid="menu-toggle">☰</button>
        <a className="brand" href={href("/")}><BrandMark />EdgeLab<span className="brand-sub">research terminal</span></a>
        {demo && <Badge tone="demo">DEMO</Badge>}
        <Search />
        <span className="spacer" />
        {ws.data?.current && <ProtocolChip />}
        {ws.data && <a className="chip" href={href("/settings")} data-testid="ws-chip"
          title={ws.data.current ? `Research workspace: ${ws.data.current.path}` : "No research workspace selected"}>
          {ws.data.current ? <>Workspace <b>{wsName}</b></> : <Badge tone="warn">no workspace</Badge>}</a>}
        <VersionChip />
        <StatusDot />
      </header>
      {demo && <div className="demo-banner" data-testid="demo-banner"><b>DEMO WORKSPACE</b> — {SYNTHETIC_NOTICE}</div>}
      <nav className="sidebar" aria-label="main navigation">
        {NAV.map((g) => (
          <div key={g.section} style={{ display: "contents" }}>
            <div className="nav-section">{g.section}</div>
            {g.items.map((n) => (
              <a key={n.path} href={href(n.path)} className={`nav-item${active === n.match ? " active" : ""}`}
                data-testid={`nav-${n.match || "dashboard"}`} aria-current={active === n.match ? "page" : undefined}>
                <Icon name={n.icon} />{n.label}{n.planned && <span className="nav-planned">planned</span>}
              </a>))}
          </div>))}
        <div className="nav-foot muted small">Research tool · historical results under stated assumptions · no live trading ·
          no broker connections</div>
      </nav>
      <div className="scrim" onClick={() => setMenu(false)} />
      <main className="main">{firstRun && ws.data ? <WelcomePage state={ws.data} /> : <Page />}</main>
      <UpdateDialog />
      <Toasts />
    </div>
  );
}

export function App() {
  return <AppProvider><Shell /></AppProvider>;
}
