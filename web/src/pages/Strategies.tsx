import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { api, ApiError } from "../api/client";
import type { ExplainResult, LibraryRow, LineageResponse, StoredStrategy, StrategyResearch, SystemStatus } from "../api/types";
import { go, href, useRoute } from "../app/router";
import { useApi, useApp } from "../app/context";
import { facetLabel, familyLabel, strategyLabel, valueLabel } from "../app/labels";
import { BacktestPanel, LineageTable, LineageTree, VariationBuilder, changesText, methodLabel, nodeLabel } from "../components/strategy";
import type { TreeNode } from "../components/strategy";
import { BatchResearch, ProvenanceCard, RunsTable, ValidationPanel } from "../components/strategy/lab";
import { ChooseWorkspaceLink } from "../components/workspace";
import { RulesTable } from "../components/research";
import { Badge, Banner, Button, Card, Checkbox, FavStar, Pager, Confirm, Empty, ErrorPanel, KeyValues, Loading, Mono, Select, TableWrap, Tabs, TechDetails, TextInput, fmt, shortTime } from "../components/ui";
import type { StrategyDoc } from "../dsl/types";

// =========================================================================== system panel (Settings & About)
export function SystemPanel() {
  const { data: s, error, loading } = useApi<SystemStatus>("/api/status");
  const { data: build } = useApi<{ source_sha256: string; built_at: string; react: string; esbuild: string }>("/build-info.json");
  if (error) return <ErrorPanel error={error} title="Backend status unavailable" />;
  if (loading || !s) return <Loading label="Loading status…" />;
  const actions: { label: string; to: string; enabled: boolean; why?: string; testId: string }[] = [
    { label: "Create Strategy", to: "/builder?new=1", enabled: true, testId: "qa-create" },
    { label: "Open Strategy Library", to: "/strategies", enabled: true, testId: "qa-library" },
    { label: "Import Dataset", to: "/datasets?import=1", enabled: true, testId: "qa-import" },
    { label: "Generate Variations", to: "/strategies", enabled: s.strategies > 0, why: "save a strategy first", testId: "qa-variations" },
    { label: "Run Single Backtest", to: "/strategies", enabled: s.strategies > 0 && s.datasets > 0,
      why: s.datasets ? "save a strategy first" : "import a dataset first", testId: "qa-backtest" },
  ];
  return (
      <div className="grid-cards">
        <Card title="System status" testId="system-status">
          <KeyValues rows={[
            ["Backend", <Badge tone="ok">{s.backend}</Badge>],
            ["Workspace", <>{s.demo ? <Badge tone="demo">demo</Badge> : <Badge>research</Badge>} <span className="small mono">{s.root}</span></>],
            ["Local changes", s.code_version.git_commit ? (s.code_version.dirty ? "Modified since the last commit" : "None") : "Not a git checkout"],
            ["Frontend build", build ? <span className="small">{shortTime(build.built_at)} · React {build.react}</span> : null],
            ["Tests", s.test_status ?? <span className="muted">Not available — run the test suite script (see Technical details)</span>],
            ["Storage", valueLabel(s.store_backend)]]} />
          <TechDetails rows={[["Software version (commit)", s.code_version.git_commit ? <Mono>{s.code_version.git_commit}</Mono> : null],
            ["Source hash", s.code_version.source_sha256 ? <Mono>{s.code_version.source_sha256}</Mono> : null],
            ["Config hash", <Mono>{s.config_hash}</Mono>], ["Frontend source hash", build ? <Mono>{build.source_sha256}</Mono> : null],
            ["Test command", s.test_status ? null : <code>python scripts/run_tests.py</code>]]} />
        </Card>
        <Card title="Workspace">
          <div className="stats">
            <a href={href("/datasets")}><b data-testid="count-datasets">{s.datasets}</b><span>datasets</span></a>
            <a href={href("/strategies")}><b data-testid="count-strategies">{s.strategies}</b><span>saved strategies</span></a>
            <a href={href("/families")}><b>{s.families}</b><span>strategy families</span></a>
            <a href={href("/variations")}><b>{s.variation_batches}</b><span>variation batches</span></a>
            <a href={href("/results")}><b>{s.runs}</b><span>recorded runs</span></a>
          </div>
          <KeyValues rows={[["Last run", s.last_run ? <a href={href(`/results/${s.last_run.run_id}`)} title={String(s.last_run.run_id)}>
            Open the last run{s.last_run.created_at ? ` (${shortTime(String(s.last_run.created_at))})` : ""}</a> : "none yet"],
            ["Research searches", <a href={href("/research")}>Experiments</a>]]} />
        </Card>
        <Card title="Quick actions">
          <div className="quick">
            {actions.map((a) => (
              <Button key={a.label} kind={a.label === "Create Strategy" ? "primary" : "secondary"} disabled={!a.enabled}
                title={a.enabled ? undefined : a.why} onClick={() => go(a.to)} testId={a.testId}>{a.label}</Button>
            ))}
          </div>
          {actions.filter((a) => !a.enabled).map((a) => <p key={a.label} className="muted small">{a.label}: {a.why}.</p>)}
        </Card>
      </div>
  );
}

