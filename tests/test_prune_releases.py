"""ADR-90: CI keeps only the newest build of each existing branch, deletes builds of deleted branches, never touches
installer-main or versioned releases, and deletes nothing when the branch list looks wrong."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
from prune_releases import to_delete  # noqa: E402

TAGS = ["installer-main", "v0.3.0", "build-main-36", "build-main-38", "build-main-47", "build-V0.2-22",
        "build-backup-main-2026-10-01-7", "build-claude-zealous-heisenberg-7xwqax-23", "build-claude-zealous-heisenberg-7xwqax-37",
        "build-claude-amazing-franklin-t9xh10-25", "build-claude-sweet-lamport-kzygwf-29", "build-claude-sweet-lamport-kzygwf-33"]
BRANCHES = ["main", "V0.2", "backup/main-2026-10-01", "claude/zealous-heisenberg-7xwqax"]


class TestPrune(unittest.TestCase):
    def test_keep_newest_per_existing_branch(self):
        out = to_delete(TAGS, BRANCHES)
        self.assertEqual(out, sorted(["build-main-36", "build-main-38", "build-claude-zealous-heisenberg-7xwqax-23",
                                      "build-claude-amazing-franklin-t9xh10-25", "build-claude-sweet-lamport-kzygwf-29",
                                      "build-claude-sweet-lamport-kzygwf-33"]))
        kept = set(TAGS) - set(out)
        self.assertEqual(kept, {"installer-main", "v0.3.0", "build-main-47", "build-V0.2-22", "build-backup-main-2026-10-01-7",
                                "build-claude-zealous-heisenberg-7xwqax-37"})      # save points keep their newest build

    def test_unsure_branch_list_deletes_nothing(self):
        self.assertEqual(to_delete(TAGS, []), [])
        self.assertEqual(to_delete(TAGS, ["V0.2"]), [])                              # no main: the listing looks wrong
        self.assertEqual(to_delete(TAGS, ["", "  "]), [])

    def test_numbers_compare_as_numbers(self):
        self.assertEqual(to_delete(["build-main-9", "build-main-10"], ["main"]), ["build-main-9"])


if __name__ == "__main__":
    unittest.main()
