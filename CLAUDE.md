# CLAUDE.md: EdgeLab project context

EdgeLab (package `edgelab/`) is an AI-assisted trading research and backtesting platform. Its job
is to find out whether a repeatable statistical edge **actually exists**, and to **disprove**
strategies as aggressively as it discovers them. The final product is a unified web GUI.

**Two sources, kept separate:**
- **Specification / roadmap (below):** what the project is *intended* to become.
- **This repository:** what is *actually* implemented. It is authoritative for current state.
- A phase described in the roadmap is not implemented until the code and tests exist. Never claim
  otherwise, and never invent requirements that are in neither source.

## Current state

**Phase 3.5 COMPLETE. Next: Phase 4 (Batch Research & Search).**
Before starting any phase, inspect the repository to establish exactly what already exists and what
remains. Do not rely on this file alone.

## Non-negotiable research principles

1. Never fabricate market data, broker costs, fills, execution or results.
2. Never silently guess critical data assumptions (timezone, timestamp convention, price basis,
   volume meaning, spread units). Refuse or ask.
3. Data is validated before it reaches the backtester (`ValidatedDataset` only).
4. Lookahead is prevented structurally *and* checked empirically (truncation tests).
5. Costs and execution assumptions are realistic, explicit and disclosed on every run.
6. Evidence = profit factor, expectancy, drawdown, sample size, stability, robustness, OOS
   behaviour. Never optimize for win rate alone.
