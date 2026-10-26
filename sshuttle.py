"""SSHuttle - small local-to-SSH file transfer desktop utility.

Run: python3 sshuttle.py
Requires: paramiko (see requirements.txt).  Uses Tkinter from Python.
"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import posixpath
import queue
import socket
import stat
import threading
import time
import uuid
from pathlib import Path
from tkinter import END, BOTH, LEFT, RIGHT, Y, X, BooleanVar, StringVar, Tk, filedialog, messagebox, ttk
import tkinter as tk

import paramiko


class TransferCancelled(Exception):
    """Raised to stop an in-flight SFTP transfer."""


def human_size(size: int) -> str:
    number = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB":
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} TB"


def read_ssh_target(alias: str, explicit_user: str, explicit_port: str) -> dict:
    """Resolve an OpenSSH Host alias, with UI values taking precedence.

    SSH config IdentityFile and HostName are supported. ProxyJump/ProxyCommand
    are deliberately rejected: Paramiko cannot automatically honour them.
    """
    alias = alias.strip()
    if not alias:
        raise ValueError("Enter a server hostname or an alias from ~/.ssh/config.")
    config_path = Path.home() / ".ssh" / "config"
    config = {}
    if config_path.is_file():
        ssh_config = paramiko.SSHConfig()
        with config_path.open(encoding="utf-8") as handle:
            ssh_config.parse(handle)
        config = ssh_config.lookup(alias)
    if config.get("proxyjump") or config.get("proxycommand"):
        raise ValueError("ProxyJump/ProxyCommand SSH aliases are not yet supported. Use a direct SSH host.")
    port_text = explicit_port.strip() or str(config.get("port", 22))
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("SSH port must be a number from 1 to 65535.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SSH port must be between 1 and 65535.")
    key_files = [
        os.path.expandvars(os.path.expanduser(item))
        for item in config.get("identityfile", [])
    ]
    return {
        "hostname": config.get("hostname", alias),
        "username": explicit_user.strip() or config.get("user") or os.environ.get("USER") or os.environ.get("USERNAME"),
        "port": port,
        "key_filename": key_files or None,
    }


def local_listing(folder: Path):
    """Return (name, is_dir, size, path) entries, sorted folders first."""
    entries = []
    for child in folder.iterdir():
        try:
            details = child.lstat()
        except OSError:
            continue
        # Do not browse or upload symbolic links; avoids loops and surprises.
        if stat.S_ISLNK(details.st_mode):
            continue
        is_dir = stat.S_ISDIR(details.st_mode)
        entries.append((child.name, is_dir, 0 if is_dir else details.st_size, child))
    return sorted(entries, key=lambda value: (not value[1], value[0].casefold()))


def manifest_for_upload(local_root: Path, selected_paths: list[Path]):
    """Enumerate regular files and directories, keeping paths relative to local_root.

    Returns (directories, [(local_path, relative_posix_path, size)], skipped_links).
    """
    root = local_root.resolve()
    dirs: set[str] = set()
    files: list[tuple[Path, str, int]] = []
    skipped = 0
    for selected in selected_paths:
        # Ensure callers cannot submit a path outside the displayed root.
        relative = selected.relative_to(root)
        if any(part == ".." for part in relative.parts):
            raise ValueError("Selected file must be inside the local folder.")
        if selected.is_symlink():
            skipped += 1
            continue
        if selected.is_file():
            files.append((selected, relative.as_posix(), selected.stat().st_size))
        elif selected.is_dir():
            for dir_path, child_dirs, file_names in os.walk(selected, followlinks=False):
                base = Path(dir_path)
                child_dirs[:] = [name for name in child_dirs if not (base / name).is_symlink()]
                dirs.add(base.relative_to(root).as_posix())
                for filename in file_names:
                    item = base / filename
                    if item.is_symlink() or not item.is_file():
                        skipped += 1
                        continue
                    files.append((item, item.relative_to(root).as_posix(), item.stat().st_size))
    return sorted(dirs, key=lambda d: (d.count("/"), d)), files, skipped



def manifest_for_download(sftp, remote_root: str, selected_paths: list[str]):
    """List regular files below selected remote paths without following symlinks.

    Return (relative_directories, [(absolute_remote, relative, size)], skipped).
    """
    root = posixpath.normpath(remote_root)
    if not root.startswith("/"):
        raise ValueError("Remote source folder must be an absolute path.")
    dirs: set[str] = set()
    files: list[tuple[str, str, int]] = []
    skipped = 0
    stack = list(selected_paths)
    while stack:
        remote = posixpath.normpath(stack.pop())
        relative = posixpath.relpath(remote, root)
        if relative == ".." or relative.startswith("../") or relative in (".", ""):
            raise ValueError("Selected remote item must be inside the remote folder.")
        # Check every relative component: remote names may be untrusted.
        if any(part in ("", ".", "..") or chr(92) in part or "\x00" in part
               for part in relative.split("/")):
            raise ValueError(f"Unsafe remote filename: {relative!r}")
        info = sftp.lstat(remote)  # lstat, not stat: do not follow remote links
        mode = info.st_mode or 0
        if stat.S_ISLNK(mode):
            skipped += 1
        elif stat.S_ISDIR(mode):
            dirs.add(relative)
            for item in sftp.listdir_attr(remote):
                name = item.filename
                if name in ("", ".", "..") or "/" in name or chr(92) in name or "\x00" in name:
                    raise ValueError(f"Unsafe remote directory entry: {name!r}")
                stack.append(posixpath.join(remote, name))
        elif stat.S_ISREG(mode):
            files.append((remote, relative, info.st_size or 0))
        else:
            skipped += 1
    return sorted(dirs, key=lambda d: (d.count("/"), d)), sorted(files, key=lambda f: f[1]), skipped


def safe_local_target(root: Path, relative: str, is_directory: bool = False) -> Path:
    """Create necessary local parent folders, rejecting escapes and symbolic links.

    Must be called by the transfer worker only. Caller supplies a resolved root.
    """
    parts = relative.split("/")
    if any(part in ("", ".", "..") or chr(92) in part or "\x00" in part for part in parts):
        raise ValueError(f"Unsafe local destination relative path: {relative!r}")
    if not root.is_dir() or root.is_symlink():
        raise NotADirectoryError(f"Local destination is unavailable: {root}")
    current = root
    for part in parts[:-1] if not is_directory else parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Refusing symbolic link folder: {current}")
        if current.exists():
            if not current.is_dir():
                raise NotADirectoryError(f"Not a directory: {current}")
        else:
            current.mkdir()
    destination = current if is_directory else current / parts[-1]
    if destination.is_symlink():
        raise ValueError(f"Refusing symbolic link destination: {destination}")
    return destination



def parse_connection_settings(timeout_text: str, retries_text: str) -> tuple[int, int]:
    """User-configurable connection limits, with sensible bounds."""
    try:
        timeout = int(timeout_text)
        retries = int(retries_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("Connection timeout and retries must be whole numbers.") from exc
    if not 15 <= timeout <= 300:
        raise ValueError("Connection timeout must be between 15 and 300 seconds.")
    if not 0 <= retries <= 5:
        raise ValueError("Retries must be between 0 and 5.")
    return timeout, retries


def is_temporary_connection_error(exc: BaseException) -> bool:
    """Only transient transport failures are safe to retry automatically."""
    if isinstance(exc, (paramiko.AuthenticationException, paramiko.BadHostKeyException)):
        return False
    if isinstance(exc, (socket.timeout, TimeoutError, ConnectionError, EOFError)):
        return True
    if isinstance(exc, paramiko.ssh_exception.NoValidConnectionsError):
        return True
    if isinstance(exc, OSError):
        return exc.errno in (
            errno.ECONNRESET, errno.ECONNREFUSED, errno.ECONNABORTED,
            errno.EPIPE, errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETUNREACH,
        )
    if isinstance(exc, paramiko.SSHException):
        reason = str(exc).lower()
        return any(word in reason for word in (
            "timed out", "timeout", "connection reset", "connection lost",
            "connection closed", "socket", "eof", "banner", "session not active",
            "no existing session",
        ))
    return False



def parse_upload_limit(value: str) -> float:
    """Limit for USB-friendly uploads in MiB/s; zero disables pacing."""
    try:
        limit = float(value.strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("USB upload limit must be a number in MiB/s.") from exc
    if not 0 <= limit <= 100 or not math.isfinite(limit):
        raise ValueError("USB upload limit must be between 0 and 100 MiB/s.")
    return limit


def upload_with_backpressure(sftp, source: Path, temporary: str, size: int,
                             progress, cancel_event: threading.Event,
                             limit_mib: float, chunk_size: int = 32768):
    """Stream bounded writes; wait for each SFTP reply before sending the next.

    Paramiko's sftp.put() enables pipelining, which can flood a slow target.
    This method explicitly disables pipelining and optionally paces writes.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if not 0 <= limit_mib <= 100:
        raise ValueError("Invalid USB upload speed")
    transferred = 0
    rate = limit_mib * 1024 * 1024
    with Path(source).open("rb") as source_file:
        with sftp.file(temporary, "wb", bufsize=0) as remote_file:
            remote_file.set_pipelined(False)
            while True:
                if cancel_event.is_set():
                    raise TransferCancelled()
                block = source_file.read(chunk_size)
                if not block:
                    break
                before = time.monotonic()
                remote_file.write(block)  # synchronous SFTP ACK, no queued writes
                transferred += len(block)
                progress(transferred, size)
                if rate:
                    pause = len(block) / rate - (time.monotonic() - before)
                    if pause > 0 and cancel_event.wait(pause):
                        raise TransferCancelled()
            if cancel_event.is_set():
                raise TransferCancelled()
    if transferred != size:
        raise IOError(f"Local source changed while uploading ({transferred} vs {size} bytes).")
    # Match put(confirm=True): a successful close alone is not size validation.
    attrs = sftp.stat(temporary)
    if attrs.st_size != size:
        raise IOError(f"Remote file size mismatch ({attrs.st_size} vs {size} bytes).")


