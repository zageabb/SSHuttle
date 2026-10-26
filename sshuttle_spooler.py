"""Ubuntu-side SSHuttle USB Spooler.

Receives completed SFTP uploads under staging/jobs/<uuid>/files, watches for a
READY.json marker, and copies to a configured mounted USB drive. No extra
network listener, SSH keys, remote shell or third-party Python packages.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path

LOG = logging.getLogger("sshuttle.spooler")
HEX_ID = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
READY = "READY.json"
STATUS = "status.json"
CHUNK = 256 * 1024


def relative_path(value: str, allow_empty: bool = False) -> str:
    """Validate POSIX paths from an untrusted uploaded manifest."""
    if not isinstance(value, str) or len(value) > 4096 or "\x00" in value or "\\" in value:
        raise ValueError("Unsafe relative path")
    if value == "" and allow_empty:
        return ""
    if value.startswith("/") or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError(f"Unsafe relative path: {value!r}")
    return value


def config_from_file(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["staging_root"] = Path(cfg["staging_root"]).expanduser().resolve()
    cfg["usb_mount"] = Path(cfg["usb_mount"]).expanduser().resolve()
    if not cfg["staging_root"].is_absolute() or not cfg["usb_mount"].is_absolute():
        raise ValueError("Absolute staging_root and usb_mount paths required")
    if cfg["staging_root"] == cfg["usb_mount"] or cfg["usb_mount"] in cfg["staging_root"].parents:
        raise ValueError("Staging must not be on the USB target")
    cfg["limit_mib"] = float(cfg.get("limit_mib", 2))
    if not 0 < cfg["limit_mib"] <= 100:
        raise ValueError("limit_mib must be between 0 (exclusive) and 100 MiB/s")
    cfg["min_staging_free_gib"] = float(cfg.get("min_staging_free_gib", 2))
    if cfg["min_staging_free_gib"] < 0:
        raise ValueError("min_staging_free_gib cannot be negative")
    cfg["poll_seconds"] = max(2, float(cfg.get("poll_seconds", 5)))
    cfg["max_retries"] = max(1, min(30, int(cfg.get("max_retries", 10))))
    cfg["verify_readback"] = bool(cfg.get("verify_readback", True))
    cfg["expected_usb_uuid"] = str(cfg.get("expected_usb_uuid", "")).strip()
    return cfg


def safe_path(root: Path, relative: str, create_dirs: bool = False) -> Path:
    """Resolve relative name under trusted root, forbidding symlink traversal."""
    relative_path(relative)
    current = root
    for part in relative.split("/")[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Refusing symlink directory {current}")
        if create_dirs:
            current.mkdir(exist_ok=True)
        if not current.is_dir():
            raise NotADirectoryError(str(current))
    target = current / relative.split("/")[-1]
    if target.is_symlink():
        raise ValueError(f"Refusing symlink path {target}")
    return target


def no_replace(source: Path, target: Path):
    """Atomic Linux no-overwrite rename, including filesystems without hardlinks."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        status = renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)
        if status == 0:
            return
        error = ctypes.get_errno()
        if error not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise OSError(error, os.strerror(error), str(target))
    # Filesystem lacks renameat2 support. Safe hard-link fallback where supported;
    # otherwise fail rather than silently overwrite an existing target.
    os.link(source, target)
    source.unlink()


