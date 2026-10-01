// Shapes of the JSON returned by the Python API (edgelab/web/app.py -> edgelab/services.py).
export interface Issue { path: string; message: string; hint: string; severity: "error" | "warning" }
export interface Identity { strategy_id: string; logic_hash: string; definition_hash: string }

export interface FeatureParam { name: string; type: "int" | "float" | "str" | "bool"; default: unknown; doc: string; min?: number; choices?: unknown[] }
export interface FeatureInfo {
  id: string; version: number; category: string; summary: string; calculation: string; edge_cases: string;
  warmup: string; known_at: string; causal: boolean; requires: string[]; params: FeatureParam[];
  session_params: string[]; outputs: { name: string; doc: string }[];
}
export interface SessionDef { name: string; timezone: string; start: string; end: string; weekdays: number[] }
export interface BuilderOptions {
  dsl_version: number; timeframes: string[]; htf_options: Record<string, string[]>;
  comparison_operators: string[]; bar_fields: string[]; arithmetic: string[]; directions: string[];
  entry_order_types: string[]; stop_types: string[]; target_types: string[]; sizing_modes: string[];
  parameter_types: string[]; weekdays: string[]; unsupported: Record<string, string>;
  sessions: Record<string, SessionDef>; features: FeatureInfo[];
  session_flatten: Record<string, unknown>; variation_modes: string[];
}
export interface RenderResult {
  yaml: string; valid: boolean; errors: Issue[]; warnings: Issue[];
  canonical: Record<string, unknown> | null; canonical_yaml: string | null; identity: Identity | null;
}
export interface LibraryRow {
  strategy_id: string; name: string; family_id: string; generation_method: string; display_name?: string; short_name?: string;
  parent_strategy_id: string | null; generation_batch_id: string | null; created_at: string | null;
  timeframe: string | null; n_parameters: number; n_lineage_records: number; archived: boolean;
}
export interface Change { parameter: string; old: unknown; new: unknown; category: string }
export interface LineageRecord {
  strategy_id: string; generation_method: string; parent_strategy_id: string | null; changes: Change[];
  generation_batch_id: string | null; generation_timestamp: string; generation_parameters: Record<string, unknown>;
  versions: Record<string, unknown>;
}
export interface StoredStrategy extends Identity {
  name: string; family_id: string; definition: Record<string, unknown>; lineage: LineageRecord[]; archived?: boolean;
}
export interface LineageResponse {
  strategy_id: string; records: LineageRecord[];
  ancestry: { strategy_id: string; generation_method: string; parent_strategy_id: string | null; changes: Change[] }[];
  children: string[];
}
export interface FamilyNode extends LibraryRow { parents: string[]; changes: Change[] }
export interface FamilyDetail {
  family_id: string; family: { id?: string; name?: string; hypothesis?: string; category?: string };
  instances: FamilyNode[]; roots: string[];
}
export interface SaveResult extends Identity { created: boolean; note: string | null }
export interface ExplainResult { identity: Identity; explain: string; canonical_definition: Record<string, unknown> }
export interface VariationPreview {
  ok: boolean; errors: Issue[]; warnings?: Issue[]; max_variants?: number; mode?: string;
  combinations?: number; full_grid?: number; values?: Record<string, unknown[]>;
  combinations_list?: Record<string, unknown>[];
}
export interface VariantRow { strategy_id: string; name: string; overrides: Record<string, unknown>; changes: Change[] }
export interface VariationResult {
  batch_id: string; base_strategy_id: string; generated: number; duplicates_removed: number; same_as_base: number;
  combinations: number; varied_parameters: string[]; variants: VariantRow[];
  duplicates: { combination: Record<string, unknown>; duplicate_of: string }[];
  same_as_base_combinations: { combination: Record<string, unknown>; strategy_id: string }[]; saved: boolean;
}
export interface BatchRow {
  batch_id: string; created_at: string; base_strategy_id: string; base_name: string; spec_name: string;
  mode: string; combinations: number; generated: number; duplicates: number; same_as_base: number;
}
export interface BatchDetail {
  batch_id: string; created_at: string; base: Identity & { definition: Record<string, unknown> };
  spec: { mode: string; max_variants: number; dimensions: { parameter: string; values: unknown[]; category: string }[] } & Record<string, unknown>;
  combinations: number; generated: number; duplicates: { combination: Record<string, unknown>; duplicate_of: string }[];
  same_as_base: { combination: Record<string, unknown>; strategy_id: string }[];
  children_detail: (VariantRow & { archived?: boolean; missing?: boolean })[];
  generator_version: string; compiler_version: string; dsl_version: number;
}
export interface DatasetRow {
  dataset_id: string; dataset_name: string; provider: string; asset_type: string; instrument: string; symbol: string;
  timeframe: string; start: string; end: string; n_bars: number; quality_status: string; content_hash: string;
  volume_type: string; has_spread: boolean; price_basis: string; parent_dataset_id: string | null; missing_bars: number;
  cost: { status: string; profile?: string | null; reason?: string }; synthetic: boolean; limitations: string[];
  runnable: boolean; reasons: string[]; preferred?: boolean; identity?: InstrumentIdentity;
}
export interface Readiness { strategy_timeframe: string | null; datasets: DatasetRow[]; preferred_dataset_id?: string | null }
export interface InstrumentIdentity {
  identity_status: string; research_proxy?: boolean; source_provider?: string | null; source_symbol?: string | null;
  asset_class?: string; exchange?: string; price_source?: string | null; identity_evidence?: string | null;
  required_metadata?: string[]; missing_metadata?: string[]; point_value?: number; tick_size?: number;
  description?: string; problem?: string | null; source_feed_code?: string | null; price_basis?: string | null;
  volume_semantics?: string | null; economics?: string | null; calendar?: string; calendar_status?: string | null;
}
export interface PreferredDataset {
  preferred: { dataset_id: string; set_at: string; content_hash: string; manifest_hash: string; provider: string;
    instrument: string; timeframe: string; quality_status: string; synthetic: boolean } | null;
  state: "unset" | "set" | "missing"; dataset: DatasetRow | null; stored_at: string;
  history: { dataset_id: string | null; set_at: string; previous: string | null }[]; note: string;
}
export interface GapReport {
  dataset_id: string; calendar: string; timeframe_minutes: number;
  summary: { expected_bars: number; present_in_session: number; missing_bars: number; missing_ratio: number; n_gaps: number;
    by_length: Record<string, number>; by_position: Record<string, number>; by_weekday: Record<string, number> };
  coverage: { first_bar: string; last_bar: string; expected_trading_days: number; trading_days_with_bars: number;
    missing_trading_days: string[]; by_year: Record<string, { expected: number; present: number; coverage: number | null }> };
  largest_gaps: { start: string; end: string; missing_bars: number; trading_date: string; weekday: string;
    length_class: string; position: string; likely: string }[];
  gaps_listed: number; note: string;
}

