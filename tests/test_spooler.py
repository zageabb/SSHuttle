"""Unit tests for the on-server USB Spooler. No real USB device required."""
import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sshuttle_spooler import Spooler, relative_path, safe_path


def config(staging, usb):
    return {"staging_root": staging, "usb_mount": usb, "limit_mib": 100,
            "expected_usb_uuid": "", "verify_readback": True,
            "min_staging_free_gib": 0, "poll_seconds": 2, "max_retries": 3}


def place_job(staging, data=b"hello there", subdir="archive", overwrite=False,
              expected_digest=None, files=True, dirs=None):
    job_id = "a" * 32
    job = staging / "jobs" / job_id
    (job / "files").mkdir(parents=True, exist_ok=True)
    entries = []
    if files:
        (job / "files" / "test.txt").write_bytes(data)
        entries = [{"path": "test.txt", "size": len(data),
                    "sha256": expected_digest or hashlib.sha256(data).hexdigest()}]
    (job / "READY.json").write_text(json.dumps({
        "version": 1, "job_id": job_id, "overwrite": overwrite,
        "destination_subdir": subdir, "dirs": dirs or [], "files": entries,
    }), encoding="utf-8")
    return job_id


class ManifestSafetyTests(unittest.TestCase):
    def test_relative_path_rejects_escapes(self):
        for bad in ("/etc", "../escape", "foo/../bar", "foo//bar", "foo\\bar", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                relative_path(bad)
        self.assertEqual(relative_path("", allow_empty=True), "")

    def test_safe_path_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "actual").mkdir()
            try:
                (root / "redirect").symlink_to(root / "actual")
            except (OSError, NotImplementedError):
                self.skipTest("No symlink support")
            with self.assertRaises(ValueError):
                safe_path(root, "redirect/file.txt", create_dirs=True)


class SpoolerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stage = Path(self.temp.name) / "stage"
        self.usb = Path(self.temp.name) / "usb"
        self.stage.mkdir()
        self.usb.mkdir()
        self.spool = Spooler(config(self.stage, self.usb))
        self.addCleanup(self.spool.close)

    def mock_mount(self):
        return patch.object(self.spool, "usb_ready", return_value=True)

    def test_stage_queue_copies_verifies_and_cleans_staging(self):
        job_id = place_job(self.stage)
        self.spool.scan()
        self.assertEqual(self.spool.db.execute(
            "SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0], "queued")
        with self.mock_mount():
            self.assertTrue(self.spool.process_one())
            self.assertTrue(self.spool.process_one())
        self.assertEqual((self.usb / "archive" / "test.txt").read_bytes(), b"hello there")
        status = json.loads((self.stage / "jobs" / job_id / "status.json").read_text())
        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["completed_files"], 1)
        self.assertFalse((self.stage / "jobs" / job_id / "files").exists())

    def test_skip_does_not_overwrite_existing_usb_file(self):
        target = self.usb / "archive"
        target.mkdir()
        (target / "test.txt").write_bytes(b"do not touch")
        job_id = place_job(self.stage)
        self.spool.scan()
        with self.mock_mount():
            self.spool.process_one()
            self.spool.process_one()
        self.assertEqual((target / "test.txt").read_bytes(), b"do not touch")
        result = self.spool.db.execute(
            "SELECT status FROM entries WHERE job=?", (job_id,)).fetchone()[0]
        self.assertEqual(result, "skipped")

    def test_explicit_overwrite(self):
        target = self.usb / "archive"
        target.mkdir()
        (target / "test.txt").write_text("old content")
        job_id = place_job(self.stage, overwrite=True)
        self.spool.scan()
        with self.mock_mount():
            self.spool.process_one()
        self.assertEqual((target / "test.txt").read_bytes(), b"hello there")

    def test_bad_checksum_never_committed(self):
        job_id = place_job(self.stage, expected_digest="0" * 64)
        self.spool.scan()
        with self.mock_mount():
            self.spool.process_one()
        self.assertFalse((self.usb / "archive" / "test.txt").exists())
        status = json.loads((self.stage / "jobs" / job_id / "status.json").read_text())
        self.assertEqual(status["state"], "retrying")

    def test_missing_mount_blocks_writes(self):
        job_id = place_job(self.stage)
        self.spool.scan()
        self.assertFalse(self.spool.process_one())
        self.assertFalse((self.usb / "archive" / "test.txt").exists())
        status = json.loads((self.stage / "jobs" / job_id / "status.json").read_text())
        self.assertEqual(status["state"], "waiting_usb")

    def test_empty_directories(self):
        job_id = place_job(self.stage, files=False, dirs=["hello/empty"])
        self.spool.scan()
        with self.mock_mount():
            self.spool.process_one()
        self.assertTrue((self.usb / "archive" / "hello" / "empty").is_dir())
        status = json.loads((self.stage / "jobs" / job_id / "status.json").read_text())
        self.assertEqual(status["state"], "completed")

    def test_rejects_corrupt_manifest(self):
        job_id = place_job(self.stage)
        marker = self.stage / "jobs" / job_id / "READY.json"
        manifest = json.loads(marker.read_text())
        manifest["destination_subdir"] = "../../etc"
        marker.write_text(json.dumps(manifest))
        self.spool.scan()
        self.assertFalse(self.spool.db.execute("SELECT id FROM jobs").fetchone())

    def test_restart_queues_unfinished_copy(self):
        job_id = place_job(self.stage)
        self.spool.scan()
        self.spool.update(job_id, state="copying")
        other = Spooler(config(self.stage, self.usb))
        self.addCleanup(other.close)
        state = other.db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.assertEqual(state, "queued")


if __name__ == "__main__":
    unittest.main()
