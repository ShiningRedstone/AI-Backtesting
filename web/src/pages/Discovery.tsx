import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { AiGeneration, AiGenerationRow, AiProposal, AiStatus, DatasetRow, LibraryRow, ProposalReport } from "../api/types";
import { go, href, useRoute } from "../app/router";
import { useApi, useApp } from "../app/context";
import { Badge, Banner, Button, Card, Checkbox, Empty, ErrorPanel, Field, KeyValues, Loading, Mono, NumberInput, Select, TableWrap,
  Tabs, TextInput, shortTime } from "../components/ui";
import { ResearchDatasetStrip } from "./Data";

type Tab = "discover" | "history" | "batch";

/** AI Discovery (Phase 9): hypothesis -> blind context -> provider -> strict gate -> human review -> library.
 *  Proposals are listed in the order the provider returned them: nothing here ranks, scores or calls anything "best". */
export function DiscoveryPage() {
  const route = useRoute();
  const [tab, setTab] = useState<Tab>((route.query.get("tab") as Tab) || "discover");
  const status = useApi<AiStatus>("/api/ai/status");
  const [gen, setGen] = useState<AiGeneration | null>(null);
  const genId = route.query.get("gen");
  useEffect(() => {
    if (genId && genId !== gen?.generation_id) api.get<AiGeneration>(`/api/ai/generations/${genId}`).then(setGen).catch(() => undefined);
  }, [genId]); // eslint-disable-line react-hooks/exhaustive-deps
  const s = status.data;
  return (
    <div className="page" data-testid="discovery-page">
      <header className="page-head"><h1>AI Discovery</h1>
        {s && <Badge tone={s.external_configured ? "info" : "neutral"} title={s.problem ?? ""}>
          {s.external_configured ? `provider: ${s.configured_provider}${s.model ? ` · ${s.model}` : ""}` : "offline · mock provider only"}</Badge>}
      </header>
      <ErrorPanel error={status.error} />
      {s?.offline_notice && <Banner tone="info" testId="ai-offline">{s.offline_notice}</Banner>}
      {s?.problem && <Banner tone="warn">AI provider configuration: {s.problem}</Banner>}
      <Banner tone="info">The AI proposes <b>hypotheses as strategy data</b>. It never sees backtest, out-of-sample, control or prop results,
        never ranks proposals and never runs anything. Every proposal passes the strict gate (schema, DSL, features, causality, parameter
        domains, your constraints, canonical compile, identity) and then waits for <b>your</b> review. Accepted proposals become ordinary
        library strategies; you test them with the existing tools.</Banner>
      <Tabs<Tab> tabs={[{ id: "discover", label: "Discover" }, { id: "history", label: "History" }, { id: "batch", label: "Import proposal batch" }]}
        active={tab} onChange={setTab} />
      {tab === "discover" && s && <DiscoverForm status={s} onGenerated={(g) => { setGen(g); go(`/discovery?gen=${g.generation_id}`); }} />}
      {tab === "discover" && gen && <GenerationView gen={gen} onChange={setGen} />}
      {tab === "history" && <History onOpen={(id) => { setTab("discover"); go(`/discovery?gen=${id}`); }} />}
      {tab === "batch" && <ProposalBatchImport />}
    </div>
  );
}

function toggle(list: string[], v: string, on: boolean) { return on ? [...new Set([...list, v])] : list.filter((x) => x !== v); }

function MultiCheck({ label, options, value, onChange, testId }: { label: string; options: string[]; value: string[];
  onChange: (v: string[]) => void; testId?: string }) {
  return (
    <Field label={label} hint={value.length ? `${value.length} allowed` : "none checked = no restriction"}>
      <div className="inline wrap" data-testid={testId}>{options.map((o) =>
        <Checkbox key={o} checked={value.includes(o)} onChange={(on) => onChange(toggle(value, o, on))} label={o} />)}</div>
    </Field>
  );
}

