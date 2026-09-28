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
  strategy_id: string; name: string; family_id: string; generation_method: string;
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
  runnable: boolean; reasons: string[];
}
export interface Readiness { strategy_timeframe: string | null; datasets: DatasetRow[] }
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
