import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { ProtocolRecordRow } from "../api/types";
import { AppProvider, useApp } from "./context";
import { RouteContext, href, useHashRoute, useRoute } from "./router";
import { SYNTHETIC_NOTICE } from "../components/strategy";
import { Badge, Icon } from "../components/ui";
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
import { RunBacktestPage } from "../pages/RunBacktest";
import { UpdateBanner, VersionChip } from "../components/updates";
import { WelcomePage } from "../components/workspace";
import type { WorkspaceState } from "../api/types";
import { useApi } from "./context";

/** The seven tabs. `heads` are the first route segments that belong to a tab (old routes stay valid). */
interface NavItem { path: string; label: string; tid: string; icon: string; heads: string[]; planned?: boolean }
const NAV: NavItem[] = [
  { path: "/", label: "Home", tid: "home", icon: "home", heads: [""] },
  { path: "/strategies", label: "Strategies", tid: "strategies", icon: "layers", heads: ["strategies", "families", "builder", "variations"] },
  { path: "/runs", label: "Run backtest", tid: "run", icon: "flask", heads: ["runs", "run", "research"] },
  { path: "/dashboard", label: "Backtest results", tid: "results", icon: "chart",
    heads: ["dashboard", "explorer", "results", "compare", "controls", "pipeline"] },
  { path: "/prop", label: "Prop firm simulator", tid: "prop", icon: "shield", heads: ["prop"] },
  { path: "/paper", label: "Paper trading", tid: "paper", icon: "pause", heads: ["paper"], planned: true },
  { path: "/settings", label: "Settings", tid: "settings", icon: "gear", heads: ["settings", "datasets"] },
];

/** Sub-views inside a tab (links, so every view keeps its own address). */
const SUBTABS: Record<string, { path: string; label: string; head: string }[]> = {
  strategies: [{ path: "/strategies", label: "Library", head: "strategies" }, { path: "/families", label: "Families", head: "families" },
    { path: "/builder", label: "Builder", head: "builder" }, { path: "/variations", label: "Variations", head: "variations" }],
  run: [{ path: "/runs", label: "Research runs", head: "runs" }, { path: "/run", label: "Single backtest", head: "run" }],
  results: [{ path: "/dashboard", label: "Overview", head: "dashboard" }, { path: "/explorer", label: "Strategies", head: "explorer" },
    { path: "/results", label: "All runs", head: "results" }, { path: "/compare", label: "Compare", head: "compare" },
    { path: "/controls", label: "Random controls", head: "controls" }, { path: "/pipeline", label: "Candidate pipeline", head: "pipeline" }],
  settings: [{ path: "/settings", label: "Settings", head: "settings" }, { path: "/datasets", label: "Data", head: "datasets" }],
};

const tabOf = (head: string) => NAV.find((n) => n.heads.includes(head));

function SubNav({ head }: { head: string }) {
  const tab = tabOf(head);
  const subs = tab ? SUBTABS[tab.tid] : undefined;
  if (!subs) return null;
  return (
    <nav className="tabs subnav" aria-label={`${tab!.label} views`}>
      {subs.map((s) => (
        <a key={s.path} href={href(s.path)} className={`tab${s.head === head ? " active" : ""}`} data-testid={`subnav-${s.head}`}
          aria-current={s.head === head ? "page" : undefined}>{s.label}</a>))}
    </nav>
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
      <span className="dot" />Research protocol<span className="muted">version {p.protocol_version ?? "?"}</span></a>
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
    case "research":                                       // no Experiments tab: only job / result deep links remain
      return route.parts[1] || route.query.get("job") || route.query.get("setup") ? <ResearchPage /> : <RedirectTo path="/runs" />;
    case "run": return <RunBacktestPage />;
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

function RedirectTo({ path }: { path: string }) {
  useEffect(() => { window.location.replace(href(path)); }, [path]);
  return null;
}

function Shell() {
  const route = useHashRoute();
  return <RouteContext.Provider value={route}><ShellBody /></RouteContext.Provider>;
}

const BrandMark = () => (
  <span className="brand-mark" aria-hidden="true">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 17l5-6 4 4 8-9" /></svg>
  </span>
);

function ShellBody() {
  const { demo, refreshPrefs } = useApp();
  const route = useRoute();
  const [menu, setMenu] = useState(false);
  useEffect(() => setMenu(false), [route.parts.join("/")]);
  useEffect(refreshPrefs, [route.parts[0]]);               // favorites / tested strategies stay current (at most every 30 s)
  const active = route.parts[0] ?? "";
  const ws = useApi<WorkspaceState>("/api/workspace");
  const firstRun = !!ws.data && ws.data.switchable && !ws.data.current && active !== "settings";
  const wsName = ws.data?.current?.path.split(/[\\/]/).filter(Boolean).pop();
  return (
    <div className={`shell${menu ? " menu-open" : ""}`}>
      <header className="topbar">
        <button className="hamburger" aria-label="menu" aria-expanded={menu} onClick={() => setMenu(!menu)} data-testid="menu-toggle">☰</button>
        <a className="brand" href={href("/")}><BrandMark />MUNYUN LAB</a>
        {demo && <Badge tone="demo">DEMO</Badge>}
        <span className="spacer" />
        {ws.data?.current && <ProtocolChip />}
        {ws.data && <a className="chip" href={href("/settings")} data-testid="ws-chip"
          title={ws.data.current ? `Research workspace: ${ws.data.current.path}` : "No research workspace selected"}>
          {ws.data.current ? <>Workspace <b>{wsName}</b></> : <Badge tone="warn">no workspace</Badge>}</a>}
        <VersionChip />
        <StatusDot />
      </header>
      {demo && <div className="demo-banner" data-testid="demo-banner"><b>DEMO WORKSPACE</b> — {SYNTHETIC_NOTICE}</div>}
      <UpdateBanner />
      <nav className="sidebar" aria-label="main navigation">
        {NAV.map((n) => {
          const on = n.heads.includes(active);
          return (
            <a key={n.path} href={href(n.path)} className={`nav-item${on ? " active" : ""}`} data-testid={`nav-${n.tid}`}
              aria-current={on ? "page" : undefined}>
              <Icon name={n.icon} />{n.label}{n.planned && <span className="nav-planned">planned</span>}
            </a>);
        })}
        <div className="nav-foot muted small">Research tool · historical results under stated assumptions · no live trading ·
          no broker connections</div>
      </nav>
      <div className="scrim" onClick={() => setMenu(false)} />
      <main className="main">{firstRun && ws.data ? <WelcomePage state={ws.data} /> : <><SubNav head={active} /><Page /></>}</main>
      <Toasts />
    </div>
  );
}

export function App() {
  return <AppProvider><Shell /></AppProvider>;
}
