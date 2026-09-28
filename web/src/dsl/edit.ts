// Scaffolding for new nodes in the visual builder. These create DSL fragments; they do not
// judge them (the backend validator does).
import type { Comparison, Condition, Operand, StrategyDoc } from "./types";

export const newComparison = (): Comparison => ({ left: { bar: "close" }, op: ">", right: 0 });
export const newGroup = (kind: "all" | "any"): Condition => (kind === "all" ? { all: [newComparison()] } : { any: [newComparison()] });

export function newStrategy(timeframe = "5m"): StrategyDoc {
  return {
    dsl_version: 1, name: "new_strategy", description: "",
    family: { id: "new_family", name: "", hypothesis: "", category: "" },
    timeframe,
    parameters: {},
    entry: { direction: "long", order: { type: "market" }, long: { all: [newComparison()] } },
    exit: { stop: { type: "points", points: 10 }, target: { type: "none" } },
    sizing: { mode: "fixed", quantity: 1 },
  };
}

export type OperandKind = "const" | "param" | "bar" | "feature" | "arith";
export function operandKind(o: Operand | undefined): OperandKind {
  if (typeof o === "number") return "const";
  if (typeof o === "string") return "param";
  if (o && typeof o === "object") {
    if ("bar" in o) return "bar";
    if ("feature" in o) return "feature";
    if ("arith" in o) return "arith";
  }
  return "const";
}

/** Condition node kind as the DSL defines it. */
export function conditionKind(c: Condition): "all" | "any" | "not" | "cmp" {
  if ("all" in c) return "all";
  if ("any" in c) return "any";
  if ("not" in c) return "not";
  return "cmp";
}

/** Rename $old -> $new everywhere in the document (parameter rename). */
export function renameRefs<T>(node: T, from: string, to: string): T {
  if (node === `$${from}`) return `$${to}` as T;
  if (Array.isArray(node)) return node.map((x) => renameRefs(x, from, to)) as T;
  if (node && typeof node === "object") {
    return Object.fromEntries(Object.entries(node as Record<string, unknown>)
      .map(([k, v]) => [k, renameRefs(v, from, to)])) as T;
  }
  return node;
}

/** Number of places that reference $name (outside the parameter block). */
export function countRefs(node: unknown, name: string): number {
  if (node === `$${name}`) return 1;
  if (Array.isArray(node)) return node.reduce((n, x) => n + countRefs(x, name), 0);
  if (node && typeof node === "object") return Object.values(node).reduce((n: number, x) => n + countRefs(x, name), 0);
  return 0;
}

export function omit<T extends object>(o: T, ...keys: string[]): T {
  const out = { ...o } as Record<string, unknown>;
  for (const k of keys) delete out[k];
  return out as T;
}
