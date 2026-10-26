"""Exercise the worker's retry flow without a live SSH server."""
import queue
import socket
import threading
import unittest
from unittest.mock import patch

from sshuttle import SSHuttleApp


class FakeClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeSFTP:
    def __init__(self):
        self.closed = False

    def normalize(self, path):
        return "/remote"

    def close(self):
        self.closed = True


class RecoveryWorkerTests(unittest.TestCase):
    def make_app(self):
        app = object.__new__(SSHuttleApp)
        app._cancel_transfer = threading.Event()
        app._work = queue.Queue()
        app._events = queue.Queue()
        return app

    def test_initial_connection_timeout_then_reconnect(self):
        app = self.make_app()
        app._work.put(("connect", ({"hostname": "slow-server"}, None, 60, 3)))
        app._work.put(("exit", None))
        with patch.object(app, "_open_connection", side_effect=[
                socket.timeout("connection timed out"),
                (FakeClient(), FakeSFTP())]) as open_connection:
            with patch("sshuttle.time.sleep", return_value=None):
                app._worker()
        events = [app._events.get_nowait() for _ in range(app._events.qsize())]
        self.assertEqual(open_connection.call_count, 2)
        self.assertTrue(any(event == "retry" for event, _ in events))
        self.assertIn(("connected", "/remote"), events)
        self.assertFalse(any(event == "connect_error" for event, _ in events))

    def test_transfer_retry_after_network_timeout(self):
        app = self.make_app()
        app._work.put(("connect", ({"hostname": "slow-server"}, None, 60, 2)))
        app._work.put(("upload", ("local", [], "/remote", False)))
        app._work.put(("exit", None))
        with patch.object(app, "_open_connection", side_effect=[
                (FakeClient(), FakeSFTP()), (FakeClient(), FakeSFTP())]) as open_connection:
            with patch.object(app, "_send_files", side_effect=[
                    socket.timeout("read timed out"), None]) as send_files:
                with patch.object(app._cancel_transfer, "wait", return_value=False):
                    app._worker()
        events = [app._events.get_nowait() for _ in range(app._events.qsize())]
        self.assertEqual(send_files.call_count, 2)
        self.assertEqual(open_connection.call_count, 2)
        self.assertTrue(any(event == "retry" and value[0] == "Upload"
                            for event, value in events))
        self.assertFalse(any(event == "error" for event, _ in events))

    def test_auth_failure_never_retry(self):
        import paramiko
        app = self.make_app()
        app._work.put(("connect", ({"hostname": "slow-server"}, "badpass", 60, 3)))
        app._work.put(("exit", None))
        with patch.object(app, "_open_connection",
                          side_effect=paramiko.AuthenticationException("bad password")) as open_connection:
            app._worker()
        events = [app._events.get_nowait() for _ in range(app._events.qsize())]
        self.assertEqual(open_connection.call_count, 1)
        self.assertTrue(any(event == "connect_error" for event, _ in events))
        self.assertFalse(any(event == "retry" for event, _ in events))


if __name__ == "__main__":
    unittest.main()
