# CLAUDE.md: Munyun Lab project context

Munyun Lab (formerly EdgeLab; package `edgelab/`, program `EdgeLab.exe`, data folders `EdgeLab` unchanged on purpose) is an AI-assisted trading research and backtesting platform. Its job
is to find out whether a repeatable statistical edge **actually exists**, and to **disprove**
strategies as aggressively as it discovers them. The final product is a unified web GUI.

**Two sources, kept separate:**
- **Specification / roadmap (below):** what the project is *intended* to become.
- **This repository:** what is *actually* implemented. It is authoritative for current state.
- A phase described in the roadmap is not implemented until the code and tests exist. Never claim
  otherwise, and never invent requirements that are in neither source.

## Current state

**Phases 1, 2, 3, 3.5 and 4 COMPLETE.** Since then, as separate user-requested tasks: Phase 5 analytics
(`analytics/research.py`), fixed-strategy OOS/walk-forward/Monte Carlo (`research/validation.py`), the
matched random-entry control (`research/controls.py`), and the prop-account simulation layer
(`edgelab/prop/`, requested as "Phase 6"; roadmap row 7, evaluation rules only; see PROP_SIMULATION.md,
ADR-49), and desktop packaging hardening ("Phase 7": `edgelab/runtime.py`, `edgelab/desktop.py`,
`packaging/`, `build_windows.ps1`; ADR-50, DESKTOP_PACKAGING.md; `EdgeLab.exe` must be built on Windows), and the
Strategy Lab ("Phase 8": `research/lab.py`, Research/Validate tabs, Compare page, AI Proposals gate page; ADR-51).
Desktop: native window (ADR-52) and research-workspace selection (`runtime.resolve_workspace`, `workspace_host.py`,
Settings → Research Workspace; ADR-53).
"Phase 9" (as requested): Dukascopy primary source (profile `dukascopy_utc_csv`, provisional instrument `NQ_DUKASCOPY`
refused for research until its identity is stated, own unconfigured costs, provisional calendar, gap classification
`data/quality.py`), workspace Preferred Research Dataset (`data/preferences.py`), and AI Discovery (`edgelab/ai/`: blind
context, providers incl. deterministic mock, strict 8-stage gate, human review, lineage; ADR-54). The real Dukascopy CSV
has NOT been imported in this repository. `NQ_DUKASCOPY` = Dukascopy USATECH.IDX/USD (feed E_NQ-100), BID, index CFD research
proxy (not CME NQ), `identity_status: user_specified`, `calendar_status: regular_hours_verified` (regular hours evidenced; holidays/early
closes explicitly unverified and shown as a caveat). Canonical Dukascopy research execution is directional BID/ASK (ADR-55):
cost scenario `dukascopy_directional_cost_assumption_v1`, `spread_source: quotes` (buys on ASK, sells on BID, spread in
the fill prices, no separate spread cost; commission/slippage are assumptions); only `has_ask_ohlc` datasets are eligible
(`ASK_OHLC_REQUIRED`). Real ASK-OHLC datasets `NQ_DUKASCOPY_BIDASK_OHLC_2021_2026_*` live in the user's workspace; the frozen
BID/BIDASK datasets and legacy single-series runs are kept as evidence (read-only check: `scripts/dukascopy_inspect.py`).
Calendar `DUKASCOPY_USATECH_OBSERVED` (NY 18:00->16:15) comes from the real-file inspection; holidays unresolved.
Version 0.2.0 (ADR-58/59): research-terminal UI (dark design system, Home, Research dashboard, Explorer + drawer,
Controls, Candidate pipeline, Settings & About) over READ-ONLY read models `research/overview.py` (holdout-evaluation
runs are always labelled Holdout, never OOS), and a Windows updater `edgelab/updater/` (GitHub Releases manifest,
SHA-256-verified staging, helper-process swap with rollback; `packaging/release.py` prepares but never publishes).
`edgelab.__version__` is the single version (web/package.json must match; the build refuses otherwise).
Strategy factory (ADR-60, generation only): `strategy/factory_space.py` (30 families, frozen allocation = 10,000),
`strategy/factory.py` (sampling, 12-stage validation, manifest, `factory` CLI, read-only `/api/factory/*`),
`strategy/daytrading.py` (central same-NY-trading-date policy); no trials run. ADR-61: real trailing/breakeven stops
(`engine/fills.py::simulate_exit_trailing`, DSL `exit.trailing`), ICT family with eight setups (order block, breaker,
opening-range sweep added), `features/library/smc.py`. ADR-62 capability audit (`strategy/capabilities.py`, FACTORY_CAPABILITIES.md):
real equity-based risk sizing, no-progress exit, per-strategy trade cap, exit-based re-entry; volume-weighted features
refused on provider-defined volume (Dukascopy); VWAP/event filters excluded. ADR-63: strategies declare `sizing.contract: MNQ`;
the ONLY MNQ spec is configs/instruments.yaml; whole-contract floor-only sizing (`engine/sizing.py::contracts_for_risk`, shared by
`risk` and `equity_risk`) on the Dukascopy index-CFD proxy series (dataset identity is never MNQ); factory `/4`, space `/4`.
ADR-64/65: default research account 50,000 USD (run parameter, not identity); every backtest automatically audits its trades
through the prop lifecycle (`edgelab/prop/lifecycle.py`, schema-2 configurable profiles in `configs/prop/profiles/`, generated by
`scripts/prop_default_profiles.py`): LucidFlex 50K, Tradeify Growth 50K, Select 50K -> Flex / Daily. Rule values are the user's
canonical defaults. ADR-66: schema-3 profiles (v3), every rule `{value, status: VERIFIED / ASSUMED_DEFAULT / CUSTOM, basis}`, no nulls;
results are PASS / FAIL / INCOMPATIBLE / NOT_APPLICABLE "UNDER DEFAULT ASSUMED RULES" with rule-basis counts. Factory /5, space /5.
ADR-67: protocol version 3 (10,000 unique trials, Bonferroni family = declared budget, 5,000,000 bootstrap replicates, holdout looks
independent); an unused protocol is superseded (`create_protocol(supersedes=)`, `scripts/protocol_supersede.py`), never edited.
Prop profiles v4 (metadata-only correction). Manifest FM_3B0B01CFC81AB15E (10,000) verified reproducible.
ADR-68: frozen-manifest campaign launcher `research/campaign.py` (`research campaign-freeze|check|run|status`): one strategy = one
cell = one trial through the protocol-gated search, workers 1, discovery only, resumable; preflight is read-only.
ADR-69: desktop "Research runs" page (`web/src/pages/Runs.tsx`, `/api/campaigns*`) launches/monitors frozen campaigns by family scope
through the same runner (`campaign.run_scope` -> `run_search(include=)`); run history in `<data>/campaigns/<CMP>/runs/`.
ADR-70: research browser tree (frozen strategy-id scopes), `strategy/presentation.py` (display names / explanations, identity untouched),
per-cell timing in `search_cells` + data-based ETA (`campaign.estimate_remaining`, "estimating" below 3 observations); the governance
layer stays strict (windowless / holdout-reaching requests refused); ad-hoc UI paths request the protocol's `discovery_period`.
ADR-72: branch-channel updater (release schema 2: channel/build number/commit; pre-releases `build-<branch>-<n>` built and
smoke-tested by `.github/workflows/windows-build.yml`; banner "Restart and update"; repo must be public). ADR-73: seven tabs
(Home, Strategies, Run backtest, Backtest results, Prop firm simulator, Paper trading, Settings; old routes kept), read-only
`research/results_view.py` (field, survivors = net > 0 AND recorded trades pass an evaluation with a payout, breakdowns,
strategy/control panels), `prop/bootstrap.py` (seeded day-block bootstrap through the unchanged lifecycle; intraday trailing
refused), controls stored as control records (never runs/trials), risk per trade ($) display preference outside the config
hash, plain-English UI (`web/src/app/labels.ts`; ids only under "Technical details"), local time for app events, no ETA.
ADR-74: shown as "Munyun Lab" (internal names unchanged); display preferences in workspace prefs `ui` (favorites, prop criteria
account default LucidFlex 50K driving Pass eval / Pass payout / survivor, Show IDs, Show read-only information); named research
runs (`runs/names.json`) pickable on Backtest results (`campaign_run` = that run's scope); simplified Prop firm simulator;
`reset_workspace("DELETE")` keeps price data only (logged); no eyebrow/subtitle, no global search, skeleton loaders.
ADR-75: read-view speed caches (library folder-stamp fingerprint, store `db_token`, `manifest_rows_view`, memoized campaign
detail/tree, explorer rows, per-user (outside the workspace) facets cache, start-up warm-up); display/short strategy names without hash; explorer fits.
ADR-76: Windows installer `packaging/installer.iss` + `build_installer.py` (per-user into `%LOCALAPPDATA%\Programs\EdgeLab`, the updater's folder;
optional Start-menu/desktop shortcuts; CI fixed release `installer-main`), icon `packaging/icon.py` -> `munyun.ico`; single backtests as background
jobs (`Services.start_backtest_job`, `/api/backtests/jobs`, lock free while computing).
ADR-77: speed without result changes: `Services._cell_dataset` (validated once per process), causality truncation feature cache
(`strategy_api.truncation_cache`, content-addressed, memory-bounded), multi-core research runs (`run_scope(processes=)`, Settings
`ui.research_processes`, default all cores but one; parent writes in plan order), trades/metrics/ledger indexes, incremental read views, browser view cache.
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
  the existing engine interfaces. Unsupported semantics (partial exits, target trailing,
  pyramiding, top-level cooldown_after_exit) are **refused by name, never approximated**. The compiler
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
| 3.5 Builder & Web UI | Flask API, React/TS UI, builder, library, families, variation batches, datasets/import, single backtests, results, Research/AI placeholders (Research replaced in Phase 4) | done |
| 4 Batch Research & Search | batch evaluation of Mode A variations and Mode B proposals through the same pipeline; strategy x dataset research; search config (`search.yaml`); search runner; batch result storage; ranking/selection; honest trial counting; parallelism where appropriate; throughput benchmark; strategy library indexing; research service/API; Research web page; async jobs with progress; cancellation/job lifecycle | done |
| **5 Analytics** | rich performance analytics; session/timeframe/weekday/month/year breakdowns; trade distributions; drawdown analysis; cost sensitivity; breakeven cost; stability; visualizations | **next** |
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
- Web API (`edgelab/web/app.py`): thin routes over `Services`; `Services.lock` is the one service
  lock (shared with the job manager; never held during a backtest).
- Phase 4 research (`edgelab/research/{search,batch,ranking,jobs}.py`): `plan_search(spec, svc)`
  (`search_hash` = canonical sources/datasets/period/seed + config_hash; `cell_id` includes
  dataset_id), `run_search(svc, spec, workers=None, lock=, cancel=)` -> `search_batches` /
  `search_cells` (SQLite) referencing normal `runs`; every cell goes through `Services._run_cell` /
  `_record_cell`; trials = eligible cells evaluated (current plan); `rank_cells` / `rank_search`
  are in-sample only (never win rate, `validated: false`); a shortlist is a tag, never a status
  promotion (Phase 6 owns promotion). Services: `validate_search`, `plan_search`, `run_search`,
  `list_searches`, `get_search`, `rank_search`, `select_shortlist`, `start_search_job`,
  `job_status`, `cancel_job`; HTTP under `/api/research/*`; CLI `research ...`.

## Known limitations (repository, as of Phase 4)

- Research storage is SQLite-only; DuckDB refuses search operations, and with
  `storage.backend: auto` installing DuckDB makes research unavailable. The DuckDB backend is
  otherwise untested.
- One process per data root: `next_run_id` is MAX+1 and restart reconciliation marks any `running`
  search `interrupted`, so concurrent CLI and web research on one root is not coordinated.
- Trials: per search (`n_trials`) AND, under an ACTIVE research protocol (ADR-56), program-wide in the
  protocol trial ledger (dedup by logic/evaluated bars/config). Without a protocol only per-search counts
  exist. The seed still changes the search identity without changing deterministic DSL results.
- Background jobs are sequential (`workers: 1`) and process-local (job ids do not survive a
  restart); worker processes copy the datasets; the service lock covers each cell's dataset load.
- Random-entry null controls exist only in `scripts/phase1_demo.py`, not as a service.
- No real market data imported; all results so far are synthetic.
- No browser file upload (imports come from `web.import_dirs`); no auth (local, loopback only).
- Known stale docs: a reference to a nonexistent `tests/test_reproducibility.py` in
  `research/runs.py`, ADR-10's `FAMILY_<hash>` id scheme (superseded for DSL strategies by
  ADR-23), and `reports/phase1_demo_output.txt` (recorded in an older environment).
