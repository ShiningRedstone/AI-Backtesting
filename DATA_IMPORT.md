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

## HistData NSXUSD (research proxy; evidence and open questions)

Files: `YYYYMMDD HHMMSS;open;high;low;close;volume`, no header (import a copy with the header
`ts;open;high;low;close;volume` prepended; never edit the raw file), bar OPEN, BID prices, volume 0
(`--volume-type none`). Instrument `NAS100_HISTDATA` (tick 0.001: raw prices are on a 0.001 grid);
it is a research proxy, not a tradable contract. Only the HISTDATA feed has costs: assumed
MNQ-equivalent research values (`CONFIG.md`), not broker-verified.

- **Timezone, evidence vs documentation:** HistData documents fixed EST without DST
  (`Etc/GMT+5`). Measured: summer FOMC 14:00 ET releases (2019-07-31, 2020-07-29, 2021-06-16,
  2022-07-27, 2024-07-31) land at 14:00 in the file, i.e. New York wall-clock time. The calendars
  therefore use `America/New_York` and imports should state `--source-timezone America/New_York`.
  The conflict is unresolved; this is an empirical reading, not a vendor statement.
- **Schedules:** R1 2017-2018 (`--calendar HISTDATA_NSX_R1`; a 16:16-16:29 pause is not modelled);
  R2 2019-2024 (`HISTDATA_NSX_R2`, default).
- **Known source anomaly:** on Sun-Thu evenings of the weeks when US and EU DST differ (2019:
  03-10..03-28 and 10-27..10-31) the file carries extra 17:00-17:59 NY bars. They fail
  `bars_outside_session` (about 600-1,200 bars a year), so 2019-2024 are refused unless an audited
  exclusion set is named. 2019, 2020, 2021, 2022 and 2024 have year-specific audited sets
  (`--source-exclusions HISTDATA_NSXUSD_<YEAR>`), one 17:00-18:00 NY window per evening on which
  the local trace of the real file found such bars (measured evidence, not a vendor statement).
  2023 has no set and remains coverage-rejected. Thresholds are not relaxed.
- **Coverage:** 2017 (7.6% missing under R1) and 2023 (13.6% under R2) are rejected as full-year
  datasets. 2018 is expected to import with WARNs (2.9% missing, measured locally with an
  equivalent in-memory calendar). No holidays are listed; they count as missing days.

## Dukascopy USATECH.IDX/USD (primary research source; Phase 9)

The file goes through the same pipeline as every other source (inspect, normalize, validate,
manifest and hashes, immutable store, derived timeframes). There is no separate importer. It is
stored as its own source identity and is never merged with or compared into HistData.

### Source identity (instrument `NQ_DUKASCOPY`)

It comes from the user's download code:

```python
dukascopy_python.fetch(INSTRUMENT_IDX_AMERICA_E_NQ_100, dukascopy_python.INTERVAL_MIN_1,
                       dukascopy_python.OFFER_SIDE_BID, START, END)
```

| Field | Value | Basis |
|---|---|---|
| Provider | DUKASCOPY | download code |
| Symbol | `USATECH.IDX/USD` (USA 100 Technical Index) | user-specified mapping of the constant |
| Feed code | `E_NQ-100` (`INSTRUMENT_IDX_AMERICA_E_NQ_100`) | verified in dukascopy-python 4.0.1 source |
| Asset class | `cfd` (Dukascopy index CFD); import with `--asset-type CFD` | user-specified; **not CME NQ futures** |
| Price basis | BID (`OFFER_SIDE_BID = "B"`) | download code |
| Timestamps | epoch ms → UTC (`utc=True`), bar open | library source |
| Volume | Dukascopy source volume (decimal, provider-defined), stored as `volume_type: unknown` | **not** CME exchange volume |
| Research proxy | yes | — |
| Economics | research units: 1 per index point on a 0.001 grid; min/step 0.01 | **not** CME ($20/pt) and not Dukascopy's contract size; no broker figures are known |
| `identity_status` | `user_specified` (not `source_verified`) | the symbol mapping could not be checked against Dukascopy's metadata from the build environment |

### Status gates (why research is refused today)

Two independent refusals are active, and both are shown on the dataset row and returned by the
API.

