"""Run the complete test suite and record a one-line summary for the web dashboard.

    python scripts/run_tests.py                 # everything (browser tests skip without Playwright)
    EDGELAB_SLOW_TESTS=1 python scripts/run_tests.py

Writes reports/last_test_run.txt, e.g. "2026-09-28 16:40 UTC: 340 run, 0 failed, 0 errors, 4 skipped".
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    res = unittest.TextTestRunner(verbosity=1).run(suite)
    line = (f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC: {res.testsRun} run, {len(res.failures)} failed, "
            f"{len(res.errors)} errors, {len(res.skipped)} skipped")
    out = ROOT / "reports" / "last_test_run.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(line + "\n")
    print(line)
    return 0 if res.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
