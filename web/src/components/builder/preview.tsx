import { useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { ExplainResult, RenderResult } from "../../api/types";
import type { StrategyDoc } from "../../dsl/types";
import { Badge, Banner, Button, ErrorPanel, IssueList, KeyValues, Mono, Spinner } from "../ui";

export function download(name: string, text: string, type = "text/yaml") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url; a.download = name; a.click();
  URL.revokeObjectURL(url);
}

export function DslPreview({ doc, render, rendering, onLoad }: {
  doc: StrategyDoc; render: RenderResult | null; rendering: boolean; onLoad: (d: StrategyDoc) => void;
}) {
  const [view, setView] = useState<"draft" | "canonical" | "json">("draft");
  const [open, setOpen] = useState(true);
  const [copied, setCopied] = useState(false);
  const [loadErr, setLoadErr] = useState<ApiError | null>(null);
  const file = useRef<HTMLInputElement | null>(null);
  const text = view === "json" ? JSON.stringify(doc, null, 2)
    : view === "canonical" ? (render?.canonical_yaml ?? "") : (render?.yaml ?? "");
  const loadText = async (t: string) => {
    try {
      const r = await api.post<{ definition: StrategyDoc }>("/api/dsl/parse", { text: t });
      setLoadErr(null);
      onLoad(r.definition);
    } catch (e) { setLoadErr(e as ApiError); }
  };
  return (
    <div className="dsl-preview" data-testid="dsl-preview">
      <div className="dsl-head">
        <button className="linklike" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? "▾" : "▸"} DSL Preview</button>
        {rendering && <Spinner />}
        {render && (render.valid ? <Badge tone="ok">valid</Badge> : <Badge tone="error">{render.errors.length} issue{render.errors.length === 1 ? "" : "s"}</Badge>)}
      </div>
      {open && <>
        <div className="dsl-tools">
          <div className="segmented small">
            <button className={view === "draft" ? "on" : ""} onClick={() => setView("draft")} data-testid="dsl-view-draft">Draft YAML</button>
            <button className={view === "canonical" ? "on" : ""} onClick={() => setView("canonical")} disabled={!render?.valid}
              title={render?.valid ? "backend canonical form" : "available once the strategy is valid"} data-testid="dsl-view-canonical">Canonical</button>
            <button className={view === "json" ? "on" : ""} onClick={() => setView("json")}>JSON</button>
          </div>
          <Button small onClick={() => { void navigator.clipboard?.writeText(text); setCopied(true); window.setTimeout(() => setCopied(false), 1500); }}>
            {copied ? "Copied" : "Copy"}</Button>
          <Button small onClick={() => download(`${doc.name || "strategy"}.${view === "json" ? "json" : "yaml"}`, text)}>Download</Button>
          <Button small onClick={() => file.current?.click()} title="load a YAML/JSON strategy file into the builder">Load file…</Button>
          <input ref={file} type="file" accept=".yaml,.yml,.json" hidden aria-label="strategy file"
            onChange={async (e: { target: HTMLInputElement }) => {
              const f = e.target.files?.[0];
              if (f) await loadText(await f.text());
              e.target.value = "";
            }} />
        </div>
        <ErrorPanel error={loadErr} />
        {view === "canonical" && <p className="muted small">Backend canonical form: defaults filled, feature versions pinned, conditions normalized. This is what is hashed and stored.</p>}
        <pre className="code" data-testid="dsl-text">{text || (rendering ? "Rendering…" : "")}</pre>
      </>}
    </div>
  );
}

export function ReviewPanel({ render, validateResult, validating, onValidate, explain, explaining, onExplain, explainError }: {
  render: RenderResult | null; validateResult: RenderResult | null; validating: boolean; onValidate: () => void;
  explain: ExplainResult | null; explaining: boolean; onExplain: () => void; explainError: ApiError | null;
}) {
  const v = validateResult;
  return (
    <div className="section">
      <div className="inline">
        <Button kind="primary" onClick={onValidate} busy={validating} busyLabel="Validating…" testId="validate">Validate Strategy</Button>
        <Button onClick={onExplain} busy={explaining} busyLabel="Compiling…" disabled={!render?.valid} testId="explain"
          title={render?.valid ? "compile and explain (backend)" : "fix validation errors first"}>Compile &amp; Explain</Button>
      </div>
      {v && (
        <div className="validation" data-testid="validation-result">
          {v.valid ? <Banner tone="ok"><b>✓ Strategy valid</b>{v.warnings.length ? ` — ${v.warnings.length} warning(s)` : ""}</Banner>
            : <Banner tone="error"><b>Strategy has {v.errors.length} issue{v.errors.length === 1 ? "" : "s"}</b></Banner>}
          <IssueList issues={[...v.errors, ...v.warnings]} testId="validation-issues" />
          {v.identity && <KeyValues rows={[
            ["Strategy ID", <Mono>{v.identity.strategy_id}</Mono>],
            ["Logic hash", <Mono>{v.identity.logic_hash}</Mono>],
            ["Definition hash", <Mono>{v.identity.definition_hash}</Mono>]]} />}
        </div>
      )}
      <ErrorPanel error={explainError} />
      {explain && (
        <div data-testid="explain-result">
          <h3>Strategy explanation <span className="muted small">(backend compiler)</span></h3>
          <pre className="code explain">{explain.explain}</pre>
        </div>
      )}
    </div>
  );
}
