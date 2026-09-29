# edgelab: prop-firm strategy research engine

> Is there a repeatable statistical edge here, does it survive realistic costs and unseen
> data, and can it operate within a prop firm's rules?

This system is built to **disprove strategies** as aggressively as it discovers them.
Source-material claims (NQ outperforms, NY AM is best, higher timeframes are more stable,
…) are treated as hypotheses to falsify, never as targets. Every result is a *historical
result under stated assumptions*, not a forecast.

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 Foundation | structure, config, logging, data schema + validation gate, synthetic data, store, backtester, tests | **done** |
| 2 Features + CFD data | CFD/any-provider import pipeline, dataset metadata/hashes, DST-safe sessions, 14 causal features, multi-timeframe, persistent feature cache, dataset comparison, CFD cost architecture, service layer + CLI | **done** (see CHANGELOG.md for IMPLEMENTED / TESTED / NOT IMPLEMENTED / REQUIRES REAL DATA) |
| 3 Strategy DSL | versioned YAML/JSON DSL, validator with path errors, canonical identity, deterministic compiler into the existing Strategy interface, lineage library, Mode A controlled variations, Mode B proposal interface (no AI calls), opt-in signal exits in the engine, `time_of_day` feature | **done** (see STRATEGY_DSL.md, STRATEGY_GENERATION.md, CHANGELOG.md) |
| 3.5 Web UI | Strategy Builder & research application foundation: visual DSL editor with live backend validation, library, families, lineage, Mode A variations, datasets, single backtests, demo workspace | **done** (see WEB_UI.md) |
| 4 Research engine | strategy x dataset batch search (strategy ids, Mode A variation batches, Mode B proposal batches, families), deterministic planning with a max-cells refusal, durable resumable SQLite search storage, honest trial accounting, in-sample ranking and shortlist, background jobs with progress, cancellation and restart reconciliation, process-parallel execution, benchmark, Research CLI / API / web page | **done** (see CHANGELOG.md) |
| 5 Analytics | breakdowns by hour/session/weekday/month/year/event, distributions, rolling | **next** |
| 6 Anti-overfitting | train/validation/OOS, walk-forward, Monte Carlo, sensitivity, random-control suites | planned |
| 7 Prop simulator | evaluation, funded, payout, multi-account | **prop-account simulation layer done** (requested as "Phase 6"): versioned rule sets, evaluation rules, multi-account, lineage, web page (PROP_SIMULATION.md); funded/payout phases not built |
| 8 Reports | HTML dashboard, PDF | planned |
| 9–11 | paper trading, notifications, human discretion, isolated live adapter (default off) | planned |

## Quickstart

```bash
pip install -r requirements.txt          # numpy, pandas, pyyaml, scipy (+ duckdb recommended)
python -m unittest discover -s tests -t .           # 318 tests, ~60 s (DuckDB + slow tests skip unless enabled; browser tests need Playwright)
EDGELAB_SLOW_TESTS=1 python -m unittest tests.test_known_answers   # + multi-path bias check (~40 s)
python scripts/phase1_demo.py                        # end-to-end synthetic demonstration (Phase 1)
python scripts/phase2_benchmark.py                   # feature generation / cache benchmark (synthetic)
python -m edgelab.cli --help                         # data import, datasets, features, feed comparison
```

`pytest` also runs the suite unchanged.

## Minimal usage

```python
from edgelab.core.config import load_config
from edgelab.instruments import load_instruments
from edgelab.data.calendar import load_calendars
from edgelab.data.providers import CSVProvider
from edgelab.data.validation import validate_and_freeze
from edgelab.engine.costs import cost_model_from_config
from edgelab.engine.signals import OrderSpec
from edgelab.engine.backtester import run_backtest
from edgelab.strategies.examples import Breakout
from edgelab.analytics.metrics import compute_metrics, format_metrics

cfg = load_config()
nq = load_instruments(cfg)["NQ"]
cal = load_calendars(cfg)[nq.calendar]
raw = CSVProvider("data/raw/nq_5m.csv", source_timezone="America/New_York",
                  timestamp_convention="open").load_bars("NQ", "5m")
ds = validate_and_freeze(raw, nq, cal, "5m", 5, "csv", "NQ_5M_V1", cfg["validation"])
print(ds.report.to_text())

strat = Breakout(OrderSpec("market", stop_points=20, target_points=40),
                 lookback=12, window=("09:30", "11:00"))
res = run_backtest(ds, strat, cost_model_from_config(cfg, "NQ"), cfg["backtest"])
print(format_metrics(compute_metrics(res.trades)))
print(res.assumptions)          # every fill rule used, disclosed
```

