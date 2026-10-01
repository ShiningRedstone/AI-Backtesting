import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiError } from "../../api/client";
import type { Issue } from "../../api/types";
import { plainProse } from "../../app/labels";
import { useApp } from "../../app/context";

export function Button({ children, onClick, kind = "secondary", busy, busyLabel, disabled, title, type = "button", small, testId }: {
  children?: ReactNode; onClick?: () => void; kind?: "primary" | "secondary" | "danger" | "ghost";
  busy?: boolean; busyLabel?: string; disabled?: boolean; title?: string; type?: "button" | "submit"; small?: boolean; testId?: string;
}) {
  return (
    <button type={type} className={`btn btn-${kind}${small ? " btn-sm" : ""}`} onClick={onClick}
      disabled={disabled || busy} title={title} aria-busy={busy ? "true" : undefined} data-testid={testId}>
      {busy ? <><Spinner />{busyLabel ?? "Working…"}</> : children}
    </button>
  );
}

export const Spinner = () => <span className="spinner" aria-hidden="true" />;

export function Field({ label, hint, children, error, wide }: { label: string; hint?: ReactNode; children?: ReactNode; error?: string; wide?: boolean }) {
  return (
    <label className={`field${wide ? " field-wide" : ""}`}>
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
      {error && <span className="field-error">{error}</span>}
    </label>
  );
}

export function TextInput({ value, onChange, placeholder, testId, mono, multiline, ariaLabel }: {
  value: string | undefined | null; onChange: (v: string) => void; placeholder?: string; testId?: string; mono?: boolean;
  multiline?: boolean; ariaLabel?: string;
}) {
  const cls = `input${mono ? " mono" : ""}`;
  return multiline
    ? <textarea className={cls} value={value ?? ""} placeholder={placeholder} rows={3} aria-label={ariaLabel}
        onChange={(e: { target: HTMLTextAreaElement }) => onChange(e.target.value)} data-testid={testId} />
    : <input className={cls} value={value ?? ""} placeholder={placeholder} aria-label={ariaLabel}
        onChange={(e: { target: HTMLInputElement }) => onChange(e.target.value)} data-testid={testId} />;
}

/** Number input that keeps what the user types; reports a number (or undefined when empty). */
export function NumberInput({ value, onChange, step, testId, ariaLabel, integer }: {
  value: number | undefined | null; onChange: (v: number | undefined) => void; step?: number | string; testId?: string;
  ariaLabel?: string; integer?: boolean;
}) {
  const [text, setText] = useState(value === undefined || value === null ? "" : String(value));
  useEffect(() => {
    const parsed = text.trim() === "" ? undefined : Number(text);
    if (parsed !== value) setText(value === undefined || value === null ? "" : String(value));
  }, [value]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <input className="input num" inputMode={integer ? "numeric" : "decimal"} value={text} step={step} aria-label={ariaLabel}
      data-testid={testId}
      onChange={(e: { target: HTMLInputElement }) => {
        const t = e.target.value;
        setText(t);
        if (t.trim() === "") onChange(undefined);
        else if (!Number.isNaN(Number(t))) onChange(Number(t));
      }} />
  );
}

export function Select<T extends string>({ value, onChange, options, testId, ariaLabel, placeholder }: {
  value: T | undefined | null; onChange: (v: T) => void; options: (T | { value: T; label: string; disabled?: boolean })[];
  testId?: string; ariaLabel?: string; placeholder?: string;
}) {
  return (
    <select className="input" value={value ?? ""} aria-label={ariaLabel} data-testid={testId}
      onChange={(e: { target: HTMLSelectElement }) => onChange(e.target.value as T)}>
      {placeholder !== undefined && <option value="" disabled>{placeholder}</option>}
      {options.map((o) => typeof o === "string"
        ? <option key={o} value={o}>{o}</option>
        : <option key={o.value} value={o.value} disabled={o.disabled}>{o.label}</option>)}
    </select>
  );
}

