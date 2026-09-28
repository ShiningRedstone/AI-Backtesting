"""EdgeLab command line (thin wrapper over edgelab.services - the same calls a web UI makes).

    python -m edgelab.cli inspect FILE [layout options] --source-timezone TZ
    python -m edgelab.cli import  FILE --instrument NAS100_CFD --provider X --asset-type CFD \\
                                        --timeframe 1m --source-timezone TZ [options]
    python -m edgelab.cli datasets
    python -m edgelab.cli dataset DATASET_ID
    python -m edgelab.cli features list | docs | build DATASET_ID | cache DATASET_ID
    python -m edgelab.cli compare-feeds DATASET_A DATASET_B
    python -m edgelab.cli sessions

Add --json to any command for machine-readable output.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _layout_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("file")
    p.add_argument("--profile", help="preset from configs/import_profiles.yaml (generic_csv, mt5_export, ...)")
    p.add_argument("--timeframe", default="1m")
    p.add_argument("--source-timezone", help="IANA zone (UTC, Europe/London) or broker server time "
                                             "like America/New_York+7h. REQUIRED for import.")
    p.add_argument("--timestamp-convention", default="open", choices=("open", "close"))
    p.add_argument("--delimiter", default=",")
    p.add_argument("--columns", default=None,
                   help="canonical=file mapping, e.g. ts=Gmt time,open=Open,high=High,low=Low,close=Close,volume=Volume")
    p.add_argument("--date-column")
    p.add_argument("--time-column")
    p.add_argument("--datetime-format")
    p.add_argument("--epoch-unit", choices=("s", "ms", "us", "ns"))
    p.add_argument("--spread-column")
    p.add_argument("--spread-multiplier", type=float)
    p.add_argument("--bid-close-column")
    p.add_argument("--ask-close-column")
    p.add_argument("--volume-type", choices=("exchange", "tick", "none"))


def _options(a, for_import: bool) -> dict:
    cols = {}
    if a.columns:
        for pair in a.columns.split(","):
            k, v = pair.split("=", 1)
            cols[k.strip()] = v.strip()
    d = dict(file=a.file, instrument=getattr(a, "instrument", None) or "UNSPECIFIED",
             provider=getattr(a, "provider", None) or "UNSPECIFIED",
             asset_type=getattr(a, "asset_type", None) or "unspecified", timeframe=a.timeframe,
             source_timezone=a.source_timezone, timestamp_convention=a.timestamp_convention,
             delimiter=a.delimiter.encode().decode("unicode_escape"), columns=cols,
             date_column=a.date_column, time_column=a.time_column, datetime_format=a.datetime_format,
             epoch_unit=a.epoch_unit, spread_column=a.spread_column, spread_multiplier=a.spread_multiplier,
             bid_close_column=a.bid_close_column, ask_close_column=a.ask_close_column,
             volume_type=a.volume_type, profile=a.profile)
    if for_import:
        d.update(symbol=a.symbol or "", dataset_name=a.dataset_name, calendar=a.calendar,
                 price_basis=a.price_basis, notes=a.notes or "",
                 derive_timeframes=[x for x in (a.derive or "").split(",") if x],
                 build_features=not a.no_features)
    return d


def _print(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=1, default=str))
        return
    if isinstance(obj, list):
        for row in obj:
            print("  ".join(f"{k}={v}" for k, v in row.items()))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and len(json.dumps(v, default=str)) > 100:
                print(f"{k}:")
                print("  " + json.dumps(v, indent=1, default=str).replace("\n", "\n  "))
            else:
                print(f"{k}: {v}")
    else:
        print(obj)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="edgelab", description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=".", help="project root (contains configs/)")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("inspect", help="dry-run: show how a raw file would be read (stores nothing)")
    _layout_args(p)
    p.add_argument("--view-timezone", default="America/New_York")

    p = sub.add_parser("import", help="validate + store an immutable dataset, derive timeframes, cache features")
    _layout_args(p)
    p.add_argument("--instrument", required=True, help="key in configs/instruments.yaml, e.g. NAS100_CFD")
    p.add_argument("--provider", required=True)
    p.add_argument("--asset-type", required=True, help="CFD | FUTURE | ...")
    p.add_argument("--symbol", help="provider's own symbol")
    p.add_argument("--dataset-name", help="default <INSTRUMENT>_<PROVIDER>")
    p.add_argument("--calendar", help="default: the instrument's calendar")
    p.add_argument("--price-basis", default="unknown", choices=("bid", "ask", "mid", "last", "unknown"))
    p.add_argument("--notes")
    p.add_argument("--derive", help="comma-separated higher timeframes to derive, e.g. 5m,15m,60m")
    p.add_argument("--no-features", action="store_true")

    sub.add_parser("datasets", help="list stored datasets")
    p = sub.add_parser("dataset", help="manifest + validation report of one dataset")
    p.add_argument("dataset_id")

    p = sub.add_parser("features", help="feature catalog / docs / build / cache status")
    p.add_argument("action", choices=("list", "docs", "build", "cache"))
    p.add_argument("dataset_id", nargs="?")

    p = sub.add_parser("compare-feeds", help="bar-level agreement of two datasets (no merging)")
    p.add_argument("a")
    p.add_argument("b")
    sub.add_parser("sessions", help="configured session windows")

    a = ap.parse_args(argv)
    from edgelab.data.importer import ImportFailed
    from edgelab.services import Services
    svc = Services(root=a.root)
    try:
        if a.cmd == "inspect":
            from edgelab.data.importer import ImportOptions, inspect_file
            out = inspect_file(ImportOptions(**_options(a, False)), svc.cfg, a.view_timezone)
            prof = out.pop("session_profile", None)
            _print(out, a.json)
            if prof and not a.json:
                import pandas as pd
                print(f"\nbars per local hour ({out['session_profile_tz']}), use to choose/verify the calendar:")
                print(pd.DataFrame(prof).T.to_string())
        elif a.cmd == "import":
            r = svc.import_file(_options(a, True))
            _print(r, a.json)
        elif a.cmd == "datasets":
            _print(svc.list_datasets(), a.json)
        elif a.cmd == "dataset":
            _print(svc.dataset_detail(a.dataset_id), a.json)
        elif a.cmd == "features":
            if a.action == "list":
                rows = [{"id": d["id"], "v": d["version"], "category": d["category"],
                         "requires": ",".join(d["requires"]) or "-", "known_at": d["known_at"],
                         "summary": d["summary"]} for d in svc.feature_catalog()]
                _print(rows if not a.json else svc.feature_catalog(), a.json)
            elif a.action == "docs":
                from edgelab.features.docs import render_markdown
                sys.stdout.write(render_markdown())
            elif not a.dataset_id:
                ap.error("features build/cache need a DATASET_ID")
            elif a.action == "build":
                _print(svc.build_features(a.dataset_id), a.json)
            else:
                _print(svc.feature_cache_status(a.dataset_id), a.json)
        elif a.cmd == "compare-feeds":
            _print(svc.compare_feeds(a.a, a.b), a.json)
        elif a.cmd == "sessions":
            _print([v.definition() for v in svc.sessions.values()], a.json)
    except ImportFailed as exc:
        print(f"IMPORT FAILED {exc}", file=sys.stderr)
        if exc.report is not None:
            print(exc.report.to_text(), file=sys.stderr)
        return 2
    except KeyError as exc:
        print(f"not found: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
