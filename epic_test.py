"""Read-only epic progress regressions against disposable embedded Beads databases.

Run: python3 -m unittest epic_test
No inherited Beads, Dolt, git routing or user configuration reaches these DBs.
"""

import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import unittest


observe_epics = runpy.run_path(
    str(Path(__file__).parent / "bin/beads_epics.py")
)["observe_epics"]


class IsolatedProject:
    def __init__(self, root):
        self.root = root
        home = root / "home"
        home.mkdir()
        self.env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(home), "TMPDIR": str(root), "LANG": "C", "LC_ALL": "C",
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_STATE_HOME": str(home / "state"),
            "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_DATA_HOME": str(home / "data"),
            "BEADS_DIR": str(root / ".beads"),
            "BEADS_ACTOR": "epic-reconciler", "BD_NON_INTERACTIVE": "1",
            "BD_LAST_TOUCHED_FALLBACK": "0", "NO_COLOR": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Epic regression", "GIT_COMMITTER_NAME": "Epic regression",
            "GIT_AUTHOR_EMAIL": "epic@example.invalid",
            "GIT_COMMITTER_EMAIL": "epic@example.invalid",
        }
        self.run("git", "init", "--quiet")
        self.run("bd", "--sandbox", "init", "--backend", "dolt", "--prefix", "epictest",
                 "--non-interactive", "--skip-agents", "--skip-hooks", "--quiet")

    def run(self, *args):
        command = subprocess.run(
            args, cwd=self.root, env=self.env, text=True, capture_output=True, timeout=60,
        )
        if command.returncode:
            raise RuntimeError(f"{' '.join(args)}: {command.stderr.strip() or command.stdout.strip()}")
        return command.stdout

    def bd(self, *args):
        result = json.loads(self.run("bd", "--sandbox", *args, "--json"))
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(result["error"])
        return result

    def show(self, issue_id):
        rows = self.bd("show", issue_id)
        if len(rows) != 1 or rows[0]["id"] != issue_id:
            raise AssertionError(f"Expected exact issue {issue_id}: {rows!r}")
        return rows[0]


@unittest.skipUnless(shutil.which("bd") and shutil.which("git"), "Requires bd and git")
class EpicReconciliationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="herdr-epics-")
        self.addCleanup(temporary.cleanup)
        self.project = IsolatedProject(Path(temporary.name).resolve())

    def create(self, name, issue_type="task", parent=None, **fields):
        issue_id = "epictest-" + name
        args = ["create", "--id", issue_id, "--title", name,
                "--type", issue_type, "--description", "Isolated epic regression"]
        for field, value in fields.items():
            args.extend(["--" + field.replace("_", "-"), value])
        self.project.bd(*args)
        if parent:
            self.project.bd("update", issue_id, "--parent", parent)
        return issue_id

    def close(self, issue_id):
        self.project.bd("close", issue_id, "--reason", "Regression fixture completion")

    def reconcile(self):
        result = observe_epics(self.project)
        self.assertFalse(result["errors"], result)
        return result

    def row(self, result, issue_id):
        return next(row for row in result["epics"] if row["id"] == issue_id)

    def test_completed_children_report_eligibility_without_closing_or_metadata_writes(self):
        epic = self.create("collection", "epic", metadata='{"preserved":{"answer":42}}')
        child = self.create("work", parent=epic)
        self.close(child)
        before = {issue_id: self.project.show(issue_id) for issue_id in (epic, child)}
        for _ in range(2):
            row = self.row(self.reconcile(), epic)
            self.assertTrue(row["eligible"])
            self.assertEqual((row["status"], row["closed_children"], row["total_children"]), ("open", 1, 1))
        self.assertEqual({issue_id: self.project.show(issue_id) for issue_id in before}, before)

    def test_externally_closed_epic_and_reopened_child_are_observations_only(self):
        epic = self.create("observed", "epic")
        child = self.create("child", parent=epic)
        self.close(child)
        self.close(epic)
        self.project.bd("update", child, "--status", "open")
        before = self.project.show(epic)
        row = self.row(self.reconcile(), epic)
        self.assertFalse(row["eligible"])
        self.assertEqual((row["status"], row["closed_children"]), ("closed", 0))
        self.assertEqual(self.project.show(epic), before)

    def test_empty_epics_never_become_eligible_or_change_status(self):
        opened = self.create("empty", "epic")
        closed = self.create("closed-empty", "epic")
        self.close(closed)
        result = self.reconcile()
        for issue_id in (opened, closed):
            row = self.row(result, issue_id)
            self.assertFalse(row["eligible"])
            self.assertEqual((row["total_children"], row["closed_children"]), (0, 0))
        self.assertEqual(self.project.show(opened)["status"], "open")
        self.assertEqual(self.project.show(closed)["status"], "closed")

    def test_nested_progress_counts_direct_children_without_transitive_closure(self):
        root = self.create("root", "epic")
        middle = self.create("middle", "epic", parent=root)
        leaf = self.create("leaf", parent=middle)
        self.close(leaf)
        result = self.reconcile()
        self.assertEqual((self.row(result, root)["closed_children"], self.row(result, root)["total_children"]), (0, 1))
        self.assertTrue(self.row(result, middle)["eligible"])
        self.assertEqual(self.project.show(middle)["status"], "open")

    def test_parent_edges_not_dotted_ids_determine_membership_and_reparenting(self):
        epic = self.create("parent", "epic")
        child = self.create("arbitrary", parent=epic)
        pretender = self.create("parent.fake")
        self.project.bd("update", pretender, "--parent", "")
        self.close(child)
        row = self.row(self.reconcile(), epic)
        self.assertEqual((row["closed_children"], row["total_children"]), (1, 1))
        self.project.bd("update", child, "--parent", "")
        row = self.row(self.reconcile(), epic)
        self.assertEqual((row["closed_children"], row["total_children"], row["eligible"]), (0, 0, False))

    def test_unfinished_statuses_do_not_count_as_closed(self):
        epic = self.create("unfinished", "epic")
        for status in ("open", "in_progress", "blocked", "deferred"):
            child = self.create("child-" + status, parent=epic)
            self.project.bd("update", child, "--status", status)
        row = self.row(self.reconcile(), epic)
        self.assertEqual((row["closed_children"], row["total_children"], row["eligible"]), (0, 4, False))

    def test_project_read_failure_holds_only_affected_epic(self):
        held = self.create("held", "epic")
        free = self.create("free", "epic")
        project = self.project
        class ReadFailure:
            def bd(self, *args):
                if args[:3] == ("list", "--parent", held):
                    raise RuntimeError("database temporarily unavailable")
                if args[0] != "list":
                    raise AssertionError("Observer attempted a status mutation")
                return project.bd(*args)
        result = observe_epics(ReadFailure())
        self.assertEqual(result["errors"][0]["id"], held)
        self.assertEqual(self.row(result, free)["status"], "open")


if __name__ == "__main__":
    unittest.main()
