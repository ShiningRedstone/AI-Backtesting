"""Atomic writes survive Windows sharing violations (field failure: research run stopped with
"PermissionError: [WinError 5] Access is denied: '...\\runs\\CR_....json.tmp' -> '...\\runs\\CR_....json'" while the Runs
page was reading the same run record outside the service lock)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from edgelab.core import fsutil
from edgelab.research import campaign as C

REAL_REPLACE = os.replace


def flaky_replace(n_failures: int):
    """os.replace that fails like Windows does while another handle has the destination open."""
    left = [n_failures]

    def rep(src, dst):
        if left[0] > 0:
            left[0] -= 1
            raise PermissionError(13, "[WinError 5] Access is denied (simulated open handle)", str(src))
        return REAL_REPLACE(src, dst)
    return rep


class _Svc:
    def __init__(self, root: Path):
        self.root = root


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_retries_until_the_destination_is_free(self):
        p = self.dir / "x.json"
        p.write_text("old")
        with mock.patch.object(fsutil.os, "replace", side_effect=flaky_replace(5)), \
                mock.patch.object(fsutil.time, "sleep"):
            fsutil.atomic_write_text(p, "new")
        self.assertEqual(p.read_text(), "new")
        self.assertEqual(sorted(x.name for x in self.dir.iterdir()), ["x.json"])          # no temp file left behind

    def test_gives_up_after_the_attempts_and_cleans_up(self):
        p = self.dir / "x.json"
        p.write_text("old")
        with mock.patch.object(fsutil.os, "replace", side_effect=flaky_replace(10_000)), \
                mock.patch.object(fsutil.time, "sleep"):
            with self.assertRaises(PermissionError):
                fsutil.atomic_write_text(p, "new")
        self.assertEqual(p.read_text(), "old")                                             # old content intact
        self.assertEqual(sorted(x.name for x in self.dir.iterdir()), ["x.json"])

    def test_concurrent_writers_use_their_own_temp_files(self):
        p = self.dir / "x.json"
        errors = []

        def write(k):
            try:
                for i in range(50):
                    fsutil.atomic_write_text(p, json.dumps({"k": k, "i": i}))
            except BaseException as exc:                                                   # pragma: no cover
                errors.append(exc)
        ts = [threading.Thread(target=write, args=(k,)) for k in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(json.loads(p.read_text())["i"], 49)
        self.assertEqual(sorted(x.name for x in self.dir.iterdir()), ["x.json"])


class RunRecordWriteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _Svc(Path(self._tmp.name))
        self.rec = {"run_record_id": "CR_20261001_215457_F41BEB", "campaign_id": "CMP_TEST", "status": "running",
                    "created_at": "2026-10-01T21:54:57+00:00"}

    def tearDown(self):
        self._tmp.cleanup()

    def test_run_record_survives_a_briefly_open_destination(self):
        with mock.patch.object(C, "campaigns_dir", return_value=self.svc.root / "campaigns"):
            C.save_run_record(self.svc, self.rec)
            with mock.patch.object(fsutil.os, "replace", side_effect=flaky_replace(3)), \
                    mock.patch.object(fsutil.time, "sleep"):
                C.save_run_record(self.svc, {**self.rec, "status": "completed"})
                C.save_scope(self.svc, "CMP_TEST", self.rec["run_record_id"], ["STR_A"])
            recs = C.run_records(self.svc, "CMP_TEST")
            self.assertEqual([r["status"] for r in recs], ["completed"])
            self.assertEqual(C.load_scope(self.svc, "CMP_TEST", self.rec["run_record_id"])["strategy_ids"], ["STR_A"])
            names = sorted(x.name for x in C.runs_dir(self.svc, "CMP_TEST").iterdir())
            self.assertEqual(names, ["CR_20261001_215457_F41BEB.json", "CR_20261001_215457_F41BEB.scope.json"])


if __name__ == "__main__":
    unittest.main()
