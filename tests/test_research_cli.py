"""Phase 4 step 8: the `research` CLI (thin wrappers over the research services).

The CLI builds its own Services, so the job test patches Services at class level: a gate holds
the first cell, the first status poll raises KeyboardInterrupt (a Ctrl-C) and the cancel call
opens the gate - so the running cell finishes and no further cell starts, deterministically."""
import contextlib
import io
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from edgelab.cli import main
from edgelab.services import Services
from tests.test_batch_runner import build_workspace

T = 30


def cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(args))
    return rc, out.getvalue(), err.getvalue()


class TestResearchCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        _, cls.ids = build_workspace(cls.root)
        cls.n = 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def spec_file(self, **kw) -> str:
        type(self).n += 1
        i = self.ids
        spec = {"strategies": {"ids": [i["ema"], i["ema3"], i["never"], i["ema15"]]},
                "datasets": [i["fut"], i["cfd"]], "seed": self.n, **kw}
        p = self.root / f"search_{self.n}.json"
        p.write_text(json.dumps(spec))
        return str(p)

    def r(self, *args):
        return cli("--root", str(self.root), *args)

    def test_validate(self):
        rc, out, _ = self.r("--json", "research", "validate", self.spec_file())
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["valid"])
        rc, out, _ = self.r("research", "validate", self.spec_file(mystery=1))
        self.assertEqual(rc, 2)
        self.assertIn("mystery: unknown key", out)

    def test_plan(self):
        rc, out, _ = self.r("--json", "research", "plan", self.spec_file())
        self.assertEqual(rc, 0)
        p = json.loads(out)
        self.assertEqual((p["counts"]["planned"], p["counts"]["eligible"]), (8, 3))
        rc, out, _ = self.r("research", "plan", self.spec_file())
        self.assertIn("INELIGIBLE: broker/provider cost profile is unconfigured", out)
        rc, _, err = self.r("research", "plan", self.spec_file(max_cells=1))
        self.assertEqual(rc, 2)
        self.assertIn("never truncated", err)

    def test_run_and_rank(self):
        f = self.spec_file()
        rc, out, _ = self.r("--json", "research", "run", f)
        self.assertEqual(rc, 0)
        s = json.loads(out)
        self.assertEqual((s["status"], s["n_evaluated"], s["n_trials"]), ("completed", 3, 3))
        self.assertEqual(s["execution"]["mode"], "sequential")
        rc, out, _ = self.r("--json", "research", "run", f)                 # resume
        self.assertEqual(json.loads(out)["n_skipped_resume"], 3)
        rc, out, _ = self.r("--json", "research", "rank", s["search_id"], "--metric", "net_r",
                            "--min-sample-label", "LOW SAMPLE SIZE")
        self.assertEqual(rc, 0)
        rk = json.loads(out)
        self.assertEqual((rk["metric"], rk["validated"], rk["n_trials"], len(rk["ranked"])), ("net_r", False, 3, 2))
        rc, out, _ = self.r("research", "rank", s["search_id"])
        self.assertIn("NOT VALIDATED", out)
        rc, _, err = self.r("research", "rank", s["search_id"], "--metric", "win_rate")
        self.assertEqual(rc, 2)
        self.assertIn("never a ranking metric", err)
        rc, _, err = self.r("research", "rank", "SRCH_000000000000")
        self.assertEqual(rc, 3)

    def test_job_runs_to_completion(self):
        rc, out, _ = self.r("--json", "research", "job", self.spec_file())
        self.assertEqual(rc, 0)
        st = json.loads(out)
        self.assertEqual((st["state"], st["progress"]["evaluated"]), ("completed", 3))
        self.assertEqual(st["history"], ["queued", "running", "completed"])

    def test_job_ctrl_c_cancels_cooperatively(self):
        entered, go, polls = threading.Event(), threading.Event(), []
        real_cell, real_status, real_cancel = Services._run_cell, Services.job_status, Services.cancel_job

        def gated_cell(svc, *a, **kw):
            entered.set()
            assert go.wait(T), "gate never opened"
            return real_cell(svc, *a, **kw)

        def status(svc, jid):
            polls.append(jid)
            if len(polls) == 1:                                         # the user presses Ctrl-C
                assert entered.wait(T)
                raise KeyboardInterrupt
            return real_status(svc, jid)

        def cancel(svc, jid):
            out = real_cancel(svc, jid)
            go.set()                                                    # now let the running cell finish
            return out
        with mock.patch.object(Services, "_run_cell", gated_cell), \
                mock.patch.object(Services, "job_status", status), mock.patch.object(Services, "cancel_job", cancel):
            rc, out, err = self.r("--json", "research", "job", self.spec_file())
        self.assertEqual(rc, 130)
        self.assertIn("cancellation requested", err)
        st = json.loads(out)
        self.assertEqual(st["history"], ["queued", "running", "cancelled"])
        self.assertEqual((st["progress"]["evaluated"], st["progress"]["cancelled"]), (1, 2))


if __name__ == "__main__":
    unittest.main()
