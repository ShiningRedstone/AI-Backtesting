/* Live charts (ADR-111): Dukascopy MNQ / NQ / ES / MES bars, live polling, saved drawings and layout. */
import { api } from "./client";

export interface ChartSymbol { symbol: string; code: string; name: string; point_value: number; tick: number; family: string; source: string }
export interface ChartMeta { symbols: ChartSymbol[]; timeframes: { tf: number; label: string }[];
  status: Record<string, { error?: string | null; last_ok?: string; last_error?: string }> }
export interface Bar { time: number; open: number; high: number; low: number; close: number; volume: number }
export interface BarsAnswer { symbol: string; tf: number; tf_label: string; bars: Bar[]; more: boolean; source: string;
  status: { error: string | null; last_ok: string | null } }

const q = (o: Record<string, string | number | undefined>) =>
  Object.entries(o).filter(([, v]) => v !== undefined).map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`).join("&");

export const charts = {
  meta: () => api.get<ChartMeta>("/api/charts"),
  bars: (symbol: string, tf: string, to?: number, count?: number) => api.get<BarsAnswer>(`/api/charts/bars?${q({ symbol, tf, to, count })}`),
  live: (symbol: string, tf: string, since: number) => api.get<BarsAnswer>(`/api/charts/live?${q({ symbol, tf, since })}`),
  drawings: (symbol: string) => api.get<{ symbol: string; drawings: unknown[] }>(`/api/charts/drawings/${symbol}`),
  saveDrawings: (symbol: string, drawings: unknown[]) => api.put<{ saved: number }>(`/api/charts/drawings/${symbol}`, { drawings }),
  layout: () => api.get<Record<string, unknown>>("/api/charts/layout"),
  saveLayout: (layout: Record<string, unknown>) => api.put<{ saved: boolean }>("/api/charts/layout", layout),
};
