/* Application updates (backend: edgelab/updater). One shared poller for the whole UI. The backend does
   the checking, downloading, SHA-256 verification and hand-off; this file only shows state and asks. */
import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { AppVersion, UpdateStatus } from "../api/types";
import { href } from "../app/router";
import { Badge, Banner, Button, Card, Checkbox, ErrorPanel, KeyValues, Loading, Modal, Mono, bytes, shortTime } from "./ui";

export const UI_VERSION = typeof __EDGELAB_VERSION__ === "string" ? __EDGELAB_VERSION__ : "unknown";

type Listener = (s: UpdateStatus | null, e: ApiError | null) => void;
const listeners = new Set<Listener>();
let current: UpdateStatus | null = null;
let lastError: ApiError | null = null;
let timer: number | undefined;

function busy(s: UpdateStatus | null) {
  return !!s && (s.check.state === "checking" || s.download.state === "downloading" || s.download.state === "verifying" || !!s.applying
    || s.install?.state === "running" || s.install?.state === "applying");
}
function publish(s: UpdateStatus | null, e: ApiError | null) {
  current = s; lastError = e;
  listeners.forEach((l) => l(s, e));
  window.clearTimeout(timer);
  timer = window.setTimeout(refresh, busy(s) ? 1500 : 10 * 60 * 1000);
}
export function refresh() {
  api.get<UpdateStatus>("/api/update/status").then((s) => publish(s, null)).catch((e: ApiError) => publish(current, e));
}
export async function updateAction(path: string, body: Record<string, unknown> = {}) {
  const s = await api.post<UpdateStatus>(`/api/update/${path}`, body);
  publish(s, null);
  return s;
}
export function useUpdates(): [UpdateStatus | null, ApiError | null] {
  const [st, setSt] = useState<[UpdateStatus | null, ApiError | null]>([current, lastError]);
  useEffect(() => {
    const l: Listener = (s, e) => setSt([s, e]);
    listeners.add(l);
    if (!current && listeners.size === 1) refresh();
    return () => { listeners.delete(l); };
  }, []);
  return st;
}

export function VersionChip() {
  const [s] = useUpdates();
  const avail = !!s?.available && !s.skipped;
  return (
    <a className={`chip${avail ? " accent" : ""}`} href={href("/settings?tab=about")} data-testid="version-chip"
      title={avail ? `EdgeLab ${s?.release?.version} is available` : `EdgeLab ${UI_VERSION}`}>
      v{UI_VERSION}{avail && <b>· update</b>}
    </a>
  );
}

function Progress({ s }: { s: UpdateStatus }) {
  const d = s.download;
  const frac = d.total ? Math.min(1, d.bytes / d.total) : 0;
  return (
    <div data-testid="update-progress">
      <div className="progress"><span style={{ width: `${d.state === "downloading" ? frac * 100 : 100}%` }} /></div>
      <p className="small muted">{d.state === "downloading" ? `Downloading ${bytes(d.bytes)} of ${bytes(d.total)}…`
        : d.state === "verifying" ? "Verifying the SHA-256 checksum…" : d.state === "ready" ? "Downloaded and verified (SHA-256 matches the release)." : ""}</p>
    </div>
  );
}

/** The prompt shown when a newer published release exists (not skipped, not deferred this session). */
export function UpdateDialog() {
  const [s] = useUpdates();
  const [err, setErr] = useState<ApiError | null>(null);
  const [closed, setClosed] = useState(false);
  const rel = s?.release;
  const inProgress = !!s && ["downloading", "verifying", "ready"].includes(s.download.state) && s.download.version === rel?.version;
  const open = !!s && !!rel && !closed && (s.prompt || inProgress || !!s.applying);
  if (!open || !s || !rel) return null;
  const act = (path: string) => async () => {
    setErr(null);
    try {
      await updateAction(path, { version: rel.version });
      if (path === "later" || path === "skip") setClosed(true);
    } catch (e) { setErr(e as ApiError); }
  };
  const d = s.download;
  return (
    <Modal open title="A new EdgeLab version is available" wide onClose={() => { if (!busy(s)) void act("later")(); }} testId="update-dialog"
      actions={s.applying ? null : d.state === "ready" ? <>
        <Button onClick={act("later")}>Later</Button>
        <Button kind="primary" onClick={act("apply")} disabled={!s.apply_supported} testId="update-apply"
          title={s.apply_unsupported_reason ?? undefined}>Restart and update</Button></> : <>
        <Button kind="ghost" onClick={act("skip")} testId="update-skip" disabled={busy(s)}>Skip this version</Button>
        <Button onClick={act("later")} testId="update-later" disabled={busy(s)}>Later</Button>
        <Button kind="primary" onClick={act("download")} busy={d.state === "downloading" || d.state === "verifying"} busyLabel="Downloading…"
          testId="update-now">Update now</Button></>}>
      <KeyValues rows={[["Current version", <Mono>{s.current_version}</Mono>], ["New version", <b className="pos">{rel.version}</b>],
        ["Released", rel.published_at ? shortTime(rel.published_at) : "—"], ["Download size", bytes(rel.size)],
        ["Source", <span className="small">{s.source}</span>]]} />
      {rel.notes && <div className="update-notes" data-testid="update-notes">{rel.notes}</div>}
      {(d.state !== "idle" && d.version === rel.version) && <Progress s={s} />}
      {d.state === "error" && d.error && <Banner tone="error" testId="update-error"><b>{d.error.code}</b> — {d.error.message}
        {" "}Nothing was installed; the current version is unchanged.</Banner>}
      {s.applying && <Banner tone="info">EdgeLab will close; the update helper replaces the application folder and starts the new version.</Banner>}
      {!s.apply_supported && <p className="small muted">{s.apply_unsupported_reason}</p>}
      <ErrorPanel error={err} />
      <p className="small muted">Updates come only from published releases and are installed only after the SHA-256 checksum matches.
        Your research workspace and settings are never touched.</p>
    </Modal>
  );
}

