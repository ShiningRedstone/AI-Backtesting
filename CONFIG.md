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
Specs are standard CME values: verify before live use. `NAS100_CFD` is broker-specific.

## `costs.yaml`

`costs.default` plus per-symbol overrides in `costs.symbols`. All values per contract per side.

| Key | Meaning |
|---|---|
| `commission_per_side` | broker commission $ |
| `fees_per_side` | exchange + clearing + NFA $ |
| `slippage_ticks_market` / `_stop` / `_limit` | adverse ticks per fill by order type |
| `spread_points` | full spread in points (CFDs); half charged per side |

The shipped numbers are **assumptions**. Replace them with your broker's or prop firm's schedule.

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

## Planned (later phases)

`features.yaml` (P2), `search.yaml` (P4), `validation_splits.yaml` / `walk_forward.yaml` (P6),
`monte_carlo.yaml` and `prop_rules/*.yaml` (P7), `paper_trading.yaml`, `notifications.yaml`,
`execution.yaml` with `TRADING_MODE=paper` default (P9–11).
