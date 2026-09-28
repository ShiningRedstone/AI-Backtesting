# Importing historical data (CFD or futures)

Status: the pipeline is **implemented and tested on synthetic files written in real provider
layouts**. No real data has been imported in this build, so nothing here is a claim about any
provider's data quality.

## Workflow

```
raw provider file
  -> inspect            (read-only: columns, spacing, decimals, hours that contain data)
  -> normalize          (timestamps -> bar OPEN, UTC, ns; columns -> canonical; spread -> points)
  -> validate           (Phase 1 gate: OHLC integrity, duplicates, gaps, session, grid, spread)
  -> manifest + hashes  (content hash of bars, sha256 of the source file, manifest hash)
  -> store              (immutable; id = <NAME>_<TF>_<first 10 hex of content hash>)
  -> derive timeframes  (session-anchored resample, each re-validated, linked to its parent)
  -> build features     (features.default_set, cached; unavailable ones listed with the reason)
  -> available to research (load_validated re-runs validation and checks hash + calendar)
```

If validation fails, **nothing is stored**; the report is written to
`reports/imports/FAILED_<id>_quality.txt` and the command exits with code 2.

## Expected file schema

Simplest form (`--profile generic_csv` or no profile):

```
ts,open,high,low,close,volume,spread
2024-03-04 00:00:00,18000.0,18002.25,17999.5,18001.0,331,1.5
```

| column | required | notes |
|---|---|---|
| `ts` | yes | any pandas-parsable datetime; or `--date-column` + `--time-column`; or epoch with `--epoch-unit` |
| `open`,`high`,`low`,`close` | yes | numeric; state the basis with `--price-basis bid|ask|mid|last` |
| `volume` | no | if absent: `volume_type=none`; volume features are unavailable, never zero-filled |
| `spread` | no | per-bar spread; `--spread-multiplier` converts file units to price points |
| bid/ask close | no | alternative to `spread`: `--bid-close-column` + `--ask-close-column` (spread = ask - bid at bar close) |

Column names can be anything: map them with `--columns "ts=Gmt time,open=Open,high=High,low=Low,close=Close,volume=Volume"`.
Timestamps may mark the bar open (`--timestamp-convention open`, default) or close (`close`).

## You must state (never guessed)

| option | examples | why |
|---|---|---|
| `--source-timezone` | `UTC`, `Europe/London`, `America/New_York+7h` | naive timestamps are ambiguous; MT4/MT5 servers commonly run at New York + 7h so midnight = 17:00 NY (DST-correct). **Verify for your broker.** |
| `--timestamp-convention` | `open` / `close` | a one-bar shift is invisible in charts and corrupts every result |
| `--instrument` | `NAS100_CFD`, `US100_CFD`, `NQ_CFD`, `NQ` | key in `configs/instruments.yaml` (tick grid, unit sizing, calendar) |
| `--provider` | your broker or vendor name | part of the dataset identity; feeds are never merged |
| `--asset-type` | `CFD`, `FUTURE` | kept on every result |
| `--price-basis` | `bid`, `ask`, `mid`, `last` | a bid feed and a mid feed differ by half a spread |

## Commands

Inspect first (stores nothing). The printed table shows bar counts per local weekday x hour:
use it to confirm the server clock (sessions should start at the hours you expect) and the
calendar (hours with data should match the calendar's session).

```bash
python -m edgelab.cli inspect FILE [layout options] --source-timezone TZ [--view-timezone America/New_York]
```

MetaTrader 5 "Bars" export (tab-separated `<DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>`):

```bash
python -m edgelab.cli import NAS100_M1.csv --profile mt5_export --timeframe 1m \
  --source-timezone "America/New_York+7h" --spread-multiplier 0.01 \
  --instrument NAS100_CFD --provider MYBROKER --asset-type CFD --symbol NAS100 \
  --price-basis bid --derive 5m,15m,60m
```

`--spread-multiplier` converts MT5 spread "points" (the symbol's price increment) into price
points: for a symbol quoted with 2 decimals, 150 -> 1.50, multiplier 0.01. Check your symbol's digits.

Dukascopy-style CSV (`Gmt time,Open,High,Low,Close,Volume`, UTC, one side per file):

```bash
python -m edgelab.cli import USA100_1m_BID.csv --profile dukascopy_csv --timeframe 1m \
  --instrument NAS100_CFD --provider DUKASCOPY --asset-type CFD --price-basis bid --derive 5m,15m
```

Generic UTC CSV with close-stamped bars and no volume:

```bash
python -m edgelab.cli import us100.csv --timeframe 1m --source-timezone UTC \
  --timestamp-convention close --instrument US100_CFD --provider VENDORX --asset-type CFD --price-basis mid
```

Futures (same pipeline):

```bash
python -m edgelab.cli import nq_1m.csv --timeframe 1m --source-timezone America/Chicago \
  --instrument NQ --provider MYVENDOR --asset-type FUTURE --price-basis last --derive 5m,15m,60m
```

Then:

```bash
python -m edgelab.cli datasets                         # all stored datasets
python -m edgelab.cli dataset <ID>                     # manifest, validation report, limitations
python -m edgelab.cli features cache <ID>              # cached features
python -m edgelab.cli compare-feeds <ID_A> <ID_B>      # shared bars, basis, return correlation
```

Add `--json` for machine-readable output and `--root DIR` to use another project directory.

Programmatic equivalent:

```python
from edgelab.services import Services
svc = Services(root=".")
svc.inspect_file(dict(file="NAS100_M1.csv", instrument="NAS100_CFD", provider="MYBROKER",
                      asset_type="CFD", timeframe="1m", profile="mt5_export",
                      source_timezone="America/New_York+7h", spread_multiplier=0.01))
res = svc.import_file({...same keys..., "price_basis": "bid", "derive_timeframes": ["5m", "15m"]})
ds = svc.load_dataset(res["dataset_id"])       # a ValidatedDataset, re-validated on load
```

## After importing

1. **Calendar.** CFD instruments default to `CME_EQUITY`. If `bars_outside_session` or
   `missing_bars` is large, your broker's hours differ: add a calendar in `configs/data.yaml` and
   re-import with `--calendar`. Holidays and early closes must be configured (none are bundled).
2. **Costs.** CFD cost profiles are unconfigured on purpose. Before any CFD backtest, enter your
   broker's numbers in `configs/costs.yaml` under `costs.symbols.<SYMBOL>.providers.<PROVIDER>`
   (see `CONFIG.md`). With a spread column you can use `spread_source: dataset`.
3. **Contract spec.** Set `min_size`, `size_step` and point value for your broker in
   `configs/instruments.yaml`.

## Behaviour you can rely on

- Re-importing the same file with the same options is idempotent (same id, nothing duplicated).
- A different file under the same name gets a different id; nothing is ever overwritten.
- Identical bar content under two providers/names is stored separately and flagged.
- Conflicting duplicate timestamps and impossible OHLC fail validation; exact duplicate rows are
  removed and recorded; missing bars are counted, never created.
- Local times inside a DST gap or a repeated hour are refused, not guessed.
- Changing a calendar after import makes `load_validated` refuse until you re-import (or pass
  `allow_calendar_change=True` deliberately).
