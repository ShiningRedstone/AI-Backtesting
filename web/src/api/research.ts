import { api } from "./client";
import type { JobStatus, Ranking, RankingMetric, SampleLabel, SearchBatch, SearchDetail, SearchPlan, SearchSpec,
  SearchValidation, Shortlist } from "./types";

/** Every /api/research call in one place (Phase 4). Pages never build these URLs themselves. */
export const research = {
  validate: (spec: SearchSpec) => api.post<SearchValidation>("/api/research/validate", { spec }),
  plan: (spec: SearchSpec) => api.post<SearchPlan>("/api/research/plan", { spec }),
  startJob: (spec: SearchSpec) => api.post<JobStatus>("/api/research/jobs", { spec }),
  job: (jobId: string) => api.get<JobStatus>(`/api/research/jobs/${encodeURIComponent(jobId)}`),
  cancelJob: (jobId: string) => api.post<JobStatus>(`/api/research/jobs/${encodeURIComponent(jobId)}/cancel`),
  searchesUrl: "/api/research/searches",
  searchUrl: (searchId: string) => `/api/research/searches/${encodeURIComponent(searchId)}`,
  searches: () => api.get<SearchBatch[]>("/api/research/searches"),
  search: (searchId: string) => api.get<SearchDetail>(research.searchUrl(searchId)),
  ranking: (searchId: string, metric?: RankingMetric, minSample?: SampleLabel) => {
    const q = new URLSearchParams();
    if (metric) q.set("metric", metric);
    if (minSample) q.set("min_sample_label", minSample);
    const qs = q.toString();
    return api.get<Ranking>(`${research.searchUrl(searchId)}/ranking${qs ? `?${qs}` : ""}`);
  },
  shortlist: (searchId: string, strategyIds: string[]) =>
    api.post<Shortlist & { search_id: string; duplicates_removed: number }>(`${research.searchUrl(searchId)}/shortlist`,
      { strategy_ids: strategyIds }),
};

/** Job states after which polling stops (plus a stored batch that a restart marked interrupted). */
export const JOB_FINAL = new Set(["completed", "failed", "cancelled", "interrupted"]);
export const POLL_MS = 2000;
