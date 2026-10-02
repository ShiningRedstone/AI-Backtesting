# Prop-account simulation

EdgeLab can replay the **recorded trades of a stored backtest run** through the rules of one or
more prop-firm style accounts. The simulator sits above the engine: it never re-runs the strategy,
never touches market data, never resizes, re-fills or re-costs a trade, and never modifies the
stored run. Its output describes what a rule set would have done to that historical trade
sequence under the run's stated assumptions. It is not a forecast. A passed evaluation is not
evidence that a strategy is profitable, robust or deployable.

Code: `edgelab/prop/` (`rules.py` config, `simulator.py` account replay, `service.py` lineage and
storage). Contracts: `Services.prop_configs`, `validate_prop_config`, `prop_simulate`,
`list_prop_simulations`, `get_prop_simulation`. CLI: `prop ...`. HTTP: `/api/prop/*`. Web page:
**Prop Simulation**. Decision record: ADR-49 in `ARCHITECTURE.md`.

## Rule sets (`configs/prop/*.yaml`)

A rule set is a versioned YAML/JSON document. It is validated strictly: unknown keys are errors,
and every problem is listed. Rule sets live outside the research config hash, as
`search.example.yaml` does, so adding one never changes research identities.

**No real firm's rules ship with EdgeLab.** `synthetic_static_eval.yaml` and
`synthetic_trailing_eval.yaml` are SYNTHETIC, TEST-ONLY examples (`synthetic_test_only: true`,
labelled on every result). To model a real program, write its rules yourself from that firm's
current official documentation.

| Field | Meaning |
|---|---|
| `kind: edgelab.prop_rules`, `schema_version: 1` | document type and version (required) |
| `id`, `name`, `description` | identifier (letters, digits, `_ . -`) and text. `name` and `description` are excluded from the hash |
| `synthetic_test_only` | `true` marks an illustrative rule set; results carry a SYNTHETIC label |
| `account.starting_balance`, `account.currency` | starting balance; currency must be `USD` (trade P&L is USD) |
| `trading_day.timezone`, `trading_day.reset_time` | **required**; the daily reset is never guessed (e.g. `America/New_York`, `"17:00"`) |
| `target.profit`, `target.stop_on_target` | profit target over the starting balance; stop the account once passed (default `true`) |
| `drawdown.max`, `drawdown.mode` | maximum total drawdown; `static` (default) or `trailing` |
| `drawdown.trailing_reference` | `closed_balance` or `end_of_day_balance` (trailing only) |
| `drawdown.lock_floor_at` | trailing floor stops rising at this balance (optional) |
| `daily_loss.max`, `daily_loss.action` | daily loss limit; `terminate` (default) or `pause_day` (skip the rest of that trading day) |
| `detection` | `end_of_trade` (default) or `intratrade_bound` (see below) |
| `min_trading_days` | trading days required before the target counts (default 0) |
| `max_calendar_days` | evaluation deadline in calendar trading-day labels from the account start (optional) |
| `position.max_units`, `position.action` | maximum trade size in the **instrument units of the trade's `contracts` column**; `terminate` (default) or `record` |
| `position.scaling` | `[{min_profit, max_units}, ...]`: larger limits once closed profit reaches `min_profit` |
| `consistency.max_best_day_share` | best day's P&L / total profit must be at most this share for the target to count |
| `payout_eligibility.min_trading_days`, `.min_profit` | reports eligibility only; no payouts are simulated |
| `session.timezone`, `entry_start`/`entry_end`, `flat_by`, `action` | entry window and a must-be-flat time, checked against trade timestamps |

Units: trade sizes are compared exactly as recorded, in the instrument units of the trade's
`contracts` column. EdgeLab never converts units implicitly.

### Refused by name (the trade records cannot support them)

- `trailing_reference: intratrade_equity_high` or `unrealized_equity_high`: a trailing drawdown
  measured from intra-trade equity highs needs the order of MFE and MAE inside each trade. The
  records hold only bar-resolution MFE/MAE magnitudes, not when they occurred.
- `payouts`: withdrawals and profit splits are not simulated.
- `news_restrictions`: the project has no economic-event calendar.
- `weekend_holding`: the backtest flattens positions (`hold_overnight: false`).
- `intratrade_bound` detection on a trade that crosses the daily reset: refused at simulation time
  (`PropDataError`), because the records don't say on which day the MAE occurred.
- Overlapping positions, and missing timestamps, sizes or P&L: refused, since concurrent
  positions would need a joint equity path.

## Exact semantics

- **Input:** the stored trades of one run. The run's `trades_hash` is recomputed and must match,
  or the simulation is refused. Trades are used as recorded: `net_usd` is P&L after the backtest's
  stated costs, and `contracts` is the size.
- **Order:** sorted by `exit_ts`, then `entry_ts`, then `trade_no` (stable). Positions must not
  overlap. This ordering is recorded in the lineage.
- **Trading day:** runs from `reset_time` to the next reset in `trading_day.timezone`. It is
  labelled by the local date on which it **ends** (with reset `00:00`, that is the local calendar
  date).
  - An entry is dated at `entry_ts` (the bar open).
  - An exit is dated at `exit_ts - 1 ns`. `exit_ts` is the close of the exit bar, so an exit on the
    bar ending exactly at the reset belongs to the day that ends then.
