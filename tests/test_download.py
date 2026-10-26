"""Download and SSH timeout/recovery tests. No live server required."""
import queue
import socket
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import paramiko

from sshuttle import (
    SSHuttleApp, TransferCancelled, is_temporary_connection_error,
    manifest_for_download, parse_connection_settings, safe_local_target,
)


class FakeSFTP:
    def __init__(self):
        self.entries = {
            "/remote": ("dir", None),
            "/remote/a.txt": ("file", b"hello"),
            "/remote/empty": ("dir", None),
            "/remote/sub": ("dir", None),
            "/remote/sub/b.bin": ("file", b"12345678"),
            "/remote/sub/link": ("link", None),
        }
        self.downloaded = []
        self.on_get = None

    def lstat(self, path):
        kind, data = self.entries[path]
        modes = {"dir": stat.S_IFDIR, "file": stat.S_IFREG, "link": stat.S_IFLNK}
        return SimpleNamespace(st_mode=modes[kind] | 0o644, st_size=len(data or b""))

    def listdir_attr(self, path):
        prefix = path.rstrip("/") + "/"
        return [SimpleNamespace(filename=key[len(prefix):])
                for key in self.entries
                if key.startswith(prefix) and "/" not in key[len(prefix):] and key[len(prefix):]]

    def get(self, remote, path, callback):
        self.downloaded.append(remote)
        data = self.entries[remote][1]
        Path(path).write_bytes(data)
        if self.on_get:
            self.on_get()
        callback(len(data), len(data))


def make_app():
    app = object.__new__(SSHuttleApp)
    app._cancel_transfer = threading.Event()
    app._events = queue.Queue()
    return app


class DownloadManifestTests(unittest.TestCase):
    def setUp(self):
        self.sftp = FakeSFTP()

    def test_recursive_manifest_and_symlink_skipped(self):
        dirs, files, skipped = manifest_for_download(
            self.sftp, "/remote", ["/remote/a.txt", "/remote/sub", "/remote/empty"])
        self.assertEqual(dirs, ["empty", "sub"])
        self.assertEqual({row[1] for row in files}, {"a.txt", "sub/b.bin"})
        self.assertEqual(skipped, 1)

    def test_refuse_source_outside_remote_folder(self):
        with self.assertRaises(ValueError):
            manifest_for_download(self.sftp, "/remote", ["/elsewhere/private"])

    def test_reject_dangerous_names(self):
        self.sftp.entries["/remote/../escape"] = ("file", b"x")
        with self.assertRaises(ValueError):
            manifest_for_download(self.sftp, "/remote", ["/remote/../escape"])

    def test_safely_create_local_subfolders(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dst = safe_local_target(root, "one/two/hello.txt")
            self.assertEqual(dst, root / "one" / "two" / "hello.txt")
            self.assertTrue((root / "one" / "two").is_dir())

    def test_reject_local_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "actual").mkdir()
            try:
                (root / "link").symlink_to(root / "actual", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("OS doesn't support symlinks")
            with self.assertRaises(ValueError):
                safe_local_target(root, "link/output.txt")


class DownloadTransferTests(unittest.TestCase):
    def test_download_selected_folder_including_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            app, sftp = make_app(), FakeSFTP()
            app._receive_files(sftp, "/remote", ["/remote/sub", "/remote/empty"],
                               Path(temporary), False)
            self.assertEqual((Path(temporary) / "sub" / "b.bin").read_bytes(), b"12345678")
            self.assertTrue((Path(temporary) / "empty").is_dir())
            self.assertFalse((Path(temporary) / "sub" / "link").exists())
            self.assertEqual(sftp.downloaded, ["/remote/sub/b.bin"])

    def test_skip_existing_local_file_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "a.txt"
            path.write_text("original")
            app, sftp = make_app(), FakeSFTP()
            app._receive_files(sftp, "/remote", ["/remote/a.txt"], Path(temporary), False)
            self.assertEqual(path.read_text(), "original")
            self.assertEqual(sftp.downloaded, [])

    def test_replace_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "a.txt"
            path.write_text("original")
            app, sftp = make_app(), FakeSFTP()
            app._receive_files(sftp, "/remote", ["/remote/a.txt"], Path(temporary), True)
            self.assertEqual(path.read_bytes(), b"hello")
            self.assertFalse(list(Path(temporary).glob("*.part")))

    def test_cancel_cleans_partial_temp_and_does_not_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            app, sftp = make_app(), FakeSFTP()
            sftp.on_get = app._cancel_transfer.set
            with self.assertRaises(TransferCancelled):
                app._receive_files(sftp, "/remote", ["/remote/a.txt"], Path(temporary), True)
            self.assertFalse((Path(temporary) / "a.txt").exists())
            self.assertFalse(list(Path(temporary).iterdir()))


class RecoveryPolicyTests(unittest.TestCase):
    def test_timeout_settings_bounds(self):
        self.assertEqual(parse_connection_settings("60", "3"), (60, 3))
        for timeout, retries in [("5", "1"), ("301", "2"), ("60", "6"), ("x", "2")]:
            with self.subTest(timeout=timeout, retries=retries):
                with self.assertRaises(ValueError):
                    parse_connection_settings(timeout, retries)

    def test_temporary_transport_errors_retryable(self):
        self.assertTrue(is_temporary_connection_error(socket.timeout("timed out")))
        self.assertTrue(is_temporary_connection_error(ConnectionResetError("reset")))
        self.assertTrue(is_temporary_connection_error(EOFError("EOF")))

    def test_auth_failure_is_not_retryable(self):
        self.assertFalse(is_temporary_connection_error(paramiko.AuthenticationException("wrong password")))
        self.assertFalse(is_temporary_connection_error(PermissionError("permission denied")))
        self.assertFalse(is_temporary_connection_error(ValueError("invalid path")))


if __name__ == "__main__":
    unittest.main()
