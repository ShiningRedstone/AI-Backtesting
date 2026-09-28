# Changelog

Status labels: **IMPLEMENTED** (code exists) · **TESTED** (covered by automated tests) ·
**NOT IMPLEMENTED** (deliberately absent) · **REQUIRES REAL DATA** (cannot be validated on synthetic data).

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
| Results page (single runs only), Research and AI Discovery placeholders, read-only Settings | IMPLEMENTED |
| Service additions (system_status, builder_options, render_strategy, variation_preview, archive/restore, batches, family_detail, backtest_readiness, list_runs, get_run); `backtest_strategy(record=)` | IMPLEMENTED, TESTED |
| **Phase 1 store change:** SQLite `check_same_thread=False` (ADR-30); Phase 1 demo identical | IMPLEMENTED, TESTED |
| Lineage library: reversible archive, richer listing, batch listing; variation summaries include overrides; feature `describe()` exposes session parameters | IMPLEMENTED, TESTED |
| Optional `configs/web.yaml`, outside the research config hash | IMPLEMENTED, TESTED |
| `scripts/run_tests.py` (dashboard test status); stale-bundle test; TypeScript type-check test | IMPLEMENTED, TESTED |
| Batch research, analytics, OOS, walk-forward, Monte Carlo, prop simulation, reports, paper/live trading, AI model calls | NOT IMPLEMENTED (Phase 4+) |

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
| Batch execution / ranking of variations and proposals | NOT IMPLEMENTED (Phase 4) |
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
| Strategy DSL, variation generation (Mode A), AI strategy families (Mode B), batch search | NOT IMPLEMENTED (Phases 3-4) |

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