// =========================================================================== library
export function LibraryPage() {
  const { toast } = useApp();
  const [archived, setArchived] = useState(false);
  const { data, error, loading, reload } = useApi<LibraryRow[]>(`/api/strategies${archived ? "?archived=1" : ""}`, [archived]);
  const [q, setQ] = useState("");
  const [fam, setFam] = useState("");
  const [confirm, setConfirm] = useState<LibraryRow | null>(null);
  const [busy, setBusy] = useState(false);
  const [actErr, setActErr] = useState<ApiError | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  useEffect(() => setPage(1), [q, fam, archived]);
  if (error) return <ErrorPanel error={error} />;
  const all = (data ?? []).filter((r) => (!fam || r.family_id === fam)
    && (!q || `${r.display_name ?? ""} ${r.name} ${r.strategy_id} ${r.family_id}`.toLowerCase().includes(q.toLowerCase())));
  const pages = Math.max(1, Math.ceil(all.length / pageSize));
  const rows = all.slice((page - 1) * pageSize, page * pageSize);        // a page at a time: thousands of rows stay fast
  const families = [...new Set((data ?? []).map((r) => r.family_id))].sort();
  const toggleArchive = async (r: LibraryRow) => {
    setBusy(true); setActErr(null);
    try {
      await api.post(`/api/strategies/${r.strategy_id}/${r.archived ? "restore" : "archive"}`);
      toast("ok", `${strategyLabel(r.name)} ${r.archived ? "restored" : "archived"}`);
      setConfirm(null); reload();
    } catch (e) { setActErr(e as ApiError); } finally { setBusy(false); }
  };
  return (
    <div className="page">
      <header className="page-head">
        <h1>Strategy Library</h1>
        <div className="actions"><Button kind="primary" onClick={() => go("/builder?new=1")}>Create Strategy</Button></div>
      </header>
      <div className="filters">
        <TextInput value={q} onChange={setQ} placeholder="Filter by name, ID or family" ariaLabel="filter strategies" testId="library-filter" />
        <Select value={fam} onChange={setFam} ariaLabel="family filter" options={[{ value: "", label: "All families" }, ...families.map((f) => ({ value: f, label: familyLabel(f) }))]} />
        <Checkbox checked={archived} onChange={setArchived} label="Show archived" testId="show-archived" />
      </div>
      <ErrorPanel error={actErr} />
      {loading && !data ? <Loading label="Loading strategies…" /> : !rows.length ? (
        <Empty>{data?.length ? "No strategies match the filter." : <span data-testid="library-empty">No saved strategies in this research workspace. <a href={href("/builder?new=1")}>Create one</a> in the
          Strategy Builder, or <ChooseWorkspaceLink /> that holds your strategies.</span>}</Empty>
      ) : (
        <TableWrap testId="library-table">
          <table>
            <thead><tr><th style={{ width: 34 }} aria-label="favorite" /><th>Name</th><th>Family</th><th>Timeframe</th><th>Created</th><th>Parent</th><th>Parameters</th><th>Status</th><th>Actions</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.strategy_id} className={r.archived ? "disabled-row" : ""} data-testid={`row-${r.strategy_id}`}>
                <td><FavStar id={r.strategy_id} /></td>
                <td><a href={href(`/strategies/${r.strategy_id}`)} title={r.strategy_id}>{r.short_name ?? strategyLabel(r.name)}</a></td>
                <td><a href={href(`/families/${r.family_id}`)} title={r.family_id}>{familyLabel(r.family_id)}</a></td>
                <td>{facetLabel("timeframe", r.timeframe)}</td><td className="small">{shortTime(r.created_at)}</td>
                <td>{r.parent_strategy_id ? <a href={href(`/strategies/${r.parent_strategy_id}`)} title={r.parent_strategy_id}>
                  {strategyLabel((data ?? []).find((x) => x.strategy_id === r.parent_strategy_id)?.name ?? "Parent version")}</a> : <span className="muted">—</span>}</td>
                <td>{r.n_parameters}</td>
                <td>{r.archived ? <Badge tone="warn">archived</Badge> : <Badge tone="ok">valid</Badge>} <Badge>{methodLabel(r.generation_method)}</Badge></td>
                <td className="row-actions">
                  <a href={href(`/strategies/${r.strategy_id}`)}>Open</a>
                  {!r.archived && <>
                    <a href={href(`/builder/${r.strategy_id}`)}>Edit</a>
                    <a href={href(`/builder?duplicate=${r.strategy_id}`)}>Duplicate</a>
                    <a href={href(`/strategies/${r.strategy_id}?tab=overview`)}>Explain</a>
                    <a href={href(`/strategies/${r.strategy_id}?tab=variations`)}>Variations</a>
                  </>}
                  <a href={href(`/strategies/${r.strategy_id}?tab=lineage`)}>Lineage</a>
                  <button className="linklike danger" onClick={() => setConfirm(r)} data-testid={`archive-${r.strategy_id}`}>{r.archived ? "Restore" : "Archive"}</button>
                </td>
              </tr>))}
            </tbody>
          </table>
        </TableWrap>
      )}
      {all.length > pageSize && <Pager page={page} pages={pages} total={all.length} pageSize={pageSize} onPage={setPage}
        onPageSize={(n) => { setPageSize(n); setPage(1); }} />}
      <Confirm open={!!confirm} busy={busy} danger={!confirm?.archived}
        title={confirm?.archived ? `Restore ${strategyLabel(confirm.name)}?` : `Archive ${strategyLabel(confirm?.name)}?`}
        confirmLabel={confirm?.archived ? "Restore" : "Archive"} onCancel={() => setConfirm(null)} onConfirm={() => confirm && toggleArchive(confirm)}>
        {confirm?.archived ? "The strategy returns to the active library."
          : "Archiving removes the strategy from the active library. Nothing is deleted: its definition and lineage stay on disk, lineage that references it keeps working, and it can be restored at any time."}
      </Confirm>
    </div>
  );
}

