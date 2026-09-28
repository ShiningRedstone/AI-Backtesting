// TypeScript view of the DSL document (STRATEGY_DSL.md). These are annotations of the backend
// format, not a second schema: the builder edits this object directly and the Python validator
// decides what is valid. Fields are permissive because drafts are allowed to be incomplete.
export type Ref = string; // "$name"
export type Operand = number | Ref | { const: number | Ref } | BarOperand | FeatureOperand | ArithOperand;
export interface BarOperand { bar: string; lag?: number | Ref }
export interface FeatureOperand {
  feature: string; params?: Record<string, unknown>; output?: string;
  timeframe?: string | null; lag?: number | Ref; version?: number;
}
export interface ArithOperand { arith: string; args: [Operand, Operand] }
export interface CondMeta { enabled?: boolean | Ref; label?: string }
export interface Comparison extends CondMeta { left: Operand; op: string; right: Operand }
export type Condition = (CondMeta & { all: Condition[] }) | (CondMeta & { any: Condition[] })
  | (CondMeta & { not: Condition }) | Comparison;
export interface ParamDecl {
  type: string; value: unknown; min?: number; max?: number; step?: number; choices?: unknown[]; description?: string;
}
export interface LocalSession { timezone: string; start: string; end: string; weekdays?: string[] }
export interface StopTarget { type: string; points?: number | Ref; multiple?: number | Ref; period?: number | Ref; timeframe?: string | null; long?: Operand; short?: Operand }
export interface StrategyDoc {
  dsl_version: number; name: string; description?: string;
  family?: { id?: string; name?: string; hypothesis?: string; category?: string };
  timeframe: string; sessions?: Record<string, LocalSession>; parameters?: Record<string, ParamDecl>;
  entry: {
    direction: string; long?: Condition; short?: Condition;
    order?: { type: string; long_price?: Operand; short_price?: Operand; expiry_bars?: number | Ref };
    session?: string | null; trading_weekdays?: string[]; cooldown_bars?: number | Ref;
  };
  exit: {
    stop: StopTarget; target?: StopTarget; time_stop_bars?: number | Ref; max_hold_bars?: number | Ref;
    signal?: { long?: Condition; short?: Condition };
  };
  sizing?: { mode: string; quantity?: number | Ref; risk_usd?: number | Ref; max_quantity?: number | Ref };
  [key: string]: unknown;
}
export const isRef = (v: unknown): v is Ref => typeof v === "string" && v.startsWith("$");
