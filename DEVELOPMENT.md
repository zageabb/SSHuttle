# SSHuttle development roadmap

## v0.1 — First working desktop version

- [x] Local and remote folder browser in Python/Tkinter.
- [x] Username/password and ~/.ssh/config + SSH agent/private key support.
- [x] Remote host key validation with known_hosts.
- [x] Multi-file and recursive-folder uploads over SFTP.
- [x] Transfer progress, cancel, file skip and explicit overwrite.
- [x] Temporary remote upload files before rename.
- [ ] Manual macOS-to-Ubuntu end-to-end test.

## v0.2 — Convenience

- Downloads from server to local machine.
- Saved non-secret connection profiles and default folders.
- Drag-and-drop, improved keyboard navigation and transfer history.
- Richer per-file progress and queue.

## v0.3 — Advanced

- Resume/retry, checksums and collision preview.
- Filters and basic directory synchronisation.
- Advanced SSH aliases (ProxyJump / ProxyCommand where feasible).
- Installable macOS and Windows packages.

## Non-negotiable principles

- No secrets stored in settings or version control.
- SSH host key verification stays enabled.
- Network work must not block the graphical main thread.
- Default is to skip existing remote files, never silently replace them.
- No remote shell command execution is needed for SFTP.
- New features receive tests, docs and a green CI run.
