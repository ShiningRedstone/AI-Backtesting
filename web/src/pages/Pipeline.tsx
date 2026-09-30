import { useState } from "react";
import type { PipelineBoard } from "../api/types";
import { href } from "../app/router";
import { useApi } from "../app/context";
import { PipelineStrip } from "../components/research";
import { Badge, Card, Empty, ErrorPanel, Loading, Mono, TableWrap } from "../components/ui";

const TONE: Record<string, "ok" | "warn" | "info" | "neutral"> = { holdout_criteria_met: "ok", holdout_criteria_not_met: "warn",
  holdout_granted: "info", shortlisted: "info", oos_tested: "info" };

export function PipelinePage() {
  const { data, error } = useApi<PipelineBoard>("/api/pipeline");
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="page" data-testid="pipeline-page">
      <header className="page-head"><div><div className="eyebrow">Research</div><h1>Candidate pipeline</h1>
        <div className="subtitle small">Hypothesis → proposal validation → numerical testing → controls → OOS → shortlist → holdout authorization →
          holdout result → paper evaluation → human review. Every state is derived by the backend from stored facts; the UI infers nothing.</div></div></header>
      {error ? <ErrorPanel error={error} /> : !data ? <Loading label="Loading the pipeline…" /> : <>
        <div className="pipeline" data-testid="pipeline-board">
          {data.stages.map((s, i) => (
            <div key={s.id} className={`stage ${s.id === "paper_evaluation" || s.id === "human_review" ? "not_available" : s.counts.done ? "done" : ""}`}>
              <div className="stage-name">{i + 1}. {s.label}</div>
              <div className="stage-counts"><span className="pos" title="done">{s.counts.done}✓</span>
                {s.counts.failed + s.counts.refused > 0 && <span className="neg" title="failed / refused">{s.counts.failed + s.counts.refused}✕</span>}
                <span className="faint" title="pending">{s.counts.pending}·</span></div>
            </div>))}
        </div>
        <p className="small muted">{data.n_strategies} strategies in the library. {data.note}</p>
        <Card className="flush" title="Candidates beyond in-sample testing" testId="pipeline-candidates">
          {!data.candidates.length ? <div className="card-body"><Empty>No strategy has an out-of-sample run, a shortlist tag or a holdout
            evaluation yet.</Empty></div> : <TableWrap><table className="dense">
            <thead><tr><th>Strategy</th><th>Family</th><th>State</th><th>Stages</th></tr></thead>
            <tbody>{data.candidates.map((c) => [
              <tr key={c.strategy_id} className="clickable" onClick={() => setOpen(open === c.strategy_id ? null : c.strategy_id)}>
                <td><a href={href(`/explorer?open=${c.strategy_id}`)} onClick={(e: { stopPropagation: () => void }) => e.stopPropagation()}>{c.name ?? c.strategy_id}</a>
                  <div className="small muted mono">{c.strategy_id}</div></td>
                <td className="small"><Mono>{c.family_id}</Mono></td>
                <td><Badge tone={TONE[c.state] ?? "neutral"}>{c.state_label}</Badge></td>
                <td className="small">{c.stages.filter((s) => s.state === "done").length} of {c.stages.length} done {open === c.strategy_id ? "▲" : "▼"}</td></tr>,
              open === c.strategy_id && <tr key={c.strategy_id + "-d"}><td colSpan={4}><PipelineStrip stages={c.stages} testId={`pipe-${c.strategy_id}`} /></td></tr>])}
            </tbody></table></TableWrap>}
        </Card>
      </>}
    </div>
  );
}
