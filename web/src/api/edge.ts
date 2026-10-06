/** Edge lab (ADR-104): the edge check (frozen hypotheses on NQ 9:30-11:00) and the trade anatomy of My strategy reports. */
import { api } from "./client";
import type { MyJob } from "./my";

const enc = encodeURIComponent;

export interface Summary { n: number; mean: number | null; sd: number | null; se: number | null; t: number | null }
export interface Hist { edges: number[]; counts: number[]; n: number }
export interface HypothesisDef { id: string; name: string; idea: string; rule: string; direction_meaning: string; against: string[];
  parameters: Record<string, string> }
export interface EdgeSet { version: number; fingerprint: string; family: number; window: string; hypotheses: HypothesisDef[] }
export type Verdict = "TOO_FEW" | "NO_EVIDENCE" | "NOT_TRADEABLE" | "INCONSISTENT" | "CANDIDATE";
export interface HypResult {
  id: string; n: number; days_usable: number; verdict: Verdict; verdict_text: string; reason?: string;
  longs?: number; shorts?: number; gross_pts?: Summary; gross_norm?: Summary; gross_pts_ci95?: [number, number] | null;
  gross_norm_ci95?: [number, number] | null; gross_norm_ci_bonf?: [number, number] | null; gross_pts_ci_bonf?: [number, number] | null;
  p?: number; p_bonf?: number; null?: Hist; null_mean?: number; null_sd?: number; observed_norm?: number;
  win_share?: number; win_share_all_up?: number; typical_pts?: number; detectable_norm?: number | null; detectable_pts?: number;
  direction?: "as stated" | "opposite"; net_pts?: Summary; net_pts_ci95?: [number, number] | null; net_win_share?: number; cost_pts?: number;
  by_year?: { year: number; n: number; gross_pts: number; gross_norm: number }[]; years_same_sign?: number; years_counted?: number;
  es?: { n: number; too_few?: boolean; gross_norm?: Summary; gross_pts?: Summary; p?: number; same_sign?: boolean };
}
export interface EdgeResult {
  set: EdgeSet; computed_at: string; app_version: string; key: string;
  window: { start: string; end: string; first_day: string; last_day: string };
  dataset: { dataset_id: string; content_hash: string; instrument: string; provider: string };
  days: { in_window: number; usable: number; skipped_missing_minutes: number; skipped_first_20_days: number };
  costs: { scenario: string | null; contract: string; note: string }; es: { content_hash: string; usable_days: number } | null;
  family: number; alpha: number; results: HypResult[];
}
export interface ReportPick { id: string; label: string; kind: string; created_at: string; trade_count: number; favorite?: boolean;
  window: { start: string; end: string } }
export interface EdgeStatus { set: EdgeSet; latest: EdgeResult | null; job: MyJob | null; reports: ReportPick[] }

export interface Block { n: number; gross_r?: Summary; net_r?: Summary; cost_r?: number; gross_win?: number; net_win?: number;
  gross_ci95?: [number, number] | null; net_ci95?: [number, number] | null }
export interface Anatomy {
  report: { id: string; label: string; kind: string; created_at: string; window: { start: string; end: string }; trade_count: number };
  all: Block; verdict: { code: string; text: string; lines?: string[] };
  excursion?: { reached: Record<string, { all: number; losers: number | null }>; never_moved: number; losers_never_moved: number | null;
    winners_heat_half_r: number | null; mfe_hist: Hist; mae_hist: Hist; median_mfe_losers: number | null; median_mfe_winners: number | null };
  by?: Record<"direction" | "model" | "exit", (Block & { group: string })[]>;
}

export const edge = {
  statusUrl: "/api/edge",
  anatomyUrl: (id: string) => `/api/edge/anatomy/${enc(id)}`,
  run: () => api.post<MyJob>("/api/edge/check", {}),
  job: (id: string) => api.get<MyJob>(`/api/edge/jobs/${enc(id)}`),
};
