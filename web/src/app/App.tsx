import { useEffect, useState } from "react";
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
import { HoldoutPage } from "../pages/Holdout";
import { FlipsPage } from "../pages/Flips";
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
  { path: "/runs", label: "Run backtest", tid: "run", icon: "flask", heads: ["runs", "run", "research", "holdout", "flips"] },
  { path: "/dashboard", label: "Backtest results", tid: "results", icon: "chart",
    heads: ["dashboard", "explorer", "holdout-results", "results", "compare", "controls", "pipeline"] },
  { path: "/paper", label: "Prop & paper", tid: "paper", icon: "shield", heads: ["paper", "prop"] },
  { path: "/settings", label: "Settings", tid: "settings", icon: "gear", heads: ["settings", "datasets"] },
];

/** Sub-views inside a tab (links, so every view keeps its own address). */
const SUBTABS: Record<string, { path: string; label: string; head: string }[]> = {
  strategies: [{ path: "/strategies", label: "Library", head: "strategies" }, { path: "/families", label: "Families", head: "families" },
    { path: "/builder", label: "Builder", head: "builder" }, { path: "/variations", label: "Variations", head: "variations" }],
  run: [{ path: "/runs", label: "Research runs", head: "runs" }, { path: "/run", label: "Single backtest", head: "run" },
    { path: "/holdout", label: "Holdout backtest", head: "holdout" }, { path: "/flips", label: "Flip scan", head: "flips" }],
  results: [{ path: "/dashboard", label: "Overview", head: "dashboard" }, { path: "/explorer", label: "Strategies", head: "explorer" },
    { path: "/holdout-results", label: "Holdout results", head: "holdout-results" },
    { path: "/results", label: "All runs", head: "results" }, { path: "/compare", label: "Compare", head: "compare" },
    { path: "/controls", label: "Random controls", head: "controls" }, { path: "/pipeline", label: "Candidate pipeline", head: "pipeline" }],
  paper: [{ path: "/paper", label: "Paper accounts", head: "paper" }, { path: "/paper/new", label: "Start paper trading", head: "paper/new" },
    { path: "/prop", label: "Backtest prop check", head: "prop" }],
  settings: [{ path: "/settings", label: "Settings", head: "settings" }, { path: "/datasets", label: "Data", head: "datasets" }],
};

const tabOf = (head: string) => NAV.find((n) => n.heads.includes(head));

function SubNav({ head, sub }: { head: string; sub: string }) {
  const tab = tabOf(head);
  const subs = tab ? SUBTABS[tab.tid] : undefined;
  if (!subs) return null;
  return (
    <nav className="tabs subnav" aria-label={`${tab!.label} views`}>
      {subs.map((s) => (
        <a key={s.path} href={href(s.path)} className={`tab${s.head === sub ? " active" : ""}`} data-testid={`subnav-${s.head.replace("/", "-")}`}
          aria-current={s.head === sub ? "page" : undefined}>{s.label}</a>))}
    </nav>
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
    case "holdout-results": return <ExplorerPage key="holdout" holdout />;
    case "holdout": return <HoldoutPage />;
    case "flips": return <FlipsPage />;
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
  return (
    <div className={`shell${menu ? " menu-open" : ""}`}>
      <header className="topbar">
        <button className="hamburger" aria-label="menu" aria-expanded={menu} onClick={() => setMenu(!menu)} data-testid="menu-toggle">☰</button>
        <a className="brand" href={href("/")}><BrandMark />MUNYUN LAB</a>
        {demo && <Badge tone="demo">DEMO</Badge>}
        <span className="spacer" />
        <VersionChip />
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
      <main className="main">{firstRun && ws.data ? <WelcomePage state={ws.data} /> : <><SubNav head={active} sub={active === "paper" && route.parts[1] === "new" ? "paper/new" : active} /><Page /></>}</main>
      <Toasts />
    </div>
  );
}

export function App() {
  return <AppProvider><Shell /></AppProvider>;
}
