

### ADR-70 Research browser, clean names, data-based ETA, and the discovery-window contract
- **Holdout error root cause.** The frozen campaign path never reached the holdout: its period is
  `campaign.discovery_period` = [discovery open, holdout open - 1 s] and `interval_stage` accepts it. The desktop error
  came from the AD-HOC paths: `/api/backtests` ignored any window (the strategy page's "Run backtest" always ran the
  FULL dataset), and the Experiments page defaulted to "full datasets"; under a protocol both are refused with
  HOLDOUT_LOCKED. Contract: the governance layer is unchanged and strict (a windowless or holdout-reaching request is
  refused, never clipped); `Services.protocol_discovery_period` serves THE discovery window as data on `/api/protocols`
  rows; `/api/backtests` accepts an explicit `period`; the strategy page and Experiments default to that window.
- **Research browser** (`campaign.tree`, `web/src/pages/Runs.tsx`): All strategies -> families (catalog order) ->
  strategies (manifest order) with tri-state family checkboxes; a selection is a set of frozen strategy ids
  (`scope_ids(strategy_ids=)`, persisted per run as `<CR>.scope.json`; the UI keeps the current selection in
  localStorage as a convenience). No dataset selection: datasets come from each strategy's frozen timeframe.
- **Presentation** (`strategy/presentation.py`): display name, 1-3 sentence mechanical explanation and key parameters,
  derived deterministically from the frozen `variation`, the definition and the family catalog; identity untouched;
  machine identity shown alongside. No performance wording.
- **Timing + ETA.** `search_cells` gains `started_at`, `finished_at`, `duration_s` (migration via `_add_missing_columns`;
  written by `run_search` for every executed cell). `campaign.estimate_remaining`: per remaining strategy the median of
  the most specific group with >= 3 observations ((family, timeframe) -> timeframe -> all), summed (workers 1);
  fewer than 3 successful observations -> "estimating" with no number. Preflight time is measured separately.
  Progress and ETA are recomputed from the persisted cells at every completed cell and stored in the run record.

### ADR-71 `storage.backend: auto` means the SQLite store
- **Problem.** `open_store` resolved `auto` to DuckDB whenever the `duckdb` package was importable (it is listed in
  `requirements.txt`). Installing requirements therefore silently switched an existing SQLite workspace to a new, empty
  DuckDB store, and the research pages failed with `SearchStorageUnsupported` (search storage is SQLite-only).
- **Decision.** `auto` opens the SQLite store; DuckDB is opened only for an explicit `storage.backend: duckdb`. The
  config files and therefore the research config hash are unchanged; `runtime.inspect_workspace` no longer reports
  `auto` + installed duckdb as a problem. DuckDB remains available and contract-tested when selected explicitly.
- **Also.** `web.bundle.bundle_status` reports an unreadable `static/build-info.json` (e.g. merge-conflict markers) as
  `built: false` with an `error` instead of failing `/api/status`.