## What Phase 1 guarantees

- **No unvalidated data reaches a strategy.** `run_backtest` only accepts a `ValidatedDataset`
  (read-only arrays, content hash re-checked). Impossible bars, conflicting duplicates, DST/
  timezone errors and grid misalignment are fatal. Missing bars are reported, never invented.
- **No lookahead.** Fills start on the bar after the signal, and every strategy must pass an
  empirical truncation test before it runs (catches `shift(-1)`, centred windows,
  full-sample normalisation).
- **Honest fills.** Gaps through stops fill at the worse open; same-bar stop/target conflicts
  follow a disclosed policy (intrabar replay from lower-timeframe data when reliable);
  touches that may have preceded a stop/limit fill are not credited.
- **Costs in R.** Commission, fees, slippage and spread are charged per trade and expressed in
  R; the 0.5×–3× cost-sensitivity table and breakeven cost multiple are exact.
- **Reproducibility.** Each run stores strategy spec, dataset manifest + hash, full config +
  hash, git commit + source hash, environment, seed, assumptions and a trades hash. Re-runs
  reproduce the hash bit-for-bit.

## Proven on synthetic data (known answers)

| Check | Theory | Engine |
|---|---|---|
| P(target 40 before stop 20), driftless walk | 0.3333 | within 4 SE (tests); demo: 0.317, z = −1.10 |
| Mean gross R, driftless walk | 0 | within 4 SE; 40 independent paths under production config: +0.016R, z = +0.56 |
| Stop / target exits | exactly −1R / +2R | exact |
| Conflict policies, same trades | conservative ≤ intrabar ≤ optimistic | holds trade-by-trade |
| Cost sensitivity post hoc vs full re-run | identical | identical to 1e-9 |
| 80%-win-rate bracket (target 10, stop 40) on a random walk | zero edge | gross expectancy within 4 SE of 0; costs push it lower |

**A lesson from the demo:** on a pure random walk, a 12-bar breakout showed **+14R net
(+$5.7k)** over 166 trades, profit factor 1.13. Its expectancy CI spans zero and it beat
only 69% of 100 rate-matched random-entry controls. Random entries alone produced per-trade
means from −0.5R to +0.5R on different two-month paths. Attractive in-sample numbers are
cheap, which is why Phases 4–6 exist.

## Phase 2 in one screen

```bash
# 1. look at a raw provider file (stores nothing): columns, spacing, decimals, hours with data
python -m edgelab.cli inspect NAS100_M1.csv --profile mt5_export --timeframe 1m \
    --source-timezone "America/New_York+7h" --spread-multiplier 0.01
# 2. import: normalize -> validate -> manifest + hashes -> immutable store -> derived TFs -> features
python -m edgelab.cli import NAS100_M1.csv --profile mt5_export --timeframe 1m \
    --source-timezone "America/New_York+7h" --spread-multiplier 0.01 \
    --instrument NAS100_CFD --provider MYBROKER --asset-type CFD --price-basis bid --derive 5m,15m,60m
python -m edgelab.cli datasets                       # Data Center listing
python -m edgelab.cli features list                  # Feature Lab catalog (FEATURES.md has the math)
python -m edgelab.cli compare-feeds <DATASET_A> <DATASET_B>
```

```python
from edgelab.features.engine import FeatureEngine
from edgelab.features.spec import FeatureSpec
eng = FeatureEngine.for_dataset(ds, sessions, cache)        # ds = a ValidatedDataset
atr_15m = eng.compute(FeatureSpec.make("atr", {"period": 14}, timeframe="15m")).arrays["atr"]
```

- Every feature is causal and verified by truncation tests; higher-timeframe values appear only
  after the higher-timeframe bar is COMPLETE. Definitions, known-at times and edge cases: `FEATURES.md`.