1. **The session calendar is not verified** (`calendar_status: provisional_unverified`).
   - The calendar is `DUKASCOPY_USATECH_OBSERVED`: America/New_York, 18:00 to 16:15, closed daily
     16:15-18:00, with no holidays listed.
   - This is the schedule measured in the real file's first inspection (SHA-256 `d92f25fc…c1d9`,
     1,709,068 rows): Sunday first bar 18:00, Friday last bar 16:14, 16:15-18:00 essentially
     empty, no Saturday bars.
   - The earlier CME-style assumption (close 17:00) counted 90,453 missing bars. 58,680 of those
     were this recurring 16:15-17:00 closure (45 bars × 1,304 trading dates).
   - Under the observed schedule, about 31,773 bars (1.825%) remain missing. That is a WARN, and
     assumes the file really has no bars at 16:15-16:59; re-inspecting confirms it.
   - The regular hours match Dukascopy's official range-of-markets entry for USATECH.IDX/USD, as
     supplied by the user: summer Sun-Fri 22:00-20:15 GMT, winter 23:00-21:15 GMT. That is
     18:00-16:15 New York time, provided "summer" follows US DST. There are 0 outside-session bars
     across 5 years, including the US/EU DST-mismatch weeks, which supports that reading.
   - The 14 whole missing trading days and early closes are not resolved, and no holiday dates are
     entered. The days are:
     2021-12-24, 2022-04-15, 2022-12-26, 2023-01-02, 2023-07-04, 2023-12-25, 2024-01-01, 2024-03-29,
     2024-12-25, 2025-01-01, 2025-04-18, 2025-12-25, 2026-01-01, 2026-04-03.
   - They coincide with US exchange full closures. This alone is not evidence: the same file has
     bars on other US holidays, for example 2023-04-07 (Good Friday 2023), 2022-07-04, 2024-07-04
     and 2025-07-04.
   - A date is entered in `holidays` / `early_closes` only with the corresponding entry from
     Dukascopy's Trading Breaks Calendar (date, instrument, GMT times) quoted in a comment. That
     calendar could not be retrieved from the build environment.
   - Genuine feed gaps, such as the long intra-session gaps on 2025-11-28, stay missing-bar warnings
     in any case.
   - Every backtest, search, validation and control is refused (HTTP `409 instrument_identity`)
     until the real file has been inspected and the calendar confirmed or replaced.
   - Then set `calendar_status: verified` and `calendar_evidence: "<what the inspection showed>"`.
     `verified` without evidence is refused.
2. **Costs are unconfigured.**
   - The Dukascopy profile is separate: `costs.symbols.NQ_DUKASCOPY` / `providers.DUKASCOPY`.
     HistData's `NAS100_HISTDATA@HISTDATA` never applies.
   - Backtests are refused (`409 cost_unconfigured`) until you enter commission/fees, spread,
     slippage (points) and financing, with `status: assumed` or `broker_verified` and a
     rationale in `notes`.
   - EdgeLab does not invent them.

Import, validation, gap analysis and the Preferred-dataset setting all work while these refusals
are active.

### Step 1: read-only inspection of the real file (stores nothing)

Windows PowerShell, in the workspace:

```powershell
cd C:\Users\Ethan\Documents\AI-Backtesting
git pull
python scripts\dukascopy_inspect.py "<PATH_TO>\nq_1min_5years.csv" --root . --out dukascopy_inspection.json
```

The script prints:

- **File facts:** source SHA-256 (checked before and after), row count and columns.
- **Data integrity:** missing fields per column, first and last timestamp (UTC), ordering, duplicate
  timestamps, bar spacing, price decimals and volume statistics.
- **Session evidence:** first and last bar per week (Sunday open, Friday close, in New York time),
  minutes of the day that are almost never present (the daily break), bars per hour in New York and
  in UTC, bars on Saturday, and DST offsets seen.
- **Gaps:** the distribution of gaps between bars, and rows per year.
- **Validation:** the full gate under the calendar, run in memory. Then gap classification, missing
  trading days and coverage by year.

Add `--calendar NAME` to test another calendar from `configs/data.yaml`. Nothing is imported or
written, apart from the optional `--out` JSON.

**Confirm the calendar from its output.** Re-run the command above after `git pull`, so that it
validates under `DUKASCOPY_USATECH_OBSERVED`. The calendar fits only if every one of these holds:

- the weekly first bar is `Sun 18:00` and the weekly last bar is `Fri 16:14` (New York time) across
  both DST regimes;
- the rarely present minutes are exactly `16:15-18:00`;
- there are no Saturday bars;
- validation passes with `bars_outside_session` at 0, or only a handful reported as WARN.

Then decide how to handle the missing trading days it lists (the 14 seen so far). Either add them
to `holidays` / `early_closes` in `configs/data.yaml` with the evidence for each, or accept them
as visible missing-day warnings. Never enter a date without evidence.

If so, set in `configs/instruments.yaml` → `NQ_DUKASCOPY`:

```yaml
calendar_status: verified
calendar_evidence: "dukascopy_inspection.json <sha256>, calendar DUKASCOPY_USATECH_OBSERVED: weekly Sun 18:00 open / Fri 16:14 last bar, 16:15-18:00 closure, no Saturday bars, <n> bars outside session, <n> missing days handled as <...>"
```

