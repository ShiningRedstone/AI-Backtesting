/* Application updates (backend: edgelab/updater). One shared poller for the whole UI. The backend does
   the checking, downloading, SHA-256 verification and hand-off; this file only shows state and asks. */
import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { AppVersion, UpdateStatus } from "../api/types";
import { href } from "../app/router";
import { humanize } from "../app/labels";
import { Badge, Banner, Button, Card, Checkbox, ErrorPanel, KeyValues, Loading, Mono, TechDetails, bytes, shortTime } from "./ui";

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
  timer = window.setTimeout(refresh, busy(s) ? 1500 : 5 * 60 * 1000);   // the backend re-checks GitHub every 30 min
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

type Rel = NonNullable<UpdateStatus["release"]>;
/** The identity the update actions use: a branch build's key ("0.2.0-b57") or a release version. */
export const relKey = (r: Rel) => r.key ?? r.version;
/** Plain-English label: "build 57" for a branch build, "version 0.3.0" for a versioned release. */
export const relLabel = (r: Rel) => (r.build_number ? `build ${r.build_number}` : `version ${r.version}`);
const currentLabel = (s: UpdateStatus) => (s.current_build ? `build ${s.current_build}` : `version ${s.current_version}`);

export function VersionChip() {
  const [s] = useUpdates();
  const avail = !!s?.available && !s.skipped;
  return (
    <a className={`chip${avail ? " accent" : ""}`} href={href("/settings?tab=about")} data-testid="version-chip"
      title={avail && s?.release ? `EdgeLab ${relLabel(s.release)} is available` : `EdgeLab ${UI_VERSION}`}>
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

/** Shown across the top of the app whenever a newer build of this installation's branch exists (ADR-72).
 *  "Restart and update" runs the backend's one-click install: check, download, SHA-256 verify, then the helper
 *  swaps the application folder and EdgeLab reopens on the new build (rolled back if it does not start). */
export function UpdateBanner() {
  const [s] = useUpdates();
  const [err, setErr] = useState<ApiError | null>(null);
  const [hidden, setHidden] = useState<string | null>(null);
  const rel = s?.release;
  const inst = s?.install;
  const installing = inst?.state === "running" || inst?.state === "applying";
  if (!s || !rel || !s.available || s.skipped || (hidden === relKey(rel) && !installing)) return null;
  const go = async () => {
    setErr(null);
    try { await updateAction("install"); } catch (e) { setErr(e as ApiError); }
  };
  const later = async () => {
    setHidden(relKey(rel));
    try { await updateAction("later", { version: relKey(rel) }); } catch { /* hiding locally is enough */ }
  };
  const step = inst?.state === "applying" || inst?.step === "applying" ? "Restarting into the new build…"
    : inst?.step === "downloading" ? "Downloading and verifying…" : "Checking…";
  return (
    <div className="update-banner" role="status" data-testid="update-banner">
      <span><b>Update available:</b> {relLabel(rel)}{rel.channel ? <> of <b>{rel.channel}</b></> : null}
        {" "}<span className="muted">(you have {currentLabel(s)})</span></span>
      {installing && <span className="muted small" data-testid="update-banner-step">{step} EdgeLab will close and reopen.</span>}
      {inst?.state === "error" && inst.error && <span className="neg small">Not installed: {inst.error.message}</span>}
      {err && <span className="neg small">{err.message}</span>}
      <span className="spacer" />
      {!installing && <Button small kind="ghost" onClick={later} testId="update-later">Later</Button>}
      <Button small kind="primary" onClick={go} busy={installing} busyLabel={step} disabled={!s.apply_supported}
        title={s.apply_unsupported_reason ?? "Download, verify and restart into the new build"} testId="update-now">Restart and update</Button>
    </div>
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
      {inst?.state === "up_to_date" && <Banner tone="info" testId="update-uptodate">EdgeLab {currentLabel(s)} is up to date; nothing to install.</Banner>}
      {installing && <Banner tone="info" testId="update-installing">{stepLabel} EdgeLab will close and reopen on the new version
        {inst?.version ? ` (${inst.version})` : ""}.</Banner>}
      {inst?.state === "error" && inst.error && <Banner tone="error" testId="update-install-error"><b>{humanize(inst.error.code)}</b> — {inst.error.message}
        {" "}Nothing was installed; the current version is unchanged.</Banner>}
      <KeyValues rows={[
        ["Installed", <>version {s.current_version}{s.current_build ? `, build ${s.current_build}` : ""}
          {" "}{v && v.version !== UI_VERSION && <Badge tone="error">UI {UI_VERSION} ≠ backend {v.version}</Badge>}</>],
        ["Update branch", s.channel ? <b>{s.channel}</b> : <span className="muted">none (this is not a branch build)</span>],
        ["Latest available", rel ? <>{relLabel(rel)} {s.available ? <Badge tone="ok">newer</Badge> : null}
          {s.skipped ? <Badge tone="warn">skipped</Badge> : null} <span className="small muted">{rel.published_at ? shortTime(rel.published_at) : ""}</span></> : "—"],
        ["Last check", s.check.checked_at ? `${shortTime(s.check.checked_at)} · ${humanize(s.check.state).toLowerCase()}` : humanize(s.check.state)],
        ["Check result", s.check.error ? <span className="warn">{humanize(s.check.error.code)}: {s.check.error.message}</span> : (s.note ?? (s.available ? "update available" : "—"))],
        ["Release source", <span className="small">{s.source}</span>], ["Platform", s.platform],
        ["Install location", s.install_dir ? "installed desktop app" : <span className="muted">development run (updates not installed)</span>],
        ["Last update", s.last_update ? `${humanize(s.last_update.event)} ${String(s.last_update.version ?? "")} · ${shortTime(String(s.last_update.at))}`
          + (s.last_update.error ? ` · ${String(s.last_update.error)}` : "") : "none recorded"]]} />
      <TechDetails rows={[["Install folder", s.install_dir ? <Mono>{s.install_dir}</Mono> : null], ["Download staging", <Mono>{s.cache_dir}</Mono>],
        ["Update log", <Mono>{s.log}</Mono>]]} />
      <div className="inline" style={{ marginTop: 10 }}>
        <Checkbox checked={s.auto_check} onChange={(on) => void act("preferences", { auto_check: on })()}
          label="Check for updates automatically (at start-up, then every 30 minutes)"
          testId="update-auto" />
      </div>
      {s.available && rel && <div className="actions" style={{ marginTop: 10 }}>
        <Button kind="primary" onClick={act("install")} busy={installing} busyLabel={stepLabel} disabled={!s.apply_supported}
          title={s.apply_unsupported_reason ?? undefined}>Update to {relLabel(rel)}</Button>
        {s.skipped ? <Button onClick={act("unskip", { version: relKey(rel) })}>Stop skipping {relLabel(rel)}</Button>
          : <Button kind="ghost" onClick={act("skip", { version: relKey(rel) })}>Skip {relLabel(rel)}</Button>}
      </div>}
      {s.download.state !== "idle" && <div style={{ marginTop: 10 }}><Progress s={s} /></div>}
      {s.download.state === "error" && s.download.error && <Banner tone="error"><b>{humanize(s.download.error.code)}</b> — {s.download.error.message}</Banner>}
      {s.skipped_versions.length > 0 && <p className="small muted">Skipped versions: {s.skipped_versions.join(", ")}</p>}
      <ErrorPanel error={err} />
      <p className="small muted">EdgeLab works fully offline; a failed check never affects research. Updates come from the builds GitHub
        makes of this installation's own branch (ShiningRedstone/AI-Backtesting) and are verified by SHA-256 before anything is installed.
        Your research workspace and settings are never touched.</p>
    </Card>
  );
}

export function useVersion() {
  const [data, setData] = useState<AppVersion | null>(null);
  useEffect(() => { api.get<AppVersion>("/api/version").then(setData).catch(() => undefined); }, []);
  return { data };
}
