import { api } from "./client";

/** Frozen-manifest research campaigns (ADR-68/69). The desktop UI is a control and presentation layer:
 *  every run goes through the same backend runner as `research campaign-run`. */

export interface ScopeProgress {
  strategies: number; completed: number; failed: number; remaining: number;
  by_status: Record<string, number>; fraction_done: number | null;
}
export interface CampaignProtocol {
  protocol_id: string; protocol_version: number; material_hash: string; max_unique_trials: number;
  family_size_rule: string; holdout_looks_budget: number;
}
export interface CampaignRunRecord {
  run_record_id: string; campaign_id: string; source: string; search_id: string; protocol_id: string;
  protocol_version: number; manifest_id: string; families: string[]; all_families: boolean | null; n_scope: number;
  created_at: string; started_at: string | null; finished_at: string | null; updated_at: string | null;
  status: string; phase: string; batch_status?: string;
  current: { strategy_id: string; family_id: string; timeframe: string; dataset_id: string; index: number; of: number; started_at: string } | null;
  counts: { completed_this_run: number; failed_this_run: number; skipped_completed: number };
  totals: ScopeProgress | null; errors: { strategy_id?: string; check?: string; error?: string; detail?: unknown; at?: string }[];
  workers: number; holdout: string;
}
export interface CampaignSummary {
  campaign_id: string; stage: string; search_id: string; config_hash: string;
  manifest: { manifest_id: string; n_strategies: number; strategies_sha256: string; factory_version: string; variation_space_version: string };
  protocol: CampaignProtocol;
  discovery: { trading_dates: string[]; period: { start: string; end: string } };
  datasets: Record<string, { dataset_id: string; content_hash: string }>;
  execution: { workers: number; max_cells: number; holdout: { enabled: boolean }; account: { starting_equity: number; currency: string };
               execution_contract: string; max_quantity: number; trials_per_strategy: number };
  prop_simulation: { enabled: boolean; profiles: { profile_id: string; version: number; profile_hash: string }[] };
  progress: ScopeProgress; error?: { code: string; message: string };
}
export interface CampaignListRow extends CampaignSummary { n_runs: number; latest_run: CampaignRunRecord | null }
export interface CampaignFamily {
  family_id: string; name: string; group: string | null; hypothesis: string | null; n_strategies: number;
  timeframes: Record<string, number>; completed: number; failed: number; remaining: number;
}
export interface CampaignDetail extends CampaignSummary {
  families: CampaignFamily[]; runs: CampaignRunRecord[]; prop_results_available: boolean; note: string;
}
export interface FamilyResults {
  campaign_id: string; family_id: string; n: number; note: string;
  strategies: { strategy_id: string; logic_hash: string; name: string; timeframe: string; dataset_id: string; status: string;
                run_id: string | null; error: string | null; headline: Record<string, number | string | null> }[];
}
export interface StrategyResult {
  campaign_id: string; strategy_id: string; family_id: string; status: string; error: string | null; run_id: string | null;
  provenance: Record<string, unknown>; metrics?: Record<string, unknown>; n_trades?: number; trades_hash?: string;
  prop?: { profiles: { profile: { profile_id: string; version: number }; status: string; final_status: string;
                       rule_basis_state?: string; summary?: Record<string, unknown> }[]; base?: Record<string, unknown> };
}
export type CampaignJobState = "queued" | "running" | "completed" | "failed" | "cancelled";
export interface CampaignJob {
  job_id: string; kind: "campaign"; campaign_id: string; search_id: string; families: string[] | null; state: CampaignJobState;
  created_at: string; started_at: string | null; finished_at: string | null; error: string | null; cancel_requested: boolean;
  live: Partial<CampaignRunRecord> & { status: string; phase: string };
}
export interface CheckReport { campaign_id: string; ready: boolean; note: string; checks: { check: string; ok: boolean; detail: unknown }[] }

const enc = encodeURIComponent;
export const campaigns = {
  listUrl: "/api/campaigns",
  detailUrl: (cid: string) => `/api/campaigns/${enc(cid)}`,
  familyUrl: (cid: string, fid: string) => `/api/campaigns/${enc(cid)}/families/${enc(fid)}`,
  strategyUrl: (cid: string, sid: string) => `/api/campaigns/${enc(cid)}/strategies/${enc(sid)}`,
  check: (cid: string) => api.get<CheckReport>(`/api/campaigns/${enc(cid)}/check`),
  start: (cid: string, families: string[] | null) => api.post<CampaignJob>(`/api/campaigns/${enc(cid)}/jobs`, { families }),
  job: (jid: string) => api.get<CampaignJob>(`/api/campaigns/jobs/${enc(jid)}`),
  cancel: (jid: string) => api.post<CampaignJob>(`/api/campaigns/jobs/${enc(jid)}/cancel`),
  active: () => api.get<{ job: CampaignJob | null }>("/api/campaigns/active-job"),
};
export const CAMPAIGN_JOB_FINAL = new Set(["completed", "failed", "cancelled"]);
export const LIVE_POLL_MS = 1000;