/** Settings & About: full updater status and manual actions. */
export function UpdatePanel() {
  const [s, e] = useUpdates();
  const [err, setErr] = useState<ApiError | null>(null);
  const { data: v } = useVersion();
  if (e && !s) return <Card title="Updates"><ErrorPanel error={e} /></Card>;
  if (!s) return <Card title="Updates"><Loading label="Loading update status…" /></Card>;
  const act = (path: string, body: Record<string, unknown> = {}) => async () => {
    setErr(null);
    try { await updateAction(path, body); } catch (x) { setErr(x as ApiError); }
  };
  const rel = s.release;
  const inst = s.install;
  const installing = inst?.state === "running" || inst?.state === "applying";
  const stepLabel = inst?.state === "applying" || inst?.step === "applying" ? "Restarting into the new version…"
    : inst?.step === "downloading" ? "Downloading and verifying…" : "Checking for updates…";
  return (
    <Card title="Updates" testId="update-panel" actions={<>
      <Button small onClick={act("check")} busy={s.check.state === "checking" && !installing} disabled={installing}
        busyLabel="Checking…" testId="update-check">Check for updates</Button>
      <Button small kind="primary" onClick={act("install")} busy={installing} busyLabel={stepLabel} disabled={!s.apply_supported}
        title={s.apply_unsupported_reason ?? "Check, download, verify and restart into the newest release"} testId="update-install">Update now</Button></>}>
      {inst?.state === "up_to_date" && <Banner tone="info" testId="update-uptodate">EdgeLab {s.current_version} is up to date; nothing to install.</Banner>}
      {installing && <Banner tone="info" testId="update-installing">{stepLabel} EdgeLab will close and reopen on the new version
        {inst?.version ? ` (${inst.version})` : ""}.</Banner>}
      {inst?.state === "error" && inst.error && <Banner tone="error" testId="update-install-error"><b>{inst.error.code}</b> — {inst.error.message}
        {" "}Nothing was installed; the current version is unchanged.</Banner>}
      <KeyValues rows={[
        ["Installed version", <><Mono>{s.current_version}</Mono> {v && v.version !== UI_VERSION && <Badge tone="error">UI {UI_VERSION} ≠ backend {v.version}</Badge>}</>],
        ["Latest published", rel ? <><Mono>{rel.version}</Mono> {s.available ? <Badge tone="ok">newer</Badge> : null}
          {s.skipped ? <Badge tone="warn">skipped</Badge> : null} <span className="small muted">{rel.published_at ? shortTime(rel.published_at) : ""}</span></> : "—"],
        ["Last check", s.check.checked_at ? `${shortTime(s.check.checked_at)} · ${s.check.state}` : s.check.state],
        ["Check result", s.check.error ? <span className="warn">{s.check.error.code}: {s.check.error.message}</span> : (s.note ?? (s.available ? "update available" : "—"))],
        ["Release source", <span className="small">{s.source}</span>], ["Platform", s.platform],
        ["Install location", s.install_dir ? <Mono>{s.install_dir}</Mono> : <span className="muted">development run (updates not installed)</span>],
        ["Download staging", <Mono>{s.cache_dir}</Mono>], ["Update log", <Mono>{s.log}</Mono>],
        ["Last update", s.last_update ? `${String(s.last_update.event)} ${String(s.last_update.version ?? "")} · ${shortTime(String(s.last_update.at))}`
          + (s.last_update.error ? ` · ${String(s.last_update.error)}` : "") : "none recorded"]]} />
      <div className="inline" style={{ marginTop: 10 }}>
        <Checkbox checked={s.auto_check} onChange={(on) => void act("preferences", { auto_check: on })()} label="Check for updates at start-up"
          testId="update-auto" />
      </div>
      {s.available && rel && <div className="actions" style={{ marginTop: 10 }}>
        <Button kind="primary" onClick={act("install")} busy={installing} busyLabel={stepLabel} disabled={!s.apply_supported}
          title={s.apply_unsupported_reason ?? undefined}>Update to {rel.version}</Button>
        {s.skipped ? <Button onClick={act("unskip", { version: rel.version })}>Stop skipping {rel.version}</Button>
          : <Button kind="ghost" onClick={act("skip", { version: rel.version })}>Skip {rel.version}</Button>}
      </div>}
      {s.download.state !== "idle" && <div style={{ marginTop: 10 }}><Progress s={s} /></div>}
      {s.download.state === "error" && s.download.error && <Banner tone="error"><b>{s.download.error.code}</b> — {s.download.error.message}</Banner>}
      {s.skipped_versions.length > 0 && <p className="small muted">Skipped versions: {s.skipped_versions.join(", ")}</p>}
      <ErrorPanel error={err} />
      <p className="small muted">EdgeLab works fully offline; a failed check never affects research. Releases come from GitHub Releases of
        ShiningRedstone/AI-Backtesting, never from a branch, and are verified by SHA-256 before anything is installed.</p>
    </Card>
  );
}

export function useVersion() {
  const [data, setData] = useState<AppVersion | null>(null);
  useEffect(() => { api.get<AppVersion>("/api/version").then(setData).catch(() => undefined); }, []);
  return { data };
}
