# BP Blake's strategy: full summary

This summary comes from the ten video transcripts you supplied (two strategy videos, the Judas swing video, the
Asia session video and the trade recaps of May 13, June 3, 4, 5, 10 and 24, July 21, August 10 and August 18,
2026). It describes what HE says he does. The mechanical version in this tab is our translation of it into fixed
rules. Every rule is a setting on the Settings page, and the "Rules in this app" boxes say how each idea was
turned into a rule.

His own claims, unverified: about a 70% win rate, a 1:1 risk-to-reward by default and 1:3 at most, 1 trade a day
(at most 2), risk 1% per trade, 3-5 trades a week, Judas swings 1-3 times a week. Nothing here has been checked
by this app. The backtests are the check.

> Several of the recap dates (May-August 2026) are likely inside the protocol's locked holdout period. Looking up
> those days in the data before the holdout test would leak holdout information into the tuning, so the app does
> not use them as tuning examples.

---

## The four steps (NY AM model)

### Step 1 - Bias and the higher-timeframe draw on liquidity

"Before you even think about executing, figure out where the market wants to go today." He says this step fixes
win rate more than anything else, and it removes about 90% of bad trades.

**Timeframes:** daily, 4 hour and 1 hour (mostly), with a 15-minute check around the open (perfect equal lows on
the 15m can change the bias).

**Question 1 - which fair value gaps (FVGs) are respected, which are disrespected?**
- Bullish = bullish FVGs are respected (price trades into them and holds, then moves away) AND bearish FVGs are
  disrespected (price closes through them).
- Bearish = bearish FVGs respected AND bullish FVGs disrespected.
- He also checks ES (S&P futures) for the same picture.

**Question 2 - which swing high or low are we going to?** Don't overcomplicate it: the next obvious swing high
(bullish) or swing low (bearish) the market is pushing toward is the draw on liquidity (DOL) for the day.

Draws he names: relative equal highs/lows, trend line liquidity (stacked highs/lows), unfilled FVGs on higher
timeframes, session highs/lows, previous day high/low, new week opening gaps (NWOG), new day opening gaps,
all-time highs, external swing highs/lows.

**Low-resistance vs high-resistance liquidity (his "orders" view):**
- Low resistance (LRL) = stops still resting: stacked or equal highs/lows that have NOT been run, failure swings,
  trend lines. The market reaches these easily.
- High resistance (HRL) = highs/lows that keep getting swept; little left there.
- Bias leans toward the side with low-resistance liquidity. Your stop should sit behind high-resistance
  liquidity (protected), your target at low-resistance liquidity (unprotected).
- "Is my stop protected (high resistance) and is my take profit unprotected (low resistance)?" is the question
  he asks on every trade.

**External vs internal liquidity:** price moves internal (FVGs) -> external (swing highs/lows) -> internal ...
After a large move takes external liquidity and hits a higher-timeframe key level, he expects a "rebalance" back
toward internal liquidity (an unfilled gap). He only takes such reversal trades once a buy/sell model has
completed at a higher-timeframe key level.

**Equilibrium (EQ):** draw the range of the higher-timeframe swing (a Gann box / 50% level). If bullish, the low of
the day should form at or below EQ (discount), often at a PD array sitting around EQ. Heavy closes through EQ
against you are a bad sign. Disrespecting EQ (no reaction at levels around EQ) supports the opposite bias.

**Trend:** "the trend is your friend". At all-time highs he mostly only longs; he only shorts at all-time highs
after bearish structure has formed (a market structure shift / CISD, obvious draws below, a pullback and more
displacement). He does not short the top just because it is high.

**Default stance:** "bullish (or bearish) until proven wrong" - keep the higher-timeframe bias until a trade in
that direction clearly fails and structure flips.

**Models inside models:** the same accumulation -> manipulation -> distribution shape should show on the higher
timeframe and again on the lower timeframe at entry.

**SMT divergence (NQ vs ES):** one index sweeps a high/low while the other does not. An added confluence, never
required.

### Step 2 - A valid key level (where price should react)

