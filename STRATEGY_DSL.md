# Strategy DSL (version 1)

A strategy is **data**, not code: a versioned YAML/JSON document that is validated,
canonicalized, hashed and compiled into the existing backtesting interfaces. The DSL
describes *rules*. It never contains, estimates or implies performance.

```
YAML / JSON / dict
  -> validate (schema, logic, architecture, causality)   edgelab/strategy/dsl.py
  -> resolve parameters, prune disabled conditions
  -> canonical form -> logic_hash -> strategy_id
  -> compile -> DSLStrategy (a Phase 2 FeatureStrategy)   edgelab/strategy/compiler.py
  -> bind to a dataset -> Phase 1 causality check -> run_backtest
```

The compiler performs **no indicator math**. Every indicator, level, session value and
clock value comes from a Phase 2 feature (`FEATURES.md`), which means it comes from the
shared feature cache and is covered by the feature causality tests.

---

## 1. Document

| Key | Required | Meaning |
|---|---|---|
| `dsl_version` | yes (1) | schema version; other values are refused |
| `name` | yes | human name (not part of the logic identity) |
| `description` | no | free text (not part of the logic identity) |
| `family` | no | `{id, name, hypothesis, category}`: the conceptual hypothesis this instance tests |
| `timeframe` | yes | bar timeframe the strategy runs on (`1m`, `5m`, `15m`, `1h`, ...). The dataset must have exactly this timeframe (refused at bind time otherwise) |
| `sessions` | no | strategy-local session windows `{NAME: {timezone, start, end, weekdays?}}`, added to the configured ones |
| `parameters` | no | declared knobs, referenced as `"$name"` anywhere below |
| `entry` | yes | when and how to enter |
| `exit` | yes | protective stop (required), target, time exits, signal exits |
| `sizing` | no | default `{mode: fixed, quantity: 1}` |

Instrument, broker, costs, point value and trading hours are **not** part of a strategy:
they come from the dataset and the config. The same definition runs on NQ futures and on a
NAS100 CFD dataset.

## 2. Parameters

```yaml
parameters:
  fast:      {type: integer, value: 9, min: 5, max: 20, step: 1}
  stop_atr:  {type: float, value: 1.5, min: 0.5, max: 3.0, step: 0.25}
  use_vol:   {type: boolean, value: false}
  or_window: {type: choice, value: OR_0930_1000, choices: [OR_0930_0945, OR_0930_1000]}
  htf:       {type: timeframe, value: 1h, choices: [30m, 1h, 2h]}
```

These are the rules:
- Names must match `[a-z][a-z0-9_]*`.
- The value must match the declared type.
- `min <= value <= max`.
- `step > 0`, `step` requires `min` and `max`, `(max - min)` must be a whole number of steps, and the value must lie on the grid.
- `choice` needs non-empty unique `choices` that include the value.
- `boolean` accepts no `min`, `max`, `step` or `choices`.

A `$name` reference to an undeclared parameter is an error, and a declared parameter that is never used is a warning.

After substitution, the value must also fit the slot it lands in. For example, a float in an integer `period` fails.

The domain (`min`/`max`/`step`/`choices`) is the space the **hypothesis** allows. Mode A
variations may only explore inside it (see `STRATEGY_GENERATION.md`).

## 3. Operands

| Form | Meaning |
|---|---|
| `20`, `1.5`, `$param` | constant |
| `{const: 20}` | constant (explicit) |
| `{bar: close, lag: 1}` | `open`/`high`/`low`/`close`/`volume` of the bar `lag` bars back (default 0) |
| `{feature: ema, params: {period: 20}, output: ema, timeframe: 1h, lag: 0}` | a feature output. `params` default to the feature's defaults; `output` may be omitted for single-output features; `timeframe` = higher timeframe, evaluated at the last **completed** HTF bar |
| `{arith: add\|sub\|mul\|div, args: [a, b]}` | derived reference, e.g. `sma + 1.5 * atr` |

`lag` counts **strategy-timeframe bars back** and must be `>= 0`. Division by zero yields
*unknown* (NaN).

## 4. Conditions

```yaml
all: [cond, ...]          # AND
any: [cond, ...]          # OR
not: cond                 # NOT
{left: operand, op: ">", right: operand}
```

Operators: `>`, `>=`, `<`, `<=`, `==`, `!=`, `crosses_above`, `crosses_below`.

- `crosses_above(a, b)` means `a[t] > b[t]` and `a[t-1] <= b[t-1]`.
- `crosses_below(a, b)` is exactly `crosses_above(b, a)`.

