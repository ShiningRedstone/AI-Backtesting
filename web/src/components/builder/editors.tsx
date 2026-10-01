import { createContext, useContext } from "react";
import type { BuilderOptions, FeatureInfo } from "../../api/types";
import { conditionKind, newComparison, newGroup, omit, operandKind } from "../../dsl/edit";
import type { OperandKind } from "../../dsl/edit";
import type { Condition, FeatureOperand, LocalSession, Operand, ParamDecl } from "../../dsl/types";
import { isRef } from "../../dsl/types";
import { facetLabel, humanize, valueLabel } from "../../app/labels";
import { Button, NumberInput, Select, TextInput } from "../ui";

/** What every editor needs to know about the draft (read-only). */
export interface BuilderCtx {
  options: BuilderOptions;
  params: Record<string, ParamDecl>;
  localSessions: Record<string, LocalSession>;
  timeframe: string;             // resolved strategy timeframe (parameter value if parameterized)
}
export const BuilderContext = createContext<BuilderCtx | null>(null);
export function useBuilder(): BuilderCtx {
  const c = useContext(BuilderContext);
  if (!c) throw new Error("builder context missing");
  return c;
}

const OP_LABEL: Record<string, string> = { crosses_above: "crosses above", crosses_below: "crosses below" };
export const opLabel = (op: string) => OP_LABEL[op] ?? (/^[a-z_]+$/.test(op) ? humanize(op).toLowerCase() : op);
/** Select options whose visible text is in words while the stored value stays as is. */
export const wordOptions = (values: string[], label: (v: string) => string = humanize) => values.map((v) => ({ value: v, label: label(v) }));
/** Names of strategy parameters as select options ("stop_atr" shown as "Stop ATR"). */
const paramOptions = (names: string[]) => wordOptions(names);
/** An IANA time zone for display ("America/New_York" -> "New York"). */
export const zoneLabel = (tz: string) => (tz.split("/").pop() ?? tz).replace(/_/g, " ");

export function paramsOfType(params: Record<string, ParamDecl>, types: string[]): string[] {
  return Object.entries(params).filter(([, d]) => types.includes(d.type)).map(([n]) => n);
}

// --------------------------------------------------------------------------- value or $param
export type SlotKind = "number" | "integer" | "text" | "select";
const PARAM_TYPES_FOR: Record<SlotKind, string[]> = {
  number: ["float", "integer"], integer: ["integer"], text: ["choice"], select: ["choice"],
};

/** A DSL scalar slot: a fixed value, or a "$parameter" reference of a compatible type. */
export function ValueOrParam({ value, onChange, kind, choices, paramTypes, testId, ariaLabel, placeholder }: {
  value: unknown; onChange: (v: unknown) => void; kind: SlotKind; choices?: { value: string; label: string }[];
  paramTypes?: string[]; testId?: string; ariaLabel?: string; placeholder?: string;
}) {
  const { params } = useBuilder();
  const usable = paramsOfType(params, paramTypes ?? PARAM_TYPES_FOR[kind]);
  const byParam = isRef(value);
  return (
    <span className="vop">
      <select className="input vop-mode" aria-label={`${ariaLabel ?? "value"} source`} value={byParam ? "param" : "fixed"}
        data-testid={testId ? `${testId}-mode` : undefined}
        onChange={(e: { target: HTMLSelectElement }) => {
          if (e.target.value === "param" && usable.length) onChange(`$${usable[0]}`);
          if (e.target.value === "fixed") onChange(undefined);
        }}>
        <option value="fixed">fixed value</option>
        <option value="param" disabled={!usable.length}>{usable.length ? "from a parameter" : "from a parameter (none matching)"}</option>
      </select>
      {byParam ? (
        <Select value={String(value).slice(1)} onChange={(n) => onChange(`$${n}`)} options={paramOptions(usable)} testId={testId} ariaLabel={ariaLabel} />
      ) : kind === "select" ? (
        <Select value={value as string} onChange={onChange} options={choices ?? []} testId={testId} ariaLabel={ariaLabel} placeholder={placeholder ?? "choose…"} />
      ) : kind === "text" ? (
        <TextInput value={value as string} onChange={onChange} testId={testId} ariaLabel={ariaLabel} placeholder={placeholder} />
      ) : (
        <NumberInput value={value as number} onChange={onChange} integer={kind === "integer"} testId={testId} ariaLabel={ariaLabel} />
      )}
    </span>
  );
}

