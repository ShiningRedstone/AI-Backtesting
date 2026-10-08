"""Live charts (ADR-111): MNQ / NQ / ES / MES prices from Dukascopy for the Charts tab.

Source (chosen by the user: the only free, unlimited, no-account source with NASDAQ and S&P data): Dukascopy's index CFD
quotes via dukascopy-python, BID side - USATECH.IDX/USD (feed E_NQ-100) for NQ and MNQ, USA500.IDX/USD (E_SandP-500) for
ES and MES. These are NOT the exchange's futures prices: they follow the CME contracts closely, but the price level can
differ by a few points (shown on the chart). MNQ draws the same series as NQ and MES the same as ES (same index, the
contracts differ only in point value). This is the series the Market simulator was trained on.

Data: 1-minute bars (cached per UTC day once the day is complete) for timeframes below one hour or not a whole number of
hours; 1-hour bars (cached per UTC month) for whole-hour, daily, weekly and monthly timeframes. Bars of every timeframe
are anchored at the 18:00 New York session open like the rest of the app (a trading date = NY date of time + 6 h; the
weekly bar = Monday-based week of trading dates; the monthly bar = month of the trading date). Nothing is filled or
invented: a bar is built from the minutes / hours that exist. Never a research dataset, never a run.

Live: while a chart polls, a thread per instrument re-fetches the last minutes every POLL_SECONDS and merges them; it
stops when nobody has asked for LIVE_IDLE_SECONDS. Download problems are kept and shown.
"""
from __future__ import annotations

import threading
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from edgelab.market import data as D

SYMBOLS = {
    "NQ": {"code": "E_NQ-100", "name": "E-mini Nasdaq-100", "point_value": 20.0, "tick": 0.25, "family": "Nasdaq-100"},
    "MNQ": {"code": "E_NQ-100", "name": "Micro E-mini Nasdaq-100", "point_value": 2.0, "tick": 0.25, "family": "Nasdaq-100"},
    "ES": {"code": "E_SandP-500", "name": "E-mini S&P 500", "point_value": 50.0, "tick": 0.25, "family": "S&P 500"},
    "MES": {"code": "E_SandP-500", "name": "Micro E-mini S&P 500", "point_value": 5.0, "tick": 0.25, "family": "S&P 500"},
}
SOURCE = {"E_NQ-100": "Dukascopy USATECH.IDX/USD (BID), the index CFD that follows NQ",
          "E_SandP-500": "Dukascopy USA500.IDX/USD (BID), the index CFD that follows ES"}
TIMEFRAMES = (1, 2, 3, 5, 10, 15, 30, 45, 60, 120, 180, 240, D.DAY, 7 * D.DAY, 31 * D.DAY)   # minutes; W / M marked
WEEK, MONTH = 7 * D.DAY, 31 * D.DAY
POLL_SECONDS = 2.0
LIVE_IDLE_SECONDS = 90.0
LIVE_WINDOW = timedelta(minutes=20)
MAX_COUNT = 5000
NS = 1_000_000_000


class ChartError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def tf_label(tf: int) -> str:
    if tf == MONTH:
        return "1M"
    if tf == WEEK:
        return "1W"
    if tf == D.DAY:
        return "1D"
    return f"{tf // 60}h" if tf % 60 == 0 else f"{tf}m"


def parse_tf(x) -> int:
    """'5', '5m', '2h', '1D', '1W', '1M' -> minutes (1W / 1M are markers). Custom minutes (1 ... 1439) and hours allowed."""
    s = str(x).strip()
    if s in ("1D", "D"):
        return D.DAY
    if s in ("1W", "W"):
        return WEEK
    if s in ("1M", "M"):
        return MONTH
    try:
        v = int(s[:-1]) * 60 if s.endswith("h") else int(s[:-1] if s.endswith("m") else s)
    except ValueError:
        raise ChartError("BAD_TIMEFRAME", f"Unknown timeframe {x!r}.") from None
    if v in (D.DAY, WEEK, MONTH):
        return v
    if not 1 <= v < D.DAY:
        raise ChartError("BAD_TIMEFRAME", "A timeframe is 1 minute ... 23 hours, 1D, 1W or 1M.")
    return v


