# EdgeLab web application (Phase 3.5, Research page Phase 4)

A graphical Strategy Builder and research workspace on top of the Phase 1–3 backend. The
browser is a presentation and input layer. The Python services stay authoritative for
validation, canonicalization, hashing, compilation, lineage, variation generation and
backtesting.

```
Browser (React + TypeScript)      edits a DSL document; renders what the backend returns
      │  JSON over HTTP
edgelab/web/app.py (Flask)        routing, input checks, error mapping. No strategy logic.
      │
edgelab/services.py               Phase 2/3 service contracts (+ Phase 3.5 web, Phase 4 research)
      │
DSL · compiler · lineage · Mode A │ feature engine │ Phase 1 backtest engine + run registry
```

## Start

```bash
pip install -r requirements.txt          # numpy, pandas, pyyaml, scipy, flask
python -m edgelab.web                    # your workspace (current directory) -> http://127.0.0.1:8765
python -m edgelab.web --demo             # separate synthetic demo workspace in ./demo_workspace
```

Options:
- `--root DIR`: the project root, which must contain `configs/`.
- `--port N`, `--host H`: where to serve.
- `--allow-remote`: required to bind to anything other than loopback. The app has **no authentication**.

The built frontend is committed in `edgelab/web/static/`, so running the app needs **no Node.js**.

### Demo mode

`--demo` creates a separate project root, marked by a `DEMO_WORKSPACE` file. It contains:
- **Datasets:** two synthetic 5-minute datasets imported through the normal pipeline. One is NQ futures-like (configured futures costs, so it can be backtested). The other is NAS100 CFD-like (costs unconfigured, so the UI demonstrates the refusal).
- **Strategies:** the eight fixture strategies.

Every screen shows *"Synthetic demonstration — not evidence of trading performance."* Demo runs live in the demo workspace's own store, so they can never mix with real research results. The Results page also separates synthetic runs from research runs in any workspace. The demo refuses to write into a directory that is not a demo workspace.

## Pages

| Page | What it does | Backend |
|---|---|---|
| Dashboard | Backend status, software/source version, frontend build, last recorded test run, counts, quick actions (each enabled only when its prerequisites exist) | `system_status` |
| Strategies | Library with filters: open, edit, duplicate, explain, variations, lineage, archive/restore (with confirmation) | `StrategyLibrary` |
| Strategy page | Overview and backend `explain()`, lineage, Generate Variations, Backtest | Phase 3 services |
| Strategy Builder | Visual editor for the DSL: General · Market & Sessions · Parameters · Entry · Exit · Sizing · Review, with live backend validation and DSL preview | `render_strategy`, validator, compiler |
| Families | Hypotheses and their instances, as a lineage tree and table | `family_detail` |
| Variations | Mode A batches with reproducibility metadata | batch records |
| Datasets | Library with provider/instrument/timeframe, validation status, cost status and an **Eligible** column (with the reasons a dataset cannot run); metadata, validation report and caveats; import over the existing pipeline | Phase 2 importer, `backtest_readiness` |
| Results | Recorded single backtests (status `IN_SAMPLE`); synthetic runs listed separately | Phase 1 run registry |
| Research | Phase 4 batch search: spec setup and check, plan preview, background job with progress and cancel, searches list, current and historical cells, in-sample ranking, shortlist (see below) | `/api/research/*` |
| Prop Simulation | Choose a stored run and one or more accounts (each with a rule set from `configs/prop/` or custom YAML, optional start), run, then view the source strategy result and the prop-account results side by side but separately: outcome, breaches, violations with detection mode, day table and per-trade progression. Recorded simulations are listed. See PROP_SIMULATION.md | `/api/prop/*` |
| AI Discovery | Placeholder: no model is connected | — |
| Settings | Read-only configuration: cost profile status, engine config, sessions, instruments | config |

## Research (Phase 4)

`#/research` sets up a search over the Phase 4 services; `#/research/SRCH_...` opens one.

- **Setup:** strategies, variation batches, families, typed `PB_` proposal-batch ids, datasets (with
  their readiness reasons), period (full, common, or explicit with a timezone), ranking metric and
  sample floor, max cells, seed. The exact JSON spec sent is shown. **Check spec** reports every
  problem; **Preview plan** shows counts, warnings and each cell as eligible or ineligible with
  reasons (a search above max cells is refused, never truncated); **Start search** starts a
  background job.
- **Job:** state, progress counts (evaluated, pending, failed, skipped, cancelled, trials) polled
  every 2 s until a final state; **Cancel search** lets the running cell finish and starts no new
  one. Job ids live in the server process (after a restart the stored search remains, marked
  `interrupted`).
- **Search page:** accounting (last invocation and cumulative trials), current cells (ineligible and
  failed cells stay visible with reasons), historical cells in a separate table (not counted, never
  ranked), the in-sample ranking with the backend's "NOT VALIDATED" label and trial count, and the
  shortlist (a research tag only; no run status changes).

## Strategy Builder