// =========================================================================== strategy detail
type DetailTab = "research" | "overview" | "backtest" | "variations" | "validate" | "lineage";

export function StrategyPage() {
  const route = useRoute();
  const id = route.parts[1];
  const qTab = route.query.get("tab") as DetailTab | null;
  const [tab, setTab] = useState<DetailTab>(qTab || "research");
  useEffect(() => { if (qTab) setTab(qTab); }, [qTab]);    // links that change only ?tab= on the same strategy
  const { data: s, error } = useApi<StoredStrategy>(`/api/strategies/${id}`, [id]);
  if (error) return <ErrorPanel error={error} title="Could not load this strategy" />;
  if (!s) return <Loading label="Loading strategy…" />;
  const fam = (s.definition.family ?? {}) as { name?: string; hypothesis?: string };
  const first = s.lineage[0];
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1 data-testid="strategy-title" title={s.strategy_id}>{strategyLabel(s.name)}</h1>
          <div className="head-meta"><a className="chip" href={href(`/families/${s.family_id}`)} title={s.family_id}>{familyLabel(s.family_id, fam.name)}</a>
            <Badge>{methodLabel(first.generation_method)}</Badge>
            {s.archived && <Badge tone="warn">archived</Badge>}</div>
        </div>
        <div className="actions">
          {!s.archived && <Button onClick={() => go(`/builder/${s.strategy_id}`)} testId="edit">Edit</Button>}
          {!s.archived && <Button onClick={() => go(`/builder?duplicate=${s.strategy_id}`)}>Duplicate</Button>}
        </div>
      </header>
      <Tabs<DetailTab> active={tab} onChange={(t) => { setTab(t); window.history.replaceState(null, "", `#/strategies/${id}?tab=${t}`); }}
        tabs={[{ id: "research", label: "Research" }, { id: "overview", label: "Overview & Explain" }, { id: "backtest", label: "Backtest" },
          { id: "variations", label: "Generate Variations" }, { id: "validate", label: "Validate (out-of-sample, walk-forward, control)" },
          { id: "lineage", label: "Lineage" }]} />
      {tab === "research" && <ResearchHub s={s} batch={route.query.get("batch")} onTab={(t) => { setTab(t); window.history.replaceState(null, "", `#/strategies/${id}?tab=${t}`); }} />}
      {tab === "validate" && <ValidationPanel strategyId={s.strategy_id} initialDataset={route.query.get("dataset")} />}
      {tab === "overview" && <Overview s={s} fam={fam} />}
      {tab === "lineage" && <StrategyLineage id={s.strategy_id} />}
      {tab === "variations" && (s.archived ? <Banner tone="warn">Restore this strategy before generating variations from it.</Banner>
        : <VariationBuilder baseId={s.strategy_id} base={s.definition as unknown as StrategyDoc} />)}
      {tab === "backtest" && <BacktestPanel strategy={s.strategy_id} />}
    </div>
  );
}

