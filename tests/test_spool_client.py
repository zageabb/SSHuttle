"""Unit tests for desktop submission of Ubuntu USB Spooler jobs."""
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from sshuttle import SSHuttleApp, spool_subdirectory, make_spool_manifest


class FakeOutput:
    def __init__(self, filesystem, path):
        self.filesystem = filesystem
        self.path = path
        self.buffer = bytearray()

    def write(self, data):
        self.buffer.extend(data)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.filesystem[self.path] = bytes(self.buffer)


class FakeSFTP:
    def __init__(self):
        self.files = {}
        self.directories = {"/srv/sshuttle/staging"}
        self.events = []

    def stat(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return SimpleNamespace(st_size=len(self.files[path]))

    def statvfs(self, path):
        return SimpleNamespace(f_bavail=10**7, f_frsize=4096)

    def file(self, path, mode):
        self.events.append(("open", path))
        return FakeOutput(self.files, path)

    def rename(self, original, destination):
        self.events.append(("rename", destination))
        self.files[destination] = self.files.pop(original)


class SpoolClientTests(unittest.TestCase):
    def test_subfolder_validation(self):
        for bad in ("/etc", "../escape", "a/../b", "a//b", "a\\b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                spool_subdirectory(bad)
        self.assertEqual(spool_subdirectory("backup/pictures"), "backup/pictures")

    def test_manifest_hashes_sources(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "foo.txt").write_bytes(b"abc")
            manifest = make_spool_manifest(root, [root / "foo.txt"], "a" * 32, "backup", False)
        self.assertEqual(manifest["files"][0]["sha256"],
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_queue_submitted_only_after_upload_completes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.txt").write_text("abc")
            app = object.__new__(SSHuttleApp)
            app._cancel_transfer = threading.Event()
            app._events = queue.Queue()
            app._mkdir_remote = Mock()
            sftp = FakeSFTP()
            job_id = "a" * 32
            def uploaded(*args, **kwargs):
                self.assertNotIn((
                    f"/srv/sshuttle/staging/jobs/{job_id}/READY.json"), sftp.files)
            app._send_files = Mock(side_effect=uploaded)
            app._upload_to_spooler(sftp, root, [root / "a.txt"],
                                   "/srv/sshuttle/staging", "archive", False, job_id)
            ready = f"/srv/sshuttle/staging/jobs/{job_id}/READY.json"
            manifest = json.loads(sftp.files[ready])
            self.assertEqual(manifest["files"][0]["size"], 3)
            self.assertEqual(manifest["destination_subdir"], "archive")
            self.assertEqual(sftp.events[-1], ("rename", ready))
            self.assertTrue(any(event == "spool_queued"
                                for event, _ in iter(app._events.get_nowait, None)
                                if not app._events.empty()))


if __name__ == "__main__":
    unittest.main()
