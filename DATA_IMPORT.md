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

## Dukascopy Nasdaq-100 (primary research source; Phase 9)

The Dukascopy file goes through the SAME pipeline as every other source (inspect, normalize,
validate, manifest and hashes, immutable store, derived timeframes). There is no separate importer.
It is stored as its own source identity and is never merged with or compared into HistData.

**File layout** (profile `dukascopy_utc_csv`): `timestamp,open,high,low,close,volume`, 1m bars, an
explicit UTC offset on every row (for example `2021-09-28T00:00:00+00:00`). Rows are read as absolute
instants and converted exactly to UTC. A file that mixes rows with and without offsets is refused.
So is any `+Nh` shift. Volume is a decimal, provider-defined measure: it is recorded as
`volume_type: unknown`, never as exchange-traded volume.

**Source identity**: instrument `NQ_DUKASCOPY` in `configs/instruments.yaml`.

- It is **PROVISIONAL**. The file name is not evidence that this is CME NQ, and nothing about it is
  assumed.
- `source_symbol`, `asset_class` and `identity_evidence` are empty. Price basis is `unknown` unless
  you state it.
- The tick and point values are **research units** (1 per index point, 0.001 grid). They are not
  broker or exchange figures.
- The manifest symbol is `UNSTATED`, because the file carries no symbol.

While the identity is provisional, the dataset imports, validates and can be inspected. However,
**every** backtest, search, validation, control and prop simulation built on a run is refused, with
the reason shown (HTTP `409 instrument_identity`).

**Source identity metadata step** (you state it; EdgeLab never fills it in). Edit
`configs/instruments.yaml` → `NQ_DUKASCOPY`:

1. Set `source_symbol` to the Dukascopy instrument you downloaded (for example the name shown in
   Dukascopy's download tool), and `asset_class` (for example `cfd`).
2. Confirm or correct `tick_size`, `tick_value`, `point_value`, `min_size` and `size_step`. Keep
   `point_value` as research units unless you have the real contract economics.
3. Set `identity_status: user_specified`. Or set `source_verified`, and add `identity_evidence:
   "<where you verified it>"`. `source_verified` without evidence is refused.
4. State the price side when you import (`--price-basis bid|ask|mid`) if you know it.

**Costs**: the source has its own profile `costs.symbols.NQ_DUKASCOPY` (and `providers.DUKASCOPY`).

- Both ship `status: unconfigured`, so the import works but research is refused until you enter
  numbers.
- The numbers to enter: `commission_per_side`, `fees_per_side`, `spread_points` (or `spread_source`),
  `slippage_ticks_market` / `slippage_ticks_stop` in points, `financing_mode`, plus a rationale in
  `notes`.
- Set `status: assumed` (a research assumption) or `broker_verified`.
- HistData's assumed costs are never reused: `NAS100_HISTDATA@HISTDATA` applies only to the HISTDATA
  feed.

**Calendar**: the source timestamps are UTC, and the session calendar is a separate choice.

- `DUKASCOPY_NQ_PROVISIONAL` assumes the CME equity-index Globex window: Sun 18:00 to Fri 17:00 New
  York, with a daily 17:00-18:00 pause and no holidays listed. This is **not yet measured on the real
  file**.
- Before importing, run `inspect` and read the "bars per local hour" table.
- If the file has bars in hours this calendar calls closed, the import is refused
  (`bars_outside_session`). If it lacks hours the calendar expects, the missing-bar ratio rises;
  above 5% the import is refused.
- In either case, add a calendar to `configs/data.yaml` that matches the measured schedule and pass
  `--calendar`. Never relax the thresholds to make a file pass.
- The entry session (for example `NY_RTH`) and flattening are part of each strategy, not of the
  dataset.
- Holidays count as missing trading days until they are listed.
- The daily break is modelled only as the single 17:00-18:00 pause.

**Gap classification** (`dataset ID --quality`, or Datasets → Inspect → "Gap classification &
coverage") is descriptive only.

- It groups the missing in-session bars into gaps, then classifies each by length (single bar,
  short up to 15 min, medium up to 2 h, long, whole day) and by position (session open, session
  close, intra-session, whole trading day).
- It also gives coverage per year and a list of missing trading days.
- Nothing is filled or excluded. An exclusion still needs an audited `source_exclusions` set (below).

### Import your file (local; the real CSV is not in this repository)

Windows PowerShell, in your research workspace (the repository checkout
`C:\Users\Ethan\Documents\AI-Backtesting` is also the workspace). Replace the source path with the
location of your CSV:

```powershell
cd C:\Users\Ethan\Documents\AI-Backtesting
git pull                                            # brings the Dukascopy profile, instrument, calendar, costs
New-Item -ItemType Directory -Force data\import | Out-Null
Copy-Item "<path to your Dukascopy CSV>" data\import\nq_dukascopy_2021_2026_1m.csv   # a copy; the original is never edited

# 1. dry run: how the file is read + bars per local hour (stores nothing)
python -m edgelab.cli --root . inspect data\import\nq_dukascopy_2021_2026_1m.csv --profile dukascopy_utc_csv --timeframe 1m

# 2. import the immutable 1m dataset and derive its 5m child (lineage recorded)
python -m edgelab.cli --root . import data\import\nq_dukascopy_2021_2026_1m.csv --profile dukascopy_utc_csv `
  --timeframe 1m --instrument NQ_DUKASCOPY --provider DUKASCOPY --asset-type unspecified `
  --dataset-name NQ_DUKASCOPY_2021_2026 --derive 5m
#    add --price-basis bid|ask|mid and --symbol <Dukascopy symbol> only if you know them
#    add --calendar <NAME> if step 1 showed a different schedule (see "Calendar" above)

# 3. review: ids, validation, identity, gaps
python -m edgelab.cli --root . datasets
python -m edgelab.cli --root . dataset NQ_DUKASCOPY_2021_2026_5M_<hash> --quality

# 4. make the 5m dataset the workspace's Preferred Research Dataset (a default for NEW research)
python -m edgelab.cli --root . prefer-dataset NQ_DUKASCOPY_2021_2026_5M_<hash>
```

The same import in the GUI:

1. Copy the CSV into the workspace's `data\import` folder.
2. Go to Datasets → Import Dataset and choose the file.
3. Fill in the fields:
   - Instrument `NQ_DUKASCOPY`
   - Provider `DUKASCOPY`
   - Asset type `unspecified`
   - Timeframe `1m`
   - Source timezone `UTC`
   - Layout profile `dukascopy_utc_csv`
   - Price basis `unknown` (unless known)
   - Dataset name `NQ_DUKASCOPY_2021_2026`
   - Derive timeframes `5m`
4. Click Inspect file, then Import.
5. On the 5m row, click "Set preferred".

Research on it stays refused until the identity step and the cost step above are done. Both are
shown on the dataset row.

**Preferred Research Dataset**: a workspace setting stored in `data/workspace_preferences.json`. It
is not part of any run, strategy, dataset or the research config hash.

- Only a stored dataset that passed validation (PASS/WARN/INFO) is eligible, and it is re-validated
  (content hash re-checked) when set.
- The Strategy Lab (backtest, batch research, validation) and AI Discovery preselect it for new
  research when it is eligible for that strategy.
- Changing it never alters stored runs, and it is not set automatically.
- CLI: `prefer-dataset [ID] [--clear]`. API: `GET/POST /api/preferences/research-dataset`.

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
