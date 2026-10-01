import { useState } from "react";
import type { Issue } from "../../api/types";
import { countRefs, newGroup, omit, renameRefs } from "../../dsl/edit";
import type { Condition, LocalSession, ParamDecl, StopTarget, StrategyDoc } from "../../dsl/types";
import { isRef } from "../../dsl/types";
import { facetLabel, humanize, valueLabel } from "../../app/labels";
import { Banner, Button, Checkbox, Field, IssueList, NumberInput, Select, TextInput } from "../ui";
import { ConditionEditor, OperandEditor, ValueOrParam, htfChoices, paramsOfType, sessionChoices, useBuilder, wordOptions, zoneLabel } from "./editors";

export type SectionId = "general" | "market" | "parameters" | "entry" | "exit" | "sizing" | "review";
export type SetDoc = (fn: (d: StrategyDoc) => StrategyDoc) => void;

/** Which builder section a backend issue path belongs to. */
export function sectionOf(path: string): SectionId {
  if (/^(timeframe|sessions|entry\.session|entry\.trading_weekdays)/.test(path)) return "market";
  if (path.startsWith("parameters")) return "parameters";
  if (path.startsWith("entry")) return "entry";
  if (path.startsWith("exit")) return "exit";
  if (path.startsWith("sizing")) return "sizing";
  return "general";
}

const WEEKDAY_LABEL: Record<string, string> = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
const PARAM_TYPE_LABEL: Record<string, string> = {
  integer: "Whole number", float: "Decimal number", boolean: "On / off", choice: "Choice", timeframe: "Timeframe",
};
const typeOptions = (types: string[]) => wordOptions(types, (t) => PARAM_TYPE_LABEL[t] ?? humanize(t));
/** Plain-English notes for exits the form cannot set (the backend validator's exact wording is the tooltip). */
const UNSUPPORTED_NOTE: Record<string, string> = {
  trailing_stop: "not set up in this form; a trailing exit is declared in the trailing-exit block of the strategy definition",
  breakeven: "not set up in this form; a breakeven move is declared in the trailing-exit block of the strategy definition",
  partial_exits: "partial exits or scaling out are not supported (one fill in, one fill out)",
  pyramiding: "pyramiding or scaling in is not supported (one position at a time)",
};
const tfLabel = (t: string) => facetLabel("timeframe", t);
/** The engine's session-flatten settings as words. */
const flattenText = (f: Record<string, unknown>) =>
  `flatten daily: ${valueLabel(f.flatten_daily)}, flatten time: ${valueLabel(f.flatten_time)}, hold overnight: ${valueLabel(f.hold_overnight)}`;

// =========================================================================== general
export function GeneralSection({ doc, setDoc, issues }: { doc: StrategyDoc; setDoc: SetDoc; issues: Issue[] }) {
  const fam = doc.family ?? {};
  const setFam = (k: string, v: string) => setDoc((d) => ({ ...d, family: { ...(d.family ?? {}), [k]: v } }));
  return (
    <div className="section">
      <IssueList issues={issues} />
      <div className="grid2">
        <Field label="Strategy name" hint="Human name. Does not change the strategy's logic identity.">
          <TextInput value={doc.name} onChange={(v) => setDoc((d) => ({ ...d, name: v }))} testId="f-name" />
        </Field>
        <Field label="Category">
          <TextInput value={fam.category} onChange={(v) => setFam("category", v)} placeholder="breakout, trend, mean reversion…" testId="f-category" />
        </Field>
        <Field label="Description" wide>
          <TextInput value={doc.description} multiline onChange={(v) => setDoc((d) => ({ ...d, description: v }))} testId="f-description" />
        </Field>
      </div>
      <h3>Strategy family</h3>
      <p className="muted small">The family is the market hypothesis; every saved instance of it is a concrete canonical
        definition. Describe the hypothesis to be tested — never expected performance.</p>
      <div className="grid2">
        <Field label="Family code" hint="A short code that groups every version of this idea: lowercase letters, digits and underscores only.">
          <TextInput value={fam.id} mono onChange={(v) => setFam("id", v)} testId="f-family-id" />
        </Field>
        <Field label="Family name">
          <TextInput value={fam.name} onChange={(v) => setFam("name", v)} testId="f-family-name" />
        </Field>
        <Field label="Hypothesis" wide hint="What market behaviour this family tests.">
          <TextInput value={fam.hypothesis} multiline onChange={(v) => setFam("hypothesis", v)} testId="f-hypothesis" />
        </Field>
      </div>
    </div>
  );
}