- **Booking:** realized P&L is booked at the exit, on the exit's trading day.
  - A trade crossing the reset is booked entirely on its exit day and counted in
    `trades_crossing_reset`.
  - A trading day counts toward `min_trading_days` when at least one trade is booked on it.
- **Daily loss:** day P&L is the closed balance minus the balance at the day's start. It's a breach
  when day P&L ≤ −`daily_loss.max`: reaching the limit counts.
- **Drawdown floor:**
  - `static`: starting balance − `max`.
  - `trailing` / `closed_balance`: highest closed balance − `max`, updated after each exit.
  - `trailing` / `end_of_day_balance`: highest end-of-day closed balance − `max`, updated at each
    day end.
  - `lock_floor_at` caps the trailing floor.
  - It's a breach when equity ≤ floor.
- **Detection:**
  - `end_of_trade`: rules are checked on the closed balance after each exit.
  - `intratrade_bound`: additionally, **before** the exit, a conservative worst-equity bound is
    checked: `balance_before − mae_points × point_value × contracts − cost_usd`.
    - `point_value × contracts` = `risk_usd / risk_points` from the trade itself.
    - The engine records MAE at bar resolution with the entry bar included in full, so the bound can
      overstate the true adverse excursion; it never understates it at bar resolution.
    - The trailing peak never moves inside a trade.
    - Maxima are reported separately as `*_intratrade_bound`.
- **Same-trade conflicts:**
  - An intra-trade breach precedes that trade's exit (the MAE occurs at or before the exit). So a
    breach and a target on the same trade resolve as the **breach**.
  - A single closed trade cannot both reach the target (a gain) and breach a loss rule (a loss).
  - When the daily loss and the maximum drawdown are breached together, both are recorded, and the
    status is `MAX_DRAWDOWN_VIOLATION`.
- **Target:** the target counts when all of these hold:
  - the closed balance ≥ starting balance + `target.profit`;
  - trading days ≥ `min_trading_days`;
  - the consistency rule holds, if one is configured.

  Unrealized equity never counts toward the target. `profit_target_balance_touched` reports whether
  the balance reached the target even when another condition was still unmet.
- **Size and session:** checked at entry (size and entry window) and over the holding interval
  (`flat_by`: violated when a local `flat_by` instant lies strictly inside `(entry_ts, exit_ts)`).
  - `terminate` ends the account at the entry. That trade's P&L is not booked, because the records
    can't say where a firm would have closed it.
  - `record` logs the violation and continues.
  - Sizes are never clipped.
- **Termination:** a terminal breach stops the account. The remaining trades are counted in
  `trades_not_processed`.
  - After a terminal breach, the ending balance is the closed balance of the breaching trade. When
    a firm would have liquidated inside that trade is unknown.
  - `pause_day` skips the rest of that day's trades (`trades_skipped_daily_loss_pause`; their P&L
    is excluded).
- **End of period:** an account with neither a pass nor a breach is `INCOMPLETE`, and
  `incomplete_reasons` says why. A trade the backtest force-closed at `END_OF_DATA` is counted in
  `trades_end_of_data`.
- **Session flattening:** is done by the backtest (`flatten_daily`, `hold_overnight: false`). The
  prop layer only checks the resulting timestamps against the rule set's session rules.
- **Multiple accounts:** each account replays the same stream independently, from its optional
  `start` (UTC; trades entering before it are ignored). Account ids must be unique. There is no
  broker routing.

Status enum (`AccountStatus`): `ACTIVE` (progression only), `TARGET_REACHED`,
`DAILY_LOSS_VIOLATION`, `MAX_DRAWDOWN_VIOLATION`, `RULE_VIOLATION` (size, session or deadline),
`INCOMPLETE`.

## Output

Each account's `summary` contains:

- status and `survived`;
- starting and ending balance, net P&L (USD) and net R;
- target reached and breach flags (drawdown, daily loss, other rule);
- trading days and trade count;
- maximum account drawdown and maximum daily loss (closed, plus the intra-trade bound);
- time to target and time to breach (timestamp, calendar days, trading days);
- violations, with rule, time, trade, detection and detail;
- `incomplete_reasons`, best-day share and payout eligibility.

`progression` has one row per trade, giving the balance, peak, drawdown, day P&L, daily-loss
headroom, drawdown floor and headroom, target progress, trading days and status. `days` has one row
per trading day. There is no aggregate "prop score".

The report keeps two parts separate:

- **Strategy result:** the source run's headline metrics, unchanged.
- **Prop-account results.**

**Lineage:**

- source run id and status, strategy id;
- definition and logic hash, as stored in the run;
- dataset id, provider, instrument, timeframe and source period;
- cost profile and status, run config hash, and the verified trades hash;
- simulator version and code version, and the ordering used;
- each account's config id, config hash, start and full rules.

`simulation_id` = `PROP_` + a hash of the run id, the trades hash, the account configs and the
simulator version. Identical inputs give the same id.

