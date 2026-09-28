import { createContext, useCallback, useContext, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api, ApiError } from "../api/client";
import type { BuilderOptions } from "../api/types";

interface Toast { id: number; kind: "ok" | "error" | "info"; text: string }
interface AppState {
  options: BuilderOptions | null; optionsError: ApiError | null; demo: boolean;
  reloadOptions: () => void;
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
  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);
  const toast = useCallback((kind: Toast["kind"], text: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, kind, text }]);
    window.setTimeout(() => dismiss(id), kind === "error" ? 8000 : 4000);
  }, [dismiss]);
  return <Ctx.Provider value={{ options, optionsError, demo, reloadOptions, toasts, toast, dismiss }}>{children}</Ctx.Provider>;
}

export function useApp(): AppState {
  const c = useContext(Ctx);
  if (!c) throw new Error("useApp outside AppProvider");
  return c;
}

/** Load data from the API with loading/error state; `reload` re-fetches. */
export function useApi<T>(url: string | null, deps: readonly unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!url) return;
    let live = true;
    setLoading(true);
    api.get<T>(url).then((d) => { if (live) { setData(d); setError(null); } })
      .catch((e: ApiError) => { if (live) setError(e); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [url, tick, ...deps]);
  return { data, error, loading, reload: () => setTick((t) => t + 1), setData };
}
