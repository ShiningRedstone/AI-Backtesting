import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { LibraryRow } from "../api/types";
import { AppProvider, useApp } from "./context";
import { RouteContext, go, href, useHashRoute, useRoute } from "./router";
import { SYNTHETIC_NOTICE } from "../components/strategy";
import { Badge, Mono } from "../components/ui";
import { BuilderPage } from "../pages/Builder";
import { DashboardPage, LibraryPage, StrategyPage } from "../pages/Strategies";
import { FamiliesPage, ResultsPage, VariationsPage } from "../pages/Research";
import { DatasetsPage, DiscoveryPage, ResearchPage, SettingsPage } from "../pages/Data";

const NAV: { path: string; label: string; match: string; planned?: boolean }[] = [
  { path: "/", label: "Dashboard", match: "" },
  { path: "/strategies", label: "Strategies", match: "strategies" },
  { path: "/builder", label: "Strategy Builder", match: "builder" },
  { path: "/families", label: "Families", match: "families" },
  { path: "/variations", label: "Variations", match: "variations" },
  { path: "/datasets", label: "Datasets", match: "datasets" },
  { path: "/research", label: "Research", match: "research", planned: true },
  { path: "/results", label: "Results", match: "results" },
  { path: "/discovery", label: "AI Discovery", match: "discovery", planned: true },
  { path: "/settings", label: "Settings", match: "settings" },
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
      <input className="input" type="search" placeholder="Search strategies…" aria-label="search strategies" value={q}
        data-testid="global-search"
        onFocus={() => { setOpen(true); api.get<LibraryRow[]>("/api/strategies").then(setRows).catch(() => setRows([])); }}
        onChange={(e: { target: HTMLInputElement }) => { setQ(e.target.value); setOpen(true); }}
        onKeyDown={(e: KeyboardEvent) => { if (e.key === "Enter" && hits[0]) { go(`/strategies/${hits[0].strategy_id}`); setOpen(false); setQ(""); } }} />
      {open && q && (
        <ul className="search-results" role="listbox">
          {hits.length ? hits.map((h) => (
            <li key={h.strategy_id}><a href={href(`/strategies/${h.strategy_id}`)} onClick={() => { setOpen(false); setQ(""); }}>
              <span>{h.name}</span> <Mono>{h.strategy_id}</Mono></a></li>))
            : <li className="muted">{rows ? "No matches" : "Searching…"}</li>}
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

const BrandMark = () => (
  <span className="brand-mark" aria-hidden="true">
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 2.5h4M7 2.5v4.2L3.6 12.4a1 1 0 0 0 .9 1.6h7a1 1 0 0 0 .9-1.6L9 6.7V2.5M5.2 10h5.6" /></svg>
  </span>
);

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
    case "": return <DashboardPage />;
    case "strategies": return route.parts[1] ? <StrategyPage key={route.parts[1]} /> : <LibraryPage />;
    case "builder": return <BuilderPage />;
    case "families": return <FamiliesPage />;
    case "variations": return <VariationsPage />;
    case "datasets": return <DatasetsPage />;
    case "research": return <ResearchPage />;
    case "results": return <ResultsPage />;
    case "discovery": return <DiscoveryPage />;
    case "settings": return <SettingsPage />;
    default: return <div className="page"><h1>Not found</h1><p><a href={href("/")}>Back to the dashboard</a></p></div>;
  }
}

function Shell() {
  const route = useHashRoute();
  return <RouteContext.Provider value={route}><ShellBody /></RouteContext.Provider>;
}

function ShellBody() {
  const { demo } = useApp();
  const route = useRoute();
  const [menu, setMenu] = useState(false);
  useEffect(() => setMenu(false), [route.parts.join("/")]);
  const active = route.parts[0] ?? "";
  return (
    <div className={`shell${menu ? " menu-open" : ""}`}>
      <header className="topbar">
        <button className="hamburger" aria-label="menu" aria-expanded={menu} onClick={() => setMenu(!menu)} data-testid="menu-toggle">☰</button>
        <a className="brand" href={href("/")}><BrandMark /><span className="brand-name">EdgeLab</span></a>
        {demo && <Badge tone="demo">DEMO</Badge>}
        <Search />
        <span className="spacer" />
        <StatusDot />
        <a className="topbar-link" href={href("/settings")}>Settings</a>
      </header>
      {demo && <div className="demo-banner" data-testid="demo-banner"><b>DEMO WORKSPACE</b> — {SYNTHETIC_NOTICE}</div>}
      <nav className="sidebar" aria-label="main navigation">
        {NAV.map((n) => (
          <a key={n.path} href={href(n.path)} className={`nav-item${active === n.match ? " active" : ""}`} data-testid={`nav-${n.match || "dashboard"}`}>
            {n.label}{n.planned && <span className="nav-planned">planned</span>}
          </a>))}
        <div className="nav-foot muted small">Research tool · no live trading · no broker connections</div>
      </nav>
      <div className="scrim" onClick={() => setMenu(false)} />
      <main className="main"><Page /></main>
      <Toasts />
    </div>
  );
}

export function App() {
  return <AppProvider><Shell /></AppProvider>;
}