- **One format.** The builder state *is* the DSL document (`STRATEGY_DSL.md`). There is no UI-specific schema and no frontend serializer: the YAML preview, canonical form and identity are rendered by the backend on every edit (debounced), and the Validate button calls the same validator as the CLI.
- **General.** Name, description, and the family (id, name, hypothesis, category). The UI notes that these do not change the logic identity.
- **Market & Sessions:**
  - Strategy timeframe (from `configs/web.yaml` plus the timeframes of imported datasets), or a `$timeframe` parameter.
  - One trading window (the DSL supports one) and a trading-date weekday filter.
  - The configured sessions (read-only), plus strategy-local sessions.
  - Session flatten, shown read-only because it is engine configuration.
- **Parameters.** Integer, float, boolean, choice and timeframe, with value/min/max/step/choices. Renaming updates every `$reference`; a reference counter warns before deleting a used parameter.
- **Conditions:**
  - Structure: ALL/ANY groups, nesting, NOT, per-condition enable toggles (`always`, `disabled`, or `when $flag`) and labels.
  - Operators: exactly the DSL list, including `crosses_above` and `crosses_below`.
- **Operands.** Constant, parameter, bar field (with lag), feature, or arithmetic (add/sub/mul/div, nested two levels). Any numeric slot can be fixed or a `$parameter` of a compatible type.
- **Features:**
  - The picker is built from the backend registry: categories, outputs with docs, parameters with defaults and types, and session-typed parameters rendered as session pickers.
  - The timeframe menu only offers higher timeframes that are exact multiples of the strategy timeframe, as computed by the backend.
  - The UI explains that higher-timeframe values use the last completed higher-timeframe bar.
- **Entry.** Long, short or both; market, stop or limit, with per-side price operands and expiry; signal cooldown; per-side condition trees.
- **Exit:**
  - Stops: points, ATR (optional higher timeframe) or price. "None" is shown as unavailable, because a stop is mandatory.
  - Targets: none, points, ATR, price or R-multiple.
  - Time exits: time stop and max hold.
  - Signal exits per side.
  - Unsupported concepts (trailing stop, breakeven, partial exits, pyramiding) are shown disabled, with the backend's reason.
- **Sizing.** Fixed quantity or risk-based (risk budget, optional maximum). Instrument minimum size and step are applied when bound to a dataset; nothing broker-specific is in the form.
- **Review.** Validate (errors and warnings with DSL paths and suggestions), then Compile & Explain (backend `explain()`).
- **Save:**
  - **Save Strategy** on a new draft stores it with method `user`.
  - On a loaded strategy it stores a `manual_edit` child. If the logic is unchanged, nothing is saved and the UI says why: identity is the logic hash.
  - **Save As New** stores the draft without a parent. **Duplicate** loads an unsaved copy; saving it records a `duplicate` lineage.
  - Invalid strategies cannot be saved: the backend refuses them with path-aware issues.
  - Unsaved drafts are kept only in this browser's local storage. The library stores only valid, canonical strategies.
- **DSL preview.** Draft YAML, backend canonical YAML (once valid) or JSON; copy, download, and load a YAML/JSON file (parsed by the backend with `yaml.safe_load`).

## Variations (Mode A) in the UI

On a saved strategy, **Generate Variations** lists the declared parameters:
- **Numeric:** a range (min/max/step) or explicit values.
- **Choice, timeframe, boolean:** checkboxes.
- **Every dimension:** a category.

Modes are grid, one-at-a-time, and a seeded random sample. The combination count and the cap are computed by the backend before anything runs (`/api/variations/preview`). Oversized or out-of-domain specs are shown as refused and the Generate button stays disabled; nothing is silently truncated.

The results show combinations, unique strategies, duplicates, those identical to the base, and the batch ID. The table lists each variant's parameter values, with Open, Lineage, and a side-by-side Compare. It has no performance columns.

## Backtests

The Backtest tab lists every dataset with its instrument, asset type, provider, timeframe, range, bars, price basis, validation status and cost status. Only rows that are validated, timeframe-compatible and cost-configured can be selected; every other row shows its reasons.

For CFDs with unconfigured costs, the UI states that the backtest is unavailable because the broker/provider cost profile is unconfigured, and nothing is invented.

A run goes through `backtest_strategy(record=True)`: the Phase 1 engine with its causality check, recorded in the Phase 1 run registry (status `IN_SAMPLE`). Results show the backend metrics with the sample-size label, exit reasons and signal diagnostics. There are no rankings, significance verdicts or edge labels.

## HTTP API

Every route is a thin call into `edgelab.services`.

