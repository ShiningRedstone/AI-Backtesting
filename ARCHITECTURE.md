# Architecture

## Purpose

A research machine whose job is to **disprove** trading strategies as aggressively as it
discovers them. Every design decision below favours "a wrong answer is impossible or
loud" over "the backtest looks good".

## Layers

```
configs/*.yaml  +  EDGELAB__* env overrides
        │
        ▼
┌──────────────────────── DATA ────────────────────────┐
│ providers (CSV, synthetic; vendor stubs → DATA REQUIRED)
│        │ raw frame
│        ▼
│ validate_and_freeze ── DataQualityReport (PASS/INFO/WARN/FAIL)
│        │   FAIL → DataIntegrityError (nothing downstream runs)
│        ▼
│ ValidatedDataset  (read-only arrays + manifest + content hash)
│ resample (session-anchored)   store (DuckDB+Parquet | SQLite)
└───────────────────────────────────────────────────────┘
        │
        ▼
┌──────────────────────── ENGINE ──────────────────────┐
│ Strategy.generate_signals ──► check_causality (lookahead gate)
│        │ SignalSet (decided at bar close)
│        ▼
│ sizing (at signal time) → find_entry → simulate_exit
│   (fills.py: gaps, same-bar conflicts, intrabar replay,
│    session flatten, time exits)            costs.py
│        ▼
│ BacktestResult: trades + skipped-signal reasons + assumptions
└───────────────────────────────────────────────────────┘
        │
        ▼
analytics/metrics ── research/runs (run record, RUN_YYYY_NNNNN) ── store
```

Later phases plug in without changing these contracts: features (P2) produce cached
arrays that strategies read; the DSL (P3) compiles to `Strategy`; batch search (P4)
calls `run_backtest` per variant; prop simulation (P7) consumes `trades`.
Execution (P9–11) will be a separate package that never imports research code paths
for order placement.

## Module map (Phase 1)

| Module | Responsibility |
|---|---|
| `core/config.py` | YAML load, env overrides, validation, stable config hash, env-only secrets |
| `core/logging.py` | JSON-lines structured logs with fixed field set |
| `core/identity.py` | content hashes, code version (git + source hash), environment fingerprint |
| `instruments.py` | contract metadata; point value derived and cross-checked |
| `data/calendar.py` | trading dates, sessions, DST-correct expected bar grid |
| `data/schema.py` | canonical bars, `BarArrays`, `DatasetManifest`, `DataRequiredError` |
| `data/validation.py` | integrity checks, non-fabricating cleaning, `ValidatedDataset` gate |
| `data/resample.py` | session-anchored aggregation, HTF→LTF mapping with reliability flags |
| `data/synthetic.py` | random-walk generator with known properties; hand-built bars for tests |
| `data/providers.py` | provider interface; CSV (timezone + close-stamp conversion); honest stubs |
| `data/store.py` | `ResultStore` with DuckDB+Parquet and SQLite backends |
| `engine/signals.py` | `Strategy`, `OrderSpec`, `SignalSet`, empirical causality checker |
| `engine/fills.py` | pure-NumPy fill/exit kernel |
| `engine/backtester.py` | gates, session flags, sizing, costs, R accounting, disclosed assumptions |
| `engine/costs.py`, `engine/sizing.py` | cost model with exact sensitivity; floor-rounded risk sizing |
| `analytics/metrics.py` | core metrics with sample-size labels and CIs; cost sensitivity |
| `research/runs.py` | reproducible run records and status labels |
| `strategies/examples.py` | `Breakout` (engine exercise, not a claim of edge), `RandomEntry` (null model) |

## Module map (Phase 2 additions)

```
edgelab/data/importer.py        raw file -> inspect -> normalize -> validate -> manifest/hash -> store
                                -> derive timeframes -> features; load_validated (re-validates)
edgelab/data/schema.py          + optional per-bar spread; manifest provenance fields; manifest_hash()
edgelab/data/store.py           + dataset_reports table, list_datasets, spread column, schema evolution
edgelab/features/spec.py        FeatureSpec (identity), FeatureDef (math + docs + metadata), registry
edgelab/features/sessions.py    named DST-safe session windows (membership, instances, scheduled end)
edgelab/features/library/       price.py (candle, atr, ema, sma, rsi, roc), volume.py (volume_stats,
                                rvol_tod, vwap), session_levels.py (session, daily_levels),
                                structure.py (swings, range_stats), fvg.py
edgelab/features/mtf.py         higher-timeframe build + leak-free mapping (effective close)
edgelab/features/engine.py      FeatureEngine: requirements, cache keys, dependencies, HTF, memo
edgelab/features/cache.py       persistent content-addressed cache (npz + json, sha256, LRU)
edgelab/features/causality.py   truncation-based no-lookahead check for features
edgelab/features/strategy_api.py FeatureStrategy / FeatureContext: dataset-agnostic consumption
edgelab/features/docs.py        FEATURES.md generator
edgelab/engine/costs.py         + status/profiles, dataset spread, point slippage, financing
edgelab/research/compare.py     per-dataset independent runs, compare_feeds, period restriction
edgelab/services.py             JSON contracts: Data Center, Feature Lab, Research Configuration
edgelab/cli.py                  command line over services.py
```

## Module map (Phase 3 additions)

```
edgelab/strategy/dsl.py          schema, loading, parameters, $references, resolve, validation with
                                 paths/suggestions, UNSUPPORTED map, canonical form, logic/definition hashes
edgelab/strategy/compiler.py     definition -> OrderSpec + sizing + FeatureSpecs -> DSLStrategy
                                 (FeatureStrategy); Kleene evaluation; diagnostics; explain()
edgelab/strategy/lineage.py      StrategyFamily, Change, LineageRecord, StrategyLibrary (JSON files)
edgelab/strategy/variations.py   Mode A: spec validation, grid / one_at_a_time / seeded random_sample,
                                 cap-before-generate, dedupe, batch records
edgelab/strategy/proposals.py    Mode B: ProposalRequest.capability_menu, StrategyProposer protocol,
                                 StaticProposer, ingest_proposals gate (no LLM calls)
edgelab/engine/signals.py        + optional SignalSet.exit_long / exit_short; causality check covers them
edgelab/engine/fills.py          + simulate_exit(signal_exit_bar=...)
edgelab/engine/backtester.py     + first exit flag at/after entry -> exit at next open; "SIGNAL" = market
edgelab/features/library/session_levels.py  + time_of_day feature (weekday, trading_weekday, hour, minute)
edgelab/services.py, cli.py      + Strategy Lab contracts and `strategy` command group
strategies/fixtures/             8 strategy fixtures, a variation spec, a proposal batch (tests only)
```

## Module map (Phase 3.5 additions)

```
edgelab/web/app.py         Flask API: routes -> Services, strict inputs, error mapping, SPA serving
edgelab/web/__main__.py    `python -m edgelab.web [--demo] [--root] [--host --port --allow-remote]`
edgelab/web/config.py      optional configs/web.yaml (outside the research config and its hash)
edgelab/web/demo.py        separate demo workspace: synthetic CSVs -> normal import pipeline + fixtures
edgelab/web/bundle.py      recomputes the frontend source hash (stale-bundle detection)
edgelab/web/static/        built frontend (committed): index.html, app.js, styles.css, build-info.json
edgelab/services.py        + system_status, builder_options, render_strategy, variation_preview,
                           archive/restore, batches, family_detail, backtest_readiness,
                           backtest_strategy(record=True) -> run registry, list_runs, get_run
edgelab/strategy/lineage.py  + reversible archive, richer listing, batch listing
edgelab/data/store.py      SQLite connection opened with check_same_thread=False (see ADR-30)
web/                       React + TypeScript sources, esbuild build script, tsconfig, React type shim
scripts/run_tests.py       full suite + reports/last_test_run.txt for the dashboard
```

## Module map (Phase 4 additions)

```
edgelab/research/search.py   search spec (strict validation), canonical form, search_hash, cell_id,
                             deterministic strategy x dataset planner with eligibility + max_cells
edgelab/research/batch.py    run_search: persist batch + every cell, run eligible cells in plan order
                             (sequential, or spawned worker processes that only compute), resume,
                             accounting, cooperative cancel hook; search_summary
edgelab/research/ranking.py  pure in-sample ranking of current cells; shortlist tag
edgelab/research/jobs.py     JobManager: one background worker thread, one active job, progress,
                             cancellation, restart reconciliation
edgelab/research/compare.py  + comparison_warnings (extracted; ADR-41)
edgelab/data/store.py        + search_batches / search_cells tables and accessors (SQLite; ADR-34)
edgelab/features/cache.py    + thread/process-safe memory LRU and disk writes (ADR-39)
edgelab/strategy/lineage.py  + derived index.json, batch kinds, batch_members, verify_index (ADR-37)
edgelab/strategy/proposals.py + IngestReport.record() (the Mode B batch record)
edgelab/services.py          + _run_cell/_record_cell, _dataset_eligibility (ADR-33), Services.lock,
                             validate/plan/run/list/get search, rank_search, select_shortlist,
                             start_search_job/job_status/cancel_job, research_config_options search
edgelab/cli.py               + `research validate|plan|run|rank|job`
edgelab/web/app.py           + /api/research/* routes and error mapping; uses Services.lock (ADR-38)
web/src/pages/ResearchEngine.tsx, web/src/api/research.ts   Research page and its API calls
configs/search.example.yaml  example search spec (not read by load_config; outside the config hash)
scripts/benchmark_search.py  synthetic throughput benchmark (sequential vs worker processes)
```

## Module map (Phase 5 additions, in progress)

```
edgelab/analytics/research.py  pooled / per-dataset metrics, stability counts, canonical-session and
                               entry-hour breakdowns, exact cost sensitivity + break-even, report
                               caveat labels; every number via analytics.metrics (ADR-46)
edgelab/services.py            + research_report(run_ids)
edgelab/cli.py                 + `report RUN_ID ...`
edgelab/web/app.py             + GET /api/results/report?run_ids=...
edgelab/research/validation.py OOS split / walk-forward windows, frozen definitions, seeded
                               trade-resampling Monte Carlo (ADR-47)
edgelab/services.py            + evaluate_oos, walk_forward; research_report gains monte_carlo;
                               _run_cell/_record_cell pass a run status (default IN_SAMPLE)
edgelab/cli.py                 + `validate oos|walkforward STRATEGY DATASET_ID ...`
edgelab/research/controls.py   matched random-entry control (RandomEntryControl, summarize) (ADR-48)
edgelab/strategy/compiler.py   signals_from_features split into _entry_allowed/_orders/_apply_cooldown/
                               _emit (behaviour-preserving; shared with the control)
edgelab/services.py            + random_entry_control; CLI `validate control`
```

## Module map (Phase 6 additions: prop-account simulation)

```
edgelab/prop/rules.py          versioned prop rule sets: strict validation, named refusals, rules-only
                               config hash (PropRules, PropConfigError) (ADR-49)
edgelab/prop/simulator.py      account replay over a stored trade stream: AccountStatus enum, trading-day
                               labels, end-of-trade / intra-trade-bound detection, multi-account,
                               deterministic simulation ids (PropDataError when records are insufficient)
edgelab/prop/service.py        read-only run loading, trades-hash verification, lineage, labels,
                               <data>/prop_simulations/PROP_*.json storage
edgelab/services.py            + prop_configs, validate_prop_config, prop_simulate, list_prop_simulations,
                               get_prop_simulation; Services.data_root attribute
edgelab/cli.py                 + `prop configs|list|validate|simulate|show`
edgelab/web/app.py             + /api/prop/configs, /validate, /simulate, /simulations[/<id>];
                               PropConfigError -> 422 prop_config, PropDataError -> 422 prop_data
web/src/pages/Prop.tsx         Prop Simulation page (run + accounts + rule sets -> outcome, violations,
                               progression; strategy result shown separately)
web/src/pages/Data.tsx         + Eligible column (existing backtest_readiness reasons)
configs/prop/*.yaml            SYNTHETIC TEST-ONLY example rule sets (outside the research config hash)
prop_smoke_real.py             local read-only smoke test on a stored real run
PROP_SIMULATION.md, DESKTOP_PACKAGING.md
```

## Decision records

### ADR-1 Storage backend
- **Problem:** Spec prefers DuckDB + Parquet; the build environment has no network and
  neither duckdb nor pyarrow installed.