def spool_subdirectory(name: str) -> str:
    value = name.strip()
    if value and (len(value) > 4096 or value.startswith("/") or "\\" in value
                  or "\x00" in value or any(p in ("", ".", "..") for p in value.split("/"))):
        raise ValueError("USB destination must be a relative folder without '..'.")
    return value


def make_spool_manifest(local_root, selected, job_id, dest_subdir, overwrite):
    dirs, files, skipped = manifest_for_upload(local_root, selected)
    result = {"version": 1, "job_id": job_id, "destination_subdir": spool_subdirectory(dest_subdir),
              "overwrite": bool(overwrite), "dirs": dirs, "files": []}
    for source, rel, size in files:
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for data in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(data)
        if source.stat().st_size != size:
            raise ValueError(f"Source changed while uploading: {rel}")
        result["files"].append({"path": rel, "size": size, "sha256": digest.hexdigest()})
    return result


    def __init__(self, root: Tk):
        self.root = root
        self.root.title("SSHuttle - SSH File Transfer")
        self.root.geometry("1060x680")
        self.root.minsize(780, 510)

        self.host = StringVar()
        self.username = StringVar()
        self.port = StringVar()
        self.password = StringVar()
        self.connect_timeout = StringVar(value="60")
        self.retry_count = StringVar(value="3")
        self.usb_safe_mode = BooleanVar(value=False)
        self.usb_rate_limit = StringVar(value="2")
        self.use_spooler = BooleanVar(value=False)
        self.spooler_root = StringVar(value="/srv/sshuttle/staging")
        self.usb_subfolder = StringVar()
        self.spool_status = StringVar(value="USB Spooler: no queued jobs")
        self._spool_jobs = {}
        self.local_path = StringVar(value=str(Path.home()))
        self.remote_path = StringVar(value="")
        self.overwrite = BooleanVar(value=False)
        self.connection_status = StringVar(value="Disconnected")
        self.transfer_status = StringVar(value="Choose files or folders on either side to transfer.")
        self.progress_label = StringVar(value="0%")
        self._connected = False
        self._busy = False
        self._cancel_transfer = threading.Event()
        self._work: queue.Queue = queue.Queue()
        self._events: queue.Queue = queue.Queue()
        self._local_items: dict[str, Path] = {}
        self._remote_items: dict[str, tuple[str, bool]] = {}
        self._remote_listed_path = ""
        self._local_listed_path = Path.home()

        self._build_ui()
        self._show_local(Path.home())
        self._thread = threading.Thread(target=self._worker, daemon=True, name="sshuttle-sftp")
        self._thread.start()
        self.root.after(80, self._drain_events)
        self.root.after(5000, self._poll_spool_status)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=BOTH, expand=True)

        credentials = ttk.LabelFrame(outer, text="SSH connection", padding=10)
        credentials.pack(fill=X)
        ttk.Label(credentials, text="Server / SSH alias").grid(row=0, column=0, sticky="w")
        ttk.Entry(credentials, textvariable=self.host, width=26).grid(row=1, column=0, padx=(0, 8), sticky="ew")
        ttk.Label(credentials, text="User (optional)").grid(row=0, column=1, sticky="w")
        ttk.Entry(credentials, textvariable=self.username, width=18).grid(row=1, column=1, padx=(0, 8), sticky="ew")
        ttk.Label(credentials, text="Port (optional)").grid(row=0, column=2, sticky="w")
        ttk.Entry(credentials, textvariable=self.port, width=9).grid(row=1, column=2, padx=(0, 8), sticky="ew")
        ttk.Label(credentials, text="Password / key passphrase (optional)").grid(row=0, column=3, sticky="w")
        ttk.Entry(credentials, textvariable=self.password, show="*", width=24).grid(row=1, column=3, padx=(0, 8), sticky="ew")
        self.connect_button = ttk.Button(credentials, text="Connect", command=self._connect)
        self.connect_button.grid(row=1, column=4, padx=(0, 8))
        ttk.Label(credentials, textvariable=self.connection_status).grid(row=1, column=5, sticky="w")
        ttk.Label(credentials, text="Timeout per connection (seconds)").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(credentials, textvariable=self.connect_timeout, width=7).grid(row=3, column=0, sticky="w")
        ttk.Label(credentials, text="Retries after connection failures").grid(row=2, column=1, sticky="w", pady=(8, 0))
        ttk.Entry(credentials, textvariable=self.retry_count, width=7).grid(row=3, column=1, sticky="w")
        ttk.Checkbutton(credentials, text="USB-safe uploads", variable=self.usb_safe_mode).grid(
            row=3, column=2, sticky="w")
        ttk.Label(credentials, text="USB speed limit (MiB/s, 0 = uncapped)").grid(
            row=2, column=3, sticky="w", pady=(8, 0))
        ttk.Entry(credentials, textvariable=self.usb_rate_limit, width=8).grid(
            row=3, column=3, sticky="w")
        ttk.Checkbutton(credentials, text="Send via USB Spooler", variable=self.use_spooler).grid(
            row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Label(credentials, text="Ubuntu staging folder").grid(row=4, column=1, sticky="w")
        ttk.Entry(credentials, textvariable=self.spooler_root).grid(row=5, column=1, sticky="ew")
        ttk.Label(credentials, text="USB destination subfolder").grid(row=4, column=2, sticky="w")
        ttk.Entry(credentials, textvariable=self.usb_subfolder).grid(
            row=5, column=2, columnspan=2, sticky="ew")
        credentials.columnconfigure(0, weight=2)
        credentials.columnconfigure(1, weight=1)
        credentials.columnconfigure(3, weight=1)

        browsers = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        browsers.pack(fill=BOTH, expand=True, pady=(12, 8))

        left = ttk.LabelFrame(browsers, text="Local machine", padding=8)
        right = ttk.LabelFrame(browsers, text="Ubuntu / remote server", padding=8)
        browsers.add(left, weight=1)
        browsers.add(right, weight=1)

        self.local_tree = self._make_browser(left, self.local_path, self._local_go, self._local_up, self._local_choose)
        self.remote_tree = self._make_browser(right, self.remote_path, self._remote_go, self._remote_up, None)
        self.local_tree.bind("<Double-1>", self._open_local_item)
        self.remote_tree.bind("<Double-1>", self._open_remote_item)

        controls = ttk.Frame(outer)
        controls.pack(fill=X, pady=(2, 8))
        self.upload_button = ttk.Button(controls, text="Upload selected  →", command=self._upload)
        self.upload_button.pack(side=LEFT)
        self.download_button = ttk.Button(controls, text="← Download selected", command=self._download)
        self.download_button.pack(side=LEFT, padx=(8, 0))
        self.cancel_button = ttk.Button(controls, text="Cancel transfer", command=self._cancel, state="disabled")
        self.cancel_button.pack(side=LEFT, padx=8)
        ttk.Checkbutton(controls, text="Replace existing destination files", variable=self.overwrite).pack(side=LEFT, padx=8)
        ttk.Button(controls, text="Refresh", command=self._refresh).pack(side=RIGHT)

        status_bar = ttk.Frame(outer)
        status_bar.pack(fill=X, pady=(0, 7))
        ttk.Label(status_bar, textvariable=self.spool_status).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(status_bar, text="Check USB queue", command=self._poll_spool_status).pack(side=RIGHT)
        progress_frame = ttk.Frame(outer)
        progress_frame.pack(fill=X)
        ttk.Label(progress_frame, textvariable=self.transfer_status).pack(anchor="w")
        progress_row = ttk.Frame(progress_frame)
        progress_row.pack(fill=X, pady=4)
        self.progress_bar = ttk.Progressbar(progress_row, mode="determinate", maximum=100)
        self.progress_bar.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(progress_row, textvariable=self.progress_label, width=8, anchor="e").pack(side=RIGHT)

        log_frame = ttk.LabelFrame(outer, text="Activity", padding=6)
        log_frame.pack(fill=BOTH, expand=False)
        self.log = tk.Text(log_frame, height=5, state="disabled", wrap="word")
        self.log.pack(fill=BOTH, expand=True)
        self._log("SSH authentication: ~/.ssh/config, SSH agent and local keys are tried automatically.")

    @staticmethod
    def _make_browser(parent, pathvar, go, up, choose):
        bar = ttk.Frame(parent)
        bar.pack(fill=X, pady=(0, 6))
        ttk.Button(bar, text="↑", width=3, command=up).pack(side=LEFT)
        ttk.Entry(bar, textvariable=pathvar).pack(side=LEFT, fill=X, expand=True, padx=5)
        ttk.Button(bar, text="Go", width=4, command=go).pack(side=LEFT)
        if choose:
            ttk.Button(bar, text="…", width=3, command=choose).pack(side=LEFT, padx=(5, 0))
        frame = ttk.Frame(parent)
        frame.pack(fill=BOTH, expand=True)
        tree = ttk.Treeview(frame, columns=("size",), selectmode="extended", show="tree headings")
        tree.heading("#0", text="Name")
        tree.column("#0", width=300, minwidth=130)
        tree.heading("size", text="Size")
        tree.column("size", width=90, stretch=False, anchor="e")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=LEFT, fill=BOTH, expand=True)
        scroll.pack(side=RIGHT, fill=Y)
        return tree

    def _show_local(self, path: Path):
        try:
            path = path.expanduser().resolve(strict=True)
            if not path.is_dir():
                raise NotADirectoryError(str(path))
            entries = local_listing(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Local folder", str(exc))
            return
        self.local_path.set(str(path))
        self._local_listed_path = path
        self.local_tree.delete(*self.local_tree.get_children())
        self._local_items.clear()
        for name, is_dir, size, child in entries:
            item_id = self.local_tree.insert("", END, text=("📁 " if is_dir else "📄 ") + name,
                                             values=("" if is_dir else human_size(size),))
            self._local_items[item_id] = child

    def _local_choose(self):
        folder = filedialog.askdirectory(initialdir=self.local_path.get(), mustexist=True)
        if folder:
            self._show_local(Path(folder))

    def _local_go(self):
        self._show_local(Path(self.local_path.get()))

    def _local_up(self):
        self._show_local(Path(self.local_path.get()).expanduser().parent)

    def _open_local_item(self, event):
        row = self.local_tree.identify_row(event.y)
        item = self._local_items.get(row)
        if item and item.is_dir():
            self._show_local(item)

    def _remote_go(self):
        if self._connected and not self._busy:
            self._work.put(("list", self.remote_path.get().strip() or "."))

    def _remote_up(self):
        path = self.remote_path.get().rstrip("/") or "/"
        self.remote_path.set(posixpath.dirname(path) or "/")
        self._remote_go()

    def _open_remote_item(self, event):
        row = self.remote_tree.identify_row(event.y)
        entry = self._remote_items.get(row)
        if entry and entry[1] and not self._busy:
            self.remote_path.set(entry[0])
            self._remote_go()

    def _connect(self):
        if self._busy:
            return
        if self._connected:
            self._work.put(("disconnect", None))
            self.connection_status.set("Disconnecting…")
            self.connect_button.configure(state="disabled")
            return
        try:
            target = read_ssh_target(self.host.get(), self.username.get(), self.port.get())
        except (ValueError, OSError) as exc:
            messagebox.showerror("SSH connection", str(exc))
            return
        self.connection_status.set("Connecting…")
        self.connect_button.configure(state="disabled")
        # Credentials are sent only to the worker, never saved to a file.
        try:
            timeout, retries = parse_connection_settings(
                self.connect_timeout.get(), self.retry_count.get())
        except ValueError as exc:
            self.connection_status.set("Disconnected")
            self.connect_button.configure(state="normal")
            messagebox.showerror("Connection settings", str(exc))
            return
        self._cancel_transfer.clear()
        self._work.put(("connect", (target, self.password.get(), timeout, retries)))

    def _upload(self):
        if not self._connected or self._busy:
            messagebox.showinfo("SSHuttle", "Connect to the server first, and finish any current transfer.")
            return
        selected = [self._local_items[item] for item in self.local_tree.selection() if item in self._local_items]
        if not selected:
            messagebox.showinfo("SSHuttle", "Select one or more files or folders in the left panel.")
            return
        remote_dir = self.remote_path.get().strip()
        if not remote_dir.startswith("/"):
            messagebox.showerror("Remote folder", "Select an absolute remote folder before uploading.")
            return
        safe_mode = self.usb_safe_mode.get()
        try:
            rate = parse_upload_limit(self.usb_rate_limit.get()) if safe_mode else 0.0
        except ValueError as exc:
            messagebox.showerror("USB upload limit", str(exc))
            return
        if self.use_spooler.get():
            try:
                subfolder = spool_subdirectory(self.usb_subfolder.get())
                stage = self.spooler_root.get().strip().rstrip("/")
                if not stage.startswith("/") or stage in ("/", "/tmp", "/var/tmp"):
                    raise ValueError("Enter an absolute dedicated Ubuntu staging directory.")
            except ValueError as exc:
                messagebox.showerror("USB Spooler", str(exc))
                return
            self._cancel_transfer.clear()
            self._set_busy(True)
            self._work.put(("spool_upload", (
                self._local_listed_path, selected, stage,
                subfolder, self.overwrite.get(), uuid.uuid4().hex)))
            return
        self._cancel_transfer.clear()
        self._set_busy(True)
        self._work.put(("upload", (
            self._local_listed_path, selected, remote_dir, self.overwrite.get(), safe_mode, rate)))

    def _download(self):
        if not self._connected or self._busy:
            messagebox.showinfo("SSHuttle", "Connect to the server first and finish any current transfer.")
            return
        selected = [self._remote_items[item][0] for item in self.remote_tree.selection()
                    if item in self._remote_items]
        if not selected:
            messagebox.showinfo("SSHuttle", "Select one or more files or folders in the right panel.")
            return
        local_dir = self._local_listed_path
        if not local_dir.is_dir():
            messagebox.showerror("Local folder", "Choose a valid local destination folder.")
            return
        self._cancel_transfer.clear()
        self._set_busy(True)
        self._work.put(("download", (self._remote_listed_path, selected,
                                     local_dir, self.overwrite.get())))

    def _poll_spool_status(self):
        if self._connected and not self._busy:
            for job_id, stage in list(self._spool_jobs.items()):
                self._work.put(("spool_status", (stage, job_id)))
        if self.root.winfo_exists():
            self.root.after(5000, self._poll_spool_status)

    def _cancel(self):
        self._cancel_transfer.set()
        self.transfer_status.set("Cancelling current file…")

    def _refresh(self):
        self._show_local(Path(self.local_path.get()))
        self._remote_go()

    def _log(self, message):
        self.log.configure(state="normal")
        self.log.insert(END, message + "\n")
        self.log.see(END)
        self.log.configure(state="disabled")

    def _set_busy(self, value):
        self._busy = value
        self.upload_button.configure(state="disabled" if value else "normal")
        self.download_button.configure(state="disabled" if value else "normal")
        self.cancel_button.configure(state="normal" if value else "disabled")
        self.connect_button.configure(state="disabled" if value else "normal")

    def _drain_events(self):
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "retry":
                    operation, next_attempt, max_attempts, reason = payload
                    self.connection_status.set("Reconnecting…")
                    self.transfer_status.set(
                        f"{operation}: temporary SSH problem; retry {next_attempt} of {max_attempts}…")
                    self._log(f"{operation}: retrying after {reason}")
                elif kind == "recovered":
                    self.connection_status.set("Connected")
                    self._log("SSH connection restored.")
                elif kind == "spool_queued":
                    stage, job_id = payload
                    self._spool_jobs[job_id] = stage
                    self.spool_status.set(f"USB Spooler job {job_id[:8]} queued")
                    self._log(f"Staged job {job_id} for Ubuntu USB service")
                elif kind == "spool_status":
                    job_id, status = payload
                    self.spool_status.set(
                        f"USB {job_id[:8]}: {status.get('state', 'pending')} "
                        f"{status.get('completed_files', 0)}/{status.get('total_files', 0)} files · "
                        f"{human_size(status.get('copied_bytes', 0))}/"
                        f"{human_size(status.get('total_bytes', 0))}"
                        + (f" · {status['error']}" if status.get("error") else ""))
                    if status.get("state") == "completed":
                        self._spool_jobs.pop(job_id, None)
                elif kind == "connected":
                    self._connected = True
                    self.password.set("")  # Do not retain credentials in the UI.
                    self.connect_button.configure(text="Disconnect", state="normal")
                    self.connection_status.set("Connected")
                    self.remote_path.set(payload)
                    self._log(f"Connected. Remote home: {payload}")
                    self._work.put(("list", payload))
                elif kind == "disconnected":
                    self._connected = False
                    self.connect_button.configure(text="Connect", state="normal")
                    self.connection_status.set("Disconnected")
                    self.remote_tree.delete(*self.remote_tree.get_children())
                    self._remote_items.clear()
                    self._remote_listed_path = ""
                    self._log("Disconnected.")
                elif kind == "connect_error":
                    self.connection_status.set("Connection failed")
                    self.connect_button.configure(state="normal", text="Connect")
                    self.password.set("")
                    self._log(f"Connection failed: {payload}")
                    messagebox.showerror("SSH connection", str(payload))
                elif kind == "remote_list":
                    path, entries = payload
                    self._remote_listed_path = path
                    self.remote_path.set(path)
                    self.remote_tree.delete(*self.remote_tree.get_children())
                    self._remote_items.clear()
                    for name, is_dir, size, remote_name in entries:
                        item_id = self.remote_tree.insert("", END,
                            text=("📁 " if is_dir else "📄 ") + name,
                            values=("" if is_dir else human_size(size),))
                        self._remote_items[item_id] = (remote_name, is_dir)
                elif kind == "started":
                    direction, total_files, total_bytes = payload
                    self.progress_bar.configure(value=0)
                    self.progress_label.set("0%")
                    self.transfer_status.set(f"{direction}ing {total_files} file(s), {human_size(total_bytes)}")
                elif kind == "progress":
                    done, total, current_file = payload
                    percent = 100 if total == 0 else min(100, round(done / total * 100))
                    self.progress_bar.configure(value=percent)
                    self.progress_label.set(f"{percent}%")
                    self.transfer_status.set(f"{current_file} · {human_size(done)} / {human_size(total)}")
                elif kind == "finished":
                    direction, completed, skipped, total = payload
                    self._set_busy(False)
                    if not self._cancel_transfer.is_set():
                        self.progress_bar.configure(value=100)
                        self.progress_label.set("100%")
                    self.transfer_status.set(f"Complete: {completed} {direction.lower()}ed, {skipped} skipped, {total} considered.")
                    self._log(self.transfer_status.get())
                    if direction == "Upload":
                        self._work.put(("list", self._remote_listed_path))
                    else:
                        self._show_local(self._local_listed_path)
                elif kind == "cancelled":
                    self._set_busy(False)
                    self.transfer_status.set(f"{payload} cancelled. Completed files remain at the destination.")
                    self._log(self.transfer_status.get())
                    self._refresh()
                elif kind == "error":
                    self.connection_status.set("Disconnected" if not self._connected else "Connected")
                    self._set_busy(False)
                    self.transfer_status.set(f"Error: {payload}")
                    self._log(f"Error: {payload}")
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(80, self._drain_events)

    @staticmethod
    def _open_connection(target, password, timeout):
        """Open a verified SSH connection and give SFTP more time for slow storage."""
        client = paramiko.SSHClient()
        try:
            client.load_system_host_keys()
            known_hosts = Path.home() / ".ssh" / "known_hosts"
            if known_hosts.exists():
                client.load_host_keys(str(known_hosts))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(
                **target,
                password=password or None,
                passphrase=password or None,
                look_for_keys=True, allow_agent=True,
                timeout=timeout,
                auth_timeout=max(90, timeout),
                banner_timeout=max(90, timeout),
            )
            transport = client.get_transport()
            if transport is None or not transport.is_active():
                raise ConnectionError("SSH transport closed during connection.")
            transport.set_keepalive(30)
            sftp = client.open_sftp()
            # Timeout here is inactivity between SFTP responses, not a limit
            # on total duration of a large file transfer.
            sftp.get_channel().settimeout(max(120, 2 * timeout))
            return client, sftp
        except BaseException:
            client.close()
            raise

    def _worker(self):
        client = None
        sftp = None
        # Keep credentials only in worker memory while the user is connected.
        # They are required for reconnecting after an interrupted transfer.
        session = None

        def close_connection():
            nonlocal client, sftp
            if sftp is not None:
                try:
                    sftp.close()
                except OSError:
                    pass
                sftp = None
            if client is not None:
                try:
                    client.close()
                finally:
                    client = None

        def run_with_recovery(operation, callback, is_transfer=False):
            nonlocal client, sftp
            if session is None:
                raise ConnectionError("Not connected.")
            target, password, timeout, retries = session
            for failed_attempt in range(retries + 1):
                if is_transfer and self._cancel_transfer.is_set():
                    raise TransferCancelled()
                try:
                    if sftp is None:
                        client, sftp = self._open_connection(target, password, timeout)
                        if failed_attempt:
                            self._events.put(("recovered", None))
                    return callback(sftp)
                except TransferCancelled:
                    raise
                except Exception as exc:
                    transient = is_temporary_connection_error(exc)
                    if transient:
                        close_connection()
                    if not transient or failed_attempt >= retries:
                        raise
                    retry_number = failed_attempt + 1
                    self._events.put(("retry", (
                        operation, retry_number, retries, str(exc))))
                    # Retrying from the start of the affected operation:
                    # completed uploads/downloads remain committed; incomplete
                    # temporary files are discarded where possible.
                    # Allow a USB drive extra time to recover or remount after a
                    # transient storage stall; normal SSH retains short retries.
                    usb_mode = action == "upload" and len(data) >= 5 and data[4]
                    delay = min(5 * (2 ** failed_attempt), 30) if usb_mode else min(2 ** failed_attempt, 8)
                    if is_transfer:
                        if self._cancel_transfer.wait(delay):
                            raise TransferCancelled()
                    else:
                        time.sleep(delay)
            raise RuntimeError("Unreachable retry state")

        try:
            while True:
                action, data = self._work.get()
                if action == "exit":
                    break
                if action == "disconnect":
                    close_connection()
                    session = None
                    self._events.put(("disconnected", None))
                elif action == "connect":
                    close_connection()
                    session = data
                    try:
                        home = run_with_recovery("Connect", lambda conn: conn.normalize("."))
                        self._events.put(("connected", home))
                    except Exception as exc:
                        close_connection()
                        session = None
                        self._events.put(("connect_error", exc))
                elif action == "list":
                    try:
                        def browse(conn):
                            path = conn.normalize(data)
                            attributes = conn.listdir_attr(path)
                            entries = []
                            for attr in attributes:
                                if attr.filename in (".", ".."):
                                    continue
                                mode = attr.st_mode or 0
                                is_dir = stat.S_ISDIR(mode)
                                if not (is_dir or stat.S_ISREG(mode)):
                                    continue
                                entries.append((
                                    attr.filename, is_dir, attr.st_size or 0,
                                    posixpath.join(path, attr.filename),
                                ))
                            entries.sort(key=lambda item: (not item[1], item[0].casefold()))
                            return path, entries

                        result = run_with_recovery("Browse", browse)
                        self._events.put(("remote_list", result))
                    except Exception as exc:
                        if sftp is None:
                            self._events.put(("disconnected", None))
                        self._events.put(("error", f"Cannot browse remote folder: {exc}"))
                elif action == "spool_status":
                    try:
                        stage, job_id = data
                        if sftp is None:
                            continue
                        with sftp.open(posixpath.join(stage, "jobs", job_id, "status.json"), "r") as handle:
                            raw = handle.read()
                            status = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
                        if status.get("job_id") == job_id:
                            self._events.put(("spool_status", (job_id, status)))
                    except (OSError, ValueError, UnicodeError):
                        pass
                elif action in ("upload", "download", "spool_upload"):
                    try:
                        def transfer(conn):
                            if action == "upload":
                                return self._send_files(conn, *data)
                            if action == "spool_upload":
                                return self._upload_to_spooler(conn, *data)
                            return self._receive_files(conn, *data)

                        run_with_recovery(action.capitalize(), transfer, is_transfer=True)
                    except TransferCancelled:
                        self._events.put(("cancelled", action.capitalize()))
                    except Exception as exc:
                        if sftp is None:
                            self._events.put(("disconnected", None))
                        self._events.put(("error", f"{action.capitalize()} stopped after retries: {exc}"))
        finally:
            close_connection()

    @staticmethod
    def _mkdir_remote(sftp, folder: str):
        """Create a remote folder (and parents), without relying on a shell."""
        folder = posixpath.normpath(folder)
        current = "/" if folder.startswith("/") else "."
        for part in folder.split("/"):
            if not part or part == ".":
                continue
            current = posixpath.join(current, part)
            try:
                info = sftp.stat(current)
                if not stat.S_ISDIR(info.st_mode):
                    raise NotADirectoryError(f"Remote target is not a folder: {current}")
            except OSError as exc:
                if getattr(exc, "errno", None) != errno.ENOENT:
                    raise
                sftp.mkdir(current)

    @staticmethod
    def _remote_exists(sftp, path: str) -> bool:
        try:
            sftp.stat(path)
            return True
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.ENOENT:
                return False
            raise

    def _send_files(self, sftp, local_root, selected_paths, remote_root, overwrite, usb_safe=False, usb_rate=2.0, emit_finished=True):
        dirs, files, skipped_links = manifest_for_upload(local_root, selected_paths)
        total = sum(size for _, _, size in files)
        self._events.put(("started", ("Upload", len(files), total)))
        if usb_safe:
            speed = "uncapped" if usb_rate == 0 else f"{usb_rate:g} MiB/s"
            self._events.put(("log", f"USB-safe mode: sequential 32 KiB writes, {speed}; slower retry backoff."))
        if skipped_links:
            self._events.put(("log", f"Skipped {skipped_links} symbolic links."))
        completed = 0
        skipped = skipped_links
        done = 0
        for relative_dir in dirs:
            if self._cancel_transfer.is_set():
                raise TransferCancelled()
            self._mkdir_remote(sftp, posixpath.join(remote_root, relative_dir))
        for local_file, relative_file, size in files:
            if self._cancel_transfer.is_set():
                raise TransferCancelled()
            destination = posixpath.join(remote_root, relative_file)
            self._mkdir_remote(sftp, posixpath.dirname(destination))
            if self._remote_exists(sftp, destination) and not overwrite:
                skipped += 1
                done += size
                self._events.put(("progress", (done, total, f"Skipped {relative_file} (exists)")))
                continue
            temporary = posixpath.join(posixpath.dirname(destination),
                                       f".{posixpath.basename(destination)}.sshuttle-{uuid.uuid4().hex}.part")
            previous_done = done
            last_update = 0.0

            def on_progress(sent, file_size):
                nonlocal last_update
                if self._cancel_transfer.is_set():
                    raise TransferCancelled()
                now = time.monotonic()
                if now - last_update >= 0.08 or sent == file_size:
                    self._events.put(("progress", (previous_done + sent, total, relative_file)))
                    last_update = now

            try:
                if usb_safe:
                    upload_with_backpressure(
                        sftp, local_file, temporary, size, on_progress,
                        self._cancel_transfer, usb_rate)
                else:
                    sftp.put(str(local_file), temporary, callback=on_progress, confirm=True)
                if self._cancel_transfer.is_set():
                    raise TransferCancelled()
                if overwrite:
                    # OpenSSH's atomic posix-rename avoids exposing half-uploaded files.
                    # Do NOT delete the existing target if the extension is unavailable.
                    sftp.posix_rename(temporary, destination)
                else:
                    if self._remote_exists(sftp, destination):
                        skipped += 1
                        sftp.remove(temporary)
                        done += size
                        continue
                    sftp.rename(temporary, destination)
            except Exception:
                try:
                    sftp.remove(temporary)
                except Exception:
                    # A disconnected SFTP channel may also fail cleanup.
                    # Preserve the original error so recovery can retry it.
                    pass
                raise
            completed += 1
            done += size
            self._events.put(("progress", (done, total, relative_file)))
        if emit_finished:
            self._events.put(("finished", ("Upload", completed, skipped, len(files) + skipped_links)))


    def _upload_to_spooler(self, sftp, root, selected, stage, subfolder, overwrite, job_id):
        """Standard SFTP upload to staging; READY.json commits the queue job."""
        job_dir = posixpath.join(stage, "jobs", job_id)
        ready = posixpath.join(job_dir, "READY.json")
        try:
            sftp.stat(ready)
        except OSError as exc:
            if getattr(exc, "errno", None) != errno.ENOENT:
                raise
        else:
            self._events.put(("finished", ("Stage", 0, 0, 0)))
            self._events.put(("spool_queued", (stage, job_id)))
            return
        dirs, files, skipped = manifest_for_upload(root, selected)
        expected = sum(size for _, _, size in files) + 2 * 1024**3
        fs = sftp.statvfs(stage)
        free = fs.f_bavail * fs.f_frsize
        if free < expected:
            raise OSError(f"Not enough staging space: {human_size(free)} free, "
                          f"{human_size(expected)} required including 2 GiB reserve.")
        self._mkdir_remote(sftp, posixpath.join(job_dir, "files"))
        self._send_files(sftp, root, selected, posixpath.join(job_dir, "files"),
                         overwrite=True, emit_finished=False)
        if self._cancel_transfer.is_set():
            raise TransferCancelled()
        self._events.put(("log", "Upload complete; calculating spooler checksums…"))
        manifest = make_spool_manifest(root, selected, job_id, subfolder, overwrite)
        if self._cancel_transfer.is_set():
            raise TransferCancelled()
        serialized = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
        if len(serialized) > 8 * 1024 * 1024:
            raise ValueError("Job manifest too large; select fewer files.")
        temporary = posixpath.join(job_dir, f".READY-{uuid.uuid4().hex}.tmp")
        try:
            with sftp.file(temporary, "wb") as output:
                output.write(serialized)
            sftp.rename(temporary, ready)
        except BaseException:
            try:
                sftp.remove(temporary)
            except Exception:
                pass
            raise
        self._events.put(("finished", ("Stage", len(files), skipped, len(files) + skipped)))
        self._events.put(("spool_queued", (stage, job_id)))

    def _receive_files(self, sftp, remote_root, selected_paths, local_root, overwrite):
        """Download selected remote files/folders, committing files only after success."""
        local_root = Path(local_root).resolve(strict=True)
        if not local_root.is_dir():
            raise NotADirectoryError(f"Local destination is not a directory: {local_root}")
        directories, files, skipped_links = manifest_for_download(sftp, remote_root, selected_paths)
        total = sum(size for _, _, size in files)
        self._events.put(("started", ("Download", len(files), total)))
        if skipped_links:
            self._events.put(("log", f"Skipped {skipped_links} remote symlinks or special files."))
        completed = 0
        skipped = skipped_links
        done = 0
        for relative_dir in directories:
            if self._cancel_transfer.is_set():
                raise TransferCancelled()
            safe_local_target(local_root, relative_dir, is_directory=True)
        for remote_file, relative_file, size in files:
            if self._cancel_transfer.is_set():
                raise TransferCancelled()
            destination = safe_local_target(local_root, relative_file)
            if destination.is_dir():
                raise IsADirectoryError(f"Local destination is a folder: {destination}")
            if destination.exists() and not overwrite:
                skipped += 1
                done += size
                self._events.put(("progress", (done, total, f"Skipped {relative_file} (exists)")))
                continue
            temporary = destination.with_name(
                f".{destination.name}.sshuttle-{uuid.uuid4().hex}.part")
            previous_done = done
            last_update = 0.0

            def on_progress(received, file_size):
                nonlocal last_update
                if self._cancel_transfer.is_set():
                    raise TransferCancelled()
                now = time.monotonic()
                if now - last_update >= 0.08 or received == file_size:
                    self._events.put(("progress", (previous_done + received, total, relative_file)))
                    last_update = now

            try:
                # Exclusive temp file creation, in the same folder for atomic rename.
                with temporary.open("xb"):
                    pass
                sftp.get(remote_file, str(temporary), callback=on_progress)
                if self._cancel_transfer.is_set():
                    raise TransferCancelled()
                if temporary.stat().st_size != size:
                    raise OSError(f"Remote file changed size while downloading: {relative_file}")
                if destination.is_symlink():
                    raise ValueError(f"Refusing symbolic link destination: {destination}")
                if overwrite:
                    os.replace(temporary, destination)
                else:
                    # Hard-link no-clobber commit is atomic: os.rename silently
                    # overwrites on POSIX, so never use it in skip-existing mode.
                    try:
                        os.link(temporary, destination)
                    except FileExistsError:
                        skipped += 1
                        done += size
                        self._events.put(("progress", (done, total, f"Skipped {relative_file} (exists)")))
                        continue
            finally:
                # Remove the temp file after a linked commit or interrupted transfer.
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            completed += 1
            done += size
            self._events.put(("progress", (done, total, relative_file)))
        self._events.put(("finished", ("Download", completed, skipped, len(files) + skipped_links)))

    def _close(self):
        self._cancel_transfer.set()
        self._work.put(("exit", None))
        self.root.destroy()


def main():
    root = Tk()
    SSHuttleApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
