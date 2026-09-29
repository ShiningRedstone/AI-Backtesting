import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { ExplainResult, RenderResult, SaveResult, StoredStrategy } from "../api/types";
import { go, href, useRoute } from "../app/router";
import { useApp } from "../app/context";
import { BuilderContext } from "../components/builder/editors";
import type { BuilderCtx } from "../components/builder/editors";
import { DslPreview, ReviewPanel } from "../components/builder/preview";
import { EntrySection, ExitSection, GeneralSection, MarketSection, ParametersSection, SizingSection, sectionOf } from "../components/builder/sections";
import type { SectionId } from "../components/builder/sections";
import { Badge, Banner, Button, Confirm, ErrorPanel, KeyValues, Loading, Mono, Tabs } from "../components/ui";
import { newStrategy } from "../dsl/edit";
import type { StrategyDoc } from "../dsl/types";
import { isRef } from "../dsl/types";

type Origin = { kind: "new" } | { kind: "edit"; id: string } | { kind: "duplicate"; id: string };
const STORE_KEY = "edgelab.builder.draft.v1";

/** Stored canonical definitions spell absent blocks as null; the builder shows them as absent. */
function stripNulls<T>(x: T): T {
  if (Array.isArray(x)) return x.map(stripNulls) as T;
  if (x && typeof x === "object") {
    return Object.fromEntries(Object.entries(x as Record<string, unknown>)
      .filter(([, v]) => v !== null).map(([k, v]) => [k, stripNulls(v)])) as T;
  }
  return x;
}

function loadLocal(): { doc: StrategyDoc; origin: Origin } | null {
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}
function saveLocal(v: { doc: StrategyDoc; origin: Origin }) {
  try { window.localStorage.setItem(STORE_KEY, JSON.stringify(v)); } catch { /* storage unavailable: draft stays in memory */ }
}

const TABS: { id: SectionId; label: string }[] = [
  { id: "general", label: "General" }, { id: "market", label: "Market & Sessions" }, { id: "parameters", label: "Parameters" },
  { id: "entry", label: "Entry" }, { id: "exit", label: "Exit" }, { id: "sizing", label: "Sizing" }, { id: "review", label: "Review" },
];

