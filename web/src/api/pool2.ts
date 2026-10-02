import { api } from "./client";

/** Strategy pool 2 (ADR-86): generate the second 10,000 strategies, then move research to the 20,000-strategy protocol. */
export interface Pool2Status {
  pool1: { manifest_id: string; n_strategies: number } | null;
  pool2: { manifest_id: string; n_strategies: number; n_families: number; seed: number; candidates_generated: number } | null;
  protocol: {
    protocol_id: string; name: string | null; trial_budget: number; trials_used: number;
    holdout_looks_budget: number; holdout_looks_used: number; discovery: string[]; holdout: string[];
  } | null;
  switched: boolean; switch_incomplete: boolean;
  old_campaign: { campaign_id: string; completed: number; remaining: number; failed: number } | null;
  campaigns_under_active: { campaign_id: string; manifest_id: string; n_strategies: number }[];
  blockers: string[]; warnings: string[]; confirm_word: string; total_budget: number;
}
export interface Pool2Job {
  job_id: string; kind: "pool2"; action: "generate" | "switch"; state: string; error: string | null;
  live: { phase: string; at?: string }; result: Record<string, unknown> | null;
  started_at: string | null; finished_at: string | null;
}

export const pool2 = {
  statusUrl: "/api/pool2",
  generate: () => api.post<Pool2Job>("/api/pool2/generate", {}),
  switchProtocol: (confirm: string) => api.post<Pool2Job>("/api/pool2/switch", { confirm }),
  job: (id: string) => api.get<Pool2Job>(`/api/pool2/jobs/${encodeURIComponent(id)}`),
};
