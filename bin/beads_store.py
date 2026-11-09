"""Private, versioned scope documents with permanent advisory lock inodes.

A transaction owns one scope lock, validates the entire document and replaces it
atomically. No index or workspace-local state participates in a commit. ``read``
is an internal controller API: its result includes adapter-private material and
must never be returned directly over a transport.
"""

from contextlib import contextmanager
import copy
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from beads_contract import Refusal, canonical_json, validate


_DOCUMENT_KEYS = {"schemaVersion", "scope", "current", "runs", "operations",
                  "requests", "private", "audit"}
_RECORD_NAME = re.compile(r"[0-9a-f]{64}\.json\Z")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK


def _invalid(message):
    raise Refusal("invalid_record", message)


def _schema(kind, value, definition=None):
    try:
        validate(kind, value, definition)
    except Refusal as error:
        raise Refusal("invalid_record", "Stored public record does not match its schema") from error


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid("Stored JSON contains duplicate keys")
        result[key] = value
    return result


def _parse(text):
    def invalid_constant(_value):
        _invalid("Stored JSON contains a non-finite number")
    try:
        return json.loads(text, object_pairs_hook=_object_pairs,
                          parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        _invalid("Stored document is not valid JSON")


def _inside(path, parent):
    return os.path.commonpath((path, parent)) == parent


class Store:
    def __init__(self, root=None):
        if root is None:
            state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
            root = Path(state) / "herdr-beads/store"
        self.root = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))

    @staticmethod
    def _scope_key(scope):
        validate("run", scope, "Scope")
        repository = scope["repository"]
        identity = {
            "repository": {key: os.path.realpath(repository[key])
                           for key in ("root", "commonDir")},
            "beadsDir": os.path.realpath(scope["beadsDir"]),
            "issueId": scope["issueId"],
        }
        return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()

    def _outside(self, scope, runs=()):
        root = os.path.realpath(self.root)
        locations = [scope["repository"]["root"], scope["repository"]["commonDir"]]
        locations.extend(run["workspace"]["path"] for run in runs
                         if run.get("workspace") is not None)
        if any(_inside(root, os.path.realpath(path)) for path in locations):
            raise Refusal("workspace_mismatch", "Controller state must remain outside the repository and workspaces")

    @contextmanager
    def _directory(self):
        """Walk directory descriptors: not even an ancestor symlink is followed."""
        descriptor = os.open(os.path.sep, _DIRECTORY_FLAGS)
        try:
            for component in self.root.parts[1:]:
                try:
                    child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(component, 0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                _invalid("Store directory must be owner-only")
            yield descriptor
        except OSError as error:
            raise Refusal("invalid_record", "Cannot safely access controller state") from error
        finally:
            os.close(descriptor)

    @staticmethod
    def _check_file(descriptor):
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            _invalid("State and lock files must be private regular files")
        return info

    @staticmethod
    def _same_lock(directory, name, descriptor):
        named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(named.st_mode) or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            _invalid("Scope lock inode changed")

    @contextmanager
    def _locked(self, directory, key):
        name = key + ".lock"
        descriptor = os.open(name, os.O_RDWR | os.O_CREAT | _FILE_FLAGS,
                             0o600, dir_fd=directory)
        try:
            self._check_file(descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN):
                    raise Refusal("pending_operation", "Another operation holds this scope", retryable=True) from error
                raise
            self._same_lock(directory, name, descriptor)
            yield descriptor
            self._same_lock(directory, name, descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _empty(scope):
        return {"schemaVersion": 1, "scope": copy.deepcopy(scope), "current": None,
                "runs": {}, "operations": {}, "requests": {}, "private": {}, "audit": []}

    @staticmethod
    def _matching_scope(actual, expected):
        if actual == expected:
            return
        if actual["repository"] != expected["repository"]:
            raise Refusal("repository_mismatch", "Stored repository differs from the requested scope")
        if actual["beadsDir"] != expected["beadsDir"]:
            raise Refusal("database_mismatch", "Stored database differs from the requested scope")
        if actual["issueId"] != expected["issueId"]:
            _invalid("Stored issue differs from the requested scope")
        raise Refusal("remote_machine", "Stored scope belongs to another machine")

    def _load(self, directory, name, scope=None):
        try:
            descriptor = os.open(name, os.O_RDONLY | _FILE_FLAGS, dir_fd=directory)
        except FileNotFoundError:
            if scope is None:
                _invalid("Discovered scope document disappeared")
            return self._empty(scope)
        try:
            self._check_file(descriptor)
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as source:
                document = _parse(source.read())
        except UnicodeError as error:
            raise Refusal("invalid_record", "Stored document is not UTF-8") from error
        finally:
            os.close(descriptor)
        self._validate(document)
        if name != self._scope_key(document["scope"]) + ".json":
            _invalid("Scope document filename does not match its identity")
        if scope is not None:
            self._matching_scope(document["scope"], scope)
        return document

    @staticmethod
    def _fences(value, expected):
        if isinstance(value, dict):
            fence = value.get("fence")
            if isinstance(fence, dict):
                _schema("run", fence, "Fence")
                if (fence["runId"] != expected["runId"]
                        or fence["attempt"] != expected["attempt"]
                        or fence["generation"] > expected["generation"]):
                    _invalid("Repeated fence does not identify its retained run")
            for child in value.values():
                Store._fences(child, expected)
        elif isinstance(value, list):
            for child in value:
                Store._fences(child, expected)

    def _validate(self, document):
        if not isinstance(document, dict):
            _invalid("Scope document must be an object")
        if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 1:
            raise Refusal("unsupported_version", "Unsupported store document version")
        if set(document) != _DOCUMENT_KEYS:
            _invalid("Scope document fields are malformed")
        _schema("run", document["scope"], "Scope")
        for field in ("runs", "operations", "requests", "private"):
            if not isinstance(document[field], dict):
                _invalid("Scope document mappings are malformed")
        if not isinstance(document["audit"], list):
            _invalid("Scope audit must be an array")
        current = document["current"]
        runs = document["runs"]
        if current is not None and (not isinstance(current, str) or current not in runs):
            _invalid("Current run does not reference a retained run")
        attempts = set()
        for run_id, run in runs.items():
            _schema("run", run)
            if run["fence"]["runId"] != run_id or run["scope"] != document["scope"]:
                _invalid("Run identity differs from its scope or map key")
            attempt = run["fence"]["attempt"]
            if attempt in attempts:
                _invalid("Run attempts are not unique within the scope")
            attempts.add(attempt)
            self._fences(run, run["fence"])
            workspace = run["workspace"]
            if workspace is not None and workspace["sourceRepository"] != document["scope"]["repository"]:
                _invalid("Workspace source repository differs from its scope")
            terminal = run["execution"]["terminal"]
            if terminal is not None and terminal["machineId"] != document["scope"]["machineId"]:
                _invalid("Terminal machine differs from its scope")
        if runs and (current is None or runs[current]["fence"]["attempt"] != max(attempts)):
            _invalid("Current pointer must select the newest retained attempt")
        self._outside(document["scope"], runs.values())
        operations = document["operations"]
        requests = document["requests"]
        for operation_id, entry in operations.items():
            if (not isinstance(entry, dict)
                    or set(entry) != {"public", "request", "result", "intent", "receipts"}
                    or not isinstance(entry["intent"], dict)
                    or not isinstance(entry["receipts"], list)
                    or (entry["result"] is not None and not isinstance(entry["result"], dict))):
                _invalid("Operation storage envelope is malformed")
            public = entry["public"]
            _schema("controller", public, "Operation")
            intent = entry["intent"]
            steps = intent.get("steps")
            if (set(intent) != {"steps", "index", "started"}
                    or not isinstance(steps, list) or not steps
                    or any(not isinstance(step, str) or step not in {"claim", "workspace", "launch", "resume", "focus", "release", "reconcile", "read_only"} for step in steps)
                    or type(intent.get("index")) is not int or not 0 <= intent["index"] <= len(steps)
                    or type(intent.get("started")) is not bool
                    or (public["state"] in ("pending", "running") and intent["index"] == len(steps))):
                _invalid("Operation step intent is malformed")
            if entry["result"] is not None:
                _schema("controller", entry["result"], "Response")
            _schema("controller", entry["request"], "Request")
            request = entry["request"]
            if (public["operationId"] != operation_id or public["requestId"] != request["requestId"]
                    or public["method"] != request["method"] or request["scope"] != document["scope"]):
                _invalid("Operation and request identities disagree")
            run = runs.get(public["fence"]["runId"])
            if run is None:
                _invalid("Operation references an unknown run")
            self._fences(public, run["fence"])
            if public["requestId"] not in requests:
                _invalid("Operation references an unknown request")
            if (not isinstance(requests[public["requestId"]], dict)
                    or requests[public["requestId"]].get("operationId") != operation_id):
                _invalid("Operation request reference is inconsistent")
        for request_id, entry in requests.items():
            if (not isinstance(entry, dict) or set(entry) != {"payload", "operationId", "result"}
                    or not isinstance(entry["payload"], str)
                    or (entry["result"] is not None and not isinstance(entry["result"], dict))):
                _invalid("Request storage envelope is malformed")
            if entry["result"] is not None:
                _schema("controller", entry["result"], "Response")
            payload = _parse(entry["payload"])
            _schema("controller", payload, "Request")
            if (canonical_json(payload) != entry["payload"] or payload["requestId"] != request_id
                    or payload["scope"] != document["scope"]):
                _invalid("Request payload identity is inconsistent")
            operation_id = entry["operationId"]
            if operation_id is not None:
                if not isinstance(operation_id, str) or operation_id not in operations:
                    _invalid("Request references an unknown operation")
                operation = operations[operation_id]
                if operation["public"]["requestId"] != request_id or operation["request"] != payload:
                    _invalid("Request operation payload differs")
        if any(run_id not in runs or not isinstance(value, dict)
               for run_id, value in document["private"].items()):
            _invalid("Private adapter state references an unknown run")
        for run_id, private in document["private"].items():
            if any(field in private for field in ("bindingId", "credential", "sequence", "events")):
                if (not isinstance(private.get("bindingId"), str) or not private["bindingId"]
                        or not isinstance(private.get("credential"), str) or not private["credential"]
                        or type(private.get("sequence")) is not int or private["sequence"] < 0
                        or not isinstance(private.get("events"), dict)):
                    _invalid("Controller binding state is malformed")
                for event_id, payload in private["events"].items():
                    if not isinstance(payload, str):
                        _invalid("Stored adapter observation is malformed")
                    observation = _parse(payload)
                    _schema("adapter", observation, "Observation")
                    self._fences(observation, runs[run_id]["fence"])
                    if (observation["eventId"] != event_id or observation["bindingId"] != private["bindingId"]
                            or observation["sequence"] > private["sequence"]):
                        _invalid("Stored adapter observation identity differs from its binding")
            if "plannedWorkspace" in private:
                planned = private["plannedWorkspace"]
                _schema("run", planned, "Workspace")
                if (planned["sourceRepository"] != document["scope"]["repository"]
                        or (runs[run_id]["workspace"] is not None and runs[run_id]["workspace"] != planned)):
                    _invalid("Planned workspace differs from its run/source identity")
        try:
            canonical_json(document).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError) as error:
            raise Refusal("invalid_record", "Scope document contains non-JSON values") from error

    @staticmethod
    def _preserved(before, after):
        for name in ("runs", "operations", "requests"):
            if not before[name].keys() <= after[name].keys():
                _invalid("Durable history cannot be removed")
        if canonical_json(after["audit"][:len(before["audit"])]) != canonical_json(before["audit"]):
            _invalid("Audit history cannot be rewritten")
        if after["scope"] != before["scope"]:
            _invalid("Scope identity cannot change")
        for run_id, previous in before["runs"].items():
            run = after["runs"][run_id]
            if run_id != before["current"] and canonical_json(run) != canonical_json(previous):
                _invalid("Prior attempts are immutable")
            if (run["fence"]["attempt"] != previous["fence"]["attempt"]
                    or run["fence"]["generation"] < previous["fence"]["generation"]
                    or run["revision"] < previous["revision"]
                    or run["createdAt"] != previous["createdAt"]):
                _invalid("Retained run identity or revision regressed")
            identities = {(s["identity"]["submissionId"], s["identity"]["submissionRevision"]): s["identity"]
                          for s in run["submissions"]}
            if any(identities.get((s["identity"]["submissionId"], s["identity"]["submissionRevision"])) != s["identity"]
                   for s in previous["submissions"]):
                _invalid("Immutable submission identities cannot be removed or rewritten")
        for request_id, previous in before["requests"].items():
            entry = after["requests"][request_id]
            if entry["payload"] != previous["payload"] or entry["operationId"] != previous["operationId"]:
                _invalid("Request reservations cannot be rebound")
        for operation_id, previous in before["operations"].items():
            entry = after["operations"][operation_id]
            if previous["public"]["state"] in ("succeeded", "failed") and canonical_json(entry) != canonical_json(previous):
                _invalid("Completed operation receipts are immutable")
            if entry["request"] != previous["request"] or entry["public"]["fence"] != previous["public"]["fence"]:
                _invalid("Operation reservations cannot be rebound")
            if canonical_json(entry["receipts"][:len(previous["receipts"])]) != canonical_json(previous["receipts"]):
                _invalid("Operation receipts cannot be removed or rewritten")

    @staticmethod
    def _save(directory, name, document):
        temporary = "." + name + "." + uuid.uuid4().hex + ".tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS,
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as destination:
                destination.write(canonical_json(document) + "\n")
                destination.flush()
                os.fsync(descriptor)
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    @contextmanager
    def worker_lock(self):
        """Serialize resource effects while allowing short per-scope requests."""
        with self._directory() as directory, self._locked(directory, "worker"):
            yield

    @contextmanager
    def transaction(self, scope):
        key = self._scope_key(scope)
        self._outside(scope)
        with self._directory() as directory, self._locked(directory, key) as lock:
            document = self._load(directory, key + ".json", scope)
            before = copy.deepcopy(document)
            yield document
            self._validate(document)
            self._preserved(before, document)
            if canonical_json(before) != canonical_json(document):
                self._same_lock(directory, key + ".lock", lock)
                self._save(directory, key + ".json", document)

    def read(self, scope):
        key = self._scope_key(scope)
        self._outside(scope)
        with self._directory() as directory:
            return self._load(directory, key + ".json", scope)

    def documents(self):
        """Discover every scope, refusing malformed records instead of hiding them."""
        scopes = []
        with self._directory() as directory:
            for name in sorted(os.listdir(directory)):
                if name.endswith(".json"):
                    if not _RECORD_NAME.fullmatch(name):
                        _invalid("Unexpected scope document filename")
                    try:
                        scopes.append(copy.deepcopy(self._load(directory, name)["scope"]))
                    except Refusal as error:
                        raise Refusal(error.code, f"{self.root / name}: {error.message}",
                                      error.retryable, error.capability) from error
        return scopes
