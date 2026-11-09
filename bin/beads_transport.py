"""Owner-only, bounded JSON transport; lifecycle policy belongs to Controller.

The socket never attests a human receipt: every call uses trusted_human=False.
Wire messages are single newline-terminated JSON objects, with at most 1 MiB
per request and 4 MiB per response (including the newline).
"""
import argparse
import ctypes
import errno
import fcntl
import io
import json
import os
from pathlib import Path
import select
import socket
import stat
import struct
import sys
import threading
import time

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEADLINE_SECONDS = 5.0
MAX_CONNECTIONS = 32


class TransportError(Exception):
    def __init__(self, code, message, *, refused=False, retryable=False):
        super().__init__(message)
        self.code = code
        self.refused = refused
        self.retryable = retryable


def default_socket_path():
    state = os.environ.get("XDG_STATE_HOME")
    root = Path(state) if state else Path.home() / ".local" / "state"
    if not root.is_absolute():
        raise TransportError("io_error", "XDG_STATE_HOME must be absolute")
    return root / "herdr-beads" / "controller.sock"


def error_response(code, message, request_id=None, *, refused=False, retryable=False):
    error = {"code": code, "message": message, "retryable": retryable}
    if refused:
        error["capability"] = None
    return {"apiVersion": 1, "requestId": request_id,
            "outcome": "refused" if refused else "error", "error": error,
            "run": None, "operation": None}


def _error(exc, request_id=None):
    return error_response(exc.code, str(exc), request_id,
                          refused=exc.refused, retryable=exc.retryable)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TransportError("timeout", "Transport deadline exceeded", retryable=True)
    return remaining


def _reject_constant(_value):
    raise ValueError("Non-finite JSON number")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _parse_object(data):
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=_reject_constant,
                           object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError):
        raise TransportError("invalid_request", "Expected exactly one UTF-8 JSON object",
                             refused=True) from None
    if not isinstance(value, dict):
        raise TransportError("invalid_request", "Expected exactly one JSON object", refused=True)
    return value