// --------------------------------------------------------------------------- sessions / timeframes
export function sessionChoices(ctx: BuilderCtx): { value: string; label: string }[] {
  const configured = Object.values(ctx.options.sessions).map((s) => ({
    value: s.name, label: `${facetLabel("session", s.name)} (${s.start}–${s.end} ${zoneLabel(s.timezone)})` }));
  const local = Object.entries(ctx.localSessions).map(([n, s]) => ({ value: n, label: `${humanize(n)} (this strategy's own window ${s.start}–${s.end})` }));
  return [...local, ...configured];
}

export function htfChoices(ctx: BuilderCtx): { value: string; label: string }[] {
  const tfs = ctx.options.htf_options[ctx.timeframe] ?? [];
  return tfs.filter((t) => t !== ctx.timeframe).map((t) => ({ value: t, label: facetLabel("timeframe", t) }));
}

// --------------------------------------------------------------------------- operands
const KIND_LABEL: Record<OperandKind, string> = {
  const: "Constant", param: "Parameter", bar: "Bar field", feature: "Feature", arith: "Arithmetic",
};

function defaultOperand(kind: OperandKind, ctx: BuilderCtx): Operand {
  switch (kind) {
    case "const": return 0;
    case "param": {
      const p = paramsOfType(ctx.params, ["float", "integer"])[0];
      return p ? `$${p}` : 0;
    }
    case "bar": return { bar: "close" };
    case "feature": {
      const f = ctx.options.features.find((x) => x.id === "ema") ?? ctx.options.features[0];
      return { feature: f.id, output: f.outputs[0].name };
    }
    case "arith": return { arith: "add", args: [{ bar: "close" }, 0] };
  }
}

export function OperandEditor({ value, onChange, testId, depth = 0 }: {
  value: Operand | undefined; onChange: (v: Operand) => void; testId: string; depth?: number;
}) {
  const ctx = useBuilder();
  const kind = operandKind(value);
  const kinds: OperandKind[] = depth >= 2 ? ["const", "param", "bar", "feature"] : ["const", "param", "bar", "feature", "arith"];
  const numParams = paramsOfType(ctx.params, ["float", "integer"]);
  return (
    <div className={`operand operand-${kind}`} data-testid={testId}>
      <Select value={kind} ariaLabel="operand type" testId={`${testId}-kind`}
        onChange={(k) => onChange(defaultOperand(k as OperandKind, ctx))}
        options={kinds.map((k) => ({ value: k, label: KIND_LABEL[k], disabled: k === "param" && !numParams.length }))} />
      {kind === "const" && (
        <NumberInput value={typeof value === "number" ? value : 0} onChange={(v) => onChange(v ?? 0)} ariaLabel="constant" testId={`${testId}-const`} />
      )}
      {kind === "param" && (
        <Select value={String(value).slice(1)} onChange={(n) => onChange(`$${n}`)} options={paramOptions(numParams)} ariaLabel="parameter" testId={`${testId}-param`} />
      )}
      {kind === "bar" && typeof value === "object" && value && "bar" in value && (
        <span className="inline">
          <Select value={value.bar} onChange={(b) => onChange({ ...value, bar: b })} options={wordOptions(ctx.options.bar_fields)} ariaLabel="bar field" testId={`${testId}-bar`} />
          <LagEditor value={value.lag} onChange={(lag) => onChange(lag === undefined ? omit(value, "lag") : { ...value, lag })} testId={`${testId}-lag`} />
        </span>
      )}
      {kind === "feature" && typeof value === "object" && value && "feature" in value && (
        <FeatureOperandEditor value={value} onChange={onChange} testId={testId} />
      )}
      {kind === "arith" && typeof value === "object" && value && "arith" in value && (
        <div className="arith">
          <Select value={value.arith} onChange={(a) => onChange({ ...value, arith: a })} ariaLabel="arithmetic"
            options={ctx.options.arithmetic.map((a) => ({ value: a, label: { add: "+ add", sub: "− subtract", mul: "× multiply", div: "÷ divide" }[a] ?? a }))}
            testId={`${testId}-arith`} />
          <OperandEditor value={value.args[0]} onChange={(a) => onChange({ ...value, args: [a, value.args[1]] })} testId={`${testId}-a`} depth={depth + 1} />
          <OperandEditor value={value.args[1]} onChange={(b) => onChange({ ...value, args: [value.args[0], b] })} testId={`${testId}-b`} depth={depth + 1} />
        </div>
      )}
    </div>
  );
}