If the schedule differs, for example a shorter break or a different Friday close, add a calendar
that matches the evidence in `configs/data.yaml` and point the instrument at it. Never relax
validation thresholds to force a pass. Holidays count as missing trading days until they are
listed. The listed missing days are the evidence for which holidays to list.

### Step 2: import (after the calendar is confirmed)

```powershell
New-Item -ItemType Directory -Force data\import | Out-Null
Copy-Item "<PATH_TO>\nq_1min_5years.csv" data\import\nq_1min_5years.csv      # a copy; the original is never edited
python -m edgelab.cli --root . import data\import\nq_1min_5years.csv --profile dukascopy_utc_csv `
  --timeframe 1m --instrument NQ_DUKASCOPY --provider DUKASCOPY --asset-type CFD `
  --symbol "USATECH.IDX/USD" --price-basis bid --dataset-name NQ_DUKASCOPY_2021_2026 --derive 5m
python -m edgelab.cli --root . datasets
python -m edgelab.cli --root . dataset NQ_DUKASCOPY_2021_2026_5M_<hash> --quality
python -m edgelab.cli --root . prefer-dataset NQ_DUKASCOPY_2021_2026_5M_<hash>
```

- **Calendar:** add `--calendar NAME` only if Step 1 required a different calendar.
- **No source exclusions:** they are added only as an audited set backed by evidence from Step 1,
  never guessed.
- **Dataset ids:** `NQ_DUKASCOPY_2021_2026_1M_<10 hex of content hash>` and its child
  `NQ_DUKASCOPY_2021_2026_5M_<hash>`, with `parent_dataset_id` set to the 1m id. The hash depends
  on the bars, so it is known only after the import.
- **If validation fails,** nothing is stored and the report is written to
  `reports\imports\FAILED_<id>_quality.txt`.

**The same import in the GUI.** Datasets → Import Dataset, with:

- File: `data\import\nq_1min_5years.csv`
- Instrument: `NQ_DUKASCOPY`
- Provider: `DUKASCOPY`
- Asset type: `CFD`
- Timeframe: `1m`
- Source timezone: `UTC`
- Layout profile: `dukascopy_utc_csv`
- Price basis: `bid`
- Dataset name: `NQ_DUKASCOPY_2021_2026`
- Source symbol: `USATECH.IDX/USD`
- Derive timeframes: `5m`

Then Inspect file → Import. On the 5m row, choose "Set preferred".

### Preferred Research Dataset

- It is a workspace setting stored in `data/workspace_preferences.json`. It is not part of any run,
  strategy, dataset or the research config hash.
- Only a stored dataset that passed validation can be chosen, and it is re-validated (content hash
  re-checked) when set.
- The Strategy Lab and AI Discovery preselect it for new research when it is eligible for the
  strategy. While the calendar and cost refusals are active they show why it was not preselected.
- It never alters stored runs and is never set automatically.
- CLI: `prefer-dataset [ID] [--clear]`. API: `GET/POST /api/preferences/research-dataset`.

**Gap classification** (`dataset ID --quality`, or Datasets → Inspect) is descriptive only.

- It groups missing in-session bars into gaps and classifies each by length and position.
- It reports coverage per year and missing trading days.
- Nothing is filled or excluded.

## Audited source-quality exclusions (opt-in)

For a known, documented source defect that lies entirely outside the session (not a way to make
a failing file pass). Define a named set in `configs/data.yaml` and name it on import:

```yaml
source_exclusions:
  MY_SET:
    description: "what the anomaly is and how it was located"
    windows:     # half-open [start, end) bar-open times; quoted; explicit UTC offset; a reason each
      - {start: "2019-03-10T17:00:00-04:00", end: "2019-03-10T18:00:00-04:00", reason: "..."}
```

```bash
python -m edgelab.cli import FILE ... --calendar HISTDATA_NSX_R2 --source-exclusions HISTDATA_NSXUSD_2019
```

- Refused (nothing stored): unknown set, naive or unquoted timestamps, start >= end, overlapping
  windows (not merged), empty reasons, a window matching no bar, and any window containing a bar
  that the import calendar puts inside a session.
- Not excluded: anything not inside a listed window. The retained bars go through the normal gate
  with the normal thresholds, so other outside-session bars, gaps and duplicates are still reported.
- Recorded: `manifest.source_detail.source_exclusions` (set, set hash, rows before/excluded/after,
  each window with its reason and row count, SHA-256 of the removed rows) and `manifest.derivation`.
  The source file and its hash are unchanged; the dataset id differs from an unexcluded import.