export function Checkbox({ checked, onChange, label, testId, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: ReactNode; testId?: string; disabled?: boolean }) {
  return (
    <label className={`check${disabled ? " disabled" : ""}`}>
      <input type="checkbox" checked={checked} disabled={disabled} data-testid={testId}
        onChange={(e: { target: HTMLInputElement }) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

export function Badge({ children, tone = "neutral", title }: { children?: ReactNode; tone?: "neutral" | "ok" | "warn" | "error" | "info" | "demo"; title?: string }) {
  return <span className={`badge badge-${tone}`} title={title}>{children}</span>;
}

export const Mono = ({ children, title }: { children?: ReactNode; title?: string }) => <code className="mono" title={title}>{children}</code>;

export function Card({ title, actions, children, className, testId }: { title?: ReactNode; actions?: ReactNode; children?: ReactNode; className?: string; testId?: string }) {
  return (
    <section className={`card ${className ?? ""}`} data-testid={testId}>
      {(title || actions) && <header className="card-head"><h2>{title}</h2><div className="card-actions">{actions}</div></header>}
      <div className="card-body">{children}</div>
    </section>
  );
}

export function Banner({ tone = "info", children, testId }: { tone?: "info" | "warn" | "error" | "ok" | "demo"; children?: ReactNode; testId?: string }) {
  return <div className={`banner banner-${tone}`} role={tone === "error" ? "alert" : "status"} data-testid={testId}>{children}</div>;
}

export function IssueList({ issues, testId }: { issues: Issue[]; testId?: string }) {
  if (!issues.length) return null;
  return (
    <ul className="issues" data-testid={testId}>
      {issues.map((i, n) => (
        <li key={n} className={`issue issue-${i.severity}`}>
          <div className="issue-path mono">{i.path || "(document)"}</div>
          <div className="issue-msg">{i.message}</div>
          {i.hint && <div className="issue-hint">{i.hint}</div>}
        </li>
      ))}
    </ul>
  );
}

/** Human-readable error with the backend's own issues; stack/details only on request. */
export function ErrorPanel({ error, title, testId }: { error: ApiError | Error | null | undefined; title?: string; testId?: string }) {
  if (!error) return null;
  const e = error instanceof ApiError ? error : new ApiError(0, "client", error.message);
  return (
    <div className="error-panel" role="alert" data-testid={testId ?? "error-panel"}>
      <div className="error-title">{title ?? e.message}</div>
      {title && <div>{e.message}</div>}
      {e.reason && <div className="error-reason">{e.reason}</div>}
      <IssueList issues={e.issues.filter((i) => i.severity === "error")} />
      {e.details && (
        <details className="tech">
          <summary>Technical details</summary>
          <pre>{`HTTP ${e.status} · ${e.kind}\n\n${e.details}`}</pre>
        </details>
      )}
    </div>
  );
}

export function Confirm({ open, title, children, confirmLabel, danger, onConfirm, onCancel, busy }: {
  open: boolean; title: string; children?: ReactNode; confirmLabel: string; danger?: boolean;
  onConfirm: () => void; onCancel: () => void; busy?: boolean;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const id = useId();
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onCancel(); };
    window.addEventListener("keydown", onKey);
    ref.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);
  if (!open) return null;
  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby={id} ref={ref}
        onClick={(e: { stopPropagation: () => void }) => e.stopPropagation()}>
        <h2 id={id}>{title}</h2>
        <div className="modal-body">{children}</div>
        <div className="modal-actions">
          <Button onClick={onCancel}>Cancel</Button>
          <Button kind={danger ? "danger" : "primary"} onClick={onConfirm} busy={busy} testId="confirm-ok">{confirmLabel}</Button>
        </div>
      </div>
    </div>
  );
}

export function Tabs<T extends string>({ tabs, active, onChange, badges }: {
  tabs: { id: T; label: string }[]; active: T; onChange: (t: T) => void; badges?: Partial<Record<T, number>>;
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.id} role="tab" aria-selected={active === t.id} className={`tab${active === t.id ? " active" : ""}`}
          onClick={() => onChange(t.id)} data-testid={`tab-${t.id}`}>
          {t.label}{badges?.[t.id] ? <span className="tab-badge" title="validation issues">{badges[t.id]}</span> : null}
        </button>
      ))}
    </div>
  );
}

export const TableWrap = ({ children, testId, className }: { children?: ReactNode; testId?: string; className?: string }) =>
  <div className={`table-wrap${className ? ` ${className}` : ""}`} data-testid={testId}>{children}</div>;

