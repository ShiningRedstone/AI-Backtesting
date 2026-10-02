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

## Design system (research terminal)

One dark theme by design (`web/src/styles.css`, `color-scheme: dark`). Tokens live on `:root`:
- Surfaces are near-black and charcoal (`--bg`, `--surface`, `--surface-2`, `--surface-3`), with hairline borders and soft glows instead of heavy shadows.
- Text is light (`--text`), with soft grey for secondary copy (`--muted`, `--faint`).
- The primary action is a **silver metallic pill**; secondary actions are dark pills; destructive actions use the error colour.
- The selected state uses light text on a raised surface (`--accent` is neutral silver, never a status).
- A warm accent (`--warm-*`, `--warm-gradient`) is decorative only: at most one featured surface per screen (the System facts panel on Home).
- Status colours are reserved: green `--ok` for positive and passed, amber `--warn`, red `--error` for failures and negative values, blue `--info`, violet `--demo` for synthetic.
- Chart series use `--c1` (green) · `--c2` (blue) · `--c3` (ochre), with their earlier CVD-validated values; each keeps at least 3.4:1 contrast on `--surface`.
- Negative values sit below the zero line (position first, colour second), and every chart shows values in text and tooltips, so colour never carries meaning alone.
- Status badges, banners and toasts also carry an icon next to their text.

**Type and motion:**
- **Inter** (variable; also registered as *Inter Display*, using the optical-size axis for display headings) and **JetBrains Mono**, used for IDs, hashes, DSL and numeric inputs. Mono uses slashed zero and has ligatures off, so `==` reads as typed.
- Both fonts are self-hosted in `web/src/fonts/` with their SIL OFL texts; `build.mjs` copies them to `edgelab/web/static/fonts/`. There are no third-party requests.
- Tabular figures apply to tables, KPIs and `.num` only.
- Only Home uses display type and the static glow/grid hero; work screens stay compact.
- Transitions are 150–200ms and are disabled under `prefers-reduced-motion`.
- Truncated hashes show their full value on hover.

Building blocks (`web/src/components/`):
- `ui/`: buttons, inputs, badges, cards, `Kpi` tiles with meters, `Scope` tags, `Modal`, `Drawer`, `Pager`,
  `SortTh` (server-side sort + resizable columns), number formatters (`r()` always prints the R unit and sign).
- `charts.tsx`: SVG chart kit, no chart library: `LineChart` (crosshair tooltip, legend toggles, areas),
  `BarChart` (grouped, signed), `HBars` (signed breakdown bars), `Histogram` (with candidate marker),
  `MonthHeatmap`, `PathsChart` (Monte Carlo / prop paths). Responsive via `ResizeObserver`.
- `research.tsx`: protocol budget panel, dataset identity (incl. "not CME NQ futures" for Dukascopy),
  execution sides (long ASK→BID, short BID→ASK), pipeline strip, random-control presentation, rules in
  plain English (unsupported semantics are named as not supported, never implied).
- `analytics.tsx`: per-run panels (performance, equity/underwater/rolling expectancy, trade behaviour,
  cost sensitivity, Monte Carlo) used by the strategy drawer and the run page.
- `updates.tsx`: one shared update-status poller, the version chip, the update dialog and the Settings panel.

**Labelling rule:** every number is tagged with its basis (*Net of costs* / *Gross*) and scope (*Discovery /
in-sample*, *Out-of-sample*, *Walk-forward*, *Holdout*, *Simulated*, *Randomized control*, *Synthetic data*).
A protocol holdout-evaluation run is stored with status `OUT_OF_SAMPLE` but is always shown as **Holdout** and
never counted or aggregated as an ordinary OOS test. Win rate is shown but never used to rank.

## Page map

```
Home               Home (#/)
Strategies         Families (#/families, default) · Library (#/strategies[/id]) · Builder (#/builder) · Variations (#/variations)
Run backtest       Single backtest (#/run) · Research runs (#/runs) · Experiments (#/research[/SRCH_..])
Backtest results   Overview (#/dashboard) · Strategies (#/explorer, drawer ?open=STR_..) · Holdout results · Random controls
                   (#/controls); run detail #/results/RUN_.. (links only); #/compare, #/pipeline -> Overview (ADR-90)
Prop Trading       Paper accounts (#/paper) · Start paper trading (#/paper/new) · Backtest prop check (#/prop)
Settings           Settings (#/settings, incl. risk per trade and updates) · Data (#/datasets)
(no menu entry)    AI Discovery (#/discovery)
```

