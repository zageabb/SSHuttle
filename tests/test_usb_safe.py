"""USB write backpressure unit tests (no SSH server or real USB device)."""
import queue
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sshuttle import (
    SSHuttleApp, TransferCancelled, parse_upload_limit, upload_with_backpressure,
)


class MemoryRemoteFile:
    def __init__(self, owner, path):
        self.owner = owner
        self.path = path
        self.pipelined = None
        self.blocks = []
        self.on_write = None

    def set_pipelined(self, value):
        self.pipelined = value

    def write(self, data):
        if self.pipelined is not False:
            raise AssertionError("Writes must wait for acknowledgements.")
        self.blocks.append(bytes(data))
        if self.on_write:
            self.on_write()

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        self.owner.remote_data[self.path] = b"".join(self.blocks)


class MemorySFTP:
    def __init__(self):
        self.remote_data = {}
        self.last_file = None
        self.report_wrong_size = False

    def file(self, path, mode, bufsize=-1):
        assert mode == "wb"
        assert bufsize == 0
        self.last_file = MemoryRemoteFile(self, path)
        return self.last_file

    def stat(self, path):
        data = self.remote_data.get(path)
        if data is None:
            raise FileNotFoundError(path)
        size = len(data) + (1 if self.report_wrong_size else 0)
        return SimpleNamespace(st_size=size)


class UsbSpeedSettingsTests(unittest.TestCase):
    def test_valid_speed_settings(self):
        for raw, expected in [("0", 0.0), ("2", 2.0), ("0.5", 0.5), ("100", 100.0)]:
            with self.subTest(raw=raw):
                self.assertEqual(parse_upload_limit(raw), expected)

    def test_invalid_speed_settings(self):
        for raw in ["-1", "101", "fast", "nan", "inf", ""]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_upload_limit(raw)


class UsbUploadTests(unittest.TestCase):
    def test_32k_sequential_writes_confirm_size_and_progress(self):
        data = b"z" * 70000
        with tempfile.TemporaryDirectory() as path:
            source = Path(path) / "source.bin"
            source.write_bytes(data)
            sftp = MemorySFTP()
            cancel = threading.Event()
            progress = []
            upload_with_backpressure(
                sftp, source, "/slow-disk/file.part", len(data),
                lambda done, total: progress.append((done, total)),
                cancel, 0,
            )
        self.assertEqual(sftp.remote_data["/slow-disk/file.part"], data)
        self.assertIs(sftp.last_file.pipelined, False)
        self.assertEqual([len(block) for block in sftp.last_file.blocks],
                         [32768, 32768, 4464])
        self.assertEqual(progress[-1], (70000, 70000))

    def test_upload_rate_paces_each_chunk(self):
        with tempfile.TemporaryDirectory() as path:
            source = Path(path) / "small.bin"
            source.write_bytes(b"12345678")
            sftp = MemorySFTP()
            fake_cancel = Mock()
            fake_cancel.is_set.return_value = False
            fake_cancel.wait.return_value = False
            with patch("sshuttle.time.monotonic", return_value=10.0):
                upload_with_backpressure(
                    sftp, source, "/slow/file.part", 8,
                    lambda current, total: None, fake_cancel, 1, chunk_size=4)
        self.assertEqual(fake_cancel.wait.call_count, 2)
        self.assertAlmostEqual(fake_cancel.wait.call_args_list[0].args[0], 4 / (1024 * 1024))

    def test_cancel_interrupts_transfer(self):
        with tempfile.TemporaryDirectory() as path:
            source = Path(path) / "small.bin"
            source.write_bytes(b"a" * 15)
            sftp = MemorySFTP()
            cancel = threading.Event()
            original_file = sftp.file

            def get_file(*args, **kwargs):
                remote_file = original_file(*args, **kwargs)
                remote_file.on_write = cancel.set
                return remote_file

            sftp.file = get_file
            with self.assertRaises(TransferCancelled):
                upload_with_backpressure(
                    sftp, source, "/slow/file.part", 15,
                    lambda current, total: None, cancel, 0, chunk_size=4)
        self.assertEqual(sftp.remote_data["/slow/file.part"], b"aaaa")

    def test_bad_remote_size_raises_instead_of_committing(self):
        with tempfile.TemporaryDirectory() as path:
            source = Path(path) / "small.bin"
            source.write_bytes(b"hello")
            sftp = MemorySFTP()
            sftp.report_wrong_size = True
            with self.assertRaisesRegex(IOError, "size mismatch"):
                upload_with_backpressure(
                    sftp, source, "/slow/file.part", 5,
                    lambda current, total: None, threading.Event(), 0)

    def test_uploaded_file_uses_temp_and_same_existing_file_policy(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            (root / "source.txt").write_text("12345")
            app = object.__new__(SSHuttleApp)
            app._events = queue.Queue()
            app._cancel_transfer = threading.Event()
            # Mock filesystem primitives; test upload mode keeps its
            # original final rename path and skips already-existing files.
            sftp = Mock()
            app._mkdir_remote = Mock()
            app._remote_exists = Mock(side_effect=[False, False])
            sftp.file.side_effect = lambda name, mode, bufsize=0: MemoryRemoteFile(sftp, name)
            # Build one valid mock remote with files registered after close.
            sftp.remote_data = {}
            sftp.stat.side_effect = lambda name: SimpleNamespace(
                st_size=len(sftp.remote_data[name]))
            app._send_files(sftp, root, [root / "source.txt"], "/usb", False, True, 0)
            self.assertEqual(sftp.rename.call_count, 1)
            name = sftp.rename.call_args.args[0]
            self.assertIn(".sshuttle-", name)
            self.assertEqual(sftp.rename.call_args.args[1], "/usb/source.txt")
            self.assertEqual(sftp.remote_data[name], b"12345")
            self.assertEqual(sftp.put.call_count, 0)


if __name__ == "__main__":
    unittest.main()
