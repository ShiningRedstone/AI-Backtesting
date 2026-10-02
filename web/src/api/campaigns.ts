import { api } from "./client";

/** Frozen-manifest research campaigns (ADR-68/69/70). The desktop UI is a control and presentation layer:
 *  every run goes through the same backend runner as `research campaign-run`. */

export interface ScopeProgress {
  strategies: number; completed: number; failed: number; remaining: number;
  by_status: Record<string, number>; fraction_done: number | null;
}
export interface Eta {
  state: "estimating" | "estimate"; n_observations: number; min_observations: number; workers: number;
  remaining_strategies: number; remaining_seconds: number | null; note: string;
  basis: { groups_used?: Record<string, number>; overall_median_s?: number; median_by_timeframe_s?: Record<string, number>; all?: { n: number } } | null;
}
export interface CampaignProtocol {
  protocol_id: string; protocol_version: number; material_hash: string; max_unique_trials: number;
  family_size_rule: string; holdout_looks_budget: number;
}
export interface CampaignRunRecord {
  run_record_id: string; campaign_id: string; source: string; search_id: string; protocol_id: string; name?: string | null;
  protocol_version: number; manifest_id: string; families: string[]; all_families: boolean | null; n_scope: number;
  scope_kind: "all" | "families" | "strategies" | null; scope_file: string | null;
  created_at: string; started_at: string | null; finished_at: string | null; updated_at: string | null;
  status: string; phase: string; batch_status?: string; preflight_seconds: number | null; eta: Eta | null;
  current: { strategy_id: string; display_name?: string; family_id: string; timeframe: string; dataset_id: string; index: number; of: number; started_at: string } | null;
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
export interface CampaignListRow extends CampaignSummary { n_runs: number; latest_run: CampaignRunRecord | null; label?: string | null }
export interface CampaignFamily {
  family_id: string; name: string; group: string | null; hypothesis: string | null; n_strategies: number;
  timeframes: Record<string, number>; completed: number; failed: number; remaining: number;
}
export interface CampaignDetail extends CampaignSummary {
  families: CampaignFamily[]; runs: CampaignRunRecord[]; latest_run: CampaignRunRecord | null; eta: Eta;
  prop_results_available: boolean; data_line: string; note: string;
}
export interface TreeStrategy { strategy_id: string; display_name: string; timeframe: string; status: string; run_id: string | null }
export interface TreeFamily {
  family_id: string; name: string; group: string | null; hypothesis: string | null; n_strategies: number;
  completed: number; failed: number; strategies: TreeStrategy[];
}
export interface CampaignTree {
  campaign_id: string; n_strategies: number; families: TreeFamily[]; progress: ScopeProgress;
  datasets: Record<string, string>; data_line: string; note: string;
}
export interface FamilyResults {
  campaign_id: string; family_id: string; name: string; group: string | null; hypothesis: string | null; n: number;
  timeframes: Record<string, number>; completed: number; remaining: number; note: string;
  strategies: { strategy_id: string; logic_hash: string; name: string; display_name: string; explanation: string; timeframe: string;
                dataset_id: string; status: string; run_id: string | null; error: string | null; duration_s: number | null;
                result_available: boolean; headline: Record<string, number | string | null> }[];
}
export interface StrategyResult {
  campaign_id: string; strategy_id: string; family_id: string; family_name: string; status: string; error: string | null; run_id: string | null;
  presentation: { display_name: string; explanation: string; key_parameters: Record<string, unknown>; strategy_id: string;
                  logic_hash: string; definition_hash: string; machine_name: string | null };
  timeframe: string; dataset_id: string | null; sizing: Record<string, unknown>; account: { starting_equity: number; currency: string };
  execution_contract: string; duration_s: number | null;
  provenance: Record<string, unknown>; metrics?: Record<string, unknown>; n_trades?: number; trades_hash?: string;
  prop?: { profiles: { profile: { profile_id: string; version: number }; status: string; final_status: string;
                       rule_basis_state?: string; summary?: Record<string, unknown> }[]; base?: Record<string, unknown> };
}
export type CampaignJobState = "queued" | "running" | "completed" | "failed" | "cancelled";
export interface CampaignJob {
  job_id: string; kind: "campaign"; campaign_id: string; search_id: string; families: string[] | null; n_strategy_ids: number | null;
  state: CampaignJobState; created_at: string; started_at: string | null; finished_at: string | null; error: string | null;
  cancel_requested: boolean; live: Partial<CampaignRunRecord> & { status: string; phase: string };
  processes?: number;                         // ADR-77: CPU cores computing strategies at once
}
export interface CheckReport { campaign_id: string; ready: boolean; note: string; checks: { check: string; ok: boolean; detail: unknown }[] }

const enc = encodeURIComponent;
export const campaigns = {
  listUrl: "/api/campaigns",
  detailUrl: (cid: string) => `/api/campaigns/${enc(cid)}`,
  treeUrl: (cid: string) => `/api/campaigns/${enc(cid)}/tree`,
  familyUrl: (cid: string, fid: string) => `/api/campaigns/${enc(cid)}/families/${enc(fid)}`,
  strategyUrl: (cid: string, sid: string) => `/api/campaigns/${enc(cid)}/strategies/${enc(sid)}`,
  check: (cid: string) => api.get<CheckReport>(`/api/campaigns/${enc(cid)}/check`),
  /** scope: null = everything; otherwise the frozen strategy ids selected in the tree. */
  start: (cid: string, strategyIds: string[] | null) =>
    api.post<CampaignJob>(`/api/campaigns/${enc(cid)}/jobs`, strategyIds === null ? { families: null } : { strategy_ids: strategyIds }),
  job: (jid: string) => api.get<CampaignJob>(`/api/campaigns/jobs/${enc(jid)}`),
  cancel: (jid: string) => api.post<CampaignJob>(`/api/campaigns/jobs/${enc(jid)}/cancel`),
  active: () => api.get<{ job: CampaignJob | null }>("/api/campaigns/active-job"),
  rename: (cid: string, rid: string, name: string) => api.post<{ name: string | null }>(`/api/campaigns/${enc(cid)}/runs/${enc(rid)}/name`, { name }),
  runScope: (cid: string, rid: string) => api.get<{ strategy_ids: string[] }>(`/api/campaigns/${enc(cid)}/runs/${enc(rid)}/scope`),
};
export const CAMPAIGN_JOB_FINAL = new Set(["completed", "failed", "cancelled"]);
export const LIVE_POLL_MS = 1000;

export const fmtDuration = (s: number | null | undefined): string => {
  if (s == null || !Number.isFinite(s)) return "—";
  const t = Math.max(0, Math.round(s));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), sec = t % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
};
