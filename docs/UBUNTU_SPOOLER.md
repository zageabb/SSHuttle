# SSHuttle USB Spooler — Ubuntu installation

This installs a **local background service** on Ubuntu. The Mac still uses standard
SSH/SFTP. USB copying takes place only after an entire job has reached internal staging.
The worker requires no incoming HTTP port and no additional credentials.

## Before installing

Choose:
- An **internal** Ubuntu filesystem with enough space to stage uploads; default:
  `/srv/sshuttle/staging`.
- Your **actual USB mount point**; e.g. `/mnt/usb`. The sample is not a
  device discovery mechanism. Confirm the mount with `findmnt`.
- The Ubuntu SSH login account that already has permission to write to the
  USB drive. The background service should run under **that same account**,
  never root.
- Optionally the USB filesystem UUID, to prevent writing to a different drive
  mounted at the same path.

Read-only discovery commands on Ubuntu:

```bash
lsblk -o NAME,SIZE,FSTYPE,UUID,TRAN,MOUNTPOINTS,MODEL
findmnt -T /mnt/usb -o TARGET,SOURCE,FSTYPE,UUID
df -h / /mnt/usb
```

**The service must not be started with an incorrect mount path.** It checks
`os.path.ismount()` and compares the filesystem devices of staging and USB
before copying. With `expected_usb_uuid` set, it also checks the UUID.

## Install the service

Clone the existing SSHuttle repository to Ubuntu, then run:

```bash
git clone https://github.com/zageabb/SSHuttle.git
cd SSHuttle
sudo install -m 755 sshuttle_spooler.py /usr/local/bin/sshuttle_spooler.py
sudo install -m 644 deploy/sshuttle-spooler.example.json /etc/sshuttle-spooler.json
sudo install -m 644 deploy/sshuttle-spooler@.service /etc/systemd/system/sshuttle-spooler@.service
```

Edit the configuration before starting:

```bash
sudo nano /etc/sshuttle-spooler.json
```

Set `usb_mount` to the real USB mount path; adjust `staging_root` if needed.
Set `expected_usb_uuid` from `lsblk` for additional protection.

For the **existing Ubuntu SSH account** replace `YOUR_SSH_USER`:

```bash
sudo mkdir -p /srv/sshuttle/staging/jobs
sudo chown -R YOUR_SSH_USER:YOUR_SSH_USER /srv/sshuttle/staging
sudo chmod 700 /srv/sshuttle/staging /srv/sshuttle/staging/jobs
sudo systemctl daemon-reload
sudo systemctl enable --now sshuttle-spooler@YOUR_SSH_USER.service
sudo systemctl status sshuttle-spooler@YOUR_SSH_USER.service
```

The user must be able to create files in the USB mount. Confirm this using a
small test file as that user. Do not recursively change the USB drive's ownership
unless you understand and intend those permission changes.

```bash
sudo -u YOUR_SSH_USER /usr/bin/test -w /mnt/usb && echo "USB writable"
```

If the configured mount is unavailable, the service **does not fall back** to
writing into the underlying Ubuntu folder. Queued jobs remain in staging.
Disk UUID checking is strongly recommended if more than one removable disk is used.

## SSHuttle desktop usage

Update the Mac app:

```bash
cd SSHuttle
git pull origin main
source .venv/bin/activate
python sshuttle.py
```

In the connection panel:

1. Connect to the normal SSH host (same SSH key/password).
2. Enable **Send via USB Spooler**. This overrides direct upload routing.
3. Enter the staging root, normally `/srv/sshuttle/staging`.
4. Enter a *relative* **USB destination subfolder** such as
   `backups/photos`, or leave it empty for the USB root. This is interpreted
   under the Ubuntu service's configured `usb_mount`. The right-hand folder
   browser remains useful for ordinary/direct SFTP uploads.
5. Select local files/folders on the left, press **Upload selected →**.
6. After the staging upload and SHA-256 calculation, SSHuttle atomically submits
   `READY.json`. It shows USB queue progress from `status.json` every five
   seconds while connected. The background service continues when SSHuttle or
   the Mac closes.

The client checks free space on the Ubuntu staging filesystem before uploading
and retains a **2 GiB reserve**. Larger queues still require adequate internal
disk space; the app does not remove files until the server has finished
verification. The speed limit is set on the Ubuntu service in MiB/s and affects
only the **staging-to-USB** copy, not the SSH network upload.

For a starting configuration, use `limit_mib: 2`, one file at a time and
`verify_readback: true`. The worker verifies the streamed SHA-256 digest and
the USB file's read-back digest before committing the destination file. It
writes a hidden temporary file and publishes it only after successful verification.

## Monitoring and recovery

```bash
sudo journalctl -u sshuttle-spooler@YOUR_SSH_USER.service -f
systemctl status sshuttle-spooler@YOUR_SSH_USER.service
ls -la /srv/sshuttle/staging/jobs
```

Job status is also retained as:
`/srv/sshuttle/staging/jobs/<32-character-job-id>/status.json`.

A SQLite journal at `/srv/sshuttle/staging/state.sqlite3` survives process or
Ubuntu restarts. Interrupted copies restart the affected file; earlier completed
files remain marked done. On complete verification, staged content in
`jobs/<id>/files` is removed, but the manifest and status remain for audit.
Status values: `queued`, `copying`, `retrying`, `waiting_usb`,
`completed`, `failed`.

When the USB drive is physically disconnected, jobs enter `waiting_usb`
rather than writing onto the unmounted directory. If the drive itself is
resetting or losing power, address the USB cable, enclosure, power supply or
filesystem issues separately.

**Current limitations:** a paused/failed job cannot yet be retried manually
from the Mac UI; jobs are not automatically rediscovered by the Mac app after
it restarts. Copy throttling is per file (2 MiB/s approximate), jobs are copied
serially, and copying an interrupted file resumes from the beginning. This
does not create an additional HTTP/API service.
