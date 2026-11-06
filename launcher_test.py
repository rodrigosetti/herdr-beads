"""Persistent launcher validation against actual temporary session journals."""
import copy
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


launcher = runpy.run_path(str(Path(__file__).parent / "bin/go-bd"))
saved_session = launcher["saved_session"]
startup_receipt = launcher["startup_receipt"]
LaunchError = launcher["LaunchError"]
confirm_started = launcher["confirm_started"]


class PersistentSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.worktree = self.root / "original-worktree"
        self.worktree.mkdir()
        self.transcript = self.root / "specific-session.jsonl"
        self.record = {
            "session_id": "actual-session-id",
            "session_file": str(self.transcript),
            "machine": "local-machine-id",
            "worktree": str(self.worktree),
        }
        self.issue = {"id": "example-123", "status": "in_progress",
                      "metadata": {"preserved": {"value": 42}, "omp_session": self.record}}
        self.write_header()

    def write_header(self, **changes):
        header = {"type": "session", "id": self.record["session_id"],
                  "cwd": str(self.worktree), **changes}
        # Omp's fixed-width title slot may precede the session header.
        self.transcript.write_text(json.dumps({"type": "session_title", "title": "A title"})
                                   + "\n" + json.dumps(header) + "\n")

    def assert_refused_without_mutation(self, pattern):
        original_issue = copy.deepcopy(self.issue)
        original_journal = self.transcript.read_bytes() if self.transcript.exists() else None
        with self.assertRaisesRegex(LaunchError, pattern):
            saved_session(self.issue, "local-machine-id")
        self.assertEqual(self.issue, original_issue)
        if original_journal is not None:
            self.assertEqual(self.transcript.read_bytes(), original_journal)

    def test_valid_record_uses_exact_transcript_and_original_worktree(self):
        newer = self.root / "newer-unrelated-session.jsonl"
        newer.write_text(json.dumps({"type": "session", "id": "unrelated", "cwd": str(self.worktree)}) + "\n")
        self.assertIs(saved_session(self.issue, "local-machine-id"), self.record)
        self.assertEqual(self.record["session_file"], str(self.transcript))
        self.assertEqual(self.record["worktree"], str(self.worktree))

    def test_no_key_is_the_only_fresh_session_boundary(self):
        for status in ("open", "in_progress"):
            self.assertIsNone(saved_session({"status": status, "metadata": {"preserved": 42}},
                                            "local-machine-id"))
        self.assertIsNone(saved_session({"status": "in_progress"}, "local-machine-id"))

    def test_present_malformed_record_never_becomes_fresh(self):
        for value in (None, "legacy-id", {}, {"session_id": "id"}):
            with self.subTest(value=value):
                self.issue["metadata"]["omp_session"] = value
                self.assert_refused_without_mutation("Invalid metadata")

    def test_non_progress_history_requires_explicit_policy(self):
        for status in ("open", "closed", "blocked", "deferred"):
            with self.subTest(status=status):
                self.issue["status"] = status
                self.assert_refused_without_mutation("not in_progress")

    def test_foreign_machine_preserves_record_and_journal(self):
        self.record["machine"] = "another-machine"
        self.assert_refused_without_mutation("another machine")

    def test_missing_transcript_cannot_start_fresh(self):
        self.transcript.unlink()
        self.assert_refused_without_mutation("transcript is missing")

    def test_missing_original_worktree_is_not_regenerated(self):
        self.worktree.rmdir()
        self.assert_refused_without_mutation("worktree is missing")
        self.assertFalse(self.worktree.exists())

    def test_transcript_id_must_equal_recorded_id(self):
        self.write_header(id="a-different-session")
        self.assert_refused_without_mutation("transcript ID differs")

    def test_transcript_cwd_must_equal_recorded_worktree(self):
        self.write_header(cwd=str(self.root))
        self.assert_refused_without_mutation("different working directory")

    def test_relative_paths_are_rejected(self):
        for key in ("session_file", "worktree"):
            with self.subTest(key=key):
                original = self.record[key]
                self.record[key] = "relative/path"
                self.assert_refused_without_mutation("must be absolute")
                self.record[key] = original

    def test_invalid_journal_is_not_replaced(self):
        self.transcript.write_text("not JSON\n")
        self.assert_refused_without_mutation("Cannot read saved omp transcript")

    def test_startup_waits_past_inherited_shell_report_for_persistent_omp(self):
        ack = self.root / "ack.json"
        ack.write_text(json.dumps({"session": self.record}))
        shell = {"state": "live", "reported": True, "foreground": {"name": "zsh"}}
        omp = {"state": "live", "foreground": {"name": "omp"},
               "status": {"app": "omp", "state": "idle"}}
        states = iter([shell, omp, omp])
        ticks = iter(range(20))
        clock = SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None)
        with patch.dict(confirm_started.__globals__, {
            "terminal_info": lambda _: next(states), "time": clock,
        }):
            info, receipt = confirm_started("terminal", ack)
        self.assertEqual(info["status"]["app"], "omp")
        self.assertEqual(receipt["session_id"], self.record["session_id"])

    def test_startup_rejects_omp_returning_to_shell_before_acknowledgement(self):
        omp = {"state": "live", "foreground": {"name": "omp"},
               "status": {"app": "omp", "state": "idle"}}
        shell = {"state": "live", "reported": True, "foreground": {"name": "zsh"}}
        states = iter([omp, shell])
        ticks = iter(range(20))
        clock = SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _: None)
        with patch.dict(confirm_started.__globals__, {
            "terminal_info": lambda _: next(states), "time": clock,
        }):
            with self.assertRaisesRegex(LaunchError, "did not remain running"):
                confirm_started("terminal", self.root / "missing-ack.json")

    def test_acknowledgement_requires_explicit_persistent_session(self):
        ack = self.root / "ack.json"
        self.assertIsNone(startup_receipt(ack))
        for value in ({}, [], {"session": {}}, {"error": "bd write failed"}):
            with self.subTest(value=value):
                ack.write_text(json.dumps(value))
                with self.assertRaises(LaunchError):
                    startup_receipt(ack)
        ack.write_text(json.dumps({"session": self.record, "pid": 123}))
        self.assertEqual(startup_receipt(ack), self.record)


if __name__ == "__main__":
    unittest.main()
