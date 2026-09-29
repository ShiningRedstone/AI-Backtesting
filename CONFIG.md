# Configuration

All tunable values live in `configs/`. Files are merged into one tree; every research
run stores the full tree and its SHA-256 hash.

## Overrides

Environment variables override YAML: `EDGELAB__<SECTION>__<KEY>=<yaml scalar>`.

```bash
EDGELAB__BACKTEST__SAME_BAR_POLICY=conservative
EDGELAB__BACKTEST__COST_MULTIPLIER=2
```

**Credentials are never read from YAML.** Code obtains them with
`edgelab.core.config.get_secret("NAME")`, which reads only the environment.

## `data.yaml`

| Key | Default | Meaning |
|---|---|---|
| `validation.max_missing_bar_ratio_warn` | 0.001 | missing in-session bars above this → WARN |
| `validation.max_missing_bar_ratio_fail` | 0.05 | above this → FAIL (blocks backtests) |
| `validation.max_outside_session_ratio_fail` | 0.001 | bars when the market is closed (DST/timezone error signature) |
| `validation.spike_range_multiple` | 25 | bar range vs median → WARN |
| `validation.tick_misaligned_ratio_warn` | 0.0 | share of off-grid prices tolerated |
| `validation.allow_nonpositive_prices` | false | enable for instruments that traded ≤ 0 |
| `sample_size.min_trades` / `preferred_trades` | 100 / 500 | thresholds for LOW / MODERATE / ADEQUATE sample labels |
| `calendars.<NAME>` | CME_EQUITY, CME_GLOBEX_COMMODITY, FX_24_5, CRYPTO_24_7 | `timezone`, `session_open`, `session_close`, `trading_weekdays`, `holidays`, `early_closes` |

A trading date D runs from `session_open` (previous calendar day when it is later than
`session_close`) to `session_close` on D. **Holidays are not bundled**; add them per calendar.

## `instruments.yaml`

Per symbol: `tick_size`, `tick_value`, `calendar` (required), plus `exchange`,
`asset_class`, `currency`, `description`, `contract_months`, `roll_methodology`.
`point_value` is derived (`tick_value / tick_size`); if stated it must match or loading fails.
Specs are standard CME values: verify before live use.

Phase 2 keys: `min_size` and `size_step` (position granularity; futures `1`/`1` = whole
contracts, CFDs e.g. `0.01`/`0.01` units; sizing floors to the step and rejects below the
minimum) and `underlying` (informational grouping, e.g. `NDX`; never used to merge data).

CFD entries (`NAS100_CFD`, `US100_CFD`, `NQ_CFD`) use a convention of 1 unit = 1 currency unit
per index point on a 0.01 price grid. Your broker's lot size, minimum and step differ: set them.
Different CFD symbols are different instruments (some track the cash index, some the future).

## `costs.yaml`

Resolution order: `costs.default` <- `costs.symbols.<SYMBOL>` <- `costs.symbols.<SYMBOL>.providers.<PROVIDER>`.
All money values are per contract (futures) or per unit (CFDs) per side.

| Key | Meaning |
|---|---|
| `status` | `assumed` (your placeholder) / `broker_verified` / `unconfigured` (refuses to build a model) |
| `commission_per_side` | broker commission $ per unit per side (`commission_mode: per_unit`, the default) |
| `commission_mode` | `per_unit` or `notional`; `notional` charges `commission_per_million` USD per USD 1,000,000 traded notional per side (notional = theoretical fill price × point value × size), e.g. Dukascopy's volume-based CFD commission |
| `fees_per_side` | exchange + clearing + NFA $ |
| `slippage_unit` | `ticks` (default) or `points` (CFD feeds often have 0.01 ticks) |
| `slippage_ticks_market` / `_stop` / `_limit` | adverse slippage per fill, in `slippage_unit` |
| `spread_source` | `fixed` (use `spread_points`) or `dataset` (per-bar `spread` column) |
| `spread_points` | full spread in points; charged once per round trip |
| `scenario` / `basis` | a named research-cost scenario and the user's statement of the source of its numbers. A profile that declares `scenario` (even `null`) refuses until the scenario is complete: a name, a non-empty `basis`, `status` `assumed` or `broker_verified`, `commission_mode: notional` with `commission_per_million`, and market + stop slippage in points. Both are recorded in every run's cost assumptions |
| `financing_mode` | `none` (there is no holding cost), `annual_rate` (one constant annual rate), or `not_modeled` (holding costs exist but are NOT charged; recorded as such on every run) |
| `financing_long_rate` / `financing_short_rate` | annual rate; + = cost, - = credit |
| `financing_day_count` | 360 or 365 |
| `rollover_time` / `rollover_timezone` | when a held position is charged (default 17:00 America/New_York) |
| `triple_rollover_weekday` | 0=Mon..6=Sun; that rollover counts 3 nights |

Dataset spread charges the average of the entry-bar and exit-bar spread once per round trip,
which is the correct total whether the feed is bid-, ask- or mid-based. Financing counts
rollover instants strictly after entry and at or before exit; weekends are not charged except
via the triple day. Costs never change which bar a stop or target triggers on, so cost
sensitivity stays exact post hoc (including financing).

The futures numbers are **assumptions** (`status: assumed`). **CFD profiles ship `unconfigured`**:
a CFD backtest raises `CostConfigError` naming the missing fields until you enter your broker's
real numbers, per provider if two feeds differ. `allow_unconfigured=True` exists for plumbing
tests only and yields an all-zero model labelled `zero_for_testing`.

