# Changelog

Status labels: **IMPLEMENTED** (code exists) · **TESTED** (covered by automated tests) ·
**NOT IMPLEMENTED** (deliberately absent) · **REQUIRES REAL DATA** (cannot be validated on synthetic data).

## Native desktop window (after Phase 8)

| Item | Status |
|---|---|
| `EdgeLab.exe` opens its own EdgeLab window (pywebview + Microsoft Edge WebView2) on the existing loopback app; closing it shuts the backend down cleanly (ADR-52) | IMPLEMENTED, TESTED (deterministic; frozen Linux build) |
| WebView2 detection before start with a clear message; `--ui browser` / `--ui none` explicit alternatives; no silent fallback | IMPLEMENTED, TESTED |
| Second launch focuses the running window (token-protected loopback control channel); never a second server | IMPLEMENTED, TESTED |
| `EdgeLabConsole.exe` (console: CLI, logs, headless smoke) next to the windowed `EdgeLab.exe`; windowed output to `logs/console.log` | IMPLEMENTED, BUILT (Linux) |
| Windows real-window integration test `packaging/window_test_windows.py` (runs with `build_windows.ps1 -Smoke`) | IMPLEMENTED; REQUIRES WINDOWS (not run here) |
| Fixed-version WebView2 bundling, tray icon, installer | NOT IMPLEMENTED |

## Phase 8 (as requested): Strategy Lab and GUI-first strategy research

| Item | Status |
|---|---|
| Strategy page as a Strategy Lab: Research hub (version, provenance, workflow, stored runs with scope, batch-on-datasets), Validate tab (OOS, walk-forward, random control on the whole dataset or OOS window), existing Backtest / Variations / Lineage tabs | IMPLEMENTED, TESTED (API + browser e2e) |
| Compare page: unranked side-by-side metrics (trades, gross/net/cost R, expectancy, PF, max DD, breakeven cost multiple, parameters, scope, costs, prop count) from a lineage / version / batch / search; sort and filter as views; selection → validate / prop | IMPLEMENTED, TESTED |
| Run page: scope badge, equity + drawdown curve from stored trades, session / entry-hour / cost-sensitivity breakdowns | IMPLEMENTED, TESTED |
| Dataset pickers with provider/instrument/timeframe filters, caveats, and eligibility enforced (ineligible not selectable) | IMPLEMENTED, TESTED |
| Variation preview lists the exact combinations before generation; batch → "run on datasets" link | IMPLEMENTED, TESTED |
| Machine-readable provenance `GET /api/strategies/<id>/research` (id, hashes, parent id/hash, batch spec, parameters, runs, validation state) | IMPLEMENTED, TESTED |
| HTTP for existing validation services and the Mode B gate; AI Proposals page (check, then save accepted proposals as strategies) | IMPLEMENTED, TESTED; no LLM connected |
| Real 2024 GUI-path smoke test (`lab_smoke_real.py`) | REQUIRES REAL DATA (local store; verified on a synthetic stand-in) |
| Scoring, ranking, "best" labels, automatic optimization, LLM calls, live/paper trading | NOT IMPLEMENTED (deliberately) |

## Phase 7 (as requested): Windows desktop / exe hardening

| Item | Status |
|---|---|
| `edgelab/runtime.py`: the one dev-vs-packaged resolver (bundled resources, persistent workspace `%LOCALAPPDATA%\EdgeLab` / `--data-root` / `EDGELAB_DATA_ROOT`); default configs copied once, never overwritten; config differences reported (ADR-50) | IMPLEMENTED, TESTED |
| Build manifest `edgelab_build.json` from the real sources (source, compiler and feature impl hashes, git commit, build id); frozen code reads it and refuses without it; development `code_version()` unchanged | IMPLEMENTED, TESTED (dev + simulated frozen) |
| Launcher `edgelab/desktop.py`: freeze_support, single instance per workspace, 127.0.0.1 on a free port, readiness wait, browser, clean shutdown, logged/visible errors, `cli` pass-through | IMPLEMENTED, TESTED |
| Repository-relative paths (config defaults, static bundle, demo fixtures, test status) resolved via `edgelab.runtime` | IMPLEMENTED, TESTED |
| PyInstaller spec, `packaging/build.py`, `build_windows.ps1`, build requirements (no DuckDB, tzdata) | IMPLEMENTED |
| Folder-mode build + packaged smoke test (launch, UI, API, static, config, SQLite, build id, data root outside the bundle, datasets, strategy edit/save/lineage, backtest, prop, single instance, clean shutdown + integrity, restart, CLI search with 2 worker processes) | BUILT AND PASSED on Linux (`dist/EdgeLab/EdgeLab`) |
| Frozen vs development research identity (trades hash, config hash, strategy id, feature-cache keys) | VERIFIED identical |
| `EdgeLab.exe` (Windows) | NOT PRODUCED here (no Windows toolchain; PyInstaller does not cross-compile); build with `build_windows.ps1` |
| Installer, signing, auto-update, tray icon, migration command | NOT IMPLEMENTED (deliberately) |

