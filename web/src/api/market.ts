/** Market simulator (ADR-106): deep analysis of NQ / ES on the discovery period and live-knowledge 15-minute forecasts. */
import { api } from "./client";
import type { MyJob } from "./my";

const enc = encodeURIComponent;

export interface Rate { k: number; n: number; p: number | null; ci: [number, number] | null }
export interface Quant { q10?: number; q25?: number; q50?: number; q75?: number; q90?: number; n: number }
export interface Score { n: number; skill: number | null; skill_ci?: [number, number] | null; real?: boolean; accuracy?: number;
  brier?: number; brier_baseline?: number; mae?: number; mae_baseline?: number; confident_n?: number; confident_accuracy?: number | null;
  spread?: number; calibration?: { from: number; n: number; said: number; happened: number }[]; corr?: number | null }
export interface TargetEval { months?: string[]; chosen?: string | null; chosen_on?: string; reported_on?: string;
  scores?: Record<string, { all: Score | null; early: Score | null; late: Score | null }>;
  bands?: { inside_50: number; inside_80: number; n: number }; by_level?: { level: string; name: string; n: number; reached: number }[] }
export interface NewsStatus { key: { set: boolean; hint: string | null }; sources: string[]; default_source: string; downloaded: boolean;
  meta?: { source: string; sha256: string; downloaded_at: string; bytes: number }; events?: number; high?: number; first?: string; last?: string;
  timezone?: { zone: string | null; match?: number; anchors: number; reason?: string; candidates?: { zone: string; match: number }[] };
  refused?: { code: string; message: string } | null }
export interface AnalysisHead { key: string; computed_at: string; days: number;
  cost_points: { all: number; by_session: number[]; fixed: number } | number | null;
  source: { nq: { dataset_id: string; content_hash: string }; es: { content_hash: string; bars_in_window: number } | null;
    window: { start: string; end: string }; holdout_start: string };
  news: { used: boolean; events: number; high: number };
  edges: { cells_tested: number; passed_find: number; confirmed: number; failed: number; p_cut: number | null };
  patterns: number; forecast: Record<string, { chosen: string | null; late: Score | null }> }
export interface MarketStatus { news: NewsStatus; analysis: AnalysisHead | null; job: (MyJob & { what?: string }) | null;
  es: { imported: boolean; first_bar_open_utc?: string; last_bar_open_utc?: string }; protocol: boolean;
  newdays: { nq: { days: number; first: string | null; last: string | null }; es: { days: number; first: string | null; last: string | null };
    first_date?: string; problem?: string;
    scores: { days: string[]; candles: number; up: Record<string, Score | null>; size: Record<string, Score | null>; computed_at: string } | null } }
export interface PatternRow { kind: string; tf: number; dir: number; n: number; per_day: number; touched: number; ce: number; filled: number;
  filled_1h: number; filled_1d: number; filled_5d: number; median_touch_min: number | null; median_fill_min: number | null; left_behind: number;
  left_behind_median_age_days: number | null; held: number | null; held_n: number; edge: number | null; edge_n: number; median_size_atr: number | null;
  by_session: { session: string; n: number; edge: number | null; filled: number; held: number | null }[]; flags?: Record<string, { n: number; edge: number | null }> }
export interface EffectRow { kind: string; tf: string; dir: number; n: number; effects: { tf: string; same_way: number | null; n: number;
  size_median: number | null; size_q90: number | null }[]; m15_bos?: number; m15_4_atr?: { q25: number; q50: number; q75: number } | null }
export interface EdgeCell { kind: string; tf: string; dir: number; condition: string; outcome: "edge" | "next15" | "rest15"; n_find: number;
  rate_find: number; base_find: number; n_confirm: number; rate_confirm: number | null; base_confirm: number | null; p: number;
  p_confirm: number | null; direction?: string; breakeven: number | null; atr_pts: number | null; cost_pts?: number | null;
  tradeable?: boolean | null }
export interface EdgeGroup { kind: string; tf: string; outcome: EdgeCell["outcome"]; more_often: boolean; cells: number; best: EdgeCell;
  conditions: string[]; tradeable: boolean; dirs: number[] }
export interface Edges { cells_tested: number; passed_find: number; confirmed: number; failed: number; p_cut: number | null;
  candidates: EdgeCell[]; failed_confirm: EdgeCell[]; fdr_q: number; find_share: number; groups?: EdgeGroup[]; tradeable?: number }
export interface HoldoutModelScore { official_model: string; models: Record<string, Score | null>; official: Score | null;
  by_month: { month: string; n: number; skill: number | null; accuracy?: number | null }[]; bands?: { inside_50: number; inside_80: number; n: number } }
export interface HoldoutStatus { available: boolean; problem?: string; used?: boolean; holdout?: { start: string; end: string };
  look?: { access_id: string; status: string; created_at: string } | null;
  result?: { access_id: string; fingerprint: string; chosen: Record<string, string>; computed_at: string; candles: number; days: number;
    holdout: { start: string; end: string }; targets: Record<string, HoldoutModelScore>;
    levelmap?: Record<string, HoldoutModelScore> & { turn?: TurnScore; mistakes?: Record<string, Mistakes> };
    mistakes?: Record<string, Mistakes> } | null }
export interface MistakeRow { bucket: string; n: number; skill: number | null; accuracy?: number; said?: number; happened?: number;
  error?: number; error_baseline?: number; discovery_skill?: number | null }