class Spooler:
    def __init__(self, cfg: dict, stop: threading.Event | None = None):
        self.cfg = cfg
        self.root: Path = cfg["staging_root"]
        self.usb: Path = cfg["usb_mount"]
        self.jobs = self.root / "jobs"
        self.jobs.mkdir(parents=True, exist_ok=True)
        self.stop = stop or threading.Event()
        self.db = sqlite3.connect(self.root / "state.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, state TEXT NOT NULL, subdir TEXT NOT NULL,
                overwrite INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                current_file TEXT NOT NULL DEFAULT '', current_bytes INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS entries(
                job TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
                checksum TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                PRIMARY KEY(job, path)
            );
        """)
        # A killed worker never assumes an unfinished copy was successful.
        self.db.execute("UPDATE jobs SET state='queued', current_file='', current_bytes=0 "
                        "WHERE state='copying'")
        self.db.commit()

    def close(self):
        self.db.close()

    def usb_ready(self) -> bool:
        # Never write onto the bare Ubuntu mount-point directory after USB unplug.
        if self.usb.is_symlink() or not self.usb.is_dir() or not os.path.ismount(self.usb):
            return False
        if self.root.stat().st_dev == self.usb.stat().st_dev:
            return False  # Staging belongs on a different filesystem.
        expected = self.cfg["expected_usb_uuid"]
        if expected:
            try:
                result = subprocess.run(
                    ["findmnt", "-n", "-o", "UUID", "--target", str(self.usb)],
                    capture_output=True, text=True, timeout=5, check=True)
                if result.stdout.strip() != expected:
                    return False
            except (OSError, subprocess.SubprocessError):
                return False
        return True

    def manifest(self, job_dir: Path, job_id: str) -> dict:
        marker = job_dir / READY
        if marker.is_symlink() or marker.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Invalid READY.json")
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if payload.get("version") != 1 or payload.get("job_id") != job_id:
            raise ValueError("Incorrect manifest version or job ID")
        subdir = relative_path(payload.get("destination_subdir", ""), allow_empty=True)
        entries = payload.get("files")
        dirs = payload.get("dirs", [])
        if not isinstance(entries, list) or not isinstance(dirs, list) or len(entries) > 50000 or len(dirs) > 50000:
            raise ValueError("Invalid manifest entries")
        for directory in dirs:
            relative_path(directory)
        if len(set(dirs)) != len(dirs):
            raise ValueError("Duplicate directories")
        seen = set()
        for item in entries:
            if not isinstance(item, dict):
                raise ValueError("Invalid file entry")
            name = relative_path(item.get("path"))
            if name in seen:
                raise ValueError("Duplicate file path")
            seen.add(name)
            size = item.get("size")
            digest = item.get("sha256")
            if type(size) is not int or size < 0 or not isinstance(digest, str) or not SHA256.fullmatch(digest):
                raise ValueError(f"Invalid size/checksum: {name}")
            source = safe_path(job_dir / "files", name)
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                raise ValueError(f"Incomplete staged file: {name}")
        return {"subdir": subdir, "files": entries, "dirs": dirs,
                "overwrite": bool(payload.get("overwrite", False))}

    def write_status(self, job_id: str):
        row = self.db.execute("SELECT state,error,current_file,current_bytes FROM jobs WHERE id=?",
                              (job_id,)).fetchone()
        if not row:
            return
        totals = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0), "
            "COALESCE(SUM(CASE WHEN status IN ('done','skipped') THEN size ELSE 0 END),0), "
            "SUM(CASE WHEN status IN ('done','skipped') THEN 1 ELSE 0 END) "
            "FROM entries WHERE job=?", (job_id,)).fetchone()
        data = {"version": 1, "job_id": job_id, "state": row[0], "error": row[1],
                "current_file": row[2], "total_files": totals[0], "completed_files": totals[3] or 0,
                "total_bytes": totals[1], "copied_bytes": totals[2] + (row[3] or 0),
                "updated_at": int(time.time())}
        target = self.jobs / job_id / STATUS
        temp = target.with_name(f".status-{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    def scan(self):
        for entry in self.jobs.iterdir():
            if not entry.is_dir() or entry.is_symlink() or not HEX_ID.fullmatch(entry.name):
                continue
            if not (entry / READY).is_file():
                continue
            if self.db.execute("SELECT 1 FROM jobs WHERE id=?", (entry.name,)).fetchone():
                continue
            try:
                manifest = self.manifest(entry, entry.name)
                with self.db:
                    self.db.execute(
                        "INSERT INTO jobs(id,state,subdir,overwrite) VALUES(?,?,?,?)",
                        (entry.name, "queued", manifest["subdir"], int(manifest["overwrite"])))
                    for item in manifest["files"]:
                        self.db.execute(
                            "INSERT INTO entries(job,path,size,checksum) VALUES(?,?,?,?,?)",
                            (entry.name, item["path"], item["size"], item["sha256"]))
                # Directories are recreated based on manifest during processing;
                # persist them as original READY marker in the staging directory.
                self.write_status(entry.name)
                LOG.info("Queued USB transfer %s (%s files)", entry.name, len(manifest["files"]))
            except Exception as exc:
                # Invalid/incomplete jobs must not stop other queued transfers.
                LOG.error("Rejected spool manifest %s: %s", entry.name, exc)

    def update(self, job_id, *, state=None, error=None, current_file=None, current_bytes=None,
               attempts=None, next_attempt=None):
        updates = {k: v for k, v in {
            "state": state, "error": error, "current_file": current_file,
            "current_bytes": current_bytes, "attempts": attempts, "next_attempt": next_attempt
        }.items() if v is not None}
        if updates:
            assignments = ",".join(f"{key}=?" for key in updates)
            with self.db:
                self.db.execute(f"UPDATE jobs SET {assignments} WHERE id=?",
                                (*updates.values(), job_id))
        self.write_status(job_id)

    def ensure_directory(self, relative: str):
        path = safe_path(self.usb, relative, create_dirs=True)
        if path.is_symlink():
            raise ValueError(f"Refusing symlink folder: {path}")
        path.mkdir(exist_ok=True)
        if not path.is_dir():
            raise NotADirectoryError(str(path))

    def copy_file(self, job_id: str, subdir: str, rel: str, size: int,
                  expected_sha: str, overwrite: bool):
        staged = safe_path(self.jobs / job_id / "files", rel)
        if not staged.is_file() or staged.is_symlink() or staged.stat().st_size != size:
            raise IOError(f"Staging file missing or changed: {rel}")
        relative_dest = f"{subdir}/{rel}" if subdir else rel
        target = safe_path(self.usb, relative_dest, create_dirs=True)
        if target.exists() and not overwrite:
            return "skipped"
        if target.is_dir():
            raise IsADirectoryError(str(target))
        temp = target.with_name(f".{target.name}.sshuttle-{job_id}.part")
        if temp.is_symlink():
            raise ValueError(f"Refusing symlink temp file {temp}")
        # A crash may leave a partial file. Restart that file cleanly.
        if temp.exists():
            temp.unlink()
        rate = self.cfg["limit_mib"] * 1024 * 1024
        hashed = hashlib.sha256()
        moved = 0
        last_status = time.monotonic()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            with staged.open("rb") as source, os.fdopen(os.open(temp, flags, 0o600), "wb") as output:
                while not self.stop.is_set():
                    chunk = source.read(CHUNK)
                    if not chunk:
                        break
                    start = time.monotonic()
                    output.write(chunk)
                    hashed.update(chunk)
                    moved += len(chunk)
                    elapsed = time.monotonic() - start
                    sleep_for = len(chunk) / rate - elapsed
                    if sleep_for > 0 and self.stop.wait(sleep_for):
                        break
                    if time.monotonic() - last_status >= 2:
                        self.update(job_id, current_file=rel, current_bytes=moved)
                        last_status = time.monotonic()
                if self.stop.is_set():
                    raise InterruptedError("Service stopping")
                output.flush()
                os.fsync(output.fileno())
            if moved != size or hashed.hexdigest() != expected_sha:
                raise IOError(f"Staged file checksum mismatch: {rel}")
            if self.cfg["verify_readback"]:
                digest = hashlib.sha256()
                with temp.open("rb") as written:
                    for block in iter(lambda: written.read(1024 * 1024), b""):
                        if self.stop.is_set():
                            raise InterruptedError("Service stopping")
                        digest.update(block)
                if digest.hexdigest() != expected_sha:
                    raise IOError(f"USB verification failed: {rel}")
            if not self.usb_ready():
                raise OSError("USB target no longer mounted")
            if target.is_symlink():
                raise ValueError(f"Refusing symlink destination {target}")
            try:
                if overwrite:
                    os.replace(temp, target)
                else:
                    no_replace(temp, target)
            except FileExistsError:
                return "skipped"
            return "done"
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                LOG.warning("Could not remove temporary file %s", temp)

    def process_one(self) -> bool:
        row = self.db.execute(
            "SELECT id,subdir,overwrite,attempts FROM jobs "
            "WHERE state IN ('queued','retrying','waiting_usb') AND next_attempt<=? "
            "ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
        if not row:
            return False
        job_id, subdir, overwrite, attempts = row
        if not self.usb_ready():
            self.update(job_id, state="waiting_usb", error="USB filesystem missing or incorrect")
            return False
        try:
            job_root = self.jobs / job_id
            manifest = json.loads((job_root / READY).read_text(encoding="utf-8"))
            # Restore even empty directories on the target.
            for directory in manifest.get("dirs", []):
                relative_path(directory)
                name = f"{subdir}/{directory}" if subdir else directory
                self.ensure_directory(name)
            entry = self.db.execute(
                "SELECT path,size,checksum FROM entries WHERE job=? AND status='pending' "
                "ORDER BY rowid LIMIT 1", (job_id,)).fetchone()
            if entry is None:
                self.update(job_id, state="completed", current_file="", current_bytes=0, error="")
                # Remove only the completed job's data, keep READY/status/DB for audit.
                shutil.rmtree(job_root / "files")
                return True
            rel, size, digest = entry
            self.update(job_id, state="copying", error="", current_file=rel, current_bytes=0)
            result = self.copy_file(job_id, subdir, rel, size, digest, bool(overwrite))
            with self.db:
                self.db.execute(
                    "UPDATE entries SET status=? WHERE job=? AND path=?",
                    (result, job_id, rel))
            self.update(job_id, state="queued", current_file="", current_bytes=0,
                        attempts=0, error="")
        except InterruptedError:
            self.update(job_id, state="queued", current_file="", current_bytes=0)
            return False
        except Exception as exc:
            attempts += 1
            failed = attempts >= self.cfg["max_retries"]
            delay = min(30 * 2 ** min(attempts - 1, 4), 600)
            self.update(job_id, state="failed" if failed else "retrying",
                        error=f"{type(exc).__name__}: {exc}", current_file="", current_bytes=0,
                        attempts=attempts, next_attempt=time.time() + delay)
            LOG.exception("Job %s error, attempt %d", job_id, attempts)
        return True

    def run(self, once: bool = False):
        while not self.stop.is_set():
            self.scan()
            worked = self.process_one()
            if once:
                break
            if not worked:
                self.stop.wait(self.cfg["poll_seconds"])


def main():
    parser = argparse.ArgumentParser(description="SSHuttle USB background spooler")
    parser.add_argument("--config", required=True, type=Path, help="JSON config path")
    parser.add_argument("--once", action="store_true", help="Scan and process one queued file")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = config_from_file(args.config)
    stop = threading.Event()
    service = Spooler(config, stop)
    try:
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        service.run(args.once)
    finally:
        service.close()


if __name__ == "__main__":
    main()