Any condition may carry `label` (documentation) and `enabled: true|false|$param`. A disabled
condition is removed before evaluation, which makes filters toggleable by parameters.

**Three-valued logic.** A comparison involving an unknown value (NaN: indicator warm-up, no
previous session yet, missing volume) is *unknown*, not false. `all`, `any` and `not` follow Kleene
logic, and a signal fires only where its condition is **known true**. Consequences:
- `not(close > ema200)` does **not** fire during the EMA warm-up (a naive boolean
  implementation would);
- `any[unknown, true]` is true; `all[unknown, true]` is unknown (no signal).

## 5. Entry

```yaml
entry:
  direction: long | short | both
  long:  <condition>            # required for long/both; forbidden for short
  short: <condition>            # required for short/both; forbidden for long
  order: {type: market}                                          # default
  order: {type: stop,  long_price: <operand>, short_price: <operand>, expiry_bars: 2}
  order: {type: limit, long_price: <operand>, short_price: <operand>, expiry_bars: 6}
  session: NY_AM                # trading window: the signal bar must OPEN inside it
  trading_weekdays: [mon, tue]  # weekday of the TRADING DATE (Sunday-evening CME bars = Monday)
  cooldown_bars: 6              # at least N bars between consecutive SIGNALS
  max_trades_per_day: 2         # optional (ADR-62): executed trades per trading date (min with the config cap)
  reentry:                      # optional (ADR-62): decided from trade EXITS that have already happened
    cooldown_bars: 3            #   no new signal for N bars after an exit (signal bar - exit bar < N)
    block_day_after: stop       #   stop | target | any: no further entry that trading date after such an exit
```

Timing is inherited from Phase 1: a signal at bar `t` is decided at the close of `t` from bars
`<= t`. A market entry fills at the open of `t+1`. A stop or limit order works from `t+1`
for `expiry_bars` bars at the operand's value at `t`.

If long and short conditions are both true on the same bar, the bar is ambiguous and emits **no**
signal (counted in `last_diagnostics.ambiguous_both`). The engine holds one position at a
time, and signals that arrive while in a position are skipped by the engine.

## 6. Exits

| Exit | DSL | Compiles to |
|---|---|---|
| Stop, points | `stop: {type: points, points: 20}` | `OrderSpec.stop_points` (distance from the actual fill) |
| Stop, ATR | `stop: {type: atr, multiple: 1.5, period: 14, timeframe?}` | absolute stop = reference ∓ k·ATR at the signal bar |
| Stop, price | `stop: {type: price, long: <operand>, short: <operand>}` | absolute stop (e.g. opening-range low, last swing high) |
| Target | `target: {type: none \| points \| atr \| price \| risk_reward}` | `target_points` or an absolute target |
| R multiple | `target: {type: risk_reward, multiple: 2}` | with a points stop: `target_points = 2 × stop_points`; otherwise reference ± 2·\|reference − stop\| |
| Time stop | `time_stop_bars: 24` | `OrderSpec.time_exit_bars` (close of the Nth bar after entry) |
| Max hold | `max_hold_bars: 36` | `OrderSpec.max_hold_bars` |
| Signal exit | `signal: {long: <condition>, short: <condition>}` | `SignalSet.exit_long / exit_short` |
| Session flatten | not in the DSL | engine config (`backtest.yaml`), unchanged |

A protective stop is mandatory.

**The reference price** is the signal bar's close for market entries and the order price for
stop/limit entries. A signal whose absolute stop or target is missing (NaN) or on the wrong
side of the reference is dropped and counted (`invalid_stop` / `invalid_target`). It is never
"fixed". The engine additionally skips fills that gap beyond the stop (Phase 1 behaviour).

**Signal exits (Phase 3 engine extension).**
- **Timing:** an exit condition true at the close of bar `k` exits at the **open of `k+1`** with a market order (market slippage), reason `SIGNAL`. The flag can be on the entry bar itself; flags before the entry are ignored.
- **Precedence:** a stop, target, session or time exit that happens earlier wins.
- **Gaps:** if the open of `k+1` gaps through the stop or target, the resting order fills there (`STOP_GAP` / `TARGET_GAP`, existing gap policy).

The initial stop is fixed at entry. An optional `exit.trailing` block (section 6.1) moves it in the favorable
direction only. Partial exits are **not supported** (section 10).

### 6.0 No-progress exit (`exit.no_progress`, ADR-62)

```yaml
exit:
  no_progress: {bars: 6, min_progress: {type: r, value: 0.5}}   # type: points | r | atr ; atr_period: 14 if atr
```