/** Decorative metallic tile (CSS + inline SVG); carries no information. */
export function IconTile() {
  return (
    <span className="icon-tile" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <rect x="4" y="5" width="16" height="14" rx="3" /><path d="M4 10h16M9 14.5h6" /></svg>
    </span>
  );
}

export function Empty({ children }: { children?: ReactNode }) {
  return <div className="empty"><IconTile /><div className="empty-text">{children}</div></div>;
}

/** Skeleton placeholders shown while data loads (shape only; never values). */
export function Skeleton({ kind = "lines", rows = 4 }: { kind?: "lines" | "kpis" | "table" | "chart"; rows?: number }) {
  if (kind === "kpis") return <div className="kpis skel-kpis" aria-hidden="true">{Array.from({ length: rows }, (_, i) =>
    <div key={i} className="kpi"><span className="skel skel-line" style={{ width: "45%" }} /><span className="skel skel-num" /></div>)}</div>;
  if (kind === "table") return <div className="skel-table" aria-hidden="true">{Array.from({ length: rows }, (_, i) =>
    <span key={i} className="skel skel-row" style={{ opacity: 1 - i * (0.5 / rows) }} />)}</div>;
  if (kind === "chart") return <span className="skel skel-chart" aria-hidden="true" />;
  return <div className="skel-lines" aria-hidden="true">{Array.from({ length: rows }, (_, i) =>
    <span key={i} className="skel skel-line" style={{ width: `${[92, 76, 84, 60, 88, 70][i % 6]}%` }} />)}</div>;
}

/** Loading state: a skeleton of the coming content plus the label for screen readers. */
export function Loading({ label, kind = "page" }: { label: string; kind?: "page" | "lines" | "kpis" | "table" | "chart" }) {
  return (
    <div className="loading-skel" role="status" aria-label={label}>
      <span className="sr-only">{label}</span>
      {kind === "page" ? <><Skeleton kind="kpis" rows={4} /><Skeleton kind="table" rows={5} /></> : <Skeleton kind={kind} />}
    </div>
  );
}

export function KeyValues({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="kv">
      {rows.map(([k, v]) => <div key={k} className="kv-row"><dt>{k}</dt><dd>{v ?? <span className="muted">Not available</span>}</dd></div>)}
    </dl>
  );
}

/** Collapsed "Technical details": the only place machine identifiers (ids, hashes, raw keys, raw JSON) are shown. */
export function TechDetails({ rows, children, testId, summary = "Technical details" }: {
  rows?: [string, ReactNode][]; children?: ReactNode; testId?: string; summary?: string;
}) {
  const { prefs } = useApp();
  if (!prefs.show_ids) return null;                       // Settings → "Show IDs" (off by default)
  return (
    <details className="tech" data-testid={testId}><summary>{summary}</summary>
      {rows && <KeyValues rows={rows.filter(([, v]) => v !== null && v !== undefined && v !== "")} />}{children}</details>
  );
}

/** Shown only with Settings → "Show read-only information" (fixed design, read-only configuration, system panels). */
export function ReadOnly({ children }: { children?: ReactNode }) {
  const { prefs } = useApp();
  return prefs.show_readonly ? <>{children}</> : null;
}

/** An id shown as text only with Settings → "Show IDs"; otherwise nothing (or the fallback). */
export function IdText({ id, fallback = null }: { id: string | null | undefined; fallback?: ReactNode }) {
  const { prefs } = useApp();
  return prefs.show_ids && id ? <Mono>{id}</Mono> : <>{fallback}</>;
}

