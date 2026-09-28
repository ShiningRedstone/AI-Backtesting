# Changelog

Status labels: **IMPLEMENTED** (code exists) · **TESTED** (covered by automated tests) ·
**NOT IMPLEMENTED** (deliberately absent) · **REQUIRES REAL DATA** (cannot be validated on synthetic data).

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
