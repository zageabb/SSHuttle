"""Unit tests of SSHuttle helpers, without a live server."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sshuttle import human_size, local_listing, manifest_for_upload, read_ssh_target


class FormattingTests(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(1024), "1.0 KB")
        self.assertEqual(human_size(1536), "1.5 KB")


class SSHConfigTests(unittest.TestCase):
    def test_without_config(self):
        with tempfile.TemporaryDirectory() as tmp, patch("sshuttle.Path.home", return_value=Path(tmp)):
            actual = read_ssh_target("192.168.1.50", "gerald", "2222")
        self.assertEqual(actual["hostname"], "192.168.1.50")
        self.assertEqual(actual["username"], "gerald")
        self.assertEqual(actual["port"], 2222)

    def test_config_alias_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            ssh = home / ".ssh"
            ssh.mkdir()
            (ssh / "config").write_text(
                "Host ubuntu-test\n"
                "    HostName 10.0.0.5\n"
                "    User testuser\n"
                "    Port 2200\n"
                "    IdentityFile /tmp/sshuttle-sample-id\n",
                encoding="utf-8",
            )
            with patch("sshuttle.Path.home", return_value=home):
                actual = read_ssh_target("ubuntu-test", "", "")
                override = read_ssh_target("ubuntu-test", "myuser", "22")
        self.assertEqual(actual["hostname"], "10.0.0.5")
        self.assertEqual(actual["username"], "testuser")
        self.assertEqual(actual["port"], 2200)
        self.assertEqual(actual["key_filename"], ["/tmp/sshuttle-sample-id"])
        self.assertEqual(override["username"], "myuser")
        self.assertEqual(override["port"], 22)

    def test_invalid_port(self):
        with tempfile.TemporaryDirectory() as tmp, patch("sshuttle.Path.home", return_value=Path(tmp)):
            with self.assertRaises(ValueError):
                read_ssh_target("host", "", "invalid")
            with self.assertRaises(ValueError):
                read_ssh_target("host", "", "65536")

    def test_proxy_jump_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".ssh").mkdir()
            (home / ".ssh" / "config").write_text("Host target\n  ProxyJump jumphost\n")
            with patch("sshuttle.Path.home", return_value=home):
                with self.assertRaisesRegex(ValueError, "ProxyJump"):
                    read_ssh_target("target", "", "")


class ManifestTests(unittest.TestCase):
    def test_recursive_manifest_preserves_relative_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("hello")
            (root / "sub").mkdir()
            (root / "sub" / "more.bin").write_bytes(b"abcd")
            dirs, files, skipped = manifest_for_upload(root, [root / "sub", root / "notes.txt"])
            self.assertIn("sub", dirs)
            self.assertEqual({f[1] for f in files}, {"notes.txt", "sub/more.bin"})
            self.assertEqual(skipped, 0)

    def test_symlinks_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "actual.txt").write_text("x")
            try:
                (root / "link.txt").symlink_to(root / "actual.txt")
            except (OSError, NotImplementedError):
                self.skipTest("Symlinks unavailable")
            listing = local_listing(root)
            self.assertEqual([item[0] for item in listing], ["actual.txt"])
            _, files, skipped = manifest_for_upload(root, [root / "link.txt"])
            self.assertEqual(files, [])
            self.assertEqual(skipped, 1)


if __name__ == "__main__":
    unittest.main()