// ------------------------------------------------------------------ AI Discovery (Phase 9)
export interface AiStatus {
  configured_provider: string; model: string | null; external_configured: boolean; api_key_present: boolean;
  problem: string | null; available: string[]; offline_notice: string | null; request_version: number;
  proposal_schema_version: number; modes: string[]; templates: Record<string, string>; max_proposals: number;
  features: string[]; sessions: string[]; directions: string[]; entry_orders: string[]; stop_types: string[];
  target_types: string[]; sizing_modes: string[]; exit_kinds: string[]; preferred_dataset_id: string | null;
}
export interface GateStage { stage: string; status: "passed" | "failed" | "not_run"; reasons: string[] }
export interface GateReport {
  status: "valid" | "rejected"; stages: GateStage[]; rejection_reasons: string[]; warnings: string[];
  identity: { strategy_id: string; logic_hash: string; definition_hash: string } | null;
  summary: {
    name: string; family_id: string; timeframe: string; session: string | null; direction: string; entry_order: string;
    entry: Record<string, unknown>; exit: Record<string, unknown>; stop: Record<string, unknown> | null;
    target: Record<string, unknown> | null; sizing: Record<string, unknown>; parameters: Record<string, unknown>;
    features: string[]; complexity: { conditions: number; parameters: number; features: number };
    canonical_definition: Record<string, unknown>; computed_changes?: string[];
  } | null;
}
export interface AiDecision {
  proposal_id: string; decision?: "accepted" | "rejected"; note?: string; decided_at?: string;
  saved_strategy_id?: string; saved_at?: string; history: Record<string, unknown>[];
}
export interface AiProposal {
  proposal_id: string; schema_version: number; request_id: string; generation_id: string; index: number;
  provider: { kind: string; name: string; model: string | null; provider_version: string; external: boolean };
  generation: { created_at: string; raw_output_sha256: string; context_hash: string; context_version: number; config_hash: string };
  parent: { strategy_id: string; logic_hash: string; definition_hash: string } | null;
  content: Record<string, any>; gate: GateReport; decision: AiDecision | null;
}
export interface AiGeneration {
  generation_id: string; request_id: string; created_at: string; request: Record<string, any>;
  scope: { dataset_id: string; instrument: string; timeframe: string; session: string | null; direction: string | null;
    date_scope: { start: string | null; end: string | null } | null; dataset_is_preferred: boolean };
  context_hash: string; context_version: number; provider: AiProposal["provider"]; raw_output_sha256: string;
  provider_notes: string[]; dropped_beyond_bound: number; proposals: AiProposal[]; note: string;
}
export interface AiGenerationRow {
  generation_id: string; request_id: string; created_at: string; mode: string; provider: AiProposal["provider"];
  scope: AiGeneration["scope"]; n_proposals: number; n_valid: number;
}
export interface BacktestResult {
  strategy_id: string; dataset_id: string; run_id: string | null; synthetic: boolean; exit_reasons: Record<string, number>;
  n_signals: number; trades_hash: string; dataset: Record<string, unknown>; cost_status: string;
  metrics: Record<string, unknown>; signal_diagnostics: Record<string, number>; skipped: Record<string, number>; note: string;
}
export interface RunRow {
  run_id: string; created_at: string; status: string; strategy_id: string; dataset_id: string; trades_hash: string;
  strategy_name: string | null; notes: string; synthetic: boolean; instrument: string; timeframe: string;
  headline_metrics: Record<string, unknown>;
}
export interface RunDetail { record: Record<string, unknown>; synthetic: boolean; n_trades: number; trades_shown: number; trades: Record<string, unknown>[] }
export interface SystemStatus {
  backend: string; demo: boolean; root: string; code_version: { git_commit?: string; source_sha256?: string; dirty?: boolean };
  config_hash: string; store_backend: string; datasets: number; strategies: number; archived_strategies: number;
  families: number; variation_batches: number; runs: number; last_run: Record<string, unknown> | null; test_status: string | null;
}