7. Negative results and failed strategies are valuable outputs, so record them.
8. Random/null controls show whether an apparent edge could arise by chance.
9. Research is reproducible (dataset hash, config hash, code version, seed, trades hash).
10. Strategy / data / config lineage is preserved.
11. Strategy identity and trial counting must prevent dishonest strategy-count inflation.
12. Research, paper trading, human discretion and live execution stay separate.
13. Live trading is isolated and **disabled by default**.
14. Every result stays tied to the assumptions that produced it ("historical result under stated
    assumptions", never a forecast). Synthetic results are labelled everywhere.
15. **The numerical engine is authoritative.** AI may propose hypotheses, variations and
    explanations, but never declares a strategy profitable and never bypasses the engine.

## Architecture rules

- **Data:** adapter-based providers; explicit timezone and timestamp conventions; DST-correct
  sessions; content-addressed, immutable datasets; separate datasets are **never merged or
  averaged** (futures and each CFD feed stay separate). CFD cost profiles ship `unconfigured` and
  the engine **refuses** to run until the user enters broker numbers.
- **Instruments:** NQ primary; ES; NQ CFD fallback; more through adapters. Adding a provider or
  instrument must not require rewriting the research engine.
- **Backtesting:** one engine. It receives validated data only, uses documented fill rules, has
  explicit costs (in R) and configurable sizing, runs a causality check on every run, and outputs
  enough for downstream analytics and reproducibility.
- **Strategies are data** (versioned DSL, YAML/JSON), validated, compiled deterministically into
  the existing engine interfaces. Unsupported semantics (trailing stops, breakeven, partial exits,
  pyramiding, exit-based cooldown) are **refused by name, never approximated**. The compiler
  contains no indicator math; every indicator is a registered causal feature.
- **Identity:** `logic_hash` (resolved logic -> `STR_...` strategy id) vs `definition_hash` (whole
  document). Cosmetic changes must not create "new" strategies; real rule changes must.
- **Mode A** (controlled variations): from a user/core strategy; grid / one-at-a-time / seeded
  random; cap before generating; dedupe equivalent logic; parent->child lineage; honest counts.
- **Mode B** (AI proposals): proposals are data, enter through the ingestion gate (strict schema,
  claim-language rejection, same validator and compiler), then use the same numerical pipeline.
- **UI:** the web app calls `edgelab/services.py` contracts and never duplicates research logic.
  The engine stays independent of UI concerns. HTTP accepts data-only inputs; bound to loopback.
- **Development:** phase by phase; do not implement future phases early; preserve contracts
  between phases; small composable services; avoid rewrites. If a change to an earlier phase's
  module is needed, keep it minimal and backward-compatible, prove prior tests and the Phase 1
  demo are unchanged, and record an ADR in `ARCHITECTURE.md` plus a `CHANGELOG.md` entry.
- **Tests verify research guarantees** (known answers, causality, reproducibility, refusals), not
  superficial coverage. Tests use stdlib `unittest`; pytest also collects them.

## Roadmap (intended; status from the repository)

| Phase | Scope | Status |
|---|---|---|
| 1 Research Foundation | config, logging, calendars/resampling, validation gate, `ValidatedDataset`, content identity, backtester, lookahead protection, honest fills, costs, risk sizing, metrics with CIs/sample labels, run registry, synthetic/null controls, reproducibility | done |
| 2 Data, CFD & Features | generic import pipeline, explicit tz/timestamp conventions, manifests/hashes, immutable store, derived TFs, feature registry, causal features, session/DST engine, causal MTF, persistent feature cache, explicit CFD costs, dataset comparison without merging | done |
| 3 Strategy DSL & Variations | versioned DSL, validation, causality, deterministic compiler, identity/hashing, lineage, Mode A (grid/OAT/seeded random, cap-before-generate, dedupe), Mode B ingestion interface | done |
| 3.5 Builder & Web UI | Flask API, React/TS UI, builder, library, families, variation batches, datasets/import, single backtests, results, Research/AI placeholders | done |
| **4 Batch Research & Search** | batch evaluation of Mode A variations and Mode B proposals through the same pipeline; strategy x dataset research; search config (`search.yaml`); search runner; batch result storage; ranking/selection; honest trial counting; parallelism where appropriate; throughput benchmark; strategy library indexing; research service/API; Research web page; async jobs with progress; cancellation/job lifecycle | **next** |
| 5 Analytics | rich performance analytics; session/timeframe/weekday/month/year breakdowns; trade distributions; drawdown analysis; cost sensitivity; breakeven cost; stability; visualizations | planned |
| 6 Anti-Overfitting | OOS, walk-forward, Monte Carlo, random/null controls, multiple robustness tests, discovery vs validation separation, overfitting detection/reporting, significance/uncertainty | planned |
| 7 Prop-Firm Simulation | evaluation/funded/payout states, daily loss, drawdown, consistency rules, minimum trading days, multi-account, configurable rule profiles (not hardcoded to one firm) | planned |
| 8 Dashboard & Reporting | unified dashboard, strategy comparison, reports, lineage visualization, decision-oriented summaries, reproducible report generation | planned |
| 9 Paper Trading | paper execution adapter, signal generation, simulated execution, journal, separate from backtests | planned |
| 10 Human Discretion | signals with chart/context; BUY / LIMIT BUY / SELL / LIMIT SELL / SKIP; decision journal; outcome tracking; kept separate from automated results | planned |
| 11 Live Execution | isolated live adapter, explicit safety controls, disabled by default, broker adapters, no coupling that could turn research into live trading | planned |

Research must eventually support analysis across sessions, timeframes, weekdays, months, years,
events/regimes, instruments/datasets, strategy families and controlled variations.

## Contracts later phases must reuse

- `engine.backtester.run_backtest(ds: ValidatedDataset, strategy, costs, bt_cfg, sizing=, ltf=)
  -> BacktestResult` (trades, skipped, n_signals, assumptions, dataset, causality, `trades_hash`).
- DSL run path (see `Services.backtest_strategy`): `strategy.compiler.compile_strategy(defn,
  sessions, config_hash)` -> `.bind(features.strategy_api.FeatureContext(ds, sessions, cache))` ->
  `run_backtest(..., sizing=strat.sizing)`.
- Data: `Services.load_dataset` / `data.importer.load_validated` (re-validates, re-checks hashes).
  Costs: `engine.costs.cost_model_from_config(cfg, symbol, provider=)` (CFD refusal must remain).
- Metrics: `analytics.metrics.compute_metrics(trades, sample_thresholds=)`, `cost_sensitivity`,
  `breakeven_cost_multiplier`.
- Run registry: `research.runs.record_run(store, cfg, result, metrics, seed=, status=,
  parent_strategy_id=, mutation=)`. Status starts at `IN_SAMPLE`; only later OOS/walk-forward gates
  promote it. A backtest alone never labels anything "validated".
- `strategy.lineage.StrategyLibrary` (file-based instances, lineage, batch records),
  `Services.generate_variations` (Mode A), `Services.ingest_proposals` (Mode B).
- `research.compare.run_across_datasets`, `restrict_to_period`, `compare_feeds`.
- `edgelab/services.py`: strict-JSON contracts; the only interface the CLI and web API use.
- Web API (`edgelab/web/app.py`): thin routes over `Services`; one service lock serializes calls.

## Known limitations (repository, as of Phase 3.5)

- No batch/search execution, parallelism, `search.yaml`, batch result storage, ranking or
  selection. The Research API/UI is a placeholder.
- No async jobs, progress or cancellation; web requests are serialized by the service lock.
- Random-entry null controls exist only in `scripts/phase1_demo.py`, not as a service.
- Strategy library listing scans a directory (no index).
- DuckDB backend exists but is untested (SQLite is used).
- No real market data imported; all results so far are synthetic.
- No browser file upload (imports come from `web.import_dirs`); no auth (local, loopback only).
- Known stale docs: the `research_config_options` "strategies" placeholder (`services.py`), a
  reference to a nonexistent `tests/test_reproducibility.py` in `research/runs.py`, and ADR-10's
  `FAMILY_<hash>` id scheme (superseded for DSL strategies by ADR-23).

## Where things are

- Docs: `README.md` (status, quickstart), `ARCHITECTURE.md` (layers, module maps, ADR-1..31, known
  limitations), `CHANGELOG.md` (per-phase IMPLEMENTED/TESTED/NOT IMPLEMENTED/REQUIRES REAL DATA),
  `CONFIG.md`, `DATA_IMPORT.md`, `FEATURES.md` (generated; drift-tested), `STRATEGY_DSL.md`,
  `STRATEGY_GENERATION.md`, `WEB_UI.md`.
- Code: `edgelab/{core,data,engine,features,strategy,research,analytics,web}`; `services.py`,
  `cli.py`. Empty placeholders for later phases: `prop/`, `reports/`, `journal/`,
  `notifications/`, `execution/`.
- Config: `configs/*.yaml` (research config is hashed; `web.yaml` is deliberately outside the hash).
- Frontend sources in `web/` (React + TS, esbuild); the built bundle is committed in
  `edgelab/web/static/`, and a test detects a stale bundle, so rebuild after frontend changes.

## Commands

```bash
python -m unittest discover -s tests -t .        # full suite (~60 s); run only when asked
python scripts/run_tests.py                      # full suite + dashboard test status
python -m edgelab.cli --help                     # data, features, strategy commands
python -m edgelab.web                            # web app at http://127.0.0.1:8765
python -m edgelab.web --demo                     # separate synthetic demo workspace
python scripts/phase1_demo.py                    # end-to-end synthetic demo (must stay identical)
```

## Working rules for Claude Code

- Git: remote `origin` = `https://github.com/ShiningRedstone/AI-Backtesting.git`, branch `main`.
  Do not commit or push unless explicitly asked.
- Stay within the requested phase and task; no unrelated refactors or doc fixes.
- When a phase is complete, update `README.md` status, `CHANGELOG.md`, `ARCHITECTURE.md` (module
  map, ADRs, known limitations) and this file's "Current state".
