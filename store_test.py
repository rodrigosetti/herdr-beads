"""Consumer-facing durable-store regressions (python3 -m unittest store_test)."""

import copy
import json
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "bin"))

from beads_contract import Refusal, canonical_json
from beads_store import Store


STAMP = "2026-11-09T00:00:00Z"


def request(scope, request_id="reservation"):
    return {"apiVersion": 1, "requestId": request_id, "method": "status",
            "scope": copy.deepcopy(scope), "params": {"runId": None}}


def run_record(scope, run_id="run-one", attempt=1):
    return {
        "schemaVersion": 1,
        "fence": {"runId": run_id, "attempt": attempt, "generation": 1},
        "revision": 1, "scope": copy.deepcopy(scope), "owner": "owner",
        "disposition": "current", "harness": "omp",
        "policy": {"closeAfterIntegration": False, "cleanupAfterRelease": False},
        "target": {"mode": "local-target", "repository": copy.deepcopy(scope["repository"]),
                   "ref": "refs/heads/main"},
        "workspace": None,
        "execution": {"state": "preparing", "access": "writable", "activity": "unknown",
                      "session": None, "terminal": None, "observedAt": None,
                      "readOnlyAcknowledged": False},
        "coverage": {"revision": 0, "workspaceHead": None, "required": [],
                     "state": "unsubmitted"},
        "submissions": [], "release": {"state": "held", "receipt": None},
        "closure": {"state": "not_requested", "evidenceIds": [], "observedAt": None,
                    "reason": None, "manualCommand": None},
        "cleanup": {"state": "held", "blockers": ["manual_cleanup_required"], "checkedAt": None},
        "issueObservation": None, "createdAt": STAMP, "updatedAt": STAMP,
    }


def _reserve_while_locked(root, scope, ready, release, channel):
    try:
        with Store(root).transaction(scope) as document:
            payload = request(scope)
            document["requests"][payload["requestId"]] = {
                "payload": canonical_json(payload), "operationId": None,
                "result": None,
            }
            document["audit"].append({"reservation": "first"})
            ready.set()
            if not release.wait(15):
                raise RuntimeError("Parent did not release scope lock")
        channel.send("committed")
    except BaseException as error:
        channel.send((type(error).__name__, str(error)))
    finally:
        channel.close()


def _contend_then_observe(root, scope, retry, channel):
    try:
        try:
            with Store(root).transaction(scope):
                channel.send("unexpectedly acquired")
        except Refusal as error:
            channel.send((error.code, error.retryable))
        if not retry.wait(15):
            raise RuntimeError("Parent did not request retry")
        with Store(root).transaction(scope) as document:
            channel.send(document["audit"][-1])
            document["audit"].append({"observed": "second"})
    except BaseException as error:
        channel.send((type(error).__name__, str(error)))
    finally:
        channel.close()


def _interrupt_before_replace(root, scope):
    with patch("beads_store.os.replace", side_effect=lambda *_args, **_kwargs: os._exit(17)):
        with Store(root).transaction(scope) as document:
            document["audit"].append({"mustNotCommit": True})


class DurableStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="beads-store-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.repo = self.base / "repository"
        self.repo.mkdir()
        self.scope = {"repository": {"root": str(self.repo), "commonDir": str(self.repo / ".git")},
                      "beadsDir": str(self.repo / ".beads"), "issueId": "example-123",
                      "machineId": "machine-one"}
        self.store = Store(self.base / "state")

    def seed(self):
        with self.store.transaction(self.scope) as document:
            document["audit"].append({"initial": True})
        return next(self.store.root.glob("*.json"))

    def install_run(self):
        with self.store.transaction(self.scope) as document:
            run = run_record(self.scope)
            document["runs"][run["fence"]["runId"]] = run
            document["current"] = run["fence"]["runId"]
        return next(self.store.root.glob("*.json"))

    def assert_refusal(self, code, action):
        with self.assertRaises(Refusal) as raised:
            action()
        self.assertEqual(raised.exception.code, code)


    def test_exception_and_unchanged_transactions_preserve_bytes_and_lock_inode(self):
        record = self.seed()
        before = record.read_bytes()
        lock = next(self.store.root.glob("*.lock"))
        inode = lock.stat().st_ino
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.store.transaction(self.scope) as document:
                document["audit"].append({"notCommitted": True})
                raise RuntimeError("abort")
        with patch("beads_store.os.replace") as replace:
            with self.store.transaction(self.scope):
                pass
        replace.assert_not_called()
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual(lock.stat().st_ino, inode)

    def test_processes_serialize_one_reservation_without_unlinking_lock(self):
        self.seed()
        lock = next(self.store.root.glob("*.lock"))
        inode = lock.stat().st_ino
        context = multiprocessing.get_context("spawn")
        ready, release, retry = context.Event(), context.Event(), context.Event()
        first_parent, first_child = context.Pipe(duplex=False)
        second_parent, second_child = context.Pipe(duplex=False)
        first = context.Process(target=_reserve_while_locked,
                                args=(str(self.store.root), self.scope, ready, release, first_child))
        second = context.Process(target=_contend_then_observe,
                                 args=(str(self.store.root), self.scope, retry, second_child))
        self.addCleanup(first_parent.close)
        self.addCleanup(second_parent.close)
        first.start()
        first_child.close()
        try:
            self.assertTrue(ready.wait(15), "First process did not acquire the lock")
            second.start()
            second_child.close()
            self.assertTrue(second_parent.poll(15))
            self.assertEqual(second_parent.recv(), ("pending_operation", True))
            release.set()
            self.assertTrue(first_parent.poll(15))
            self.assertEqual(first_parent.recv(), "committed")
            first.join(15)
            self.assertEqual(first.exitcode, 0)
            retry.set()
            self.assertTrue(second_parent.poll(15))
            self.assertEqual(second_parent.recv(), {"reservation": "first"})
            second.join(15)
            self.assertEqual(second.exitcode, 0)
        finally:
            release.set()
            retry.set()
            for process in (first, second):
                if process.pid is not None:
                    process.join(2)
                    if process.is_alive():
                        process.terminate()
                        process.join(5)
        document = self.store.read(self.scope)
        self.assertEqual(len(document["requests"]), 1)
        self.assertEqual(document["audit"], [{"initial": True}, {"reservation": "first"},
                                              {"observed": "second"}])
        self.assertEqual(lock.stat().st_ino, inode)

    def test_process_interruption_before_replace_retains_parseable_prior_state(self):
        record = self.seed()
        before = record.read_bytes()
        context = multiprocessing.get_context("spawn")
        process = context.Process(target=_interrupt_before_replace,
                                  args=(str(self.store.root), self.scope))
        process.start()
        process.join(15)
        try:
            self.assertEqual(process.exitcode, 17)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual(self.store.read(self.scope)["audit"], [{"initial": True}])
        self.assertEqual(self.store.documents(), [self.scope])
        with self.store.transaction(self.scope) as document:
            document["audit"].append({"recovered": True})
        self.assertEqual(len(self.store.read(self.scope)["audit"]), 2)

    def test_distinct_repository_database_and_issue_scopes_are_isolated(self):
        scopes = [copy.deepcopy(self.scope) for _ in range(4)]
        scopes[1]["issueId"] = "example-456"
        scopes[2]["beadsDir"] = str(self.repo / "other-db")
        other_repo = self.base / "other-repository"
        other_repo.mkdir()
        scopes[3]["repository"] = {"root": str(other_repo), "commonDir": str(other_repo / ".git")}
        for index, scope in enumerate(scopes):
            with self.store.transaction(scope) as document:
                document["audit"].append({"scope": index})
        self.assertEqual(len(list(self.store.root.glob("*.json"))), 4)
        for index, scope in enumerate(scopes):
            self.assertEqual(self.store.read(scope)["audit"], [{"scope": index}])
        self.assertEqual({canonical_json(scope) for scope in self.store.documents()},
                         {canonical_json(scope) for scope in scopes})

    def test_foreign_machine_and_hash_collision_never_expose_other_scope(self):
        record = self.seed()
        before = record.read_bytes()
        foreign = {**self.scope, "machineId": "other-machine"}
        self.assert_refusal("remote_machine", lambda: self.store.read(foreign))
        key = record.stem
        collision = {**self.scope, "issueId": "unrelated-issue"}
        with patch.object(Store, "_scope_key", return_value=key):
            self.assert_refusal("invalid_record", lambda: self.store.read(collision))
            self.assert_refusal("invalid_record", lambda: self._append(collision))
        self.assertEqual(record.read_bytes(), before)

    def _append(self, scope):
        with self.store.transaction(scope) as document:
            document["audit"].append({"changed": True})

    def test_corrupt_and_unknown_version_documents_remain_byte_identical(self):
        record = self.seed()
        valid = record.read_bytes()
        malformed = [b"{broken", b"[]", b'{"schemaVersion":1,"schemaVersion":1}',
                     b"\xff", b'{"schemaVersion":NaN}',
                     canonical_json({**json.loads(valid), "current": "missing"}).encode(),
                     canonical_json({**json.loads(valid), "unexpected": True}).encode()]
        cases = [(value, "invalid_record") for value in malformed]
        cases.extend((canonical_json({**json.loads(valid), "schemaVersion": version}).encode(),
                      "unsupported_version") for version in (0, 2, True, "1"))
        for data, code in cases:
            with self.subTest(data=data):
                record.write_bytes(data)
                self.assert_refusal(code, lambda: self.store.read(self.scope))
                self.assert_refusal(code, lambda: self._append(self.scope))
                self.assert_refusal(code, self.store.documents)
                self.assertEqual(record.read_bytes(), data)

    def test_store_rejects_repository_and_discovered_workspace_locations(self):
        inside = Store(self.repo / "controller-state")
        self.assert_refusal("workspace_mismatch", lambda: inside.read(self.scope))
        self.assertFalse(inside.root.exists())
        record = self.install_run()
        before = record.read_bytes()
        workspace = self.base
        with self.assertRaises(Refusal) as raised:
            with self.store.transaction(self.scope) as document:
                document["runs"]["run-one"]["workspace"] = {
                    "provider": "omp-worktree", "backing": "linked", "path": str(workspace),
                    "branchRef": "refs/heads/work", "sourceRepository": self.scope["repository"],
                    "commonDir": self.scope["repository"]["commonDir"],
                }
        self.assertEqual(raised.exception.code, "workspace_mismatch")
        self.assertEqual(record.read_bytes(), before)

    def test_symlink_paths_locks_records_and_permissive_files_are_refused(self):
        record = self.seed()
        before = record.read_bytes()
        alias = self.base / "alias"
        alias.symlink_to(self.store.root, target_is_directory=True)
        self.assert_refusal("invalid_record", lambda: Store(alias).read(self.scope))
        self.assert_refusal("invalid_record", lambda: Store(alias / "nested").read(self.scope))
        record.chmod(0o644)
        self.assert_refusal("invalid_record", lambda: self.store.read(self.scope))
        record.chmod(0o600)
        record.unlink()
        outside = self.base / "untouched.json"
        outside.write_bytes(before)
        outside.chmod(0o600)
        record.symlink_to(outside)
        self.assert_refusal("invalid_record", lambda: self.store.read(self.scope))
        self.assert_refusal("invalid_record", self.store.documents)
        record.unlink()
        record.write_bytes(before)
        record.chmod(0o600)
        lock = next(self.store.root.glob("*.lock"))
        lock.unlink()
        lock.symlink_to(outside)
        self.assert_refusal("invalid_record", lambda: self._append(self.scope))
        self.assertEqual(outside.read_bytes(), before)

    def test_documents_surfaces_invalid_names_and_foreign_scope_records(self):
        record = self.seed()
        data = record.read_bytes()
        unexpected = self.store.root / "unrecognized.json"
        unexpected.write_bytes(data)
        unexpected.chmod(0o600)
        self.assert_refusal("invalid_record", self.store.documents)
        unexpected.unlink()
        wrong_name = self.store.root / (("0" * 64) + ".json")
        wrong_name.write_bytes(data)
        wrong_name.chmod(0o600)
        self.assert_refusal("invalid_record", self.store.documents)
        self.assertEqual(wrong_name.read_bytes(), data)

    def test_old_attempts_and_audit_are_retained_and_immutable(self):
        record = self.install_run()
        with self.store.transaction(self.scope) as document:
            document["audit"].append({"firstAttempt": copy.deepcopy(document["runs"]["run-one"])})
            document["runs"]["run-one"]["disposition"] = "superseded"
            next_run = run_record(self.scope, "run-two", 2)
            document["runs"]["run-two"] = next_run
            document["current"] = "run-two"
        before = record.read_bytes()
        for mutation in (lambda document: document["runs"].pop("run-one"),
                         lambda document: document["runs"]["run-one"].update(owner="other"),
                         lambda document: document["audit"].clear()):
            with self.subTest(mutation=mutation):
                with self.assertRaises(Refusal) as raised:
                    with self.store.transaction(self.scope) as document:
                        mutation(document)
                self.assertEqual(raised.exception.code, "invalid_record")
                self.assertEqual(record.read_bytes(), before)
        value = self.store.read(self.scope)
        value["audit"].clear()
        self.assertEqual(record.read_bytes(), before)

    def test_run_and_operation_schemas_and_repeated_fences_are_enforced(self):
        record = self.install_run()
        payload = request(self.scope)
        operation = {"operationId": "operation-one", "requestId": payload["requestId"],
                     "method": "status", "fence": {"runId": "run-one", "attempt": 1, "generation": 1},
                     "state": "succeeded", "phase": "complete", "createdAt": STAMP,
                     "updatedAt": STAMP, "error": None}
        with self.store.transaction(self.scope) as document:
            document["requests"][payload["requestId"]] = {
                "payload": canonical_json(payload), "operationId": "operation-one", "result": None,
            }
            document["operations"]["operation-one"] = {
                "public": operation, "request": payload, "result": None,
                "intent": {"steps": ["reconcile"], "index": 1, "started": False}, "receipts": [],
            }
            document["private"]["run-one"] = {"bindingId": "incarnation", "credential": "not-for-public-responses",
                                              "sequence": 0, "events": {}}
        with self.store.transaction(self.scope) as document:
            document["runs"]["run-one"]["fence"]["generation"] = 2
            document["runs"]["run-one"]["revision"] += 1
        before = record.read_bytes()
        for change in (lambda document: document["runs"]["run-one"].update(harness="unsupported"),
                       lambda document: document["operations"]["operation-one"]["public"].update(state="unknown"),
                       lambda document: document["operations"]["operation-one"]["public"]["fence"].update(attempt=2),
                       lambda document: document["operations"]["operation-one"].update(request={}),
                       lambda document: document["requests"].update(reservation=None),
                       lambda document: document["private"].update(unknown={})):
            with self.subTest(change=change):
                with self.assertRaises(Refusal) as raised:
                    with self.store.transaction(self.scope) as document:
                        change(document)
                self.assertEqual(raised.exception.code, "invalid_record")
                self.assertEqual(record.read_bytes(), before)
        self.assertEqual(self.store.documents(), [self.scope])
        self.assertNotIn("credential", canonical_json(self.store.documents()))


if __name__ == "__main__":
    unittest.main()