**Storage:** with `record`, the full document is written to `<data>/prop_simulations/PROP_*.json`,
never to the run table. Each simulation re-reads the run afterwards and asserts that the record and
trades are byte-identical.

## Commands

```bash
python -m edgelab.cli prop configs
python -m edgelab.cli prop validate configs/prop/synthetic_static_eval.yaml
python -m edgelab.cli prop simulate RUN_2026_00001 --config SYNTH_STATIC_EVAL --config SYNTH_TRAILING_EVAL --accounts 2 [--record]
python -m edgelab.cli prop list | show PROP_...
```

## Limitations

- Only evaluation-phase rules are modelled. Funded-account phases, payouts, resets and refunds are
  not.
- The intra-trade check is a bar-resolution bound, not a tick path. The time of an intra-trade
  breach is known only to lie within `[entry_ts, exit_ts]`, and `time_to_breach` uses the exit.
- Trades carry the backtest's cost model. A firm's own commissions or fees are not re-applied.
- One source run per simulation. Several datasets (e.g. years) are simulated one run at a time and
  are never concatenated.

## Lifecycle layer (ADR-64)

`edgelab/prop/lifecycle.py` + versioned profiles (`configs/prop/profiles/`) simulate evaluation -> funded -> payouts ->
live-transition for every backtest automatically (`run["prop"]`; `prop profiles|lifecycle` CLI; `/api/prop/profiles`,
`/api/prop/lifecycle/<run>`). A profile without per-rule evidence reports `RULES NOT VERIFIED` and claims nothing. This layer
never ranks or selects strategies.

## Configurable rulebook (ADR-65)

Every rule is a field of a versioned profile `configs/prop/profiles/<ID>.v<N>.yaml` (schema 2). To change a rule, edit the
value in `scripts/prop_default_profiles.py` (or copy a YAML file), bump `VERSION`, run the script: a new version is registered
and old results stay reproducible from their recorded profile hash. The simulator code does not change.

| Block | Fields |
|---|---|
| top | `account_size`, `quantity_unit` (MNQ), `purchase_date`, `trading_day.{timezone, reset_time}` |
| `evaluation` | `starting_balance`, `profit_target`, `minimum_trading_days`, `max_micros`, `drawdown`, `dll`, `consistency`, `scaling` |
| `funded` | `starting_balance`, `carry_evaluation_profit`, `max_micros`, `drawdown`, `dll`, `consistency`, `scaling` |
| `drawdown` | `max_loss`, `mode` (eod_trailing / static), `update_frequency`, `lock_trigger_offset`, `locked_floor_offset`, `breach_comparison`, `enforcement` |
| `dll` | `enabled`, `amount`, `behavior` (soft_breach / hard_breach), `detection` |
| `consistency` | `enabled`, `percent`, `applies_to`, `window`, `cushion.{amount_usd, percent_points}` |
| `scaling` | `enabled`, `basis`, `update` (end_of_session), `persist`, `start_micros`, `tiers[{min_profit, micros}]` |
| `payout` | `frequency.{mode, winning_days}`, `winning_day_threshold`, `require_positive_cycle_profit`, `min_balance_to_request`, `buffer_offset`, `formula.{mode, share, multiple, profit_basis}`, `minimum`, `cap.{mode, amount, schedule, table}`, `split_trader`, `count_limit`, `cycle_reset`, `after_payout_drawdown`, `request` |
| `live_transition` | `rule`, `payout_count` |
| `basis` | `status` (user_specified / official_verified), `source`, `modelling_choices` |

## Rule-basis model (ADR-66, schema 3)

Schema 3 replaces the nested schema-2 blocks with a flat `rules:` map: every key of `edgelab.prop.profiles.RULE_SPEC`
(`account.*`, `evaluation.*`, `funded.*` including `*.day_boundary.*`, `*.drawdown.measurement`, `*.dll.measurement`,
`*.consistency.cushion_*`, `*.scaling.effective`, `payout.*`, `live_transition.*`) is `{value, status, basis}` with
status VERIFIED, ASSUMED_DEFAULT or CUSTOM and no null value. `edgelab.prop.profiles.customize(profile, {rule: value}, basis)`
produces a CUSTOM new version; register it with `register_profile`. Every result reports the verified / assumed / custom
counts and names the assumed rules; outcomes read "UNDER DEFAULT ASSUMED RULES" whenever an assumed rule is active.

## Paper accounts (ADR-81)

The same lifecycle (`simulate_lifecycle`, unchanged) also runs forward in **paper accounts** ("Prop & paper" tab). There,
it is applied to the trades a strategy makes on new Dukascopy days, one attempt at a time:

- An evaluation breach ends the attempt. The next attempt starts with the trades entered after the breach, and the reset
  fee is charged (or the evaluation price, if no reset fee is entered).
- A pass charges the activation fee and continues funded. Every payout is recorded.
- A funded breach, the live-transition point or the payout limit ends the cycle, and a new evaluation is charged.
- INCOMPATIBLE stops the account.

Fees come from Settings → Prop account fees (workspace preferences, never guessed). Results stay labelled "under default
assumed rules" and are never research results. See ARCHITECTURE.md ADR-81.