// ------------------------------------------------------------------ research (Phase 4: /api/research)
export type RankingMetric = "expectancy_r" | "profit_factor" | "net_r";
export type SampleLabel = "LOW SAMPLE SIZE" | "MODERATE SAMPLE" | "ADEQUATE SAMPLE";
export interface SearchSpec {
  search_spec_version?: number;
  strategies: { ids?: string[]; variation_batches?: string[]; proposal_batches?: string[]; families?: string[] };
  datasets: string[];
  period?: "common" | { start: string; end: string } | null;
  ranking?: { metric?: RankingMetric; min_sample_label?: SampleLabel };
  max_cells?: number; seed?: number | null; workers?: number;
}
export interface SearchValidation { valid: boolean; errors: Issue[]; warnings: Issue[]; canonical?: SearchSpec; search_hash?: string }
export interface PlanCell {
  cell_id: string; plan_index: number; strategy_id: string; dataset_id: string; dataset_content_hash: string;
  strategy_timeframe: string | null; cost_status: string; synthetic: boolean; eligible: boolean; reasons: string[];
}
export interface SearchPlan {
  search_id: string; search_hash: string; config_hash: string; plan_hash: string; spec: SearchSpec;
  strategies: { strategy_id: string; timeframe: string | null; sources: string[] }[];
  period: { mode: string; start: string; end: string } | null; cells: PlanCell[];
  counts: { strategy_references: number; strategies: number; duplicate_references_collapsed: number;
            excluded_archived: number; datasets: number; planned: number; eligible: number; ineligible: number };
  excluded: { strategy_id: string; reason: string; sources: string[] }[]; warnings: string[]; note: string;
}
export type JobState = "queued" | "running" | "completed" | "failed" | "cancelled";
export interface JobProgress {
  stored: boolean; batch_status?: string; planned?: number; eligible?: number; ineligible?: number; evaluated?: number;
  failed?: number; skipped_resume?: number; cancelled?: number; trials?: number; pending?: number;
  cell_status?: Record<string, number>; fraction_done?: number | null;
}
export interface JobStatus {
  job_id: string; search_id: string; state: JobState; history: string[]; created_at: string; started_at: string | null;
  finished_at: string | null; error: string | null; cancel_requested: boolean; progress: JobProgress;
}
export interface SearchBatch {
  search_id: string; search_hash: string; config_hash: string; created_at: string; finished_at: string | null;
  status: string; spec: SearchSpec; shortlist: Shortlist | null; warnings: string[];
  n_planned: number; n_eligible: number; n_ineligible: number; n_evaluated: number; n_skipped_resume: number;
  n_failed: number; n_cancelled: number; n_trials: number; protocol_id?: string | null;
}
export interface SearchCell {
  search_id: string; cell_id: string; plan_index: number; strategy_id: string; dataset_id: string;
  dataset_content_hash: string; status: string; run_id: string | null; trades_hash: string | null;
  reasons: string[] | null; error: string | null; headline: Record<string, unknown> | null; current: boolean;
}
export interface SearchDetail extends SearchBatch {
  cells: SearchCell[]; historical_cells: SearchCell[];
  cumulative: Record<string, number>; note: string;
}
export interface RankedRow {
  rank: number; strategy_id: string; dataset_id: string; cell_id: string; run_id: string | null; trades_hash: string | null;
  value: number | null; value_infinite: boolean; metrics: Record<string, unknown>;
}
export interface Ranking {
  search_id: string; search_status: string; metric: RankingMetric; direction: string; min_sample_label: SampleLabel;
  n_trials: number; n_current_cells: number; n_ranked: number; excluded: Record<string, number>;
  in_sample: boolean; status: string; validated: boolean; ranked: RankedRow[]; label: string; note: string;
}
export interface Shortlist { strategy_ids: string[]; selected_at: string; in_sample: boolean; validated: boolean; note: string }