- **Options:** (a) block until installable; (b) SQLite only; (c) interface with both backends.
- **Chosen:** (c) `ResultStore` interface; `DuckDBStore` (Parquet written by DuckDB itself,
  so pyarrow isn't required) is auto-selected when installed; `SQLiteStore` otherwise.
- **Why:** keeps the target architecture while shipping a working, tested system now.
- **Tradeoffs:** the DuckDB backend is **untested in this sandbox**. The same contract test
  class runs against it automatically once `pip install duckdb` succeeds. SQLite is slower
  for large analytical scans; fine at Phase 1 scale.

### ADR-2 Timestamp convention
- **Problem:** vendors stamp bars at open *or* close, in assorted timezones. Mixing them
  silently shifts signals one bar into the future.
- **Chosen:** internal `ts` = bar **open**, UTC, **nanosecond** unit. A bar is known at
  `ts + timeframe`. CSV import converts close-stamped data explicitly; tz-naive input is
  rejected. pandas 3 defaults to microsecond resolution, so all conversions go through
  `to_utc_ns`.
- **Tradeoffs:** one conversion step on import, in exchange for a single unambiguous rule.

### ADR-3 Validation gate and cleaning policy
- **Chosen:** the backtester accepts only `ValidatedDataset` (constructed via
  `validate_and_freeze`; arrays are read-only; hash re-verified before every run).
  Cleaning may sort and drop *exact* duplicates only. Missing bars are counted, never filled;
  conflicting duplicates and impossible OHLC are fatal.
- **Why:** fabricated bars create fabricated trades.
- **Tradeoffs:** some real feeds will need explicit handling (e.g. holiday lists) before passing.

### ADR-4 Lookahead prevention
- **Problem:** arbitrary Python can peek forward (`shift(-1)`, centred windows, full-sample
  normalisation) and no static rule can prevent it.
- **Chosen:** two layers. (1) Structural: signals are decided at bar close; the engine's
  earliest fill is the next bar. (2) Empirical: `check_causality` recomputes signals on
  ~20 random truncations of history; any change in the overlapping region is lookahead,
  and the backtest refuses to run.
- **Tradeoffs:** ~20× signal-generation cost per strategy (cheap relative to simulation);
  a peek that never changes a signal on any tested cut could slip through. Tests show it
  catches next-bar peeks, full-sample z-scores and centred windows.

### ADR-5 Fill model and same-bar conflicts
- **Chosen:** explicit, configurable rules (see `engine/fills.py` docstring): market at next
  open; stops fill at the level or at a worse gap open; target gaps fill at the target by
  default. Same-bar stop+target: `conservative` | `optimistic` | `intrabar` (replay LTF bars;
  falls back when LTF is missing, inconsistent with the HTF bar, or itself ambiguous).
  On the fill bar of a stop/limit entry, a touch counts as certain only if price had to
  cross the entry level to reach it; otherwise it is a conflict.
- **Why:** these ambiguities are where backtests silently flatter themselves. Every trade
  records which rule decided it; every run records requested *and* effective policy.
- **Tradeoffs:** conservative defaults understate performance slightly; tests prove
  `conservative ≤ intrabar ≤ optimistic` trade-by-trade.

### ADR-6 R unit and cost accounting
- **Chosen:** 1R = |fill − stop| × point value × contracts at the theoretical fill. Fills
  are triggered by theoretical levels; slippage adjusts prices afterwards. Therefore
  `net_R(k× costs) = gross_R − k·cost_R(1×)` exactly, and the 0.5×–3× sensitivity table
  plus breakeven multiple need no re-run (verified by test against full re-runs).
- **Tradeoffs:** assumes costs never change *whether* an order fills, which is true for
  this bar-level model but not for queue-position effects on limits (future work).

### ADR-7 Simulation loop
- **Options:** fully vectorised (fast, can't express position state / pending orders);
  classic event loop over every bar (slow in Python); per-signal loop with vectorised
  exit search.
- **Chosen:** per-signal loop; exit search scans geometrically growing NumPy chunks for
  the first stop/target/flatten event, then resolves that one bar with scalar rules.
  The kernel is pure functions over arrays, ready for numba.
- **Result:** ~100 ms per variant on 35k 5m bars (≈11k trades/s) single-core, no numba.

### ADR-8 Calendars without holiday data
- **Chosen:** session model per trading date (open possibly on the previous day), local-time
  rules via zoneinfo so DST is handled by construction. Holidays/early closes are
  configured, not bundled; unlisted closures surface as `MISSING_DAY` warnings.
- **Tradeoffs:** users must maintain holiday lists (or install `exchange_calendars` later).

### ADR-9 Package layout and tests
- **Chosen:** one importable package `edgelab/` mirroring the spec's areas (data, engine,
  research, prop, …) instead of many top-level packages, which avoids import-path hacks.
  Tests use stdlib `unittest` (pytest unavailable offline; pytest collects them unchanged).

### ADR-10 Strategy identity
- **Chosen:** `strategy_id = FAMILY_<sha256(spec)[:10]>` where spec = family + params +
  order. Identical configurations get identical IDs; any change gets a new ID. Run records
  carry `parent_strategy_id` and `mutation` for lineage.

### ADR-11 CFD datasets: generic import, content-addressed identity (Phase 2)
- **Problem:** CFD history comes from many brokers/vendors in different layouts, clocks and
  price bases; feeds of "the same" index are not interchangeable and must never be merged.
- **Options:** per-vendor adapters; one generic importer driven by options/presets; accept a
  single canonical CSV only.
- **Chosen:** one generic importer (`data/importer.py`) driven by explicit options, with
  layout-only presets in `import_profiles.yaml`. Dataset id = `<NAME>_<TF>_<content hash[:10]>`;
  the manifest carries provider, asset type, symbol, source timezone and convention, file sha256,
  volume type, spread source, price basis, calendar fingerprint, import version, parent/derivation.
- **Why:** no vendor code to maintain; a content-addressed id makes re-import idempotent and makes
  overwriting impossible; identical content under two names is kept separate and flagged.
- **Tradeoffs:** users must state timezone, convention and price basis explicitly (deliberate:
  guessing timestamps is the most common silent data error). Broker-server clocks are expressed
  as `IANA+Nh` rather than auto-detected.

### ADR-12 Timezone and timestamp normalization at import (Phase 2)
- **Problem:** naive timestamps, broker server time (often New York + 7h), close-stamped bars.
- **Chosen:** `source_timezone` is mandatory; naive local times are localized with
  `ambiguous/nonexistent = raise`; close-stamped bars shift by one timeframe; output is the
  Phase 1 convention (bar OPEN, UTC, ns). Validation's session/grid checks catch a wrong convention.
- **Why:** a wrong hour shifts every session feature; refusing is safer than guessing.
- **Tradeoffs:** a feed with bars inside a repeated DST hour must be exported in UTC or with offsets.

### ADR-13 Feature identity and persistent cache (Phase 2)
- **Problem:** thousands of variants must reuse features, and a stale cache is a silent error.
- **Options:** recompute per variant; cache by (feature, params); cache by full input identity.
- **Chosen:** spec identity = {id, version, timeframe, normalized params}. Cache key = sha256 of
  dataset id + content hash, base and computed timeframe, calendar fingerprint, volume type, spec,
  **implementation hash of the feature's source**, resolved session definitions, and dependency
  keys. Storage: `<root>/<dataset>/<key[:2]>/<key>.npz` + json, atomic writes, sha256 re-verified on
  load (corrupt entries are discarded and recomputed), in-memory LRU, per-engine memo.
- **Why:** any change to data, calendar, session window, parameters, version, code or a
  dependency produces a new key; identical inputs give bit-identical arrays.
- **Tradeoffs:** a comment-only code change also invalidates (safe, costs one recompute); no
  size-based eviction on disk yet.

### ADR-14 Multi-timeframe without unfinished candles (Phase 2)
- **Problem:** resample-then-forward-fill exposes the final value of an unfinished HTF bar.
- **Chosen:** HTF bars are built by the Phase 1 session-anchored resampler; each has an
  *effective close* = min(open + timeframe, session close). Base bar t sees HTF bar k only if
  effective_close[k] <= close[t]. Effective closes are cached.
- **Why:** provably causal (truncation-tested); a partial last bucket of the session is exposed
  at the session close rather than 15 minutes into the next session.
- **Tradeoffs:** HTF event outputs (e.g. `bos_up`) persist on base bars until the next HTF close.

### ADR-15 Session engine (Phase 2)
- **Problem:** "NY 09:00-10:00" must stay 09:00-10:00 New York time across DST, and London/New
  York change DST on different dates.
- **Chosen:** named windows in `sessions.yaml`, each with its own IANA zone; membership by bar
  OPEN on the local wall clock; instance = local start date; midnight-wrapping windows supported;
  "previous session" values exposed only after the SCHEDULED end (DST gap -> shift forward,
  ambiguous -> the later instant).
- **Why:** local-time semantics match how traders define sessions; scheduled-end exposure avoids
  leaking a session's range early.
- **Tradeoffs:** HTF bars straddling a window edge belong to the window they open in.

### ADR-16 Feature causality policy (Phase 2)
- **Chosen:** every registered feature must be causal (`known_at` bar_open/bar_close); raw
  forward-looking labels (e.g. an unconfirmed pivot) are never exposed. A truncation checker
  recomputes each feature on prefixes with an uncached engine; the test suite also proves the
  checker fails on deliberately leaky code (centered windows, future shifts, full-sample
  normalization, final session ranges, unfinished HTF candles). Feature strategies pass through
  the Phase 1 strategy causality check end to end.

### ADR-17 CFD cost profiles: refuse rather than invent (Phase 2)
- **Problem:** CFD costs (spread, commission, financing, contract size) are broker-specific.
- **Chosen:** cost profiles carry `status` (assumed / broker_verified / unconfigured); CFD profiles
  ship unconfigured and raise `CostConfigError`. Per-provider overrides; `spread_source: dataset`
  charges the average entry/exit bar spread once per round trip; slippage in ticks or points;
  overnight financing by rollover instants with a configurable triple day. Every run records the
  cost status.
- **Why:** an invented spread silently decides whether a CFD strategy "works".
- **Tradeoffs:** CFD backtests do nothing until the user enters numbers (intended).

### ADR-18 Dataset comparison without merging (Phase 2)
- **Chosen:** `run_across_datasets` binds the same strategy (same strategy_id) to each dataset's
  own calendar, costs and feature cache and reports runs side by side with provenance; identical
  content, differing periods and differing timeframes are flagged; a failure on one dataset is
  recorded, not hidden. `compare_feeds` reports shared bars, basis and return correlation. No
  averaged or concatenated dataset exists anywhere in the API.

### ADR-19 Service layer as the UI contract (Phase 2)
- **Chosen:** `edgelab/services.py` returns strict-JSON dicts (tested with `allow_nan=False`) for the
  Data Center, Feature Lab and Research Configuration; the CLI is a thin client of it, so a web
  API later exposes the same calls. No UI was built in Phase 2.

### ADR-20 Strategies are data, compiled into the existing interfaces (Phase 3)
- **Problem:** strategies must be serializable, versioned, hashable and AI-proposable, yet run on
  the Phase 1 engine without a second execution path.
- **Options:** (a) Python strategy classes; (b) an expression language evaluated by the DSL;
  (c) a declarative document compiled into `FeatureStrategy` + `OrderSpec` + `SignalSet`.
- **Chosen:** (c). The compiler maps every operand onto a Phase 2 feature output, a bar field or a
  constant, and every rule onto existing order/exit/sizing contracts.
- **Why:** one engine, one causality check and one feature cache. The compiler contains no
  indicator math, so nothing is computed twice or differently.
- **Tradeoffs:** anything not expressible as a feature needs a new feature (with docs and causality
  tests) rather than an inline formula. Only `add/sub/mul/div` are available inline.

### ADR-21 Signal exits as an opt-in engine extension (Phase 3; Phase 1 engine modified)
- **Problem:** signal exits were required, but the engine only supported stop, target, time, max-hold,
  session and end-of-data exits.
- **Options:** (a) refuse signal exits in the DSL; (b) emulate them in the compiler (impossible:
  exits depend on the fill, which only the engine knows); (c) extend the engine minimally.
- **Chosen:** (c). `SignalSet` gains optional `exit_long`/`exit_short` bool arrays. A flag at
  bar k exits at the open of k+1 as a market order (reason `SIGNAL`). Earlier exits win, and a gap
  through the stop or target at that open uses the existing `_gap` policy.
- **Why:** the same timing rule as entries (decide at the close, act at the next open), so there is no
  new lookahead surface. The causality check compares the exit arrays too.
- **Tradeoffs:** a Phase 1 module changed. Absent arrays leave behaviour unchanged: all 219
  Phase 1/2 tests and the Phase 1 demo give identical results. A signal exit cannot fill at the signal
  bar's close (market-on-close), by design.

### ADR-22 Three-valued (Kleene) condition logic (Phase 3)
- **Problem:** with NumPy booleans, NaN comparisons are False, so `not(x > ema)` is True during warm-up
  and `x != y` is True for NaN. Both would fire on data that does not exist yet.
- **Chosen:** each condition evaluates to (value, known). `all/any/not` follow Kleene logic, and
  signals fire only where the condition is known-true.
- **Tradeoffs:** `any[unknown, true]` fires (correctly) while `all[unknown, true]` does not. Users
  must read unknown as "no signal".

### ADR-23 Two hashes: logic identity vs document identity (Phase 3)
- **Problem:** reordered keys, renamed strategies or `$param` vs literal must not create "new"
  strategies (that would inflate the count of strategies tested), but every real rule change must.
- **Chosen:** `logic_hash` covers the canonical *resolved* logic, including referenced session
  definitions, and gives the `strategy_id`. `definition_hash` covers the whole canonical document.
  Canonicalization flips `<`, rewrites `crosses_below`, sorts `all/any`, fills feature defaults and
  pins feature versions.
- **Tradeoffs:** equivalence detection is syntactic plus a few algebraic rules, not full logical
  equivalence (e.g. `x > 5 and x > 3` is not reduced to `x > 5`).

### ADR-24 File-based strategy library with append-only lineage (Phase 3)
- **Problem:** instances, their parents, exact changes and generating batches must be durable and
  inspectable, without committing to a database schema before Phase 4.
- **Chosen:** one JSON file per instance (canonical definition plus lineage records), one per batch,
  written atomically. The same logic from two parents keeps both records.
- **Tradeoffs:** `list`/`children` scan the directory (fine for thousands). Phase 4 added a derived
  index for listings and batch membership (ADR-37); `children`/`ancestry` still scan.

### ADR-25 Mode A varies only declared parameters inside declared domains (Phase 3)
- **Problem:** "controlled variations" must not become unbounded data mining, and the count of
  generated instances must be honest.
- **Chosen:** the base strategy declares domains, and the variation spec can only select inside them.
  The count is checked before generation (loud failure), random sampling requires a seed, any invalid
  child fails the batch, and logic duplicates are removed and reported. Batch ids are
  content-derived.
- **Tradeoffs:** structural variations must be parameterized in the base strategy (`enabled: $flag`,
  `choice` sessions, `timeframe` parameters).

### ADR-26 Mode B is an interface plus a gate, with no AI calls (Phase 3)
- **Problem:** AI proposals must never carry or imply performance, and must never bypass validation.
- **Chosen:** a strict proposal schema (unknown keys rejected, so there is no place for performance
  numbers), claim-language rejection, the same validator and compiler, duplicate and structure checks,
  and a `StrategyProposer` protocol with a `StaticProposer`.
- **Tradeoffs:** claim detection is a pattern list and can miss creative phrasing, but the schema, not
  the pattern list, is the primary barrier. Proposal quality ("genuinely different") beyond hashes
  needs human review.

### ADR-27 Web stack: React + TypeScript bundled with esbuild, served by the Python backend (Phase 3.5)
- **Problem:** a real GUI was required, preferably React + TypeScript + Vite, but the build environment had no network access.
- **Options:** (a) Vite (not installable offline); (b) dependency-free vanilla JS; (c) React + TypeScript bundled by esbuild (available offline; it is also the bundler Vite uses internally).
- **Chosen:** (c). The built bundle is committed under `edgelab/web/static`, so running the app needs only Python. `build.mjs` records a source hash that a Python test checks for staleness. A small React type shim replaces `@types/react`, which was unavailable offline.
- **Tradeoffs:** there is no Vite dev server or hot module reloading (`npm run watch` rebuilds instead), and the typings are narrower until `@types/react` is installed.

### ADR-28 One strategy format: the UI edits the DSL document; the backend renders and judges it
- **Problem:** a visual builder tempts a second, UI-specific model with its own serializer and validation rules.
- **Chosen:** the builder's state is the DSL document itself. `/api/strategies/render` (debounced on every edit) returns the backend YAML, validation issues, the canonical form and identity. Feature pickers, operators, parameter types, valid higher-timeframe multiples, sessions and unsupported concepts all come from `/api/options`.
- **Why:** there is exactly one source of truth for validity and identity, the same as the CLI.
- **Tradeoffs:** live validation costs one small request per edit burst (around 300 ms debounce).

### ADR-29 Thin HTTP layer with data-only inputs
- **Problem:** the service layer accepts file paths and YAML text, which is convenient for the CLI but unsafe from a browser.
- **Chosen:** HTTP accepts only JSON objects or well-formed IDs. Imports read only from configured folders, with whitelisted options. YAML parsing is a separate parse-only endpoint (`yaml.safe_load`). The server binds to loopback unless `--allow-remote` is given. Errors map to stable kinds, with tracebacks only in `details`.
- **Tradeoffs:** there is no authentication, so the app is a local single-user tool, as documented.

### ADR-30 SQLite connection usable from the web server's worker threads (Phase 1 store modified)
- **Problem:** the threaded web server handles requests on worker threads, and `sqlite3` refuses a connection created on another thread. The browser tests found this: `/api/status` returned 500 on a real server.
- **Options:** (a) run the server single-threaded, so a long backtest would block even health checks; (b) open one connection per request; (c) keep one connection, allow cross-thread use, and serialize access.
- **Chosen:** (c). `check_same_thread=False`, with every service call behind one lock in the API layer.
- **Tradeoffs:** requests are serialized. CLI and test behaviour is unchanged: the Phase 1 demo gives identical results and all 298 prior tests pass.

### ADR-31 Demo workspace is a separate root; single backtests use the existing run registry
- **Problem:** a demo must show the whole flow without fabricating data and without mixing with real research results.
- **Chosen:** `--demo` creates a separate root, marked by `DEMO_WORKSPACE`. Synthetic bars go through the normal import pipeline with `SYNTHETIC*` providers, which the system flags everywhere. Single backtests are recorded by the Phase 1 run registry (status `IN_SAMPLE`, notes marking synthetic data), and the Results page lists synthetic runs separately.
- **Tradeoffs:** a second workspace directory. No new results store was added.

### ADR-32 Search and cell identity (Phase 4)
- **Problem:** a search must be reproducible and resumable, and its cells must never be confused
  across datasets or configurations.
- **Chosen:** `search_hash` covers the canonical spec (sorted, de-duplicated sources and datasets,
  period in UTC, seed, spec version) plus the execution `config_hash`; ranking settings, `max_cells`
  and `workers` are excluded (analysis of stored results, a safety cap, and something that never
  changes results). `search_id = SRCH_<hash[:12]>`. `cell_id` covers strategy_id, **dataset_id**,
  dataset content hash, config_hash and search_hash: two datasets may hold identical bars (a futures
  and a CFD import of one file) yet differ in instrument, costs and calendar.
- **Consequences:** a different config is a different search, never a resume of the old one. The
  same spec and config resume the same search.

### ADR-33 One cell pipeline for single backtests and searches (Phase 3 services modified)
- **Problem:** batch search must use exactly the numerical path of a single backtest.
- **Chosen:** `Services._run_cell` is the body of `backtest_strategy` (load and re-validate ->
  optional `restrict_to_period` -> compile -> costs with the CFD refusal -> bind -> causality-checked
  `run_backtest(sizing=strat.sizing)` -> metrics), with the run-record step as `_record_cell`.
  `backtest_strategy` calls it; `_dataset_eligibility` is the per-dataset body of
  `backtest_readiness`. Synthetic data is always labelled first in run notes.
  `run_search(workers=None)` takes the spec's worker count.
- **Verified:** `backtest_strategy` and `backtest_readiness` output unchanged (snapshot comparison
  and tests); a search cell's trades_hash equals the single backtest's.

### ADR-34 Search storage: additive SQLite tables (Phase 1 store modified)
- **Problem:** durable batch and cell state without a second results store.
- **Chosen:** `search_batches` and `search_cells` created with `CREATE TABLE IF NOT EXISTS` in
  `SQLiteStore` only; cells reference `run_id`s in the unchanged, authoritative `runs` table. A
  `current` flag separates the latest plan's cells from historical rows (never deleted).
  `DuckDBStore` refuses every search accessor (`SearchStorageUnsupported`).
- **Tradeoffs:** research requires the SQLite store. With `storage.backend: auto`, installing
  DuckDB switches the store and research then refuses (see Phase 4 limitations).

### ADR-35 Honest trial accounting (Phase 4)
- **Chosen:** a trial is an eligible cell actually evaluated (completed, including zero trades, or
  failed); ineligible cells are reported but are not trials; a completed cell skipped on resume is
  not a new trial; duplicate strategy references count once. Cumulative totals come from the
  current plan's stored cells, so a retried cell is one trial.
- **Tradeoffs:** trials are counted per search, not across searches (Phase 4 limitations).

### ADR-36 Ranking is an in-sample view; a shortlist is a tag (Phase 4)
- **Chosen:** rank completed current cells by expectancy_r (default), profit_factor or net_r;
  win rate is refused; a minimum sample label filters; ties by strategy_id then cell_id. A +inf
  profit factor (no losing trade, stored as null) is recognised from the stored loss_rate and net_r
  and flagged, never replaced by a number. Every output states the trial count, `in_sample: true`
  and `validated: false`. A shortlist is stored on the batch row only and changes no run status;
  promotion belongs to Phase 6.

### ADR-37 Derived library index and Mode B batch records (Phase 3 lineage modified)
- **Chosen:** `<library>/index.json` holds listing rows, batch summaries and batch membership, with
  a fingerprint of the library files (name, size, mtime, inode) and a checksum of its own payload;
  a missing, stale, corrupted or foreign-version index is rebuilt from the directory scan, which
  stays authoritative (`verify_index`). `ingest_proposals(save=True)` stores the proposal batch
  record (`kind: proposal`); variation records keep their format; `list_batches()` still returns
  variation batches only by default.
- **Tradeoffs:** any write makes the next read rescan; an in-place edit that keeps size, mtime and
  inode is caught only by `verify_index`.

### ADR-38 Background jobs share the one service lock (Phase 3.5 web app modified)
- **Problem:** long searches must not freeze the web app, and the SQLite connection must still be
  serialized (ADR-30).
- **Chosen:** `Services.lock` is the single service lock; `create_app` uses it instead of creating
  its own. The `JobManager` (one worker thread, one active job) takes it only around short store and
  library operations, never during a backtest, so status and cancel stay responsive. Cancellation is
  checked between cells. Creating a `JobManager` (at web start-up, SQLite only) marks batches left
  `running` as `interrupted`; they are resumed only explicitly.
- **Tradeoffs:** job ids and state live in the server process; reconciliation assumes one process.

### ADR-39 FeatureCache safe under threads and processes (Phase 2 cache modified)
- **Problem:** a job cell and a web request, or several worker processes, may use one cache.
- **Chosen:** a private lock guards the in-memory LRU and stats (lookup + move is one step); no
  I/O happens under it. Metadata and arrays are each written to a unique temp file and moved into
  place with `os.replace`, metadata first, so a reader that finds an `.npz` finds its metadata.
  Keys, file format, verification and API are unchanged.
- **Tradeoffs:** two simultaneous misses compute the same (deterministic) entry twice.

### ADR-40 Process-parallel search with parent-only writes (Phase 4)
- **Chosen:** `workers > 1` runs cells in spawned (never forked) processes. The parent loads and
  re-validates datasets once and reads definitions and lineage; each worker gets a read-only context
  with no store and calls the unmodified `_run_cell` with `record=False`. The parent submits in plan
  order (at most `workers` in flight), consumes results strictly in plan order and records them with
  `_record_cell`, so run ids and rows never depend on completion order. Workers share the disk
  feature cache (ADR-39). A crashed worker fails only the cells it did not return.
- **Verified:** `workers=1` and `workers=N` give identical cells and trades hashes; the worker count
  is not part of the search identity.
- **Tradeoffs:** each worker receives a pickled copy of the datasets; process start-up makes tiny
  searches slower in parallel.

### ADR-41 Cross-dataset warnings shared by comparison and search (Phase 2 compare modified)
- **Chosen:** the identical-content, mixed-timeframe and different-period warnings of
  `run_across_datasets` were extracted into `comparison_warnings(manifests, check_periods=True)` and
  are reused by the planner (the period warning is suppressed when a period is imposed).
  `run_across_datasets` output is unchanged; it is not used by batch search (it sizes with the
  config default, not the strategy's own sizing).

### ADR-42 Explicit UTC offsets may differ within one file (Phase 2 importer modified)
- **Problem:** the first real CFD export mixed `...Z` and `...-04:00` timestamps. `pd.to_datetime`
  refuses mixed offsets ("Mixed timezones detected"), so the file could not even be inspected.
- **Options:** (a) `utc=True` always (would silently treat naive values as UTC: a guess);
  (b) require the user to rewrite the file; (c) convert only when every value states its offset.
- **Chosen:** (c). In `parse_timestamps`, if every non-blank value ends in `Z` or `+/-HH[:MM]` after a
  time, the values are absolute instants and are parsed with `utc=True` (exact conversion, bar-open
  semantics and nanosecond resolution unchanged). Naive values still need `source_timezone`; a mix
  of explicit and naive values is refused; malformed values still fail. A `+Nh` server-clock shift
  on explicit offsets is still refused. All other import behaviour is unchanged.
- **Verified:** a 1m week across US DST written with alternating `Z` / `-05:00` / `-04:00` stamps
  imports to exactly the original UTC nanoseconds; the prior importer tests pass unchanged.

### ADR-43 HistData NSXUSD research proxy: own instrument and measured-schedule calendars (config only)
- **Problem:** HistData NSXUSD M1 prices sit on a 0.001 grid (NAS100_CFD uses 0.01, which drives
  rounding, limit penetration and tick slippage); its session schedule is neither CME nor FX hours;
  HistData documents fixed EST without DST, but FOMC 14:00 ET releases land at 14:00 in the file in
  summer 2019-2024 (New York wall-clock behaviour).
- **Chosen:** config additions only. Instrument `NAS100_HISTDATA` (tick 0.001, CFD unit convention,
  `research_proxy: true`; not a tradable contract); cost profile `NAS100_HISTDATA` ships
  `unconfigured`, so the engine refuses to run it until real costs are entered. Calendars
  `HISTDATA_NSX_R1` (2017-2018, Sun 18:00 - Fri 17:00) and `HISTDATA_NSX_R2` (2019+, Sun 18:00 -
  Fri 16:15) in `America/New_York`, the empirical reading; the documentation conflict is recorded
  in DATA_IMPORT.md, not resolved. No holidays listed (not established). NAS100_CFD, existing
  calendars, importer, validation and thresholds are unchanged; the config hash changes.
- **Not solved (deliberately):** the source adds 17:00-17:59 NY bars on runs of days around US DST
  transitions. They stay `bars_outside_session` FAILs. The architecture cannot represent disjoint
  source-quality exclusions: `restrict_to_period` is one contiguous window on an already-frozen
  dataset, and the raw FAIL stops `validate_and_freeze` before any derivation exists. Admitting
  these years needs a new, explicit, audited import-time exclusion (not implemented).

### ADR-44 Audited source-quality exclusion windows at import (Phase 2 importer extended)
- **Problem:** HistData NSXUSD 2019 delivers 1,197 extra bars at 17:00-17:59 New York on Sun-Thu
  evenings of 2019-03-10..03-28 and 2019-10-27..10-31 (the weeks when US and EU DST differ). They
  fail `bars_outside_session` (0.351% > 0.1%), so the year cannot be imported (ADR-43).
- **Options:** (a) raise the threshold or add an "ignore outside session" switch (weakens the
  DST/timezone-error detector for every dataset); (b) a calendar that opens at 17:00 on those dates
  (the model has no per-date opens, and it would declare the anomaly a session); (c) drop the bars
  in a pre-processed copy (invisible to lineage); (d) explicit, named exclusion windows applied at
  import and recorded in the manifest.
- **Chosen:** (d). `edgelab/data/exclusions.py`; sets live in `configs/data.yaml` under
  `source_exclusions.<NAME>` (so they are in the config hash) and an import opts in with
  `ImportOptions.source_exclusions` / `--source-exclusions NAME`. Windows are half-open
  `[start, end)` bar-open instants written as quoted strings with an explicit UTC offset, each with a
  reason. Refused, with nothing stored: unknown set; missing/extra keys; empty reason; naive,
  unquoted or unparseable timestamps; start >= end; overlapping windows (never merged; touching
  half-open windows are allowed); a window that matches no bar; a window containing ANY bar the
  import calendar puts inside a session. So it can only remove bars `bars_outside_session` already
  flags, where someone wrote down why. The exclusion runs after normalization and before
  `validate_and_freeze`, which validates the retained bars with unchanged thresholds.
- **Provenance:** `manifest.source_detail.source_exclusions` = set name, set hash, description,
  calendar, rows before / excluded / after, each window (as written, in UTC, reason, rows removed)
  and the SHA-256 of the removed rows; `manifest.derivation` names the set and counts; the source
  file hash is unchanged and the source file is never written. Derived timeframes carry the same
  record. Removing rows changes the content hash, hence the dataset id, so results on an excluded
  dataset cannot share an id with the unmodified source. `restrict_to_period` children keep the
  parent id (`restricted_from`) but do not copy the record.
- **Shipped sets:** year-specific audited sets for 2019, 2020, 2021, 2022 and 2024
  (`HISTDATA_NSXUSD_<YEAR>`: 20, 20, 15, 15 and 17 one-hour 17:00-18:00 EDT windows), one per
  evening on which the local outside-session trace of the real file found bars; evenings without
  such bars (e.g. 2024-10-28/29) have no window, and partial hours are not assumed complete. The
  dates are measured evidence, not a vendor statement. 2023 has no set and stays coverage-rejected.
  2018's 131 sparse outside-session bars stay a WARN and are not excluded.

### ADR-45 Reloading a dataset reproduces its stored manifest (Phase 2 `load_validated` fixed)
- **Problem:** `load_validated` re-runs `validate_and_freeze` on the stored bars. Those bars were
  sorted and de-duplicated at import, so the re-run found nothing to clean and overwrote the stored
  import-time facts (`source_detail.cleaning`, `source_detail.raw_duplicate_bars`,
  `duplicate_bars`) with empty/zero values. Any dataset whose source needed cleaning therefore
  reloaded with a different manifest hash than the stored one (found on the real HistData
  2019-2024 imports, which carry a 60-row exact-copy block; synthetic fixtures had none).
  `compare_feeds` provenance records `manifest_hash()` of reloaded datasets, so it was affected.
- **Chosen:** after the unchanged re-validation and content-hash check, `load_validated` keeps the
  stored `source_detail` and `duplicate_bars` (facts about the raw source, not the stored bars).
  Everything that re-validation legitimately decides (quality status, missing bars, the report)
  still comes from the re-run, so a changed config or calendar still shows up. Stored manifests
  were always correct; no re-import is needed.
- **Verified:** regression test (source with a repeated exact-copy block: reloaded manifest and hash
  equal the stored ones); prior import/store/compare/research tests pass; the Phase 1 demo results
  are unchanged (only the code hash, timings and artifact paths differ).

### ADR-46 Phase 5 research analytics are selections over stored trades, not new metrics
- **Problem:** runs were only measurable one at a time; cost sensitivity and the break-even cost
  multiple existed but no report used them; nothing aggregated a fixed strategy across datasets.
- **Chosen:** `analytics/research.py` only selects, orders and labels trades, and calls the Phase 1
  functions (`compute_metrics`, `cost_sensitivity`, `breakeven_cost_multiplier`) on each subset.
  Pooled figures are ONE measurement of all trades in entry-time order (drawdowns and streaks run
  across dataset boundaries), never averages of per-dataset figures. Stability is counts and spread
  of per-dataset figures; no composite score. Sessions are the canonical `configs/sessions.yaml`
  windows by entry time (they overlap; rows do not sum). Cost sensitivity uses
  `backtest.cost_sensitivity_multipliers` unchanged. `Services.research_report(run_ids)` reads stored
  runs only, refuses mixed strategies and repeated datasets, and attaches caveat labels from what the
  runs recorded (cost status and profile, price basis, research-proxy instruments) plus a small
  documented table of standing notes (HistData CFD BID proxy, MNQ-equivalent assumed costs, 2023
  excluded).
- **Deferred:** UI page, Monte Carlo, walk-forward/OOS, distributions/plots, weekday/month
  breakdowns.

### ADR-47 Validation of FIXED strategies: time windows over one dataset, seeded trade resampling
- **Problem:** nothing split data in time, walk-forward was not representable, run statuses
  `OUT_OF_SAMPLE` / `WALK_FORWARD` existed but were never set, and there was no Monte Carlo.
- **Chosen:** windows are UTC bar-open ranges `[start, end]`, consecutive windows 1 ns apart (no bar
  in two windows). Each window runs through the existing `_run_cell(period=...)` path, i.e. on a
  re-validated `restrict_to_period` dataset linked to its parent, so indicators restart at the window
  start (causal; early bars may give no signals) and no position crosses a boundary. The strategy
  document is frozen (JSON-canonical copy + hash, re-checked after every window) and every window must
  compile to one `strategy_id`. OOS: `[start, split)` = train (`IN_SAMPLE`), `[split, end]` =
  `OUT_OF_SAMPLE`. Walk-forward: consecutive non-overlapping test windows (`WALK_FORWARD`, step = test
  length) each preceded by a rolling or anchored train window; nothing is re-fitted. Runs are
  recorded only on request, notes carry a `VAL_...` id. Monte Carlo resamples OBSERVED per-trade R with
  `numpy.random.default_rng(seed)`: `bootstrap` (with replacement; i.i.d. assumption, not established)
  and `shuffle` (permutation; total R fixed, tests drawdown sequence dependence). It is not a market
  simulation. With a fixed strategy a split measures stability across time, not a selection
  procedure; reports say so.
- **Deferred:** randomized-entry controls through research (`RandomEntry` exists only as a code
  strategy used by `scripts/phase1_demo.py`, with fixed-point stops, so it is not directly comparable
  to DSL strategies with feature-based stops); walk-forward across several stored datasets (windows are
  within one dataset; the HistData datasets are one per year); HTTP/UI for validation.

### ADR-48 Matched random-entry control (conditional null) for DSL candidates (Phase 3 compiler refactored)
- **Problem:** `RandomEntry` (Phase 1 demo) uses fixed-point stops and its own window, so it is not
  comparable to a DSL candidate with feature-based stops, sessions, cooldowns and signal exits.
- **Chosen:** `DSLStrategy.signals_from_features` was split, without behaviour change, into
  `_entry_directions` (trigger stage), `_entry_allowed` (session/weekday), `_orders` (entry reference,
  stop, target, validity for a given direction array), `_apply_cooldown` and `_emit`; all 8 fixtures
  give identical signals, levels, exits, diagnostics and ids before and after. `research/controls.RandomEntryControl` re-uses the
  candidate's compiled definition and replaces only the entry decision: per bar
  `default_rng(seed).random((n, 2))` (prefix-stable), a bar fires if it is eligible for the drawn
  direction and its draw is below p. Eligible = what the candidate could have entered at that bar's
  close (its session, weekdays, executable reference/stop/target). Both sides then pass the SAME
  stages: candidate trigger -> `_orders` -> `_apply_cooldown`; control random fire -> `_orders` ->
  `_apply_cooldown`. Calibration is at the pre-cooldown stage on both sides and uses pre-entry
  quantities only: p = candidate valid entries before cooldown / eligible bars (per realization), so
  expected pre-cooldown control fires equal the candidate's; P(long) = long share of those entries.
  (Method v1 calibrated to the candidate's post-cooldown count while applying cooldown after firing;
  controls of cooldown strategies under-fired, about 11% on `mtf_trend_filter`. Fixed as v2.)
  Post-cooldown signal counts and trade counts are reported, not forced: cooldown removes more of a
  clustered candidate's signals than of uniformly spread random ones, and the engine's
  one-position rule removes more again. Everything else is the candidate's: costs object, sizing,
  backtest config, engine and its causality check (which each realization passes). N realizations
  use `SeedSequence(seed).spawn(N)`. Control ids are `CTRL_...`; results are never stored as runs.
- **Interpretation:** a conditional null, conditioned on the candidate's pre-cooldown entry frequency
  and direction mix (whole-period design constants carrying no outcome information), not on trade
  outcomes. "Fraction of controls exceeding the candidate" is a descriptive rank, not a p-value. It
  does not test exits, sizing, cooldown or costs (identical on both sides); the candidate's
  clustering in time is part of what differs, since control fires are spread uniformly over
  eligible bars.

### ADR-49 Prop-account simulation replays stored trades above the engine (Phase 6)
- **Problem:** prop-firm rules (targets, static or trailing drawdown, daily loss, sizes, sessions)
  must be applied to strategies without turning the engine into a firm-specific simulator, without
  hard-coding any firm, and without claiming precision the trade records do not have.
- **Chosen:** a separate layer (`edgelab/prop/`) consumes the STORED trades of one run.
  - It verifies them against the run's `trades_hash` and replays them in (exit, entry, trade_no)
    order, per account, through a versioned rule set (YAML, `kind: edgelab.prop_rules`,
    `schema_version: 1`).
  - Trades are used exactly as recorded (size, `net_usd` after stated costs). Sizes are never
    clipped; an oversize trade is a recorded violation.
  - Two detection modes:
    - `end_of_trade`: closed balance after each exit.
    - `intratrade_bound`: additionally, before the exit, the conservative bound
      `balance - mae_points x (risk_usd / risk_points) - cost_usd`, from the engine's
      bar-resolution MAE.
  - The daily reset is explicit (timezone + time; days labelled by the date they end), and P&L is
    booked on the exit's day.
  - Same-trade conflicts are fixed rules: an intra-trade breach beats a target; both breaches are
    recorded, and the drawdown takes precedence in the status.
  - Results carry lineage (run, strategy, definition/logic hash as stored, dataset, period, cost
    profile/status, verified trades hash, rule-set ids and hashes, ordering, simulator/code
    version) and a deterministic `PROP_` id.
  - They are stored, on request, as their own JSON documents; the run table is never written.
    Each simulation re-reads the source afterwards and asserts it is unchanged.
- **Refused, not approximated** (the records lack the data):
  - trailing drawdown from intra-trade equity highs (it needs the order of MFE vs MAE);
  - intra-trade detection on trades crossing the reset (it needs the MAE's timestamp);
  - overlapping positions (they need a joint path);
  - payouts, news rules and weekend holding.
- **Rejected:**
  - Adding account rules to the backtester: this would couple research to one firm's semantics
    and alter the strategy result.
  - Re-simulating bars for intra-trade paths: that would be a second market simulator.
  - A composite "prop score".
- **Minor changes to earlier modules:** additive `Services` methods plus a `data_root` attribute;
  additive routes and error mappings in `web/app.py`; an additive CLI command; a dataset
  Eligible column showing the existing `backtest_readiness` reasons. No engine, DSL, store or
  metric change; prior tests unchanged.

### ADR-50 Desktop packaging: one runtime module, a build manifest for code identity (Phase 7)
- **Problem:** a PyInstaller build has no source files and no Git.
  - `code_version()` (source hash, git commit), `compiler_source_hash()` and
    `FeatureDef.impl_hash` (via `inspect.getsource`) would silently hash nothing, or raise.
  - Configs and static files were found relative to the repository, and data relative to the
    current directory.
  - A fixed port and multiprocessing `spawn` would break or conflict.
- **Chosen:**
  - `edgelab/runtime.py` is the only place that distinguishes development (repository) from packaged
    (`sys.frozen`, `sys._MEIPASS`). It resolves:
    - read-only bundled resources: package, static bundle, default configs, fixtures;
    - the persistent workspace: `%LOCALAPPDATA%\EdgeLab`, or `--data-root` / `EDGELAB_DATA_ROOT`.
      Default configs are copied into it once and never overwritten; differences are only
      reported.
  - The build writes `edgelab_build.json` from the REAL sources. It holds the source, compiler and
    per-feature implementation hashes (computed by the same functions as in development), plus the
    git commit and a `build_id` over the identity fields.
  - Frozen code reads these values and refuses without them. Packaged `code_version()` adds
    `packaged`, `app_version` and `build_id`; development output is byte-identical to before.
  - `edgelab/desktop.py` is the launcher:
    - `freeze_support()` first;
    - a single-instance lock per workspace;
    - 127.0.0.1 on an OS-assigned port;
    - readiness wait, then the browser;
    - clean shutdown: cancel/join a job, close SQLite;
    - errors to the log and a message box;
    - a `cli` pass-through.
  - SQLite by excluding DuckDB from the build, so `backend: auto` and the config hash stay
    identical.
- **Rejected:**
  - A webview or native UI framework (the browser suffices; no new UI stack).
  - onefile mode (slower start, temp extraction).
  - Forcing `backend: sqlite` via a config override (it would change the research config hash).
  - Bundling `.py` sources to keep runtime hashing (it hides that the running code is compiled,
    and is easy to desynchronise).
- **Minor changes to earlier modules:**
  - `core/identity.py`, `strategy/compiler.py` and `features/spec.py` read the manifest when frozen.
  - `core/config.py`, `web/app.py`, `web/bundle.py` and `web/__main__.py` resolve paths through
    `edgelab.runtime`.
  - `/api/status` adds `runtime`.
  - No research semantics changed. Verified: a frozen run and a development run give identical
    trades hash, config hash, strategy id and feature-cache keys.

### ADR-58 Versioned, verified application updates (packaged Windows app)
- **Problem:** the packaged app (PyInstaller folder) had no way to update itself; a running `EdgeLab.exe` cannot be
  replaced in place on Windows, and pulling source from a branch would bypass every release check.
- **Decision:** one authoritative version (`edgelab.__version__`, MAJOR.MINOR.PATCH) stamped into the UI bundle,
  build manifest and exe metadata, with builds refusing mismatches. Updates come only from GitHub Releases
  (`/releases/latest`, no drafts/pre-releases) carrying `edgelab-release.json` (schema `edgelab-release/1`: version,
  tag, platform, artifact name/size/SHA-256, notes, build) and the zipped app folder. `edgelab/updater/`:
  `core` (strict manifest validation), `source` (GitHub + local-folder sources behind an injectable transport;
  HTTPS to GitHub hosts only), `manager` (check / later / persistent skip / staged download → size + SHA-256 →
  safe extraction → version check → hand-off; never fatal, offline-safe), `apply` (separate helper process started
  from the staged, verified build: wait for exit, refuse non-app or workspace-bearing folders, copy → two-rename swap
  → relaunch → rollback on immediate failure; JSON-lines log), `service` (process-wide manager + `/api/version`,
  `/api/update/*`, also served by the workspace shell). `packaging/release.py` prepares (never publishes) the assets.
- **Safety:** downgrades and other-platform releases are never offered; nothing is executed or installed before the
  SHA-256 matches; the old installation is kept until the new one is in place; user data (workspaces, settings,
  staging) lives outside the install folder and folders overlapping it are refused. Automatic checks run only in the
  packaged app (development runs and tests never contact the network by themselves).
- **Limits:** no code signing (SHA-256 anchors trust in the GitHub release, not in a signing key); no delta updates;
  a helper killed between the two renames needs a manual rename (documented).
- **Changes to earlier modules (minimal, additive):** `edgelab/__init__.py` version 0.1.0 → 0.2.0 (single source);
  `desktop.py` dispatches `--apply-update` and configures the updater when frozen (the stop event is created before
  the server thread starts); `runtime.py` adds `update_cache_dir()` / `install_dir()`; `workspace_host.py` routes
  `/api/version` and `/api/update/*` to the shell; `web/bundle.py` treats a version mismatch as a stale bundle;
  `packaging/build.py` + `edgelab.spec` add the version checks and the Windows version resource.
- **Bug fix found by the offline update smoke test:** the launcher's own loopback calls (`wait_ready`, the
  second-launch focus request) went through `urllib`'s environment proxy, so a user with `HTTP(S)_PROXY` set could
  not start EdgeLab. They now use a no-proxy opener (`desktop._LOOPBACK`); regression test in `tests/test_desktop.py`.
  If the folder swap fails after EdgeLab has exited, the helper now relaunches the unchanged previous version.
- **Readiness handshake (audit fix):** "the new process is still alive after 8 s" was not proof of a working
  start: a windowed exe that fails at start-up blocks in a modal error dialog and stays alive. The helper now passes
  `EDGELAB_UPDATE_READY_FILE` to the relaunched app, which writes it only after its own `/api/health` answered
  (`desktop._report_ready`). Exited or not ready within 180 s -> the new process is stopped, kept as
  `<install>.failed-<stamp>`, the previous version restored and relaunched, and `update_failed` (step `restart`)
  is logged; `update_completed` is written only after the ready report.

### ADR-59 Research-terminal UI and read models
- **Problem:** the UI had no overview of protocol budgets, no server-side strategy search, no per-run analytics
  beyond Phase 5 tables and no presentation of controls, the candidate pipeline or simulated prop paths.
- **Decision:** `research/overview.py` adds READ-ONLY read models (overview, explorer with server-side
  filter/sort/paging, cross-run research dashboard, per-run analytics, per-strategy and board pipeline states)
  behind thin `Services` methods and GET routes; they reuse `compute_metrics`, the Phase-5 breakdowns, cost
  sensitivity and Monte Carlo and never evaluate, count a trial, touch a ledger or change a protocol (tests assert
  unchanged ledgers, runs and protocol hashes). Pipeline states are derived from stored facts only (library, runs,
  shortlist tags, holdout ledger); later stages (paper, human review) are reported "not implemented".
  A protocol holdout-evaluation run (stored `OUT_OF_SAMPLE`) is identified from the holdout ledger and always
  labelled Holdout, never counted as OOS nor pooled into aggregates. The React UI gets a dark design system, an SVG
  chart kit (palette validated for colour-vision deficiency) and new pages (Home, Research dashboard, Explorer with
  a detail drawer, Controls, Candidate pipeline, Paper placeholder, richer Results/Prop/Settings & About).
- **Minor additive change:** `Services.list_protocols` rows add `protocol_version`. Ad-hoc random-entry controls stay
  descriptive (a rank, no p-value; `test_random_control`); the UI shows an exact Monte-Carlo p-value only where the
  protocol stored one (holdout evaluations). No HTTP route creates, edits or retires a protocol or resets a ledger.
### ADR-63 MNQ whole-contract execution model (sizing audit)
- **Problem:** the engine sized and computed USD P&L on the DATASET's instrument. For the canonical Dukascopy index-CFD
  proxy that is a fractional "unit" model (point value 1, step 0.01), not whole Micro E-mini Nasdaq-100 contracts, and
  nothing tied the research target (MNQ, whole contracts) to the sizing.
- **Single specification:** the `MNQ` entry of `configs/instruments.yaml` (tick 0.25, tick value $0.50, point value $2 =
  tick value / tick size, min size 1, size step 1, i.e. whole contracts). It is the only place these numbers exist (a test scans
  the source tree). The file is unchanged by this ADR: editing it would change the research-config hash the active protocol
  is bound to. No instrument-level quantity cap is defined (none invented); the cap is the strategy's `max_quantity`.
- **Dataset vs contract:** the dataset identity stays `NQ_DUKASCOPY` (Dukascopy USATECH.IDX/USD, an index-CFD research
  proxy, never CME MNQ). A strategy declares `sizing.contract: MNQ` (strategy logic, part of its identity; strategies without it
  run exactly as before). The backtester builds an *execution view*: the data series' price grid, calendar and identity,
  with the contract's point value, minimum size and size step. Translation: 1 index point of the research series = 1 point of
  the contract = $2 per contract; prices are not snapped to the contract tick; P&L, risk and cost USD use that point value
  (the frozen Dukascopy cost scenario is per point / per notional, so it scales consistently). R-multiples, MFE/MAE in R
  and cost-in-R are independent of the contract, which is tested (quote-aware, long and short). No futures volume is
  derived from the Dukascopy volume field. A contract named without its spec, a different contract, or MNQ on another
  *futures* series (whose cost profile describes another contract) is refused, never silently sized on the data series.
- **One conversion for every risk mode:** `engine/sizing.contracts_for_risk` is the only place a dollar budget becomes a
  quantity: `contracts = floor(budget / (planned stop points x point value) / size_step) x size_step`, in exact decimal
  arithmetic (dollars at 1e-9: float noise such as 499.99999999999994 is removed, real shortfalls are not). It never rounds up
  (`contracts x risk per contract <= budget`), is maximal, returns 0 (trade rejected) when one contract does not fit, and
  applies the optional hard cap. `risk` (budget = `risk_usd`) and `equity_risk` (budget = `risk_pct`% of realised equity at
  the signal, ADR-62) both call it. Fixed contracts for a named contract must be whole (DSL and engine refuse fractions);
  `check_quantity` is a last guard before execution. Whole contracts are therefore guaranteed for every MNQ strategy.
- **Planned vs realised risk:** sizing happens at the signal and cannot know the fill, so the budget guarantee is for the
  PLANNED initial stop distance (signal-bar close or the order level to the stop). The realised initial risk at the fill (`risk_usd`)
  can differ by the entry gap (next open versus signal close). Contract runs add `planned_risk_usd` to each trade and disclose
  this in the run assumptions.
- **Factory:** every generated strategy declares the contract (`factory_space.EXECUTION_CONTRACT`); factory and space are `/4`.
- **Found by the tests:** process workers run cells through a lightweight context, not `Services`, so the contract resolver is
  a plain function `instruments.contract_for(cfg, sizing)`.

### ADR-62 Capability audit: equity-based risk, no-progress exit, trade caps, exit-based re-entry, volume gate
- **Problem:** before the research space is frozen, every dimension the factory varies must be shown to be executed by
  the numerical engine. The audit (`edgelab/strategy/capabilities.py`, rendered to FACTORY_CAPABILITIES.md, drift- and
  evidence-tested) found four dimensions that were declared "not executable" although they are mechanical and part of
  the requested space, and one structural hole: the feature engine accepted volume-weighted features on Dukascopy data
  (`volume_type: unknown` is in `VOLUME_OK`), i.e. it could silently treat provider-defined decimal volume as exchange volume.
- **Decisions:**
  - **Equity-based risk (implemented):** `sizing.mode: equity_risk` (`risk_pct`, `starting_equity`, optional cap). The backtester keeps
    `equity = starting_equity + net P&L of trades already exited`; it is updated after a trade's costs are known and read
    only by LATER signals (positions are sequential, so every earlier trade has exited). The size is decided at the signal
    from the planned initial stop, rounded down, 0 = rejected, cap deterministic. Quote-aware sizing is unchanged
    (planned entry on the entry-side series). Trade rows gain `equity_before` (equity-sized runs only). Known answers and
    "future outcomes cannot change earlier sizes" are tested. Path dependence within a run is disclosed in the run assumptions.
  - **No-progress exit (implemented):** in the managed-exit kernel (`TrailSpec.np_*`, DSL `exit.no_progress`); semantics in STRATEGY_DSL.md 6.0.
  - **Per-strategy trade cap and exit-based re-entry (implemented):** `entry.max_trades_per_day`, `entry.reentry`
    (`SignalSet.max_trades_per_day / exit_cooldown_bars / block_after`). They count executed trades and use only exits
    that have already happened, so they are causal by construction and need no extra signal-layer state. The top-level
    `cooldown_after_exit` key remains refused by name (use `entry.reentry`).
  - **Volume gate (implemented):** the feature engine refuses `vwap`, `volume_stats` and `rvol_tod` for any instrument that declares
    `volume_semantics` (Dukascopy: "NOT CME exchange-traded volume"). Not part of any cache key; existing caches are valid.
  - **Excluded, with reasons (capability matrix):** VWAP/volume levels and event/news filters (canonical data cannot support
    them), one-direction-per-session (needs per-session path state), partial exits / scaling / target trailing / tier tables.
- **Factory:** factory `/3`, space `/3`. New dimensions: equity-risk sizing variants, no-progress exit, per-strategy cap,
  re-entry, `htf_breakout` MTF filter, `prior_day_nr` regime. The misleading `(atr_normalized)` label was removed (every risk
  mode scales with the stop). Redundant combinations are refused (cap 1 with any re-entry rule, re-entry on a target that
  does not exist, a no-progress check after the window ends, ...). The allocation numbers/version are unchanged.
- **Manifest impact:** the factory and space versions changed, so the previous manifest is superseded; the new manifest id
  records `capability_sha256` and embeds the matrix.

### ADR-61 Trailing / breakeven stops and completion of the 30-family catalog
- **Problem:** the execution kernel fixed the stop at entry, so trailing was only a declared, refused dimension
  in the factory. The catalog also lacked Order Block, Breaker Block and ICT Opening Range setups, the
  session-level NR7, and the percentage form of rate-of-change momentum.
- **Trailing decision:** a separate kernel function `engine/fills.py::simulate_exit_trailing` used only when a
  strategy carries a `TrailSpec` (`engine/signals.py`); `simulate_exit` and every non-trailing path are untouched.
  A differential test proves a trail that never moves equals the legacy kernel on random data (all exit kinds,
  both directions), and the Phase 1 demo output is unchanged. Semantics (documented in full in STRATEGY_DSL.md 6.1):
  - **When/what activates:** `immediate`, or once the favorable excursion from the fill reaches N points, N x R
    (R = |fill - initial stop|) or N x ATR(signal bar). An optional breakeven trigger can fire first.
  - **Update frequency:** a decision at every bar close, optionally every N bars since entry and/or only on a new
    favorable extreme, with an optional minimum step. Effective from the next bar: no same-bar use of the bar's own
    extreme and no future information.
  - **New level:** distance mode = extreme -/+ points or ATR multiple (ATR at that bar's close); level mode = the
    strategy's own causal per-bar operand (swing, previous bar, moving average, channel, chandelier). Breakeven =
    fill +/- offset.
  - **Ratchet:** never loosens; a candidate not strictly protective of the bar's close is ignored.
  - **Execution:** each bar is resolved with the existing `resolve_bar` using the current stop (gap at the open,
    touch at the stop, conflict policy incl. intrabar replay). Exit-side quote arrays drive both the extreme and the
    touches (long: BID, short: ASK); level fills do not count the entry bar's pre-fill extreme.
  - **Other exits:** target fixed; forced session close, time stop, max hold, end of data and signal exits keep
    their precedence, so the same-trading-date and <23 h guarantees hold by construction and are tested.
  - **Causality:** the per-bar trailing arrays are compared by the existing truncation test (`check_causality`).
  - **Identity:** `exit.trailing` enters the canonical logic only when present; existing logic hashes are unchanged.
    R, `risk_points` and sizing stay relative to the initial stop; trade rows gain `final_stop_price` and
    `trail_updates` (trailing strategies only) and the run assumptions disclose the rules.
- **Catalog completion:** new causal features `order_block` (order block + breaker block) and `daily_nr`
  (`features/library/smc.py`); ICT family 30 now carries eight setups (liquidity sweep reversal, liquidity raid
  reversal, FVG reaction, kill-zone momentum, OTE, order-block reaction, breaker block, opening-range/initial-balance
  sweep); NR7 has bar and trading-date scopes; ROC has ATR-scaled and percentage units. The repository held no earlier
  definitions of the ICT concepts, so each is defined mechanically here (FEATURES.md): e.g. an order block is the last
  opposite candle before a displacement that also breaks structure, consumed by its first touch or invalidated by a
  close through its far edge; a breaker is a failed block with flipped polarity; the opening-range setup is a
  sweep-and-reclaim of the completed NY opening range (30 min) or initial balance (60 min), distinct from family 17's
  breakout. No discretionary rule is used.
- **Factory:** factory `/2`, space `/2`. Trailing is a sampled dimension (kind, activation, update rules, step,
  breakeven-before-trail) with redundancy rules (an activation the target always precedes, a throttle longer than the
  window, `only_new_extreme` on a distance trail, ...). A `zone` stop is added for the ICT setups. The manifest pins
  each family's declarative definition (`family_spec_sha256`, `catalog_sha256`). The allocation numbers and version
  are unchanged and use no results.
- **Capability gaps:** superseded by the ADR-62 audit (FACTORY_CAPABILITIES.md). Equity sizing, no-progress exits,
  per-strategy trade caps and exit-based re-entry are implemented there; VWAP/volume levels and event/news filters are
  excluded for data reasons. The trailing candidate of a short on a BID-based feature series is evaluated against ASK
  touches (as for every price stop): the ask-side level is not modelled.

### ADR-60 Day-trading strategy factory (generation only)
- **Problem:** a future high-budget search needs ~10,000 distinct, mechanically valid day-trading
  strategies from 30 predefined families. They must be reproducible, honestly de-duplicated and
  generated WITHOUT any numerical evaluation. Mode A varies one base's declared parameters only.
- **Decision:** `strategy/factory_space.py` holds the frozen space (30 families, sessions,
  shared dimensions, allocation). `strategy/factory.py` samples, validates, compiles and writes the
  manifest. `strategy/daytrading.py` holds the central day-trading policy.
  - Families are DSL templates. The compiler, identity (logic hash) and feature registry are unchanged.
  - Sampling: `sha256(seed|family|candidate_seq|dimension)` per draw. Platform- and order-independent.
  - Allocation (`edgelab-dt-allocation/1`): fixed group totals (technical 4200, price action 4000,
    SMC 800, ICT 1000). Each family gets 200, plus a share of the rest by a predeclared structural
    score. No results are used. `allocate()` is pinned by a test.
  - Validation: 12 named stages, machine-readable codes, never repaired. Duplicate logic (global logic
    hash) is recorded, not counted. An under-filled family fails loudly.
  - Behaviour-equivalent forms are refused (points target with points stop == risk_reward, cooldown
    >= entry window == one per window, non-binding time exits / early flat, pinned inactive parameters).
- **Day-trading invariant:** `validate_day_trading` inspects the DSL document itself. It requires:
  - strategy-local ENTRY and HOLD windows;
  - a top-level `session(HOLD).in_session == 0` exit on every traded side, so the position is flat by
    `HOLD.end + tf`, checked under four DST regimes to lie inside one NY trading date by 16:00 NY;
  - `max_hold_bars x tf < 23 h`, and resting orders that expire inside HOLD.
  `check_engine_config` refuses `hold_overnight` / no daily flatten / late flatten.
  The policy is not a variation dimension.
- **Not executable, declared and refused by name:** equity-based sizing, VWAP/volume levels
  (Dukascopy volume is not exchange volume), event filters, no-progress exits, per-strategy
  max-trades-per-day, and exit-based re-entry rules. Trailing stops were first declared here as not
  executable; ADR-61 implements them in the engine and the factory varies them for real.
- **Earlier-phase changes (additive):**
  - 7 new registered causal features (`features/library/indicators.py`: `macd`, `adx`, `stoch`,
    `bollinger`, `donchian`, `narrow_range`, `atr_regime`); FEATURES.md is regenerated. The
    registry-wide truncation test covers them. Existing feature implementations and cache keys are unchanged.
  - Lineage method `factory_variant`.
  - Read-only Services / CLI (`factory ...`) / HTTP (`/api/factory/...`) Explorer foundation.
- **Separation:** the factory reads no market data. It does not backtest, and does not touch
  protocols, trial ledgers or holdouts (tested with those entry points mocked to fail). The manifest
  is input for a future dedicated protocol. The active protocol is not modified.

### ADR-57 Robust acceptance statistics (research protocol version 2)
- **Problem:** the v1 OOS confidence criterion was the normal bound `mean - z*se` at the Bonferroni
  one-sided alpha `0.05/N`. At N = 2000 that is alpha 2.5e-5, z ~ 4.06. For left-skewed trade returns
  (many small wins, rare large losses) the studentized mean's upper tail is heavier than normal. In a
  synthetic check at alpha 1e-3 the normal bound exceeded the true mean ~3x as often as nominal
  (right-skewed returns: ~nominal), so v1 was anti-conservative for exactly the strategies it must
  reject. The v1 control rule (a linear-interpolated 95th percentile of 100 controls) had an inexact
  definition and could be misread as a significance test.
- **Chosen (protocol_version 2, `research/protocol.py`):**
  - `oos_confidence` = `min_normal_bootstrap_t_v1`:
    `LB = min(mean - z*se, mean - q*se) = mean - max(z, q)*se`, with
    `se = std(ddof=1)/sqrt(n)` of the OOS per-trade net R, `z = Phi^-1(1 - alpha')`, and `q` the
    `ceil((1 - alpha')*B)`-th smallest of `B` studentized bootstrap statistics
    `t*_b = (mean*_b - mean) / (std*_b/sqrt(n))`.
  - Bootstrap parameters, all pre-registered:
    - trades resampled with replacement, `B = 1,000,000`, which leaves >= 25 replicates beyond the
      tail at the full 2000-trial budget;
    - drawn in chunks of 2000 with `numpy.random.default_rng(seed)`;
    - `seed = int(hash_obj({protocol_id, logic_hash, purpose})[:16], 16)`: derived, never chosen,
      and one look per candidate, so no seed shopping;
    - a zero resample se gives a signed `inf` (conservative).
  - `n < 30`, `se = 0` or non-finite returns -> unavailable -> NOT met.
  - The minimum of the two bounds is valid whenever either approximation is. It is never easier than
    the normal bound, and it is monotone non-increasing in the trial count (the same `B` draws; only
    the quantile level moves).
  - Multiple testing is unchanged (`Bonferroni-familywise-alpha`, familywise 0.05, family = counted
    unique trials). The per-test `alpha' = 0.05/N` is passed exactly into both bounds.
  - Random control = `monte_carlo_pvalue_v1`: `p = (1 + #{controls with expectancy_r >= candidate or
    non-finite}) / (100 + 1) <= 0.05`, i.e. at most 4 of 100 controls may match or beat the candidate.
    It is a **robustness filter** against a conditional null, not a familywise test: its smallest
    attainable p is 1/101, so it cannot and does not carry the trial-count adjustment.
  - Protocol records store the whole definition. Version-1 records are still assessed by their own
    rules (normal / percentile).
  - `store.save_protocol` verifies a record's identity (`PROTOCOL_TAMPERED`) before any write.
- **Limitations (disclosed, not solved):**
  - trade-level resampling assumes i.i.d. trades; serial dependence and regime change are not modelled
    (a block bootstrap would be the next step);
  - bootstrap quantiles at ~2.5e-5 rest on ~25 tail replicates and on the empirical distribution,
    which cannot represent losses larger than any observed;
  - results depend on numpy's PCG64 stream (the numpy version is recorded in each assessment).
- **Protocol versioning:** the change alters `acceptance_criteria` and `protocol_version`, so it is a
  new protocol identity. The user-workspace protocol `RP_257969CFAFFD` (v1) had zero trials, zero
  proposal attempts and zero holdout looks. It is retired, never edited, before any numerical trial,
  and a v2 protocol with the same windows, budgets and exposure replaces it.
- **Pre-grant identity check (preflight fix):** before a holdout look is granted, `evaluate_holdout`
  recompiles the frozen library definition. It refuses `HOLDOUT_DEFINITION_CHANGED` unless the result
  has the shortlisted `strategy_id`/`logic_hash`. Before this fix, a library instance changed in place
  was only caught by the gate after the grant, which spent the look.

### ADR-56 Locked research protocol, holdout ledger, program-level trial ledger (pre-AI gate)
- **Problem:** the pre-AI audit found three protocol blockers:
  - no locked holdout (OOS splits were chosen per call and searches could span all data);
  - trials counted per search only;
  - no pre-registered acceptance criteria tied to the number of trials.
- **Chosen:** `edgelab/research/protocol.py` plus four SQLite tables in the existing store
  (`research_protocols`, `protocol_trials`, `protocol_proposals`, `holdout_access`), and
  `search_batches.protocol_id`.
  - **Protocol record:** trading-date discovery and holdout windows (with exact resolved bars),
    source dataset identity, execution (cost model, backtest-config hash, research config hash),
    trial budget, holdout-look budget, acceptance criteria, multiple-testing rule and
    `pre_protocol_exposure` (run identities, never metrics).
  - **Identity and lifecycle:** `protocol_id = RP_ + hash(material)`. Records are insert-only;
    tampering is detected. Status only moves ACTIVE -> RETIRED; one ACTIVE per instrument/provider.
  - **Enforcement (Services):**
    - `_run_cell` gates every evaluation by the bars it actually uses (discovery / holdout /
      overlap) before running;
    - `plan_search` refuses windows touching the holdout, and the protocol is part of the search
      identity;
    - `evaluate_oos` / `walk_forward` pre-check every window before any run (optional `bounds`
      for internal validation inside discovery);
    - the AI request scope must state a `date_scope` inside discovery;
    - a changed research config is refused (`PROTOCOL_CONFIG_CHANGED`);
    - refusals are `ProtocolRefusal(code)`, HTTP 409 `protocol_refusal`.
  - **Trial ledger:**
    - every discovery evaluation event is recorded (backtest_strategy, search cells sequential and
      parallel, internal validation, random-control candidate);
    - one unique trial = (protocol, logic_hash, content hash of the evaluated bars, config hash);
    - duplicates and failures are recorded, never counted;
    - AI generations and Mode B batches are separate proposal attempts;
    - the budget is enforced before running.
  - **Holdout:** only `Services.evaluate_holdout(protocol, search, strategy)`:
    - requires an ACTIVE protocol with an unchanged config, a search attributed to it, a
      shortlisted candidate with a counted discovery trial, no prior look at that logic, and
      remaining look budget;
    - every attempt is written to the ledger first (refusals included);
    - runs the frozen candidate on exactly the holdout bars (OUT_OF_SAMPLE), the protocol's
      random-entry control and cost stress, then applies the pre-registered criteria with the
      Bonferroni family size = counted unique trials;
    - the result is `HOLDOUT_CRITERIA_MET` / `_NOT_MET`, never "accepted".
- **Scope limit:** enforcement is at the `Services` boundary (CLI, web, AI). Library functions called
  directly (`run_backtest`, `research.compare.run_across_datasets`, scripts) are not governed.
- **Unchanged:** the engine, fills, costs, DSL, stored runs, datasets, ranking maths. With no
  ACTIVE protocol every path behaves as before (legacy suites and the Phase 1 demo unchanged).

### ADR-55 Directional BID/ASK quote execution (after Phase 9)
- **Problem:** every trigger and fill used the dataset's one OHLC series. For Dukascopy that series
  is BID, so buy-side events (a long's entry, a short's stop-loss, target and close exits) were
  evaluated on BID, although a buy executes against ASK. Short stops fired late or not at all and
  short targets filled early (optimistic). The per-bar spread was charged as an average cost
  instead. The imported BID/ASK datasets kept only `ask_close` (for the spread), but the combined
  source file carries the ASK feed's own `ask_open/ask_high/ask_low/ask_close`.
- **Chosen (data layer):**
  - `BarArrays` gains optional `ask_open/ask_high/ask_low/ask_close`, all four or none;
    `DatasetManifest.has_ask_ohlc` (default false) is omitted from `to_dict` when false.
  - The content hash appends the ASK arrays only when present. Datasets without ASK keep
    byte-identical hashes, manifests and manifest hashes.
  - The importer adds the options `ask_open_column/ask_high_column/ask_low_column` (with
    `ask_close_column`). It requires all four, `price_basis: bid` and `bid_close_column` = the
    primary close column; `spread_column` is refused. Values are parsed like BID OHLC and never
    inferred.
  - Validation FAILs on a partial ASK set, a non-finite ASK value, an internally inconsistent ASK
    bar, or ASK < BID on open, high, low or close. Duplicate timestamps differing only in ASK are
    conflicts.
  - Resampling: ASK first/max/min/last over the same present sub-bars as BID. If any present
    sub-bar lacks an ASK value, that bucket's whole ASK side is NaN and validation then fails it.
    BID aggregation and the spread (mean of sub-bar spreads) are unchanged.
  - The SQLite `bars` table gains four nullable columns (migration on open); ASK is loaded only
    when the manifest declares it.
- **Chosen (engine), opt-in via cost `spread_source: quotes`:**
  - **Sides:** buys execute on ASK and sells on BID. A long enters on ASK and exits on BID; a
    short enters on BID and exits on ASK. The backtester builds a second `MarketArrays` from the
    stored ASK OHLC (same session masks). `find_entry` runs on the entry side and
    `simulate_exit` on the exit side, so every existing fill, gap, close-exit, signal-exit and
    excursion rule applies unchanged on the side that executes.
  - **Unchanged inputs:** signals and features stay on the primary BID series. Position sizing
    for market entries uses the entry side's signal-bar close.
  - **Entry-bar certainty (two-sided):** a touch at X on the fill bar of a level fill at L is
    certain only if (a) the exit-side touch implies the entry side reached X, and (b)
    `(L - entry_side_open) * (X - L) > 0`.
    - (a) follows from BID <= ASK: a long's BID high >= target implies ASK >= target, and a
      short's ASK low <= target implies BID <= target. A stop touch never satisfies it.
    - Otherwise the touch is a conflict for the configured policy.
    - This is never more optimistic than the single-series rule. It differs only for a
      limit-entry fill bar's stop touch: previously certain, now a conflict, which gives the same
      outcome under `conservative`.
  - **Intrabar replay:** requires ASK OHLC in the lower-timeframe dataset, otherwise replay is
    refused and the fallback policy applies (recorded). An HTF bar is reliable only if both BID
    and ASK minutes reproduce it. The entry trigger replays on the entry side and stops/targets on
    the exit side.
  - **Refusals:** the run is refused when the dataset has no ASK OHLC or any non-finite ASK value
    (never treated as "no trigger"). The same condition is an eligibility reason in
    `Services._dataset_eligibility`.
  - **Costs:** the separate spread is exactly 0: `spread_points` must be 0/unset, and
    `round_trip_base` refuses a spread override. Commission (notional on the actual quote-side
    fill prices), fees, slippage (once per fill: market/stop configured, limit configured) and
    financing are unchanged.
- **Consequences:**
  - In `quotes` mode, gross P&L is after the bid/ask spread. `cost_sensitivity` and
    `breakeven_cost_multiplier` scale only the explicit costs (commission, fees, slippage,
    financing), never the spread. This is recorded in the assumption `gross_pnl`.
  - R = |fill - stop| uses the quote-side fill, so it includes the spread paid at entry.
- **Provenance:**
  - Assumptions: `quote_model` (`single_series` | `directional_bid_ask`), `execution_sides`,
    `spread_treatment` (`none` | `fixed_cost` | `cost_avg_entry_exit` | `embedded_in_quotes`),
    `dataset_has_ask_ohlc`, `gross_pnl`, and side-naming fill-rule strings (`market_entry`,
    `stop_entry`, `limit_entry`, `stop_exit`, `target_exit`, `close_exit`, `signal_exit`).
  - Intrabar info: `ltf_has_ask_ohlc` and `quote_sides`.
  - Trades: `entry_quote_side` and `exit_quote_side`. `trades_hash` is unchanged in definition.
  - Costs: `spread_source: quotes`.
- **Equivalence:** with ASK == BID, directional and single-series runs give identical trades
  hashes (tested for market, stop and limit entries).
- **Rejected:**
  - a mid series;
  - ASK OHLC inferred from BID + spread or from `ask_close`;
  - charging the spread both in prices and as a cost;
  - switching existing datasets or profiles automatically.
- **Unchanged:**
  - `fixed`/`dataset` spread modes and all single-series behaviour, and the Phase 1 demo;
  - the frozen BID/BIDASK datasets;
  - the active Dukascopy cost profile at the time (`spread_source: dataset`; switched by the follow-up below).
  - `Services.list_datasets` rows do not yet show `has_ask_ohlc` (visible in dataset detail).
- **Follow-up: directional BID/ASK is the canonical Dukascopy research execution model.** After the
  real ASK-OHLC re-import (`NQ_DUKASCOPY_BIDASK_OHLC_2021_2026_1M_6B0A245100` / `_5M_96699F7568`, BID,
  spread, timestamps, quality and gaps identical to the frozen BIDASK datasets; zero BID > ASK) and the
  reconciled real comparison (RUN_2026_00027 single-series vs RUN_2026_00028 directional; aggregate net
  delta closed to ~1e-14 R):
  - `NQ_DUKASCOPY@DUKASCOPY` uses scenario `dukascopy_directional_cost_assumption_v1` with
    `spread_source: quotes`. Every number is unchanged from `dukascopy_central_cost_assumption_v1`
    (notional 30.15 per USD 1M per side, fees 0, slippage 0.50 / 0.50 / limit 0 points, financing
    `not_modeled`); status stays `assumed` (not broker-verified).
  - Only datasets with `has_ask_ohlc` are eligible. BID-only and BID+spread datasets (including the
    frozen `NQ_DUKASCOPY_BIDASK_2021_2026_*`) are refused with reason code `ASK_OHLC_REQUIRED`.
    Eligibility rows add `reason_codes` (quote/spread refusals: `ASK_OHLC_REQUIRED`,
    `DATASET_SPREAD_REQUIRED`), `has_ask_ohlc`, and `cost.spread_source` / `cost.quote_model`.
  - Legacy single-series runs (BID + explicit spread cost, scenario
    `dukascopy_central_cost_assumption_v1`) and the frozen datasets stay stored, unchanged, as evidence;
    they are not recomputed. Re-running them needs a config that restores the legacy profile.

### ADR-54 Dukascopy source identity, preferred research dataset, AI discovery over the Mode B gate (Phase 9)
- **Problem:**
  - The user's primary data is a Dukascopy Nasdaq-100 1m CSV. Its symbol, asset class, price side,
    volume meaning and contract economics are not stated by the file.
  - Research needs one place to say "use this dataset for new work".
  - AI-assisted discovery must not become a way around the gate, a results feedback loop, or a
    source of claims.
- **Chosen (data):**
  - **Import:** the SAME import pipeline, with a new layout profile `dukascopy_utc_csv` (explicit
    offsets → exact UTC; `volume_type: unknown`; `symbol: UNSTATED`).
  - **Identity:** a new instrument `NQ_DUKASCOPY` with `identity_status: provisional` (plus
    `required_metadata`, empty `source_symbol` / `identity_evidence`). `instruments.identity_problem` /
    `check_identity` refuse, in `Services._run_cell` (the ONE run path), anything that interprets
    point value / sizing / costs until the user states the identity (`user_specified`) or verifies
    it (`source_verified` + evidence).
  - **Costs:** its own cost profile (`NQ_DUKASCOPY`, `providers.DUKASCOPY`), unconfigured. HistData's
    assumed profile is untouched and never applies.
  - **Calendar:** a source calendar the validation gate checks rather than trusts. It was
    `DUKASCOPY_NQ_PROVISIONAL` and is now `DUKASCOPY_USATECH_OBSERVED` (18:00 to 16:15 New York,
    measured on the real file), still `calendar_status: provisional_unverified`.
  - **Gaps:** a read-only gap classification (`data/quality.py`).
  - **Preferred dataset:** stored in `data/workspace_preferences.json`, outside runs, datasets and the
    config hash. Setting it re-validates the dataset.
- **Chosen (AI):** the `edgelab/ai/` package.
  - **Request:** a versioned request schema (claims, code and unknown keys refused).
  - **Context:** a versioned **blind context** built only from dataset identities, instrument
    metadata, the DSL menu and cost status; it never reads the run registry. A forbidden-key guard
    and a hash-stability test enforce this.
  - **Providers:** `AIProvider.generate_proposals(request, context)` with a deterministic
    `MockProvider`, and an optional stdlib-HTTPS Anthropic provider configured only by environment
    variables (no default model; the key is never persisted).
  - **Envelope:** EdgeLab assigns `request_id` / `generation_id` / `proposal_id` from content hashes.
  - **Gate:** schema → DSL validation → supported features → causality → parameter domain → request
    constraints → canonical compile → identity. The DSL validator's own issues are routed to stages,
    and nothing is repaired.
  - **Review:** human accept/reject. Saving creates an ordinary library strategy with the generation
    method `mode_b_proposal`, or `mode_b_modification` with its parent.
  - **Records:** file-based and immutable, under `data/ai_discovery/`.
- **Rejected:**
  - a second importer;
  - assuming CME NQ economics or reusing HistData costs;
  - auto-setting the preferred dataset on import;
  - storing the preference in the config (it would change the config hash) or in run records;
  - letting providers assign ids or pass partially valid output after a "fix";
  - any ranking or "best" label;
  - feeding results to the provider;
  - an automated generate→test→regenerate loop.
- **Unchanged:** the backtester, fill/cost methodology, random-entry control, prop simulation,
  validation gate thresholds, HistData datasets/configs and every stored run.
- **Additive changes to earlier modules:**
  - `Services._dataset_eligibility` rows gain `identity` and `preferred`, and a provisional
    identity is an ineligibility reason;
  - `backtest_readiness` returns `preferred_dataset_id`;
  - `GENERATION_METHODS` gains `mode_b_modification`;
  - `_limitations` names unknown volume semantics.
  - The research config hash changes because configs gained entries; stored runs keep theirs.

### ADR-53 Research workspace selection is a pointer, resolved in one place (desktop change)
- **Problem:** a packaged launch without `--data-root` silently used `%LOCALAPPDATA%\EdgeLab`
  (`runtime.user_data_root()`), and `init_workspace` created a fresh, empty workspace there. So the
  app showed no datasets even though the user's research lived in the development folder, and the
  GUI had no way to point at it.
- **Chosen:**
  - **Resolution:** `runtime.resolve_workspace` is the single authority: `--data-root`, then
    `EDGELAB_DATA_ROOT`, then the saved selection, else none (first run: the Welcome screen).
  - **Persistence:** the selection lives in the app settings file (`runtime.settings_path`, outside
    every workspace).
  - **Validation:** `runtime.inspect_workspace` is read-only. It loads configs, opens the store
    SQLite `mode=ro`, reads counts, checks writability and refuses demo folders.
  - **Creation:** `runtime.create_workspace` works only in empty folders.
  - **Serving:** the desktop launcher serves `edgelab.workspace_host.WorkspaceHost`, a WSGI
    dispatcher holding one `create_app(root)` (unchanged) plus the workspace lock. `/api/workspace*`
    switches by validating, locking, cancelling the old job, closing the old store, swapping and
    saving the selection. With no workspace, the UI is still served and other API calls answer 409
    `no_workspace`.
  - **Development:** the server (`python -m edgelab.web`) reports its fixed root read-only.
- **Rejected:**
  - auto-detecting or auto-migrating a development folder (guessing, and mutation);
  - copying data into the default location;
  - storing the selection inside a workspace (it would conflict between workspaces);
  - a second, GUI-only storage path.
- **Unchanged:** the web app, services, store, engine and research code; `--data-root` / `--demo`
  semantics; the per-workspace single-instance lock.

### ADR-52 Native desktop window: pywebview over the existing loopback app (desktop change)
- **Problem:** the packaged app showed its UI in the user's default browser. The product needs its own
  window, without a new UI stack or backend changes.
- **Chosen:** `edgelab/desktop_window.py` hosts a pywebview window (Windows: Microsoft Edge WebView2 via
  pythonnet/WinForms, `gui="edgechromium"`), navigated to the loopback URL the launcher already serves.
  - The GUI loop runs on the main thread, and closing the window returns to the launcher's existing
    shutdown path.
  - WebView2 presence is checked from the registry before anything starts, with a clear message
    (`--ui browser` as the explicit alternative). There is no silent fallback engine.
  - A loopback control channel, token-protected with the per-launch token in `runtime.json` and added
    only by the launcher, lets a second launch focus the running window. It also lets the Windows
    integration test navigate the window and observe what it loaded.
  - The PyInstaller spec builds `EdgeLab.exe` (windowed) and `EdgeLabConsole.exe` (console: CLI,
    logs, headless smoke) over one bundle. The windowed exe's stdout/stderr go to
    `logs/console.log`.
- **Rejected:**
  - Electron: a Node runtime and a second packaging system.
  - CEF / Qt WebEngine: large, and duplicate what Windows already ships.
  - Keeping the browser: not a desktop app.
  - Bundling a fixed-version WebView2: about 150 MB, and security updates would lag the Evergreen
    runtime.
- **Unchanged:** the frontend, the backend, the research code, `python -m edgelab.web`, the data root,
  the single-instance lock and the build command.

### ADR-51 Strategy Lab: read models over stored research, thin validation/proposal routes (Phase 8)
- **Problem:** the backend could already version strategies, generate variations, run searches, OOS / walk-forward
  / random controls and prop simulations, but the GUI reached only part of it. Comparison and provenance
  existed only as scattered records.
- **Chosen:** `research/lab.py` holds read models only, and adds no research logic:
  - `strategy_research`: machine-readable strategy provenance (id, definition/logic hash, parent id and
    parent definition hash, method, batch spec, parameters, stored runs with scope, validation state
    `validated: false`);
  - `compare_runs`: stored runs from exactly one source (lineage / version / batch / search / run ids), with
    stored headline metrics, `breakeven_cost_multiplier`, scope label and cost profile/status;
    **unranked**, with no score;
  - `run_curve`: cumulative net R and drawdown from stored trades;
  - `oos_control`: the unchanged `random_entry_control` on the `oos_windows` OOS window, labelled
    OUT_OF_SAMPLE, the convention of `run_oos_random_controls_5y.py`.
- **HTTP:** thin routes for these, for the existing `evaluate_oos` / `walk_forward` / `random_entry_control`
  (synchronous, inputs bounded) and for the existing Mode B gate (`ingest_proposals`, `proposal_menu`).
- **Variation preview:** now lists the exact combinations via the generator's own `_combos`, so what is
  previewed is exactly what generation enumerates.
- **Frontend:** the React app gains the Research hub and Validate tabs, the Compare page, run curves and
  breakdowns, the AI Proposals gate page and dataset pickers that cannot select ineligible datasets.
- **Rejected:**
  - a composite score or "best" ranking (it would imply a verdict the engine does not make);
  - a UI-side strategy model (the DSL document stays the source of truth);
  - running validations as background jobs (the Phase 4 job manager is search-specific, so this
    is deferred and documented).
- **Unchanged:** the research methodology, the control method, the engine, the DSL and stored datasets.

## Known limitations (Phase 1)

- Bar-level simulation: holding time and excursions are bar-resolution; partial fills and
  queue position are not modelled.
- Signals whose *absolute* stop is already breached at the fill are skipped (optimistic);
  counted in `skipped["STOP_BEYOND_FILL"]`. Distance-based stops cannot hit this case.
- One position per strategy; no pyramiding.
- No real market data, event data, or holiday calendars are bundled.
- DuckDB backend untested in the build sandbox.

## Known limitations (Phase 2)

- No real CFD or futures data has been imported; CFD handling is validated on synthetic files
  written in real provider layouts only.
- CFD datasets default to the CME equity calendar; broker trading hours may differ (validation
  flags bars outside the calendar; `inspect` shows the hours that actually contain data).
- On bid-based feeds, the fact that a short's stop triggers on the ask is not modelled; total
  spread cost per round trip is charged instead.
- Dataset spread is the average of the entry-bar and exit-bar spread (bar-level approximation).
- FVG is the slowest feature (~3.6 s per year of 1m bars; computed once, then cached).
- Loading a stored dataset re-runs validation (seconds for multi-year 1m data).
- The feature cache has no size limit or garbage collection yet.
- `rvol_tod` averages the previous k occurrences of a slot, not k calendar days.

## Known limitations (Phase 3)

- `lag` counts strategy-timeframe bars. On a higher-timeframe operand it does not step back one
  HTF bar.
- `entry.cooldown_bars` is measured between signals, not from trade exits (exits exist only inside the
  backtest).
- Targets are fixed at signal time and the initial stop is fixed at entry; it can only be trailed or moved to
  breakeven through `exit.trailing` (ADR-61). There are no partial exits or pyramiding (refused by name).
- The bid/ask trigger asymmetry on bid-based CFD feeds remains unmodelled (Phase 2).
- Canonical equivalence is syntactic plus a few algebraic rules; logically equivalent but
  differently written conditions can hash differently.
- Mode B claim detection is pattern-based. No AI model is called anywhere.
- No batch execution of variations or proposals in Phase 3 (added in Phase 4). `strategy backtest`
  runs one strategy on one dataset.

## Known limitations (Phase 3.5)

- The development server and the lock serialize requests. Single backtests and variation generation stay synchronous, with busy states; Phase 4 searches run as background jobs with progress.
- No authentication: the app is a local, single-user tool bound to loopback by default.
- No dataset deletion (datasets are immutable) and no browser file upload (files are imported from `web.import_dirs`).
- React typings come from a local shim (offline build); `npm install` restores `@types/react`.

## Known limitations (Phase 4)

- Research storage is SQLite-only: `DuckDBStore` refuses every search operation. With
  `storage.backend: auto`, installing DuckDB switches the store and research then refuses
  (HTTP 409, CLI exit 2); set `storage.backend: sqlite` to keep research available.
- Cross-process use of one data root is not coordinated: `next_run_id` is MAX+1, so a CLI
  `research run` and the web app running at the same time could collide on a run id (the cell is
  recorded as failed and re-runs on resume), and a new web process marks a search that another
  process is still running as `interrupted`.
- Trials are counted per search. The seed is part of the search identity but does not change
  deterministic DSL results, so the same work can be re-evaluated under another search id; there is
  no global trial count across searches (needed before Phase 6 overfitting reports).
- Background jobs run sequentially (`workers: 1`); worker processes are available through
  `run_search` / `research run --workers N`.
- Job ids and job state are process-local: after a restart a job id returns 404 (its stored search
  persists and is marked `interrupted`), and the CLI `research job` follows its job in the foreground
  (Ctrl-C cancels).
- Each worker process receives its own copy of the datasets (memory grows with workers x data) and
  start-up costs about 1-2 s.
- FeatureCache: two simultaneous misses compute the same entry twice; `clear()` racing a write, or a
  reader discarding a genuinely corrupt entry while it is rewritten, only causes a recompute.
- The Research page takes proposal batches as typed `PB_` ids (the batch listing API returns
  variation batches by default).
- `scripts/benchmark_search.py` builds its synthetic fixture with `tests/phase2_helpers.py`.
- The service lock is held while each cell's dataset is loaded and re-validated (it reads the
  shared SQLite connection); the backtest itself runs without it.
- No real market or CFD data has been imported: every Phase 4 result so far is on synthetic data.

## Known limitations (Phase 6 prop simulation)

- Evaluation-phase rules only: funded phases, payouts, resets and refunds are not modelled.
- The intra-trade check is a bar-resolution bound (it can overstate adverse excursions; the entry
  bar is included in full), not a tick path. The time of an intra-trade breach is only known to lie
  inside the trade; `time_to_breach` reports the exit.
- Ending balances after a terminal breach use the breaching trade's closed P&L (where a firm would
  have liquidated is unknown). An entry-time `terminate` books no P&L for that trade.
- Trades carry the backtest's cost model; a firm's own commissions/fees are not re-applied.
- One source run per simulation (years/datasets are not concatenated).
- Sizes are compared in the instrument units of the trade records (NAS100_HISTDATA: 1 MNQ = 2 units);
  no automatic contract conversion.
- No real firm's rules are shipped; the examples are synthetic and test-only.
- Windows `.exe` packaging: see DESKTOP_PACKAGING.md (Phase 7).

## Module map (Phase 7 additions: desktop packaging)

```
edgelab/runtime.py             dev vs packaged locations (bundled resources, user workspace), workspace
                               init, build manifest generation/loading, runtime_info (ADR-50)
edgelab/desktop.py             launcher: freeze_support, workspace, instance lock, 127.0.0.1 + free port,
                               readiness, browser, clean shutdown, error reporting, `cli` pass-through
edgelab/core/identity.py       source_hash / git_commit / code_version read the manifest when frozen
edgelab/strategy/compiler.py   compiler_source_hash reads the manifest when frozen
edgelab/features/spec.py       FeatureDef.impl_hash reads the manifest when frozen (dev: cached source hash)
edgelab/core/config.py, web/{app,bundle,__main__}.py   paths via edgelab.runtime; /api/status + runtime
packaging/edgelab.spec         PyInstaller (folder mode, console, DuckDB excluded, tzdata collected)
packaging/build.py             frontend -> manifest -> PyInstaller -> dist/EdgeLab
packaging/launcher.py          PyInstaller entry script
packaging/smoke_packaged.py    read-only packaged smoke test (scratch demo workspace)
packaging/requirements-build.txt, build_windows.ps1
```

## Known limitations (Phase 7 desktop)

- `EdgeLab.exe` itself was not produced in the development environment (Linux, no cross-compilation). The
  identical spec was built and smoke-tested as a Linux folder app. Windows-only code paths (message box,
  console-close handler, msvcrt lock, CTRL_BREAK) are untested until the first Windows build.
- The console window is the app's lifetime (no tray icon / in-app quit); werkzeug's server; unsigned;
  no installer or updates; imports only from `<workspace>/data/import`.
- The CLI pass-through does not take the desktop instance lock.
- Existing repository data is not migrated (documented manual paths only).

## Module map (Phase 8 additions: Strategy Lab)

```
edgelab/research/lab.py        strategy_research (AI-ready provenance), compare_runs (unranked), run_curve,
                               oos_control (unchanged control on the OOS window) (ADR-51)
edgelab/services.py            + strategy_research, compare_runs, run_curve, oos_random_control;
                               variation_preview lists the exact combinations (<= 500)
edgelab/web/app.py             + /api/strategies/<id>/research, /api/compare, /api/results/<id>/curve,
                               /api/validation/{oos,walkforward,control}, /api/proposals/{menu,ingest}
web/src/components/strategy/lab.tsx   ScopeBadge, EquityChart, DatasetPicker, ProvenanceCard, RunsTable,
                               BatchResearch, ValidationPanel, CompareTable
web/src/pages/Compare.tsx      Compare page; Strategies.tsx Research hub + Validate tabs; Research.tsx run
                               curve/breakdowns; Data.tsx AI Proposals gate page
lab_smoke_real.py              local GUI-path smoke test on a real stored dataset
```

## Known limitations (Phase 8 Strategy Lab)

- Backtests, OOS, walk-forward and controls run synchronously in the request, holding the service lock, so the
  UI waits and other calls queue. Only batch searches are background jobs.
- Date-range restriction exists only through validation windows (OOS split, walk-forward), not for single
  backtests.
- Comparison is capped at 500 runs per view. The equity curve is thinned above 5,000 trades for display only.
- Declared parameter grids are strict: a value off `min + k*step` is refused until the step is changed (for
  example `ema_crossover` `slow` 15 + 3k: 20 needs step 1). That is a real, hashed rule change.
- Random-control results are shown, never stored (unchanged). Re-running one needs the same seed.
- No external model is connected; AI Proposals accepts machine-readable batches only.

## Module map (native desktop window)

```
edgelab/desktop_window.py      WindowController (pywebview/WebView2 window on the loopback URL), WebView2 registry
                               check, install_control (/api/desktop/{status,focus,navigate}, token) (ADR-52)
edgelab/desktop.py             --ui window|browser|none (default window); runtime check before start; second-launch
                               hand-off (focus the running window); windowed-exe output -> logs/console.log
packaging/edgelab.spec         EdgeLab (windowed) + EdgeLabConsole (console) over one bundle; pywebview required
packaging/window_test_windows.py   Windows-only real-window integration test (scratch data)
tests/test_desktop_window.py   deterministic window lifecycle tests (fake GUI loop over the real launcher)
```

## Known limitations (native desktop window)

- The real WebView2 window has not been exercised in the development environment (Linux). It must be
  verified on Windows with `packaging/window_test_windows.py` (it runs automatically with
  `build_windows.ps1 -Smoke`).
- Requires the Microsoft Edge WebView2 Runtime (Windows 11 built in; Windows 10 through Windows Update).
  It is not bundled or auto-installed; a missing runtime gets a clear message.
- The packaged Linux build has no GTK/Qt, so window mode there fails with a clear message
  (use `--ui browser`).

## Module map (research workspace selection)

```
edgelab/runtime.py             + settings_path / load_settings / save_settings, resolve_workspace (one authority),
                               inspect_workspace (read-only validation), create_workspace (empty folders only) (ADR-53)
edgelab/workspace_host.py      WorkspaceHost: WSGI dispatcher over one create_app(root) + lock; /api/workspace* shell;
                               close_app (cancel job, close store)
edgelab/desktop.py             resolves via runtime.resolve_workspace; first-run (no workspace) mode; saved-but-missing
                               workspace -> chooser with a notice; cli pass-through uses the selected workspace
edgelab/web/app.py             + GET /api/workspace (read-only, development server)
web/src/components/workspace.tsx   WorkspacePanel (Settings), WelcomePage (first run), ChooseWorkspaceLink
packaging/workspace_snapshot.py    read-only before/after check of a workspace (counts, hashes, files)
```

## Known limitations (research workspace selection)

- One workspace at a time per EdgeLab window; a workspace open in another EdgeLab process is refused
  (its lock). The development server does not take that lock, so do not run both on one folder.
- Switching waits for an in-flight service call (it holds the service lock) and cancels a running
  background search (resumable later as `interrupted`, Phase 4).
- The Browse button needs the native window; in `--ui browser` mode, type the path.
- The per-launch first-run instance holds no lock until a workspace is chosen, so two first-run windows
  can coexist until one selects a workspace.

## Module map (Phase 9: Dukascopy source + AI discovery)

```
configs/import_profiles.yaml   + dukascopy_utc_csv (timestamp,open,high,low,close,volume; explicit UTC offsets)
configs/instruments.yaml       + NQ_DUKASCOPY (identity_status: provisional; research units; not CME NQ)
configs/data.yaml              + DUKASCOPY_USATECH_OBSERVED calendar (18:00->16:15 NY, measured on the real file; gate-checked)
configs/costs.yaml             + NQ_DUKASCOPY / providers.DUKASCOPY (unconfigured)
edgelab/instruments.py         + identity_info / identity_problem / check_identity, InstrumentIdentityError
edgelab/data/quality.py        gap_analysis: gap runs by length/position/weekday, coverage by year (read-only)
edgelab/data/preferences.py    workspace_preferences.json (Preferred Research Dataset), atomic writes
edgelab/ai/schema.py           request + proposal content schemas, claim/code detection
edgelab/ai/context.py          resolve_scope, build_context (blind; context_hash; forbidden-key guard)
edgelab/ai/providers.py        AIProvider protocol, MockProvider, AnthropicProvider (env-configured), provider_status
edgelab/ai/gate.py             gate_proposal: 8 stages with exact reasons; features_used, n_conditions, change diff
edgelab/ai/discovery.py        DiscoveryStore (generations/decisions), generate, decide, save, lineage
edgelab/services.py            + preferred_dataset*, dataset_quality, ai_status/context/generate/generations/
                               generation/decide/save/lineage; identity gate in _run_cell
edgelab/web/app.py             + /api/preferences/research-dataset, /api/datasets/<id>/quality, /api/ai/*
edgelab/cli.py                 + prefer-dataset, dataset --quality
web/src/pages/Discovery.tsx    AI Discovery page (request, blind-context preview, proposal review, history, batch import)
web/src/pages/Data.tsx         Datasets: identity/proxy/source hash/preferred columns, Preferred card, gap report
```

## Known limitations (Phase 9)

- **Directional BID/ASK (ADR-55):**
  - Canonical for Dukascopy (`spread_source: quotes`, see the ADR-55 follow-up); opt-in for every
    other profile. The real ASK-OHLC datasets live in the user's workspace, not in this repository.
  - One cost profile per instrument/provider: the legacy single-series scenario is no longer
    selectable from the shipped config (re-running it needs an edited config copy).
  - The Preferred Research Dataset is one per workspace, not per instrument/timeframe.
  - Commission and slippage remain research assumptions; the 538 partial real 5m bars, unverified
    holidays/early closes and unmodelled financing remain disclosed limitations.
  - The CLI import command and web import form do not expose the ASK OHLC columns yet;
    `Services.import_file` accepts them.
  - ASK-side tick alignment and range spikes are not separately checked.
  - DuckDB bars storage carries the columns but is untested.

- **Identity and calendar gates (added after ADR-54):** `NQ_DUKASCOPY` is Dukascopy USATECH.IDX/USD (feed E_NQ-100, BID)
  with `identity_status: user_specified`. `identity_problem` also refuses research while `calendar_status:
  provisional_unverified`, so an unverified session calendar cannot silently shape results. The refusal goes
  through the same single gate in `_run_cell`.
- **Dukascopy-style costs:**
  - The cost model gains `commission_mode: notional` (a per-USD-1M rate on the traded notional) and
    `financing_mode: not_modeled` (charges nothing, but is disclosed). Both are opt-in, with a
    single call site in the backtester.
  - The commission is a flat rate. Dukascopy's volume tiers, which depend on cumulative traded
    volume, are not modelled.
  - Time-varying financing rates are not modelled.
  - A variable spread uses the existing per-bar spread path, which needs a historical ASK series.
- **Partial calendar verification:** a third status, `regular_hours_verified`, needs
  `calendar_evidence` and `calendar_unverified_scope`. It allows research, and
  `instruments.calendar_caveat` adds the unverified scope as a dataset limitation.
  - It exists because the evidence verifies the regular hours but not the special dates. Neither
    "verified" nor "unverified" would describe that honestly.
  - Validation and thresholds are untouched: unverified special dates stay data-quality warnings.
  - `NQ_DUKASCOPY` uses it. Its only remaining refusal is the unconfigured Dukascopy cost profile,
    because no Dukascopy cost figures exist in the project records.
- **Real data:** the real Dukascopy CSV is not in this repository. The pipeline was exercised only on
  a SYNTHETIC Dukascopy-shaped fixture. Its calendar is provisional until `inspect` evidence from the
  real file confirms or replaces it, and its holidays are not listed.
- **Identity metadata:** kept in `configs/instruments.yaml` (edited by hand); there is no GUI editor.
  Stating it changes the research config hash, as any config change does.
- **Preferred dataset:** one per workspace, not per strategy or per page.
- **AI requests:** synchronous and under the service lock. One external provider kind is
  implemented, and its output is parsed as a JSON array; anything else becomes one rejected
  "unparsed_output" proposal.
- **Mock provider:** keyword routing over three templates; it exists to exercise the pipeline, not
  to discover.
- **Date scope:** recorded with the request and checked against the dataset range. It does not
  restrict the handoff backtest, which runs the whole dataset as before.
- **Lineage:** random-entry control realizations are not stored, so they do not appear in AI
  lineage.

### ADR-64 Research account and automatic prop lifecycle
- **Account is an evaluation environment, not a strategy property.** `run_backtest(..., account=)` (default 50,000 USD) seeds
  `equity_risk`; definitions and `logic_hash` never contain it. All sizing goes through the single floor-only
  `contracts_for_risk` on the MNQ spec (ADR-63).
- **Layers kept separate:** dataset identity, MNQ spec, execution/cost, account, each firm's rules (versioned profiles).
- **Profiles:** `configs/prop/profiles/<ID>.v<N>.yaml` + `REGISTRY.json` (append-only, content hash, never overwritten).
  A profile is VERIFIED only when every required rule value carries evidence `{source, quote, value}`; a flag alone never
  verifies. Unverified profiles produce `RULES NOT VERIFIED` and no pass/fail/payout (fail closed). Outside the research
  config hash.
- **Lifecycle:** `simulate_lifecycle` is a pure function of (trades, profile): evaluation (MLL, daily loss, consistency,
  min days, size limit) -> funded on the next trading day (scaling tiers update at session end only; oversize =
  violation, never clipped) -> payout cycles -> live-transition eligibility. Deterministic payout convention is explicit.
- **Automatic:** `Services._run_cell` computes `prop` for every run; it is stored on the run record. It is read-only
  reporting: it never ranks, selects, or alters the factory manifest, protocol, trial ledger or holdout.
- **Known gaps:** no official rule was verifiable (hosts blocked); Tradeify values null; RISK_QUANTITY_CAP=40 is a research cap.

### ADR-65 Configurable prop rulebook (schema 2) with user-supplied canonical defaults
- **Rules are data.** Profile schema 2 (`edgelab/prop/profiles.py::_validate_v2`) makes every rule a variable: account size,
  starting balances, target, drawdown (max_loss, mode, end-of-day update, lock trigger / locked floor offsets, breach comparison,
  enforcement), DLL (enabled, amount, soft/hard), consistency (percent, window, separate cushion), minimum trading days,
  max micros, funded scaling (start micros, tiers, end-of-session update, basis, persist), payout (frequency, winning-day
  threshold, positive cycle profit, balance requirement, buffer, formula, minimum, cap fixed / by payout number / by purchase
  date, split, count limit, cycle reset, post-payout drawdown, request convention), live-transition rule, purchase date.
  `lifecycle.py` contains no provider number (tested).
- **Defaults:** v2 profiles for LucidFlex 50K, Tradeify Growth 50K, Select 50K -> Flex, Select 50K -> Daily, generated by
  `scripts/prop_default_profiles.py` from the values the user supplied on 2026-09-30 (`basis.status: user_specified`,
  simulated, labelled "not independently verified"). Values the user did not supply are listed in
  `basis.modelling_choices` and on every result. v1 drafts stay registered (history) and still claim nothing.
- **Audit, not a filter:** the lifecycle runs on the SAME chronological trades after every backtest (never a re-run per
  profile), never resizes a trade (oversize = FAILED / INCOMPATIBLE with requested / permitted / rule), and never feeds the
  factory, selection or manifest. The base result covers the whole research period regardless of prop failures.
- **Applicability:** micro limits apply only to runs whose execution contract is the profile's `quantity_unit` (MNQ);
  other runs report NOT_APPLICABLE rather than comparing dataset units with micros.

### ADR-66 Rule-basis model: every prop rule has a value, a status and a basis
- **Schema 3** (`edgelab/prop/profiles.py::RULE_SPEC`, 89 rules): a flat `rules` map, each rule `{value, status, basis}`.
  No rule may be null; a profile is always runnable. Status: VERIFIED (supplied by the user / authoritative project
  evidence), ASSUMED_DEFAULT (EdgeLab fallback, an ACTIVE rule the simulator checks), CUSTOM (a user change; created by
  `profiles.customize`, which writes a NEW version). Rules switched off by a gate (e.g. DLL amount when the DLL is disabled)
  are counted as inactive, not in the verified/assumed/custom counts.
- **Defaults** v3 (`scripts/prop_default_profiles.py`): LucidFlex 50K, Tradeify Growth 50K, Select 50K -> Flex / Daily. v1/v2
  stay registered as history and now report NOT_APPLICABLE (superseded schema, no claim).
- **Per-stage day boundaries**, configurable drawdown measurement (closed balance / worst executable price inside the trade)
  and DLL measurement (closed trades / unrealized / intraday worst price, bar-resolution bound).
- **States:** PASS, FAIL, INCOMPATIBLE, NOT_APPLICABLE per stage and overall; `rule_basis_state` RULE_ASSUMED whenever an
  active rule is ASSUMED_DEFAULT, and every outcome is labelled "UNDER DEFAULT ASSUMED RULES" (never a provider pass).
  IN_PROGRESS is internal only: an evaluation not passed when the data ends is FAIL (NOT_PASSED_BY_END_OF_DATA); a funded
  account not breached when the data ends is PASS (ACTIVE_AT_END_OF_DATA).

### ADR-67 Pre-campaign governance: 10,000-trial protocol version 3, corrected prop metadata
- **Capacity.** Protocol version 3: `DEFAULT_TRIAL_BUDGET = 10_000` (the declared factory universe); holdout looks stay an
  independent budget (default 10). The budget is enforced by the unchanged ledger checks (`_protocol_gate` per evaluation,
  `_protocol_budget_check` per search plan): trial 10,000 is permitted, 10,001 refused.
- **Declared multiplicity family.** `multiple_testing.family_size_rule: declared_max_unique_trials`: Bonferroni per-test alpha
  = 0.05 / 10,000 = 5e-6 at EVERY holdout look (not the growing counted-trial family of version 2, which made early looks
  easier). The bootstrap-t replicate count is derived from the declared family: ceil(25 x 10,000 / 0.05) = 5,000,000
  (>= 25 replicates beyond the tail). Version-1/2 records keep their own rules.
- **Supersession, never mutation.** `create_protocol(..., supersedes=<id>)` replaces the ACTIVE protocol of the scope only if
  it has zero trial events and zero holdout accesses; the new record is fully built and checked first, then the old one is
  retired (material unchanged) and the new material records `supersedes`. `scripts/protocol_supersede.py` (check by default,
  `--apply` to supersede) keeps the dataset, trading-date windows, exposure and holdout-look budget.
- **Prop metadata.** Profiles v4 correct v3's statuses (values identical, simulation identical, tested): an EdgeLab
  interpretation is never VERIFIED (funded start timing, Growth funded contract limit, Select trailing interpretation of
  "EOD", unsupplied balance requirements); the LucidFlex consistency ratio definition is VERIFIED; the Select Daily winning-day
  threshold is inactive in daily mode.

### ADR-68 Frozen-manifest discovery campaign (launcher only)
- **Mechanism.** `edgelab/research/campaign.py` + `research campaign-freeze|campaign-check|campaign-run|campaign-status`.
  `freeze` builds a spec bound to the manifest (id, file sha256s, strategies/catalog/capability hashes, factory/space
  versions), the ACTIVE protocol (id, version, material hash, budget, family rule, holdout-look budget), the resolved
  datasets (content hashes), the discovery period, the config hash and the four prop profiles; `CMP_` id = hash(spec);
  written once to `<data>/campaigns/<CMP>/campaign.json`, never overwritten. It then materializes every manifest row into the
  library (the EXACT manifest definition, compiled and checked against strategy_id / logic_hash / definition_hash;
  factory lineage with manifest id, catalog hash, family_spec_sha256, setup, campaign id). No evaluation.
- **One strategy = one cell = one trial.** Execution is ONE Phase-4 search (`research.batch.run_search`, `workers = 1`,
  `max_cells = n`) over the frozen spec: every strategy's own timeframe resolves to exactly one dataset of the protocol
  source's import (root or derived), so the other timeframes are ineligible cells that never run; MTF features are derived
  inside the evaluation (`features.mtf`); prop profiles audit the same trades. Trial key unchanged:
  (protocol, logic_hash, evaluated discovery-slice content hash, config hash).
- **Discovery only.** Period = [discovery session open, holdout session open - 1 s]; the protocol gate refuses anything else.
- **Resumable.** search id = f(frozen spec, config, protocol); completed cells are skipped, so no second event. Exceptions
  stay failed cells and failed (uncounted) protocol events (ADR-56 semantics), retried next run; the run stops at the first
  failure by default (`--max-failures`).
- **Preflight** (`campaign-check`, read-only): manifest integrity, uniqueness, every row's timeframe/MTF/MNQ binding, protocol
  identity/capacity/family/discovery/config, zero foreign trials, protocol trials = completed campaign cells, zero holdout
  access, datasets present/unchanged/runnable/covering, plan = one eligible cell per strategy, library copies compile to the
  frozen identity, MNQ spec, $50K, cap 40, day-trading engine config, four prop profiles, workers 1, holdout disabled.
- **Found while building it:** `canonical_definition` (the hashing normal form) is not valid DSL input for `exit.trailing`
  without ATR (`atr_period: null`) or `exit.no_progress` (flattened); a library copy saved through `save_strategy` would not
  compile. The campaign therefore stores the exact manifest definition; the DSL is unchanged (changing the normal form would
  change definition hashes). Fixing `save_strategy`'s round trip is a separate task.

### ADR-69 Desktop Research Runs (control and presentation layer over the campaign runner)
- **Entry point:** "Research runs" (nav, and "Run research" on Home) -> `web/src/pages/Runs.tsx` -> `/api/campaigns*`
  -> `Services` -> `research/campaign.py::run_scope` -> `research/batch.run_search` (the same runner as the CLI
  `research campaign-run`, which now calls `run_scope` for the whole campaign). No evaluation logic in the UI.
- **Family scope:** families come from the frozen manifest's catalog (`header.families`, catalog order, never ranked).
  A scope maps to the manifest rows' own strategy ids; "Select all" = every frozen strategy. `run_search(include=)` executes
  only the scope's cells of the SAME frozen search; other cells stay `pending`, the batch ends `partial`. Search id, cell ids,
  trial keys, protocol gate, budget check (counted on the scope's remaining cells) and resume are unchanged.
- **Background job:** `JobManager.start_campaign` (same one-job-at-a-time rule, service lock, cancel flag; workers 1). The
  preflight runs under the service lock; the live status is in memory and served lock-free, so polling never waits for
  the research. Cancel stops between cells; a running cell finishes and is recorded; nothing else is marked done.
- **Persistence:** authoritative results stay in the workspace store (runs, trades, metrics, prop audit in the run record,
  search cells, protocol trial ledger). Run history is a durable JSON record per run in
  `<data>/campaigns/<CMP>/runs/CR_*.json`, rewritten atomically at every step (status, scope, counts, current strategy,
  errors, timestamps, protocol/manifest/search ids). On app start, records left `preflight`/`running` become
  `interrupted` (resumable). Provenance for every strategy result is served from the campaign spec + run record.