Exception, research proxy only: `NAS100_HISTDATA@HISTDATA` (HistData NSXUSD BID prices) carries
approved MNQ-equivalent research **assumptions** (`status: assumed`, never `broker_verified`):
commission $0.50 per research unit per side (1 MNQ = 2 units, so $1.00 per MNQ per side), market
and stop slippage 0.25 points (1 MNQ tick), limit slippage 0, fixed spread 0.50 points (2 MNQ
ticks), financing `none` (futures concept). They are not any broker's actual costs. The
`NAS100_HISTDATA` symbol level and every other feed stay `unconfigured`.

## `backtest.yaml`

| Key | Default | Meaning |
|---|---|---|
| `same_bar_policy` | intrabar | `conservative` / `optimistic` / `intrabar` |
| `intrabar_fallback` | conservative | used when lower-timeframe data can't decide |
| `gap_fill.target` | limit_price | gap through target fills at target, or `open` |
| `limit_fill.penetration_ticks` | 0 | ticks price must trade *through* a limit |
| `entry.allow_next_session_entry` | false | may a signal on a session's last bar fill next session |
| `session.flatten_daily` / `flatten_time` | true / "16:00" | exchange-local flatten time |
| `session.hold_overnight` | false | also flatten at each trading date's last bar |
| `max_trades_per_day` | null | cap per trading date |
| `require_causality_check` / `causality_cuts` | true / 20 | lookahead gate |
| `cost_multiplier` | 1.0 | scale all costs for a run |
| `cost_sensitivity_multipliers` | [0.5, 1, 1.5, 2, 3] | post-hoc sensitivity table |

## `storage.yaml`

`backend: auto` uses DuckDB + Parquet if `duckdb` is importable, else SQLite
(`data/edgelab.sqlite`). Force one with `duckdb` or `sqlite`.

## `logging.yaml`

`level`, `json_file` (JSON lines with fields timestamp, component, severity, event,
strategy, signal, account, order, error, run_id, …), `console`.

## `sessions.yaml` (Phase 2)

Named windows: `timezone` (IANA), `start`, `end` (`HH:MM`, local wall clock), optional
`weekdays` (local weekday on which an instance STARTS; default mon-fri). A bar belongs to a
window if its OPEN is in `[start, end)`; `end <= start` wraps midnight. Shipped: `NY_0900_1000`,
`NY_1000_1100`, `NY_0930_1030`, `NY_0930_1100`, `NY_1100_1200`, `NY_AM`, `NY_PM`, `NY_RTH`,
`LONDON` (Europe/London 08:00-16:30), `ASIA_TOKYO` (Asia/Tokyo 09:00-15:00), `ASIA_NY_EVENING`
(New York 19:00-02:00, Sun-Thu). All are conventions; edit freely. A feature's cache identity
includes the resolved window definition, so editing a window invalidates exactly the features
that use it.

## `features.yaml` (Phase 2)

| Key | Meaning |
|---|---|
| `cache_dir` | feature cache directory, relative to `storage.root` |
| `verify_cache_checksums` | re-hash arrays on every disk load (default true; corruption -> recompute) |
| `memory_cache_entries` | in-process LRU size |
| `default_set` | specs built after every import: `{id, params, timeframe}` |

## `import_profiles.yaml` (Phase 2)

File-layout presets (`generic_csv`, `mt5_export`, `dukascopy_csv`): delimiter, column names,
date/time columns, datetime format, volume/spread columns. A value of `REQUIRED` must be given
explicitly at import time (e.g. MT5 `source_timezone` and `spread_multiplier`, which vary by
broker). Profiles never supply provider, instrument, asset type or price basis. See `DATA_IMPORT.md`.

## Strategy library and DSL sessions (Phase 3)

- Strategies are **not** configuration: they are DSL documents (`STRATEGY_DSL.md`), stored
  canonically in `data/strategy_library/` (next to the data store; git-ignored).
- Every window in `sessions.yaml` can be named by a strategy (`entry.session`, session-based
  features). A strategy may also declare its own windows under `sessions:`. Using a configured
  name with a different definition is refused.
- A referenced session's **resolved definition is part of the strategy identity**: editing a
  window in `sessions.yaml` gives every strategy that uses it a new `strategy_id`.
- The compile provenance records the hash of the loaded configuration.
- Single backtests through `strategy backtest` use `costs.yaml` exactly like Phase 1/2. CFD
  datasets are refused until a broker cost profile is configured.

## Web application settings (Phase 3.5): `configs/web.yaml` (optional)

| Key | Default | Meaning |
|---|---|---|
| `web.host` | `127.0.0.1` | bind address; non-loopback addresses need `--allow-remote` (no authentication exists) |
| `web.port` | `8765` | HTTP port |
| `web.builder_timeframes` | `1m … 4h` | strategy timeframes offered by the builder (plus those of imported datasets); each must parse as a DSL timeframe |
| `web.import_dirs` | `[data/import]` | folders (relative to the root) the UI may import files from |
| `web.max_request_mb` | `5` | request size limit |

This file is deliberately **not** part of the research configuration: it is not in
`CONFIG_FILES`, not validated with it, and not included in `config_hash`, so changing a port
or a menu can never change a research result or its provenance. Unknown keys are refused.

## Planned (later phases)

`search.yaml` (P4), `validation_splits.yaml` / `walk_forward.yaml` (P6),
`monte_carlo.yaml` and `prop_rules/*.yaml` (P7), `paper_trading.yaml`, `notifications.yaml`,
`execution.yaml` with `TRADING_MODE=paper` default (P9–11).
