# SSHuttle contributor guidance

The Python desktop app lives in sshuttle.py. It uses Tkinter on the main thread and one worker thread that owns SSHClient/SFTPClient and processes queued actions.

1. No worker thread may touch Tk widgets. Pass results through the event queue.
2. Never use Paramiko AutoAddPolicy or ignore changed host keys.
3. Do not log or save passwords, secrets or private keys.
4. Prefer remote SFTP filesystem operations, not shell commands.
5. Upload to a temporary remote filename, rename after success, and default to skipping existing files.
6. Do not follow symlinks automatically; prevent recursive cycles.
7. Test with python -m py_compile sshuttle.py and python -m unittest discover -s tests -v.
8. Keep DEVELOPMENT.md and README.md current, and distinguish manual tests from CI checks.

## Ubuntu Spooler

- sshuttle_spooler.py is a standalone **stdlib-only** Python Linux service; no remote shell execution or listening HTTP port.
- Durable SQLite queue under configured internal staging_root. SFTP client places jobs/<uuid>/files then publishes READY.json atomically. Service writes status.json.
- Never copy unless the configured USB target is actually mounted and different from staging.
- Validate every manifest path against traversal and symlink escapes. Do not trust remote-uploaded manifests.
- Verify SHA-256 before commit; preserve already completed destination files unless overwrite was explicitly requested.
- Keep staged data intact after incomplete/failed jobs. Clear only after verified completion.
- Install sample systemd template running as an unprivileged existing Ubuntu SSH account.
- Update docs/UBUNTU_SPOOLER.md when changing paths, protocol or deployment.

## Slow USB targets

- Keep USB-safe transfers single-threaded with synchronous, non-pipelined SFTP writes and a cancellable speed cap. Preserve fast mode.
- Distinguish transport timeouts (retryable) from destination I/O errors (not silently retryable).
- Retain the temporary-file then rename semantics and default skip-existing behaviour.
- Test against a real USB target separately from mocked CI tests before claiming live reliability.