/** Yellow star: favorite a TESTED strategy (display metadata in the workspace; changes no result). */
export function FavStar({ id, testId }: { id: string; testId?: string }) {
  const { prefs, tested, toggleFavorite } = useApp();
  const on = prefs.favorites.includes(id);
  if (!on && !tested.has(id)) return <span className="fav-star off" title="Only tested strategies can be favorites" aria-hidden="true" />;
  return (
    <button type="button" className={`fav-star${on ? " on" : ""}`} aria-pressed={on} data-testid={testId ?? `fav-${id}`}
      title={on ? "Remove from favorites" : "Add to favorites"} aria-label={on ? "remove from favorites" : "add to favorites"}
      onClick={(e: { stopPropagation: () => void }) => { e.stopPropagation(); void toggleFavorite(id); }}>
      <svg width="15" height="15" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.8l2.8 5.9 6.4.8-4.7 4.4 1.2 6.4L12 17.2l-5.7 3.1 1.2-6.4-4.7-4.4 6.4-.8z"
        fill={on ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" /></svg>
    </button>
  );
}

/** A stored object (settings, assumptions, metrics) as labelled rows: keys and enum values in words, nested objects
    indented. Raw JSON, if wanted, belongs in TechDetails. */
export function ObjectView({ value, label = humanizeKey, depth = 0 }: { value: unknown; label?: (k: string) => string; depth?: number }) {
  if (value === null || value === undefined) return <span className="muted">—</span>;
  if (typeof value !== "object" || Array.isArray(value) && value.every((x) => typeof x !== "object" || x === null))
    return <>{displayValue(value)}</>;
  const entries = Array.isArray(value) ? value.map((x, i) => [String(i + 1), x] as [string, unknown]) : Object.entries(value as Record<string, unknown>);
  if (!entries.length) return <span className="muted">none</span>;
  return <dl className="kv" style={depth ? { margin: "2px 0 0 0" } : undefined}>
    {entries.map(([k, v]) => <div key={k} className="kv-row"><dt>{label(k)}</dt><dd><ObjectView value={v} label={label} depth={depth + 1} /></dd></div>)}
  </dl>;
}
const humanizeKey = (k: string) => k.replace(/([a-z0-9])([A-Z])/g, "$1 $2").split(/[_\-\s]+/).filter(Boolean)
  .map((w, i) => (/^(atr|ema|sma|rsi|usd|cfd|nq|es|mnq|oos|r|id|ui|ai|ny|utc|dsl)$/i.test(w) ? w.toUpperCase() : i ? w.toLowerCase() : w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())).join(" ");
function displayValue(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 4 });
  if (Array.isArray(v)) return v.map(displayValue).join(", ") || "none";
  const s = String(v);
  return /^[A-Za-z0-9]+(_[A-Za-z0-9]+)+$/.test(s) && !/^(STR|RUN|CTRL|VAL|RP|CMP|FM|AIP|PB|SB)_/.test(s) ? humanizeKey(s) : plainProse(s);
}

export function fmt(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 4 });
  if (typeof v === "boolean") return v ? "on" : "off";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** App event timestamps (created / started / finished / checked / built) in this computer's local time zone.
 *  Only strings with an explicit UTC offset are converted; anything else is shown as stored. Market, trade, session
 *  and backtest-period times never go through this helper: they keep the dataset's own time convention. */
const LOCAL_TIME = new Intl.DateTimeFormat(undefined, {
  year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZoneName: "short",
});
export const shortTime = (iso: string | null | undefined) => {
  if (!iso) return "—";
  const t = /(Z|[+-]\d{2}:?\d{2})$/.test(iso) ? Date.parse(iso) : NaN;
  return Number.isNaN(t) ? iso.replace("T", " ").slice(0, 16) : LOCAL_TIME.format(t);
};

// =========================================================================== research-terminal primitives
/** Numbers: fixed decimals, "—" when missing/non-finite. */
export function n(v: unknown, digits = 2): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return v === Infinity ? "∞" : "—";
  return v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
/** R values always carry the unit and an explicit sign. */
export function r(v: unknown, digits = 3): string {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)} R`;
}
export function pct(v: unknown, digits = 1): string {
  return typeof v === "number" && Number.isFinite(v) ? `${(v * 100).toFixed(digits)}%` : "—";
}
export const signCls = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? (v > 0 ? "pos" : v < 0 ? "neg" : "") : "");
export function bytes(v: number | null | undefined): string {
  if (typeof v !== "number") return "—";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0, x = v;
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
  return `${x.toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value);
  useEffect(() => { const t = window.setTimeout(() => setV(value), ms); return () => window.clearTimeout(t); }, [value, ms]);
  return v;
}

