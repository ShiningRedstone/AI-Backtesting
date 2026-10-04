import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api, ApiError, viewCache } from "../api/client";
import type { BuilderOptions, PaperFees } from "../api/types";

interface Toast { id: number; kind: "ok" | "error" | "info"; text: string }
/** Workspace display preferences (ADR-74): favorites, the prop account for pass criteria, and the two switches. */
export interface UiPrefs { favorites: string[]; prop_criteria_profile: string; show_ids: boolean; show_readonly: boolean;
  profile_choices?: { profile_id: string; name: string }[]; prop_fees?: Record<string, PaperFees>; theme?: Theme;
  // ADR-90 (display / paper only)
  currency?: "USD" | "CHF"; chf_per_usd?: number | null; chart_cluster?: boolean; chart_cluster_distance?: number;
  live_dd_limit_usd?: number; prop_discount?: { enabled: boolean; pct: Record<string, number> };
  autotune_goals?: Record<string, { on: boolean; value?: number }> }   // ADR-99
export type Theme = "dark" | "light" | "system";
const THEME_KEY = "munyun.theme";
/** ADR-86: apply a theme to <html data-theme>; "system" follows the Windows / browser setting. Remembered locally so the
 *  next start paints in the right theme before the workspace preferences arrive (display only). */
export function applyTheme(t: Theme | undefined) {
  const want = t ?? "dark";
  const dark = want === "system" ? !(window.matchMedia?.("(prefers-color-scheme: light)").matches) : want === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  try { localStorage.setItem(THEME_KEY, want); } catch { /* storage unavailable: only the first paint is affected */ }
}
export function initialTheme(): Theme {
  try { const v = localStorage.getItem(THEME_KEY); if (v === "dark" || v === "light" || v === "system") return v; } catch { /* ignore */ }
  return "dark";
}
const PREF_DEFAULTS: UiPrefs = { favorites: [], prop_criteria_profile: "LUCID_LUCIDFLEX_50K", show_ids: false, show_readonly: false,
  theme: initialTheme() };
interface AppState {
  options: BuilderOptions | null; optionsError: ApiError | null; demo: boolean;
  reloadOptions: () => void;
  prefs: UiPrefs; setPref: (changes: Partial<Omit<UiPrefs, "favorites">>) => Promise<void>; reloadPrefs: () => void;
  refreshPrefs: () => void;
  tested: Set<string>; toggleFavorite: (sid: string) => Promise<void>;
  toasts: Toast[]; toast: (kind: Toast["kind"], text: string) => void; dismiss: (id: number) => void;
}
const Ctx = createContext<AppState | null>(null);

export function AppProvider({ children }: { children?: ReactNode }) {
  const [options, setOptions] = useState<BuilderOptions | null>(null);
  const [optionsError, setOptionsError] = useState<ApiError | null>(null);
  const [demo, setDemo] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const reloadOptions = useCallback(() => {
    api.get<BuilderOptions>("/api/options").then((o) => { setOptions(o); setOptionsError(null); })
      .catch((e: ApiError) => setOptionsError(e));
    api.get<{ demo: boolean }>("/api/health").then((h) => setDemo(h.demo)).catch(() => undefined);
  }, []);
  useEffect(reloadOptions, [reloadOptions]);
  const [prefs, setPrefs] = useState<UiPrefs>(PREF_DEFAULTS);
  const [tested, setTested] = useState<Set<string>>(new Set());
  const lastPrefsLoad = useRef(0);
  const reloadPrefs = useCallback(() => {
    lastPrefsLoad.current = Date.now();
    api.get<UiPrefs>("/api/preferences/ui").then(setPrefs).catch(() => setPrefs(PREF_DEFAULTS));    // no workspace: defaults
    api.get<{ favorites: string[]; tested: string[] }>("/api/favorites").then((f) => setTested(new Set(f.tested))).catch(() => undefined);
  }, []);
  useEffect(reloadPrefs, [reloadPrefs]);
  useEffect(() => {                                  // ADR-86 theme, and live changes of the Windows setting
    applyTheme(prefs.theme);
    if (prefs.theme !== "system" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const on = () => applyTheme("system");
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, [prefs.theme]);
  /** Page changes refresh favorites / tested strategies at most every 30 s (ADR-77); writes refresh them directly. */
  const refreshPrefs = useCallback(() => { if (Date.now() - lastPrefsLoad.current > 30_000) reloadPrefs(); }, [reloadPrefs]);
  const setPref = useCallback(async (changes: Partial<Omit<UiPrefs, "favorites">>) => {
    const next = await api.post<UiPrefs>("/api/preferences/ui", changes);
    setPrefs((p) => ({ ...next, profile_choices: p.profile_choices }));
  }, []);
  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);
  const toast = useCallback((kind: Toast["kind"], text: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, kind, text }]);
    window.setTimeout(() => dismiss(id), kind === "error" ? 8000 : 4000);
  }, [dismiss]);
  const toggleFavorite = useCallback(async (sid: string) => {
    try {
      const next = await api.post<UiPrefs>(`/api/favorites/${sid}`, { favorite: !prefs.favorites.includes(sid) });
      setPrefs((p) => ({ ...next, profile_choices: p.profile_choices }));
    } catch (e) { toast("error", (e as Error).message); }
  }, [prefs.favorites, toast]);
  return <Ctx.Provider value={{ options, optionsError, demo, reloadOptions, toasts, toast, dismiss, prefs, setPref, reloadPrefs, refreshPrefs,
    tested, toggleFavorite }}>{children}</Ctx.Provider>;
}

export function useApp(): AppState {
  const c = useContext(Ctx);
  if (!c) throw new Error("useApp outside AppProvider");
  return c;
}

/** Load data from the API with loading/error state; `reload` re-fetches. ADR-77: when this url was loaded before in
 * this browser session (and nothing was written since), its previous answer shows at once while the fresh one loads. */
export function useApi<T>(url: string | null, deps: readonly unknown[] = []) {
  const [data, setData] = useState<T | null>(() => (url && viewCache.has(url) ? viewCache.get(url) as T : null));
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!url) return;
    let live = true;
    setLoading(true);
    if (viewCache.has(url)) setData(viewCache.get(url) as T);
    api.get<T>(url).then((d) => { viewCache.put(url, d); if (live) { setData(d); setError(null); } })
      .catch((e: ApiError) => { if (live) setError(e); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [url, tick, ...deps]);
  return { data, error, loading, reload: () => setTick((t) => t + 1), setData };
}