function ResearchHub({ s, batch, onTab }: { s: StoredStrategy; batch: string | null; onTab: (t: DetailTab) => void }) {
  const { data: sr, error, reload } = useApi<StrategyResearch>(`/api/strategies/${s.strategy_id}/research`, [s.strategy_id]);
  if (error) return <ErrorPanel error={error} />;
  if (!sr) return <Loading label="Loading research state…" />;
  const hasRun = sr.runs.length > 0;
  const steps: [string, string, ReactNode][] = [
    ["1", "Edit or duplicate", <>{!s.archived && <Button small onClick={() => go(`/builder/${s.strategy_id}`)}>Edit</Button>}
      {!s.archived && <Button small onClick={() => go(`/builder?duplicate=${s.strategy_id}`)} testId="lab-duplicate">Duplicate</Button>}
      <span className="muted small">Saving a changed rule creates a new version; this one never changes.</span></>],
    ["2", "Backtest on a dataset", <Button small onClick={() => onTab("backtest")} testId="lab-goto-backtest">Backtest</Button>],
    ["3", "Generate controlled variations", <Button small onClick={() => onTab("variations")} disabled={s.archived}>Generate variations</Button>],
    ["4", "Run a batch on datasets", <span className="muted small">below</span>],
    ["5", "Compare results", <Button small onClick={() => go(`/compare?source=lineage&id=${s.strategy_id}`)} disabled={!hasRun} testId="lab-goto-compare">Compare this lineage</Button>],
    ["6", "Validate (out-of-sample, walk-forward, random control)", <Button small onClick={() => onTab("validate")} testId="lab-goto-validate">Validate</Button>],
    ["7", "Prop simulation on a stored run", <Button small onClick={() => go("/prop")} disabled={!hasRun}>Prop simulation</Button>],
  ];
  return (
    <div className="grid-cards" data-testid="lab-hub">
      <ProvenanceCard sr={sr} />
      <Card title="Research workflow">
        <table className="steps"><tbody>{steps.map(([n, label, action]) => (
          <tr key={n}><td className="muted">{n}</td><td>{label}</td><td className="inline">{action}</td></tr>))}</tbody></table>
        <p className="muted small">Every step calls the existing research engine; the stored strategy definition is the single source of truth.</p>
      </Card>
      <Card title={`Stored runs of this version (${sr.runs.length})`} className="wide" actions={<Button small onClick={reload}>Refresh</Button>}>
        <RunsTable runs={sr.runs} />
      </Card>
      <div className="wide"><BatchResearch strategyId={s.strategy_id} batches={sr.variation_batches_from_this_strategy} preselect={batch} /></div>
    </div>
  );
}