const ICONS: Record<string, string> = {
  home: "M3 10.5 12 3l9 7.5V21h-6v-6H9v6H3z", chart: "M4 20V10m6 10V4m6 16v-8m4 8H2", search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zm10 3-5-5",
  layers: "m12 3 9 5-9 5-9-5zm-9 9 9 5 9-5m-18 4 9 5 9-5", flask: "M9 3h6M10 3v6l-5 9a2 2 0 0 0 2 3h10a2 2 0 0 0 2-3l-5-9V3",
  list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01", grid: "M3 3h7v7H3zm11 0h7v7h-7zM3 14h7v7H3zm11 0h7v7h-7z",
  shuffle: "M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5", flow: "M5 12h14m-4-4 4 4-4 4M3 5h4m-4 14h4",
  shield: "M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z", wallet: "M3 7h18v12H3zm0 0 2-3h12l2 3M16 13h2",
  file: "M14 3H6v18h12V7zm0 0v4h4", db: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zm0 0v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6m-16 6c0 1.7 3.6 3 8 3s8-1.3 8-3",
  gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm8-3 2-1-2-4-2 1-2-1V5l-4-1-1 2h-2L8 4 4 5v2l-2 1 2 4-2 1 2 4 2-1 2 1v2l4 1 1-2h2l1 2 4-1v-2l2-1z",
  build: "M14 6l4 4-8 8H6v-4zM3 21h18", sparkle: "M12 3v4m0 10v4M3 12h4m10 0h4M6 6l2.5 2.5m7 7L18 18M6 18l2.5-2.5m7-7L18 6",
  compare: "M8 3v18M16 3v18M3 8h5m8 0h5M3 16h5m8 0h5", pause: "M8 5v14m8-14v14", tree: "M12 3v6m0 0-6 6m6-6 6 6M6 15v6m12-6v6",
};
export function Icon({ name, size = 15 }: { name: string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round"
      strokeLinejoin="round" aria-hidden="true"><path d={ICONS[name] ?? ICONS.list} /></svg>
  );
}

/** What a number is: net/gross, and which sample (in-sample, OOS, holdout, simulated, control, synthetic). */
export type ScopeKind = "net" | "gross" | "is" | "oos" | "wf" | "holdout" | "sim" | "control" | "synthetic" | "descriptive";
const SCOPE_TEXT: Record<ScopeKind, [string, string]> = {
  net: ["Net of costs", "scope-net"], gross: ["Gross", "scope-gross"], is: ["Discovery / in-sample", "scope-is"],
  oos: ["Out-of-sample", "scope-oos"], wf: ["Walk-forward", "scope-oos"], holdout: ["Holdout", "scope-holdout"],
  sim: ["Simulated", "scope-sim"], control: ["Randomized control", "scope-control"], synthetic: ["Synthetic data", "scope-synthetic"],
  descriptive: ["Descriptive", "scope-is"],
};
export function Scope({ kind, children }: { kind: ScopeKind; children?: ReactNode }) {
  const [text, cls] = SCOPE_TEXT[kind];
  return <span className={`scope ${cls}`}>{children ?? text}</span>;
}
/** A run's sample scope. A protocol holdout evaluation is stored with status OUT_OF_SAMPLE but is always shown as Holdout. */
export function ScopeOf({ status, holdout }: { status: string | null | undefined; holdout?: boolean | null }) {
  const k: ScopeKind = holdout ? "holdout" : status === "OUT_OF_SAMPLE" ? "oos" : status === "WALK_FORWARD" ? "wf" : "is";
  return <Scope kind={k} />;
}
export function Labels({ children }: { children?: ReactNode }) { return <span className="labels">{children}</span>; }

export function Kpi({ label, value, sub, tone, accent, testId, meter, meterTone }: {
  label: ReactNode; value: ReactNode; sub?: ReactNode; tone?: "pos" | "neg" | ""; accent?: boolean; testId?: string;
  meter?: number | null; meterTone?: "warn" | "error";
}) {
  return (
    <div className={`kpi${accent ? " accent" : ""}`} data-testid={testId}>
      <div className="kpi-label">{label}</div>
      <div className={`kpi-value ${tone ?? ""}`}>{value}</div>
      {sub != null && <div className="kpi-sub">{sub}</div>}
      {meter != null && <div className={`meter ${meterTone ?? ""}`}><span style={{ width: `${Math.max(0, Math.min(1, meter)) * 100}%` }} /></div>}
    </div>
  );
}