function LagEditor({ value, onChange, testId }: { value: unknown; onChange: (v: number | string | undefined) => void; testId: string }) {
  return (
    <span className="lag" title="bars back on the strategy timeframe (0 = current bar)">
      <span className="muted small">bars back</span>
      <ValueOrParam value={value} onChange={(v) => onChange(v === 0 ? undefined : (v as number | string | undefined))} kind="integer" ariaLabel="lag" testId={testId} />
    </span>
  );
}

/** A feature parameter's default for display (session defaults as session names). */
const defaultLabel = (d: unknown, session = false) => (session && typeof d === "string" ? facetLabel("session", d) : valueLabel(d));

export function featureInfo(options: BuilderOptions, id: string): FeatureInfo | undefined {
  return options.features.find((f) => f.id === id);
}

export function FeatureOperandEditor({ value, onChange, testId }: { value: FeatureOperand; onChange: (v: FeatureOperand) => void; testId: string }) {
  const ctx = useBuilder();
  const info = featureInfo(ctx.options, value.feature);
  const byCat = new Map<string, FeatureInfo[]>();
  ctx.options.features.forEach((f) => byCat.set(f.category, [...(byCat.get(f.category) ?? []), f]));
  const params = value.params ?? {};
  const setParam = (name: string, v: unknown) => {
    const next = { ...params } as Record<string, unknown>;
    if (v === undefined || v === "") delete next[name]; else next[name] = v;
    onChange(Object.keys(next).length ? { ...value, params: next } : omit(value, "params"));
  };
  const out = info?.outputs.find((o) => o.name === value.output);
  const htf = htfChoices(ctx);
  const tfParams = paramsOfType(ctx.params, ["timeframe"]);
  return (
    <div className="feature-op">
      <div className="inline">
        <select className="input" aria-label="feature" value={value.feature} data-testid={`${testId}-feature`}
          onChange={(e: { target: HTMLSelectElement }) => {
            const f = featureInfo(ctx.options, e.target.value);
            onChange({ feature: e.target.value, output: f?.outputs[0]?.name });
          }}>
          {[...byCat.entries()].map(([cat, fs]) => (
            <optgroup key={cat} label={humanize(cat)}>{fs.map((f) => <option key={f.id} value={f.id}>{humanize(f.id)}</option>)}</optgroup>
          ))}
        </select>
        <span className="muted">.</span>
        <Select value={value.output} ariaLabel="feature output" testId={`${testId}-output`}
          onChange={(o) => onChange({ ...value, output: o })} options={wordOptions((info?.outputs ?? []).map((o) => o.name))} />
      </div>
      {info && (
        <div className="feature-params">
          {info.params.map((p) => {
            const isSession = info.session_params.includes(p.name);
            const v = params[p.name];
            return (
              <label key={p.name} className="fp" title={p.doc}>
                <span className="fp-name">{humanize(p.name)}</span>
                {isSession ? (
                  <ValueOrParam value={v} onChange={(x) => setParam(p.name, x)} kind="select" ariaLabel={p.name}
                    choices={sessionChoices(ctx)}
                    placeholder={`default ${defaultLabel(p.default, true)}`} testId={`${testId}-p-${p.name}`} />
                ) : p.choices ? (
                  <ValueOrParam value={v} onChange={(x) => setParam(p.name, x)} kind="select" ariaLabel={p.name}
                    choices={p.choices.map((c) => ({ value: String(c), label: valueLabel(c) }))} placeholder={`default ${defaultLabel(p.default)}`}
                    testId={`${testId}-p-${p.name}`} />
                ) : p.type === "int" || p.type === "float" ? (
                  <ValueOrParam value={v} onChange={(x) => setParam(p.name, x)} kind={p.type === "int" ? "integer" : "number"}
                    ariaLabel={p.name} testId={`${testId}-p-${p.name}`} />
                ) : (
                  <ValueOrParam value={v} onChange={(x) => setParam(p.name, x)} kind="text" ariaLabel={p.name}
                    placeholder={`default ${defaultLabel(p.default)}`} testId={`${testId}-p-${p.name}`} />
                )}
                {v === undefined && <span className="muted small">default {defaultLabel(p.default, isSession)}</span>}
              </label>
            );
          })}
          <label className="fp" title="Higher-timeframe values use the last completed higher-timeframe bar.">
            <span className="fp-name">Timeframe</span>
            <select className="input" aria-label="feature timeframe" data-testid={`${testId}-tf`}
              value={value.timeframe ? String(value.timeframe) : ""}
              onChange={(e: { target: HTMLSelectElement }) =>
                onChange(e.target.value ? { ...value, timeframe: e.target.value } : omit(value, "timeframe"))}>
              <option value="">strategy timeframe ({facetLabel("timeframe", ctx.timeframe)})</option>
              {htf.map((h) => <option key={h.value} value={h.value}>{h.label} (higher)</option>)}
              {tfParams.map((n) => <option key={n} value={`$${n}`}>{`from parameter: ${humanize(n)}`}</option>)}
            </select>
          </label>
          <LagEditor value={value.lag} onChange={(lag) => onChange(lag === undefined ? omit(value, "lag") : { ...value, lag })} testId={`${testId}-lag`} />
        </div>
      )}
      <div className="feature-doc small muted">
        {info?.summary} {out ? <>· <b>{humanize(out.name)}</b>: {out.doc}</> : null}
        {value.timeframe ? <> · Higher-timeframe values use the last <b>completed</b> {isRef(value.timeframe)
          ? `bar of the timeframe set by the parameter ${humanize(String(value.timeframe).slice(1))}` : `${facetLabel("timeframe", value.timeframe)} bar`}.</> : null}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- conditions
function EnabledControl({ node, onChange, testId }: { node: Condition; onChange: (c: Condition) => void; testId: string }) {
  const { params } = useBuilder();
  const flags = paramsOfType(params, ["boolean"]);
  const cur = node.enabled === undefined || node.enabled === true ? "on" : node.enabled === false ? "off" : String(node.enabled);
  return (
    <select className="input input-sm" aria-label="condition enabled" value={cur} data-testid={`${testId}-enabled`}
      title="Optional conditions can be switched by an on/off parameter (and varied in controlled variations)"
      onChange={(e: { target: HTMLSelectElement }) => {
        const v = e.target.value;
        onChange(v === "on" ? omit(node, "enabled") : { ...node, enabled: v === "off" ? false : v } as Condition);
      }}>
      <option value="on">always on</option>
      <option value="off">disabled</option>
      {flags.map((f) => <option key={f} value={`$${f}`}>{`when ${humanize(f)} is on`}</option>)}
    </select>
  );
}

export function ConditionEditor({ node, onChange, onRemove, testId, depth = 0 }: {
  node: Condition; onChange: (c: Condition) => void; onRemove?: () => void; testId: string; depth?: number;
}) {
  const ctx = useBuilder();
  const kind = conditionKind(node);
  const meta = (
    <span className="cond-meta">
      <EnabledControl node={node} onChange={onChange} testId={testId} />
      <input className="input input-sm label-input" placeholder="label (optional)" aria-label="condition label"
        value={node.label ?? ""} onChange={(e: { target: HTMLInputElement }) =>
          onChange(e.target.value ? { ...node, label: e.target.value } : omit(node, "label"))} />
    </span>
  );
  if (kind === "all" || kind === "any") {
    const items = (kind === "all" ? (node as { all: Condition[] }).all : (node as { any: Condition[] }).any);
    const set = (next: Condition[]) => onChange({ ...omit(node, "all", "any"), [kind]: next } as Condition);
    return (
      <div className={`cond-group cond-${kind} depth-${depth % 3}`} data-testid={testId}>
        <div className="cond-head">
          <select className="input input-sm group-kind" aria-label="group type" value={kind} data-testid={`${testId}-kind`}
            onChange={(e: { target: HTMLSelectElement }) =>
              onChange({ ...omit(node, "all", "any"), [e.target.value]: items } as Condition)}>
            <option value="all">All of these (and)</option>
            <option value="any">Any of these (or)</option>
          </select>
          {meta}
          <span className="spacer" />
          <Button small onClick={() => set([...items, newComparison()])} testId={`${testId}-add`}>+ Condition</Button>
          <Button small onClick={() => set([...items, newGroup(kind === "all" ? "any" : "all")])} testId={`${testId}-addgroup`}>+ Group</Button>
          <Button small kind="ghost" onClick={() => onChange({ not: node })} title="negate this group" testId={`${testId}-not`}>NOT</Button>
          {onRemove && <Button small kind="ghost" onClick={onRemove} title="remove" testId={`${testId}-remove`}>✕</Button>}
        </div>
        <div className="cond-children">
          {items.length === 0 && <div className="muted small">Empty group — add a condition.</div>}
          {items.map((c, i) => (
            <ConditionEditor key={i} node={c} depth={depth + 1} testId={`${testId}-${i}`}
              onChange={(nc) => set(items.map((x, j) => (j === i ? nc : x)))}
              onRemove={() => set(items.filter((_, j) => j !== i))} />
          ))}
        </div>
      </div>
    );
  }
  if (kind === "not") {
    const inner = (node as { not: Condition }).not;
    return (
      <div className="cond-group cond-not" data-testid={testId}>
        <div className="cond-head">
          <b className="not-label">NOT</b>{meta}<span className="spacer" />
          <Button small kind="ghost" onClick={() => onChange(inner)} testId={`${testId}-unwrap`}>Remove NOT</Button>
          {onRemove && <Button small kind="ghost" onClick={onRemove} testId={`${testId}-remove`}>✕</Button>}
        </div>
        <div className="cond-children">
          <ConditionEditor node={inner} depth={depth + 1} testId={`${testId}-n`} onChange={(c) => onChange({ ...node, not: c } as Condition)} />
        </div>
      </div>
    );
  }
  const cmp = node as { left: Operand; op: string; right: Operand };
  return (
    <div className="cond-cmp" data-testid={testId}>
      <OperandEditor value={cmp.left} onChange={(l) => onChange({ ...cmp, left: l } as Condition)} testId={`${testId}-left`} />
      <Select value={cmp.op} ariaLabel="operator" testId={`${testId}-op`}
        onChange={(op) => onChange({ ...cmp, op } as Condition)}
        options={ctx.options.comparison_operators.map((o) => ({ value: o, label: opLabel(o) }))} />
      <OperandEditor value={cmp.right} onChange={(r) => onChange({ ...cmp, right: r } as Condition)} testId={`${testId}-right`} />
      <div className="cmp-tools">
        {meta}
        <Button small kind="ghost" onClick={() => onChange({ not: node })} title="negate" testId={`${testId}-not`}>NOT</Button>
        <Button small kind="ghost" onClick={() => onChange({ all: [node] })} title="wrap in a group" testId={`${testId}-wrap`}>Group</Button>
        {onRemove && <Button small kind="ghost" onClick={onRemove} title="remove" testId={`${testId}-remove`}>✕</Button>}
      </div>
    </div>
  );
}
