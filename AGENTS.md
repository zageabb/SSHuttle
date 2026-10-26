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
