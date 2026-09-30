"""packaging/smoke_packaged.py must never leave the launcher (or its children) running, however it ends."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# a stand-in launcher: announces itself like edgelab.desktop (runtime.json), spawns a child, then runs until killed
FAKE = r'''
import json, os, subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[sys.argv.index("--data-root") + 1]) / "demo"
(root / "logs").mkdir(parents=True)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
(root / "logs" / "pids.json").write_text(json.dumps({"parent": os.getpid(), "child": child.pid}))
(root / "logs" / "runtime.json").write_text(json.dumps(
    {"pid": os.getpid(), "url": "http://127.0.0.1:9", "port": 9, "data_root": str(root), "runtime": {}}))
time.sleep(300)
'''


def alive(pid: int) -> bool:
    from edgelab.updater.apply import pid_alive
    if not pid_alive(pid):
        return False
    try:                                              # Linux: a killed orphan that nobody reaped (zombie) is not running
        return "Z" not in (Path(f"/proc/{pid}/status").read_text().split("State:")[1].split()[0])
    except (OSError, IndexError):
        return True


class TestSmokeLifecycle(unittest.TestCase):
    def test_an_exception_mid_run_leaves_no_process_and_no_open_files(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "fake_launcher.py"
            fake.write_text(FAKE)
            base = Path(d) / "root"
            r = subprocess.run([sys.executable, str(REPO / "packaging" / "smoke_packaged.py"),
                                "--cmd", f'"{sys.executable}" "{fake}"', "--data-root", str(base)],
                               capture_output=True, text=True, timeout=180)
            # the health request to the dead port raises inside the smoke run: a recorded FAIL, not a hang or a leak
            self.assertEqual(r.returncode, 1, r.stdout[-1500:] + r.stderr[-1500:])
            self.assertIn("PACKAGED SMOKE TEST: FAIL", r.stdout)
            pids = json.loads((base / "demo" / "logs" / "pids.json").read_text())
            self.assertFalse(alive(pids["parent"]), "launcher process still running")
            self.assertFalse(alive(pids["child"]), "launcher's child process still running")
        # leaving the with-block deleted the scratch tree: on Windows that fails while anything still holds files in it


if __name__ == "__main__":
    unittest.main()
