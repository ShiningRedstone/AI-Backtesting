/* Simulated Lucid accounts (ADR-112): orders are simulated in the app, never sent to a broker. */
import { api } from "./client";

export type OrderType = "market" | "limit" | "stop" | "stop_limit" | "mit" | "trailing_stop" | "trailing_stop_limit";
export type Tif = "day" | "gtc" | "ioc" | "fok";
export interface SimOrder { id: string; symbol: string; side: "buy" | "sell"; qty: number; type: OrderType; tif: Tif; status: string;
  price: number | null; stop: number | null; trail: number | null; offset: number | null; role: string; parent: string | null;
  oco: string | null; placed_ms: number; done_ms?: number; fill_price?: number; reason?: string; triggered?: boolean; ticks?: number }
export interface SimFill { id: string; order: string; symbol: string; side: string; qty: number; price: number; ms: number; realized: number;
  fee: number; position: number; why: string }
export interface SimTrade { n: number; entry_ms: number; exit_ms: number; net: number; mae: number; micros: number; fills: number }
export interface SimRules {
  stage: "evaluation" | "funded"; start: number; balance: number; equity: number; unrealized: number; floor: number; room: number;
  max_loss: number; floor_locked: boolean; highest_eod: number; lock_at: number | null; micros_allowed: number; micros_now: number;
  today_pnl: number; trading_days: number; profit: number;
  target?: number; target_left?: number; best_day?: number; consistency_percent?: number; consistency_now?: number | null; consistency_ok?: boolean;
  payouts?: { n: number; date: string; gross: number; trader_share: number; balance_after: number }[]; winning_days?: number;
  winning_days_required?: number; winning_day_threshold?: number; payout_minimum?: number; payouts_left?: number | null;
  trader_paid?: number; pass_date?: string | null;
}
export interface SimAccount {
  id: string; name: string; created_ms: number; attempt: number; attempts: number; state: "active" | "failed" | "complete" | "reset";
  end_reason: string | null; start_balance: number; rules: SimRules; breach: { ms: number; detail: string } | null; label: string;
  rule_basis: string; assumed_rules: Record<string, { value: unknown; basis: string }>;
  positions: { symbol: string; qty: number; avg: number; pnl: number }[]; working: SimOrder[];
  orders?: SimOrder[]; fills?: SimFill[]; trades?: SimTrade[]; notes?: string[]; headline?: string[];
  days?: { date: string; stage: string; day_pnl: number; balance_end: number; floor: number; n_trades: number }[];
  history?: { n: number; start_balance: number; state: string; end_reason: string | null; started_ms: number; ended_ms: number | null; trades: number; net: number }[];
  error?: string;
}
export interface Quote { symbol: string; bid: number | null; ask: number | null; ms: number | null; error: string | null }
export interface OrderSpec { symbol: string; side: "buy" | "sell"; qty: number; type: OrderType; tif?: Tif; price?: number; stop?: number;
  trail?: number; offset?: number; tp_ticks?: number; sl_ticks?: number; sl_trailing?: boolean }

const A = (id: string, action: string, body: unknown = {}) => api.post<{ account: SimAccount } & Record<string, unknown>>(`/api/sim/accounts/${id}/${action}`, body);
export const sim = {
  accounts: () => api.get<{ accounts: SimAccount[]; costs: Record<string, number> }>("/api/sim/accounts"),
  create: (name: string, start_balance: number) => api.post<SimAccount>("/api/sim/accounts", { name, start_balance }),
  account: (id: string) => api.get<SimAccount>(`/api/sim/accounts/${id}`),
  quote: (symbol: string) => api.get<Quote>(`/api/sim/quote?symbol=${symbol}`),
  order: (id: string, spec: OrderSpec | { oco: OrderSpec[] }) => A(id, "order", spec),
  modify: (id: string, order: string, change: { price?: number; stop?: number; trail?: number; qty?: number }) => A(id, "modify", { order, ...change }),
  cancel: (id: string, order?: string) => A(id, "cancel", order ? { order } : {}),
  flatten: (id: string, symbol?: string) => A(id, "flatten", symbol ? { symbol } : {}),
  reverse: (id: string, symbol: string) => A(id, "reverse", { symbol }),
  reset: (id: string, start_balance?: number) => A(id, "reset", start_balance ? { start_balance } : {}),
  rename: (id: string, name: string) => A(id, "rename", { name }),
  remove: (id: string) => api.post<{ deleted: string }>(`/api/sim/accounts/${id}/delete`, {}),
};
