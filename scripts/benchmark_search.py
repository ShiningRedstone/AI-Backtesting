"""Phase 4 search throughput benchmark (informational; not a test gate, no performance target).

Run from the project root:   python scripts/benchmark_search.py [--workers 4] [--quick]

Builds two identical throw-away workspaces on SYNTHETIC NQ-like bars (a driftless random walk:
no strategy has an edge on it), stores an EMA-crossover base strategy plus a Mode A variation
grid, then runs the same search sequentially (workers=1) in one workspace and with worker
processes (workers=N) in the other. Each run starts with a cold feature cache.

Reports, per run: worker count, cells evaluated, elapsed seconds and cells/second, and whether
the two runs produced the same cells (cell ids, statuses and trades hashes). Speed-ups depend
on the machine and the search size; nothing here is a claim about any particular speed-up.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from edgelab.services import Services  # noqa: E402
from tests.phase2_helpers import synthetic_canonical, write_generic_utc  # noqa: E402  (synthetic fixture)


def build(root: Path, quick: bool) -> tuple[Services, dict]:
    shutil.copytree(ROOT / "configs", root / "configs")
    svc = Services(root=root)
    periods = [("2024-03-04", "2024-03-15", 9)] if quick else [("2024-03-04", "2024-04-26", 9),
                                                               ("2024-03-04", "2024-04-26", 21)]
    datasets = []
    for k, (start, end, seed) in enumerate(periods):
        f = root / f"syn{k}.csv"
        write_generic_utc(synthetic_canonical(start, end, tf=5, seed=seed), f)
        datasets.append(svc.import_file(dict(file=str(f), instrument="NQ", provider=f"SYNTH{k}",
                                             asset_type="FUTURE", timeframe="5m", source_timezone="UTC"))["dataset_id"])
    base = yaml.safe_load((ROOT / "strategies" / "fixtures" / "ema_crossover.yaml").read_text())
    dims = [{"parameter": "rr", "values": [1.5, 3.0]}] if quick else \
        [{"parameter": "rr", "values": [1.0, 1.5, 2.5, 3.0]}, {"parameter": "fast", "values": [5, 7, 11, 13]}]
    svc.save_strategy(copy.deepcopy(base))
    vb = svc.generate_variations(base, {"mode": "grid", "dimensions": dims})
    spec = {"strategies": {"variation_batches": [vb["batch_id"]]}, "datasets": datasets, "max_cells": 10_000}
    return svc, spec


def timed(svc: Services, spec: dict, workers: int) -> tuple[dict, float]:
    t0 = time.perf_counter()
    out = svc.run_search(spec, workers=workers)
    return out, time.perf_counter() - t0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 2))
    ap.add_argument("--quick", action="store_true", help="tiny search (smoke test)")
    ap.add_argument("--json", action="store_true", help="print one JSON object instead of a table")
    a = ap.parse_args(argv)
    if a.workers < 2:
        ap.error("--workers must be >= 2 (the sequential run is always included)")
    tmp = Path(tempfile.mkdtemp(prefix="edgelab_bench_search_"))
    try:
        rows, results = [], []
        for n in (1, a.workers):
            svc, spec = build(tmp / f"w{n}", a.quick)
            out, secs = timed(svc, spec, n)
            results.append(out)
            rows.append({"workers": n, "mode": out["execution"]["mode"], "cells_evaluated": out["n_evaluated"],
                         "failed": out["n_failed"], "elapsed_s": round(secs, 3),
                         "cells_per_s": round(out["n_evaluated"] / secs, 3) if secs > 0 else None})
        key = lambda s: [(c["cell_id"], c["status"], c["trades_hash"]) for c in s["cells"]]  # noqa: E731
        match = key(results[0]) == key(results[1]) and results[0]["search_id"] == results[1]["search_id"]
        report = {"search_id": results[0]["search_id"], "cpu_count": os.cpu_count(), "runs": rows,
                  "sequential_parallel_match": match, "data": "SYNTHETIC random walk (engine benchmark only)"}
        if a.json:
            print(json.dumps(report))
        else:
            print(f"search {report['search_id']}  (synthetic data, cpu_count={report['cpu_count']})")
            print(f"{'workers':>8} {'mode':>11} {'cells':>6} {'failed':>7} {'elapsed_s':>10} {'cells/s':>9}")
            for r in rows:
                print(f"{r['workers']:>8} {r['mode']:>11} {r['cells_evaluated']:>6} {r['failed']:>7} "
                      f"{r['elapsed_s']:>10.3f} {r['cells_per_s']:>9.3f}")
            print(f"sequential and parallel results match (cell ids + trades hashes): {match}")
        return 0 if match else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