def _hourly(tf: int) -> bool:
    return tf >= 60 and tf % 60 == 0


def dukascopy_fetch(code: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
    import dukascopy_python as dp
    iv = {"1m": dp.INTERVAL_MIN_1, "1h": dp.INTERVAL_HOUR_1}[interval]
    return dp.fetch(code, iv, dp.OFFER_SIDE_BID, start, end)


def _frame(df: pd.DataFrame | None, start: datetime, end: datetime) -> pd.DataFrame:
    cols = ["open", "high", "low", "close", "volume"]
    if df is None or not len(df):
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC").as_unit("ns"))
    idx = pd.DatetimeIndex(df.index)
    idx = (idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")).as_unit("ns")
    out = pd.DataFrame({c: pd.to_numeric(df[c], errors="coerce").to_numpy(float) if c in df else np.zeros(len(df))
                        for c in cols}, index=idx)
    out = out[(out.index >= pd.Timestamp(start)) & (out.index < pd.Timestamp(end))]
    out = out[np.isfinite(out[["open", "high", "low", "close"]]).all(axis=1)]
    out[["open", "high", "low", "close"]] = out[["open", "high", "low", "close"]].round(6)   # exact through the CSV cache
    return out[~out.index.duplicated(keep="last")].sort_index()


class Feed:
    """History cache + live pollers for the Dukascopy chart series."""

    def __init__(self, data_root, fetch: Callable | None = None, now: Callable[[], datetime] | None = None):
        self.root = Path(data_root) / "charts" / "cache"
        self.fetch = fetch or dukascopy_fetch
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lock = threading.Lock()
        self.recent: dict[str, pd.DataFrame] = {}
        self.status: dict[str, dict] = {}
        self.asked: dict[str, float] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.mem: dict[tuple, tuple[float, pd.DataFrame]] = {}
        self.done: dict[Path, pd.DataFrame] = {}                        # complete chunks already read from disk

    # ------------------------------------------------------------------ history
    def _cache_path(self, code: str, interval: str, key: str) -> Path:
        p = self.root / code / interval
        p.mkdir(parents=True, exist_ok=True)
        return p / f"{key}.csv.gz"

    def _chunk(self, code: str, interval: str, start: datetime, end: datetime, key: str) -> pd.DataFrame:
        """One UTC day (1m) or UTC month (1h). Complete chunks are cached on disk; the open one for 60 s in memory."""
        complete = end <= self.now() - timedelta(minutes=30)
        p = self._cache_path(code, interval, key)
        if complete and p in self.done:
            return self.done[p]
        if complete and p.exists():
            df = _frame(pd.read_csv(p, index_col=0, parse_dates=True), start, end)
            return self._keep(p, df)
        mk = (code, interval, key)
        hit = self.mem.get(mk)
        if not complete and hit and _time.time() - hit[0] < 60:
            return hit[1]
        try:
            df = _frame(self.fetch(code, interval, start, min(end, self.now() + timedelta(minutes=1))), start, end)
        except Exception as e:                                         # network / package: shown, retried next time
            self._problem(code, f"{type(e).__name__}: {e}")
            raise ChartError("DOWNLOAD_FAILED", f"Dukascopy download failed ({type(e).__name__}). It is retried "
                                                f"automatically; check the internet connection.") from e
        if complete:
            self._store(p, df)
        else:
            self.mem[mk] = (_time.time(), df)
        return df

    def _keep(self, p: Path, df: pd.DataFrame) -> pd.DataFrame:
        with self.lock:
            self.done[p] = df
            while len(self.done) > 600:
                self.done.pop(next(iter(self.done)))
        return df

    def _prefetch(self, code: str, interval: str, chunks: list[tuple[datetime, datetime, str]], max_rows: int) -> None:
        """Downloads runs of consecutive complete chunks that are not cached yet in one request each (fewer round trips
        on a first load) and caches them chunk by chunk. Chunks still missing afterwards are fetched one by one."""
        cutoff = self.now() - timedelta(minutes=30)
        todo = [c for c in chunks if c[1] <= cutoff and self._cache_path(code, interval, c[2]) not in self.done
                and not self._cache_path(code, interval, c[2]).exists()]
        per = 1440 if interval == "1m" else 24 * 31
        run: list = []
        for c in todo + [None]:
            if c is not None and run and c[0] == run[-1][1] and (len(run) + 1) * per <= max_rows:
                run.append(c)
                continue
            if len(run) > 1:
                try:
                    df = _frame(self.fetch(code, interval, run[0][0], run[-1][1]), run[0][0], run[-1][1])
                except Exception as e:                                 # retried chunk by chunk (and reported) below
                    self._problem(code, f"{type(e).__name__}: {e}")
                else:
                    for a, b, key in run:
                        part = df[(df.index >= pd.Timestamp(a)) & (df.index < pd.Timestamp(b))]
                        self._store(self._cache_path(code, interval, key), part)
            run = [c] if c is not None else []

    def _store(self, p: Path, df: pd.DataFrame) -> None:
        tmp = p.with_name(p.name + f".{threading.get_ident()}.tmp")
        df.to_csv(tmp, compression="gzip")
        tmp.replace(p)
        self._keep(p, df)

    def minutes(self, code: str, start: datetime, end: datetime) -> pd.DataFrame:
        days, d = [], datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        while d < end:
            nd = d + timedelta(days=1)
            if d.weekday() != 5:                                        # Saturday: the market is closed all day (UTC)
                days.append((d, nd, d.strftime("%Y-%m-%d")))
            d = nd
        if len(days) > 2:
            self._prefetch(code, "1m", days, 20_000)
        parts = [self._chunk(code, "1m", a, b, key) for a, b, key in days]
        df = pd.concat(parts) if parts else _frame(None, start, end)
        return df[(df.index >= pd.Timestamp(start)) & (df.index < pd.Timestamp(end))]

    def hours(self, code: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Hourly bars: one chunk per UTC month (a past month is complete once it has ended)."""
        months, d = [], datetime(start.year, start.month, 1, tzinfo=timezone.utc)
        while d < end:
            nd = datetime(d.year + (d.month == 12), d.month % 12 + 1, 1, tzinfo=timezone.utc)
            months.append((d, nd, d.strftime("%Y-%m")))
            d = nd
        if len(months) > 2:
            self._prefetch(code, "1h", months, 25_000)
        parts = [self._chunk(code, "1h", a, b, key) for a, b, key in months]
        df = pd.concat(parts) if parts else _frame(None, start, end)
        return df[(df.index >= pd.Timestamp(start)) & (df.index < pd.Timestamp(end))]

    def _problem(self, code: str, msg: str | None):
        with self.lock:
            st = self.status.setdefault(code, {})
            if msg is None:
                st.update(error=None, last_ok=self.now().isoformat())
            else:
                st.update(error=msg, last_error=self.now().isoformat())

    # ------------------------------------------------------------------ live
    def _poll_once(self, code: str) -> None:
        now = self.now()
        df = _frame(self.fetch(code, "1m", now - LIVE_WINDOW, now + timedelta(minutes=1)), now - LIVE_WINDOW,
                    now + timedelta(minutes=1))
        with self.lock:
            old = self.recent.get(code)
            merged = df if old is None else pd.concat([old[old.index < (df.index[0] if len(df) else now)], df])
            self.recent[code] = merged[merged.index >= pd.Timestamp(now - timedelta(hours=6))]
        self._problem(code, None)

    def _run(self, code: str) -> None:
        while _time.time() - self.asked.get(code, 0) < LIVE_IDLE_SECONDS:
            try:
                self._poll_once(code)
            except Exception as e:                                     # noqa: BLE001 - kept, shown, retried
                self._problem(code, f"{type(e).__name__}: {e}")
            _time.sleep(POLL_SECONDS)
        with self.lock:
            self.threads.pop(code, None)

    def touch(self, code: str, start_thread: bool = True) -> None:
        self.asked[code] = _time.time()
        if not start_thread:
            return
        with self.lock:
            if code not in self.threads:
                t = threading.Thread(target=self._run, args=(code,), daemon=True, name=f"charts-live-{code}")
                self.threads[code] = t
                t.start()

    # ------------------------------------------------------------------ bars
    def bars(self, symbol: str, tf: int, to_ns: int | None = None, count: int = 1500, live: bool = True) -> dict:
        if symbol not in SYMBOLS:
            raise ChartError("BAD_SYMBOL", f"Unknown symbol {symbol!r}: MNQ, NQ, ES or MES.")
        count = max(10, min(int(count), MAX_COUNT))
        code = SYMBOLS[symbol]["code"]
        now = self.now()
        end = min(datetime.fromtimestamp(to_ns / NS, timezone.utc), now + timedelta(minutes=1)) if to_ns else now + timedelta(minutes=1)
        hourly = _hourly(tf) or tf >= D.DAY
        span_min = tf * count * (1.5 if tf < D.DAY else 1.6) + 3 * 1440
        start = end - timedelta(minutes=span_min)
        start = max(start, datetime(2010, 1, 1, tzinfo=timezone.utc))
        df = self.hours(code, start, end) if hourly else self.minutes(code, start, end)
        if live and to_ns is None:
            with self.lock:
                rc = self.recent.get(code)
            if rc is not None and len(rc):                              # the live minutes replace the history from there on
                cut = rc.index[0].ceil("h") if hourly else rc.index[0]
                df = pd.concat([df[df.index < cut], rc[rc.index >= cut]])
        out = resample(df, tf)
        if to_ns:
            out = [b for b in out if b["time"] * NS < to_ns]
        more = len(out) > count or start > datetime(2010, 1, 2, tzinfo=timezone.utc)
        out = out[-count:]
        st = self.status.get(code, {})
        return {"symbol": symbol, "tf": tf, "tf_label": tf_label(tf), "bars": out, "more": bool(more and len(out)),
                "source": SOURCE[code], "status": {"error": st.get("error"), "last_ok": st.get("last_ok")}}

    def live_bars(self, symbol: str, tf: int, since_s: int) -> dict:
        """Bars whose bucket starts at or after ``since_s`` (the last one or two), from history + the live poller."""
        if symbol not in SYMBOLS:
            raise ChartError("BAD_SYMBOL", f"Unknown symbol {symbol!r}.")
        code = SYMBOLS[symbol]["code"]
        self.touch(code)
        res = self.bars(symbol, tf, count=3 if tf < D.DAY else 2)
        res["bars"] = [b for b in res["bars"] if b["time"] >= since_s]
        return res


def resample(df: pd.DataFrame, tf: int) -> list[dict]:
    """Minute / hour bars -> bars of ``tf`` anchored at the 18:00 NY session open (D / W / M by trading date)."""
    if df is None or not len(df):
        return []
    df = df[~df.index.duplicated(keep="last")].sort_index()
    ts = pd.DatetimeIndex(df.index).as_unit("ns").asi8
    m = D.Minute(ts - ts % (60 * NS), df["open"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy())
    vol = df["volume"].to_numpy(float) if "volume" in df else np.zeros(len(df))
    if tf < D.DAY:
        b = D.resample(m, tf)
        v = np.add.reduceat(vol, b.i0) if len(b) else np.zeros(0)
        return [{"time": int(b.ts[k] // NS), "open": float(b.o[k]), "high": float(b.h[k]), "low": float(b.l[k]),
                 "close": float(b.c[k]), "volume": float(v[k])} for k in range(len(b))]
    day = m.day.astype("datetime64[D]")
    if tf == WEEK:
        key = (day.astype(np.int64) - 4) // 7                       # Monday-based weeks (1970-01-05 = Monday)
        stamp = lambda k: np.datetime64(int(k) * 7 + 4, "D")
    elif tf == MONTH:
        key = day.astype("datetime64[M]").astype(np.int64)
        stamp = lambda k: np.datetime64(int(k), "M").astype("datetime64[D]")
    else:
        key = day.astype(np.int64)
        stamp = lambda k: np.datetime64(int(k), "D")
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    ends = np.r_[starts[1:], len(key)] - 1
    hi = np.maximum.reduceat(m.h, starts)
    lo = np.minimum.reduceat(m.l, starts)
    v = np.add.reduceat(vol, starts)
    out = []
    for q, (s, e) in enumerate(zip(starts, ends)):
        t = stamp(key[s]).astype("datetime64[s]").astype(np.int64)
        out.append({"time": int(t), "open": float(m.o[s]), "high": float(hi[q]), "low": float(lo[q]), "close": float(m.c[e]),
                    "volume": float(v[q])})
    return out