// ---------------------------------------------------------------- prop simulation (Phase 6)
export interface PropConfigRow {
  file: string; id: string | null; name: string; valid: boolean; errors: string[]; config_hash: string | null;
  synthetic_test_only: boolean | null; text: string;
}
export interface PropViolation { rule: string; at: string; trade_no: number; detail: string; detection: string }
export interface PropElapsed { at: string; calendar_days: number; trading_days: number }
export interface PropAccountSummary {
  account_id: string; prop_config_id: string; prop_config_hash: string; synthetic_test_only_rules: boolean;
  account_start: string | null; status: string; survived: boolean; starting_balance: number; ending_balance: number;
  net_pnl_usd: number; net_r: number; trade_count: number; trades_not_processed: number;
  trades_skipped_daily_loss_pause: number; target_usd: number | null; profit_target_reached: boolean;
  profit_target_balance_touched: boolean; drawdown_breach: boolean; daily_loss_breach: boolean; rule_violation: boolean;
  trading_days: number; max_drawdown_usd_closed: number; max_drawdown_usd_intratrade_bound: number | null;
  max_daily_loss_usd_closed: number; max_daily_loss_usd_intratrade_bound: number | null;
  time_to_target: PropElapsed | null; time_to_breach: PropElapsed | null; violation_reason: string | null;
  violations: PropViolation[]; incomplete_reasons: string[]; best_day_share_of_profit: number | null;
  payout_eligible: boolean | null; trades_crossing_reset: number; trades_end_of_data: number; detection: string;
}
export interface PropAccountResult { summary: PropAccountSummary; progression: Record<string, unknown>[]; days: Record<string, unknown>[] }
export interface PropSimulation {
  simulation_id: string; created_at: string; recorded: boolean; labels: string[];
  lineage: Record<string, unknown>; strategy_result: Record<string, unknown>;
  accounts: PropAccountResult[]; account_configs: { account_id: string; prop_config_id: string; prop_config_hash: string; start: string | null }[];
}
export interface PropSimRow {
  simulation_id: string; created_at: string; source_run_id: string; strategy_id: string; dataset_id: string;
  accounts: { account_id: string; prop_config_id: string; status: string }[];
}

// ---------------------------------------------------------------- strategy lab (Phase 8)
export interface RunSummary {
  run_id: string; created_at: string; status: string; scope: string; validated: boolean; synthetic: boolean; notes: string;
  strategy_id: string; strategy_name: string | null; definition_hash: string | null; logic_hash: string | null;
  parent_strategy_id: string | null; dataset_id: string; dataset_name: string | null; parent_dataset_id: string | null;
  provider: string; instrument: string; timeframe: string; period: { start: string; end: string };
  cost_profile: string | null; cost_status: string | null; config_hash: string; trades_hash: string;
  cost_scenario?: string | null; cost_basis?: string | null;
  metrics: Record<string, number | string | null>; prop_simulations: number;
}
export interface CompareRow extends RunSummary {
  breakeven_cost_multiplier: number | null; breakeven_note: string | null; parameters: Record<string, unknown>;
  generation_method: string | null; library_parent: string | null; changes: Change[]; generation_batch_id: string | null;
}
export interface Comparison { object: string; source: Record<string, unknown>; n_runs: number; rows: CompareRow[]; labels: string[] }
export interface StrategyResearch {
  object: string;
  strategy: { strategy_id: string; name: string; family_id: string; logic_hash: string; definition_hash: string; timeframe: string;
    dsl_version: number; parameters: Record<string, unknown>; archived: boolean };
  lineage: { generation_method: string | null; parent_strategy_id: string | null; parent_definition_hash: string | null;
    changes: Change[]; generation_parameters: Record<string, unknown>; generation_timestamp: string | null; records: number;
    generation_batch: { batch_id: string; kind?: string; spec?: Record<string, unknown>; missing?: boolean } | null;
    ancestry: string[]; children: string[] };
  variation_batches_from_this_strategy: string[];
  runs: RunSummary[];
  validation_state: { run_statuses: string[]; has_out_of_sample_runs: boolean; validated: boolean; note: string };
}
export interface CurvePoint { i: number; exit_ts: string; equity_r: number; drawdown_r: number }
export interface RunCurve { run_id: string; status: string; scope: string; n_trades: number; thinned?: boolean;
  final_net_r?: number; max_drawdown_r?: number; points: CurvePoint[]; note?: string }
export interface ValidationWindow {
  window: { role: string; start: string; end: string }; status: string; run_id: string | null; strategy_id: string;
  dataset_id: string; parent_dataset_id: string | null; cost_status: string; cost_profile: string; trades_hash: string;
  metrics: Record<string, unknown>; segment?: number; partial?: boolean;
}
export interface ValidationReport {
  validation: string; validation_id: string; strategy_id: string; definition_hash: string; dataset_id: string;
  cost_status: string[]; labels: string[]; windows: ValidationWindow[];
  monte_carlo_oos?: Record<string, Record<string, unknown>>; oos_pooled?: Record<string, unknown>;
  oos_segments?: Record<string, unknown>[]; scheme?: string;
}
export interface ControlReport {
  validation: string; validation_id: string; sample_status: string; labels: string[]; stored_as_runs: boolean;
  candidate: { strategy_id: string; definition_hash: string; trades_hash: string; pre_cooldown_signals: number; signals: number;
    metrics: Record<string, unknown> };
  dataset: Record<string, unknown>; cost_profile: string; cost_status: string;
  control_config: { method: string; n_controls: number; base_seed: number; period: string[] | null };
  comparison: Record<string, any>;  // eslint-disable-line @typescript-eslint/no-explicit-any
  realizations: Record<string, unknown>[]; oos_window?: { split_at: string; start: string; end: string };
}
export interface ProposalReport {
  batch_id: string; n_accepted: number; n_rejected: number; saved: boolean; warnings: unknown[];
  accepted: { strategy_id: string; name?: string; family_id?: string; [k: string]: unknown }[];
  rejected: { index?: number; name?: string; reasons?: unknown; [k: string]: unknown }[];
}

