

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

### ADR-72 Branch-channel updates built by GitHub Actions
- **Problem.** The updater read only `/releases/latest`, compared strict x.y.z versions and refused branches; nothing
  built the Windows app automatically, so a pushed change never reached the installed `EdgeLab.exe`.
- **Build identity.** `packaging/build.py --channel <branch> --build-number <n>` writes both into `edgelab_build.json`
  (and the 4th field of the Windows file version, `n mod 65536`); local builds are channel `local`, build 0. The app
  version stays `edgelab.__version__` (0.2.0): builds of one version are told apart by the build number.
- **Release format.** Manifest schema `edgelab-release/2` adds `channel`, `build_number`, `commit`; tag
  `build-<branch-slug>-<n>`, artifact `EdgeLab-<ver>-b<n>-<platform>.zip`, release key `<ver>-b<n>`. Schema `/1`
  (versioned releases) is still parsed. `packaging/release.py` emits schema 2 for a branch build and writes
  `release-tag.txt`.
- **Discovery and comparison.** `GitHubReleaseSource(channel=)` lists `/releases?per_page=100`, accepts pre-releases,
  keeps manifests of the running build's channel and picks the highest build number; newer = same channel and a
  higher build number (`core.is_newer_release`). Size and SHA-256 verification, safe extraction, the helper-process
  swap with rollback and the READY handshake are unchanged; READY, skip and staging are keyed by the release key and
  the helper also verifies `--build`. The packaged app checks at start and every 30 minutes; running from source
  still refuses to apply (`APPLY_UNSUPPORTED`).
- **UI.** A banner "Update available: build N of <branch>" with **Restart and update** (one click: download, verify,
  apply, restart into the new build) and **Later**.
- **CI.** `.github/workflows/windows-build.yml`: on every push, windows-latest builds from the committed frontend
  bundle, runs the headless packaged smoke test, prepares the release files and publishes a pre-release with
  `GITHUB_TOKEN` (`contents: write`); the newest 5 builds per branch are kept. Branch and run number reach the
  scripts through environment variables only.
- **Requirements.** The repository must be public (the app reads releases without credentials; a private repository
  answers 404, which the updater reports with that hint). A 0.2.0 build made before this ADR only knows
  `/releases/latest`, so the first Actions build is installed once by hand.

### ADR-73 Backtest results views, bootstrapped evaluations, stored controls, risk per trade, seven tabs
- **Read models** (`research/results_view.py`, read-only like `research/overview.py`; thin routes
  `/api/results-view/{overview,strategies/<id>,controls/<id>}`): the field of tested strategies (win rate against
  average reward to risk; net, or gross from one SQL aggregate over stored trades), break-even curves computed in the
  backend (`RR = (1 - WR + c) / WR`, average loser taken as -1R, stated), facts, breakdowns by target / entry /
  trailing / stop / direction / session (median expectancy and survivor rate), the opposite-signal vs fixed-target
  comparison, and per-strategy panels (KPIs, last 12 months of data ending at the last trade, out-of-sample runs,
  holdout kept separate, in-sample rank, prop results, rules in plain English, technical details). Everything is
  in-sample unless labelled; synthetic results are flagged; nothing evaluates, records a trial or touches a ledger.
- **Survivor** (user definition): positive net R per trade AND the run's recorded trade sequence passes an evaluation
  and reaches the first payout under at least one prop rule profile (the chronological audit stored with every run,
  ADR-64/65). A filter on in-sample measurements: never "validated", never a status promotion.
- **Evaluation simulator** (`prop/bootstrap.py`): a seeded block bootstrap of the run's trading days (grouped by the
  profile's own evaluation day boundary; blocks of 5 days; 500 / 1,000 / 2,000 replays), each replay through the
  existing `prop.lifecycle.simulate_lifecycle`. Drawdown what-ifs override only the profile's drawdown mode
  (`eod_trailing`, `static`); an intraday-trailing drawdown is not implemented by the lifecycle and is refused by
  name. Results (chance to pass, of a first payout, of a breach, days to pass, failure reasons, balance paths) are
  cached under `<data>/prop_bootstrap/` by trades hash, profile hash and parameters, computed in a background thread
  with progress, and labelled simulated. The lifecycle module is unchanged.
- **Random controls** returned by `Services.random_entry_control` are also kept as CONTROL records
  (`<data>/controls/*.json`) so the results views can plot them; they are never runs, strategies, trials or ledger
  entries.
- **Risk per trade ($)** is a workspace preference (default 250) outside the research config and its hash; dollar
  figures in the results views are R x amount. Backtests, sizing and records are unchanged.
- **Navigation.** Seven tabs (Home, Strategies, Run backtest, Backtest results, Prop firm simulator, Paper trading,
  Settings) with sub-views; every old route still resolves to its tab; AI Discovery keeps its route without a menu
  entry. On-screen text is plain English (`web/src/app/labels.ts`); machine identifiers and raw keys appear only under
  collapsed "Technical details". App event timestamps are shown in the computer's local time; market, bar, trade and
  backtest-period times keep their dataset convention. The research tab no longer shows an estimated time remaining
  (`campaign.estimate_remaining` and its tests stay).
