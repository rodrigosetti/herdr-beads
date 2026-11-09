"""Epic policy regressions against disposable, embedded Beads databases.

Run: python3 -m unittest epic_test
No inherited Beads, Dolt, git routing or user configuration reaches these DBs.
"""

import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest


reconcile_epics = runpy.run_path(
    str(Path(__file__).parent / "bin/beads_epics.py")
)["reconcile_epics"]


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
        result = reconcile_epics(self.project)
        self.assertFalse(result["errors"], result)
        return result

    def row(self, result, issue_id):
        return next(row for row in result["epics"] if row["id"] == issue_id)

    def test_closes_without_sessions_and_preserves_metadata_and_unrelated_tasks(self):
        metadata = {"custom": {"answer": 42}, "omp_session": {"legacy": "untouched"}}
        epic = self.create("collection", "epic", metadata=json.dumps(metadata),
                           labels="retain", assignee="epic-reconciler", priority="1")
        child = self.create("arbitrary-child", parent=epic)
        unrelated = self.create("unrelated", metadata='{"untouched":true}')
        self.close(child)
        before_task = self.project.show(unrelated)
        before_child = self.project.show(child)
        result = self.reconcile()
        self.assertEqual(result["closed"], [epic])
        row = self.row(result, epic)
        self.assertEqual((row["status"], row["closed_children"], row["total_children"]),
                         ("closed", 1, 1))
        stored = self.project.show(epic)
        self.assertEqual(stored["metadata"], metadata)
        self.assertEqual(stored["labels"], ["retain"])
        self.assertEqual(stored["assignee"], "epic-reconciler")
        self.assertEqual(stored["priority"], 1)
        after_child = self.project.show(child)
        for field in ("status", "updated_at", "revision", "metadata"):
            self.assertEqual(after_child.get(field), before_child.get(field))
        self.assertEqual(self.project.show(unrelated), before_task)
        second = self.reconcile()
        self.assertEqual(second["closed"], [])
        self.assertEqual(second["reopened"], [])

    def test_empty_epics_stay_open_and_previously_closed_empty_reopens(self):
        open_epic = self.create("empty-open", "epic")
        closed_epic = self.create("empty-closed", "epic")
        self.close(closed_epic)
        result = self.reconcile()
        self.assertEqual(result["closed"], [])
        self.assertEqual(result["reopened"], [closed_epic])
        for issue_id in (open_epic, closed_epic):
            row = self.row(result, issue_id)
            self.assertEqual((row["total_children"], row["closed_children"], row["status"]),
                             (0, 0, "open"))

    def test_reopened_child_reopens_epic_and_next_completion_closes_it(self):
        epic = self.create("parent", "epic")
        child = self.create("work", parent=epic)
        self.close(child)
        self.reconcile()
        self.project.bd("update", child, "--status", "open")
        result = self.reconcile()
        self.assertEqual(result["reopened"], [epic])
        self.assertEqual(self.row(result, epic)["closed_children"], 0)
        self.close(child)
        self.assertEqual(self.reconcile()["closed"], [epic])

    def test_arbitrary_id_addition_and_reparent_use_actual_edges_not_prefixes(self):
        epic = self.create("root", "epic")
        child = self.create("complete", parent=epic)
        self.close(child)
        self.reconcile()
        added = self.create("totally-unrelated-id", parent=epic)
        result = self.reconcile()
        self.assertEqual(result["reopened"], [epic])
        self.assertEqual(self.row(result, epic)["total_children"], 2)
        # A dotted-looking ID is NOT a child without its parent edge.
        pretender = self.create("root.fake")
        # Keep this fixture explicitly parentless despite its dotted ID.
        self.project.bd("update", pretender, "--parent", "")
        self.project.bd("update", added, "--parent", "")
        result = self.reconcile()
        self.assertEqual(self.row(result, epic)["status"], "closed")
        self.assertEqual(self.project.show(pretender)["status"], "open")
        self.project.bd("update", child, "--parent", "")
        result = self.reconcile()
        self.assertEqual(self.row(result, epic)["status"], "open")
        self.assertEqual(self.row(result, epic)["total_children"], 0)

    def test_nested_epics_close_and_reopen_bottom_up_with_direct_progress(self):
        root = self.create("a-root", "epic")
        middle = self.create("m-middle", "epic", parent=root)
        inner = self.create("z-inner", "epic", parent=middle)
        leaf = self.create("leaf", parent=inner)
        self.close(leaf)
        result = self.reconcile()
        self.assertEqual(result["closed"], [inner, middle, root])
        for issue_id in (root, middle, inner):
            row = self.row(result, issue_id)
            self.assertEqual((row["closed_children"], row["total_children"]), (1, 1))
        self.project.bd("update", leaf, "--status", "in_progress")
        result = self.reconcile()
        self.assertEqual(result["reopened"], [inner, middle, root])
        self.assertTrue(all(row["status"] == "open" for row in result["epics"]))

    def test_unfinished_child_statuses_never_count_as_closed(self):
        epic = self.create("active", "epic")
        children = [self.create("status-" + status, parent=epic)
                    for status in ("open", "in_progress", "blocked", "deferred")]
        for child, status in zip(children, ("open", "in_progress", "blocked", "deferred")):
            self.project.bd("update", child, "--status", status)
        result = self.reconcile()
        row = self.row(result, epic)
        self.assertEqual((row["status"], row["closed_children"], row["total_children"]),
                         ("open", 0, 4))

    def test_live_blocker_holds_only_its_epic_and_keeps_progress_visible(self):
        held = self.create("held", "epic")
        held_child = self.create("held-work", parent=held)
        self.close(held_child)
        blocker = self.create("external-blocker")
        self.project.bd("dep", "add", held, blocker)
        free = self.create("free", "epic")
        free_child = self.create("free-work", parent=free)
        self.close(free_child)
        before = self.project.show(blocker)
        result = reconcile_epics(self.project)
        self.assertIn(held, [error["id"] for error in result["errors"]])
        row = self.row(result, held)
        self.assertTrue(row["held"])
        self.assertEqual((row["status"], row["total_children"], row["closed_children"]),
                         ("open", 1, 1))
        self.assertEqual(self.row(result, free)["status"], "closed")
        self.assertEqual(self.project.show(blocker), before)
        self.close(blocker)
        self.assertEqual(self.row(self.reconcile(), held)["status"], "closed")

    def test_launcher_cli_rejects_epics_without_receipts_or_workspaces(self):
        epic = self.create("sessionless", "epic", metadata='{"preserved":{"answer":42}}')
        before = self.project.show(epic)
        traps = self.project.root / "command-traps"
        traps.mkdir()
        tripwire = self.project.root / "unexpected-launch"
        # Required binaries exist for preflight but must never execute. This
        # prevents the regression itself from starting a real omp/Herdr session.
        for name in ("omp", "herdr-app", "hc"):
            executable = traps / name
            executable.write_text(
                '#!/bin/sh\nprintf "%s\\n" invoked > "$HOME/../unexpected-launch"\nexit 99\n'
            )
            executable.chmod(0o700)
        env = dict(self.project.env)
        env["PATH"] = str(traps) + os.pathsep + env["PATH"]
        launcher = str(Path(__file__).parent.resolve() / "bin/go-bd")
        for args in (("--herdr", epic, "--json"), (epic,)):
            with self.subTest(args=args):
                command = subprocess.run(
                    [sys.executable, launcher, *args], cwd=self.project.root, env=env,
                    text=True, capture_output=True, timeout=60,
                )
                self.assertNotEqual(command.returncode, 0)
                self.assertIn("sessionless", command.stderr.lower())
                self.assertEqual(self.project.show(epic), before)
                self.assertFalse(tripwire.exists())
                state = Path(env["XDG_STATE_HOME"]) / "go-bd"
                self.assertEqual(list(state.rglob("*.json")), [])
                self.assertEqual(list(state.rglob("launch-*")), [])
                self.assertFalse((Path(env["HOME"]) / ".omp").exists())
                self.assertFalse((self.project.root / ".git/worktrees").exists())

    def test_child_added_between_snapshot_and_close_is_refused_without_force(self):
        epic = self.create("racing-parent", "epic")
        child = self.create("old-work", parent=epic)
        self.close(child)
        project = self.project
        added = None

        class AddBeforeClose:
            def bd(wrapper, *args):
                nonlocal added
                if args[0] == "close" and args[1] == epic and added is None:
                    added = self.create("new-work", parent=epic)
                return project.bd(*args)

        result = reconcile_epics(AddBeforeClose())
        self.assertIsNotNone(added)
        self.assertIn(epic, [error["id"] for error in result["errors"]])
        self.assertEqual(self.row(result, epic)["status"], "open")
        self.assertEqual(self.project.show(added)["status"], "open")

    def test_removing_last_child_during_close_is_detected_and_repaired(self):
        epic = self.create("removal-parent", "epic")
        child = self.create("removed-work", parent=epic)
        self.close(child)
        project = self.project
        removed = False

        class RemoveBeforeClose:
            def bd(wrapper, *args):
                nonlocal removed
                if args[0] == "close" and args[1] == epic and not removed:
                    removed = True
                    project.bd("update", child, "--parent", "")
                return project.bd(*args)

        result = reconcile_epics(RemoveBeforeClose())
        self.assertFalse(result["errors"], result)
        self.assertEqual(result["closed"], [epic])
        self.assertEqual(result["reopened"], [epic])
        self.assertEqual(self.row(result, epic)["status"], "open")
        self.assertEqual(self.row(result, epic)["total_children"], 0)


if __name__ == "__main__":
    unittest.main()