function DiscoverForm({ status, onGenerated }: { status: AiStatus; onGenerated: (g: AiGeneration) => void }) {
  const datasets = useApi<DatasetRow[]>("/api/datasets");
  const strategies = useApi<LibraryRow[]>("/api/strategies");
  const [mode, setMode] = useState("hypothesis");
  const [hyp, setHyp] = useState("");
  const [template, setTemplate] = useState("ema_trend");
  const [base, setBase] = useState("");
  const [ds, setDs] = useState("");
  const [session, setSession] = useState("NY_RTH");
  const [direction, setDirection] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [n, setN] = useState<number | undefined>(4);
  const [features, setFeatures] = useState<string[]>([]);
  const [orders, setOrders] = useState<string[]>([]);
  const [stops, setStops] = useState<string[]>([]);
  const [targets, setTargets] = useState<string[]>([]);
  const [sizing, setSizing] = useState<string[]>([]);
  const [exits, setExits] = useState<string[]>([]);
  const [maxCond, setMaxCond] = useState<number | undefined>(undefined);
  const [maxCd, setMaxCd] = useState<number | undefined>(undefined);
  const [maxParams, setMaxParams] = useState<number | undefined>(undefined);
  const [provider, setProvider] = useState(status.available[status.available.length - 1] ?? "mock");
  const [busy, setBusy] = useState<"" | "gen" | "ctx">("");
  const [err, setErr] = useState<ApiError | null>(null);
  const [ctx, setCtx] = useState<Record<string, unknown> | null>(null);
  // new discovery starts on the workspace's Preferred Research Dataset
  useEffect(() => { if (!ds && status.preferred_dataset_id) setDs(status.preferred_dataset_id); }, [status.preferred_dataset_id]); // eslint-disable-line react-hooks/exhaustive-deps
  const rows = (datasets.data ?? []).filter((d) => d.quality_status !== "FAIL");
  const row = rows.find((d) => d.dataset_id === ds);
  const request = () => ({
    request_version: status.request_version, mode, n_proposals: n ?? 4, provider,
    ...(mode === "hypothesis" || hyp.trim() ? { hypothesis: hyp } : {}),
    ...(mode === "template" ? { template } : {}),
    ...(mode === "modify" ? { base_strategy_id: base } : {}),
    scope: { dataset_id: ds || null, session: session || null, direction: direction || null,
      date_scope: start || end ? { start: start || null, end: end || null } : null },
    constraints: {
      ...(features.length ? { features } : {}), ...(orders.length ? { entry_orders: orders } : {}),
      ...(stops.length ? { stop_types: stops } : {}), ...(targets.length ? { target_types: targets } : {}),
      ...(sizing.length ? { sizing_modes: sizing } : {}), ...(exits.length ? { exit_kinds: exits } : {}),
      ...(maxCond !== undefined ? { max_conditions: maxCond } : {}), ...(maxCd !== undefined ? { max_cooldown_bars: maxCd } : {}),
      ...(maxParams !== undefined ? { max_parameters: maxParams } : {}),
    },
  });
  const generate = async () => {
    setBusy("gen"); setErr(null);
    try { onGenerated(await api.post<AiGeneration>("/api/ai/generate", { request: request() })); }
    catch (e) { setErr(e as ApiError); } finally { setBusy(""); }
  };
  const preview = async () => {
    setBusy("ctx"); setErr(null);
    try { setCtx(await api.post<Record<string, unknown>>("/api/ai/context", { request: request() })); }
    catch (e) { setErr(e as ApiError); } finally { setBusy(""); }
  };
  return (
    <Card title="Request" testId="ai-request">
      <ResearchDatasetStrip row={row ? { ...row, preferred: row.dataset_id === status.preferred_dataset_id } : null} />
      <div className="segmented" role="tablist">
        {[["hypothesis", "Mode A · hypothesis + constraints"], ["template", "Mode B · family/template + constraints"],
          ["modify", "Modify an existing strategy"]].map(([k, l]) => (
          <button key={k} className={mode === k ? "on" : ""} onClick={() => setMode(k)} data-testid={`ai-mode-${k}`}>{l}</button>))}
      </div>
      <div className="grid3">
        <Field label="Research dataset" hint="default: the Preferred Research Dataset">
          <Select value={ds} onChange={setDs} testId="ai-dataset" placeholder="choose a dataset…"
            options={rows.map((d) => ({ value: d.dataset_id, label: `${d.dataset_id}${d.dataset_id === status.preferred_dataset_id ? " (preferred)" : ""}` }))} />
        </Field>
        <Field label="Instrument / timeframe" hint="from the dataset"><span data-testid="ai-inst-tf">{row ? `${row.instrument} · ${row.timeframe}` : "—"}</span></Field>
        <Field label="Session"><Select value={session} onChange={setSession} testId="ai-session"
          options={[{ value: "", label: "any" }, ...status.sessions.map((x) => ({ value: x, label: x }))]} /></Field>
        <Field label="Direction"><Select value={direction} onChange={setDirection} testId="ai-direction"
          options={[{ value: "", label: "any" }, ...status.directions.map((x) => ({ value: x, label: x }))]} /></Field>
        <Field label="Date scope start" hint="YYYY-MM-DD, recorded with the request"><TextInput value={start} onChange={setStart} /></Field>
        <Field label="Date scope end"><TextInput value={end} onChange={setEnd} /></Field>
      </div>
      {mode === "hypothesis" && <Field label="Hypothesis (plain language; no performance claims)" wide>
        <TextInput multiline value={hyp} onChange={setHyp} testId="ai-hypothesis"
          placeholder="e.g. After an oversold stretch in the New York session, price tends to revert toward its recent mean." /></Field>}
      {mode === "template" && <Field label="Family / template"><Select value={template} onChange={setTemplate} testId="ai-template"
        options={Object.entries(status.templates).map(([k, v]) => ({ value: k, label: `${k} — ${v}` }))} /></Field>}
      {mode === "modify" && <Field label="Base strategy (a new version is created; the original is never changed)">
        <Select value={base} onChange={setBase} testId="ai-base" placeholder="choose a library strategy…"
          options={(strategies.data ?? []).map((r) => ({ value: r.strategy_id, label: `${r.name} (${r.strategy_id})` }))} /></Field>}
      <details><summary>Constraints (only DSL-supported constructs)</summary>
        <MultiCheck label="Features" options={status.features} value={features} onChange={setFeatures} testId="ai-features" />
        <div className="grid3">
          <MultiCheck label="Entry orders" options={status.entry_orders} value={orders} onChange={setOrders} />
          <MultiCheck label="Stops" options={status.stop_types} value={stops} onChange={setStops} />
          <MultiCheck label="Targets" options={status.target_types} value={targets} onChange={setTargets} />
          <MultiCheck label="Exits" options={status.exit_kinds} value={exits} onChange={setExits} />
          <MultiCheck label="Sizing" options={status.sizing_modes} value={sizing} onChange={setSizing} />
        </div>
        <div className="grid3">
          <Field label="Max conditions"><NumberInput integer value={maxCond} onChange={setMaxCond} /></Field>
          <Field label="Max cooldown bars"><NumberInput integer value={maxCd} onChange={setMaxCd} /></Field>
          <Field label="Max parameters"><NumberInput integer value={maxParams} onChange={setMaxParams} /></Field>
        </div>
      </details>
      <div className="grid3">
        <Field label={`Number of proposals (1–${status.max_proposals})`}><NumberInput integer value={n} onChange={setN} testId="ai-n" /></Field>
        <Field label="Provider" hint={status.external_configured ? "" : "no external AI configured"}>
          <Select value={provider} onChange={setProvider} testId="ai-provider"
            options={status.available.map((p) => ({ value: p, label: p === "mock" ? "mock (deterministic, not an AI)" : p }))} /></Field>
      </div>
      <div className="actions">
        <Button onClick={preview} busy={busy === "ctx"} busyLabel="Building…" testId="ai-preview-context">Show what the AI sees</Button>
        <Button kind="primary" onClick={generate} busy={busy === "gen"} busyLabel="Generating & gating…" testId="ai-generate"
          disabled={!ds || (mode === "modify" && !base)}>Generate proposals</Button>
      </div>
      <ErrorPanel error={err} title="Refused" testId="ai-error" />
      {ctx && <details open data-testid="ai-context"><summary>Blind context (context hash <Mono title={String(ctx.context_hash)}>{String(ctx.context_hash).slice(0, 16)}</Mono>) — no results of any kind</summary>
        <pre className="code">{JSON.stringify(ctx, null, 2)}</pre></details>}
    </Card>
  );
}