At the close of bar `N` since entry (entry bar = 1) the trade exits at that close (`NO_PROGRESS`) if the favorable excursion
from the fill (exit-side extreme including bar `N`) is still below the threshold. A stop/target touch on bar `N` and the
forced / time / max-hold closes take precedence. Combines with `exit.trailing` (one ATR series: equal `atr_period`).

### 6.1 Trailing / breakeven stops (`exit.trailing`, ADR-61)

```yaml
exit:
  stop: {type: atr, multiple: 2.0}
  trailing:
    mode: distance            # distance | level | breakeven
    distance: {type: atr, multiple: 2.0}     # distance mode: {type: points, points: N} | {type: atr, multiple: N}
    # level: {long: <operand>, short: <operand>}   # level mode: the strategy's per-bar candidate stop
    activation: {type: r, value: 1.0}        # immediate (default) | points | r | atr : favorable excursion from the fill
    breakeven: {trigger: {type: r, value: 0.5}, offset_points: 0}   # optional; breakeven-only when mode: breakeven
    atr_period: 14            # only when an ATR distance / activation / trigger is used
    update: {every_bars: 1, only_new_extreme: false, min_step_points: 0}
```

Exact semantics (engine/fills.py `simulate_exit_trailing`; every rule is tested):

- **Decided at a bar close, effective next bar.** The stop that applies on bar `k` was fixed before bar `k` opened;
  bar `k`'s own extreme cannot raise the stop it is tested against.
- **Favorable extreme** = highest high (short: lowest low) since the fill on the *exit side* of the quote model
  (long: BID, short: ASK); a stop/limit *level* fill starts the extreme at the fill price, a market/gap-open fill
  includes the whole entry bar. Activation measures `extreme - fill`.
- **distance:** candidate = extreme -/+ distance (points, or `multiple x ATR` at that bar). **level:** candidate =
  the operand evaluated at that bar's close (swing, previous bar low, moving average, channel, chandelier ...).
  **breakeven:** once the excursion reaches the trigger, candidate = fill +/- `offset_points`.
- **Ratchet:** the stop only moves in the favorable direction (and by at least `min_step_points` for a trail).
  A candidate not strictly on the protective side of the bar's close is ignored, never applied.
- **Update rules:** `every_bars: n` updates only on bars whose count since entry (entry bar = 1) is a multiple of n;
  `only_new_extreme` updates only on bars that set a new favorable extreme. Breakeven updates are not throttled.
- **Execution:** each bar is resolved like a fixed stop: an open gapping through the stop exits at the open,
  a touch exits at the stop, a stop/target touch on one bar follows the conflict policy (incl. intrabar replay).
  Exit reasons are `TRAIL_STOP` / `TRAIL_STOP_GAP` when the stop had moved, else `STOP` / `STOP_GAP`.
- **Unchanged:** the take-profit is fixed; forced session close, `time_stop_bars`, `max_hold_bars`, end of data and
  signal exits keep their precedence. Trailing only adds earlier stop exits, so same-date and holding-time
  limits cannot be weakened. `risk_points` / R stay relative to the *initial* stop.
- Identity: `exit.trailing` is part of the canonical logic only when declared (strategies without it keep
  their logic hash). Defaults are filled in the canonical form, so cosmetic variants share one identity.

## 7. Sizing

| DSL | Backtester sizing config |
|---|---|
| `{mode: fixed, quantity: 1}` | `{mode: fixed, contracts: 1}`: contracts (futures) or units (CFD) |
| `{mode: risk, risk_usd: 500, max_quantity: 5}` | `{mode: risk, risk_usd: 500, max_contracts: 5}`: floor(budget / risk per unit) on the instrument's size step |

| `{mode: equity_risk, risk_pct: 0.5, max_quantity: 20}` | `{mode: equity_risk, ...}`: budget = `risk_pct`% of the **equity at the signal**, then as `risk` |

`equity_risk` (ADR-62): equity = the **research account** (run parameter `account=`, default `DEFAULT_RESEARCH_ACCOUNT` = 50,000 USD, ADR-64; NOT part of the definition or identity) + the net P&L (after costs) of trades that have already **exited**. The engine runs
one position at a time, so no open or future trade is visible; the quantity comes from the planned initial stop, rounded
down (0 = trade rejected), with the optional cap. Sizes are path-dependent within one run (a different window or start
gives different sizes); R-based metrics are unaffected. A `starting_equity` key in a definition is refused. The account is recorded in the run assumptions only.