// =========================================================================== market / sessions
export function MarketSection({ doc, setDoc, issues }: { doc: StrategyDoc; setDoc: SetDoc; issues: Issue[] }) {
  const ctx = useBuilder();
  const tfParams = paramsOfType(ctx.params, ["timeframe"]);
  const local = doc.sessions ?? {};
  const [newName, setNewName] = useState("");
  const setLocal = (next: Record<string, LocalSession>) =>
    setDoc((d) => (Object.keys(next).length ? { ...d, sessions: next } : omit(d, "sessions")));
  const wd = doc.entry.trading_weekdays ?? [];
  const flat = ctx.options.session_flatten;
  return (
    <div className="section">
      <IssueList issues={issues} />
      <div className="grid2">
        <Field label="Strategy timeframe"
          hint="Strategy timeframe must match the dataset timeframe when the strategy is bound for execution. Higher-timeframe feature references are configured inside feature operands.">
          <select className="input" value={doc.timeframe} data-testid="f-timeframe" aria-label="strategy timeframe"
            onChange={(e: { target: HTMLSelectElement }) => setDoc((d) => ({ ...d, timeframe: e.target.value }))}>
            {ctx.options.timeframes.map((t) => <option key={t} value={t}>{tfLabel(t)}</option>)}
            {tfParams.map((n) => <option key={n} value={`$${n}`}>{`from parameter: ${humanize(n)}`}</option>)}
            {!ctx.options.timeframes.includes(doc.timeframe) && !isRef(doc.timeframe) && <option value={doc.timeframe}>{tfLabel(doc.timeframe)}</option>}
          </select>
        </Field>
        <Field label="Trading window" hint="A strategy has one trading window: a signal bar must OPEN inside it. Session windows can also be referenced by session-based features.">
          <select className="input" value={doc.entry.session ?? ""} data-testid="f-entry-session" aria-label="trading window"
            onChange={(e: { target: HTMLSelectElement }) => setDoc((d) => ({
              ...d, entry: e.target.value ? { ...d.entry, session: e.target.value } : omit(d.entry, "session") }))}>
            <option value="">any time (no window)</option>
            {sessionChoices(ctx).map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </Field>
      </div>
      <Field label="Trading weekdays" hint="Weekday of the TRADING DATE (a Sunday-evening CME bar belongs to Monday). None selected = every day.">
        <div className="checks" data-testid="f-weekdays">
          {ctx.options.weekdays.map((w) => (
            <Checkbox key={w} label={WEEKDAY_LABEL[w] ?? w} checked={wd.includes(w)} testId={`wd-${w}`}
              onChange={(on) => setDoc((d) => {
                const cur = d.entry.trading_weekdays ?? [];
                const next = on ? ctx.options.weekdays.filter((x) => x === w || cur.includes(x)) : cur.filter((x) => x !== w);
                return { ...d, entry: next.length ? { ...d.entry, trading_weekdays: next } : omit(d.entry, "trading_weekdays") };
              })} />
          ))}
        </div>
      </Field>

      <h3>Configured sessions <span className="muted small">(from the sessions configuration file — daylight-saving safe, computed by the backend)</span></h3>
      <div className="session-list">
        {Object.values(ctx.options.sessions).map((s) => (
          <span key={s.name} className="session-chip" title={`${zoneLabel(s.timezone)}, ${s.weekdays.map((w) => WEEKDAY_LABEL[w] ?? w).join(", ")}`}>
            <b>{facetLabel("session", s.name)}</b> {s.start}–{s.end} <span className="muted">{zoneLabel(s.timezone)}</span>
          </span>
        ))}
      </div>

      <h3>Strategy-local sessions</h3>
      <p className="muted small">Windows defined inside this strategy (e.g. opening ranges). They become part of the strategy's identity.</p>
      {Object.entries(local).map(([name, s]) => (
        <div className="local-session" key={name} data-testid={`local-session-${name}`}>
          <b title={name}>{humanize(name)}</b>
          <Field label="Timezone"><TextInput value={s.timezone} onChange={(v) => setLocal({ ...local, [name]: { ...s, timezone: v } })} /></Field>
          <Field label="Start"><input className="input" type="time" value={s.start} aria-label={`${name} start`}
            onChange={(e: { target: HTMLInputElement }) => setLocal({ ...local, [name]: { ...s, start: e.target.value } })} /></Field>
          <Field label="End"><input className="input" type="time" value={s.end} aria-label={`${name} end`}
            onChange={(e: { target: HTMLInputElement }) => setLocal({ ...local, [name]: { ...s, end: e.target.value } })} /></Field>
          <div className="checks">
            {ctx.options.weekdays.map((w) => {
              const days = s.weekdays ?? ["mon", "tue", "wed", "thu", "fri"];
              return <Checkbox key={w} label={WEEKDAY_LABEL[w]} checked={days.includes(w)}
                onChange={(on) => setLocal({ ...local, [name]: { ...s, weekdays: on ? ctx.options.weekdays.filter((x) => x === w || days.includes(x)) : days.filter((x) => x !== w) } })} />;
            })}
          </div>
          <Button small kind="ghost" onClick={() => setLocal(omit(local, name))}>Remove</Button>
        </div>
      ))}
      <div className="inline">
        <TextInput value={newName} onChange={(v) => setNewName(v.toUpperCase().replace(/[^A-Z0-9_]/g, "_"))} placeholder="New session name" mono testId="new-session-name" ariaLabel="new session name" />
        <Button small disabled={!newName || newName in local} testId="add-session"
          onClick={() => { setLocal({ ...local, [newName]: { timezone: "America/New_York", start: "09:30", end: "10:30", weekdays: ["mon", "tue", "wed", "thu", "fri"] } }); setNewName(""); }}>
          + Add session</Button>
      </div>

      <h3>Session flatten</h3>
      <p className="muted small">Engine configuration (the backtest settings file), applied to every strategy; not part of the strategy definition.
        Current: <b>{flattenText(flat)}</b>.</p>
    </div>
  );
}

// =========================================================================== parameters
function defaultDecl(type: string, timeframes: string[]): ParamDecl {
  switch (type) {
    case "integer": return { type, value: 10, min: 5, max: 50, step: 5 };
    case "float": return { type, value: 1.0, min: 0.5, max: 3.0, step: 0.25 };
    case "boolean": return { type, value: false };
    case "choice": return { type, value: "a", choices: ["a", "b"] };
    default: return { type: "timeframe", value: timeframes.includes("60m") ? "60m" : timeframes[0] };  // choices optional
  }
}

export function ParametersSection({ doc, setDoc, issues }: { doc: StrategyDoc; setDoc: SetDoc; issues: Issue[] }) {
  const ctx = useBuilder();
  const params = doc.parameters ?? {};
  const [name, setName] = useState("");
  const [type, setType] = useState("float");
  const setParams = (next: Record<string, ParamDecl>) => setDoc((d) => ({ ...d, parameters: next }));
  const setDecl = (n: string, decl: ParamDecl) => setParams({ ...params, [n]: decl });
  const rename = (from: string, to: string) => setDoc((d) => {
    const body = renameRefs(omit(d, "parameters"), from, to);
    const next = Object.fromEntries(Object.entries(d.parameters ?? {}).map(([k, v]) => [k === from ? to : k, v]));
    return { ...body, parameters: next } as StrategyDoc;
  });
  const valid = /^[a-z][a-z0-9_]*$/.test(name);
  return (
    <div className="section">
      <p className="muted small">Parameters are the declared knobs of the hypothesis. Any value that offers “from a parameter” can use them.
        Their minimum, maximum, step and choices define the only space controlled variations may explore. The backend validator checks every declaration.</p>
      <IssueList issues={issues} />
      <div className="params">
        {Object.entries(params).map(([n, d]) => (
          <ParamCard key={n} name={n} decl={d} refs={countRefs(omit(doc, "parameters"), n)}
            issues={issues.filter((i) => i.path === `parameters.${n}` || i.path.startsWith(`parameters.${n}.`))}
            timeframes={ctx.options.timeframes} types={ctx.options.parameter_types}
            onChange={(nd) => setDecl(n, nd)} onRename={(to) => rename(n, to)}
            onDelete={() => setParams(omit(params, n))} existing={Object.keys(params)} />
        ))}
        {!Object.keys(params).length && <div className="muted">No parameters yet.</div>}
      </div>
      <div className="add-param">
        <Field label="New parameter name"><TextInput value={name} mono onChange={(v) => setName(v.toLowerCase())} placeholder="stop_atr" testId="new-param-name" /></Field>
        <Field label="Type"><Select value={type} onChange={setType} options={typeOptions(ctx.options.parameter_types)} testId="new-param-type" /></Field>
        <Button kind="primary" disabled={!valid || name in params} testId="add-param"
          title={!valid ? "use lowercase letters, digits and underscores, starting with a letter" : name in params ? "already exists" : undefined}
          onClick={() => { setDecl(name, defaultDecl(type, ctx.options.timeframes)); setName(""); }}>+ Add parameter</Button>
      </div>
    </div>
  );
}

function ParamCard({ name, decl, refs, issues, timeframes, types, onChange, onRename, onDelete, existing }: {
  name: string; decl: ParamDecl; refs: number; issues: Issue[]; timeframes: string[]; types: string[];
  onChange: (d: ParamDecl) => void; onRename: (to: string) => void; onDelete: () => void; existing: string[];
}) {
  const [editName, setEditName] = useState(name);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const num = decl.type === "integer" || decl.type === "float";
  const choicesText = (decl.choices ?? []).map(String).join(", ");
  const setBound = (k: "min" | "max" | "step", v: number | undefined) => onChange(v === undefined ? omit(decl, k) : { ...decl, [k]: v });
  return (
    <div className={`param-card${issues.some((i) => i.severity === "error") ? " has-error" : ""}`} data-testid={`param-${name}`}>
      <div className="param-head">
        <span className="mono">$</span>
        <input className="input mono param-name" value={editName} aria-label="parameter name"
          onChange={(e: { target: HTMLInputElement }) => setEditName(e.target.value.toLowerCase())}
          onBlur={() => {
            if (editName !== name && /^[a-z][a-z0-9_]*$/.test(editName) && !existing.includes(editName)) onRename(editName);
            else setEditName(name);
          }} />
        <Select value={decl.type} ariaLabel="parameter type" testId={`param-${name}-type`}
          onChange={(t) => onChange(defaultDecl(t, timeframes))} options={typeOptions(types)} />
        <span className="muted small">{refs ? `used in ${refs} place${refs > 1 ? "s" : ""}` : "not used yet"}</span>
        <span className="spacer" />
        {confirmDelete
          ? <>{refs > 0 && <span className="warn small">still referenced — validation will fail</span>}
              <Button small kind="danger" onClick={onDelete} testId={`param-${name}-delete-confirm`}>Delete</Button>
              <Button small onClick={() => setConfirmDelete(false)}>Keep</Button></>
          : <Button small kind="ghost" onClick={() => setConfirmDelete(true)} testId={`param-${name}-delete`}>✕</Button>}
      </div>
      <div className="param-body">
        {num && <>
          <Field label="Value"><NumberInput value={decl.value as number} integer={decl.type === "integer"} onChange={(v) => onChange({ ...decl, value: v })} testId={`param-${name}-value`} /></Field>
          <Field label="Minimum"><NumberInput value={decl.min} onChange={(v) => setBound("min", v)} testId={`param-${name}-min`} /></Field>
          <Field label="Maximum"><NumberInput value={decl.max} onChange={(v) => setBound("max", v)} testId={`param-${name}-max`} /></Field>
          <Field label="Step"><NumberInput value={decl.step} onChange={(v) => setBound("step", v)} testId={`param-${name}-step`} /></Field>
        </>}
        {decl.type === "boolean" && (
          <Field label="Default"><Checkbox label={decl.value ? "On" : "Off"} checked={Boolean(decl.value)} onChange={(v) => onChange({ ...decl, value: v })} testId={`param-${name}-value`} /></Field>
        )}
        {decl.type === "choice" && <>
          <Field label="Choices" hint="comma-separated (e.g. session names)">
            <TextInput value={choicesText} testId={`param-${name}-choices`}
              onChange={(v) => {
                const ch = v.split(",").map((x) => x.trim()).filter(Boolean);
                onChange({ ...decl, choices: ch, value: ch.includes(String(decl.value)) ? decl.value : ch[0] });
              }} />
          </Field>
          <Field label="Current"><Select value={String(decl.value)} onChange={(v) => onChange({ ...decl, value: v })} options={wordOptions((decl.choices ?? []).map(String))} testId={`param-${name}-value`} /></Field>
        </>}
        {decl.type === "timeframe" && <>
          <Field label="Current"><Select value={String(decl.value)} onChange={(v) => onChange({ ...decl, value: v })} options={wordOptions(timeframes, tfLabel)} testId={`param-${name}-value`} /></Field>
          <Field label="Choices" hint="allowed values (optional)">
            <div className="checks">{timeframes.map((t) => (
              <Checkbox key={t} label={tfLabel(t)} checked={(decl.choices ?? []).map(String).includes(t)} testId={`param-${name}-choice-${t}`}
                onChange={(on) => {
                  const cur = (decl.choices ?? []).map(String);
                  const ch = on ? timeframes.filter((x) => x === t || cur.includes(x)) : cur.filter((x) => x !== t);
                  onChange(ch.length ? { ...decl, choices: ch } : omit(decl, "choices"));
                }} />))}</div>
          </Field>
        </>}
        <Field label="Description" wide><TextInput value={decl.description} onChange={(v) => onChange(v ? { ...decl, description: v } : omit(decl, "description"))} /></Field>
      </div>
      <IssueList issues={issues} />
    </div>
  );
}

// =========================================================================== entry
export function EntrySection({ doc, setDoc, issues }: { doc: StrategyDoc; setDoc: SetDoc; issues: Issue[] }) {
  const ctx = useBuilder();
  const [stash, setStash] = useState<{ long?: Condition; short?: Condition }>({});
  const e = doc.entry;
  const order = e.order ?? { type: "market" };
  const sides = e.direction === "both" ? ["long", "short"] : [e.direction];
  const setEntry = (fn: (x: StrategyDoc["entry"]) => StrategyDoc["entry"]) => setDoc((d) => ({ ...d, entry: fn(d.entry) }));
  const setDirection = (dir: string) => setEntry((x) => {
    const want = dir === "both" ? ["long", "short"] : [dir];
    let next: StrategyDoc["entry"] = { ...x, direction: dir };
    const keep: { long?: Condition; short?: Condition } = { ...stash };
    for (const side of ["long", "short"] as const) {
      if (want.includes(side) && !next[side]) next = { ...next, [side]: stash[side] ?? newGroup("all") };
      if (!want.includes(side) && next[side]) { keep[side] = next[side]; next = omit(next, side); }
    }
    const ord = next.order;
    if (ord && ord.type !== "market") {
      let o = { ...ord };
      for (const side of ["long", "short"] as const) if (!want.includes(side)) o = omit(o, `${side}_price`);
      next = { ...next, order: o };
    }
    setStash(keep);
    return next;
  });
  const setOrderType = (t: string) => setEntry((x) => ({
    ...x, order: t === "market" ? { type: "market" } : {
      type: t, expiry_bars: (x.order?.expiry_bars as number) ?? 1,
      ...(sides.includes("long") ? { long_price: x.order?.long_price ?? { bar: t === "stop" ? "high" : "low" } } : {}),
      ...(sides.includes("short") ? { short_price: x.order?.short_price ?? { bar: t === "stop" ? "low" : "high" } } : {}),
    } }));
  return (
    <div className="section">
      <IssueList issues={issues} />
      <div className="grid3">
        <Field label="Direction">
          <div className="segmented" role="radiogroup" aria-label="direction">
            {ctx.options.directions.map((d) => (
              <button key={d} role="radio" aria-checked={e.direction === d} className={e.direction === d ? "on" : ""}
                onClick={() => setDirection(d)} data-testid={`dir-${d}`}>{d[0].toUpperCase() + d.slice(1)}</button>
            ))}
          </div>
        </Field>
        <Field label="Entry order" hint={order.type === "market" ? "Fills at the next bar's open." : "Works from the next bar at the operand's value on the signal bar."}>
          <Select value={order.type} onChange={setOrderType} testId="f-order-type"
            options={wordOptions(ctx.options.entry_order_types)} />
        </Field>
        <Field label="Cooldown (bars between signals)" hint="Measured between signals, not from trade exits.">
          <ValueOrParam value={e.cooldown_bars} kind="integer" testId="f-cooldown"
            onChange={(v) => setEntry((x) => (v === undefined || v === 0 ? omit(x, "cooldown_bars") : { ...x, cooldown_bars: v as number }))} />
        </Field>
      </div>
      {order.type !== "market" && (
        <div className="pending">
          {sides.map((side) => (
            <Field key={side} label={`${side === "long" ? "Long" : "Short"} ${order.type} price`}>
              <OperandEditor value={(order as Record<string, unknown>)[`${side}_price`] as never} testId={`order-${side}-price`}
                onChange={(o) => setEntry((x) => ({ ...x, order: { ...(x.order ?? { type: order.type }), [`${side}_price`]: o } }))} />
            </Field>
          ))}
          <Field label="Expiry (bars)">
            <ValueOrParam value={order.expiry_bars} kind="integer" testId="f-expiry"
              onChange={(v) => setEntry((x) => ({ ...x, order: { ...(x.order ?? { type: order.type }), expiry_bars: v as number } }))} />
          </Field>
        </div>
      )}
      {sides.map((side) => (
        <div key={side} className="cond-root">
          <h3>Enter {side.toUpperCase()} when</h3>
          {e[side as "long" | "short"] ? (
            <ConditionEditor node={e[side as "long" | "short"] as Condition} testId={`cond-${side}`}
              onChange={(c) => setEntry((x) => ({ ...x, [side]: c }))} />
          ) : <Button onClick={() => setEntry((x) => ({ ...x, [side]: newGroup("all") }))}>+ Add condition</Button>}
        </div>
      ))}
    </div>
  );
}

// =========================================================================== exit
function StopTargetEditor({ which, value, onChange, types, sides, testId }: {
  which: "stop" | "target"; value: StopTarget; onChange: (v: StopTarget) => void; types: string[]; sides: string[]; testId: string;
}) {
  const ctx = useBuilder();
  const labels: Record<string, string> = { none: "None", points: "Points", atr: "ATR multiple", price: "Price level", risk_reward: "R-multiple" };
  const set = (k: string, v: unknown) => onChange(v === undefined ? omit(value, k) : { ...value, [k]: v } as StopTarget);
  return (
    <div className="stop-target">
      <Field label={which === "stop" ? "Stop" : "Target"}>
        <select className="input" value={value.type} data-testid={`${testId}-type`} aria-label={`${which} type`}
          onChange={(e: { target: HTMLSelectElement }) => {
            const t = e.target.value;
            onChange(t === "points" ? { type: t, points: 10 } : t === "atr" ? { type: t, multiple: 1.5, period: 14 }
              : t === "risk_reward" ? { type: t, multiple: 2 } : t === "price"
                ? { type: t, ...Object.fromEntries(sides.map((s) => [s, { bar: s === "long" ? "low" : "high" }])) } : { type: t });
          }}>
          {which === "stop" && <option value="" disabled>None (a protective stop is required)</option>}
          {types.map((t) => <option key={t} value={t}>{labels[t] ?? facetLabel(which === "stop" ? "stop_type" : "target_type", t)}</option>)}
        </select>
      </Field>
      {value.type === "points" && <Field label="Points"><ValueOrParam value={value.points} kind="number" onChange={(v) => set("points", v)} testId={`${testId}-points`} /></Field>}
      {(value.type === "atr" || value.type === "risk_reward") && (
        <Field label={value.type === "atr" ? "ATR multiple" : "R multiple"}>
          <ValueOrParam value={value.multiple} kind="number" onChange={(v) => set("multiple", v)} testId={`${testId}-multiple`} />
        </Field>)}
      {value.type === "atr" && <>
        <Field label="ATR period"><ValueOrParam value={value.period ?? 14} kind="integer" onChange={(v) => set("period", v)} testId={`${testId}-period`} /></Field>
        <Field label="ATR timeframe" hint="Higher-timeframe values use the last completed higher-timeframe bar.">
          <select className="input" value={value.timeframe ?? ""} aria-label={`${which} ATR timeframe`}
            onChange={(e: { target: HTMLSelectElement }) => set("timeframe", e.target.value || undefined)}>
            <option value="">strategy timeframe</option>
            {htfChoices(ctx).map((h) => <option key={h.value} value={h.value}>{h.label}</option>)}
          </select>
        </Field>
      </>}
      {value.type === "price" && sides.map((s) => (
        <Field key={s} label={`${s === "long" ? "Long" : "Short"} ${which} level`}>
          <OperandEditor value={value[s as "long" | "short"]} onChange={(o) => set(s, o)} testId={`${testId}-${s}`} />
        </Field>
      ))}
      {value.type === "risk_reward" && <span className="muted small">Target distance = multiple × distance to the stop.</span>}
    </div>
  );
}

export function ExitSection({ doc, setDoc, issues }: { doc: StrategyDoc; setDoc: SetDoc; issues: Issue[] }) {
  const ctx = useBuilder();
  const x = doc.exit;
  const sides = doc.entry.direction === "both" ? ["long", "short"] : [doc.entry.direction];
  const setExit = (fn: (e: StrategyDoc["exit"]) => StrategyDoc["exit"]) => setDoc((d) => ({ ...d, exit: fn(d.exit) }));
  const optNum = (k: "time_stop_bars" | "max_hold_bars", label: string, hint: string) => (
    <Field label={label} hint={hint}>
      <div className="inline">
        <Checkbox label="on" checked={x[k] !== undefined} testId={`f-${k}-on`}
          onChange={(on) => setExit((e) => (on ? { ...e, [k]: 12 } : omit(e, k)))} />
        {x[k] !== undefined && <ValueOrParam value={x[k]} kind="integer" onChange={(v) => setExit((e) => ({ ...e, [k]: v as number }))} testId={`f-${k}`} />}
      </div>
    </Field>
  );
  const sig = x.signal ?? {};
  return (
    <div className="section">
      <IssueList issues={issues} />
      <StopTargetEditor which="stop" value={x.stop ?? { type: "points" }} types={ctx.options.stop_types} sides={sides} testId="stop"
        onChange={(v) => setExit((e) => ({ ...e, stop: v }))} />
      <StopTargetEditor which="target" value={x.target ?? { type: "none" }} types={ctx.options.target_types} sides={sides} testId="target"
        onChange={(v) => setExit((e) => ({ ...e, target: v }))} />
      <div className="grid2">
        {optNum("time_stop_bars", "Time stop (bars)", "Exit at the close of the Nth bar after entry.")}
        {optNum("max_hold_bars", "Maximum hold (bars)", "Hard limit on bars in the trade.")}
      </div>
      <h3>Signal exits</h3>
      <p className="muted small">When the condition is true at a bar's close, the position exits at the next bar's open (market order).
        Earlier stop / target / session / time exits win.</p>
      {sides.map((side) => (
        <div key={side} className="cond-root">
          <Checkbox label={`Exit ${side} positions on a condition`} checked={Boolean(sig[side as "long" | "short"])} testId={`sigexit-${side}-on`}
            onChange={(on) => setExit((e) => {
              const cur = { ...(e.signal ?? {}) } as Record<string, Condition>;
              if (on) cur[side] = newGroup("all"); else delete cur[side];
              return Object.keys(cur).length ? { ...e, signal: cur } : omit(e, "signal");
            })} />
          {sig[side as "long" | "short"] && (
            <ConditionEditor node={sig[side as "long" | "short"] as Condition} testId={`sigexit-${side}`}
              onChange={(c) => setExit((e) => ({ ...e, signal: { ...(e.signal ?? {}), [side]: c } }))} />
          )}
        </div>
      ))}
      <h3>Session flatten</h3>
      <p className="muted small">Configured for the engine in the backtest settings file ({flattenText(ctx.options.session_flatten)}); not a strategy setting.</p>
      <h3>Not available in this form</h3>
      <div className="unsupported" data-testid="unsupported-exits">
        {["trailing_stop", "breakeven", "partial_exits", "pyramiding"].map((k) => (
          <Checkbox key={k} disabled checked={false} onChange={() => undefined}
            label={<><s>{humanize(k)}</s> <span className="muted small" title={ctx.options.unsupported[k]}>— {UNSUPPORTED_NOTE[k] ?? "not supported"}</span></>} />
        ))}
      </div>
    </div>
  );
}

// =========================================================================== sizing
export function SizingSection({ doc, setDoc, issues }: { doc: StrategyDoc; setDoc: SetDoc; issues: Issue[] }) {
  const ctx = useBuilder();
  const sz = doc.sizing ?? { mode: "fixed", quantity: 1 };
  const set = (next: StrategyDoc["sizing"]) => setDoc((d) => ({ ...d, sizing: next }));
  return (
    <div className="section">
      <IssueList issues={issues} />
      <div className="segmented" role="radiogroup" aria-label="sizing mode">
        {ctx.options.sizing_modes.map((m) => (
          <button key={m} role="radio" aria-checked={sz.mode === m} className={sz.mode === m ? "on" : ""} data-testid={`sizing-${m}`}
            onClick={() => set(m === "fixed" ? { mode: "fixed", quantity: 1 } : { mode: "risk", risk_usd: 500 })}>
            {m === "fixed" ? "Fixed quantity" : m === "risk" ? "Risk-based" : humanize(m)}</button>
        ))}
      </div>
      <div className="grid2">
        {sz.mode === "fixed" && (
          <Field label="Quantity" hint="Contracts (futures) or units (CFD).">
            <ValueOrParam value={sz.quantity} kind="number" onChange={(v) => set({ ...sz, quantity: v as number })} testId="f-quantity" />
          </Field>)}
        {sz.mode === "risk" && <>
          <Field label="Risk budget per trade (USD)" hint="Your risk budget — not a broker value.">
            <ValueOrParam value={sz.risk_usd} kind="number" onChange={(v) => set({ ...sz, risk_usd: v as number })} testId="f-risk-usd" />
          </Field>
          <Field label="Maximum quantity (optional)">
            <ValueOrParam value={sz.max_quantity} kind="integer" testId="f-max-qty"
              onChange={(v) => set(v === undefined ? omit(sz, "max_quantity") : { ...sz, max_quantity: v as number })} />
          </Field>
        </>}
      </div>
      <Banner tone="info">Instrument-specific minimum size and size step are applied when the strategy is bound to a dataset
        (e.g. 0.5 NQ contracts is refused; CFD lot rules are broker-specific and come from configuration, never from this form).</Banner>
    </div>
  );
}