// ---------------------------------------------------------------- research workspace
export interface WorkspaceInfo {
  path: string; exists: boolean; valid: boolean; empty?: boolean; has_store: boolean; store_backend: string | null;
  store_path: string | null; data_root?: string; writable: boolean; demo: boolean; datasets: number; runs: number;
  strategies: number; prop_simulations: number; has_feature_cache: boolean; problems: string[]; source?: string;
}
export interface WorkspaceState {
  current: WorkspaceInfo | null; switchable: boolean; notice: string | null; settings_path: string | null;
  default: { path: string; info: WorkspaceInfo } | null; browse_available: boolean;
}

// ---------------------------------------------------------------- research terminal read models (research/overview.py)
/* eslint-disable @typescript-eslint/no-explicit-any */
export type Num = number | null;
export interface Hist { edges: number[]; counts: number[]; n: number; clipped: number; median?: number; mean?: number }
export interface ProtocolStatus {
  protocol_id: string; status: string; error?: string; scope: { instrument: string; provider: string; timeframe: string };
  source_dataset: { dataset_id: string; content_hash: string; calendar: string; has_ask_ohlc: boolean; quality_status?: string };
  windows: Record<"discovery" | "holdout", { trading_dates: [string, string]; first_bar: string; last_bar: string; n_bars: number }>;
  trials: { unique_numerical_trials: number; budget: number; remaining: number; unique_logic_hashes: number; evaluation_events: number;
    duplicate_events: number; failed_events: number; by_entry_point: Record<string, number>; by_family: Record<string, number> };
  proposal_attempts: { total: number; by_source: Record<string, number>; by_gate_status: Record<string, number>; distinct_logic_hashes: number };
  holdout: { looks_used: number; budget: number; remaining: number; refusals: number; evaluated_strategy_ids: string[] };
  multiple_testing: { family_size: number; per_test_alpha: number; familywise_alpha: number; z_one_sided: number; effect: string; method?: string };
  pre_protocol_exposure: { statement: string; runs: { run_id: string }[] };
}
export interface ProtocolRecordRow { protocol_id: string; status: string; created_at: string; name: string;
  scope: { instrument: string; provider: string; timeframe: string }; protocol_version?: number;
  source_dataset_id?: string; discovery_trading_dates?: string[]; holdout_trading_dates?: string[];
  discovery_period?: { start: string; end: string } }
export interface ExecutionModel {
  status: string; instrument: string; provider?: string; reason?: string; cost_scenario?: string; cost_profile?: string;
  spread_source?: string; quote_model?: string; has_ask_ohlc?: boolean; spread_treatment?: string;
  quote_sides?: { long_entry: string; long_exit: string; short_entry: string; short_exit: string } | null;
  cost_model?: Record<string, unknown>;
}
export interface OverviewRun { run_id: string; created_at: string; status: string; scope: string; holdout?: boolean; strategy_id: string; strategy_name: string | null;
  dataset_id: string; trade_count: number; expectancy_r: Num; net_r: Num; profit_factor: Num; max_drawdown_r: Num; synthetic: boolean;
  cost_status: string | null }
export interface Overview {
  facts: { strategies: number; families: number; runs: number; runs_by_status: Record<string, number>; searches: number;
    variation_batches: number; prop_simulations: number; ai_generations: number; datasets: number; store_backend: string };
  protocols: ProtocolStatus[]; protocol_records: ProtocolRecordRow[];
  dataset: { manifest: Record<string, any>; manifest_hash: string; identity: Record<string, any>; limitations: string[];
    validation_report: Record<string, any> | null; error?: string; dataset_id?: string } | null;
  dataset_source: string | null; execution: ExecutionModel | null;
  recent_runs: OverviewRun[];
  recent_searches: { search_id: string; created_at: string; status: string; n_trials: number; n_evaluated: number; n_failed: number;
    protocol_id: string | null }[];
  candidates: { strategy_id: string; protocol_id: string | null; status: string; reason_code?: string | null; outcome?: string | null;
    created_at?: string; run_id?: string | null; search_id?: string }[];
  warnings: { level: "info" | "warn"; text: string }[]; code_version: Record<string, unknown>; note: string;
}
export interface ExplorerRow {
  strategy_id: string; name: string | null; family_id: string | null; family_name: string | null; category: string | null;
  hypothesis: string | null; timeframe: string | null; session: string | null; direction: string | null; entry_type: string;
  stop_type: string | null; target_type: string; source: string | null; proposal_id: string | null; created_at: string | null;
  logic_hash: string | null; state: string; state_label: string; n_runs: number; n_oos_runs: number; protocols: string[];
  oos_expectancy_r: Num; oos_run_id: string | null; holdout_outcome: string | null; holdout_random_control_p: Num;
  ref_run: { run_id: string; status: string; scope: string; dataset_id: string; instrument: string; synthetic: boolean;
    cost_status: string | null; start: string; end: string } | null;
  instrument: string | null; trade_count: number | null; trades_per_week: Num; win_rate: Num; expectancy_r: Num;
  gross_r_per_trade: Num; net_r: Num; profit_factor: Num; max_drawdown_r: Num; cost_r_per_trade: Num;
  sample_label: string | null; synthetic: boolean | null;
  target_multiple?: number | null; trailing?: string | null; signal_exit?: string | null;
  avg_rr?: Num; max_loss_streak?: number | null; avg_hold_minutes?: Num; prop_pass_eval?: boolean | null; prop_pass_payout?: boolean | null;
  survivor?: boolean; favorite?: boolean; display_name?: string; short_name?: string;
}
export interface ExplorerResponse {
  rows: ExplorerRow[]; total: number; page: number; page_size: number; pages: number; scope: string; scope_label: string;
  sort: string; order: "asc" | "desc"; facets: Record<string, string[]>; states: Record<string, string>;
  protocols: ProtocolRecordRow[]; library_total: number; basis: string; note: string;
}
export interface GroupRow { group: string; runs: number; trades: number; net_r_per_trade: Num; gross_r_per_trade: Num;
  median_run_expectancy_r: Num; pct_runs_positive_net: Num }
