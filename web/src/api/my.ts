/** My strategy (ADR-93): every URL of the tab is built here. */
import { api } from "./client";

const enc = encodeURIComponent;

export type Candle = [number, number, number, number, number];   // [epoch seconds (UTC), open, high, low, close]

export interface SettingDef {
  key: string; group: string; label: string; type: "bool" | "int" | "float" | "choice" | "time" | "text";
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
  exported?: { at: string; file: string } | null;
}
export interface MonthRow { month: string; trades: number; wins: number; net_r: number; net_usd: number }
export interface TradeRow {
  trade_no: number; entry_ts: string; exit_ts: string; direction: number; entry_price_theo: number; stop_price: number;
  target_price: number; exit_price_theo: number; exit_reason: string; net_r: number; net_usd: number; contracts: number;
  risk_points: number; model?: string; checklist?: Record<string, boolean | null>; quality?: number;
  confirmation_tf?: string; r_planned?: number;
}
export interface Report extends ReportRow {
  settings: Record<string, unknown>; monthly: MonthRow[]; rule_stats: Record<string, number>; n_signals: number;
  skipped: Record<string, number>; causality_passed: boolean | null; run_id: string | null; trial_id: string | null;
  dataset: Record<string, unknown>; trades: TradeRow[]; decisions?: Record<string, { take: boolean }>; note?: string;
  review_id?: string;
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
  job: MyJob | null;
  review: { id: string; status: string; created_at: string; settings_hash: string; mechanical_report: string; final_report: string | null } | null;
}

export interface Candidate { signal_bar: number; signal_ts: string; explanation: Explanation; charts: string[]; candles: Record<string, Candle[]> }
export interface ReviewView {
  review: { id: string; created_at: string; status: string; settings_hash: string; window: { start: string; end: string };
    mechanical_report: string; final_report: string | null; settings_changed: Record<string, unknown> } | null;
  progress?: { decided: number; taken: number; skipped: number; taken_net_r: number; taken_wins: number };
  candidate?: Candidate | null; finished?: Report;
}
export interface Decision { signal_bar: number; take: boolean; outcome?: TradeRow & { candles: Record<string, Candle[]>; exit_bar: number } }

export interface PlanVariant { label: string; overrides: Record<string, unknown>; settings_hash: string }
export interface PlanResult { id: string; name: string; note?: string; created_at: string; variants: (PlanVariant & {
  backtest_id?: string; trade_count?: number; metrics?: Metrics; prop?: PropBrief | null; error?: { kind: string; message: string } })[];
  exported?: { at: string; file: string } }

export const my = {
  overviewUrl: "/api/my",
  settingsUrl: "/api/my/settings",
  reviewUrl: "/api/my/review",
  reportUrl: (id: string) => `/api/my/reports/${enc(id)}`,
  tradeUrl: (id: string, n: number) => `/api/my/reports/${enc(id)}/trades/${n}`,
  planResultUrl: (id: string) => `/api/my/plan-results/${enc(id)}`,
  saveSettings: (overrides: Record<string, unknown>) => api.post<SettingsPayload>("/api/my/settings", { overrides }),
  startBacktest: (b: { start?: string | null; end?: string | null; label?: string }) => api.post<MyJob>("/api/my/backtests", b),
  job: (id: string) => api.get<MyJob>(`/api/my/jobs/${enc(id)}`),
  startReview: () => api.post<MyJob>("/api/my/review", {}),
  decide: (signal_bar: number, take: boolean) => api.post<Decision>("/api/my/review/decide", { signal_bar, take }),
  exportReports: (report_ids: string[], include_candles: boolean) => api.post<ExportResult>("/api/my/export", { report_ids, include_candles }),
  openExports: () => api.post<{ folder: string }>("/api/my/exports/open", {}),
  checkPlan: (plan: unknown) => api.post<{ name?: string; note?: string; variants: PlanVariant[] }>("/api/my/plans/check", { plan }),
  runPlan: (plan: unknown) => api.post<MyJob>("/api/my/plans/run", { plan }),
};