export function Modal({ open, title, children, actions, onClose, wide, testId }: {
  open: boolean; title: ReactNode; children?: ReactNode; actions?: ReactNode; onClose: () => void; wide?: boolean; testId?: string;
}) {
  const id = useId();
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className={`modal${wide ? " wide" : ""}`} role="dialog" aria-modal="true" aria-labelledby={id} data-testid={testId}
        onClick={(e: { stopPropagation: () => void }) => e.stopPropagation()}>
        <h2 id={id}>{title}</h2>
        <div className="modal-body">{children}</div>
        {actions && <div className="modal-actions">{actions}</div>}
      </div>
    </div>
  );
}

export function Drawer({ open, onClose, title, subtitle, actions, children, testId }: {
  open: boolean; onClose: () => void; title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children?: ReactNode; testId?: string;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true" data-testid={testId}>
        <header className="drawer-head">
          <div style={{ minWidth: 0, flex: 1 }}><h2 style={{ fontSize: 16 }}>{title}</h2>{subtitle && <div className="subtitle small">{subtitle}</div>}</div>
          <div className="actions">{actions}<Button small kind="ghost" onClick={onClose} testId="drawer-close">Close ✕</Button></div>
        </header>
        <div className="drawer-body">{children}</div>
      </aside>
    </>
  );
}

export function Pager({ page, pages, total, pageSize, onPage, onPageSize }: {
  page: number; pages: number; total: number; pageSize: number; onPage: (p: number) => void; onPageSize?: (n: number) => void;
}) {
  const from = total ? (page - 1) * pageSize + 1 : 0, to = Math.min(total, page * pageSize);
  return (
    <div className="pager" data-testid="pager">
      <span><b className="num">{from.toLocaleString()}–{to.toLocaleString()}</b> of <b className="num">{total.toLocaleString()}</b></span>
      <span className="spacer" />
      {onPageSize && <label className="inline small">Rows <select className="input input-sm" value={pageSize}
        onChange={(e: { target: HTMLSelectElement }) => onPageSize(Number(e.target.value))}>
        {[25, 50, 100, 200].map((x) => <option key={x} value={x}>{x}</option>)}</select></label>}
      <Button small disabled={page <= 1} onClick={() => onPage(1)}>«</Button>
      <Button small disabled={page <= 1} onClick={() => onPage(page - 1)} testId="page-prev">‹ Prev</Button>
      <span className="num">{page} / {pages}</span>
      <Button small disabled={page >= pages} onClick={() => onPage(page + 1)} testId="page-next">Next ›</Button>
      <Button small disabled={page >= pages} onClick={() => onPage(pages)}>»</Button>
    </div>
  );
}

/** Sortable header cell (server-side sort: it only reports the requested key/order). */
export function SortTh({ k, label, sort, order, onSort, right, title, width }: {
  k: string; label: ReactNode; sort: string; order: "asc" | "desc"; onSort: (k: string, o: "asc" | "desc") => void;
  right?: boolean; title?: string; width?: number;
}) {
  const on = sort === k;
  const [w, setW] = useState<number | undefined>(width);
  const drag = (e: MouseEvent) => {
    e.preventDefault(); e.stopPropagation();
    const th = (e.target as HTMLElement).parentElement as HTMLElement;
    const x0 = e.clientX, w0 = th.getBoundingClientRect().width;
    const move = (ev: MouseEvent) => setW(Math.max(48, w0 + ev.clientX - x0));
    const up = () => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
    window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
  };
  return (
    <th className={`sortable${on ? " sorted" : ""}${right ? " r" : ""}`} title={title} style={w ? { width: w, minWidth: w } : undefined}
      aria-sort={on ? (order === "asc" ? "ascending" : "descending") : "none"}
      onClick={() => onSort(k, on && order === "desc" ? "asc" : "desc")}>
      {label}{on ? (order === "asc" ? " ▲" : " ▼") : ""}
      <span className="resizer" onMouseDown={drag} onClick={(e: { stopPropagation: () => void }) => e.stopPropagation()} />
    </th>
  );
}