- Full list: `ARCHITECTURE.md`, "Known limitations (Phase 4)".

## Where things are

- Docs: `README.md` (status, quickstart), `ARCHITECTURE.md` (layers, module maps, ADR-1..31, known
  limitations), `CHANGELOG.md` (per-phase IMPLEMENTED/TESTED/NOT IMPLEMENTED/REQUIRES REAL DATA),
  `CONFIG.md`, `DATA_IMPORT.md`, `FEATURES.md` (generated; drift-tested), `STRATEGY_DSL.md`,
  `STRATEGY_GENERATION.md`, `WEB_UI.md`.
- Code: `edgelab/{core,data,engine,features,strategy,research,analytics,web}` (Phase 4 research:
  `research/{search,batch,ranking,jobs}.py`, Research page `web/src/pages/ResearchEngine.tsx`); `services.py`,
  `cli.py`. Empty placeholders for later phases: `prop/`, `reports/`, `journal/`,
  `notifications/`, `execution/`.
- Config: `configs/*.yaml` (research config is hashed; `web.yaml` and the example search spec
  `search.example.yaml` are deliberately outside the hash).
- Frontend sources in `web/` (React + TS, esbuild); the built bundle is committed in
  `edgelab/web/static/`, and a test detects a stale bundle, so rebuild after frontend changes.

