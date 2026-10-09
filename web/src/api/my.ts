/** My strategy (ADR-93): every URL of the tab is built here. */
import { api } from "./client";

const enc = encodeURIComponent;

export type Candle = [number, number, number, number, number];   // [epoch seconds (UTC), open, high, low, close]

export interface SettingDef {
  key: string; group: string; label: string; type: "bool" | "int" | "float" | "choice" | "time";
  default: unknown; help: string; source: string; min?: number; max?: number; options?: string[]; unavailable?: string;
}
export interface SettingsPayload {
  schema: { version: number; groups: { id: string; label: string }[]; settings: SettingDef[] };
  overrides: Record<string, unknown>; resolved: Record<string, unknown>; settings_hash: string; problems: string[];
}

export interface ProtocolInfo {
  ready: boolean; problem?: string; parent_protocol_id?: string; parent_name?: string; protocol_id?: string | null;
  created?: boolean; discovery?: { start: string; end: string }; holdout?: { start: string; end: string };
  discovery_trading_dates?: string[]; holdout_trading_dates?: string[]; config_ok?: boolean;
  trial_budget?: number; holdout_looks?: number; trials_used?: number; holdout_looks_used?: number;
}

export interface Metrics {
  trade_count?: number; win_rate?: number | null; expectancy_r?: number | null; net_r?: number | null; net_usd?: number | null;
  trades_per_week?: number | null; profit_factor?: number | null; max_drawdown_r?: number | null; max_drawdown_usd?: number | null;
  months_losing?: number; months_total?: number; months_winning?: number; avg_planned_rr?: number | null;
  avg_win_r?: number | null; avg_loser_r?: number | null; avg_winner_r?: number | null; sample_label?: string;
  max_loss_streak?: number; max_win_streak?: number; exit_reasons?: Record<string, number>; breakeven_exits?: number;
  weekly?: { weeks?: number; mean?: number; median?: number; weeks_with_0?: number; weeks_3_to_5?: number; weeks_above_5?: number; max?: number };
}
export interface PropBrief { profile: string; status: string; evaluation: string; payouts: number | null; trader_payout: number | null; headline?: string[] }
export interface ReportRow {
  id: string; kind: string; label: string; created_at: string; settings_hash: string; window: { start: string; end: string };
  trade_count: number; prop: PropBrief | null; settings_changed: Record<string, unknown>; metrics: Metrics;
  exported?: { at: string; file: string } | null; favorite?: boolean; challenge?: ChallengeView | null; optimizer_run?: string | null;
}
export interface MonthRow { month: string; trades: number; wins: number; net_r: number; net_usd: number }
export interface TradeRow {
  trade_no: number; entry_ts: string; exit_ts: string; direction: number; entry_price_theo: number; stop_price: number;
  target_price: number; exit_price_theo: number; exit_reason: string; net_r: number; net_usd: number; contracts: number;
  risk_points: number; model?: string; checklist?: Record<string, boolean | null>; quality?: number;
  confirmation_tf?: string; r_planned?: number;
  phase?: "eval" | "funded"; session?: string; target_points?: number; attempt?: number;     // ADR-114 (Fair price)
}
export interface Report extends ReportRow {
  settings: Record<string, unknown>; monthly: MonthRow[]; rule_stats: Record<string, number>; n_signals: number;
  skipped: Record<string, number>; causality_passed: boolean | null; run_id: string | null; trial_id: string | null;
  dataset: Record<string, unknown>; trades: TradeRow[]; decisions?: Record<string, { take: boolean }>; note?: string;
  review_id?: string; criteria_profile?: string;
  /** ADR-114 (Fair price): each rule set traded on every day; the headline trades are the chain's under criteria_profile */
  phases?: Record<"eval" | "funded", { trade_count: number; metrics: Metrics; monthly: MonthRow[] }>;
}

export interface Zone { kind: string; tf: string; top: number; bottom: number; t_from: string; known_ts: string; ce?: number;
  from_gap?: { tf: string; top: number; bottom: number }; swept_inside_level?: number }
