"""READ-ONLY BID/ASK alignment check for two Dukascopy-layout 1m CSVs (after Phase 9).

Both files are parsed with the production importer (`read_raw` + `normalize`, profile
`dukascopy_utc_csv`), exactly as an import would read them. Nothing is repaired: no forward-fill,
interpolation, resampling or row dropping. Reports source SHA-256s, row counts, ranges, exact
timestamp overlap, one-sided timestamps, per-side anomalies and the close-to-close spread
(ask_close - bid_close, in price points) over the exact overlap.

    python scripts/dukascopy_bid_ask_check.py BID.csv ASK.csv --root . [--out report.json]
        [--write-combined NEW.csv --out report.json [--intersection]]

`--write-combined` writes a NEW file (never an existing path, never either input) only when there
are no anomalies and no negative spread, and either
  * alignment is EXACT (identical timestamps on both sides), or
  * `--intersection` is given and every one-sided timestamp lies OUTSIDE the exact overlap's range
    (boundary-only: the two downloads cover shifted periods). Output = the exact overlap only; an
    interior one-sided timestamp (a gap on one side inside the overlap) always refuses.
Rows are the BID file's own rows (text values verbatim, timestamps untouched) plus
`ask_open/ask_high/ask_low/ask_close` from the ASK row with the identical timestamp text. Provenance
(both source SHA-256s, row counts, one-sided counts and ranges, overlap range, output rows and
SHA-256) goes into the `--out` report (required) and into an `import_notes` string for the import's
`--notes` (stored in the dataset manifest as provider_notes). That file is the input for a later, separate import with
`--bid-close-column close --ask-close-column ask_close` (a NEW dataset; the frozen BID-only
datasets are untouched). Nothing is imported here.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path[:0] = [str(Path.cwd()), str(Path(__file__).resolve().parents[1])]

from edgelab.core.config import load_config                                   # noqa: E402
from edgelab.data.importer import (_EXPLICIT_OFFSET, ImportOptions, apply_profile, file_sha256,  # noqa: E402
                                   normalize, read_raw)

REPORT_VERSION = 1
LIST_LIMIT = 50


def _side(path: str, cfg, profile: str) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    o = apply_profile(ImportOptions(file=path, instrument="NQ_DUKASCOPY", provider="DUKASCOPY",
                                    asset_type="CFD", timeframe="1m", profile=profile), cfg)
    raw = read_raw(o)
    tcol = o.columns.get("ts", "timestamp")
    stamps = raw[tcol].astype(str).str.strip()
    explicit = stamps.str.contains(_EXPLICIT_OFFSET, na=False)
    utc_offset = stamps.str.contains(r"(?:Z|[+-]00:?00)$", regex=True, na=False)
    df, _ = normalize(raw, o)                        # raises on mixed naive/explicit offsets
    ts = pd.DatetimeIndex(df["ts"])
    ns = ts.asi8
    d = np.diff(ns)
    o_, h, lo, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    bad_ohlc = (h < np.maximum(o_, c)) | (lo > np.minimum(o_, c)) | (h < lo)
    nan_px = ~np.isfinite(np.c_[o_, h, lo, c]).all(axis=1)
    facts = {
        "file": path, "sha256": file_sha256(path), "rows": int(len(df)), "columns": list(raw.columns),
        "first": str(ts.min()) if len(ts) else None, "last": str(ts.max()) if len(ts) else None,
        "all_explicit_offsets": bool(explicit.all()), "all_utc_offsets": bool(utc_offset.all()),
        "non_utc_offset_rows": int((~utc_offset).sum()),
        "monotonic": bool((d > 0).all()) if len(d) else True,
        "backwards_steps": int((d < 0).sum()), "duplicate_timestamps": int(ts.duplicated().sum()),
        "off_minute_timestamps": int((ns % 60_000_000_000 != 0).sum()),
        "most_common_step_minutes": float(pd.Series(d[d > 0] / 6e10).mode().iloc[0]) if (d > 0).any() else None,
        "missing_price_rows": int(nan_px.sum()), "ohlc_integrity_violations": int((bad_ohlc & ~nan_px).sum()),
        "nonpositive_price_rows": int(((np.c_[o_, h, lo, c] <= 0).any(axis=1) & ~nan_px).sum()),
        "missing_volume_rows": int((~np.isfinite(df["volume"].to_numpy(float))).sum()),
    }
    return raw, df, facts


def _pct(x: np.ndarray) -> dict:
    if not len(x):
        return {}
    return {"min": float(np.min(x)), "p05": float(np.percentile(x, 5)), "median": float(np.median(x)),
            "mean": float(np.mean(x)), "p95": float(np.percentile(x, 95)), "p99": float(np.percentile(x, 99)),
            "max": float(np.max(x))}


def check(bid_path: str, ask_path: str, root: str = ".", profile: str = "dukascopy_utc_csv") -> tuple[dict, tuple]:
    cfg = load_config(Path(root) / "configs")
    if Path(bid_path).resolve() == Path(ask_path).resolve():
        raise SystemExit("BID and ASK must be different files")
    braw, bid, bf = _side(bid_path, cfg, profile)
    araw, ask, af = _side(ask_path, cfg, profile)
    b_ns, a_ns = bid["ts"].to_numpy("datetime64[ns]").astype(np.int64), ask["ts"].to_numpy("datetime64[ns]").astype(np.int64)
    both = np.intersect1d(b_ns, a_ns)
    bid_only = np.setdiff1d(b_ns, a_ns)
    ask_only = np.setdiff1d(a_ns, b_ns)
    fmt = lambda arr: [str(pd.Timestamp(int(x), tz="UTC")) for x in arr[:LIST_LIMIT]]
    bi = pd.Series(np.arange(len(b_ns)), index=b_ns)
    ai = pd.Series(np.arange(len(a_ns)), index=a_ns)
    rb, ra = bi[~bi.index.duplicated()].reindex(both).to_numpy(), ai[~ai.index.duplicated()].reindex(both).to_numpy()
    spreads = {}
    for k in ("open", "close"):
        sp = ask[k].to_numpy(float)[ra] - bid[k].to_numpy(float)[rb]
        fin = sp[np.isfinite(sp)]
        spreads[k] = {"stats_points": _pct(fin), "negative": int((fin < 0).sum()), "zero": int((fin == 0).sum()),
                      "not_computable": int((~np.isfinite(sp)).sum())}
    neg_close = ask["close"].to_numpy(float)[ra] - bid["close"].to_numpy(float)[rb] < 0
    anomalies = [f"{side}: {k}={v}" for side, f in (("BID", bf), ("ASK", af)) for k, v in f.items()
                 if (k in ("backwards_steps", "duplicate_timestamps", "off_minute_timestamps", "missing_price_rows",
                           "ohlc_integrity_violations", "nonpositive_price_rows", "non_utc_offset_rows") and v)
                 or (k == "all_explicit_offsets" and v is False)]
    if spreads["close"]["negative"]:
        anomalies.append(f"negative close spread on {spreads['close']['negative']} bars (ask < bid)")
    exact = not len(bid_only) and not len(ask_only) and not anomalies
    lo, hi = (both[0], both[-1]) if len(both) else (0, -1)
    interior = int(((bid_only >= lo) & (bid_only <= hi)).sum() + ((ask_only >= lo) & (ask_only <= hi)).sum())
    rng = lambda arr: ([str(pd.Timestamp(int(arr[0]), tz="UTC")), str(pd.Timestamp(int(arr[-1]), tz="UTC"))]
                       if len(arr) else None)
    rep = {"report_version": REPORT_VERSION, "read_only": True, "profile": profile,
           "bid": bf, "ask": af,
           "alignment": {"overlap": int(len(both)), "bid_only": int(len(bid_only)), "ask_only": int(len(ask_only)),
                         "bid_only_first": fmt(bid_only), "ask_only_first": fmt(ask_only),
                         "overlap_first": str(pd.Timestamp(int(both[0]), tz="UTC")) if len(both) else None,
                         "overlap_last": str(pd.Timestamp(int(both[-1]), tz="UTC")) if len(both) else None,
                         "negative_close_spread_first": fmt(both[neg_close]),
                         "bid_only_range": rng(bid_only), "ask_only_range": rng(ask_only),
                         "one_sided_inside_overlap": interior},
           "spread_points": spreads,
           "anomalies": anomalies,
           "exactly_aligned": bool(exact),
           "intersection_aligned": bool(len(both) and not anomalies and interior == 0),
           "note": "spread = ask - bid over the exact timestamp overlap only; nothing filled, interpolated or "
                   "resampled; one-sided timestamps are reported, never paired"}
    return rep, (braw, araw, bf, af, both, rb, ra)


def write_combined(rep: dict, parts: tuple, out: str, intersection: bool = False) -> dict:
    braw, araw, bf, af, both, rb, ra = parts
    outp = Path(out).resolve()
    ok = rep["exactly_aligned"] or (intersection and rep["intersection_aligned"])
    if not ok:
        why = ("one-sided timestamps inside the overlap or anomalies (see alignment/anomalies)" if intersection
               else "BID and ASK are not exactly aligned (use --intersection only for boundary-only differences)")
        raise SystemExit(f"refused: {why}; nothing written")
    if outp.exists():
        raise SystemExit(f"refused: {outp} already exists; nothing is ever overwritten")
    if outp in (Path(bf["file"]).resolve(), Path(af["file"]).resolve()):
        raise SystemExit("refused: output must be a new file, not an input")
    tcol = "timestamp"
    b_sel, a_sel = braw.iloc[rb], araw.iloc[ra]                # the exact overlap, source order (sorted, monotonic)
    if list(b_sel[tcol]) != list(a_sel[tcol]):              # identical timestamp TEXT row by row
        raise SystemExit("refused: timestamp text differs row-by-row between BID and ASK; nothing written")
    comb = b_sel.reset_index(drop=True).copy()
    for k in ("open", "high", "low", "close"):
        comb[f"ask_{k}"] = a_sel[k].to_numpy()
    outp.parent.mkdir(parents=True, exist_ok=True)
    comb.to_csv(outp, index=False)
    al = rep["alignment"]
    prov = {"file": str(outp), "sha256": file_sha256(str(outp)), "mode": "exact" if rep["exactly_aligned"] else "intersection",
            "columns": list(comb.columns), "output_rows": int(len(comb)),
            "bid_sha256": bf["sha256"], "ask_sha256": af["sha256"], "bid_rows": bf["rows"], "ask_rows": af["rows"],
            "bid_only": al["bid_only"], "ask_only": al["ask_only"],
            "bid_only_range": al["bid_only_range"], "ask_only_range": al["ask_only_range"],
            "overlap_first": al["overlap_first"], "overlap_last": al["overlap_last"],
            "transform": "BID rows restricted to the exact timestamp overlap, values verbatim; ask OHLC appended from the "
                         "ASK row with the identical timestamp; no fill, interpolation, resampling or timestamp change"}
    if prov["output_rows"] != al["overlap"]:
        raise SystemExit("internal: output rows != overlap")
    return prov


def _print(r: dict) -> None:
    print("==== DUKASCOPY BID/ASK ALIGNMENT (read-only) ====")
    for side in ("bid", "ask"):
        f = r[side]
        print(f"{side.upper()}: {f['file']}\n  sha256 {f['sha256']}  rows {f['rows']:,}  {f['first']} .. {f['last']}")
        print(f"  UTC offsets on every row: {f['all_utc_offsets']}  monotonic {f['monotonic']}  duplicates "
              f"{f['duplicate_timestamps']}  off-minute {f['off_minute_timestamps']}  step {f['most_common_step_minutes']}m")
        print(f"  missing prices {f['missing_price_rows']}  OHLC violations {f['ohlc_integrity_violations']}  "
              f"non-positive {f['nonpositive_price_rows']}  missing volume {f['missing_volume_rows']}")
    a = r["alignment"]
    print(f"OVERLAP {a['overlap']:,} ({a['overlap_first']} .. {a['overlap_last']})  BID-only {a['bid_only']:,}  "
          f"ASK-only {a['ask_only']:,}")
    if a["bid_only"]:
        print(f"  BID-only first: {a['bid_only_first'][:10]}")
    if a["ask_only"]:
        print(f"  ASK-only first: {a['ask_only_first'][:10]}")
    for k, s in r["spread_points"].items():
        print(f"SPREAD ({k}, points): {s['stats_points']}  negative {s['negative']}  zero {s['zero']}  "
              f"not computable {s['not_computable']}")
    print(f"ANOMALIES: {r['anomalies'] or 'none'}")
    print(f"EXACTLY ALIGNED: {r['exactly_aligned']}   BOUNDARY-ONLY (intersection usable): {r['intersection_aligned']}  "
          f"(one-sided inside overlap: {a['one_sided_inside_overlap']})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bid")
    ap.add_argument("ask")
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", help="write the JSON report here (a new or report file; never an input)")
    ap.add_argument("--write-combined", help="NEW csv path (requires --out); exact alignment, or --intersection")
    ap.add_argument("--intersection", action="store_true",
                    help="with --write-combined: write the exact overlap when one-sided rows are boundary-only")
    a = ap.parse_args(argv)
    ins = {Path(a.bid).resolve(), Path(a.ask).resolve()}
    if a.out and Path(a.out).resolve() in ins:
        raise SystemExit("refused: --out must not be an input file")
    if a.write_combined and not a.out:
        raise SystemExit("refused: --write-combined needs --out (the provenance report)")
    if a.write_combined and Path(a.out).resolve() == Path(a.write_combined).resolve():
        raise SystemExit("refused: --out and --write-combined must be different files")
    shas = (file_sha256(a.bid), file_sha256(a.ask))
    rep, parts = check(a.bid, a.ask, a.root)
    if a.write_combined:
        rep["combined"] = write_combined(rep, parts, a.write_combined, a.intersection)
    rep["sources_unchanged"] = (file_sha256(a.bid), file_sha256(a.ask)) == shas
    if a.out:
        Path(a.out).write_text(json.dumps(rep, indent=1, default=str))
    _print(rep)
    if "combined" in rep:
        c = rep["combined"]
        rep_sha = file_sha256(a.out)
        c["import_notes"] = (f"BID/ASK combined ({c['mode']}): bid sha256 {c['bid_sha256']} ({c['bid_rows']} rows, "
                             f"{c['bid_only']} bid-only {c['bid_only_range']}); ask sha256 {c['ask_sha256']} "
                             f"({c['ask_rows']} rows, {c['ask_only']} ask-only {c['ask_only_range']}); overlap "
                             f"{c['overlap_first']}..{c['overlap_last']}; {c['output_rows']} rows; report sha256 {rep_sha}")
        print(f"COMBINED (new file, not imported): {c['file']}  sha256 {c['sha256']}  rows {c['output_rows']:,}  "
              f"mode {c['mode']}")
        print(f"IMPORT NOTES (pass as --notes): {c['import_notes']}")
    print(f"SOURCES UNCHANGED: {rep['sources_unchanged']}")
    return 0 if rep["exactly_aligned"] or ("combined" in rep) else 2


if __name__ == "__main__":
    raise SystemExit(main())
