"""Transport tests use real owner-only Unix sockets and injectable controllers."""
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / "bin"))

import beads_transport as transport


CLI = Path(__file__).parent / "bin" / "herdr-beads"


def status_request(request_id="status-1"):
    return {"apiVersion": 1, "requestId": request_id, "method": "status",
            "scope": {"repository": {"root": "/repository", "commonDir": "/repository/.git"},
                      "beadsDir": "/repository/.beads", "issueId": "issue-1",
                      "machineId": "machine-1"}, "params": {"runId": None}}


class FakeController:
    def __init__(self):
        self.calls = []
        self.work_calls = 0

    def handle(self, value, *, trusted_human):
        self.calls.append((value, trusted_human))
        return {"apiVersion": 1, "requestId": value.get("requestId"), "outcome": "ok",
                "action": "status", "run": None, "operation": None, "actions": [],
                "support": {"harnesses": ["omp"],
                            "execution": {"harness": "omp", "supported": [], "unsupported": []},
                            "workspaceProvider": "omp-worktree",
                            "integrationModes": ["local-target", "github-pr", "remote-target"]}}

    def work_once(self):
        self.work_calls += 1
        return False


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "c.sock"
        self.servers = []
        self.addCleanup(self.stop_servers)

    def start_server(self, controller=None):
        controller = controller or FakeController()
        server = transport.ControllerServer(controller, self.path).start()
        errors = []

        def run():
            try:
                server.serve_forever()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.servers.append((server, thread, errors))
        return server, controller, thread, errors

    def stop_servers(self):
        for server, thread, _ in self.servers:
            server.stopped.set()
            thread.join(6)
            server.close()

    def exchange(self, data):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(self.path))
            connection.sendall(data)
            if not data.endswith(b"\n"):
                # EOF is only needed to exercise a missing frame terminator.
                connection.shutdown(socket.SHUT_WR)
            chunks = bytearray()
            while True:
                try:
                    part = connection.recv(65536)
                except ConnectionResetError:
                    if chunks:
                        break
                    raise
                if not part:
                    break
                chunks.extend(part)
        return json.loads(chunks)

    def cli(self, value, *args):
        return subprocess.run([sys.executable, str(CLI), "request", "--socket", str(self.path), *args],
                              input=value, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)

    def test_status_round_trip_with_exact_forwarding_and_private_paths(self):
        server, controller, _, _ = self.start_server()
        value = status_request()
        response = transport.request(value, self.path)
        self.assertEqual(response["outcome"], "ok")
        self.assertEqual(response["action"], "status")
        self.assertEqual(controller.calls, [(value, False)])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertIsNotNone(server.lock_fd)

    def test_cli_status_and_exit_codes(self):
        self.start_server()
        result = self.cli(json.dumps(status_request()).encode())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["outcome"], "ok")
        for outcome, code in (("ok", 0), ("accepted", 0), ("refused", 2), ("error", 1)):
            self.assertEqual(transport.exit_code({"outcome": outcome}), code)

    def test_client_json_never_attests_human_authority(self):
        _, controller, _, _ = self.start_server()
        value = {**status_request(), "method": "confirm_integration", "trusted_human": True,
                 "trustedHuman": True, "humanConfirmed": True}
        self.assertEqual(transport.request(value, self.path)["outcome"], "ok")
        self.assertEqual(controller.calls, [(value, False)])

    def test_accepted_response_survives_server_close_before_client_reads(self):
        class AcceptingController(FakeController):
            def handle(self, value, *, trusted_human):
                response = super().handle(value, trusted_human=trusted_human)
                response.update(outcome="accepted",
                                operation={"operationId": "operation-1", "state": "pending"})
                return response

        server, controller, _, errors = self.start_server(AcceptingController())
        closed = threading.Event()
        serve_connection = server._connection

        def complete_connection(connection):
            serve_connection(connection)
            closed.set()

        caller = threading.current_thread()
        test = self

        class ScheduledSocket(socket.socket):
            def sendall(self, data, flags=0):
                super().sendall(data, flags)
                if threading.current_thread() is caller:
                    # Force the peer's real close before the client can read or half-close.
                    test.assertTrue(closed.wait(2), "Server did not finish the request")

        value = {**status_request("accepted-1"), "method": "reconcile"}
        with patch.object(server, "_connection", complete_connection):
            with patch.object(socket, "socket", ScheduledSocket):
                response = transport.request(value, self.path)

        self.assertEqual(response["outcome"], "accepted", response)
        self.assertEqual(response["operation"]["operationId"], "operation-1")
        self.assertEqual(controller.calls, [(value, False)])
        self.assertEqual(errors, [])

    def test_bad_frames_are_json_refusals_without_controller_calls(self):
        _, controller, _, _ = self.start_server()
        for frame in (b"\n", b"[]\n", b"{bad}\n", b"\xff\n", b"{}", b"{}\n{}\n",
                      b'{"a":1,"a":2}\n', b'{"value":NaN}\n'):
            with self.subTest(frame=frame):
                response = self.exchange(frame)
                self.assertEqual(response["outcome"], "refused")
                self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertEqual(controller.calls, [])

    def test_oversized_socket_frame_is_refused(self):
        _, controller, _, _ = self.start_server()
        with patch.object(transport, "MAX_REQUEST_BYTES", 128):
            response = self.exchange(b'{"value":"' + b"x" * 200 + b'"}\n')
        self.assertEqual(response["outcome"], "refused")
        self.assertEqual(controller.calls, [])

    def test_bounded_input_and_single_object(self):
        for data in (b" " * (transport.MAX_REQUEST_BYTES + 1), b"{} {}", b"[]", b"", b'{"x":1,"x":2}'):
            with self.subTest(size=len(data)):
                with self.assertRaises(transport.TransportError) as raised:
                    transport.read_input(io.BytesIO(data), time.monotonic() + 1)
                self.assertTrue(raised.exception.refused)
        self.assertEqual(transport.read_input(io.BytesIO(b' {"value": 1}\n '),
                                             time.monotonic() + 1), {"value": 1})

    def test_cli_oversized_input_returns_refusal_json(self):
        result = self.cli(b" " * (transport.MAX_REQUEST_BYTES + 1))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "invalid_request")
        self.assertFalse(self.path.exists())

    def test_unavailable_server_returns_error_and_exit_one(self):
        response = transport.request(status_request(), self.path)
        self.assertEqual(response["outcome"], "error")
        self.assertEqual(response["error"]["code"], "service_unavailable")
        result = self.cli(json.dumps(status_request()).encode())
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "service_unavailable")

    def test_frame_and_input_waits_obey_deadline(self):
        reader, writer = socket.socketpair()
        with reader, writer:
            start = time.monotonic()
            with self.assertRaises((transport.TransportError, TimeoutError)):
                transport._read_frame(reader, 128, start + 0.05)
            self.assertLess(time.monotonic() - start, 1)
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(read_fd, "rb") as stream:
                start = time.monotonic()
                with self.assertRaises(transport.TransportError) as raised:
                    transport.read_input(stream, start + 0.05)
                self.assertEqual(raised.exception.code, "timeout")
                self.assertLess(time.monotonic() - start, 1)
        finally:
            os.close(write_fd)

    def test_blocked_worker_does_not_block_status_or_depend_on_caller(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingController(FakeController):
            def work_once(self):
                entered.set()
                release.wait(4)
                return False

        server, controller, _, _ = self.start_server(BlockingController())
        try:
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.assertEqual(transport.request(status_request(), self.path)["outcome"], "ok")
            self.assertLess(time.monotonic() - started, 1)
            self.assertTrue(server.worker.is_alive())
            self.assertEqual(len(controller.calls), 1)
        finally:
            release.set()

    def test_singleton_lock_is_retained_and_second_server_refuses(self):
        server, _, thread, _ = self.start_server()
        lock_path = Path(str(self.path) + ".lock")
        identity = lock_path.stat().st_ino
        with self.assertRaises(transport.TransportError) as raised:
            transport.ControllerServer(FakeController(), self.path).start()
        self.assertEqual(raised.exception.code, "pending_operation")
        self.assertTrue(self.path.exists())
        server.stopped.set()
        thread.join(2)
        self.assertFalse(self.path.exists())
        self.assertEqual(lock_path.stat().st_ino, identity)
        replacement = transport.ControllerServer(FakeController(), self.path).start()
        try:
            self.assertEqual(lock_path.stat().st_ino, identity)
        finally:
            replacement.close()

    def test_unknown_file_and_live_socket_are_never_unlinked(self):
        self.path.write_text("do not remove")
        with self.assertRaises(transport.TransportError):
            transport.ControllerServer(FakeController(), self.path).start()
        self.assertEqual(self.path.read_text(), "do not remove")
        self.path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(self.path))
            listener.listen(1)
            identity = self.path.stat().st_ino
            with self.assertRaises(transport.TransportError) as raised:
                transport.ControllerServer(FakeController(), self.path).start()
            self.assertEqual(raised.exception.code, "pending_operation")
            self.assertEqual(self.path.stat().st_ino, identity)

    def test_stale_same_owner_socket_is_reclaimed_under_lock(self):
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.path))
        stale.close()
        self.start_server()
        self.assertEqual(transport.request(status_request(), self.path)["outcome"], "ok")

    def test_symlink_paths_and_insecure_directory_are_refused(self):
        other = self.root / "other"
        other.write_text("preserve")
        self.path.symlink_to(other)
        with self.assertRaises(transport.TransportError):
            transport.ControllerServer(FakeController(), self.path).start()
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(other.read_text(), "preserve")
        self.path.unlink()
        self.root.chmod(0o755)
        try:
            with self.assertRaises(transport.TransportError) as raised:
                transport.ControllerServer(FakeController(), self.path).start()
            self.assertEqual(raised.exception.code, "owner_mismatch")
        finally:
            self.root.chmod(0o700)

    def test_peer_owner_mismatch_is_refused(self):
        _, controller, _, _ = self.start_server()
        with patch.object(transport, "_peer_uid", return_value=os.getuid() + 1):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(2)
                connection.connect(str(self.path))
                # Peer authorization precedes reading any request bytes.
                response = transport._read_frame(connection, transport.MAX_RESPONSE_BYTES,
                                                 time.monotonic() + 2)
        self.assertEqual(response["outcome"], "refused")
        self.assertEqual(response["error"]["code"], "owner_mismatch")
        self.assertEqual(controller.calls, [])

    def test_worker_failure_is_visible_and_stops_service(self):
        class FailingController(FakeController):
            def work_once(self):
                raise RuntimeError("private adapter details")

        server, _, thread, errors = self.start_server(FailingController())
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(server.stopped.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], transport.TransportError)
        self.assertEqual(errors[0].code, "dependency_failed")
        self.assertNotIn("private adapter details", str(errors[0]))

    def test_oversized_response_is_visible_error(self):
        class OversizedController(FakeController):
            def handle(self, value, *, trusted_human):
                response = super().handle(value, trusted_human=trusted_human)
                response["padding"] = "x" * transport.MAX_RESPONSE_BYTES
                return response

        self.start_server(OversizedController())
        response = transport.request(status_request(), self.path)
        self.assertEqual(response["outcome"], "error")
        self.assertEqual(response["error"]["code"], "io_error")

    def test_default_socket_respects_xdg_state_home(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root)}):
            self.assertEqual(transport.default_socket_path(), self.root / "herdr-beads" / "controller.sock")
        with patch.dict(os.environ, {"XDG_STATE_HOME": "relative"}):
            with self.assertRaises(transport.TransportError):
                transport.default_socket_path()


if __name__ == "__main__":
    unittest.main()
