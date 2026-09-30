/* Shared research-terminal components: protocol budgets, dataset/execution identity, pipeline strip,
   random-control presentation and strategy rules in plain English. Display only - every number comes
   from the backend; nothing here computes a research result. */
import type { ReactNode } from "react";
import type { ExecutionModel, Hist, Overview, PipelineStage, ProtocolStatus } from "../api/types";
import { href } from "../app/router";
import { Histogram } from "./charts";
import { Badge, Banner, Card, KeyValues, Kpi, Mono, Scope, pct, r } from "./ui";

// ---------------------------------------------------------------------------------------------- protocol
export function ProtocolPanel({ p, compact }: { p: ProtocolStatus; compact?: boolean }) {
  if (p.error) return <Banner tone="error">Protocol {p.protocol_id}: {p.error}</Banner>;
  const t = p.trials, h = p.holdout, mt = p.multiple_testing;
  const tUsed = t.unique_numerical_trials / Math.max(1, t.budget), hUsed = h.looks_used / Math.max(1, h.budget);
  return (
    <Card testId="protocol-panel" title={<>Research protocol <Mono>{p.protocol_id}</Mono> <Badge tone={p.status === "ACTIVE" ? "ok" : "neutral"}>{p.status}</Badge>
      <span className="small muted">{p.scope.instrument} @ {p.scope.provider} · {p.scope.timeframe}</span></>}
      actions={<span className="small muted" title="There is no UI control that edits a protocol, moves the holdout or resets a counter.">🔒 locked</span>}>
      <div className="kpis">
        <Kpi label="Unique numerical trials" value={`${t.unique_numerical_trials.toLocaleString()} / ${t.budget.toLocaleString()}`}
          sub={`${t.remaining.toLocaleString()} remaining · ${t.evaluation_events} evaluation events`} meter={tUsed}
          meterTone={tUsed > 0.9 ? "error" : tUsed > 0.7 ? "warn" : undefined} testId="kpi-trials" />
        <Kpi label="Holdout looks" value={`${h.looks_used} / ${h.budget}`} sub={`${h.remaining} remaining · one per candidate · ${h.refusals} refusal(s)`}
          meter={hUsed} meterTone={hUsed > 0.7 ? "warn" : undefined} testId="kpi-looks" />
        <Kpi label="Proposal attempts" value={p.proposal_attempts.total.toLocaleString()}
          sub={`${p.proposal_attempts.distinct_logic_hashes} distinct logic · counted separately from trials`} />
        <Kpi label="Multiple testing" value={`α′ ${mt.per_test_alpha.toPrecision(3)}`}
          sub={`family size ${mt.family_size} · familywise α ${mt.familywise_alpha} (Bonferroni)`} />
      </div>
      {!compact && <div className="grid2" style={{ marginTop: 12 }}>
        <KeyValues rows={[
          ["Discovery window", <><Scope kind="is" /> {p.windows.discovery.trading_dates[0]} → {p.windows.discovery.trading_dates[1]}
            <span className="small muted"> · {p.windows.discovery.n_bars.toLocaleString()} bars</span></>],
          ["Holdout window", <><Scope kind="holdout" /> {p.windows.holdout.trading_dates[0]} → {p.windows.holdout.trading_dates[1]}
            <span className="small muted"> · {p.windows.holdout.n_bars.toLocaleString()} bars · locked</span></>],
          ["Source dataset", <><Mono>{p.source_dataset.dataset_id}</Mono> {p.source_dataset.has_ask_ohlc && <Badge tone="info">BID+ASK</Badge>}</>],
          ["Content hash", <Mono>{p.source_dataset.content_hash.slice(0, 24)}…</Mono>]]} />
        <KeyValues rows={[
          ["Trials by entry point", Object.entries(t.by_entry_point).map(([k, v]) => `${k}: ${v}`).join(" · ") || "none"],
          ["Pre-protocol exposure", `${p.pre_protocol_exposure.runs.length} run(s) disclosed`],
          ["Acceptance statistic", <span className="small">{mt.effect}</span>]]} />
      </div>}
    </Card>
  );
}