export interface Level { kind: string; tf?: string; price: number; top?: number; ts?: string | null; distance?: number; r?: number }
export interface Explanation {
  model: string; direction: number; signal_ts: string;
  bias: { direction: number; score: number | null; method: string; per_tf: Record<string, { score: number; weight: number; structure?: number;
    gaps?: { side: string; state: string; event_ts: string; top: number; bottom: number; t_from: string; tf: string }[] }> };
  draw: (Level & { distance: number }) | null;
  eq: { high: number; low: number; eq: number; ok: boolean; position: number } | null;
  key_levels: Zone[];
  leg: { start_ts: string; start_price: number; end_ts: string; end_price: number; size_points: number; swept: Level | null; equal: Level | null };
  confirmation: { tf: string; rule: string; level: number; gaps_per_tf: Record<string, number>; gaps: { top: number; bottom: number; t_from: string }[];
    candle_ts: string; candle: number[]; displacement: { body_ratio: number; range_x_avg: number; ok: boolean } };
  entry: { type: string; reference_price: number };
  stop: { mode: string; price: number; widened_to_minimum: boolean; risk_points: number };
  target: { price: number; r_planned: number; source: string | null; level: Level | null; candidates: Level[] };
  breakeven: { mode: string; level: number | null };
  chop: { gaps_created: number; gaps_closed_through: number; share: number; ok: boolean } | null;
  checklist: Record<string, boolean | null>; quality: number;
  flip?: { setup_direction: number; setup_stop: number; setup_target: number; setup_r_planned: number };   // ADR-97
  smt?: { divergence: boolean | null; reason: string; nq_took?: boolean; es_took?: boolean; tf?: string; ref_ts?: string;
    nq_ref?: number; nq_extreme?: number; es_ref?: number; es_extreme?: number | null };
}
export interface EsStatus {
  imported: boolean; es_id?: string; content_hash?: string; source_file?: string; source_sha256?: string; bars?: number;
  first_bar_open_utc?: string; last_bar_open_utc?: string; imported_at?: string; identity_status?: string;
  identity: { provider: string; instrument: string; feed: string; description: string; price_basis: string; timeframe: string;
    timestamps: string; volume: string; role: string };
}
export interface TradeDoc extends TradeRow {
  explanation: Explanation; charts: string[]; candles: Record<string, Candle[]>; backtest_id: string; count: number;
  signal_bar: number; exit_bar: number; final_stop_price?: number; mfe_r?: number; mae_r?: number;
}

export interface MyJob {
  job_id: string; kind: string; state: "running" | "completed" | "failed"; step: string; created_at: string;
  finished_at: string | null; result: unknown; error: { kind: string; message: string; trace?: string } | null;
}
export interface ExportResult { path: string; file: string; folder: string; bytes: number; reports: string[] }
export interface Overview {
  protocol: ProtocolInfo; settings_hash: string | null; settings_changed: Record<string, unknown>; backtests: ReportRow[];
  reports: ReportRow[]; plans: { id: string; name: string; created_at: string; variants: number; exported?: unknown }[];
  job: MyJob | null; es?: EsStatus | null; criteria_profile?: string;
  news?: { downloaded: boolean; events?: number; high?: number; first?: string | null; last?: string | null; refused?: unknown; zone?: string | null } | null;
  review: { id: string; status: string; created_at: string; settings_hash: string; mechanical_report: string; final_report: string | null } | null;
}

export interface Candidate { signal_bar: number; signal_ts: string; explanation: Explanation; charts: string[]; candles: Record<string, Candle[]> }
export interface ReviewView {
  review: { id: string; created_at: string; status: string; settings_hash: string; window: { start: string; end: string };
    mechanical_report: string; final_report: string | null; settings_changed: Record<string, unknown> } | null;
  progress?: { decided: number; taken: number; skipped: number; taken_net_r: number; taken_wins: number };
  candidate?: Candidate | null; finished?: Report;
}
/** ADR-102: one strategy, two holdout looks (automatic, then manual). */
export interface HoldoutCandidate { ref: string; source: "backtest" | "autotuner"; label: string; favorite: boolean; created_at: string | null;
  settings_hash: string; trade_count: number | null; metrics: Metrics | null; settings_changed: Record<string, unknown> | null }
export interface HoldoutSlot { n: number; source_label: string; settings_hash: string; settings_changed: Record<string, unknown>;
  automatic: { access_id: string; report: string; at: string; run_id: string }; manual: { access_id: string; review: string; at: string } | null;
  review_status: "in_progress" | "complete" | null; final_report: string | null }
export interface HoldoutAllowance extends ReviewView {
  ready: boolean; problem?: string; looks: string[]; max_strategies: number; protocol_id: string | null; config_ok?: boolean;
  holdout?: { start: string; end: string }; discovery?: { start: string; end: string };
  strategies: HoldoutSlot[]; review_strategy?: number; candidates: HoldoutCandidate[]; job: MyJob | null;
}
export interface Decision { signal_bar: number; take: boolean; r_planned?: number | null; outcome?: TradeRow & { candles: Record<string, Candle[]>; exit_bar: number } }

export interface PlanVariant { label: string; overrides: Record<string, unknown>; settings_hash: string }
export interface PlanResult { id: string; name: string; note?: string; created_at: string; variants: (PlanVariant & {
  backtest_id?: string; trade_count?: number; metrics?: Metrics; prop?: PropBrief | null; error?: { kind: string; message: string } })[];
  exported?: { at: string; file: string } }

