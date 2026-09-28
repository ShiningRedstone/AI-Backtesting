"""Phase 2 benchmark: feature generation and cached retrieval.

    python scripts/phase2_benchmark.py [--days 365] [--timeframes 1,5]

Data is SYNTHETIC (random walk on the CME calendar): timings only, no market meaning.
Reports, per dataset: bars, feature specs/arrays, first-generation time per feature,
memory-cache and disk-cache retrieval (checksum verification on and off), array memory,
process peak RSS, and the cost for one strategy variant to fetch its features.
"""
from __future__ import annotations

import argparse
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edgelab.core.config import load_config  # noqa: E402
from edgelab.data.calendar import load_calendars  # noqa: E402
from edgelab.data.synthetic import generate_bars  # noqa: E402
from edgelab.data.validation import validate_and_freeze  # noqa: E402
from edgelab.features.cache import FeatureCache  # noqa: E402
from edgelab.features.engine import FeatureEngine  # noqa: E402
from edgelab.features.sessions import load_sessions  # noqa: E402
from edgelab.features.spec import FeatureSpec  # noqa: E402
from edgelab.instruments import load_instruments  # noqa: E402


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def bench(tf: int, days: int, cfg, sessions, specs, root: Path) -> None:
    cal = load_calendars(cfg)["CME_EQUITY"]
    inst = load_instruments(cfg)["NQ"]
    end = str(np.datetime64("2023-01-03") + np.timedelta64(days, "D"))
    t = time.perf_counter()
    df, _ = generate_bars(cal, "2023-01-03", end, tf_minutes=tf, seed=1)
    ds = validate_and_freeze(df, inst, cal, f"{tf}m", tf, "synthetic", f"BENCH_{tf}M", volume_type="synthetic")
    prep = time.perf_counter() - t
    n = len(ds.bars)
    print(f"\n=== {tf}m synthetic, {days} calendar days: {n:,} bars "
          f"({ds.manifest.start[:10]} .. {ds.manifest.end[:10]}); data gen+validation {prep:.1f}s ===")

    cache = FeatureCache(root / f"tf{tf}", verify=True, memory_entries=4096)
    eng = FeatureEngine.for_dataset(ds, sessions, cache)
    print(f"{'feature spec':<48}{'first gen':>10}{'arrays':>8}")
    total_first, n_arrays, nbytes = 0.0, 0, 0
    for s in specs:
        t = time.perf_counter()
        r = eng.compute(s)
        dt = time.perf_counter() - t
        total_first += dt
        n_arrays += len(r.arrays)
        nbytes += sum(a.nbytes for a in r.arrays.values())
        print(f"{s.label[:47]:<48}{dt * 1000:>8.0f}ms{len(r.arrays):>8}")
    print(f"{'TOTAL first generation (' + str(len(specs)) + ' specs)':<48}{total_first:>9.2f}s{n_arrays:>8}")

    def timed_all(e):
        t = time.perf_counter()
        for s in specs:
            e.compute(s)
        return time.perf_counter() - t

    def fresh(verify, shared=None):
        return FeatureEngine.for_dataset(ds, sessions, shared or FeatureCache(root / f"tf{tf}", verify=verify))

    cold = timed_all(fresh(True))                                  # first disk load in this process
    disk_v = min(timed_all(fresh(True)) for _ in range(3))
    disk_nv = min(timed_all(fresh(False)) for _ in range(3))
    mem = min(timed_all(fresh(True, shared=cache)) for _ in range(3))   # new engine, warm memory cache
    memo = min(timed_all(eng) for _ in range(3))                        # same engine (per-run memo)
    disk_bytes = sum(p.stat().st_size for p in (root / f"tf{tf}").rglob("*.npz"))
    print(f"retrieval of all {len(specs)} specs:")
    print(f"  disk, first load in process (cold, verified) {cold * 1000:8.0f} ms")
    print(f"  disk, checksum-verified (warm, min of 3)     {disk_v * 1000:8.0f} ms   {total_first / disk_v:6.1f}x faster than generating")
    print(f"  disk, no verification  (warm, min of 3)      {disk_nv * 1000:8.0f} ms   {total_first / disk_nv:6.1f}x")
    print(f"  memory cache, new engine                     {mem * 1000:8.1f} ms   {total_first / mem:6.0f}x")
    print(f"  same engine (memoized)                       {memo * 1000:8.2f} ms")
    print(f"feature arrays: {nbytes / 1e6:,.0f} MB in memory; {disk_bytes / 1e6:,.0f} MB on disk; "
          f"process peak RSS so far {rss_mb():,.0f} MB")

    # a strategy variant typically needs ~3-5 specs; parameter variants reuse the same specs
    variant = [FeatureSpec.make("ema", {"period": 20}, "60m"), FeatureSpec.make("atr", {"period": 14}),
               FeatureSpec.make("session", {"session": "NY_RTH"})]
    for s in variant:
        eng.compute(s)
    reps = 2000
    t = time.perf_counter()
    for _ in range(reps):
        eng.frame(variant)
    per = (time.perf_counter() - t) / reps
    print(f"per-variant feature fetch (3 specs, warm): {per * 1e6:.0f} us -> 10,000 variants: {per * 1e4:.1f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--timeframes", default="1,5")
    a = ap.parse_args()
    cfg = load_config(Path(__file__).resolve().parents[1] / "configs", environ={})
    sessions = load_sessions(cfg)
    specs = [FeatureSpec.from_dict(d) for d in cfg["features"]["default_set"]]
    root = Path(tempfile.mkdtemp(prefix="edgelab_bench_"))
    print("EdgeLab Phase 2 feature benchmark (SYNTHETIC data; timings only)")
    print(f"python {sys.version.split()[0]}, numpy {np.__version__}; single process")
    try:
        for tf in (int(x) for x in a.timeframes.split(",")):
            bench(tf, a.days, cfg, sessions, specs, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