def _encode(value, limit):
    try:
        data = json.dumps(value, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("utf-8") + b"\n"
    except (TypeError, ValueError, RecursionError):
        raise TransportError("io_error", "Cannot encode JSON message") from None
    if len(data) > limit:
        raise TransportError("invalid_request" if limit == MAX_REQUEST_BYTES else "io_error",
                             "JSON message exceeds transport limit",
                             refused=limit == MAX_REQUEST_BYTES)
    return data


def read_input(stream, deadline):
    """Read one bounded JSON object, including a deadline for a stalled pipe."""
    data = bytearray()
    try:
        fd = stream.fileno()
    except (AttributeError, io.UnsupportedOperation):
        # In-memory streams are useful to embedders and cannot block on I/O.
        if not isinstance(stream, (io.BytesIO, io.StringIO)):
            raise TransportError("io_error", "Input must expose a selectable descriptor") from None
        chunk = stream.read(MAX_REQUEST_BYTES + 1)
        data.extend(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
    else:
        while True:
            readable, _, _ = select.select([fd], [], [], _remaining(deadline))
            if not readable:
                raise TransportError("timeout", "Input deadline exceeded", retryable=True)
            chunk = os.read(fd, min(65536, MAX_REQUEST_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_REQUEST_BYTES:
                break
    if len(data) > MAX_REQUEST_BYTES:
        raise TransportError("invalid_request", "Input exceeds 1 MiB", refused=True)
    _remaining(deadline)
    return _parse_object(bytes(data))


def _read_frame(connection, limit, deadline):
    data = bytearray()
    while True:
        connection.settimeout(_remaining(deadline))
        chunk = connection.recv(min(65536, limit + 1 - len(data)))
        if not chunk:
            raise TransportError("invalid_request", "Missing newline-terminated JSON frame",
                                 refused=True)
        data.extend(chunk)
        if len(data) > limit:
            raise TransportError("invalid_request", "JSON frame exceeds transport limit", refused=True)
        if b"\n" in chunk:
            newline = data.index(b"\n")
            if newline != len(data) - 1:
                raise TransportError("invalid_request", "Only one JSON frame is allowed", refused=True)
            return _parse_object(bytes(data[:-1]))


def _peer_uid(connection):
    if hasattr(socket, "SO_PEERCRED"):
        credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                             struct.calcsize("3i"))
        return struct.unpack("3i", credentials)[1]
    if hasattr(connection, "getpeereid"):
        return connection.getpeereid()[0]
    if sys.platform == "darwin":
        try:
            getpeereid = ctypes.CDLL(None, use_errno=True).getpeereid
        except AttributeError:
            return None
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
                              ctypes.POINTER(ctypes.c_uint)]
        getpeereid.restype = ctypes.c_int
        if getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise OSError(ctypes.get_errno(), "Cannot verify socket peer")
        return uid.value
    return None


def _check_peer(connection):
    uid = _peer_uid(connection)
    if uid is not None and uid != os.getuid():
        raise TransportError("owner_mismatch", "Socket peer is not the current user", refused=True)


def _directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise TransportError("owner_mismatch", "Socket directory must be owner-only (0700)",
                                 refused=True)
    finally:
        os.close(fd)


def _socket_info(path):
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise TransportError("owner_mismatch", "Refusing an unknown or foreign socket path", refused=True)
    return info


def request(value, socket_path=None, *, deadline=None):
    """Forward exactly one object, without interpreting lifecycle policy."""
    deadline = time.monotonic() + DEADLINE_SECONDS if deadline is None else min(
        deadline, time.monotonic() + DEADLINE_SECONDS)
    request_id = value.get("requestId") if isinstance(value, dict) else None
    if not isinstance(request_id, str) or not request_id:
        request_id = None
    try:
        if not isinstance(value, dict):
            raise TransportError("invalid_request", "Expected one JSON object", refused=True)
        data = _encode(value, MAX_REQUEST_BYTES)
        path = Path(socket_path) if socket_path is not None else default_socket_path()
        _directory(path.parent)
        info = _socket_info(path)
        if info.st_mode & 0o077:
            raise TransportError("owner_mismatch", "Socket must be owner-only (0600)", refused=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_remaining(deadline))
            connection.connect(str(path))
            _check_peer(connection)
            connection.settimeout(_remaining(deadline))
            connection.sendall(data)
            connection.shutdown(socket.SHUT_WR)
            try:
                response = _read_frame(connection, MAX_RESPONSE_BYTES, deadline)
            except TransportError as exc:
                if exc.refused:
                    raise TransportError("io_error", "Server returned an invalid response") from None
                raise
        outcome = response.get("outcome")
        response_id = response.get("requestId")
        uncorrelated_error = response_id is None and outcome in ("refused", "error")
        if (response.get("apiVersion") != 1
                or (response_id != request_id and not uncorrelated_error)
                or outcome not in ("ok", "accepted", "refused", "error")):
            raise TransportError("io_error", "Server returned an invalid response envelope")
        return response
    except TransportError as exc:
        return _error(exc, request_id)
    except (TimeoutError, socket.timeout):
        return error_response("timeout", "Transport deadline exceeded", request_id, retryable=True)
    except OSError:
        return error_response("service_unavailable", "Controller socket is unavailable", request_id,
                              retryable=True)


class ControllerServer:
    """Injectable foreground socket server with one independent lifecycle worker."""
    def __init__(self, controller, socket_path=None):
        self.controller = controller
        self.path = Path(socket_path) if socket_path is not None else default_socket_path()
        self.stopped = threading.Event()
        self.wakeup = threading.Event()
        self.failure = None
        self.listener = None
        self.lock_fd = None
        self.bound_identity = None
        self.worker = None
        self.connections = set()
        self.connections_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def start(self):
        if self.listener is not None or self.stopped.is_set():
            raise TransportError("io_error", "Server cannot be started again")
        try:
            _directory(self.path.parent, create=True)
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            self.lock_fd = os.open(str(self.path) + ".lock", flags, 0o600)
            info = os.fstat(self.lock_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise TransportError("owner_mismatch", "Unsafe controller singleton lock", refused=True)
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise TransportError("pending_operation", "Controller server is already running",
                                     refused=True, retryable=True) from None
            self._reclaim_stale()
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(str(self.path))
            self.bound_identity = self.path.lstat()
            os.chmod(self.path, 0o600)
            self.listener.listen(MAX_CONNECTIONS)
            self.listener.settimeout(0.1)
            self.worker = threading.Thread(target=self._work, name="beads-lifecycle", daemon=True)
            self.worker.start()
            return self
        except BaseException:
            self.close()
            raise

    def _reclaim_stale(self):
        try:
            original = _socket_info(self.path)
        except FileNotFoundError:
            return
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            try:
                probe.connect(str(self.path))
            except OSError as exc:
                if exc.errno != errno.ECONNREFUSED:
                    raise TransportError("io_error", "Cannot establish that existing socket is stale") from None
            else:
                raise TransportError("pending_operation", "An existing socket server is live",
                                     refused=True, retryable=True)
        current = _socket_info(self.path)
        if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
            raise TransportError("io_error", "Socket changed during stale recovery")
        self.path.unlink()

    def _work(self):
        try:
            while not self.stopped.is_set():
                if not self.controller.work_once():
                    self.stopped.wait(0.05)
        except Exception as error:
            from beads_contract import Refusal
            if isinstance(error, Refusal):
                self.failure = TransportError(error.code, str(error), refused=True, retryable=error.retryable)
            else:
                self.failure = TransportError("dependency_failed", "Lifecycle worker failed; service stopped")
            self.stopped.set()

    def _connection(self, connection):
        request_id = None
        deadline = time.monotonic() + DEADLINE_SECONDS
        try:
            with connection:
                try:
                    _check_peer(connection)
                    value = _read_frame(connection, MAX_REQUEST_BYTES, deadline)
                    candidate = value.get("requestId")
                    request_id = candidate if isinstance(candidate, str) and candidate else None
                    if self.failure is not None:
                        response = _error(self.failure, request_id)
                    else:
                        response = self.controller.handle(value, trusted_human=False)
                        if value.get("method") != "status" and response.get("outcome") in ("ok", "accepted"):
                            self.wakeup.set()
                    data = _encode(response, MAX_RESPONSE_BYTES)
                except TransportError as exc:
                    data = _encode(_error(exc, request_id), MAX_RESPONSE_BYTES)
                except (TimeoutError, socket.timeout):
                    data = _encode(error_response("timeout", "Request deadline exceeded", request_id,
                                                  retryable=True), MAX_RESPONSE_BYTES)
                except Exception:
                    data = _encode(error_response("dependency_failed", "Controller request failed", request_id),
                                   MAX_RESPONSE_BYTES)
                # A bad or stalled peer must not retain a request thread indefinitely.
                connection.settimeout(max(0.001, deadline - time.monotonic()))
                connection.sendall(data)
        except OSError:
            pass  # Disconnected callers do not cancel accepted lifecycle operations.
        finally:
            with self.connections_lock:
                self.connections.discard(threading.current_thread())
            self.slots.release()

    def serve_forever(self):
        if self.listener is None:
            self.start()
        try:
            while not self.stopped.is_set():
                try:
                    connection, _ = self.listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stopped.is_set():
                        break
                    raise
                if not self.slots.acquire(blocking=False):
                    try:
                        connection.settimeout(0.01)
                        connection.sendall(_encode(error_response("service_unavailable", "Server is busy",
                                                                  retryable=True), MAX_RESPONSE_BYTES))
                    except OSError:
                        pass
                    finally:
                        connection.close()
                    continue
                thread = threading.Thread(target=self._connection, args=(connection,), daemon=True)
                with self.connections_lock:
                    self.connections.add(thread)
                thread.start()
            if self.failure is not None:
                raise self.failure
        finally:
            self.close()

    def close(self):
        self.stopped.set()
        self.wakeup.set()
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        if self.bound_identity is not None:
            try:
                current = self.path.lstat()
                if ((current.st_dev, current.st_ino, current.st_uid)
                        == (self.bound_identity.st_dev, self.bound_identity.st_ino, os.getuid())):
                    self.path.unlink()
            except FileNotFoundError:
                pass
            self.bound_identity = None
        deadline = time.monotonic() + DEADLINE_SECONDS
        with self.connections_lock:
            threads = list(self.connections)
        if self.worker is not None:
            threads.append(self.worker)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))
        # Never release the singleton while a previous controller call can still mutate.
        # In a foreground CLI, process exit closes this retained descriptor regardless.
        if self.lock_fd is not None and not any(thread.is_alive() for thread in threads):
            os.close(self.lock_fd)
            self.lock_fd = None


def exit_code(response):
    return {"ok": 0, "accepted": 0, "refused": 2}.get(response.get("outcome"), 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Owner-only Bead controller transport")
    parser.add_argument("--socket", help="Explicit Unix socket path for isolated runs")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("request", "serve"):
        child = subparsers.add_parser(command)
        child.add_argument("--socket", dest="command_socket", help="Explicit Unix socket path")
    args = parser.parse_args(argv)
    path = args.command_socket or args.socket
    try:
        if args.command == "request":
            deadline = time.monotonic() + DEADLINE_SECONDS
            value = read_input(sys.stdin.buffer, deadline)
            response = request(value, path, deadline=deadline)
        else:
            from beads_controller import Controller
            from beads_identity import machine_id
            from beads_identity import Authority
            from beads_omp import OmpExecution
            from beads_workspace import OmpWorkspace
            authority = Authority()
            execution = OmpExecution(authority=authority, socket_path=path or default_socket_path())
            ControllerServer(Controller(machine_id(), authority=authority, execution=execution,
                                        workspace=OmpWorkspace()), path).serve_forever()
            return 0
    except KeyboardInterrupt:
        return 0 if args.command == "serve" else 1
    except TransportError as exc:
        response = _error(exc)
    except (TimeoutError, socket.timeout):
        response = error_response("timeout", "Transport deadline exceeded", retryable=True)
    except Exception as exc:
        # Contract refusals can occur before the service exists (e.g. unknown machine).
        from beads_contract import Refusal
        if isinstance(exc, Refusal):
            response = error_response(exc.code, str(exc), refused=True, retryable=exc.retryable)
        else:
            response = error_response("io_error", "Controller service could not complete the request")
    sys.stdout.write(json.dumps(response, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n")
    return exit_code(response)
