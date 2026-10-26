# SSHuttle

SSHuttle is a simple **Python desktop app** for transferring files from a local computer to an Ubuntu server over SSH/SFTP. It is designed for local use on macOS, Linux, or Windows.

## Features

- Side-by-side local and remote file browsers, folder navigation and editable directory paths.
- Select multiple files or folders on either side for recursive uploads **and downloads**.
- Progress bar, byte counters, current file, transfer summary and cancellation for both directions.
- Connection by hostname/IP or OpenSSH Host alias from ~/.ssh/config.
- SSH agent and local private keys attempted automatically; optional user, password or key passphrase.
- SSH host keys verified against ~/.ssh/known_hosts (no automatic acceptance of unknown host keys).
- Existing destination files are skipped by default, with an explicit option to replace them.
- Write transfers to temporary files, committing completed uploads/downloads safely.
- Optional **USB-safe uploads**: 32 KiB acknowledged SFTP writes without pipelining, rate limit (default 2 MiB/s), and extended retry backoff.
- Passwords are never written to disk.

**Slow-server recovery:** 60-second connection timeout (configurable up to 300 seconds), 90-second minimum SSH authentication/banner timeouts, and longer SFTP inactivity timeouts. SSH keepalives every 30 seconds, plus three retries by default (configurable 0–5), automatically reconnect after temporary failures. A failed transfer attempt restarts the operation rather than resuming partway through an individual file.

## Install

Requires Python 3.10+ with Tkinter, an SSH server with SFTP enabled, and the Paramiko package.

On your Mac or Linux laptop:

    git clone https://github.com/zageabb/SSHuttle.git
    cd SSHuttle
    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install -r requirements.txt
    python sshuttle.py

On Windows activate .venv\Scripts\activate and run python sshuttle.py.

If Python reports a missing tkinter module on macOS, install a Python build that includes Tkinter or the matching Homebrew python-tk package.

## SSH authentication

An example ~/.ssh/config entry:

    Host ubuntu-home
        HostName 192.168.1.50
        User myuser
        Port 22
        IdentityFile ~/.ssh/id_ed25519

Enter ubuntu-home into the server field. The username and port can be left blank to use the SSH config values.

**First-time host verification:** Connect using your system terminal first, for example ssh ubuntu-home, verify the fingerprint out of band, and accept it only when correct. SSHuttle rejects unknown/changed host keys by default. This protects against impersonation.

OpenSSH ProxyJump and ProxyCommand directives are not supported in v0.1. Hardware-backed key types may also need separate support.

## Upload and download

1. Connect using host/IP or SSH config alias.
2. For **upload**, browse to the source folder on the left and destination on the right, select local files/folders, then press **Upload selected →**.
3. For **download**, browse to the destination folder on the left and source on the right, select remote files/folders, then press **← Download selected**.
4. Monitor the filename, bytes and progress bar. Press **Cancel transfer** to stop.
5. Existing destination files are skipped. To replace files, check **Replace existing destination files**.

The **Timeout per connection** input defaults to 60 seconds and supports 15–300 seconds. **Retries** defaults to 3 (range 0–5). If your Ubuntu server is frequently busy, try a 120–180 second connection timeout and 4 retries. SFTP operation timeout scales to at least 120 seconds and at most 600 seconds.

A temporary disconnect triggers a new SSH connection and retries the operation. Transfers **restart the interrupted file**, not byte-level resume. Finished destination files remain in place; in default skip mode they will not be re-transferred. With overwrite enabled, a retry may retransmit files already replaced. Passwords/key passphrases remain only in process memory during the connection to support reconnects, not on disk.

When overwrite is selected, the server must support the SFTP posix-rename extension (typical for Ubuntu OpenSSH). If it is unavailable, SSHuttle leaves existing remote files intact and reports an error.

In-flight temporary files are deleted on a best-effort basis after error/cancellation. If the SSH connection drops completely, remote orphaned `.sshuttle-*.part` files might remain and can be deleted after checking they are not part of an active transfer. Previously completed transfers remain. Symbolic links are not followed or transferred.

## USB-attached drive reliability

If Ubuntu is writing to an external USB disk that occasionally stalls, tick **USB-safe uploads** before pressing Upload. The mode uses acknowledged 32 KiB writes (no Paramiko SFTP pipelining), with an optional upload limit expressed in **MiB/s**. It defaults to **2 MiB/s** when enabled; set it to **0** for uncapped sequential transfers. The limit must be in the range 0–100 MiB/s.

For a slow drive, try **USB-safe uploads ON**, **2 MiB/s**, **Connection timeout 120–180 seconds**, and **4 retries**. Reconnect to apply changes to SSH timeouts and retry count. USB-safe uploads use a 5, 10, 20, 30 second retry backoff after transient SSH errors; ordinary transfers retain shorter retries.

USB-safe mode is OFF by default, so existing fast SFTP uploads are unchanged. Downloads are unaffected. This feature cannot fix a faulty USB cable, insufficient drive power, hardware resets, or disk filesystem I/O errors. A disconnect during an upload can leave a hidden remote temporary \`.sshuttle-*.part\` file; clean up after confirming it is not active.

To diagnose errors on Ubuntu without changing drive data:

\`\`\`bash
lsblk -o NAME,TRAN,SIZE,FSTYPE,MOUNTPOINTS,MODEL
lsusb -t
df -h /path/to/usb/mount
sudo journalctl -k --since "30 minutes ago" --no-pager | grep -Ei 'usb|uas|reset|i/o error|buffer i/o|ext4|exfat|ntfs|xfs'
\`\`\`

Look for USB resets/disconnects or I/O errors. Try a different cable/port and powered hub for bus-powered disks, especially if a drive disconnects under load. A safe next architectural option if stalls persist is **upload into an internal Ubuntu staging directory and move to USB using an independent server-side worker**, so the SFTP connection never waits for the slow final drive.

## Test

    python -m py_compile sshuttle.py
    python -m unittest discover -s tests -v

CI checks compilation and unit tests, but does not cover live SSH server interactions, reconnection during actual network loss, or GUI end-to-end behaviour.

## Design

- sshuttle.py — Tkinter UI, SSH worker and upload engine.
- tests/ — helper unit tests.
- DEVELOPMENT.md — roadmap.
- AGENTS.md — development guidelines.
- .github/workflows/ci.yml — CI checks.

**Security:** SSHuttle works through SFTP only, without a remote shell. Never commit passwords, keys or actual server secrets to the public repository.