## Commands

```bash
python -m unittest discover -s tests -t .        # full suite (~60 s); run only when asked
python scripts/run_tests.py                      # full suite + dashboard test status
python -m edgelab.cli --help                     # data, features, strategy, research commands
python -m edgelab.cli research plan|run|rank ... # Phase 4 batch search (see README)
python -m edgelab.web                            # web app at http://127.0.0.1:8765
python -m edgelab.web --demo                     # separate synthetic demo workspace
python -m edgelab.desktop [--data-root DIR] [--ui window|browser|none]  # desktop launcher from source (own window by default)
python packaging/build.py [--smoke]              # packaged folder build (Windows: build_windows.ps1)
python scripts/phase1_demo.py                    # end-to-end synthetic demo (must stay identical)
python scripts/benchmark_search.py               # Phase 4 search throughput (informational)
```

## Working rules for Claude Code

- Git: remote `origin` = `https://github.com/ShiningRedstone/AI-Backtesting.git`, branch `main`.
  Do not commit or push unless explicitly asked.
- The user's local machine is Windows, with the clone at `C:\Users\Ethan\Documents\AI-Backtesting`. Python there is
  `.\.venv\Scripts\python.exe`. PowerShell commands given to the user start with
  `Set-Location 'C:\Users\Ethan\Documents\AI-Backtesting'`.
- Stay within the requested phase and task; no unrelated refactors or doc fixes.
- When a phase is complete, update `README.md` status, `CHANGELOG.md`, `ARCHITECTURE.md` (module
  map, ADRs, known limitations) and this file's "Current state".