/** Setup review on the discovery period (ADR-96). */
export interface SetupReviewRow { id: string; status: "in_progress" | "complete"; created_at: string; finished_at: string | null;
  base_report: string; base_label: string; settings_hash: string; exported: { at: string; file: string } | null; size: number; decided: number }
export interface SetupReviews { reviews: SetupReviewRow[]; open: SetupReviewRow | null; reasons: Record<string, string>; sample_size: number;
  backtests: ReportRow[] }
export interface SetupStats { trades: number; wins: number; win_rate: number | null; net_r: number; avg_r: number | null }
export interface SetupResultRow { trade_no: number; entry_ts: string; direction: number; model?: string; confirmation_tf?: string;
  quality?: number; r_planned?: number; take: boolean; reasons: string[]; note: string; net_r: number; net_usd?: number; exit_reason?: string }
export interface SetupProgress { size: number; decided: number; taken: number; skipped: number }
export interface SetupView {
  review: { id: string; status: "in_progress" | "complete"; created_at: string; finished_at: string | null; base_report: string;
    base_label: string; settings_hash: string; settings_changed: Record<string, unknown>; window: { start: string; end: string };
    sample: { size: number; of: number }; exported: { at: string; file: string } | null };
  progress: SetupProgress; reasons: Record<string, string>;
  candidate?: { trade_no: number; position: number; signal_bar: number; signal_ts: string; explanation: Explanation; charts: string[];
    candles: Record<string, Candle[]> } | null;
  results?: { all: SetupStats; taken: SetupStats; skipped: SetupStats; by_reason: (SetupStats & { reason: string; label: string })[];
    rows: SetupResultRow[]; note: string };
}

/** Prop challenge chain (ADR-101): fail -> next challenge, pass -> funded payouts, every fee deducted. */
export interface ChallengeSummary { challenges: number; fails: number; passes: number; funded_lost: number; funded_completed: number;
  payouts: number; trader_payouts: number; avg_payout_per_pass: number | null; fees_total: number; fees_complete: boolean;
  net: number | null; open_at_end: boolean; incompatible: boolean; stopped: string | null; dropped: number; error?: string }
export interface ChallengeView { profiles?: Record<string, ChallengeSummary | { error: string }>; start?: string; error?: string;
  names?: Record<string, string> }

/** Strategy autotuner (ADR-101): the step-by-step optimiser. */
export interface GoalCheck { goal: string; ok: boolean; short: number; value: number | null }
export interface PartScore { met: number; goals: number; short: number; prop_net: number | null; net_r: number | null; rows: GoalCheck[];
  chain: ChallengeSummary | null; key: number[] }
export interface OptBest {
  n: number; parent: number | null; change: { key: string; from: unknown; to: unknown } | null; overrides: Record<string, unknown>;
  settings_hash: string; trades_hash: string; found_at: string; tries_before: number; train: PartScore; check: PartScore;
  lookahead: "pending" | "passed" | "failed" | "not_checked"; lookahead_detail?: string; status: "best" | "discarded" | "failed_lookahead";
  full: null | { metrics: Metrics; monthly?: MonthRow[]; weekly?: Metrics["weekly"]; chains: Record<string, ChallengeSummary | { error: string } | null>;
    score: PartScore };
}
export interface OptTryBrief { met: number; goals: number; short: number; prop_net: number | null; net_r: number | null; payouts: number | null;
  passes: number | null; fails: number | null }
export interface OptTry { i: number; best: number; key: string | null; from: unknown; to: unknown; settings_hash: string; accepted: boolean;
  rejected_by_check: boolean; reused: boolean; train: OptTryBrief; check: OptTryBrief; trades: { train: number | null; check: number | null };
  win_rate: { train: number | null; check: number | null }; duration_s: number | null }
export interface OptLive { running: boolean; stopping?: boolean; step?: string; started_at?: string; finished_at?: string | null; processes?: number;
  tries_now?: number; reused_now?: number; failed_now?: number; bests_now?: number; checks_pending?: number; run_id?: string | null;
  start_id?: string; error?: { kind: string; message: string; trace?: string } | null; memory_note?: string; last_duration_s?: number;
  restarts?: number; current_best?: number; neighbourhood?: number }
export interface OptRunBrief { id: string; created_at: string; finished_at: string | null; status: string; stop_reason: string | null; tries: number;
  reused: number; failed: number; rejected_by_check: number; max_tries: number; profile: string; profile_name: string;
  final_backtest: string | null; error: { kind: string; message: string } | null; start: { id: string; label: string; overrides: Record<string, unknown> };
  bests: number; start_full: OptBest["full"]; final_full: OptBest["full"]; final_n: number | null }