// ---------------------------------------------------------------------------------------------- dataset + execution
export function DatasetIdentity({ o }: { o: Overview }) {
  const d = o.dataset;
  if (!d) return <Card title="Research dataset"><p className="muted">No protocol dataset or preferred research dataset is set. <a href={href("/datasets")}>Choose one</a>.</p></Card>;
  if (d.error) return <Card title="Research dataset"><Banner tone="warn"><Mono>{d.dataset_id}</Mono>: {d.error}</Banner></Card>;
  const m = d.manifest, idn = d.identity ?? {};
  const q = m.quality_status ?? d.validation_report?.quality_status;
  return (
    <Card title={<>Research dataset <span className="small muted">({o.dataset_source})</span></>} testId="dataset-identity"
      actions={<a className="small" href={href(`/datasets`)}>Data ›</a>}>
      <KeyValues rows={[
        ["Dataset", <Mono>{m.dataset_id}</Mono>],
        ["Content hash", <Mono>{String(m.content_hash).slice(0, 24)}…</Mono>],
        ["Instrument", <>{m.instrument} · {m.provider} · {m.timeframe} {idn.research_proxy && <Badge tone="warn">research proxy</Badge>}</>],
        ["What it is", <span className="small">{idn.description ?? "—"}</span>],
        ["Source symbol / feed", <span className="small">{idn.source_symbol ?? "—"}{idn.source_feed_code ? ` (feed ${idn.source_feed_code})` : ""}</span>],
        ["Price basis", `${m.price_basis}${m.has_ask_ohlc ? " + ASK OHLC" : ""}`],
        ["Volume", <span className="small">{String(idn.volume_semantics ?? m.volume_type)}</span>],
        ["Calendar", <>{m.calendar} <span className="small muted">{idn.calendar_status ?? ""}</span></>],
        ["Coverage", `${String(m.start ?? "").slice(0, 10)} → ${String(m.end ?? "").slice(0, 10)} · ${Number(m.n_bars ?? 0).toLocaleString()} bars`],
        ["Validation", <Badge tone={q === "PASS" ? "ok" : q === "FAIL" ? "error" : "warn"}>{q ?? "—"}</Badge>]]} />
      {m.provider === "DUKASCOPY" && <Banner tone="warn" testId="not-futures">A Dukascopy NAS100 / USA Tech index-CFD research series
        (BID/ASK quotes). It is <b>not</b> CME NQ futures data and carries no exchange-traded futures volume.</Banner>}
      {d.limitations.length > 0 && <ul className="small muted" style={{ margin: "8px 0 0", paddingLeft: 18 }}>
        {d.limitations.map((l) => <li key={l}>{l}</li>)}</ul>}
    </Card>
  );
}

export function ExecutionPanel({ e }: { e: ExecutionModel | null }) {
  if (!e) return <Card title="Execution model"><p className="muted">No research dataset selected.</p></Card>;
  if (e.status === "unconfigured" || e.status === "unknown")
    return <Card title="Execution model"><Banner tone="error">Costs for {e.instrument} are {e.status}: {e.reason}. The engine refuses to run.</Banner></Card>;
  const sides = e.quote_sides;
  return (
    <Card title="Execution & cost model" testId="execution-panel">
      <KeyValues rows={[["Quote model", <Mono>{e.quote_model}</Mono>], ["Spread", <span className="small">{e.spread_treatment}</span>],
        ["Cost scenario", <Mono>{e.cost_scenario || "—"}</Mono>], ["Cost status", <Badge tone={e.status === "assumed" ? "warn" : "neutral"}>{e.status}</Badge>]]} />
      {sides && <div className="exec-sides" style={{ marginTop: 10 }}>
        <div className="exec-side ask"><div className="small muted">Long entry</div><b>{sides.long_entry}</b></div>
        <div className="exec-side bid"><div className="small muted">Long exit</div><b>{sides.long_exit}</b></div>
        <div className="exec-side bid"><div className="small muted">Short entry</div><b>{sides.short_entry}</b></div>
        <div className="exec-side ask"><div className="small muted">Short exit</div><b>{sides.short_exit}</b></div>
      </div>}
      {e.status === "assumed" && <p className="small muted">Commission and slippage are stated research assumptions, not broker-verified rates;
        every run records them.</p>}
    </Card>
  );
}

// ---------------------------------------------------------------------------------------------- pipeline
const STATE_TEXT: Record<string, string> = { done: "done", failed: "failed", refused: "refused", pending: "pending",
  not_recorded: "not recorded", not_available: "not implemented" };
