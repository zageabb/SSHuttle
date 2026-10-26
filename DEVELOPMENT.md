# SSHuttle development roadmap

## v0.1 — First working desktop version

- [x] Local and remote folder browser in Python/Tkinter.
- [x] Username/password and ~/.ssh/config + SSH agent/private key support.
- [x] Remote host key validation with known_hosts.
- [x] Multi-file and recursive-folder uploads over SFTP.
- [x] Transfer progress, cancel, file skip and explicit overwrite.
- [x] Temporary remote upload files before rename.
- [ ] Manual macOS-to-Ubuntu end-to-end test.

## v0.2 — Bidirectional transfer and resilience

- [x] Downloads from server to local machine, including recursive folders and empty directories.
- [x] Local destination safety checks, temp files, cancel and skip/overwrite options.
- [x] Connection timeout and retry settings for slow Ubuntu servers.
- [x] SSH keepalives and automatic reconnect/retry on temporary network errors.
- [ ] Validate live timeout recovery by interrupting a transfer over SSH.
- [ ] Saved non-secret connection profiles and default folders.
- [ ] Drag-and-drop, improved keyboard navigation and transfer history.
- [ ] Richer per-file progress and queue.

## v0.2.1 — USB-safe upload option

- [x] Optional non-pipelined SFTP uploads, with 32 KiB write chunks.
- [x] Configurable 0–100 MiB/s rate limit (default 2 when enabled).
- [x] Slower exponential retry backoff for stalled USB destinations.
- [x] Unit tests for pacing, cancellation, size verification and original upload commits.
- [ ] Validate transfers to a real USB-attached Ubuntu destination under load.
- [ ] Investigate staging to internal disk with server-side deferred copying if USB stalls persist.

## v0.2.2 — Ubuntu USB Spooler

- [x] Standard SFTP uploads into an internal Ubuntu staging directory.
- [x] SHA-256 manifest committed atomically after staging upload.
- [x] Background Python service, SQLite queue and systemd unit.
- [x] USB mount detection, configurable speed limit and read-back verification.
- [x] Retry and recover incomplete jobs after Ubuntu service restart.
- [x] Expose Ubuntu job progress in Mac SSHuttle using SFTP status.json.
- [x] Do not require an HTTP port or non-SSH credentials.
- [x] Tests for staging manifests, missing USB mount, overwrites, bad checksums.
- [ ] Install and validate on the actual Ubuntu server and external USB drive.
- [ ] Add saved job tracking after restarting the Mac app.
- [ ] Add pause/retry/requeue controls and staging disk-space queue limit.

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