export function BuilderPage() {
  const { options, optionsError, toast } = useApp();
  const route = useRoute();
  const editId = route.parts[1];
  const dupId = route.query.get("duplicate");
  const fresh = route.query.get("new");
  const [doc, setDocRaw] = useState<StrategyDoc | null>(null);
  const [origin, setOrigin] = useState<Origin>({ kind: "new" });
  const [dirty, setDirty] = useState(false);
  const [loadError, setLoadError] = useState<ApiError | null>(null);
  const [tab, setTab] = useState<SectionId>("general");
  const [render, setRender] = useState<RenderResult | null>(null);
  const [rendering, setRendering] = useState(false);
  const [renderError, setRenderError] = useState<ApiError | null>(null);
  const [validateResult, setValidateResult] = useState<RenderResult | null>(null);
  const [validating, setValidating] = useState(false);
  const [explain, setExplain] = useState<ExplainResult | null>(null);
  const [explaining, setExplaining] = useState(false);
  const [explainError, setExplainError] = useState<ApiError | null>(null);
  const [saving, setSaving] = useState<"" | "save" | "new">("");
  const [saveResult, setSaveResult] = useState<SaveResult | null>(null);
  const [saveError, setSaveError] = useState<ApiError | null>(null);
  const [confirmNew, setConfirmNew] = useState(false);
  const seq = useRef(0);

  // ---- load: stored strategy (edit), duplicate draft, fresh, or the browser-kept draft
  useEffect(() => {
    let live = true;
    setLoadError(null); setSaveResult(null); setValidateResult(null); setExplain(null);
    const done = (d: StrategyDoc, o: Origin) => {
      if (!live) return;
      setDocRaw(d); setOrigin(o); setDirty(false);
      // plain #/builder from now on, so reloading the page restores this draft
      if (!editId) window.history.replaceState(null, "", "#/builder");
    };
    if (editId) {
      api.get<StoredStrategy>(`/api/strategies/${editId}`)
        .then((s) => done(stripNulls(s.definition) as unknown as StrategyDoc, { kind: "edit", id: s.strategy_id }))
        .catch((e: ApiError) => live && setLoadError(e));
    } else if (dupId) {
      api.get<StoredStrategy>(`/api/strategies/${dupId}`)
        .then((s) => api.post<{ draft: StrategyDoc }>(`/api/strategies/${dupId}/duplicate`, { name: `${s.name}_copy` }))
        .then((r) => { done(stripNulls(r.draft), { kind: "duplicate", id: dupId }); if (live) setDirty(true); })
        .catch((e: ApiError) => live && setLoadError(e));
    } else if (fresh) {
      done(newStrategy(options?.timeframes.includes("5m") ? "5m" : options?.timeframes[0]), { kind: "new" });
    } else {
      const kept = loadLocal();
      if (kept) { done(kept.doc, kept.origin); if (live) setDirty(true); }
      else done(newStrategy(), { kind: "new" });
    }
    return () => { live = false; };
  }, [editId, dupId, fresh]); // eslint-disable-line react-hooks/exhaustive-deps

  const setDoc = (fn: (d: StrategyDoc) => StrategyDoc) => {
    setDocRaw((d) => (d ? fn(d) : d));
    setDirty(true); setSaveResult(null); setValidateResult(null); setExplain(null);
  };

  // ---- keep the draft in this browser; live backend render (debounced)
  useEffect(() => {
    if (!doc) return;
    saveLocal({ doc, origin });
    const my = ++seq.current;
    setRendering(true);
    const t = window.setTimeout(() => {
      api.post<RenderResult>("/api/strategies/render", { definition: doc })
        .then((r) => { if (my === seq.current) { setRender(r); setRenderError(null); } })
        .catch((e: ApiError) => { if (my === seq.current) setRenderError(e); })
        .finally(() => { if (my === seq.current) setRendering(false); });
    }, 300);
    return () => window.clearTimeout(t);
  }, [doc, origin]);

  const ctx: BuilderCtx | null = useMemo(() => {
    if (!options || !doc) return null;
    const params = doc.parameters ?? {};
    const tf = isRef(doc.timeframe) ? String(params[doc.timeframe.slice(1)]?.value ?? "") : doc.timeframe;
    const norm = options.timeframes.find((t) => t === tf) ?? (tf === "1h" ? "60m" : tf);
    return { options, params, localSessions: doc.sessions ?? {}, timeframe: norm };
  }, [options, doc]);

  const badges = useMemo(() => {
    const b: Partial<Record<SectionId, number>> = {};
    for (const i of render?.errors ?? []) { const s = sectionOf(i.path); b[s] = (b[s] ?? 0) + 1; }
    return b;
  }, [render]);

  if (optionsError) return <ErrorPanel error={optionsError} title="Could not load builder options from the backend" />;
  if (loadError) return <ErrorPanel error={loadError} title="Could not open this strategy" />;
  if (!options || !doc || !ctx) return <Loading label="Loading builder…" />;

  const issuesFor = (s: SectionId) => [...(render?.errors ?? []), ...(render?.warnings ?? [])].filter((i) => sectionOf(i.path) === s);

  const validate = async () => {
    setValidating(true);
    try { setValidateResult(await api.post<RenderResult>("/api/strategies/render", { definition: doc })); }
    catch (e) { setRenderError(e as ApiError); }
    finally { setValidating(false); }
  };
  const doExplain = async () => {
    setExplaining(true); setExplainError(null);
    try { setExplain(await api.post<ExplainResult>("/api/strategies/explain", { definition: doc })); }
    catch (e) { setExplainError(e as ApiError); setExplain(null); }
    finally { setExplaining(false); }
  };
  const save = async (asNew: boolean) => {
    setSaving(asNew ? "new" : "save"); setSaveError(null);
    const body: Record<string, unknown> = { definition: doc };
    if (!asNew && origin.kind !== "new") {
      body.parent_strategy_id = origin.id;
      body.method = origin.kind === "duplicate" ? "duplicate" : "manual_edit";
    }
    try {
      const r = await api.post<SaveResult>("/api/strategies/save", body);
      setSaveResult(r); setDirty(false);
      setOrigin({ kind: "edit", id: r.strategy_id });
      toast(r.created ? "ok" : "info", r.created ? `Saved ${r.strategy_id}` : (r.note ?? `Already stored as ${r.strategy_id}`));
    } catch (e) {
      setSaveError(e as ApiError); setTab("review");
    } finally { setSaving(""); }
  };

  const savedId = origin.kind === "edit" ? origin.id : null;
  return (
    <BuilderContext.Provider value={ctx}>
      <div className="page builder">
        <header className="page-head">
          <div>
            <h1>Strategy Builder</h1>
            <div className="subtitle">
              <b data-testid="builder-name">{doc.name || "(unnamed)"}</b>{" "}
              {origin.kind === "new" && <Badge>new draft</Badge>}
              {origin.kind === "edit" && <>editing <a href={href(`/strategies/${origin.id}`)}><Mono>{origin.id}</Mono></a> — saving a logic change creates a new instance with lineage</>}
              {origin.kind === "duplicate" && <>duplicate of <a href={href(`/strategies/${origin.id}`)}><Mono>{origin.id}</Mono></a></>}
              {dirty && <Badge tone="warn">unsaved changes</Badge>}
            </div>
          </div>
          <div className="actions">
            <Button onClick={() => (dirty ? setConfirmNew(true) : go("/builder?new=1"))} testId="new-strategy">New</Button>
            {savedId && <Button onClick={() => go(`/builder?duplicate=${savedId}`)} testId="duplicate">Duplicate</Button>}
            <Button onClick={() => save(true)} busy={saving === "new"} busyLabel="Saving…" testId="save-as-new"
              title="Save as a new instance without a parent">Save As New</Button>
            <Button kind="primary" onClick={() => save(false)} busy={saving === "save"} busyLabel="Saving…" testId="save">Save Strategy</Button>
            {savedId && !dirty && <Button onClick={() => go(`/strategies/${savedId}?tab=variations`)} testId="goto-variations">Generate Variations</Button>}
          </div>
        </header>

        <ErrorPanel error={renderError} title="Live validation unavailable" />
        <ErrorPanel error={saveError} title="The strategy was not saved" testId="save-error" />
        {saveResult && (
          <Banner tone={saveResult.created ? "ok" : "info"} testId="save-result">
            <b>{saveResult.created ? "Saved to the strategy library." : "Nothing new saved."}</b> {saveResult.note}
            <KeyValues rows={[
              ["Strategy ID", <a href={href(`/strategies/${saveResult.strategy_id}`)}><Mono>{saveResult.strategy_id}</Mono></a>],
              ["Logic hash", <Mono>{saveResult.logic_hash}</Mono>], ["Definition hash", <Mono>{saveResult.definition_hash}</Mono>]]} />
            <p><a href={href(`/strategies/${saveResult.strategy_id}?tab=research`)} data-testid="open-in-lab">Open this version in the Strategy Lab</a>
              {" · "}<a href={href(`/strategies/${saveResult.strategy_id}?tab=backtest`)} data-testid="goto-backtest">Backtest it</a></p>
          </Banner>
        )}

        <div className="builder-layout">
          <div className="builder-main">
            <Tabs tabs={TABS} active={tab} onChange={setTab} badges={badges} />
            {tab === "general" && <GeneralSection doc={doc} setDoc={setDoc} issues={issuesFor("general")} />}
            {tab === "market" && <MarketSection doc={doc} setDoc={setDoc} issues={issuesFor("market")} />}
            {tab === "parameters" && <ParametersSection doc={doc} setDoc={setDoc} issues={issuesFor("parameters")} />}
            {tab === "entry" && <EntrySection doc={doc} setDoc={setDoc} issues={issuesFor("entry")} />}
            {tab === "exit" && <ExitSection doc={doc} setDoc={setDoc} issues={issuesFor("exit")} />}
            {tab === "sizing" && <SizingSection doc={doc} setDoc={setDoc} issues={issuesFor("sizing")} />}
            {tab === "review" && (
              <ReviewPanel render={render} validateResult={validateResult} validating={validating} onValidate={validate}
                explain={explain} explaining={explaining} onExplain={doExplain} explainError={explainError} />
            )}
          </div>
          <aside className="builder-side">
            <div className="live-status" data-testid="live-status">
              {rendering ? <span className="muted">Checking…</span>
                : render?.valid ? <><Badge tone="ok">valid</Badge> <Mono>{render.identity?.strategy_id}</Mono></>
                  : render ? <Badge tone="error">{render.errors.length} validation issue{render.errors.length === 1 ? "" : "s"}</Badge> : null}
              <span className="muted small"> · checked by the backend validator</span>
            </div>
            <DslPreview doc={doc} render={render} rendering={rendering}
              onLoad={(d) => { setDoc(() => d); setOrigin({ kind: "new" }); toast("info", "Loaded into the builder as a new draft"); }} />
          </aside>
        </div>
        <Confirm open={confirmNew} title="Discard unsaved changes?" confirmLabel="Start a new strategy" danger
          onCancel={() => setConfirmNew(false)} onConfirm={() => { setConfirmNew(false); go("/builder?new=1"); }}>
          The current draft has changes that are not in the strategy library.
        </Confirm>
      </div>
    </BuilderContext.Provider>
  );
}