export interface OptRun extends Omit<OptRunBrief, "bests" | "start_full" | "final_full" | "final_n"> {
  goals: Record<string, { on: boolean; value?: number }>; fees: Record<string, number | null>; window: { start: string; end: string; split: string; train_share: number };
  bests: OptBest[]; final?: number | null; current?: number | null; tries_log: OptTry[]; labels: Record<string, string>;
  saved: { id: string; best: number; created_at: string }[]; live?: OptLive;
  changes?: { key: string; label: string; from: unknown; to: unknown }[]; final_error?: string; memory_plan?: Record<string, unknown>; batch: number;
}
export interface OptStatus {
  protocol: { ready: boolean; problem?: string; protocol_id?: string | null; created?: boolean; discovery?: { start: string; end: string };
    config_ok?: boolean; trial_budget?: number; holdout_looks?: number; trials_used?: number };
  run: OptLive; runs: OptRunBrief[]; starts: (ReportRow & { challenge: ChallengeView | null })[]; es: EsStatus | null; cpu_count: number;
  processes_default: number; context: null | { goals: Record<string, { on: boolean; value?: number }>; profile: string; profile_name: string;
    fees: Record<string, number | null> }; context_problem: { kind: string; message: string } | null; default_max_tries: number; batch: number;
  train_share: number; fixed: string[];
}

/** Every URL of a My strategy-style tab (ADR-114: base base = My strategy, "/api/fair" = Fair price). */
export function makeMy(base: string) {
  return {
    overviewUrl: base,
    settingsUrl: base + "/settings",
    esUrl: base + "/es",
    importEs: (path: string, identity_confirmed: boolean) => api.post<MyJob>(base + "/es/import", { path, identity_confirmed }),
    reviewUrl: base + "/review",
    holdoutUrl: base + "/holdout",
    holdoutAutomatic: (ref: string) => api.post<MyJob>(base + "/holdout/automatic", { ref }),
    holdoutManual: (strategy: number) => api.post<{ id: string }>(base + "/holdout/manual", { strategy }),
    reportUrl: (id: string) => `${base}/reports/${enc(id)}`,
    tradeUrl: (id: string, n: number) => `${base}/reports/${enc(id)}/trades/${n}`,
    planResultUrl: (id: string) => `${base}/plan-results/${enc(id)}`,
    saveSettings: (overrides: Record<string, unknown>) => api.post<SettingsPayload>(base + "/settings", { overrides }),
    startBacktest: (b: { start?: string | null; end?: string | null; label?: string }) => api.post<MyJob>(base + "/backtests", b),
    job: (id: string) => api.get<MyJob>(`${base}/jobs/${enc(id)}`),
    startReview: () => api.post<MyJob>(base + "/review", {}),
    decide: (signal_bar: number, take: boolean) => api.post<Decision>(base + "/review/decide", { signal_bar, take }),
    exportReports: (report_ids: string[], include_candles: boolean) => api.post<ExportResult>(base + "/export", { report_ids, include_candles }),
    openExports: () => api.post<{ folder: string }>(base + "/exports/open", {}),
    checkPlan: (plan: unknown) => api.post<{ name?: string; note?: string; variants: PlanVariant[] }>(base + "/plans/check", { plan }),
    runPlan: (plan: unknown) => api.post<MyJob>(base + "/plans/run", { plan }),
    setupReviewsUrl: base + "/setup-reviews",
    setupReviewUrl: (id: string) => `${base}/setup-reviews/${enc(id)}`,
    startSetupReview: (report_id: string) => api.post<{ id: string }>(base + "/setup-reviews", { report_id }),
    setupDecide: (id: string, trade_no: number, take: boolean, reasons: string[], note: string) =>
      api.post<{ trade_no: number; progress: SetupProgress }>(`${base}/setup-reviews/${enc(id)}/decide`, { trade_no, take, reasons, note }),
    autotuneUrl: base + "/autotune",
    autotuneRunUrl: (id: string) => `${base}/autotune/runs/${enc(id)}`,
    autotuneStart: (b: { start_id: string; processes: number; max_tries: number }) => api.post<OptLive>(base + "/autotune/start", b),
    autotuneStop: () => api.post<OptLive>(base + "/autotune/stop", {}),
    autotuneSave: (id: string, n: number) => api.post<MyJob>(`${base}/autotune/runs/${enc(id)}/bests/${n}/save`, {}),
    setMeta: (id: string, b: { favorite?: boolean; label?: string }) =>
      api.post<{ id: string; favorite: boolean; label: string }>(`${base}/reports/${enc(id)}/meta`, b),
    setupUndo: (id: string) => api.post<{ trade_no: number; progress: SetupProgress }>(`${base}/setup-reviews/${enc(id)}/undo`, {}),
  };
}

export type MyApi = ReturnType<typeof makeMy>;
export const my = makeMy("/api/my");
