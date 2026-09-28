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
