/* ADR-114: the My strategy pages serve two hand-built strategies. The context says which one a page shows: its API
   (/api/my or /api/fair), its routes (/my-... or /fair-...) and its name. My strategy is the default. */
import { createContext, useContext, type ReactNode } from "react";
import { makeMy, my, type MyApi } from "../api/my";

export interface StrategyKind {
  id: "my" | "fair";
  label: string;
  api: MyApi;
  /** route of a sub-page: r("") = the overview, r("trades") = "/my-trades" or "/fair-trades" */
  r: (sub: string) => string;
}

export const MY_KIND: StrategyKind = { id: "my", label: "My strategy", api: my, r: (s) => (s ? `/my-${s}` : "/my") };
export const FAIR_KIND: StrategyKind = { id: "fair", label: "Fair price", api: makeMy("/api/fair"),
  r: (s) => (s ? `/fair-${s}` : "/fair") };

const Ctx = createContext<StrategyKind>(MY_KIND);

export function useKind(): StrategyKind {
  return useContext(Ctx);
}

export function KindProvider({ kind, children }: { kind: StrategyKind; children: ReactNode }) {
  return <Ctx.Provider value={kind}>{children}</Ctx.Provider>;
}
