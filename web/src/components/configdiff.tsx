import { useState } from "react";
import { api } from "../api/client";
import { useApp } from "../app/context";
import { Button, Confirm, Mono, TechDetails, fmt } from "./ui";

/** ADR-89: the preflight detail when the workspace's research settings differ from the protocol's. */
export type ConfigMismatch = {
  current_hash: string; protocol_hash: string; config_folder: string; env_overrides: string[];
  differences: { path: string; change: "added" | "removed" | "changed"; protocol: unknown; current: unknown }[];
  n_differences: number | null; restorable: boolean; reason: string | null; protocol_id?: string;
};

export const isConfigMismatch = (d: unknown): d is ConfigMismatch =>
  !!d && typeof d === "object" && "protocol_hash" in d && "differences" in d;

const show = (v: unknown) => v == null ? "—" : typeof v === "string" ? v : JSON.stringify(v);
const CHANGE = { added: "only in your current settings", removed: "missing from your current settings", changed: "different value" };

export function ConfigMismatchPanel({ d, onRestored }: { d: ConfigMismatch; onRestored?: () => void }) {
  const { toast } = useApp();
  const [ask, setAsk] = useState(false);
  const [busy, setBusy] = useState(false);
  const restore = async () => {
    setBusy(true);
    try {
      const out = await api.post<{ restored: boolean; files?: string[]; backup?: string; restart_required?: boolean }>(
        `/api/protocols/${encodeURIComponent(d.protocol_id ?? "")}/restore-config`, { confirm: "RESTORE" });
      setAsk(false);
      toast("ok", out.restored ? `Settings restored (${(out.files ?? []).join(", ")}); the old files are backed up.`
        + (out.restart_required ? " Restart Munyun Lab to use them." : "") : "Your settings already match the protocol.");
      onRestored?.();
    } catch (e) { toast("error", (e as Error).message); } finally { setBusy(false); }
  };
  const top = d.differences.slice(0, 12);
  return (
    <div className="config-mismatch" data-testid="config-mismatch">
      <p>Your workspace's research settings (the files in <Mono>{d.config_folder}</Mono>) are no longer the settings this
        research protocol was created with, so the protocol refuses to run. Nothing was evaluated.</p>
      {d.n_differences != null && <>
        <p><b>{fmt(d.n_differences)} setting{d.n_differences === 1 ? "" : "s"} differ</b>{d.n_differences > top.length ? ` (first ${top.length} shown)` : ""}:</p>
        <ul className="small" data-testid="config-differences">
          {top.map((x) => <li key={x.path}><Mono>{x.path}</Mono>: {CHANGE[x.change]}
            {x.change === "changed" && <> (protocol {show(x.protocol)}, now {show(x.current)})</>}</li>)}
        </ul></>}
      {!!d.env_overrides.length && <p className="small">Environment variables that change settings: {d.env_overrides.join(", ")}</p>}
      {d.restorable
        ? <Button small onClick={() => setAsk(true)} testId="config-restore">Restore the protocol's settings</Button>
        : <p className="small muted">{d.reason}</p>}
      <TechDetails rows={[["Current settings fingerprint", <Mono>{d.current_hash}</Mono>], ["Protocol's fingerprint", <Mono>{d.protocol_hash}</Mono>]]} />
      <Confirm open={ask} title="Restore the protocol's settings?" confirmLabel="Restore" busy={busy}
        onConfirm={restore} onCancel={() => setAsk(false)}>
        <p>The config files in <Mono>{d.config_folder}</Mono> are rewritten to exactly the settings recorded with this
          protocol's own backtests. This only happens if the result matches the protocol's fingerprint exactly; the
          current files are first copied to a <Mono>configs.backup-…</Mono> folder next to them. No result, strategy or
          protocol is changed.</p>
      </Confirm>
    </div>
  );
}
