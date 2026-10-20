# Premarket breakout: summary

These are your rules, typed in the chat, turned into a fixed mechanical strategy for NQ (traded with MNQ micros on the
Dukascopy NQ stand-in, BID / ASK prices). Every choice your rules left open is a setting on the Settings page, marked
"Chosen default"; your own rules are marked "Your rule". Nothing here says the strategy works: the backtests are the
check, and the holdout review is the final test.

> This is a widely shared public setup and the rules came with no dates. You may have seen or traded it on days inside
> the locked holdout period, so a holdout result is only as clean as that allows.

---

## Your rules

1. **Mark the New York premarket high and low.**
2. **Wait for a 5-minute candle to close above it** (the high; below the low for a short).
3. **Enter**, with the **stop at the candle's wick** (its low for a long, its high for a short).
4. **Take profit at 1 : 8** reward : risk.

## How the app trades them (the defaults)

**Premarket:** every 1-minute bar from 4:00 up to 9:30 New York time (US premarket hours). The highest high and the
lowest low are the levels. A day with less than an hour of premarket data is skipped.

**Breakout:** the first 5-minute candle from 9:30 (candles on the New York clock: 9:30-9:35, 9:35-9:40, ...) that
closes above the high goes long; one that closes below the low goes short. Breakout candles may start until 12:00.
Only the first close beyond each level counts, and only one trade per day by default.

**Entry:** at the open of the next minute after the candle closed (a market order: buys fill on the ASK, sells on the
BID, so the spread is paid).

**Stop:** the breakout candle's low (long) or high (short). **Target:** 8 times the stop distance from the planned
entry (the ASK close for a long, the BID close for a short).

**Closing:** a trade still open at 15:55 is closed at the next minute's open. Nothing is held overnight.

**Size:** $500 risk per trade in MNQ micros, rounded down, at most 40 (Settings has % of the account instead). In the
prop challenge chain a trade bigger than the account allows at that moment is cut to the allowed number of micros (the
rule you chose for Fair price), so the chain never stops on a size limit.

## Things to know

- With a 1 : 8 target most trades lose; the strategy lives on the few that reach the target. Expect long losing streaks
  and look at the drawdown and the prop challenge chain, not the win rate.
- Wide premarket ranges give fewer breakouts; tight ones give more false breakouts. The range-size and stop-size limits
  are off by default and can be tried in the Strategy autotuner.
- Costs matter more with small stops: a 5-point stop pays the same spread and commission as a 50-point one.
