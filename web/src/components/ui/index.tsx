import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ApiError } from "../../api/client";
import type { Issue } from "../../api/types";

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
export function IconTile({ icon = "empty" }: { icon?: "empty" | "planned" }) {
  return (
    <span className="icon-tile" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        {icon === "empty"
          ? <><rect x="4" y="5" width="16" height="14" rx="3" /><path d="M4 10h16M9 14.5h6" /></>
          : <><circle cx="12" cy="12" r="8" /><path d="M12 7.5V12l3 2" /></>}
      </svg>
    </span>
  );
}

export function Empty({ children }: { children?: ReactNode }) {
  return <div className="empty"><IconTile /><div className="empty-text">{children}</div></div>;
}

export function Loading({ label }: { label: string }) {
  return <div className="loading" role="status"><Spinner />{label}</div>;
}

export function KeyValues({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="kv">
      {rows.map(([k, v]) => <div key={k} className="kv-row"><dt>{k}</dt><dd>{v ?? <span className="muted">Not available</span>}</dd></div>)}
    </dl>
  );
}

export function fmt(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString() : v.toLocaleString(undefined, { maximumFractionDigits: 4 });
  if (typeof v === "boolean") return v ? "on" : "off";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export const shortTime = (iso: string | null | undefined) => (iso ? iso.replace("T", " ").slice(0, 16) : "—");