export interface CalendarRow { bucket: string; trades: number; net_r: number; net_r_per_trade: Num; gross_r_per_trade: Num }
export interface ResearchDashboard {
  scope: string; scope_label: string; basis: string; n_runs: number; n_strategies: number; n_trades: number;
  synthetic_excluded: number; includes_synthetic: boolean; holdout_runs_excluded: number; breakdowns: Record<string, GroupRow[]>;
  distributions: Record<string, Hist>; pct_runs_positive_net: Num;
  cost_share: { gross_r: number; cost_r: number; net_r: number; trades: number; cost_r_per_trade: Num };
  calendar: { weekday: CalendarRow[]; month: CalendarRow[]; year: CalendarRow[]; runs_pooled: number; timezone?: string; note?: string };
  filters: { instruments: string[]; datasets: string[]; families: string[] }; note: string;
}
export interface BucketRow { bucket: string; trade_count: number; sample_label: string; gross_r?: Num; net_r?: Num; cost_r?: Num;
  expectancy_r?: Num; profit_factor?: Num; win_rate?: Num; [k: string]: unknown }
export interface RunAnalytics {
  run_id: string; run: Record<string, any>; labels: string[]; note: string; n_trades: number;
  basis: { net: string; gross: string };
  metrics?: { net: Record<string, any>; gross: Record<string, any> };
  curve?: RunCurve; rolling_expectancy?: { window: number; points: { i: number; exit_ts: string; value: number }[]; basis: string };
  direction?: BucketRow[]; year?: BucketRow[]; month?: BucketRow[]; weekday?: BucketRow[];
  session?: { note: string; rows: (BucketRow & { session: string; window: string | null })[] };
  hour?: { timezone: string; rows: BucketRow[] }; exit_reason?: BucketRow[]; entry_type?: BucketRow[];
  monthly_heatmap?: { years: string[]; months: string[]; cells: Record<string, Record<string, { net_r: number; trades: number }>> };
  r_histogram?: { net: Hist; gross: Hist }; holding_minutes?: Hist;
  win_loss?: { wins: number; losses: number; flat: number };
  streaks?: { wins: Record<string, number>; losses: Record<string, number> };
  trades_per_month?: { month: string; trades: number }[];
  cost_sensitivity?: { rows: Record<string, unknown>[]; breakeven_cost_multiplier?: Num };
  breakeven_cost_multiplier?: Num;
  monte_carlo?: Record<string, Record<string, any>>;
  monte_carlo_paths?: { paths: number[][]; fan: Record<string, number[]>; index?: number[]; observed?: number[]; n_sims: number;
    seed?: number; method?: string; note?: string };
  quote_sides?: { counts: Record<string, number>; rule: string } | null;
  mfe_mae?: { avg_mfe_r: Num; avg_mae_r: Num };
}
export interface PipelineStage { id: string; label: string; state: "done" | "failed" | "refused" | "pending" | "not_recorded" | "not_available";
  evidence: string }
export interface HoldoutAccess { protocol_id: string; access_id: string; status: string; reason_code: string | null; run_id: string | null;
  outcome: string | null; random_control?: Record<string, any> | null; created_at: string; search_id?: string | null }
export interface StrategyPipeline { strategy_id: string; stages: PipelineStage[]; furthest_stage: string | null; note: string;
  holdout: HoldoutAccess[]; shortlists: { search_id: string; protocol_id: string | null }[] }
export interface PipelineBoard {
  stages: { id: string; label: string; counts: Record<string, number> }[];
  candidates: { strategy_id: string; name: string | null; family_id: string | null; state: string; state_label: string; stages: PipelineStage[] }[];
  n_strategies: number; protocols: ProtocolRecordRow[]; note: string;
}