**Timeframes:** 3m, 5m, 15m, 30m, 1h, 4h (and daily/weekly gaps for context). 2-3 good levels are enough; 20 lines
on a chart is the mistake.

1. **Fair value gaps:** an unmitigated (never touched) FVG is valid on the first touch. If price already traded into
   the gap earlier (for example pre-market) and left, the gap itself is no longer valid: wait for the intermediate
   low (bullish) / high (bearish) that formed inside the gap to be swept. That sweep is the valid key level.
2. **CISD - change in state of delivery:** bullish = a body close ABOVE the opening price of the candle (or series of
   down-close candles) that traded into an FVG or swept an intermediate low inside an FVG. Bearish is mirrored. The
   area (that series' open, like an order block) becomes a key level price often returns to. Strongest when it
   lines up with an FVG; it can be used on its own. Seen on 30s, 5m, 15m, 1h.
3. **Rejection blocks:** a bullish rejection block is a wick that traded into a bullish FVG or an intermediate low
   inside one (bearish mirrored). Mark the wick as a box and its 50% (CE) - price often returns into the wick and
   the CE gives "bottom tick" entries.

Also mentioned: order blocks (the down-close candles before the CISD), breaker blocks, BPR (balanced price range:
an inverted gap overlapping an FVG), 30-minute and hourly intermediate lows, daily intermediate lows, weekly FVGs.
The key level must line up with the draw on liquidity and the bias.

### Step 3 - Confirmation: the inversion fair value gap (IFG)

An IFG is an FVG that price closes through in the opposite direction, which confirms order flow flipped.

- **Timeframes:** 1, 2, 3, 4 and 5 minute. The 30-second chart is for experienced traders only (many low-quality
  30s setups); beginners should stick to minute charts.
- **Manipulation leg:** the swing high to the swing low that hit your key level (bullish), or swing low to swing
  high (bearish).
- **Highest-timeframe rule ("nine times out of ten"):** look for FVGs created INSIDE that leg on 1m-5m; take the
  highest timeframe that has one and wait for a candle on that timeframe to close (body) through it. Lower
  timeframe inversions inside a leg that also has a higher-timeframe gap often fail. Gaps above the leg do not
  matter - only the leg that hit your key level.
- Waiting for the highest timeframe gives a worse risk-to-reward but the best win rate, which is what prop firm
  trading needs.
- If the stop would be huge (he faded a 124-point stop), he may wait for more structure.
- A strong displacement candle closing through the gap raises the win rate ("you can't fade trades like this").
- With poor conditions he only takes entries once the candle has CLOSED below/above the gap.

### Step 4 - Execution and risk

- **Entry:** on the body close of the IFG. If the close is too far away (bad risk-to-reward), a limit order at the
  IFG or at the CISD instead; sometimes he waits for a small retrace for a better entry.
- **Stop loss:** usually at the swing low/high of the manipulation leg (the safest, his default for beginners).
  Other choices he uses: at the displacement candle's body, at the FVG's far edge, at an order block, at the IFG
  candle's body high.
- **Take profit:** "low-hanging fruit" - the most obvious high/low on the lower timeframe toward the draw (not the
  far higher-timeframe draw). Usually 1:1 to 1:3. If the obvious level is less than 1:1 he moves the target a
  bit higher to make 1:1. Targets: unfilled gaps, intermediate highs/lows inside gaps, session highs/lows,
  previous day high/low, new week opening gap, equal highs/lows, order blocks.
- **Prop firm reality:** consistency rules mean no huge 1:10 trades; take your piece of the pie.
- **Breakeven:** mostly at the swing point of the manipulation leg, or at the first internal high/low. He does NOT
  move to breakeven when the level only protects low-resistance liquidity, or in fast Judas trades. After being
  stopped at breakeven he may re-enter if the idea is intact (price usually returns to the internal low inside
  the gap or the idea is invalid).
- **Trailing:** sometimes trails the stop to new swing lows once well in profit; a "hard trail" near targets.
- **Daily rules:** one win and he is done; one loss usually done; a second trade only if it is A+ at a new key
  level; two losses and he is definitely done. No adding to losers, no moving stops, no instant re-entry.
- **Risk:** 1% of the account per trade.
- **Time:** 9:30 to 11:00 a.m. New York, the "golden hour". Very rarely after 11, only if the morning was bad and
  liquidity was built.

---

## Session opens and power of three (PO3)

- Every session plays accumulation, manipulation or distribution.
- Opening times he watches: 6 p.m. (true day open), 8 p.m. (Asia), 2 a.m. (London), 8:30 a.m., 9:30 a.m. and
  10:00 a.m. New York.
- **Bullish open shape - open, low, high, close (OLHC):** the open candle manipulates DOWN into a bullish
  higher-timeframe key level, then distributes up.
- **Bearish - open, high, low, close (OHLC):** manipulates UP into a bearish key level, then down.
- "Am I going to take a long here? No - zero manipulation lower." He needs the manipulation first; generating new
  relative equal lows instead of sweeping is NOT valid.
- If pre-market (e.g. 8 a.m.) already manipulated into the higher-timeframe level, levels inside that range are
  often just respected without a new sweep.

## The Judas swing (open manipulation model)

- The fake move at the session open that traps traders, then reverses ("the move that pretends to be your
  friend"). Usually in the first 15 minutes after the open, most trades done within 5 minutes. Happens 1-3 times
  a week; not every day.
- Three parts: bias, manipulation, entry.
- **Manipulation:** must trade into an FVG on the 5-minute chart or higher (or sweep an intermediate high/low
  inside an already-traded gap, or reach a breaker / EQ-area gap). If a high already rests in the gap, price must
  take it out first - not form relative equal highs.
- **Entry difference:** do NOT wait for the highest timeframe (you will rarely get an entry). Use 15s / 30s / 1m and
  take the time frame that shows ONE single FVG in the leg. If no time frame has a single gap, wait for all gaps in
  the leg to be closed through.
- Strong displacement and a protected stop are what matter; breakeven is often not needed; taking out a small
  internal high (the breakeven point) does not invalidate the trade.

## Asia session model (documented, not built yet)

- 8:00 p.m. - 12:00 a.m. New York. 6-8 p.m. is low-volatility pre-market (rarely traded).
- **Filter 1 - PO3 of the previous NY session:** if New York was choppy, did not take major draws and did not
  deliver ~500 points, Asia will likely manipulate and distribute (trade it). If New York expanded and took major
  draws, Asia will likely accumulate (skip it).
- **Filter 2 - FVG behaviour on 5m and 15m:** are FVGs being created, and are they respected? Barcode price action,
  gaps alternately disrespected = chop, skip.
- Same four steps; the 8 p.m. open should manipulate into the key level (OLHC/OHLC); highest-timeframe IFG of the
  manipulation leg (lower timeframe 30s/15s entries if confident).
- Asia moves slower, generates lots of lower-timeframe liquidity (ignore lower-timeframe equal highs/lows; focus
  on the higher-timeframe trend).
- New week opening gaps are "king" in Asia; trades toward the NWOG can start from about 7:00 p.m.
- Same rules: 1 win or 2 losses and done, 1% risk, stop at the swing, 1:1 default, 1:3 max. A win in Asia means no
  New York trade that day; a choppy Asia means wait for New York.

## London (mentioned only)

The 2:00 a.m. open can produce the same Judas swing. No separate rules were given.

---

## What he avoids (red flags)

- No clear bias or draw; conflicting higher-timeframe picture ("I just shouldn't have traded today").
- Choppy, wicky, barcode price action; no FVGs being created or gaps flipping back and forth.
- No manipulation at the open, or the open creating equal lows/highs instead of a sweep.
- Low-resistance liquidity sitting at your stop (unprotected stop).
- Targets at high-resistance liquidity.
- Entries before the candle closes through the gap in poor conditions.
- Huge stops (100+ points) for a small target.
- Shorting at all-time highs without bearish structure.
- Lower-timeframe IFG when a higher-timeframe gap exists in the same leg (NY model).
- Revenge trading, overtrading, moving stops, adding to losers, re-entering right after a stop.

## The recap trades (for reference)

| Date (2026) | Side | Bias reason | Key level | Confirmation | Stop / target | Result |
|---|---|---|---|---|---|---|
| May 13 | Long | ATH, bullish 4h/1h gaps held, equal highs | 5m FVG + 5m intermediate low (1h intermediate low below) | 30s IFG, then 1m IFG re-entry (a 4m IFG was the earlier textbook entry) | swing low / unfilled 1h gap | BE, then ~1R |
| (2-week examples) | Short | bearish PDAs respected | 1h rejection block + 15m CISD | 1m IFG | swing high / ~1:3 | win |
| (2-week examples) | Long | ATH | unfilled 15m FVG | 1m IFG | swing low / 1:1 | win |
| June 3 | Short | trend line + ES equal lows at PDL | 5m CISD after 15m bearish FVG + ATH sweep | 1m IFG | 1:1, order block | win (+1R) |
| June 4 | Long | sell model complete, 1h FVG, bullish 15m gap + CISD | sweep of stacked lows, bullish SMT (Judas) | 30s CISD + IFG | displacement candle / equal highs, ~1.2R | win |
| June 5 | Short | bearish gaps respected, equal lows, NWOG | 15m FVG, rejection wick, SMT | 5m IFG (took 3m late) | FVG high / 4h equal lows | win |
| June 10 | Long | rebalance after daily low, 4h unfilled gap | 15m intermediate low + BPR | 30s IFG + CISD | BE at internal high / buy side | win |
| June 24 | Short | 4h bearish FVG, PDL equal lows | 30m FVG + 1h high, bearish SMT, 5m CISD | 30s IFG | high / PDL, ~1.3R | win |
| July 21 | Long, then short | bullish until proven wrong | 15m FVG + rejection block | 30s CISD; then 1m IFG short | - | loss, then BE |
| Aug 10 (Asia) | Long | 4h bullish, NY choppy | daily FVG + 4h rejection block, 8 p.m. open low | 1m IFG | swing low / ~1.8R | win |
| Aug 18 | Short | disrespecting EQ, daily low + weekly FVG draw | 1h FVG + 1h rejection block, 5m internal high swept | 2m IFG | 2m body high / equal lows | win |
| (live stream) | Long | daily bullish, Friday high draw | 15m discounted FVG at EQ, 1h gap, ES SMT | 30s IFG | swing low / ~2.7R; BE; 15m rejection block re-entry | win |

---

## How this app turns it into rules (summary)

Everything below is a setting. The defaults are the app's best reading of the videos; they are NOT tuned.

- **Data:** 1-minute Dukascopy BID/ASK (the NQ stand-in, trades MNQ). Higher timeframes are built from it, aligned to
  New York time (4h candles start at 18:00, daily = 18:00-16:15). No 15s/30s data exists, so 1m is the lowest
  timeframe (you chose that). Where he uses 30s, the app uses 1m.
- **SMT:** needs ES data, which is not imported; the setting exists but is off and marked "needs ES data".
- **Bias:** a score from FVGs respected/disrespected on daily/4h/1h (optionally 15m and swing structure), decided
  before 9:30. No bias = no trade. A draw on liquidity must exist in the bias direction.
- **Key levels:** FVGs (fresh, or swept intermediate low inside), CISDs, rejection blocks (and optional BPRs) on the
  chosen timeframes, in premium/discount of the equilibrium range.
- **Manipulation leg:** from the last 1m swing high/low to the low/high that hit the key level (Judas: from the
  9:30 open). Optional: must sweep liquidity, must not form equal lows/highs.
- **Confirmation:** an IFG on 1-5m: highest timeframe (NY model) or the single-gap timeframe (Judas), body close
  through, optional displacement check.
- **Execution:** market at the next 1m open after the confirming close (or a limit at the gap), stop at the leg
  extreme (or other choices), target at the nearest liquidity between 1R and 3R (or a fixed R), optional breakeven
  at the leg's swing point or at an R multiple, optional trailing, 1 win or 2 trades per day, 1% risk.
- **Judgement calls** ("A+", "displacement", "chop") are numeric thresholds you can change.
- **Not built (documented above):** Asia, London, SMT (no ES data), 15s/30s entries, intraday bias flips after a
  failed trade, discretionary "A+" overrides.
