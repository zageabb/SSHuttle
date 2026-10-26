# SSHuttle

SSHuttle is a simple **Python desktop app** for transferring files from a local computer to an Ubuntu server over SSH/SFTP. It is designed for local use on macOS, Linux, or Windows.

## Version 0.1 features

- Side-by-side local and remote file browsers, folder navigation and editable directory paths.
- Select multiple local files and/or folders for recursive uploads.
- Progress bar, byte counters, current file, transfer summary and cancellation.
- Connection by hostname/IP or OpenSSH Host alias from ~/.ssh/config.
- SSH agent and local private keys attempted automatically; optional user, password or key passphrase.
- SSH host keys verified against ~/.ssh/known_hosts (no automatic acceptance of unknown host keys).
- Remote files are skipped by default, with an explicit option to replace them.
- Upload to a temporary file and rename it only after completion, helping to protect existing files.
- Passwords are never written to disk.

Version 0.1 is **local-to-server upload only**. Downloads can be added in a later release.

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

## Upload

1. Connect using host/IP or SSH config alias.
2. Browse to the source folder on the left; select one or more files or subfolders.
3. Browse to the destination on the right, or enter an absolute remote path and click Go.
4. Press **Upload selected →**. Monitor current filename, total bytes and percentage.
5. By default files already on the server are skipped. Check **Replace existing remote files** to overwrite.

When overwrite is selected, the server must support the SFTP posix-rename extension (typical for Ubuntu OpenSSH). If it is unavailable, SSHuttle leaves existing remote files intact and reports an error.

In-flight temporary uploads are deleted on a best-effort basis after error/cancellation. Previously completed uploads remain. Symbolic links are not followed or transferred.

## Test

    python -m py_compile sshuttle.py
    python -m unittest discover -s tests -v

CI checks compilation and unit tests, but does not cover live SSH server interactions or GUI end-to-end behaviour.

## Design

- sshuttle.py — Tkinter UI, SSH worker and upload engine.
- tests/ — helper unit tests.
- DEVELOPMENT.md — roadmap.
- AGENTS.md — development guidelines.
- .github/workflows/ci.yml — CI checks.

**Security:** SSHuttle works through SFTP only, without a remote shell. Never commit passwords, keys or actual server secrets to the public repository.