// ---------------------------------------------------------------- version + updates
export interface AppVersion { version: string; packaged: boolean; build_id: string | null; git_commit: string | null; built_at: string | null; build: string }
export interface UpdateStatus {
  current_version: string; current_build?: number; channel?: string | null; current_key?: string;
  platform: string; source: string; auto_check: boolean; skipped_versions: string[];
  check: { state: "idle" | "checking" | "done" | "error"; checked_at: string | null; last_success_at?: string | null;
    error: { code: string; message: string } | null };
  release: { version: string; key?: string; build_number?: number; channel?: string | null; commit?: string | null;
    tag: string; published_at: string | null; notes: string; size: number; artifact: string; sha256: string;
    platform: string } | null;
  available: boolean; note: string | null; skipped: boolean; prompt: boolean;
  download: { state: "idle" | "downloading" | "verifying" | "ready" | "error"; bytes: number; total: number | null;
    error: { code: string; message: string } | null; version: string | null; staged?: string };
  install?: { state: "idle" | "running" | "up_to_date" | "applying" | "error"; step: "checking" | "downloading" | "applying" | null;
    version: string | null; error: { code: string; message: string } | null };
  apply_supported: boolean; apply_unsupported_reason: string | null; install_dir: string | null; cache_dir: string; log: string;
  last_update: Record<string, any> | null; applying?: boolean;
}
/* eslint-enable @typescript-eslint/no-explicit-any */

// ---------------------------------------------------------------- backtest results views (ADR-73, read-only)
export interface FieldPoint { strategy_id: string; name: string | null; display_name?: string | null; family_id: string | null; run_id: string; trades: number;
  synthetic: boolean; survivor: boolean; win_rate: Num; avg_rr: Num; expectancy_r: Num }
export interface ControlPoint { control_id: string; candidate_strategy_id: string | null; validation_id: string | null; realization: number;
  seed: number | null; trades: number; win_rate: Num; avg_rr: Num; expectancy_r: Num; gross_r_per_trade: Num; net_r: Num;
  max_drawdown_r: Num; max_loss_streak: number | null; dataset_id: string | null; synthetic: boolean; sample_status: string | null }
export interface CurvePoint { win_rate: number; avg_rr: number }
export interface BreakdownGroup { group: string; strategies: number; median_expectancy_r: Num; survivor_rate: number }
export interface ResultsOverview {
  scope: string; scope_label: string; basis: "net" | "gross"; basis_label: string;
  facts: { strategies: number; tested: number; survivors: number; gross_positive: number; net_positive: number; synthetic_tested: number;
    median_cost_r_per_trade: Num };
  points: FieldPoint[]; controls: ControlPoint[];
  breakeven: { zero: { label: string; points: CurvePoint[] }; after_cost?: { label: string; cost_r: number; points: CurvePoint[] }; note: string };
  breakdowns: Record<string, BreakdownGroup[]>;
  exit_comparison: { signal_exit: { strategies: number; median_expectancy_r: Num };
    fixed_target: { strategies: number; median_expectancy_r: Num; multiples: number[] }; difference_r: Num; label: string };
  eval_summary: { survivors: number; survivors_simulated: number; median_p_pass: Record<string, Num>; profile_names?: Record<string, string>;
    defaults: { replays: number; block_days: number; seed: number }; label: string };
  survivor_rule: string; note: string; criteria_profile?: string | null; campaign_run?: string | null;
}
export interface PropSummaryRow { profile_id: string | null; profile_name: string | null; version: number | null; status: string | null;
  evaluation: string | null; failure_reason: string | null; payouts: number; pass_days: number | null; passes_with_payout: boolean;
  rule_basis_state: string | null;
  bootstrap?: { p_pass: Num; p_first_payout: Num; p_evaluation_breach: Num; median_days_to_pass: Num; valid_replays: number } | null }
export interface PeriodStats { trades: number; net_r: Num; net_usd_at_risk: Num; expectancy_r: Num; win_rate: Num }
export interface YearRow extends PeriodStats { year: number; months: (PeriodStats & { month: string })[] }
export interface StrategyPanelData {
  strategy_id: string; display_name: string; explanation: string; family_id: string | null; family_name: string | null;
  facets: Record<string, unknown>; scope: string; risk_per_trade_usd: number; survivor_rule: string; tested: boolean;
  technical: Record<string, string | null | undefined>; rules: { rule: string; text: string }[];
  run_id?: string; synthetic?: boolean; status?: string; scope_label?: string; survivor?: boolean; cost_status?: string | null;
  kpis?: { expectancy_r: Num; trades: number; trades_per_week: Num; win_rate: Num; avg_rr: Num; net_r: Num; net_usd_at_risk: Num;
    max_drawdown_r: Num; max_drawdown_usd_at_risk: Num; max_loss_streak: number | null; cost_r_per_trade: Num; pct_weeks_with_trade: Num;
    weeks_in_data: number | null; avg_hold_minutes: Num; gross_r_per_trade: Num; profit_factor: Num; sample_label: string | null;
    net_usd_recorded: Num };
  years?: YearRow[];
  curve?: RunCurve;
  out_of_sample?: { run_id: string; status: string; scope: string; trades: number; expectancy_r: Num; net_r: Num; start: string; end: string;
    net_usd_at_risk: Num }[];
  holdout?: { run_id: string; scope: string; trades: number; expectancy_r: Num; net_r: Num }[];
  rank?: { position: number | null; of: number; by: string; label: string };
  prop?: PropSummaryRow[];
  dataset?: { instrument: string | null; provider: string | null; timeframe: string | null; start: string; end: string };
}
export interface ControlPanelData extends ControlPoint { kind: string; risk_per_trade_usd: number; net_usd_at_risk: Num; note: string }
export interface BootstrapResult { replays: number; valid_replays: number; p_pass: Num; p_first_payout: Num; p_evaluation_breach: Num;
  p_not_passed_by_end: Num; p_incompatible: Num; median_days_to_pass: Num; days_to_pass_p10_p90: [number, number] | null;
  failure_reasons: Record<string, number>; computed_at?: string;
  paths: { days?: number[]; percentiles?: Record<string, number[]>; samples?: number[][]; start_balance?: number | null;
    target?: number | null; max_loss?: number | null; note?: string } }