function StageBadges({ p }: { p: AiProposal }) {
  return <div className="gate-stages">{p.gate.stages.map((s) => (
    <Badge key={s.stage} tone={s.status === "passed" ? "ok" : s.status === "failed" ? "error" : "neutral"}
      title={s.reasons.join("; ")}>{s.stage}{s.status === "not_run" ? " · not run" : ""}</Badge>))}</div>;
}

const brief = (v: unknown) => (v === null || v === undefined ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v));

function ProposalCard({ p, onChange }: { p: AiProposal; onChange: () => void }) {
  const { toast } = useApp();
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState<ApiError | null>(null);
  const valid = p.gate.status === "valid";
  const dec = p.decision;
  const saved = dec?.saved_strategy_id;
  const act = async (what: "accepted" | "rejected" | "save") => {
    setBusy(what); setErr(null);
    try {
      if (what === "save") {
        const r = await api.post<{ strategy_id: string; created: boolean }>(`/api/ai/proposals/${p.proposal_id}/save`);
        toast("ok", `Saved as ${r.strategy_id}${r.created ? "" : " (already in the library)"}`);
      } else await api.post(`/api/ai/proposals/${p.proposal_id}/decision`, { decision: what, note });
      onChange();
    } catch (e) { setErr(e as ApiError); } finally { setBusy(""); }
  };
  const S = p.gate.summary;
  const c = p.content ?? {};
  return (
    <div className={`proposal-card ${valid ? "" : "rejected"}`} data-testid={`proposal-${p.index}`}>
      <h3>#{p.index + 1} · {String(c.strategy_name ?? "(unnamed)")}{" "}
        <Badge tone={valid ? "ok" : "error"}>{valid ? "passed the gate" : "rejected by the gate"}</Badge>{" "}
        {dec?.decision && <Badge tone={dec.decision === "accepted" ? "info" : "neutral"}>{dec.decision}</Badge>}{" "}
        {saved && <Badge tone="info">saved</Badge>}</h3>
      <p className="small"><b>Hypothesis:</b> {String(c.hypothesis ?? "—")}</p>
      <StageBadges p={p} />
      {!valid && <ul className="small" data-testid={`reasons-${p.index}`}>{p.gate.rejection_reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
      {p.gate.warnings.length > 0 && <Banner tone="warn">{p.gate.warnings.join(" · ")}</Banner>}
      {S && <KeyValues rows={[
        ["Timeframe / session / direction", `${S.timeframe} · ${S.session ?? "any"} · ${S.direction}`],
        ["Entry", <span className="mono small">{S.entry_order} · {brief(S.entry)}</span>],
        ["Stop", <span className="mono small">{brief(S.stop)}</span>], ["Target", <span className="mono small">{brief(S.target)}</span>],
        ["Other exits", <span className="mono small">{brief(Object.fromEntries(Object.entries(S.exit).filter(([k]) => !["stop", "target"].includes(k))))}</span>],
        ["Parameters", <span className="mono small">{brief(S.parameters)}</span>],
        ["Complexity", `${S.complexity.conditions} conditions · ${S.complexity.parameters} parameters · features ${S.features.join(", ") || "none"}`],
        ...(S.computed_changes ? [["Changes vs base", S.computed_changes.join("; ") || "none"] as [string, string]] : []),
        ["Strategy ID", <Mono>{p.gate.identity?.strategy_id}</Mono>], ["Definition hash", <Mono title={String(p.gate.identity?.definition_hash ?? "") || undefined}>{p.gate.identity?.definition_hash.slice(0, 16)}</Mono>],
      ]} />}
      <KeyValues rows={[["Proposal ID", <Mono>{p.proposal_id}</Mono>], ["Rationale", String(c.rationale ?? "—")],
        ...(p.parent ? [["Parent", <Mono>{p.parent.strategy_id}</Mono>] as [string, ReactNode]] : [])]} />
      <div className="actions">
        <Button small onClick={() => setOpen(!open)} testId={`inspect-${p.index}`}>{open ? "Hide" : "Inspect"}</Button>
        {valid && !saved && dec?.decision !== "accepted" && <>
          <TextInput value={note} onChange={setNote} placeholder="review note (optional)" ariaLabel="review note" />
          <Button small kind="primary" onClick={() => act("accepted")} busy={busy === "accepted"} testId={`accept-${p.index}`}>Accept</Button></>}
        {!saved && dec?.decision !== "rejected" && <Button small onClick={() => act("rejected")} busy={busy === "rejected"} testId={`reject-${p.index}`}>Reject</Button>}
        {valid && dec?.decision === "accepted" && !saved &&
          <Button small kind="primary" onClick={() => act("save")} busy={busy === "save"} testId={`save-${p.index}`}>Save to library</Button>}
        {saved && <>
          <Button small kind="primary" onClick={() => go(`/strategies/${saved}?tab=backtest`)} testId={`backtest-${p.index}`}>Send to Backtest</Button>
          <a href={href(`/strategies/${saved}?tab=research`)} data-testid={`open-lab-${p.index}`}>Open in Strategy Lab</a></>}
      </div>
      <ErrorPanel error={err} />
      {open && <div data-testid={`inspect-body-${p.index}`}>
        <details open><summary>Proposal content (as returned by the provider)</summary><pre className="code">{JSON.stringify(c, null, 2)}</pre></details>
        <details><summary>Gate stages</summary><pre className="code">{JSON.stringify(p.gate.stages, null, 2)}</pre></details>
        <details><summary>Envelope</summary><KeyValues rows={[["Schema version", String(p.schema_version)], ["Request", <Mono>{p.request_id}</Mono>],
          ["Generation", <Mono>{p.generation_id}</Mono>], ["Provider", `${p.provider.kind}${p.provider.model ? ` · ${p.provider.model}` : ""} (${p.provider.provider_version})`],
          ["Context hash", <Mono title={String(p.generation.context_hash ?? "") || undefined}>{p.generation.context_hash.slice(0, 16)}</Mono>], ["Created", shortTime(p.generation.created_at)]]} /></details>
      </div>}
    </div>
  );
}

function GenerationView({ gen, onChange }: { gen: AiGeneration; onChange: (g: AiGeneration) => void }) {
  const reload = () => api.get<AiGeneration>(`/api/ai/generations/${gen.generation_id}`).then(onChange).catch(() => undefined);
  const nValid = gen.proposals.filter((p) => p.gate.status === "valid").length;
  return (
    <Card title={<>Proposals — <Mono>{gen.generation_id}</Mono></>} testId="ai-generation">
      <KeyValues rows={[["Research dataset", <Mono>{gen.scope.dataset_id}</Mono>], ["Instrument / timeframe", `${gen.scope.instrument} · ${gen.scope.timeframe}`],
        ["Provider", `${gen.provider.kind}${gen.provider.model ? ` · ${gen.provider.model}` : ""}${gen.provider.external ? "" : " (not an AI)"}`],
        ["Request", <Mono>{gen.request_id}</Mono>], ["Context hash", <Mono title={String(gen.context_hash ?? "") || undefined}>{gen.context_hash.slice(0, 16)}</Mono>],
        ["Gate", `${nValid} of ${gen.proposals.length} passed · order = provider order (no ranking)`]]} />
      {gen.provider_notes.map((n) => <Banner key={n} tone="info">{n}</Banner>)}
      {gen.dropped_beyond_bound > 0 && <Banner tone="warn">{gen.dropped_beyond_bound} extra proposals beyond the requested number were discarded.</Banner>}
      <p className="muted small">{gen.note}</p>
      {gen.proposals.map((p) => <ProposalCard key={p.proposal_id} p={p} onChange={reload} />)}
    </Card>
  );
}

function History({ onOpen }: { onOpen: (id: string) => void }) {
  const { data, error } = useApi<AiGenerationRow[]>("/api/ai/generations");
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <Loading label="Loading…" />;
  if (!data.length) return <Empty>No AI discovery requests yet.</Empty>;
  return (
    <TableWrap testId="ai-history"><table>
      <thead><tr><th>Generation</th><th>Created</th><th>Mode</th><th>Provider</th><th>Dataset</th><th>Passed gate</th><th /></tr></thead>
      <tbody>{data.map((g) => <tr key={g.generation_id}><td><Mono>{g.generation_id}</Mono></td><td>{shortTime(g.created_at)}</td><td>{g.mode}</td>
        <td>{g.provider.kind}</td><td><Mono>{g.scope.dataset_id}</Mono></td><td>{g.n_valid} / {g.n_proposals}</td>
        <td><button className="linklike" onClick={() => onOpen(g.generation_id)}>Open</button></td></tr>)}</tbody>
    </table></TableWrap>
  );
}

/** The Phase 3 Mode B gate for a hand-written or offline batch (unchanged behaviour). */
function ProposalBatchImport() {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState<"" | "check" | "save">("");
  const [err, setErr] = useState<ApiError | null>(null);
  const [rep, setRep] = useState<ProposalReport | null>(null);
  const [menu, setMenu] = useState<unknown>(null);
  const ingest = async (save: boolean) => {
    setBusy(save ? "save" : "check"); setErr(null);
    try { setRep(await api.post<ProposalReport>("/api/proposals/ingest", { batch: text, save })); }
    catch (e) { setErr(e as ApiError); setRep(null); } finally { setBusy(""); }
  };
  return (
    <Card title="Import a proposal batch (YAML or JSON)">
      <p className="muted small">For proposals written by hand or by an offline AI session: the same Mode B gate (strict schema, claim language
        refused, same DSL validator and compiler).</p>
      <textarea className="input mono" rows={14} value={text} data-testid="proposal-text" placeholder="proposal_batch_version: 1&#10;proposals: …"
        onChange={(e: { target: HTMLTextAreaElement }) => setText(e.target.value)} />
      <div className="actions">
        <Button onClick={() => ingest(false)} busy={busy === "check"} busyLabel="Checking…" disabled={!text.trim()} testId="proposal-check">Check (nothing saved)</Button>
        <Button kind="primary" onClick={() => ingest(true)} busy={busy === "save"} busyLabel="Saving…" disabled={!rep || rep.saved || !rep.n_accepted}
          testId="proposal-save">Save accepted proposals as strategies</Button>
        <Button onClick={() => api.get("/api/proposals/menu?n=20").then(setMenu).catch(setErr)}>Show capability menu</Button>
      </div>
      <ErrorPanel error={err} title="The batch was refused" testId="proposal-error" />
      {rep && <div data-testid="proposal-report">
        <KeyValues rows={[["Batch", <Mono>{rep.batch_id}</Mono>], ["Accepted", String(rep.n_accepted)], ["Rejected", String(rep.n_rejected)],
          ["Saved", rep.saved ? "yes — accepted proposals are now library strategies" : "no (check only)"]]} />
        {rep.accepted.length > 0 && <TableWrap><table><thead><tr><th>Accepted</th><th>Strategy ID</th><th /></tr></thead>
          <tbody>{rep.accepted.map((a) => <tr key={a.strategy_id}><td>{String(a.name ?? a.family_id ?? "")}</td><td><Mono>{a.strategy_id}</Mono></td>
            <td>{rep.saved && <a href={href(`/strategies/${a.strategy_id}?tab=research`)}>Open in Strategy Lab</a>}</td></tr>)}</tbody></table></TableWrap>}
        {rep.rejected.length > 0 && <details open><summary>{rep.rejected.length} rejected (with reasons)</summary>
          <pre className="code">{JSON.stringify(rep.rejected, null, 2)}</pre></details>}
      </div>}
      {menu !== null && <details open><summary>Capability menu (what a proposer may use)</summary><pre className="code">{JSON.stringify(menu, null, 2)}</pre></details>}
    </Card>
  );
}
