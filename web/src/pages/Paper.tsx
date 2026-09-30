import { href } from "../app/router";
import { Banner, Card } from "../components/ui";

/** Paper trading is a future phase: this page only states that, so the navigation shows the whole product honestly. */
export function PaperPage() {
  return (
    <div className="page" data-testid="paper-page">
      <header className="page-head"><div><div className="eyebrow">Simulation</div><h1>Paper trading</h1></div></header>
      <Banner tone="info"><b>Not implemented.</b> Paper trading (a separate paper-execution adapter, signal generation, simulated execution and a
        journal kept apart from backtests) is a planned phase. Nothing here trades, and EdgeLab has no broker connection.</Banner>
      <Card title="Where candidates stand today">
        <p>Candidates move through the research pipeline up to the protocol holdout result. The <a href={href("/pipeline")}>Candidate pipeline</a> shows
          “Paper evaluation” and “Human review” as not implemented for every strategy.</p>
      </Card>
    </div>
  );
}