export function PipelineStrip({ stages, testId = "pipeline" }: { stages: PipelineStage[]; testId?: string }) {
  return (
    <div className="pipeline" data-testid={testId}>
      {stages.map((s, i) => (
        <div key={s.id} className={`stage ${s.state}`} title={s.evidence}>
          <div className="stage-name">{i + 1}. {s.label}</div>
          <div className="stage-state">{STATE_TEXT[s.state] ?? s.state}</div>
          <div className="stage-ev">{s.evidence}</div>
        </div>))}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------- random controls
export interface ControlView {
  candidate: number | null; controls: (number | null)[]; pValue?: number | null; percentile?: number | null; nControls: number;
  rule?: string; role?: string; formal: boolean; preserved?: string; randomized?: string; scope?: ReactNode; statistic?: string;
}
export function controlHist(values: (number | null)[], bins = 20): Hist {
  const v = values.filter((x): x is number => typeof x === "number" && Number.isFinite(x));
  if (!v.length) return { edges: [], counts: [], n: 0, clipped: 0 };
  const lo = Math.min(...v), hi = Math.max(...v) === lo ? lo + 1 : Math.max(...v);
  const counts = new Array(bins).fill(0) as number[];
  for (const x of v) counts[Math.min(bins - 1, Math.floor(((x - lo) / (hi - lo)) * bins))]++;
  return { edges: Array.from({ length: bins + 1 }, (_, i) => lo + ((hi - lo) * i) / bins), counts, n: v.length, clipped: 0 };
}
/** Candidate vs its conditional-null distribution: first-class, plainly worded. Histogram binning is for
 *  display only; the p-value shown always comes from the backend's exact rule. */
export function ControlPresentation({ c, testId = "control-presentation" }: { c: ControlView; testId?: string }) {
  const finite = c.controls.filter((x): x is number => typeof x === "number" && Number.isFinite(x));
  const pass = typeof c.pValue === "number" ? c.pValue <= 0.05 : null;
  return (
    <div data-testid={testId}>
      <div className="inline" style={{ marginBottom: 8 }}>
        <Scope kind="control" />{c.scope}<Scope kind="net" />
        <Badge tone={c.formal ? "info" : "neutral"}>{c.formal ? "part of the holdout acceptance criteria" : "robustness filter only (not an acceptance test)"}</Badge>
      </div>
      <div className="kpis">
        <Kpi label="Candidate expectancy" value={r(c.candidate)} tone={typeof c.candidate === "number" ? (c.candidate > 0 ? "pos" : "neg") : ""} />
        {c.controls.length > 0 && <>
          <Kpi label="Control median" value={r(finite.length ? [...finite].sort((a, b) => a - b)[Math.floor(finite.length / 2)] : null)}
            sub={`${finite.length} of ${c.nControls} finite`} />
          <Kpi label="Candidate percentile" value={pct(c.percentile, 0)} sub="share of controls below the candidate" /></>}
        <Kpi label="Controls" value={String(c.nControls)} sub="matched realizations" />
        {c.formal && <Kpi label="Exact Monte-Carlo p" value={typeof c.pValue === "number" ? c.pValue.toFixed(4) : "—"} accent={pass === true}
          sub={pass == null ? "not available" : pass ? "≤ 0.05" : "> 0.05"} testId="control-p" />}
      </div>
      {c.controls.length > 0 ? <div style={{ marginTop: 10 }}>
        <Histogram hist={controlHist(c.controls)} unit={`${c.statistic ?? "expectancy"} (R/trade)`} color="var(--c-neutral)" signed
          marker={typeof c.candidate === "number" ? { value: c.candidate, label: `candidate ${c.candidate.toFixed(3)}` } : null} testId="control-hist" />
      </div> : <p className="small muted" style={{ marginTop: 8 }}>The control distribution itself is not stored with this result; only the
        exact p-value, the candidate statistic and the control count are.</p>}
      <div className="grid2 small" style={{ marginTop: 8 }}>
        <div><b>What was preserved:</b> {c.preserved ?? "the candidate's exits, stop/target rules, costs, sizing, cooldown and engine rules; the same data window"}</div>
        <div><b>What was randomized:</b> {c.randomized ?? "entry timing and direction, among the candidate's own eligible bars, matched to its signal rate and long/short mix"}</div>
      </div>
      <p className="small muted" style={{ marginTop: 6 }}>{c.rule ?? "p = (1 + #controls ≥ candidate or non-finite) / (N + 1)"}.
        {" "}How to read it: {c.formal ? "a small p means" : "a high percentile means"} random entries with the same exits and costs rarely did as
        well as the candidate on this window. It does not prove an edge and is {c.role ?? "a robustness filter, not a familywise significance test"}.</p>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------- rules in plain English
/* eslint-disable @typescript-eslint/no-explicit-any */
function val(x: any, params: Record<string, any>): string {
  if (typeof x === "string" && x.startsWith("$")) {
    const p = params[x.slice(1)];
    return p ? `${String(p.value)} (parameter ${x.slice(1)})` : x;
  }
  return x == null ? "—" : String(x);
}
function levelText(x: any, params: Record<string, any>, kind: "stop" | "target"): string {
  if (!x || x.type === "none") return kind === "target" ? "no fixed target (exits by stop, time or signal)" : "—";
  const side = (s: any) => (s == null ? "" : typeof s === "object" ? (s.feature ? `${s.feature}${s.output ? `.${s.output}` : ""}` : s.bar ?? JSON.stringify(s)) : val(s, params));
  switch (x.type) {
    case "points": return `${val(x.points ?? x.value ?? x.distance, params)} points from the entry price`;
    case "atr": return `${val(x.multiple, params)} × ATR(${val(x.period ?? 14, params)}) from the entry price`;
    case "risk_reward": return `${val(x.multiple, params)} × the initial risk (R-multiple)`;
    case "price": return `a price level: long ${side(x.long) || "—"} / short ${side(x.short) || "—"}`;
    default: return JSON.stringify(x);
  }
}
export function RulesTable({ definition, explain }: { definition: Record<string, any>; explain?: string | null }) {
  const d = definition ?? {}, e = d.entry ?? {}, x = d.exit ?? {}, params = d.parameters ?? {}, fam = d.family ?? {}, sz = d.sizing ?? {};
  const dir = e.direction === "both" ? "long and short" : e.direction === "long" ? "long only" : e.direction === "short" ? "short only" : "—";
  const order = e.order ?? { type: "market" };
  const rows: [string, ReactNode, boolean?][] = [
    ["Family", <>{fam.name ?? fam.id ?? "—"} {fam.category && <Badge>{fam.category}</Badge>}{fam.hypothesis && <div className="small muted">Hypothesis: {fam.hypothesis}</div>}</>],
    ["Timeframe", d.timeframe ?? "—"],
    ["Direction", dir],
    ["Signal", explain ? <pre className="code explain" style={{ margin: 0, maxHeight: 220 }}>{explain}</pre> : <span className="muted">see definition</span>],
    ["Entry order", order.type === "market" ? "market order on the bar after the signal" : `${order.type} order (${Object.entries(order).filter(([k]) => k !== "type").map(([k, v]) => `${k} ${val(v, params)}`).join(", ") || "as defined"})`],
    ["Stop", levelText(x.stop, params, "stop")],
    ["Target", levelText(x.target, params, "target")],
    ["Time exit", x.time_stop_bars || x.max_hold_bars ? [x.time_stop_bars && `time stop after ${val(x.time_stop_bars, params)} bars`, x.max_hold_bars && `maximum hold ${val(x.max_hold_bars, params)} bars`].filter(Boolean).join("; ") : "none", !(x.time_stop_bars || x.max_hold_bars)],
    ["Exit signal", x.signal ? "an exit condition is defined (see signal text)" : "none", !x.signal],
    ["Trailing", "none — trailing stops are not supported by the engine (stops are fixed at entry)", true],
    ["Partial exits", "none — one fill in, one fill out (partial exits are not supported)", true],
    ["Session", e.session ? <>{e.session}{d.sessions?.[e.session] && <span className="small muted"> ({d.sessions[e.session].start}–{d.sessions[e.session].end} {d.sessions[e.session].timezone})</span>}</> : "any time"],
    ["Weekdays", Array.isArray(e.trading_weekdays) ? e.trading_weekdays.join(", ") : "all trading days"],
    ["Daily flat rule", "not a rule of this definition (no forced end-of-day flattening in the DSL)", true],
    ["Max trades / day", `no per-day limit in the DSL; one position at a time${e.cooldown_bars ? `, ${val(e.cooldown_bars, params)} bars between signals` : ""}`, !e.cooldown_bars],
    ["Sizing", sz.mode === "risk" ? `risk-based (${Object.entries(sz).filter(([k]) => k !== "mode").map(([k, v]) => `${k} ${val(v, params)}`).join(", ")})` : `fixed ${val(sz.quantity ?? 1, params)} contract(s)`],
  ];
  return (
    <div className="rules" data-testid="rules-table">
      {rows.map(([k, v, na]) => [<div key={k + "k"} className="rk">{k}</div>, <div key={k + "v"} className={`rv${na ? " na" : ""}`}>{v}</div>])}
    </div>
  );
}
/* eslint-enable @typescript-eslint/no-explicit-any */