- Datasets are content-addressed (`NAS100_CFD_MYBROKER_1M_3FA9C1D2E4`) and never merged or
  overwritten; futures and each CFD feed stay separate. `research/compare.py` runs one strategy on
  each dataset independently and reports them side by side.
- CFD cost profiles ship **unconfigured**: the engine refuses to run a CFD backtest until you enter
  your broker's numbers (`CONFIG.md`). No broker figures are invented.
- Import guide and expected file schema: `DATA_IMPORT.md`.

## Web application (Phase 3.5)

```bash
python -m edgelab.web            # http://127.0.0.1:8765 - your workspace
python -m edgelab.web --demo     # separate synthetic demo workspace (./demo_workspace)
```

Build strategies visually, validate them against the backend, save them, generate controlled
variations, inspect lineage, run single causality-checked backtests and (Phase 4) plan, run and rank
batch searches on the Research page. The browser edits the DSL
document itself; validation, hashing, compilation and backtesting stay in Python. No Node.js is
needed to run it (the built frontend is committed). Details: WEB_UI.md.

## Phase 4 research in one screen

```bash
cp configs/search.example.yaml my_search.yaml   # replace the illustrative ids with your own
python -m edgelab.cli research validate my_search.yaml   # strict keys / types / values
python -m edgelab.cli research plan     my_search.yaml   # strategy x dataset cells, eligibility, nothing runs
python -m edgelab.cli research run      my_search.yaml [--workers 4]   # durable; re-running resumes
python -m edgelab.cli research rank     SRCH_...          # in-sample ranking, NOT validated
python -m edgelab.cli research job      my_search.yaml   # background job with progress (Ctrl-C cancels)
python scripts/benchmark_search.py                       # synthetic throughput, sequential vs processes
```

- A search runs stored strategies on datasets, one strategy on one dataset per cell, through the same
  pipeline as a single backtest. Datasets are never merged; unconfigured CFD costs make a cell
  ineligible, never estimated.
- Every planned cell is stored (completed, failed with its error, ineligible with its reasons), runs
  keep status `IN_SAMPLE`, and a search larger than `max_cells` is refused, never truncated.
- Trials are counted honestly (evaluated cells only, duplicates once, resumed cells not again).
  Ranking uses expectancy, profit factor or net R (never win rate), always with its trial count and
  "NOT VALIDATED". A shortlist is a tag; out-of-sample and walk-forward checks are Phase 6.
- `workers=1` and `workers=N` produce identical cells and trades hashes; only the parent process
  writes results. Limitations: `ARCHITECTURE.md` (Phase 4).

## Phase 3 in one screen

```bash
F=strategies/fixtures        # test fixtures - rules to exercise the machinery, not claims of edge
python -m edgelab.cli strategy validate $F/opening_range_breakout.yaml     # every issue, with its path
python -m edgelab.cli strategy explain  $F/opening_range_breakout.yaml     # plain-language rules
python -m edgelab.cli strategy variations $F/opening_range_breakout.yaml $F/orb_variations.yaml
#   batch VB_...: 48 combinations -> 35 variants (11 logic duplicates removed, 2 identical to base)
python -m edgelab.cli strategy proposals $F/proposals_example.yaml        # Mode B gate: 3 accepted, 5 rejected
python -m edgelab.cli strategy lineage STR_...                             # parent, exact changes, batch
python -m edgelab.cli strategy backtest $F/ema_crossover.yaml <DATASET_ID> # one causality-checked run
```

- A strategy is data. It compiles into the Phase 1/2 interfaces, and every indicator comes from the
  feature cache (no math in the compiler).
- Signals use three-valued logic: warm-up NaNs never fire, even under `not`.
- Unsupported engine concepts (trailing stops, partial exits, pyramiding, exit-based cooldown) are
  refused by name, never approximated.
- Identity is a hash of the resolved logic. Key order, formatting, names and `a < b` vs `b > a` do not
  change it; any rule, parameter, session-window or sizing change does.
- Mode A varies only declared parameters inside declared domains, caps the count before generating,
  de-duplicates identical logic, and records lineage. Batches regenerate identically.
- Mode B accepts proposals as data only. Performance fields and claim language are rejected, and
  every strategy passes the same validator and compiler.

## Environment notes

