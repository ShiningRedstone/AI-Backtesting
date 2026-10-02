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
Version 0.2.0 introduced (ADR-58/59): research-terminal UI (dark design system, Home, Research dashboard, Explorer + drawer,
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
ADR-78: GET requests run in `Services.read_context()` on pooled `ReadOnlySQLiteStore` connections WITHOUT the service lock (`Services.store`
is a property; writer = `writer_store`); `db_token` = writer total_changes + file change counter (5 s reuse for pages during a job);
research run reads (preflight, progress, stop) via `campaign.read_outside_lock`; planned cells in one transaction.
ADR-79: Run backtest = Research runs | Single backtest (Experiments tab removed; `/research` -> `/runs`, job/id deep links kept); `results_view.session_group`
(market-hours groups, `session_group` breakdown); deep-rose decorative gradient; desktop relaunch waits for a closing instance (`_hand_off` ->
`HANDOFF_WAIT`, `_wait_for_previous`), runtime.json removed first on shutdown, splash `edgelab/desktop_splash.py` (`EdgeLab.exe --splash`).
Version 0.3.0 (after the V0.2 save point): version bump only, no behaviour change.
ADR-80: `core/fsutil.atomic_write_text` (unique temp + `replace_with_retry` on Windows sharing violations) for run records,
scopes and run names; a research run no longer dies with WinError 5 while a page reads its run record.
ADR-81: paper trading in simulated prop accounts (`edgelab/paper/`: `feed.py` daily Dukascopy download via dukascopy-python, in-memory
validated feed, never a research dataset; `engine.py` attempts/fees around the unchanged lifecycle; `store.py` JSON under `<data>/paper/`;
`manager.py` 30-min thread, launchers only); tab "Prop & paper" (Paper accounts, Start paper trading, Backtest prop check); fees in
`ui.prop_fees`; engine option `account.equity_from_ts` (absent = byte-identical backtests). Paper results are never runs or trials.
ADR-82: strategy panel shows an equity curve (`StepTimeChart`, time axis) and "Results by year" with expandable months
(`results_view.calendar_years`, by exit date in New York time, display only) instead of "Last 12 months of data".
ADR-83: paper feed checked against the research data (`paper/feed.py::source_check`: last 3 complete trading days inside the
1m NQ_DUKASCOPY ASK-OHLC research dataset re-downloaded and compared bar by bar; never stored in the feed; verdict in
`<data>/paper/feed/source_check.json`; mismatch pauses paper accounts until a match or "Continue anyway"); browser test `tests/test_paper_e2e.py`.
ADR-84: the strategy panel shows the protocol's locked holdout after a discovery run (`results_view.holdout_period`: shaded curve
band and "locked, not backtested" year rows; a holdout evaluation's own years and second curve line, labelled Holdout); read only.
ADR-85: Run backtest → Holdout backtest (`/holdout`, `research/holdout.py`: survivors ranked on 8 discovery criteria, drawdown and
negative months ×2; `HoldoutJob` in the one-at-a-time JobManager; auto-shortlist; `evaluate_holdout` per strategy) and Backtest
results → Holdout results (`/holdout-results` = explorer `scope=holdout`); holdout gate made timeframe-aware (`stage_of(tf_ns)`,
`Services._holdout_dataset` checks everything BEFORE a look is spent).
ADR-86: display round: amber survivors and size-capped grouping of overlapping strategy dots on the Overview field chart
(`ScatterGroup.cluster`, `clusterMarks`; a group click lists its strategies), axis titles outside the plot, light theme
(`ui.theme` dark|light|system, `:root[data-theme="light"]` tokens), Home "Backtested trades" (`facts.trades_total`), top
bar chips removed, Start paper trading default "Passed the holdout" (`paper_candidates(view=)`), HistData removed from the
app and the default configs (a workspace's own configs are untouched).
Before starting any phase, inspect the repository to establish exactly what already exists and what
remains. Do not rely on this file alone.

## The app today (version 0.3.0)

- **Version:** `edgelab.__version__` = **0.3.0** (web/package.json and package-lock.json must match; the frontend build
  records it in `edgelab/web/static/build-info.json`). Save points (never modify or delete them):
  - branch `backup/main-2026-10-01` = 0d0baeb
  - branch `V0.2` = 716da4d, the last 0.2.0 state
- **How the user runs it:** an installed Windows desktop app.
  - Installer: `MunyunLab-Setup.exe`. Permanent link for the latest main build:
    https://github.com/ShiningRedstone/AI-Backtesting/releases/download/installer-main/MunyunLab-Setup.exe
  - Per-user install into `%LOCALAPPDATA%\Programs\EdgeLab`; shortcuts start in the user's home folder. The
    installer is unsigned, so SmartScreen asks "More info → Run anyway".
  - It updates itself from GitHub builds of `main`: Settings → Updates → "Check for updates" → "Restart and update".
  - The window is pywebview / WebView2 (a native window, not a browser).
  - A relaunch while the old instance is closing waits for it. The packaged app shows a splash
    (`EdgeLab.exe --splash`).
- **CI:** `.github/workflows/windows-build.yml` runs on every push.
  1. Builds `EdgeLab.exe` (the build refuses if pywebview or dukascopy-python is missing).
  2. Runs the packaged smoke test (also checks the Dukascopy downloader is bundled), the splash self-close check and
     the packaged updater smoke (app started from its own folder).
  3. Builds the installer and smoke-tests it: silent install, smoke, silent uninstall.
  4. Publishes the pre-release `build-<branch>-<n>`; on `main` it also updates `installer-main`.
- **Data:** Dukascopy only (USATECH.IDX/USD, feed E_NQ-100, 1-minute BID + ASK; the index-CFD stand-in for NQ;
  strategies trade MNQ contracts). HistData support was removed (ADR-86).
  - A workspace keeps its OWN copy of `configs/`, made when the workspace was created; app updates never change it.
    The ACTIVE research protocol is bound to that config's hash, so never change a user's workspace configs
    (research and holdout tests would be refused with PROTOCOL_CONFIG_CHANGED).
- **Research flow the user follows:** discovery (Research runs; the protocol's discovery window only) → survivors
  (net > 0 AND the trades pass a prop evaluation with a payout under the Settings account) → Holdout backtest (the
  locked final period, protocol gate, 10 tests in total by default, once per strategy, "criteria met / not met")
  → Prop & paper (paper accounts on new days; default list = strategies that passed the holdout).
- **Paper trading (ADR-81/83):** daily forward Dukascopy days (downloaded by dukascopy-python at start, every 30 min,
  "Update now"); accounts start the next trading day; failed eval → new attempt (reset fee, else eval price);
  pass → activation fee, funded, every payout; funded loss / live point / payout limit → new eval; net = payouts
  (trader share) − fees. Fees per account type in Settings (nothing starts without an evaluation price). The feed is
  checked once per research dataset against the research data (last 3 complete days inside it, bar by bar);
  a mismatch pauses accounts until a match or "Continue anyway". Paper results are never runs or trials.
- **Tabs (UI, plain English):**
  - Home: system facts (strategies, stored runs, backtested trades, AI generations, datasets, version), latest results.
  - Strategies: Library, Families, Builder, Variations
  - Run backtest: **Research runs** (default), Single backtest, Holdout backtest (survivors only, ranked best → worst
    for prop trading on 8 discovery criteria with drawdown and negative months ×2). `/research` → `/runs`; job and
    result deep links still work.
  - Backtest results: Overview, Strategies explorer, Holdout results, All runs, Compare, Random controls, Candidate
    pipeline
  - Prop & paper: Paper accounts, Start paper trading (batch; Passed the holdout | Survivors | All tested), Backtest
    prop check (the old simulator)
  - Settings: theme (Dark / Light / Same as Windows), CPU cores for research runs, the pass-criteria prop account,
    prop account fees, Show IDs / read-only switches, updates, workspace, delete-all.
  - The top bar shows only the brand, the DEMO badge (demo workspace) and the version chip (Backend OK, protocol and
    workspace chips were removed at the user's request).
- **Design decisions the user made:**
  - Dark UI by default, plus a light theme (ADR-86). Decorative surfaces use a deep-rose → plum gradient
    (`--warm-gradient`); red is ONLY for losses, negative values and errors.
  - Field chart (Overview): strategies blue; survivors amber (`--c-survivor`); overlapping blue dots group into bigger
    circles (size capped, no number; survivors and random controls never group); clicking a group lists its
    strategies in the side panel, click one to open it. Scatter dots have no outlines; axis titles sit outside the plot.
  - "By session" is grouped by market hours (Asia, London, London–NY overlap, NY AM, NY PM, NY full day, Any time),
    with "Show all windows".
  - IDs are hidden unless "Show IDs" is on. Holdout runs are always labelled Holdout, never out-of-sample.
  - The user rejected GPU acceleration: pages were lock-bound, and GPU math could change result hashes.
- **Performance architecture:**
  - ADR-75: read caches.
  - ADR-76: background single backtests.
  - ADR-77: datasets validated once per process; the causality-feature cache; multi-core research runs, default all
    cores but one.
  - ADR-78: page GETs on read-only SQLite connections outside the service lock; page numbers may lag up to 5 s
    during a run.
  - None of these change any result: every cache is content-keyed and tested against the fresh path.
- **One research job at a time:** research runs and holdout backtests share the JobManager (a second start → 409).

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

## Known limitations (repository, current)

- Research storage is SQLite-only; DuckDB refuses search operations, and with
  `storage.backend: auto` installing DuckDB makes research unavailable. The DuckDB backend is
  otherwise untested. SQLite stays in rollback-journal mode (no WAL: read-only workspace tools open it `mode=ro`).
- One process per data root: `next_run_id` is MAX+1 and restart reconciliation marks any `running`
  search `interrupted`, so concurrent CLI and web research on one root is not coordinated.
- Trials: per search (`n_trials`) AND, under an ACTIVE research protocol (ADR-56), program-wide in the
  protocol trial ledger (dedup by logic/evaluated bars/config). Without a protocol only per-search counts
  exist. The seed still changes the search identity without changing deterministic DSL results.
- Background jobs are process-local (job ids do not survive a restart). Frozen-campaign research runs use several
  CPU cores (ADR-77; the parent writes in plan order; on cancel, cells already running finish and are recorded).
  The old "Research Engine" search jobs stay sequential. Worker processes copy the datasets.
- Real Dukascopy BID/ASK datasets live only in the user's own workspace (not in this repository); repository tests
  and demos use synthetic data, labelled everywhere.
- No browser file upload (imports come from `web.import_dirs`); no auth (local, loopback only).
- Windows-only behaviour (WebView2 window, splash, installer, updater swap) is verified by the Windows CI smoke tests;
  the splash and window are not visually checked in CI.
- The live Dukascopy download (paper feed, research-data check) cannot be reached from the build/CI environment; it is
  tested with a synthetic stand-in downloader (`tests/paper_fixture.py`). Real holdout tests run on the user's PC.
- Known stale docs: a reference to a nonexistent `tests/test_reproducibility.py` in
  `research/runs.py`, ADR-10's `FAMILY_<hash>` id scheme (superseded for DSL strategies by
  ADR-23), and `reports/phase1_demo_output.txt` (recorded in an older environment).
- Full list: `ARCHITECTURE.md`, "Known limitations" sections and ADR-72..86.

## Where things are

- Docs: `README.md` (status, quickstart), `ARCHITECTURE.md` (layers, module maps, ADR-1..86, known
  limitations), `CHANGELOG.md` (per change: IMPLEMENTED/TESTED/NOT IMPLEMENTED/REQUIRES REAL DATA, newest first),
  `CONFIG.md`, `DATA_IMPORT.md`, `FEATURES.md` (generated; drift-tested), `STRATEGY_DSL.md`,
  `STRATEGY_GENERATION.md`, `WEB_UI.md`, `DESKTOP_PACKAGING.md` (desktop app, installer, updater, CI),
  `PROP_SIMULATION.md`, `FACTORY_CAPABILITIES.md`.
- Code: `edgelab/{core,data,engine,features,strategy,research,analytics,prop,paper,ai,updater,web}`; `services.py` (the one
  service layer; `read_context`, background backtest jobs, campaigns), `cli.py`, `desktop.py` / `desktop_window.py` /
  `desktop_splash.py` / `workspace_host.py` / `runtime.py` (desktop app). Research runs: `research/campaign.py`,
  `research/batch.py` (sequential + process-parallel runner), `research/jobs.py` (one research job at a time: campaign
  and holdout jobs), `research/holdout.py` (holdout candidates/ranking/job). Paper trading: `paper/{feed,engine,store,
  manager}.py`. Read models: `research/overview.py` (explorer incl. `scope=holdout`), `research/results_view.py`.
  Small files written while pages read them use `core/fsutil.atomic_write_text` (Windows sharing violations). Packaging: `packaging/` (`edgelab.spec`, `build.py`, `release.py`, `installer.iss`,
  `build_installer.py`, `icon.py`, smoke tests). Empty placeholders for later phases: `reports/`, `journal/`,
  `notifications/`, `execution/`.
- Config: `configs/*.yaml` (research config is hashed; `web.yaml` and the example search spec
  `search.example.yaml` are deliberately outside the hash).
- Frontend sources in `web/` (React + TS, esbuild); the built bundle is committed in
  `edgelab/web/static/`, and a test detects a stale bundle, so rebuild after frontend changes.

## Commands

```bash
python -m unittest discover -s tests -t .        # full suite (~1,000 tests, ~15 min here); only when asked
python -m unittest tests.test_x tests.test_y     # targeted: what the user wants before a commit
python scripts/run_tests.py                      # full suite + dashboard test status
python -m edgelab.cli --help                     # data, features, strategy, research commands
python -m edgelab.cli research plan|run|rank ... # Phase 4 batch search (see README)
python -m edgelab.web                            # web app at http://127.0.0.1:8765
python -m edgelab.web --demo                     # separate synthetic demo workspace
python -m edgelab.desktop [--data-root DIR] [--ui window|browser|none]  # desktop launcher from source (own window by default)
python packaging/build.py [--smoke]              # packaged folder build (Windows: build_windows.ps1)
python packaging/build_installer.py              # Windows + Inno Setup 6: dist/release/MunyunLab-Setup-<ver>-b<n>.exe
python packaging/icon.py                         # regenerate packaging/munyun.ico (deterministic)
cd web && npm run typecheck && npm run build     # after ANY frontend change (the committed bundle is tested)
python scripts/phase1_demo.py                    # end-to-end synthetic demo (must stay identical)
python scripts/benchmark_search.py               # Phase 4 search throughput (informational)
```

## Working rules for Claude Code

- Git: remote `origin` = `https://github.com/ShiningRedstone/AI-Backtesting.git`, default branch `main`.
  Do not commit or push unless the user asks for it in that task. ("push it once the tests pass" counts.)
- Delivery flow used in this project:
  1. Develop on branch `claude/zealous-heisenberg-7xwqax` (or the session's designated branch; if a designated remote
     branch holds unrelated old history, deliver through `claude/zealous-heisenberg-7xwqax` instead of force-pushing).
     Other Claude sessions also push to `main`: fetch and build on the latest `origin/main`, and take the next free
     ADR number from it.
  2. Commit only explicit paths (`git add -u` plus named new files; **never `git add .`**).
  3. Push the branch and wait for the Windows CI build to go green.
  4. Fast-forward `main` to it: `git push origin HEAD:main`, no force. Then wait for main's build, which is the
     update the user's app installs.
  5. Watch CI through the GitHub API or MCP tools.
- Never stage or commit: `data/strategy_factory/` (large generated research data) or `histdata_4year_sets.patch`
  (do not modify or delete it either). Preserve local modifications to `tests/test_directional_quotes.py`.
- The user (Ethan) works on Windows, with the clone at `C:\Users\Ethan\Documents\AI-Backtesting`. Python there is
  `.\.venv\Scripts\python.exe`. PowerShell commands given to the user start with
  `Set-Location 'C:\Users\Ethan\Documents\AI-Backtesting'`. The user mostly uses the installed app, not the
  source tree.
- **User preferences (repeated throughout development):**
  - Ask questions instead of assuming. Use multiple-choice questions with a recommended option, and give previews
    for visual choices.
  - Never change how backtests are executed or what they produce: no engine, fill, sizing, cost, compiler or
    prop-rule changes unless explicitly requested. Speed work must give bit-identical results, proven by tests.
  - Never fabricate data or results. Display-only conveniences (caches, groupings) must recompute from real data.
  - Before committing: do a real test run (browser screenshots for UI work) and run only the affected test modules,
    not the full suite, to save time. The Phase 1 demo must stay identical when research code is touched.
  - Explain outcomes in plain English. The user-facing name is "Munyun Lab".
- Stay within the requested task; no unrelated refactors or doc fixes.
- When a feature or phase is done: add an ADR to `ARCHITECTURE.md`, a `CHANGELOG.md` entry, and a line in this file's
  "Current state" (ADR numbering continues after ADR-86). Update `README.md` status for phases.