export interface Mistakes { n: number; target?: string; model?: string; skill?: number | null; discovery_skill?: number | null; from?: string | null;
  groups?: { group: string; rows: MistakeRow[] }[]; findings?: { group: string; bucket: string; n: number; skill: number; text: string }[];
  worst?: { t: number; said: number; happened: number; baseline: number; level?: string; price?: number; dist?: number; context: Record<string, string> }[] }
export interface TurnScore { n: number; none_share?: number; model?: number; baseline?: number; nearest?: number;
  model_minus_baseline_ci?: [number, number]; real?: boolean }
export interface LevelKindRow { kind: string; kind_name: string; chart: string | null; n: number; reach2h: number; reach: number; react_n: number;
  react: number | null; median_dist: number }
export interface LevelMapSummary { missing?: boolean; decisions: number; levels: number; targets: Record<string, TargetEval> & { turn?: TurnScore };
  by_kind: LevelKindRow[]; mistakes: Record<string, Mistakes>; volatility_cuts: number[] }
export interface MapLevel { label: string; price: number; side: number; dist: number; kind: string; stack: number; p_reach2h: number | null;
  p_reach: number | null; p_react: number | null; base_reach: number | null; base_react: number | null; turn_pick: boolean; reached2h: boolean;
  reached: boolean; reacted: number | null; touch_ns: number | null }
export interface MapLanding { median: number | null; band80: (number | null)[]; band50: (number | null)[]; actual: number }
export interface MapMoment { t: number; px: number; atr15: number; levels: MapLevel[]; p_up_first: number | null; p_up_first_random_walk: number | null;
  up_first: number | null; turn_prob: Record<string, number>; land: Record<"land2h" | "land", MapLanding> }
export interface CallScore { candles: number; calls: number; share?: number; tau?: number; accuracy?: number; accuracy_ci?: [number, number] | null;
  baseline_accuracy?: number; up_calls?: number; vs_baseline_ci?: [number, number] | null; real?: boolean; beats_chance?: boolean }
export interface CallRule { tau: number | null; calls?: number; accuracy?: number; wilson_low?: number; share?: number; why?: string }
export interface DirectionStage extends TargetEval { calls?: { rule: CallRule; late: CallScore; all: CallScore } }
export interface DirectionSummary { version: number; analysis_key: string; computed_at: string; rows: number; inputs: string[];
  stages: Record<string, DirectionStage>; mistakes: Mistakes; top_inputs: { input: string; words: string; share: number }[] }
export interface DirectionHoldoutStage { official_model: string; models: Record<string, Score | null>; official: Score | null; calls: CallScore;
  by_month: { month: string; calls?: number; accuracy?: number | null }[] }
export interface DirectionHoldout { available: boolean; problem?: string; used?: boolean; holdout?: { start: string; end: string };
  look?: { access_id: string; status: string; created_at: string } | null; first_look?: { access_id: string; created_at: string } | null;
  result?: { access_id: string; fingerprint: string; computed_at: string; candles: number; days: number; second_look: boolean;
    rules: Record<string, { model: string; tau: number | null }>; stages: Record<string, DirectionHoldoutStage>; mistakes: Mistakes;
    discovery: Record<string, { calls: CallScore | null; skill: Score | null }> } | null }
export interface DirectionStatus { analysis: boolean; summary: DirectionSummary | null; holdout: DirectionHoldout; words: Record<string, string> }
export interface DayCall { p: number | null; called: boolean; ref: number; actual: number }
export interface Section<T> { key: string; name: string; data: T; kinds: Record<string, string>; sessions: string[] }

export interface DayCandle { t: number; o: number; h: number; l: number; c: number; p_up?: Record<string, number | null>;
  size?: Record<string, number | null>; size_q?: (number | null)[]; actual_size?: number | null; why?: { input: string | null; push: number | null }[] }
export interface DayLevel { t: number; level: string; name: string; price: number; reached: boolean; [model: string]: number | string | boolean | null }
export interface DayView { date: string; src: string; candles: DayCandle[]; bias: ({ t: number } & Record<string, number | null>)[];
  levels: DayLevel[]; news: { t: number; name: string; impact: number; forecast: number | null; actual: number | null; surprise_z: number | null }[];
  shocks: { start: number; end: number; main: string; move_pts: number; tfs: Record<string, number>; tags: { tag: string; detail?: string }[];
    m15_kept: number | null; minutes_to_return: number | null }[]; chosen: { up: string; size: string }; levelmap?: MapMoment[];
  direction?: Record<string, Record<string, DayCall>> }

export const market = {
  statusUrl: "/api/market",
  sectionUrl: (name: string) => `/api/market/section/${enc(name)}`,
  daysUrl: (src: string) => `/api/market/days?src=${enc(src)}`,
  dayUrl: (d: string, src: string) => `/api/market/day/${enc(d)}?src=${enc(src)}`,
  setKey: (key: string | null) => api.post<NewsStatus["key"]>("/api/market/news/key", { key }),
  downloadNews: (source: string) => api.post<MyJob>("/api/market/news/download", { source }),
  analyze: (force = false) => api.post<MyJob>("/api/market/analyze", { force }),
  newdays: () => api.post<MyJob>("/api/market/newdays", {}),
  directionUrl: "/api/market/direction",
  runDirection: (force = false) => api.post<MyJob>("/api/market/direction/run", { force }),
  runDirectionHoldout: (confirm: string) => api.post<MyJob>("/api/market/direction/holdout", { confirm }),
  job: (id: string) => api.get<MyJob & { done: boolean }>(`/api/market/jobs/${enc(id)}`),
  holdoutUrl: "/api/market/holdout",
  runHoldout: (confirm: string) => api.post<MyJob>("/api/market/holdout", { confirm }),
};