Every old address still opens its page inside the right tab. On-screen text is plain English (`web/src/app/labels.ts`):
ids, hashes and raw keys appear only under a collapsed **Technical details**. App event times (created, started,
checked, built) are shown in the computer's local time; market, bar, trade and period times keep the dataset's
convention.

Backtest results views (ADR-73, read-only, `research/results_view.py`): `/api/results-view/overview`
(`basis=net|gross`, `controls=0|1`), `/api/results-view/strategies/<id>`, `/api/results-view/controls/<CTRL_..>`;
the evaluation simulator `POST /api/prop/bootstrap` (`run_id`, `profile_id`, `n`, `mode`; returns the cached result
or the background job's progress); `GET|POST /api/preferences/risk-per-trade` (display only, outside the config hash).

Read models behind the new pages (all read-only, `research/overview.py`; they never evaluate, record a trial,
touch a ledger or change a protocol): `/api/overview`, `/api/explorer/strategies` (server-side filter / sort /
paging; `scope=in_sample|oos|walk_forward|any`), `/api/research/dashboard`, `/api/results/<run>/analytics`,
`/api/strategies/<id>/pipeline`, `/api/pipeline`, `/api/protocols`, `/api/protocols/<id>` (there is deliberately
**no** HTTP route that creates, edits or retires a protocol or resets a ledger). Updates: `/api/version`,
`/api/update/{status,check,skip,unskip,later,preferences,download,apply}` (DESKTOP_PACKAGING.md "Updates").

## Pages

| Page | What it does | Backend |
|---|---|---|
| Home | System facts (strategies, runs by status, batches, AI generations, prop simulations, datasets, version/update), the ACTIVE protocol with trial and holdout-look budgets and the Bonferroni per-test α, research dataset identity and quality, execution & cost model, latest results (scope-tagged), recent experiments, candidates (shortlist tags, holdout ledger), warnings. Never labels anything profitable | `/api/overview` |
| Research dashboard | Scope switch (IS / OOS / walk-forward / all), market/dataset/family filters, breakdowns by market, timeframe, session, entry, stop, target, direction, family, source (chart or table, resizable), distributions of expectancy, profit factor, drawdown, trade count, win rate, pooled weekday/month/year, cost share; synthetic and holdout runs excluded unless stated | `/api/research/dashboard` |
| Explorer | Terminal filter bar (text, id, family, market, TF, session, entry, stop, target, direction, source, state, protocol, min trades, max trades/week), active-filter chips + reset, server-side sort/paging, resizable columns, row selection → Compare; right-side drawer: identity, rules in plain English, performance, equity, trade behaviour, robustness (OOS/WF runs, holdout evaluations with their control), pipeline. Filters persist in the URL | `/api/explorer/strategies`, `/api/results/<id>/analytics`, `/api/strategies/<id>/pipeline` |
| Controls | What a random-entry control preserves/randomizes; formal controls from holdout evaluations with the exact Monte-Carlo p-value the protocol stored; running an ad-hoc control (a gated discovery evaluation) shown as the candidate on the control distribution with a descriptive percentile (no p-value outside a holdout evaluation) | `/api/pipeline`, `/api/validation/control` |
| Candidate pipeline | Stage counts (hypothesis → … → human review) and every candidate beyond in-sample testing with its stage states and evidence, all derived by the backend | `/api/pipeline` |
| Prop Trading | Paper accounts (daily forward trading in simulated prop accounts: attempts, fees, payouts, net; ADR-81), batch start, Backtest prop check | `/api/paper/*`, `/api/prop/*` |
| Strategy Lab (library) | Library with filters: open, edit, duplicate, explain, variations, lineage, archive/restore (with confirmation) | `StrategyLibrary` |
| Strategy page (Strategy Lab) | **Research** hub (version + provenance, workflow steps, stored runs with scope badges, run this version + a variation batch on datasets), Overview and backend `explain()`, Backtest, Generate Variations (exact combinations previewed), **Validate** (OOS split, walk-forward, random-entry control on the whole dataset or the OOS window), Lineage | Phase 3/4/5 services, `strategy_research`, validation services |
| Compare | Stored runs of a lineage, a version, a variation batch or a search side by side: trades, gross/net/cost R, expectancy, profit factor, max drawdown, breakeven cost multiple, parameters, scope, cost status, prop count. Sort and filter (views, not rankings); selected run → validate or prop simulation | `compare_runs` |
| Strategy Builder | Visual editor for the DSL: General · Market & Sessions · Parameters · Entry · Exit · Sizing · Review, with live backend validation and DSL preview | `render_strategy`, validator, compiler |
| Families | Hypotheses and their instances, as a lineage tree and table | `family_detail` |
| Variations | Mode A batches with reproducibility metadata | batch records |
| Datasets | Library with provider, instrument + **source identity** (configured / provisional, research proxy), timeframe, range, validation, source content hash, price/volume semantics, cost status, caveats and an **Eligible** column (with the reasons a dataset cannot run); **Preferred Research Dataset** card and "Set preferred"; metadata, validation report, identity and **gap classification & coverage**; import over the existing pipeline (incl. dataset name, symbol, calendar, derived timeframes) | Phase 2 importer, `backtest_readiness`, `dataset_quality`, `/api/preferences/research-dataset` |
| Results | Recorded runs (any status); a run page shows its scope (Holdout for a protocol holdout evaluation), KPIs, an **Integrity / provenance** section (strategy logic/definition hashes, dataset content hash, period, config hash, code version, trades hash, causality check, execution and cost scenario/basis), headline metrics, equity and drawdown curve, session / entry-hour / cost-sensitivity breakdowns, then year/weekday/month/hour/direction breakdowns, monthly heatmap, underwater and rolling expectancy, R and holding-time distributions, streaks, exits, trade frequency, quote sides, cost sensitivity and Monte Carlo resampling | run registry, `run_curve`, `research_report`, `/api/results/<id>/analytics` |
| Experiments (`#/research`) | Phase 4 batch search (a search page also shows its protocol's program-wide trial and holdout-look budget): spec setup and check, plan preview, background job with progress and cancel, searches list, current and historical cells, in-sample ranking, shortlist (see below) | `/api/research/*` |
| Prop Simulation | Choose a stored run and one or more accounts (each with a rule set from `configs/prop/` or custom YAML, optional start), run, then view the source strategy result and the prop-account results side by side but separately: outcome, breaches, violations with detection mode, day table and per-trade progression, plus a simulated-evaluation summary (survived, target reached, breaches, payout eligibility, days to target, outcome distribution) and per-account simulated balance paths against the drawdown floor and target, and drawdown trajectories — all labelled *Simulated*, never a prediction of real funding. Recorded simulations are listed. See PROP_SIMULATION.md | `/api/prop/*` |
| AI Discovery | Hypothesis-, template- or modification-based proposals from an AI provider (or the deterministic mock), each through the strict gate, then human review: Inspect, Accept, Reject, Save to library, Send to Backtest; history of requests; the old proposal-batch import as a tab (see "AI Discovery (Phase 9)") | `/api/ai/*`, `ingest_proposals` |
| Settings & about | **About** (version of UI and backend, build mode, build date, commit, architecture, workspace, data root, backend, database, protocol, settings file), **Updates** (installed/latest version, last check and result, release source, install and staging folders, update log, start-up check preference, download / restart and update / skip), system status and quick actions, **Research Workspace** (desktop app: current folder with validity, SQLite, read/write, dataset/run/strategy/prop counts; check a folder read-only, use it, create a new one, open the default; development server: shows its fixed `--root`); read-only configuration: cost profile status, engine config, sessions, instruments | `/api/workspace*`, config |
| Welcome (desktop, first run) | Shown instead of the pages while no research workspace is selected: open an existing workspace or create a new one | `/api/workspace*` |

## Strategy Lab workflow (Phase 8)

The GUI is the front door; the stored DSL definition stays the single source of truth. Every button calls an
existing service and the research engine; the UI never holds a second strategy format.

1. **Start**: Strategy Lab → open a strategy (Research tab), or New / Duplicate / Edit in the builder.
2. **Edit**: parameters (value, domain, step), entry and exit rules, stop, target, sizing, session and cooldown
   through the builder's controls (only constructs the compiler supports). Live backend validation shows
   errors with paths and the canonical strategy id; unsupported semantics are refused by name.
3. **Save**: a changed rule saves a NEW version with lineage (parent id and parent definition hash);
   stored versions are never modified. Cosmetic edits keep the same id.
4. **Dataset**: pickers list validated datasets with provider, instrument, timeframe, validation, cost
   profile/status, caveats and eligibility; ineligible datasets are shown with reasons and cannot be selected.
5. **Backtest** → run page (equity/drawdown, breakdowns, breakeven).
6. **Variations**: one-at-a-time, grid, or seeded random sample inside declared domains, with a cap; the
   exact combinations are listed before generating; the backend dedupes logic.
7. **Batch research**: Research tab → run this version + a variation batch on chosen datasets (the Phase 4
   background job, progress and cancel on the Research page).
8. **Compare**: unranked metric table; selecting a run leads to Validate or Prop.
9. **Validate**: OOS split, walk-forward, random-entry control (whole dataset or OOS window). OOS and
   walk-forward windows can be recorded as runs (OUT_OF_SAMPLE / WALK_FORWARD); controls are never runs.
10. **Prop simulation** on any stored run (e.g. an OOS window).

Scope is always visible: in-sample results are labelled exploratory; out-of-sample, walk-forward, random
control and prop results are shown as separate things with their labels. Nothing is ranked, scored,
promoted or called a winner.

Synchronous calls: backtests and validations run in the request (the page shows a busy state); only batch
searches are background jobs.

## Preferred Research Dataset (Phase 9)

A workspace-level default for NEW research, set on the Datasets page ("Set preferred") and stored in
`data/workspace_preferences.json`. Only a stored dataset that passed validation can be chosen.

- The Strategy Lab backtest, batch-research and validation pickers, and AI Discovery, preselect it
  when it is eligible for the strategy. When it is not eligible, a banner gives the reasons and
  nothing is preselected.
- A research-dataset strip shows the data identity: **Research Dataset / Provider / Instrument /
  Timeframe**, with the cost status and a "provisional identity" badge where that applies.
- Changing or clearing it never modifies stored runs, strategies or datasets, and it is never
  changed automatically (for example, importing a new dataset does not change it).

## AI Discovery (Phase 9)

The flow is: request, then the blind context, then the provider, then the strict gate, then your
review, then the library. After that come the existing Backtest / Variations / Compare / OOS /
Walk-forward / Control / Prop tools.

**The request**

- **Mode A:** a plain-language hypothesis plus constraints.
- **Mode B:** a family template (`ema_trend`, `rsi_reversion`, `range_breakout`) plus constraints.
- **Modify:** an existing library strategy becomes a NEW version with its parent recorded; the
  original is never changed.
- **Scope:** the research dataset (default: the preferred one), which fixes the instrument and
  timeframe; a session; a direction; an optional date scope, recorded with the request.
- **Constraints:** allowed features, entry orders, stops, targets, exits, sizing, maximum
  conditions/cooldown/parameters, and parameter limits. Only DSL-supported constructs can be
  chosen.
- **Size:** 1-10 proposals per request.
- **Refusals:** the request is refused (not repaired) for performance-claim language, code, or
  unknown keys.

**The blind context** ("Show what the AI sees") is versioned and hashed. It holds:

- dataset identities and coverage, and the preferred dataset;
- instrument identity metadata and cost-status caveats;
- the DSL menu: features, constructs, sessions, parameter domains;
- and, when modifying, the base definition.

It contains **no** results: no runs, metrics, OOS, walk-forward, control or prop output. It is built
without reading the run registry, so its hash does not change when runs are recorded. Nothing feeds
results back into generation, and there is no automated loop.

**Providers**

- `mock`: always available, deterministic, not an AI. It returns one valid, one malformed (code plus
  a performance field), one causality-invalid (reads the next bar) and one parameter-invalid
  (value outside its domain) proposal.
- An external provider is optional and configured only by environment variables:
  `EDGELAB_AI_PROVIDER=anthropic`, `EDGELAB_AI_MODEL=<model name>` and `ANTHROPIC_API_KEY`. No
  model is assumed. The key is never stored in a workspace, strategy, run or request record, and
  the API reports only whether it is present.
- With no external provider, the page shows an offline notice.

**The gate** runs these stages, each reporting exact reasons, and never repairs anything:

schema → dsl_validation → supported_features → causality → parameter_domain → request_constraints →
compile → identity.

- Content must agree with its own definition (name, family, timeframe, session, parameter values).
- Compile and identity run only when all earlier stages pass.
- Identical logic already in the library gives a warning (saving adds a lineage record, not a new
  strategy). Identical logic within one generation is rejected.

**Review**

- Proposals appear in provider order; there is no ranking, score or "best".
- Each shows its hypothesis, entry, exit, stop, target, timeframe, session, parameters, complexity,
  per-stage gate status, strategy id / definition hash and rejection reasons.
- **Accept** is available only on gate-valid proposals. **Save** stores an accepted proposal as an
  ordinary library strategy:
  - `generation_method` is `mode_b_proposal`, or `mode_b_modification` with `parent_strategy_id`,
    AI-stated changes and EdgeLab-computed changes;
  - `generation_parameters` hold the proposal id, request id, generation id, provider, context
    hash, hypothesis and scope.
- **Send to Backtest** opens the Strategy Lab backtest with the preferred dataset preselected.

**Records**

- `data/ai_discovery/generations/AIG_*.json` holds the request, context hash, provider, raw-output
  hash and every proposal with its gate report. Records are immutable.
- `data/ai_discovery/decisions/AIP_*.json` holds your decisions, which are append-only.
- `/api/ai/proposals/{id}/lineage` shows AI request → proposal → strategy version → runs, with each
  run's dataset, provider, instrument, timeframe, cost status, hashes and status, plus its prop
  simulations. Random-entry controls are returned, not stored, so they do not appear there.
- The previous proposal-batch import (Phase 3 Mode B gate) remains as the "Import proposal batch"
  tab.

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
| GET | `/api/workspace` | current workspace (read-only on the development server) |
| POST | `/api/workspace/inspect` `{path}` · `/api/workspace/select` `{path}` · `/api/workspace/create` `{path}` · `/api/workspace/browse` | desktop app only (`edgelab.workspace_host`): read-only check, switch (persisted), create in an empty folder, native folder dialog |
| GET | `/api/strategies/{id}/research` | `strategy_research` (machine-readable provenance + stored runs) |
| GET | `/api/compare?source=lineage\|strategy\|batch\|search\|runs&id=...` | `compare_runs` (unranked) |
| GET | `/api/results/{run_id}/curve` | `run_curve` |
| POST | `/api/validation/oos` `{strategy, dataset_id, split_at, record?}` · `/api/validation/walkforward` `{strategy, dataset_id, train_months, test_months, anchored?, record?}` · `/api/validation/control` `{strategy, dataset_id, n_controls, seed, split_at?}` | `evaluate_oos`, `walk_forward`, `random_entry_control` / `oos_random_control` (synchronous) |
| GET/POST | `/api/proposals/menu`, `/api/proposals/ingest` `{batch, save}` | `proposal_menu`, `ingest_proposals` |
| GET/POST | `/api/preferences/research-dataset` (POST `{dataset_id}`; `null` clears) | `preferred_dataset`, `set_preferred_dataset`, `clear_preferred_dataset` |
| GET | `/api/datasets/{id}/quality` | `dataset_quality` (gap classification + coverage; read-only) |
| GET | `/api/ai/status`, `/api/ai/generations`, `/api/ai/generations/{AIG_id}`, `/api/ai/proposals/{AIP_id}/lineage` | `ai_status`, `ai_generations`, `ai_generation`, `ai_lineage` |
| POST | `/api/ai/context` `{request}` · `/api/ai/generate` `{request}` · `/api/ai/proposals/{AIP_id}/decision` `{decision, note?}` · `/api/ai/proposals/{AIP_id}/save` | `ai_context`, `ai_generate`, `ai_decide`, `ai_save` |
| GET | `/api/prop/configs`, `/api/prop/simulations`, `/api/prop/simulations/{PROP_id}` | `prop_configs`, `list_prop_simulations`, `get_prop_simulation` |
| POST | `/api/prop/validate` `{config}` · `/api/prop/simulate` `{run_id, accounts: [{account_id?, config, start?}], record?}` | `validate_prop_config`, `prop_simulate` (reads the run; never writes it) |

Errors are returned as `{"error": {"kind", "message", "issues"?, "reason"?, "details"?}}`. The kinds are `validation`, `compile`, `variation`, `cost_unconfigured`, `instrument_identity`, `ai_request`, `ai_scope`, `ai_provider`, `backtest`, `prop_config`, `prop_data`, `import_failed`, `not_found`, `bad_request`, `forbidden`, `parse`, `invalid_request` and `internal`. The UI shows `message`, `reason` and `issues`; stack traces appear only under **Technical details**.

## Security

- **Local, single user.** The app binds to loopback by default; anything else needs `--allow-remote`, and there is no authentication.
- **Data only.** Strategy sources from the browser must be a JSON object or a `STR_<12 hex>` id. The service layer's file-path and YAML-text inputs are not reachable over HTTP, and nothing executes code.
- **Imports.** Imports read only from `web.import_dirs` (default `data/import`), and only real `ImportOptions` fields are accepted.
- **No credentials, broker access or order execution.** Credentials remain environment-only, as described in CONFIG.md.

## Development

```bash
cd web
npm ci                 # exact locked versions from package-lock.json into web/node_modules (network needed)
npm run build          # -> edgelab/web/static/{app.js, styles.css, index.html, build-info.json}
npm run watch          # rebuild on change
npm run typecheck      # tsc --noEmit
```

- **Locked dependencies.** `web/package-lock.json` pins the toolchain that built the committed bundle (esbuild 0.27.7, React 19.2.5) for every platform, including esbuild's Windows binaries. `npm ci` installs exactly that into `web/node_modules`, and the desktop build (`packaging/build.py`) runs it before `build.mjs`. Change dependencies with `npm install <pkg>@<version>`, which updates both files, and commit both.
- **Offline builds.** `build.mjs` falls back to globally installed packages when `web/node_modules` is absent. That is how the first bundle was built offline, but reproducible builds should use `npm ci`.
- **Type shim.** `@types/react` could not be installed offline, so `web/src/types/react-shim.d.ts` types the React APIs used. Delete it after `npm install`.
- **Bundle freshness.** `build-info.json` records a SHA-256 of the sources, and a test fails if the committed bundle is stale.

## Tests

```bash
python scripts/run_tests.py               # full suite; records reports/last_test_run.txt (shown on the dashboard)
python -m unittest tests.test_web_api     # API contracts (Flask test client)
python -m unittest tests.test_web_e2e     # real server + headless Chromium (skips without Playwright)
python -m unittest tests.test_research_api  # Phase 4 research routes (jobs held mid-cell by a gate)
python -m unittest tests.test_dukascopy tests.test_ai_discovery   # Phase 9: Dukascopy source, preferred dataset, AI discovery
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
- AI Discovery requests are synchronous and run under the service lock: an external provider call (up to 120 s)
  delays other requests until it returns. Only one external provider kind (Anthropic Messages API) is implemented.

## Holdout backtest and Holdout results (ADR-85)

- **Run backtest → Holdout backtest (`#/holdout`).**
  - Survivors only, ranked best → worst for prop trading from discovery numbers.
  - Select up to the protocol's tests left and confirm; each strategy is tested once.
  - Live progress with cancel, and the history of every attempt.
- **Backtest results → Holdout results (`#/holdout-results`).**
  - The Strategies explorer over each strategy's holdout run.
  - Adds the verdict, random comparison, cost stress and discovery net R per trade.

## Flip scan (ADR-88)

- **Run backtest → Flip scan (`#/flips`).**
  - Preview: research results that lose clearly before costs, worst first; each row says "Will be flipped" or why not.
  - One confirmation creates the flip protocol and saves the flipped strategies; nothing is backtested yet.
  - "Backtest the flipped strategies" runs them as one background research job (live progress, stop, resume).
  - The results table shows each flip beside its original; survivors go to Holdout backtest (marked "Flipped").

## Display options (ADR-86)

- **Theme:** Settings → Display → Theme (Dark / Light / Same as Windows).
- **Overview field chart:**
  - Survivors are amber.
  - Overlapping strategy dots group into bigger circles (capped size); a click lists the strategies inside.
- **Top bar:** brand, DEMO badge, version.

## ADR-90 display round

- Backtest results: pool picker (All | Pool 1 | Pool 2), no scope pills, positive/negative net-R split bars, tested / drawn /
  not drawn totals, "Live 50K OK" count / filter / column / panel check; All stored backtests without the R boxes, Breakdowns
  and the OOS text; drawdown histogram cut at the 99th percentile.
- Settings: display options first; USD / CHF display (your rate; calculations stay USD); chart grouping switch and distance
  slider; Live 50K drawdown limit; fee discounts (one switch, % per account type).
- Research runs restart themselves after an error; a banner (Research runs) and a top strip (every page) when it keeps failing.
- F11 / Esc fullscreen; no zooming in the desktop window; rounded gear; page-shaped skeletons; teal/orange light-mode bubbles.
