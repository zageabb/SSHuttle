"""SSHuttle - small local-to-SSH file transfer desktop utility.

Run: python3 sshuttle.py
Requires: paramiko (see requirements.txt).  Uses Tkinter from Python.
"""
from __future__ import annotations

import errno
import os
import posixpath
import queue
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


class SSHuttleApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("SSHuttle - SSH File Transfer")
        self.root.geometry("1060x680")
        self.root.minsize(780, 510)

        self.host = StringVar()
        self.username = StringVar()
        self.port = StringVar()
        self.password = StringVar()
        self.local_path = StringVar(value=str(Path.home()))
        self.remote_path = StringVar(value="")
        self.overwrite = BooleanVar(value=False)
        self.connection_status = StringVar(value="Disconnected")
        self.transfer_status = StringVar(value="Select files or folders on the left, then upload.")
        self.progress_label = StringVar(value="0%")
        self._connected = False
        self._busy = False
        self._cancel_transfer = threading.Event()
        self._work: queue.Queue = queue.Queue()
        self._events: queue.Queue = queue.Queue()
        self._local_items: dict[str, Path] = {}
        self._remote_items: dict[str, tuple[str, bool]] = {}

        self._build_ui()
        self._show_local(Path.home())
        self._thread = threading.Thread(target=self._worker, daemon=True, name="sshuttle-sftp")
        self._thread.start()
        self.root.after(80, self._drain_events)
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
        self.cancel_button = ttk.Button(controls, text="Cancel transfer", command=self._cancel, state="disabled")
        self.cancel_button.pack(side=LEFT, padx=8)
        ttk.Checkbutton(controls, text="Replace existing remote files", variable=self.overwrite).pack(side=LEFT, padx=8)
        ttk.Button(controls, text="Refresh", command=self._refresh).pack(side=RIGHT)

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
        self._work.put(("connect", (target, self.password.get())))

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
        self._cancel_transfer.clear()
        self._set_busy(True)
        self._work.put(("upload", (Path(self.local_path.get()).resolve(), selected, remote_dir, self.overwrite.get())))

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
        self.cancel_button.configure(state="normal" if value else "disabled")
        self.connect_button.configure(state="disabled" if value else "normal")

    def _drain_events(self):
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "connected":
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
                    self._log("Disconnected.")
                elif kind == "connect_error":
                    self.connection_status.set("Connection failed")
                    self.connect_button.configure(state="normal", text="Connect")
                    self.password.set("")
                    self._log(f"Connection failed: {payload}")
                    messagebox.showerror("SSH connection", str(payload))
                elif kind == "remote_list":
                    path, entries = payload
                    self.remote_path.set(path)
                    self.remote_tree.delete(*self.remote_tree.get_children())
                    self._remote_items.clear()
                    for name, is_dir, size, remote_name in entries:
                        item_id = self.remote_tree.insert("", END,
                            text=("📁 " if is_dir else "📄 ") + name,
                            values=("" if is_dir else human_size(size),))
                        self._remote_items[item_id] = (remote_name, is_dir)
                elif kind == "started":
                    total_files, total_bytes = payload
                    self.progress_bar.configure(value=0)
                    self.progress_label.set("0%")
                    self.transfer_status.set(f"Uploading {total_files} file(s), {human_size(total_bytes)}")
                elif kind == "progress":
                    done, total, current_file = payload
                    percent = 100 if total == 0 else min(100, round(done / total * 100))
                    self.progress_bar.configure(value=percent)
                    self.progress_label.set(f"{percent}%")
                    self.transfer_status.set(f"{current_file} · {human_size(done)} / {human_size(total)}")
                elif kind == "finished":
                    completed, skipped, total = payload
                    self._set_busy(False)
                    if not self._cancel_transfer.is_set():
                        self.progress_bar.configure(value=100)
                        self.progress_label.set("100%")
                    self.transfer_status.set(f"Complete: {completed} uploaded, {skipped} skipped, {total} considered.")
                    self._log(self.transfer_status.get())
                    self._work.put(("list", self.remote_path.get()))
                elif kind == "cancelled":
                    self._set_busy(False)
                    self.transfer_status.set("Upload cancelled. Completed files remain on server.")
                    self._log(self.transfer_status.get())
                    self._work.put(("list", self.remote_path.get()))
                elif kind == "error":
                    self._set_busy(False)
                    self.transfer_status.set(f"Error: {payload}")
                    self._log(f"Error: {payload}")
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(80, self._drain_events)

    def _worker(self):
        client = None
        sftp = None
        try:
            while True:
                action, data = self._work.get()
                if action == "exit":
                    break
                if action == "disconnect":
                    if sftp:
                        sftp.close()
                        sftp = None
                    if client:
                        client.close()
                        client = None
                    self._events.put(("disconnected", None))
                elif action == "connect":
                    target, password = data
                    try:
                        client = paramiko.SSHClient()
                        client.load_system_host_keys()
                        known_hosts = Path.home() / ".ssh" / "known_hosts"
                        if known_hosts.exists():
                            client.load_host_keys(str(known_hosts))
                        client.set_missing_host_key_policy(paramiko.RejectPolicy())
                        client.connect(**target, password=password or None,
                            passphrase=password or None, look_for_keys=True,
                            allow_agent=True, timeout=15, auth_timeout=15, banner_timeout=15)
                        sftp = client.open_sftp()
                        home = sftp.normalize(".")
                        self._events.put(("connected", home))
                    except Exception as exc:
                        if client:
                            client.close()
                        client, sftp = None, None
                        self._events.put(("connect_error", exc))
                elif action == "list":
                    try:
                        if sftp is None:
                            continue
                        path = sftp.normalize(data)
                        attributes = sftp.listdir_attr(path)
                        entries = []
                        for attr in attributes:
                            if attr.filename in (".", ".."):
                                continue
                            is_dir = stat.S_ISDIR(attr.st_mode or 0)
                            entries.append((attr.filename, is_dir, attr.st_size or 0,
                                            posixpath.join(path, attr.filename)))
                        entries.sort(key=lambda item: (not item[1], item[0].casefold()))
                        self._events.put(("remote_list", (path, entries)))
                    except Exception as exc:
                        self._events.put(("error", f"Cannot browse remote folder: {exc}"))
                elif action == "upload":
                    try:
                        if sftp is None:
                            raise ConnectionError("Not connected.")
                        self._send_files(sftp, *data)
                    except TransferCancelled:
                        self._events.put(("cancelled", None))
                    except Exception as exc:
                        self._events.put(("error", str(exc)))
        finally:
            if sftp:
                sftp.close()
            if client:
                client.close()

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

    def _send_files(self, sftp, local_root, selected_paths, remote_root, overwrite):
        dirs, files, skipped_links = manifest_for_upload(local_root, selected_paths)
        total = sum(size for _, _, size in files)
        self._events.put(("started", (len(files), total)))
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
            except BaseException:
                try:
                    sftp.remove(temporary)
                except OSError:
                    pass
                raise
            completed += 1
            done += size
            self._events.put(("progress", (done, total, relative_file)))
        self._events.put(("finished", (completed, skipped, len(files) + skipped_links)))

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