## Phase 6 (as requested): Prop-firm simulation layer

Roadmap row 7 ("Prop-Firm Simulation"), evaluation rules only. It sits above the engine and reads stored runs.

| Item | Status |
|---|---|
| Versioned prop rule sets (`kind: edgelab.prop_rules`, `schema_version: 1`), with strict validation listing every error, a rules-only hash, and refusal by name of rules the trade records cannot support (trailing from intra-trade highs, payouts, news, weekend holding) (ADR-49) | IMPLEMENTED, TESTED |
| Rules: target, minimum trading days, static drawdown, trailing drawdown (closed-balance or end-of-day reference, optional lock), daily loss (terminate or pause the day) with an explicit reset timezone and time, maximum position units with scaling tiers (violations recorded, never clipped), consistency (best-day share), evaluation deadline, session entry window and flat-by time, payout eligibility (reported only) | IMPLEMENTED, TESTED (synthetic trades) |
| Account replay: chronological (exit, entry, trade_no) order; overlapping positions refused; P&L booked on the exit's trading day; `AccountStatus` enum; per-trade progression (balance, peak, drawdown, day P&L, headrooms, target progress, trading days) and per-day table | IMPLEMENTED, TESTED |
| Detection `end_of_trade`, and `intratrade_bound` (conservative bar-resolution MAE bound; refused for trades crossing the reset); same-trade conflicts (a breach beats the target; drawdown takes precedence when both breach) | IMPLEMENTED, TESTED |
| Multiple independent accounts per simulation (same or different rule sets, optional start); no broker routing | IMPLEMENTED, TESTED |
| Lineage (run, strategy, stored definition/logic hash, dataset, period, cost profile/status, verified trades hash, rule-set ids/hashes, ordering, simulator/code version); deterministic `PROP_` ids; optional storage in `<data>/prop_simulations/`; source run verified unchanged after every simulation | IMPLEMENTED, TESTED |
| Report with the strategy result (the source run's metrics, unchanged) kept separate from the prop-account results; no composite score; labels (not evidence of profitability; SYNTHETIC rules; data and cost caveats) | IMPLEMENTED, TESTED |
| `Services.prop_configs / validate_prop_config / prop_simulate / list_prop_simulations / get_prop_simulation`, CLI `prop ...`, `/api/prop/*` | IMPLEMENTED, TESTED |
| Web: Prop Simulation page; Datasets page gains an Eligible column (reasons from `backtest_readiness`) | IMPLEMENTED, TESTED (browser e2e) |
| Strategy workspace (list, open, edit, validate, save a new version or duplicate, lineage) | VERIFIED (existing Phase 3.5 tests; definition-hash preservation assertions added) |
| Synthetic test-only example rule sets `configs/prop/synthetic_{static,trailing}_eval.yaml` | IMPLEMENTED; no real firm's rules shipped |
| Real stored run through the simulator (`prop_smoke_real.py`) | REQUIRES REAL DATA (local store; the script was verified on a synthetic store) |
| Funded phases, payouts, resets; firm-specific commissions; tick-level intra-trade paths | NOT IMPLEMENTED (deliberately) |
| Windows `.exe` | NOT IMPLEMENTED; architecture and blockers in DESKTOP_PACKAGING.md |

## Real-data import fix (after Phase 4, before Phase 5)

| Item | Status |
|---|---|
| Finding: the first real CFD file (`time,open,high,low,close,volume`; stamps such as `2025-10-02T13:14:00Z` and `2026-04-03T09:10:00-04:00`) failed inspection with "Mixed timezones detected" | FOUND (real data) |
| **Phase 2 importer change:** timestamps that all carry an explicit offset (`Z`, `+/-HH:MM`) are converted to UTC exactly even when offsets differ; naive stamps still require `source_timezone`; explicit + naive mixes and malformed stamps are refused (ADR-42) | IMPLEMENTED, TESTED |
| Whether the file's stamps mark bar open or close, its price basis, volume meaning and broker costs | REQUIRES REAL DATA (user must state them; not inferred) |

## HistData NSXUSD research-proxy configuration (after Phase 4, before Phase 5)

| Item | Status |
|---|---|
| Instrument `NAS100_HISTDATA` (tick 0.001, research proxy, no broker figures) + `unconfigured` cost profile; `NAS100_CFD` unchanged (ADR-43) | IMPLEMENTED, TESTED |
| Calendars `HISTDATA_NSX_R1` (Sun 18:00 - Fri 17:00 NY) and `HISTDATA_NSX_R2` (Sun 18:00 - Fri 16:15 NY); existing calendars unchanged | IMPLEMENTED, TESTED (semantics on synthetic timestamps) |
| Timestamps read as `America/New_York`: supported by FOMC events, contradicts HistData's fixed-EST documentation | EVIDENCE (local real-data diagnostics), unresolved conflict recorded |
| Exclusion of the 17:00-17:59 NY DST-transition source anomaly | superseded by ADR-44 below |
| 2017 and 2023 as full-year datasets | REJECTED (missing bars > 5% under their own-schedule calendars) |
| Any HistData year passing validation with the new calendars | REQUIRES REAL DATA (not run in this environment) |
| 2018 real file (local run): `HISTDATA_NSX_R1`, 348,607 rows, 2.873% missing, 131 outside-session bars (WARN), gate accepts with WARN, no exclusions | MEASURED LOCALLY (in memory, not stored) |
| **Phase 2 importer change:** audited source-quality exclusion windows (`source_exclusions.<NAME>` in `configs/data.yaml`, `--source-exclusions`); refuses malformed/overlapping/empty windows and any window touching an in-session bar; full record in the manifest; thresholds and calendars unchanged (ADR-44) | IMPLEMENTED, TESTED (synthetic) |
| Set `HISTDATA_NSXUSD_2019` (20 windows, 17:00-18:00 EDT) | VERIFIED LOCALLY (real file: 1,197 bars excluded, 0 outside-session left, 6,961 missing / 2.005% WARN, gate accepts in memory) |
| Sets `HISTDATA_NSXUSD_2020` / `_2021` / `_2022` / `_2024` (20 / 15 / 15 / 17 windows, 17:00-18:00 EDT, dates from the local trace; none for 2024-10-28/29) | CONFIGURED, TESTED (config); REQUIRES REAL DATA (local run must confirm every window matches bars and the gate accepts) |
| Exclusion set for 2023 | NOT IMPLEMENTED (deliberately; 2023 stays coverage-rejected) |

## Phase 5: Research Analytics (in progress)

| Item | Status |
|---|---|
| `analytics/research.py`: pooled (entry-time ordered, not averaged) and per-dataset metrics, stability counts, canonical-session and entry-hour breakdowns, exact cost sensitivity at the configured multipliers, break-even cost multiple, caveat labels (ADR-46) | IMPLEMENTED, TESTED |
| `Services.research_report(run_ids)`, CLI `report RUN_ID ...`, `GET /api/results/report?run_ids=...` (one fixed strategy; mixed strategies and repeated datasets refused) | IMPLEMENTED, TESTED |
| Research page UI, Monte Carlo, walk-forward/OOS, distributions, weekday/month breakdowns | NOT IMPLEMENTED (deferred) |
| Real five-year EMA pipeline baseline analysed through the report | REQUIRES the local store (runs are local) |
| Validation of fixed strategies (ADR-47): OOS split (`IN_SAMPLE` / `OUT_OF_SAMPLE` runs), rolling/anchored walk-forward (`WALK_FORWARD` runs), frozen definition checks, seeded bootstrap/shuffle Monte Carlo of observed trades; `Services.evaluate_oos`, `Services.walk_forward`, CLI `validate`; `research_report` gains `monte_carlo` | IMPLEMENTED, TESTED |
| Matched random-entry control (ADR-48): candidate's own eligibility/levels/cooldown/exits/sizing/costs, entry timing and direction randomized per bar (seeded, prefix-stable), calibrated to the candidate's pre-cooldown entry count and long share (method v2; v1 under-fired cooldown strategies); N realizations, candidate-vs-control distribution summary; `Services.random_entry_control`, CLI `validate control`; control results never stored as runs | IMPLEMENTED, TESTED |
| **Phase 3 compiler refactor:** `signals_from_features` split into shared helpers, behaviour-preserving (8/8 fixtures identical) | IMPLEMENTED, TESTED |
| Walk-forward across several datasets; validation HTTP/UI | NOT IMPLEMENTED (deferred) |

## HistData research cost baseline (after HistData import, before Phase 5)

| Item | Status |
|---|---|
| `costs.symbols.NAS100_HISTDATA.providers.HISTDATA`: approved MNQ-equivalent research ASSUMPTIONS (`status: assumed`; commission 0.50/unit/side, slippage 0.25 pts market/stop, 0 limit, fixed spread 0.50 pts, financing none). Symbol level and other feeds stay unconfigured; `NAS100_CFD`/`US100_CFD`/`NQ_CFD` unchanged | CONFIGURED, TESTED |
| These values as a broker's actual costs | NOT CLAIMED (assumed research baseline; replace per broker when verified) |

## Dataset reload fix (after HistData import, before Phase 5)

| Item | Status |
|---|---|
| **Phase 2 fix:** `load_validated` kept overwriting import-time cleaning facts (`source_detail.cleaning`, `raw_duplicate_bars`, `duplicate_bars`) on reload, so datasets whose source needed cleaning reloaded with a different manifest hash; it now keeps the stored facts (ADR-45) | IMPLEMENTED, TESTED |
| Real HistData 2019-2022, 2024 imported locally (`NAS100_HISTDATA_<YEAR>`, WARN, exclusions 1197/1106/900/900/898, 60-row rollback cleaned); only the reload manifest-hash check failed before this fix | MEASURED LOCALLY; re-check after the fix REQUIRES the local run |

## Phase 4: Batch Research & Search

| Item | Status |
|---|---|
| Search spec (`research/search.py`): sources = strategy ids, Mode A `VB_` batches, Mode B `PB_` batches, families; datasets; period (full / common / explicit with timezone); ranking settings; `max_cells`; seed; workers. Strict validation (unknown keys, malformed values, invalid references) with every issue reported; example `configs/search.example.yaml` (outside the config hash) | IMPLEMENTED, TESTED |
| Deterministic planner: strategy x dataset cells in plan order; duplicate strategies collapse; archived batch/family members excluded and reported; eligibility = the exact `backtest_readiness` reasons (unconfigured CFD costs stay ineligible) plus "outside the period"; eligible cells above `max_cells` refused before anything runs, never truncated | IMPLEMENTED, TESTED |
| Identity: `search_hash` = canonical sources, datasets, period, seed, spec version + execution `config_hash` (ranking, `max_cells`, workers excluded); `cell_id` = strategy_id, dataset_id, dataset content hash, config_hash, search_hash (ADR-32) | IMPLEMENTED, TESTED |
| One cell pipeline: `Services._run_cell` / `_record_cell` extracted from `backtest_strategy` (unchanged output); `_dataset_eligibility` from `backtest_readiness` (ADR-33) | IMPLEMENTED, TESTED |
| Durable SQLite storage: `search_batches`, `search_cells` (additive, ADR-34); the `runs` table stays authoritative; every planned cell stored (completed incl. zero-trade / failed with error / ineligible with reasons / cancelled) | IMPLEMENTED, TESTED |
| `run_search`: synchronous, plan order, lineage (`parent_strategy_id`, `mutation`) and search/cell id on each run, status `IN_SAMPLE`; resume skips completed cells whose run exists; failed/pending/cancelled cells re-run | IMPLEMENTED, TESTED |
| Trial accounting per invocation and cumulative over the CURRENT plan; cells of an earlier plan kept as historical, never counted (ADR-35) | IMPLEMENTED, TESTED |
| In-sample ranking (`research/ranking.py`): expectancy_r (default) / profit_factor / net_r, win rate refused, minimum sample label, deterministic ties, exclusions counted by reason, +inf profit factor recognised from stored fields and flagged (never a number); every output labelled in-sample / NOT VALIDATED with its trial count; shortlist = a tag on the batch, no run status changed (ADR-36) | IMPLEMENTED, TESTED |
| Mode B proposal-batch records (`kind: proposal`) saved by `ingest_proposals(save=True)`; `list_variation_batches(kind=)` with unchanged default; derived, verified strategy-library index (ADR-37) | IMPLEMENTED, TESTED |
| Background jobs (`research/jobs.py`): one worker thread, one active job (409 otherwise), queued -> running -> completed / failed / cancelled, polling progress from stored cells, cooperative cancellation between cells, restart reconciliation (`running` -> `interrupted`, never auto-resumed); the service lock is shared and never held during a backtest (ADR-38) | IMPLEMENTED, TESTED |
| `FeatureCache` thread and process safety: private lock for the memory LRU and stats; unique temp files for metadata and arrays, metadata written first (ADR-39) | IMPLEMENTED, TESTED (thread + multi-process stress) |
| Process-parallel search (`workers > 1`, spawned processes): workers compute only, the parent writes everything in plan order; `workers=1` and `workers=N` give identical cell ids, statuses, trades hashes and errors (ADR-40) | IMPLEMENTED, TESTED |
| `scripts/benchmark_search.py` (synthetic, informational): measured 32 cells, 17.97 s sequential vs 6.78 s with 4 workers on a 4-CPU container, results identical; tiny searches are slower in parallel (process start-up) | IMPLEMENTED, TESTED (runs in a test) |
| `research_config_options` exposes the stored strategies and the search spec options (the Phase 2 "NOT IMPLEMENTED" placeholder is gone) | IMPLEMENTED, TESTED |
| Research CLI: `research validate / plan / run [--workers N] / rank / job` | IMPLEMENTED, TESTED |
| Research HTTP API: `/api/research/validate`, `plan`, `jobs`, `jobs/<id>`, `jobs/<id>/cancel`, `searches`, `searches/<id>`, `searches/<id>/ranking`, `searches/<id>/shortlist`; 422 search spec / ranking, 409 job conflict / unsupported storage, 404 unknown job or search | IMPLEMENTED, TESTED |
| Research web page: setup, spec check, plan preview, background job with polling and cancel, searches list, current and historical cells, in-sample ranking, shortlist; committed bundle rebuilt | IMPLEMENTED, TESTED (browser) |
| `research/compare.py`: cross-dataset warnings extracted into `comparison_warnings` (same text, ADR-41) | IMPLEMENTED, TESTED |
| Analytics breakdowns, OOS, walk-forward, Monte Carlo, significance testing, null-control services, prop simulation, reports, paper/live trading, AI model calls | NOT IMPLEMENTED (Phase 5+) |
| Research on real market or CFD data; broker CFD costs | REQUIRES REAL DATA (none imported; all results so far are synthetic) |

Tests: full regression 416 passed, 4 skipped (DuckDB x3, slow opt-in); the Phase 4 audit run had 415 before the example-config test was added. Phase 4 added 98 tests (318 after Phase 3.5), including 9 browser end-to-end tests of which 4 cover the Research page. Phase 1 demo: identical to the pre-Phase-4 output apart from the source hash and timings.

## Phase 3.5: Strategy Builder & research UI

| Item | Status |
|---|---|
| Web app: `python -m edgelab.web` (Flask API over `services`, committed React + TypeScript bundle); `--demo` separate synthetic workspace | IMPLEMENTED, TESTED |
| App shell: sidebar, top bar with strategy search, backend status and Settings; mobile drawer; demo banner | IMPLEMENTED, TESTED (browser) |
| Dashboard: status, version, frontend build, last test run, counts, quick actions gated on prerequisites | IMPLEMENTED, TESTED |
| Strategy Builder: general/family, timeframe, trading window, weekdays, local sessions, 5 parameter types, ALL/ANY/NOT condition trees with all DSL operators, operands (constant/parameter/bar/feature/arithmetic), registry-driven feature picker, higher-timeframe operands, market/stop/limit entries with expiry, stops, targets, time and signal exits, sizing | IMPLEMENTED, TESTED (browser) |
| Live backend validation with section badges; Validate; Compile & Explain; backend DSL preview (draft/canonical/JSON), copy, download, load | IMPLEMENTED, TESTED |
| Save / Save As New / Duplicate / Edit with lineage methods; unchanged logic saves nothing; invalid strategies refused | IMPLEMENTED, TESTED |
| Library (filters, archive/restore with confirmation), strategy page, families with lineage tree and accessible table, variation batches | IMPLEMENTED, TESTED |
| Mode A in the UI: grid / one-at-a-time / seeded random, backend combination preview and cap, results with duplicates, batch ID and compare | IMPLEMENTED, TESTED (browser) |
| Datasets: library, metadata and validation report, import over the Phase 2 pipeline (import folders only) | IMPLEMENTED, TESTED (API) |
| Single backtest: readiness per dataset (validation, timeframe, cost reasons), explicit selection, run recorded in the Phase 1 run registry; synthetic runs labelled and listed separately; CFD refused while costs are unconfigured | IMPLEMENTED, TESTED (browser + API) |
| Results page (single runs only), Research and AI Discovery placeholders, read-only Settings | IMPLEMENTED (the Research placeholder was replaced by the Phase 4 Research page) |
| Service additions (system_status, builder_options, render_strategy, variation_preview, archive/restore, batches, family_detail, backtest_readiness, list_runs, get_run); `backtest_strategy(record=)` | IMPLEMENTED, TESTED |
| **Phase 1 store change:** SQLite `check_same_thread=False` (ADR-30); Phase 1 demo identical | IMPLEMENTED, TESTED |
| Lineage library: reversible archive, richer listing, batch listing; variation summaries include overrides; feature `describe()` exposes session parameters | IMPLEMENTED, TESTED |
| Optional `configs/web.yaml`, outside the research config hash | IMPLEMENTED, TESTED |
| `scripts/run_tests.py` (dashboard test status); stale-bundle test; TypeScript type-check test | IMPLEMENTED, TESTED |
| Batch research, analytics, OOS, walk-forward, Monte Carlo, prop simulation, reports, paper/live trading, AI model calls | NOT IMPLEMENTED (Phase 4+); batch research was implemented in Phase 4 |

Tests: 318 (20 new: 15 API, 5 browser end-to-end); 4 skipped (DuckDB x3, slow opt-in).

## Phase 3: Strategy DSL, compiler, lineage, controlled variations, proposal interface

### DSL and validation (`edgelab/strategy/dsl.py`, STRATEGY_DSL.md)
| Item | Status |
|---|---|
| Versioned DSL v1 (YAML/JSON/dict): family, timeframe, local sessions, parameters, entry, exit, sizing | IMPLEMENTED, TESTED |
| Operands: constants, `$param`, bar fields with lag, feature outputs (params/output/HTF/lag/pinned version), add/sub/mul/div | IMPLEMENTED, TESTED |
| Conditions: all/any/not, `> >= < <= == !=`, crosses_above/below, per-condition `enabled` and `label` | IMPLEMENTED, TESTED |
| Typed parameters (integer/float/boolean/choice/timeframe) with strict domain and grid checks | IMPLEMENTED, TESTED |
| Validator: schema, parameter, feature, logic, architecture and causality rules; all issues reported with paths and suggestions; CLI exit code 2 | IMPLEMENTED, TESTED |
| Unsupported concepts refused by name: trailing stop, breakeven, partial exits, pyramiding, exit-based cooldown, lead/future | IMPLEMENTED, TESTED |
| Canonical form; `logic_hash` (strategy_id) vs `definition_hash`; session definitions part of identity | IMPLEMENTED, TESTED |

### Compiler (`edgelab/strategy/compiler.py`)
| Item | Status |
|---|---|
| Deterministic compile into `OrderSpec` + sizing config + de-duplicated `FeatureSpec`s -> `DSLStrategy` (FeatureStrategy / Strategy) | IMPLEMENTED, TESTED |
| Entries: market / stop / limit with expiry; long / short / both; trading window; trading-date weekdays; signal cooldown | IMPLEMENTED, TESTED |
| Exits: points / ATR / price stops; none / points / ATR / price / R-multiple targets; time stop; max hold; signal exits | IMPLEMENTED, TESTED |
| Sizing: fixed quantity (checked against instrument size rules at bind) and risk-based | IMPLEMENTED, TESTED |
| Kleene three-valued evaluation; ambiguous both-direction bars dropped; invalid stop/target sides dropped and counted | IMPLEMENTED, TESTED |
| Causality: static rules + Phase 1 truncation check (incl. exit arrays) on every run; MTF cut inside unfinished HTF bars | IMPLEMENTED, TESTED |
| Provenance (DSL/compiler versions, compiler source hash, feature spec ids + impl hashes, sessions, config hash); `explain()` | IMPLEMENTED, TESTED |
| Same definition on futures and CFD datasets; CFD runs refused while broker costs are unconfigured | IMPLEMENTED, TESTED (synthetic) |

### Engine and features (Phase 1/2 modules changed)
| Item | Status |
|---|---|
| **Engine change:** optional `SignalSet.exit_long/exit_short`; exit at next open, reason `SIGNAL` (market costs); earlier exits win; gap at that open uses the existing gap policy. Absent arrays = unchanged behaviour (219 prior tests + Phase 1 demo identical) | IMPLEMENTED, TESTED |
| New feature `time_of_day` v1 (weekday, trading_weekday, hour, minute_of_day; known at bar open); FEATURES.md regenerated | IMPLEMENTED, TESTED |

### Lineage, Mode A, Mode B, services
| Item | Status |
|---|---|
| Families vs instances; `LineageRecord` (method, parent, exact changes, batch, timestamp, versions); file-based `StrategyLibrary` (atomic, idempotent, multi-parent, ancestry/children) | IMPLEMENTED, TESTED |
| Mode A: grid / one_at_a_time / seeded random_sample; declared-domain enforcement; cap before generation; invalid child fails batch; logic dedupe reported; reproducible batch ids and records | IMPLEMENTED, TESTED |
| Mode B: capability menu, `StrategyProposer` protocol, `StaticProposer`, ingestion gate (strict schema, claim-language rejection, same validator/compiler, duplicate/structure checks, lineage) | IMPLEMENTED, TESTED |
| Services (strict JSON) + CLI `strategy validate/compile/explain/save/variations/proposals/list/show/lineage/menu/backtest` | IMPLEMENTED, TESTED |
| 8 strategy fixtures, variation spec, proposal batch (test fixtures, not claims of edge) | IMPLEMENTED, TESTED |

### Not implemented (deliberately)
| Item | Status |
|---|---|
| Trailing stops, breakeven, partial exits, pyramiding, exit-based cooldown (engine does not support them) | NOT IMPLEMENTED (refused by the validator) |
| Batch execution / ranking of variations and proposals | NOT IMPLEMENTED in Phase 3; IMPLEMENTED in Phase 4 |
| Any AI model call | NOT IMPLEMENTED (interface only) |
| UI, live trading, broker execution | NOT IMPLEMENTED (out of scope) |
| Performance of any fixture strategy on real data | REQUIRES REAL DATA |

Tests: 298 (4 skipped: DuckDB unavailable, slow tests opt-in); Phase 3 added 79.

## Phase 2: Features + CFD-ready data foundation

### Data foundation
| Item | Status |
|---|---|
| Generic import pipeline: inspect, normalize, validate, manifest, hash, store, derive timeframes, build features (`data/importer.py`, CLI, services) | IMPLEMENTED, TESTED |
| Layout presets `generic_csv`, `mt5_export`, `dukascopy_csv`; column mapping; date+time columns; epoch timestamps | IMPLEMENTED, TESTED (on synthetic files in those layouts) |
| Timezones: IANA zones and broker-server clocks (`America/New_York+7h`); close to open conversion; DST gap / repeated hour refused | IMPLEMENTED, TESTED |
| Manifest provenance (dataset name, asset type, symbol, source tz/convention, file sha256, volume type, price basis, spread source, bid/ask, calendar + fingerprint, import version, parent/derivation) and deterministic `manifest_hash()` | IMPLEMENTED, TESTED |
| Content-addressed dataset ids; idempotent re-import; no overwrite; identical content flagged | IMPLEMENTED, TESTED |
| Optional per-bar spread (part of the content hash when present; absent -> Phase 1 hashes unchanged) | IMPLEMENTED, TESTED |
| Validation: `spread_non_negative` (FAIL), `spread_availability` (WARN), `volume_availability` | IMPLEMENTED, TESTED |
| Store: validation reports, dataset listing, spread column migration, schema-evolving result tables | IMPLEMENTED, TESTED (SQLite); DuckDB paths IMPLEMENTED, untested (duckdb unavailable) |
| `load_validated`: re-runs the gate, checks content hash and calendar fingerprint | IMPLEMENTED, TESTED |
| Real CFD data quality, broker server clocks, broker trading hours, spread units, MT5 export details | REQUIRES REAL DATA |

### Sessions and features
| Item | Status |
|---|---|
| Configurable DST-safe session windows (NY 09:00-10:00, 10:00-11:00, 09:30-10:30, RTH, AM/PM, London, Tokyo, NY-evening Asia with midnight wrap) | IMPLEMENTED, TESTED (US/UK DST, mismatch weeks, fall-back, gaps, ambiguity) |
| 14 features: candle, atr, ema, sma, rsi, roc, vwap, volume_stats, rvol_tod, session, daily_levels, swings (BOS, sweeps, trend), range_stats, fvg | IMPLEMENTED, TESTED (hand-computed known answers) |
| Machine-readable feature definitions and generated `FEATURES.md` (drift-tested) | IMPLEMENTED, TESTED |
| Causality: truncation checker; every feature and HTF variant passes; checker proven to catch 5 leak classes | IMPLEMENTED, TESTED |
| Multi-timeframe: HTF exposed only at effective close (partial session-end bucket at session close) | IMPLEMENTED, TESTED |
| Persistent feature cache: full-input keys incl. implementation hash and dependency keys, checksum-verified, corruption-safe, memory LRU, per-engine memo | IMPLEMENTED, TESTED |
| Volume features refuse no-volume datasets; tick volume recorded in result metadata | IMPLEMENTED, TESTED |
| `FeatureStrategy` / `FeatureContext`: dataset-agnostic strategy consumption; Phase 1 causality check passes end to end | IMPLEMENTED, TESTED |
| Whether any feature is predictive on real markets | REQUIRES REAL DATA (and Phase 6 validation) |

### Costs, comparison, interfaces
| Item | Status |
|---|---|
| Cost profiles with status; per-provider overrides; CFD profiles ship unconfigured and refuse | IMPLEMENTED, TESTED |
| Dataset spread mode, point-based slippage, overnight financing (rollover instants, triple day, credits) | IMPLEMENTED, TESTED (exact post-hoc cost sensitivity preserved) |
| Fractional CFD unit sizing (`min_size`, `size_step`) | IMPLEMENTED, TESTED |
| Broker-specific CFD numbers | NOT IMPLEMENTED (by design: user must enter them); REQUIRES REAL DATA / broker schedules |
| Bid/ask trigger asymmetry for stops on bid-based feeds | NOT IMPLEMENTED (documented limitation) |
| Dataset comparison: per-dataset independent runs with provenance, feed comparison, period restriction; no merged or averaged dataset | IMPLEMENTED, TESTED |
| Service layer (strict-JSON contracts for Data Center / Feature Lab / Research Configuration) and CLI | IMPLEMENTED, TESTED |
| Web UI | NOT IMPLEMENTED (contracts ready; no placeholder UI was built) |
| Benchmark script and results (`reports/phase2_benchmark.txt`) | IMPLEMENTED (synthetic data) |
| Strategy DSL, variation generation (Mode A), AI strategy families (Mode B), batch search | NOT IMPLEMENTED in Phase 2; IMPLEMENTED in Phases 3-4 |

### Changes to Phase 1 code (all Phase 1 tests still pass)
- **Bug fix - resampling fabricated volume.** `resample_bars` summed volume with pandas `sum()`, so a
  bucket with no volume became 0.0 and a partially missing bucket was under-counted. A bucket's
  volume is now NaN unless every sub-bar has volume. (Never triggered in Phase 1, which had no
  missing volume; would have corrupted any no-volume CFD feed.)
- **Correctness fix - invented CFD cost numbers removed.** `costs.yaml` contained a CFD spread of
  1.0 and 2-tick slippage that were not from any broker. CFD profiles are now `unconfigured`.
  Consequently `tests/test_costs_sizing.py::test_cfd_spread` now builds an explicit
  `CostModel(spread_points=1.0)` (same arithmetic assertion) and additionally asserts that the
  unconfigured profile refuses.
- Additive, backward-compatible: `BarArrays.spread` (optional), new manifest fields with defaults
  and a tolerant `DatasetManifest.from_dict`, `SessionCalendar.fingerprint()`,
  `validate_and_freeze(**manifest_fields)`, instrument `min_size`/`size_step`/`underlying`,
  sizing floors to the size step (integer contracts unchanged for futures), backtester size check
  `<= 0` (identical for integer sizes), new trade column `financing_usd`, assumption `cost_status`,
  `CostModel` new fields and `round_trip_base(..., spread_points=None)`.
- CFD instrument templates changed to a 0.01 price grid with explicit unit sizing (were 0.1 ticks).

### Test counts
Phase 1: 104 tests (unchanged except `test_cfd_spread`, above). Phase 2: 115 new tests in
`test_features_numeric`, `test_sessions_dst`, `test_mtf_causality`, `test_feature_cache`,
`test_cfd_data`, `test_cfd_costs`, `test_dataset_compare`, `test_services_cli`.

## Phase 1: Foundation
Configuration, JSON logging, data schema and mandatory validation gate, synthetic data, calendars,
session-anchored resampling, fill model, costs, sizing, backtester with causality enforcement,
metrics with sample labels, run records, SQLite store (DuckDB backend untested), 104 tests.
