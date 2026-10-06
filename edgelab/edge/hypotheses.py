"""The frozen hypothesis set of the edge check (ADR-104, version 2 = ADR-105). Written BEFORE any result was seen;
changing it is a new version (every hypothesis ever tested stays in the Bonferroni family: never drop a failed one to make
the rest look better). Version 1 (H1-H4) was tested on the discovery period, all four "no evidence"; version 2 keeps
them unchanged and adds H5 and H6, so the family is 6.

H1-H4: window 9:30-11:00 New York time (the user's choice), every trade closed at the close of the 10:59 bar.
H5-H6: the two PUBLISHED end-of-day ideas, added at the user's request after H1-H4 failed: entry 15:30, exit at the close
of the 15:59 bar. One decision per hypothesis per day. All signals use bars that are complete at the decision; the trade
enters at the NEXT bar's open.

Day table (built by ``check.day_table``):
  o, h, l, c   (days x 390) BID prices of the regular-session minutes 9:30 ... 15:59 (NaN = missing minute); slots
               0..89 = the 9:30-11:00 window, 359 = 15:29, 360 = 15:30, 389 = 15:59
  prev_close   yesterday's regular-session close (close of the 15:59 bar)
  prev_high/low  yesterday's regular-session (9:30-15:59) high / low
A hypothesis returns, per day, a direction (+1 long, -1 short, 0 no signal) and the entry minute (slot 0..89).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

VERSION = 2
SLOTS = 90                 # 9:30 ... 10:59 (the morning window)
DAY_SLOTS = 390            # 9:30 ... 15:59 (the day table's width)
EXIT_SLOT = 89             # H1-H4 close at the close of the 10:59 bar
LAST_SLOT = 389            # H5-H6 close at the close of the 15:59 bar


@dataclass(frozen=True)
class Hypothesis:
    id: str
    name: str
    idea: str
    rule: str
    direction_meaning: str           # what a POSITIVE average means; a negative one means the opposite trade
    against: tuple[str, ...]         # reasons it may NOT work, written before the test
    parameters: dict = field(default_factory=dict)
    exit_slot: int = EXIT_SLOT       # the trade closes at this slot's close


H = [
    Hypothesis(
        "H1", "Opening drive",
        "The direction of the first 30 minutes continues into 10:00-11:00.",
        "Signal at the close of the 9:59 bar: long if the 9:59 close is above the 9:30 open, short if below. Enter at the "
        "10:00 open, exit at the 10:59 close.",
        "positive = the first 30 minutes continue; negative = they reverse",
        ("The published intraday-momentum result (Gao, Han, Li & Zhou, 2018) links the first half hour to the LAST half "
         "hour of the day, driven by end-of-day hedging; it says nothing about the next hour.",
         "Returns of liquid index futures over 30-60 minutes are close to unpredictable; where studies find anything at "
         "these horizons it is more often slight reversal than continuation.",
         "10:00 New York carries scheduled US data releases (ISM, consumer confidence, new home sales ...) that can "
         "overturn the morning's direction at exactly the entry time.",
         "'Trade with the opening drive' is one of the best-known ideas; a simple version that worked would be traded "
         "away.",
         "Even a real effect can be smaller than spread + commission + slippage over a one-hour hold."),
        {"decision": "9:59 close", "entry": "10:00 open"}),
    Hypothesis(
        "H2", "Overnight move",
        "The direction of the overnight move (yesterday's 16:00 close to today's 9:30 open) continues until 11:00.",
        "Signal at the close of the 9:30 bar: long if the 9:30 open is above yesterday's regular-session close (close of "
        "the 15:59 bar), short if below. Enter at the 9:31 open, exit at the 10:59 close. Every overnight move counts "
        "(no size threshold, so no threshold to tune).",
        "positive = the overnight move continues; negative = it fades ('gap fill')",
        ("NQ trades almost 24 hours: there is no real gap, only an overnight move that had all night to be priced.",
         "'Gaps fill' is usually a win-rate story (small targets) that says nothing about average profit.",
         "Large overnight moves come with news; news days continue or reverse without a stable pattern.",
         "Without a size threshold most signals are tiny, meaningless moves that dilute any real effect (accepted to "
         "avoid tuning a threshold).",
         "Published overnight / intraday patterns (e.g. Lou, Polk & Skouras, 2019) are about individual stocks, not a "
         "timing rule for an index."),
        {"decision": "9:30 close", "entry": "9:31 open", "threshold": "none"}),
    Hypothesis(
        "H3", "30-minute opening range breakout",
        "The first close beyond the 9:30-10:00 range starts a move in that direction.",
        "Opening range = high / low of 9:30-9:59. From 10:00 to 10:44 the first 1-minute close above the range high is "
        "a long, below the range low a short. Enter at the next bar's open, exit at the 10:59 close.",
        "positive = breakouts continue; negative = they fail and reverse",
        ("One of the most-tested retail strategies: a simple edge would be crowded out.",
         "A break after a strong first 30 minutes carries much of the same information as H1 (the tests are correlated).",
         "Entering after a close beyond the range buys late; false breaks that snap back are common around 10:00 data "
         "releases.",
         "Holding only until 11:00 cuts real trend days short while still paying full costs."),
        {"range": "9:30-9:59", "scan": "10:00-10:44", "entry": "next bar open"}),
    Hypothesis(
        "H4", "Prior-day high / low sweep and reversal",
        "Price runs yesterday's high (or low), fails and reverses: the liquidity-sweep idea at the core of the Blake / "
        "ICT model, in its plainest form.",
        "Between 9:30 and 10:29, the first minute that trades above yesterday's regular-session high (or below its low) "
        "after an open inside yesterday's range. If a 1-minute bar then closes back inside within 15 minutes (the sweep "
        "minute included), trade back the other way (short after a high sweep, long after a low sweep) at the next "
        "bar's open; exit at the 10:59 close. Only the first sweep of a day; a bar that trades beyond both sides is "
        "skipped.",
        "positive = sweeps reverse; negative = they continue (a breakout)",
        ("Fewer events (a few a month): less statistical power, so a small real effect may not be found.",
         "'15 minutes' and 'regular-session high' are single choices; other definitions could answer differently. They "
         "are NOT tested, on purpose, to avoid trying many versions.",
         "Breaking yesterday's high is also how trend days start; the reversal story may come from remembering only the "
         "sweeps that reversed.",
         "On the CFD stand-in for NQ, yesterday's high can differ slightly from the futures' high."),
        {"sweep": "9:30-10:29", "back_inside_within": "15 minutes", "level": "yesterday 9:30-15:59 high / low"}),
    Hypothesis(
        "H5", "First half hour predicts the last half hour",
        "The market's move from yesterday's close to 10:00 continues in the last 30 minutes of the day (Gao, Han, Li & "
        "Zhou, 2018: 'Market intraday momentum', S&P 500 ETF 1993-2013).",
        "Signal at the close of the 9:59 bar: long if the 9:59 close is above yesterday's regular-session close (close "
        "of the 15:59 bar), short if below. Enter at the 15:30 open, exit at the 15:59 close. Every move counts (no "
        "threshold).",
        "positive = the morning's direction continues into the close; negative = it reverses",
        ("Published in 2018 on data up to 2013; effects usually shrink or vanish once published, and NQ 2021+ is far "
         "from that sample.",
         "The paper's effect is small (a few basis points per day): even if it is still there it can be smaller than the "
         "spread and costs of a 30-minute trade.",
         "The paper finds the effect strongest on volatile days and in crisis periods (such as 2008); in calm years it "
         "can be absent, so a handful of days can carry the average.",
         "The signal's first part is the overnight move (H2's information) and its second part the first 30 minutes "
         "(H1's): the tests overlap, and H1 and H2 found nothing.",
         "Outside the user's 9:30-11:00 window and held into the close; a prop account that must be flat before 16:00 "
         "or reduces size into the close may not be able to trade it as tested."),
        {"decision": "9:59 close", "from": "yesterday's 15:59 close", "entry": "15:30 open", "exit": "15:59 close",
         "threshold": "none"},
        exit_slot=LAST_SLOT),
    Hypothesis(
        "H6", "Rest of the day predicts the last half hour",
        "The market's move from yesterday's close to 15:30 continues in the last 30 minutes: dealers hedging short "
        "options (gamma) and leveraged ETFs rebalancing trade in the direction of the day's move into the close "
        "(Baltussen, Da, Lammers & Martens, 2021: 'Hedging demand and market intraday momentum').",
        "Signal at the close of the 15:29 bar: long if the 15:29 close is above yesterday's regular-session close, short "
        "if below. Enter at the 15:30 open, exit at the 15:59 close. Every move counts (no threshold).",
        "positive = the day's direction continues into the close; negative = it reverses",
        ("Published in 2021 and widely discussed since; hedging flows into the close are now anticipated by other "
         "traders, which can move the price before 15:30 instead of after it.",
         "The paper says the effect depends on how much negative gamma dealers hold; that changes over time and is not "
         "measured here, so a real but time-varying effect can average out.",
         "Most days' moves are small; the effect in the paper comes mostly from large-move days, which are few.",
         "It shares the last half hour and much of the signal with H5: if H5 is chance, H6 is likely chance too (the "
         "two tests are strongly correlated).",
         "Outside the user's 9:30-11:00 window and held into the close (see H5); the CFD stand-in's close can differ "
         "from the futures' close."),
        {"decision": "15:29 close", "from": "yesterday's 15:59 close", "entry": "15:30 open", "exit": "15:59 close",
         "threshold": "none"},
        exit_slot=LAST_SLOT),
]
BY_ID = {x.id: x for x in H}
FAMILY = len(H)                      # Bonferroni family = every hypothesis of the set


def _nan(a) -> bool:
    return not np.isfinite(a)


def signals(hid: str, t: dict) -> tuple[np.ndarray, np.ndarray]:
    """(direction, entry slot) per day for hypothesis ``hid``. Direction 0 = no signal on that day."""
    o, h, l, c = t["o"], t["h"], t["l"], t["c"]
    n = o.shape[0]
    d = np.zeros(n, dtype=np.int8)
    e = np.full(n, -1, dtype=np.int16)
    for i in range(n):
        if hid == "H1":
            a, b = o[i, 0], c[i, 29]
            if _nan(a) or _nan(b) or a == b:
                continue
            d[i], e[i] = (1 if b > a else -1), 30
        elif hid == "H2":
            a, b = t["prev_close"][i], o[i, 0]
            if _nan(a) or _nan(b) or a == b:
                continue
            d[i], e[i] = (1 if b > a else -1), 1
        elif hid == "H3":
            if np.isnan(h[i, :30]).all():
                continue
            hi, lo = np.nanmax(h[i, :30]), np.nanmin(l[i, :30])
            for s in range(30, 75):
                x = c[i, s]
                if _nan(x):
                    continue
                if x > hi:
                    d[i], e[i] = 1, s + 1
                    break
                if x < lo:
                    d[i], e[i] = -1, s + 1
                    break
        elif hid in ("H5", "H6"):
            a, b = t["prev_close"][i], c[i, 29 if hid == "H5" else 359]
            if _nan(a) or _nan(b) or a == b:
                continue
            d[i], e[i] = (1 if b > a else -1), 360
        elif hid == "H4":
            ph, pl, op = t["prev_high"][i], t["prev_low"][i], o[i, 0]
            if _nan(ph) or _nan(pl) or _nan(op) or not (pl <= op <= ph):
                continue
            for s in range(0, 60):
                up, dn = h[i, s] > ph, l[i, s] < pl
                if not (up or dn):
                    continue
                if up and dn:
                    break
                level = ph if up else pl
                for k in range(s, min(s + 15, EXIT_SLOT)):
                    x = c[i, k]
                    if _nan(x):
                        continue
                    if (up and x < level) or (dn and x > level):
                        d[i], e[i] = (-1 if up else 1), k + 1
                        break
                break
        else:
            raise KeyError(hid)
    return d, e


def fingerprint() -> str:
    from edgelab.core.identity import hash_obj
    return hash_obj({"version": VERSION, "slots": SLOTS, "day_slots": DAY_SLOTS,
                     "hypotheses": [{"id": x.id, "rule": x.rule, "parameters": x.parameters, "exit": x.exit_slot}
                                    for x in H]}, 16)


def manifest() -> dict:
    return {"version": VERSION, "fingerprint": fingerprint(), "family": FAMILY,
            "window": "H1-H4: 9:30-11:00 New York; H5-H6: 15:30-16:00",
            "hypotheses": [{"id": x.id, "name": x.name, "idea": x.idea, "rule": x.rule,
                            "direction_meaning": x.direction_meaning, "against": list(x.against),
                            "parameters": x.parameters, "exit": "10:59 close" if x.exit_slot == EXIT_SLOT else "15:59 close",
                            "added_in": 1 if x.exit_slot == EXIT_SLOT else 2} for x in H]}