Built offline with Python 3.12, numpy 2.4, pandas 3.0. `duckdb`, `pyarrow`, `numba`,
`polars`, `pytest` were unavailable, so storage falls back to SQLite (DuckDB backend ships,
auto-selects when installed, and has a ready contract test) and the kernel is pure NumPy.

## Data you will need (not bundled; never fabricated)

```
DATA REQUIRED:
  Provider:    a licensed futures vendor (e.g. Databento, CQG, Rithmic, IQFeed) or exported CSV
  Instrument:  NQ (continuous, with roll method stated), ES
  Date range:  as long as available (ideally 7+ years for OOS and walk-forward)
  Timeframe:   1m (higher timeframes are built by session-anchored resampling)
  Credentials: vendor API key via environment variable
Also: CME holiday / early-close calendar; economic-event calendar for event analysis (Phase 5)

DATA REQUIRED (CFD research):
  Provider:    your CFD broker's history export, or a CFD data vendor
  Instrument:  e.g. NAS100 / US100 / USTEC (whatever your broker calls it; each is a separate dataset)
  Date range:  as many years as available
  Timeframe:   1m preferred
  Must know:   the file's timezone / server clock, whether timestamps mark bar open or close,
               whether prices are bid, ask or mid, what the volume column means, spread units
  Costs:       your broker's spread (or a spread column), commission, financing rates
```

No real market data has been imported in this build. All results so far are on synthetic data.

See `ARCHITECTURE.md` for design decisions and `CONFIG.md` for every setting.

## Prop-account simulation in one screen

```bash
python -m edgelab.cli prop configs                                   # SYNTHETIC test-only example rule sets
python -m edgelab.cli prop simulate RUN_2026_00001 --config SYNTH_STATIC_EVAL --accounts 2
python -m edgelab.web                                                # Prop Simulation page
```

- Replays the **stored trades** of a run through account rules: target, static or trailing drawdown,
  daily loss with an explicit reset, minimum days, size limits and scaling, consistency, sessions.
  Nothing about the strategy, engine or stored run changes.
- Rules the trade records cannot support honestly are refused by name. No real firm's rules ship.
- The result shows the strategy result and the account result separately, with full lineage. Passing
  is not evidence of profitability. See PROP_SIMULATION.md; packaging plan in DESKTOP_PACKAGING.md.

## Windows desktop build (developers)

The packaged app (`EdgeLab.exe`) needs no Python, Node or Git on the user's machine. Your data lives in
`%LOCALAPPDATA%\EdgeLab`, outside the executable. To build it on Windows (Python 3.11+ 64-bit; Node 18+
only to rebuild the frontend):

```powershell
powershell -ExecutionPolicy Bypass -File build_windows.ps1 -Smoke   # -> dist\EdgeLab\EdgeLab.exe
```

Double-click `dist\EdgeLab\EdgeLab.exe`. On first launch it asks which **research workspace** to use: open your existing
folder (for example `…\AI-Backtesting`, with its datasets, strategies and runs, used in place, never copied or migrated) or
create a new one; the choice is remembered (Settings → Research Workspace to switch). It starts the local server on
127.0.0.1 with a free port and opens
EdgeLab in its **own application window** (Microsoft Edge WebView2, part of Windows 11 and installed on Windows 10 by
Windows Update). Close the window to stop EdgeLab. `EdgeLab.exe --ui browser` uses your default browser instead;
`EdgeLabConsole.exe` is the console variant (logs, `cli`). Run from
source with `python -m edgelab.desktop`. Details, the data layout and the migration path for existing
repository data are in DESKTOP_PACKAGING.md.

## Strategy Lab in one screen (Phase 8)

Open EdgeLab (the desktop app or `python -m edgelab.web`), then go to **Strategy Lab**:

- pick or create a strategy, then duplicate or edit it and save a new version (DSL = source of truth, lineage kept);
- choose an eligible dataset and backtest;
- generate controlled variations (the exact combinations are shown first);
- run the batch on datasets, then **Compare** (unranked metrics, scope visible);
- **Validate** a selection (OOS, walk-forward, random-entry control);
- run a **Prop Simulation** on the stored run.

**AI Proposals** is the Mode B gate for machine-readable proposal batches; no model is connected. See WEB_UI.md.