export interface BootstrapResponse { run_id: string; profile_id: string; profile_version: number | null; mode: string; mode_label: string;
  n: number; block_days: number; seed: number; key: string; method: string; label: string; modes: Record<string, string>;
  unsupported_modes: Record<string, string>; state: "running" | "done" | "error"; done?: number; total?: number; error?: string | null;
  result?: BootstrapResult }
export interface RiskPreference { risk_per_trade_usd: number; default: boolean; note: string }
export interface BacktestJob { job_id: string; state: "running" | "completed" | "failed"; dataset_id: string; created_at: string;
  finished_at: string | null; error: { kind: string; message: string } | null; result?: BacktestResult | null }

/* ---------------------------------------------------------------- paper trading (ADR-81) */
export interface PaperFees { eval_price: number | null; reset_fee: number | null; activation_fee: number | null }
export interface PaperFeedStatus {
  n_days: number; first_day: string | null; newest_day: string | null; checked_at: string | null; last_completed_date: string | null;
  errors: Record<string, string>; skipped: Record<string, string>; source: string; downloader_available: boolean; next_start_date: string;
  manager: { state: string; last_run: string | null; last_error: string | null; next_check: string | null; started?: string;
    last_result?: { accounts_updated: number } | null };
}
export interface PaperCandidate {
  strategy_id: string; display_name: string | null; family_id: string | null; timeframe: string | null; survivor: boolean;
  expectancy_r: number | null; trades: number; run_id: string | null; already_running: boolean;
}
export interface PaperCandidates { profile_id: string; show_all: boolean; strategies: PaperCandidate[]; n_survivors: number }
export interface PaperAccountRow {
  account_id: string; batch_id: string; created_at: string; strategy_id: string; display_name: string | null; profile_id: string;
  profile_version: number; start_date: string; status: "running" | "stopped"; stop_reason: string | null;
  state: "waiting" | "evaluation" | "funded" | "stopped"; current_attempt: number; attempts: number; passes: number; payouts: number;
  trader_payouts: number; fees_total: number; net: number; n_trades: number; balance: number | null; last_day: string | null;
  computed_at: string | null;
}
export interface PaperAttempt {
  n: number; start: string; end: string | null; fee: number | null; n_trades: number; stage: "evaluation" | "funded";
  status: "in_progress" | "failed" | "incompatible" | "funded" | "funded_lost" | "funded_completed";
  reason: string | null; detail: string | null; eval_profit?: number; eval_trading_days?: number; balance?: number | null;
  passed_at?: string | null; payouts?: number; trader_payout?: number;
}
export interface PaperTrade {
  trade_no?: number; entry_ts: string; exit_ts: string; direction: number; contracts: number; entry_price_eff: number; exit_price_eff: number;
  exit_reason: string; net_usd: number; net_r?: number; cost_usd?: number;
}
export interface PaperState {
  state: PaperAccountRow["state"]; current_attempt: number; attempts: PaperAttempt[];
  payouts: { attempt: number; n: number; date: string; gross: number; trader_share: number; firm_share: number; balance_after: number }[];
  fees: { attempt: number; kind: "eval_price" | "reset_fee" | "activation_fee"; amount: number | null; at: string | null }[];
  fees_total: number; fees_complete: boolean; trader_payouts: number; net: number; passes: number; stopped: string | null;
  trades: PaperTrade[]; n_trades: number; feed: { first_day: string; last_day: string; content_hash: string }; label: string;
  computed_at: string;
}
export interface PaperAccountDetail {
  account: Omit<PaperAccountRow, "state" | "current_attempt" | "attempts" | "passes" | "payouts" | "trader_payouts" | "fees_total" | "net" |
    "n_trades" | "balance" | "last_day" | "computed_at"> & { fees: PaperFees; start_ts: string; logic_hash?: string; definition_hash?: string;
    stopped_at?: string | null; label: string };
  state: PaperState | null;
}