| Method | Path | Service |
|---|---|---|
| GET | `/api/health`, `/api/status`, `/api/config`, `/api/options`, `/api/features` | status, config, `builder_options`, feature registry |
| POST | `/api/strategies/render` · `validate` · `compile` · `explain` | `render_strategy`, `validate_strategy`, `compile_strategy`, `preview_strategy` |
| POST | `/api/dsl/parse` | `yaml.safe_load` → mapping (parse only) |
| GET | `/api/strategies[?family=&archived=1]`, `/api/strategies/{id}`, `…/{id}/explain`, `…/{id}/lineage` | library, `preview_strategy`, `strategy_lineage` |
| POST | `/api/strategies/save` `{definition, parent_strategy_id?, method?}` | `save_strategy` (`user` / `manual_edit` / `duplicate`) |
| POST | `/api/strategies/{id}/duplicate` · `archive` · `restore` | `duplicate_strategy`, `archive_strategy`, `restore_strategy` |
| GET | `/api/families`, `/api/families/{id}` | `strategy_families`, `family_detail` |
| POST | `/api/variations/preview`, `/api/variations` | `variation_preview`, `generate_variations` |
| GET | `/api/variation-batches`, `/api/variation-batches/{id}` | batch records |
| GET | `/api/datasets`, `/api/datasets/{id}` | `backtest_readiness`, `dataset_detail` |
| GET/POST | `/api/import/files`, `/api/import/inspect`, `/api/import` | Phase 2 `inspect_file` / `import_file` |
| POST | `/api/backtests/readiness`, `/api/backtests` | `backtest_readiness`, `backtest_strategy(record=True)` |
| GET | `/api/results`, `/api/results/{run_id}` | run registry |
| GET | `/api/results/report?run_ids=RUN_...,RUN_...` | Phase 5 descriptive report over stored runs of one strategy (no UI page yet) |
| GET | `/api/prop/configs`, `/api/prop/simulations`, `/api/prop/simulations/{PROP_id}` | `prop_configs`, `list_prop_simulations`, `get_prop_simulation` |
| POST | `/api/prop/validate` `{config}` · `/api/prop/simulate` `{run_id, accounts: [{account_id?, config, start?}], record?}` | `validate_prop_config`, `prop_simulate` (reads the run; never writes it) |

Errors are returned as `{"error": {"kind", "message", "issues"?, "reason"?, "details"?}}`. The kinds are `validation`, `compile`, `variation`, `cost_unconfigured`, `backtest`, `prop_config`, `prop_data`, `import_failed`, `not_found`, `bad_request`, `forbidden`, `parse`, `invalid_request` and `internal`. The UI shows `message`, `reason` and `issues`; stack traces appear only under **Technical details**.

## Security

- **Local, single user.** The app binds to loopback by default; anything else needs `--allow-remote`, and there is no authentication.
- **Data only.** Strategy sources from the browser must be a JSON object or a `STR_<12 hex>` id. The service layer's file-path and YAML-text inputs are not reachable over HTTP, and nothing executes code.
- **Imports.** Imports read only from `web.import_dirs` (default `data/import`), and only real `ImportOptions` fields are accepted.
- **No credentials, broker access or order execution.** Credentials remain environment-only, as described in CONFIG.md.

## Development

```bash
cd web
npm install            # react, react-dom, esbuild, typescript, @types/react (network needed)
npm run build          # -> edgelab/web/static/{app.js, styles.css, index.html, build-info.json}
npm run watch          # rebuild on change
npm run typecheck      # tsc --noEmit
```

- **Offline builds.** `build.mjs` falls back to globally installed packages, which is how this build was made offline.
- **Type shim.** `@types/react` could not be installed offline, so `web/src/types/react-shim.d.ts` types the React APIs used. Delete it after `npm install`.
- **Bundle freshness.** `build-info.json` records a SHA-256 of the sources, and a test fails if the committed bundle is stale.

## Tests

```bash
python scripts/run_tests.py               # full suite; records reports/last_test_run.txt (shown on the dashboard)
python -m unittest tests.test_web_api     # API contracts (Flask test client)
python -m unittest tests.test_web_e2e     # real server + headless Chromium (skips without Playwright)
python -m unittest tests.test_research_api  # Phase 4 research routes (jobs held mid-cell by a gate)
```

The browser tests need `pip install playwright` and a Chromium build (`playwright install chromium`, or set `PLAYWRIGHT_BROWSERS_PATH`).

## Known limitations

- The Flask development server is single-process. Requests are serialized behind one lock (the SQLite store and services are single-user), so a long backtest delays other calls until it finishes. There is no progress reporting beyond busy states.
- Variation generation and single backtests are synchronous requests. Batch searches run as background jobs (one at a time, sequential); worker processes are available from the CLI (`research run --workers N`).
- The Research page takes proposal batches as typed `PB_` ids, and research needs the SQLite result store (see `ARCHITECTURE.md`, Phase 4 limitations).
- Dataset delete/archive is not offered, because stored datasets are immutable and referenced by runs. There is no in-browser file upload: files are imported from the import folder.
- Typing uses a local React shim instead of `@types/react` (offline build).
- The DSL supports one trading window per strategy; several windows need a local session or session-feature conditions.
- The builder offers the timeframes in `configs/web.yaml` plus those of imported datasets; the backend accepts any `Nm`/`Nh`.