function Overview({ s, fam }: { s: StoredStrategy; fam: { name?: string; hypothesis?: string } }) {
  const { data: ex, error } = useApi<ExplainResult>(`/api/strategies/${s.strategy_id}/explain`, [s.strategy_id]);
  return (
    <div className="grid-cards">
      <Card title="Identity">
        <KeyValues rows={[["Name", strategyLabel(s.name)], ["Timeframe", facetLabel("timeframe", s.definition.timeframe)],
          ["Family", familyLabel(s.family_id, fam.name)], ["Hypothesis", fam.hypothesis || <span className="muted">not stated</span>]]} />
        <TechDetails rows={[["Strategy ID", <Mono>{s.strategy_id}</Mono>], ["Logic hash", <Mono>{s.logic_hash}</Mono>],
          ["Definition hash", <Mono>{s.definition_hash}</Mono>], ["Family ID", <Mono>{s.family_id}</Mono>]]} />
      </Card>
      <Card title="Rules in plain English" className="wide">
        <ErrorPanel error={error} />
        {ex ? <><RulesTable definition={s.definition as unknown as Record<string, never>} />
          <TechDetails summary="Explanation from the compiler (technical)"><pre className="code explain" data-testid="strategy-explain">{ex.explain}</pre></TechDetails></>
          : !error && <Loading label="Compiling…" />}
      </Card>
      <Card title="Stored canonical definition" className="wide">
        <TechDetails summary="Strategy definition (technical)"><pre className="code">{JSON.stringify(s.definition, null, 2)}</pre></TechDetails>
      </Card>
    </div>
  );
}

function StrategyLineage({ id }: { id: string }) {
  const { data, error } = useApi<LineageResponse>(`/api/strategies/${id}/lineage`, [id]);
  if (error) return <ErrorPanel error={error} />;
  if (!data) return <Loading label="Loading lineage…" />;
  const chain: TreeNode[] = [...data.ancestry].reverse().map((a) => ({
    strategy_id: a.strategy_id, name: null, generation_method: a.generation_method,
    parents: a.parent_strategy_id ? [a.parent_strategy_id] : [], changes: a.changes }));
  const kids: TreeNode[] = data.children.map((c) => ({ strategy_id: c, name: null, generation_method: "child", parents: [id], changes: [] }));
  const nodes = [...chain, ...kids];
  const label = (sid: string) => nodeLabel(nodes.find((x) => x.strategy_id === sid), sid, id);
  return (
    <div className="grid-cards" data-testid="lineage-view">
      <Card title="Ancestry and children" className="wide">
        <LineageTree nodes={nodes} focus={id} />
        <p className="muted small">From stored lineage records (not reconstructed from names). The full family tree is on the family page.</p>
      </Card>
      <Card title="Lineage records of this version" className="wide">
        <TableWrap><table>
          <thead><tr><th>Method</th><th>Parent</th><th>Changes</th><th>Batch</th><th>Time</th></tr></thead>
          <tbody>{data.records.map((r, i) => (
            <tr key={i}><td>{methodLabel(r.generation_method)}</td>
              <td>{r.parent_strategy_id ? <a href={href(`/strategies/${r.parent_strategy_id}`)} title={r.parent_strategy_id}>{label(r.parent_strategy_id)}</a> : "—"}</td>
              <td className="small">{changesText(r.changes) || "—"}</td>
              <td>{r.generation_batch_id ? <a href={href(`/variations/${r.generation_batch_id}`)} title={r.generation_batch_id}>Open batch</a> : "—"}</td>
              <td className="small">{shortTime(r.generation_timestamp)}</td></tr>))}
          </tbody>
        </table></TableWrap>
        <p className="muted small">Children: {data.children.length ? fmt(data.children.length) : "none"}.</p>
        <TechDetails rows={[["Strategy ID", <Mono>{id}</Mono>],
          ...data.records.flatMap((r, i) => [[`Record ${i + 1}: parent strategy ID`, r.parent_strategy_id ? <Mono>{r.parent_strategy_id}</Mono> : null],
            [`Record ${i + 1}: batch ID`, r.generation_batch_id ? <Mono>{r.generation_batch_id}</Mono> : null]] as [string, ReactNode][])]} />
      </Card>
      <Card title="Accessible table" className="wide"><LineageTable nodes={nodes} focus={id} /></Card>
    </div>
  );
}
