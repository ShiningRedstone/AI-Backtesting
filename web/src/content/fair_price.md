# Fair price strategy: summary

This summary comes from the video interview transcript you supplied (a trader explaining his "fair pricing theory" for
NQ on prop-firm accounts, with whiteboard examples and live trading). It describes what HE says he does, in our own
words. The mechanical version in this tab is our translation into fixed rules; every rule is a setting on the Settings
page, and the choices the video leaves open were made by you (marked "Your answer").

His own claims, unverified: large payouts from prop accounts, a pass rate around a third, and that a small edge in the
direction of the fair price is profitable on prop accounts when paired with fixed risk and fixed targets. Nothing here
has been checked by this app. The backtests are the check.

> His chart examples are from July and August (the year is not stated). They may fall inside the protocol's locked
> holdout period, so the app does not use them as tuning examples.

---

## The idea

Only outside events move the fair price of the index: session opens and news. At a session open, a burst of orders
creates an unfair first move; the opening price is the fair price, and the bet is that price returns to it. On a
scheduled 8:30 news day, the outcome is usually close to the forecast and already priced in, so the price just before
the release is the fair price and the first big candle is unfair.

**Sessions:** the first 90 minutes of each session he trades: New York open (9:30), the New York afternoon (14:00),
Asia and London. *Rules in this app:* each session is a setting; Asia opens at 20:00 New York and London at 03:00
(your answers); entries only inside the window; a trade still open 2 hours after the window is closed (your answer);
nothing is held into the daily close.

**Fair price:** the session's opening price. In the afternoon he trades back towards the 9:30 open (your answer: the
9:30 open by default). On 8:30 news days the pre-news price stays fair through the 9:30 open until 11:00. Moving the
fair price to a new consolidation is partly discretionary in the video; *in this app* an optional rule does it (off by
default): a narrow range just before the open, or the last minutes' narrow range after the losing-streak stop.

## The trades

1. **Opening-candle continuation** (once per session): a trade in the colour of the first candle, once a candle
   closes beyond the structure before the open, if it agrees with the higher-timeframe bias (the reversal of the
   previous hours' move). Static 25-point stop and 38-point target; with an opening candle over 25 points, both are
   doubled and the size halved. *Rules in this app:* structure = the last 1-minute swing (your answer); bias = the
   last 12 hours (your answer); only in the first 5 minutes; evaluations only by default (your answer).
2. **Reversions to the fair price**, on a 1-minute candle close:
   - **Displacement:** the body is bigger than the previous candle's body, it closes beyond the previous candle's
     wick, and the previous candle has the other colour.
   - **Break of structure:** a close beyond the most recent swing (a wick beyond the candles on both sides). He
     calls this the stronger entry.
3. **News reversions:** the same entries, back to the pre-news price. Surprise outcomes (far from the forecast) are
   not priced in; a setting can skip those days. Unexpected news (posts, speeches) cannot be seen in price data and
   is left out.

**Stop after three losses in a row** in one session: the market is not reverting.

## Evaluation and funded accounts

He trades evaluations and funded accounts differently:

- **Evaluations:** displacement and break-of-structure entries, a static 38-point target and 25-point stop (the 1 to 1.5
  shape of a 3,000 / 2,000 evaluation), about $500 risk on a 50K account.
- **Funded accounts:** the stronger break-of-structure entries only; the take profit comes from the room to the fair
  price (he needs at least about 80% of the target in his favour and uses steps of 25 points), a static stop, and the
  size is set so a winning trade pays a fixed dollar amount.

*Rules in this app:* a backtest runs both rule sets through the engine, each on every day. The prop challenge chain of
your pass-criteria account (Settings) then trades the evaluation rules while an evaluation runs and the funded rules once
it passes (from the next trading day). The headline numbers are the trades that chain took (your answer); each rule set
on its own is shown too. Funded win = $1,500 by default (your answer); contracts = that amount / (target points x $2 per
MNQ point), rounded down.

## What the app does not copy

- Several accounts at once: here one simulated account takes every trade (your answer), one position at a time.
- Discretion: adjusting the fair price by feel, early entries on wicks, mid-candle entries. Every entry waits for the
  candle close.
- Live-account and bonus tactics: outside a backtest of the rules.