**Execution contract (ADR-63).** An optional `sizing.contract: MNQ` names the contract that is traded (its specification is the
instrument of that name in `configs/instruments.yaml`). The data series stays the research proxy; the contract supplies the
point value ($2 per index point for MNQ), minimum size and size step (whole contracts). A named contract requires whole-number
quantities and caps (validation and engine), every risk mode converts dollars to contracts with the same floor-only rule
`floor(budget / (planned stop points x point value))` (0 = trade rejected, never rounded up), and a run without the contract's
specification, or MNQ on another futures series, is refused. The key is part of the strategy's identity only when present.

A fixed quantity must satisfy the instrument's `min_size`/`size_step`. This is checked when the
strategy is bound to a dataset: 0.5 NQ is refused, while 0.5 units of a CFD with step 0.01 is fine.
`risk_usd` is your risk budget per trade, not a broker value.

## 8. Validation

`python -m edgelab.cli strategy validate FILE` reports **every** issue, each with its path:

```
Strategy validation failed (2 error(s)):

entry.long.left.feature:
  unknown feature 'ema_fastest'
  did you mean: ema?

entry.long.right.lag:
  lag -1 looks into the future (non-causal)
  lag counts bars BACK; only lag >= 0 is allowed
```

| Class | Rules |
|---|---|
| Schema | unknown keys (with suggestions), missing required blocks, types, `dsl_version`, invalid operators / order types / stop and target types / sizing modes / timeframes |
| Parameters | section 2, plus undeclared references and unused declarations (warning) |
| Features | unknown feature / output (with suggestions), invalid feature params (the feature's own checks), multi-output feature without `output`, unknown session names, pinned feature version not implemented |
| Logic | comparison of two constants or of an operand with itself; `any: []`; an entry or exit condition that becomes empty after disabling; conditions for a side the direction excludes; missing stop; stop/limit entries without prices; prices on market entries; non-positive distances, multiples, bars; `max_hold_bars >= time_stop_bars` (warning) |
| Architecture | engine-unsupported concepts are named explicitly (section 10); local session names that clash with a differently defined configured session |
| Causality | negative lags and `lead`/`future` keys; features flagged non-causal; feature timeframes lower than, or not a multiple of, the strategy timeframe |

## 9. Causality safeguards

1. **Static (validator):** only past lags; no future keys; only causal registered features;
   HTF features only on multiples of the strategy timeframe.
2. **By construction:** operands are feature outputs, bar fields and constants. There is no
   expression language that can index the future. HTF values are the last **completed** HTF bar
   (Phase 2 MTF alignment, effective close = min(open + tf, session close)). Cooldown looks only
   at earlier signals.
3. **Empirical (engine):** `run_backtest` re-runs the Phase 1 truncation check on every
   compiled strategy. Signal, price **and exit** arrays must be identical when the future is
   removed. Tests show that a feature which *claims* to be causal but peeks ahead passes the
   static validator and is then stopped with `LOOKAHEAD` before any trade.

## 10. Not supported (refused with a precise message)

| Concept | Reason |
|---|---|
| top-level `trailing_stop`, `breakeven` | declare trailing in `exit.trailing` (section 6.1) |
| `partial_exits`, `scale_out` | one fill in, one fill out |
| `pyramiding`, `scale_in` | one position at a time |
| top-level `cooldown_after_exit` | use `entry.cooldown_bars` (between signals) or `entry.reentry` (from trade exits, ADR-62) |
| `lead`, `future`, negative `lag` | non-causal |

These are extension points: adding any of them requires an engine change first, then a DSL key.

## 11. Canonical form and identity

The canonical form normalizes the document:
- `a < b` becomes `b > a`, and `crosses_below(a, b)` becomes `crosses_above(b, a)`;
- `all`/`any` children are flattened and sorted, `+`/`*` args are sorted, and `not(not x)` becomes `x`;
- feature params are filled with their defaults and the feature **version is pinned**;
- timeframes are normalized (`1h` becomes `60m`) and numbers are typed (`2` becomes `2.0`);
- weekdays are normalized and sorted, and default blocks are filled in.

Two hashes result:

| Hash | Covers | Ignores |
|---|---|---|
| `logic_hash` → `strategy_id = STR_<first 12>` | resolved logic (parameters substituted, disabled conditions removed), timeframe, entry, exits, sizing, and the **resolved definitions** of every referenced session window | name, description, family text, labels, parameter domains, key order, formatting |
| `definition_hash` | the whole canonical document, including parameters and metadata | key order, formatting |

A `$param` version and a literal version of the same rules share a `logic_hash`. Changing a
threshold, operator, feature parameter, feature timeframe, strategy timeframe, stop, time exit,
session (or a session's configured hours), sizing or direction changes it.

Stored canonical definitions re-validate. If a pinned feature version stops being implemented,
loading fails loudly instead of silently changing meaning.

## 12. Compilation and provenance

`compile_definition()` returns:
- the canonical logic and identity;
- the `OrderSpec` and the sizing config;
- the de-duplicated list of `FeatureSpec`s;
- the strategy-local sessions;
- provenance: DSL version, `COMPILER_VERSION`, a SHA-256 of the strategy-package source, each feature's spec id and implementation hash, the session definitions used, and the config hash.

`compile_strategy()` wraps the definition in a `DSLStrategy`. It implements the Phase 1
`Strategy` interface through Phase 2's `FeatureStrategy`: bound per dataset, features read from
the shared cache, and the `spec` property carries the DSL identity into run records.
`last_diagnostics` counts raw, ambiguous, invalid and cooldown-suppressed signals.
`explain()` renders a plain-language preview of the rules.

## 13. Known limitations

- `lag` counts strategy-timeframe bars. On an HTF operand, `lag: 1` means one base bar earlier,
  not the previous HTF bar.
- Targets are fixed when the signal is generated; the stop can only be trailed through `exit.trailing`
  (no target trailing, no partial exits).
- On bid-based CFD feeds, the ask-side trigger of a short's stop is not modelled (Phase 2 limitation, unchanged).
- A signal exit executes at the next open even when the condition is evaluated on an HTF operand.
- No expression language beyond `add`/`sub`/`mul`/`div`: rolling functions, max/min of
  operands and custom indicators must be added as features (with docs and causality tests).

## 14. Complete example

`strategies/fixtures/opening_range_breakout.yaml` (a test fixture, not a claim of edge):

```yaml
dsl_version: 1
name: ny_orb_breakout
family: {id: ny_opening_range_breakout, name: NY opening-range breakout, category: breakout,
         hypothesis: "A break of the New York opening range continues in the break direction."}
timeframe: 5m
sessions:
  OR_0930_0945: {timezone: America/New_York, start: "09:30", end: "09:45"}
  OR_0930_1000: {timezone: America/New_York, start: "09:30", end: "10:00"}
  OR_0930_1030: {timezone: America/New_York, start: "09:30", end: "10:30"}
parameters:
  or_window: {type: choice, value: OR_0930_1000, choices: [OR_0930_0945, OR_0930_1000, OR_0930_1030]}
  rr: {type: float, value: 2.0, min: 1.0, max: 3.0, step: 0.5}
  use_volume_filter: {type: boolean, value: false}
  min_rvol: {type: float, value: 1.2, min: 1.0, max: 2.0, step: 0.1}
entry:
  direction: long
  session: NY_0930_1100
  trading_weekdays: [mon, tue, wed, thu, fri]
  long:
    all:
      - label: opening range is complete
        left: {feature: session, params: {session: $or_window}, output: in_session}
        op: "=="
        right: 0
      - label: close breaks above the opening-range high
        left: {bar: close}
        op: crosses_above
        right: {feature: session, params: {session: $or_window}, output: prev_high}
      - label: optional relative-volume confirmation
        enabled: $use_volume_filter
        left: {feature: volume_stats, params: {period: 20}, output: rel_volume}
        op: ">="
        right: $min_rvol
exit:
  stop: {type: price, long: {feature: session, params: {session: $or_window}, output: prev_low}}
  target: {type: risk_reward, multiple: $rr}
  time_stop_bars: 36
sizing: {mode: fixed, quantity: 1}
```

Other fixtures cover these cases:

| Fixture | What it exercises |
|---|---|
| `ema_crossover` | cross-overs, both directions, ATR stop |
| `rsi_threshold` | signal exit, time stop, no target |
| `vwap_reclaim` | VWAP |
| `atr_breakout` | derived band, stop entries, ATR target, risk sizing |
| `fvg_entry` | limit entry into the gap, price stop with buffer |
| `structure_bos` | short side, swing-high stop |
| `mtf_trend_filter` | 1h filter, point stop/target, weekdays, cooldown, max hold |

## 15. Commands

```bash
python -m edgelab.cli strategy validate strategies/fixtures/opening_range_breakout.yaml
python -m edgelab.cli strategy explain  strategies/fixtures/opening_range_breakout.yaml
python -m edgelab.cli strategy compile  strategies/fixtures/opening_range_breakout.yaml   # + --json
python -m edgelab.cli strategy save     strategies/fixtures/opening_range_breakout.yaml
python -m edgelab.cli strategy backtest strategies/fixtures/opening_range_breakout.yaml <DATASET_ID>
```

Exit code 0 means OK. Exit code 2 means a validation, compile, variation or cost-configuration error, with the message on
stderr (the validation report goes to stdout for `validate`).
