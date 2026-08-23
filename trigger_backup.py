"""Nightly backup trigger, meant to run as a Render Cron Job.

A Cron Job is a separate, ephemeral Render service and can't reach the web
service's persistent disk directly - so instead of doing the backup here,
this just pings the web app's own /internal/backup-db endpoint, which runs
on the instance that actually has the disk mounted and does the real work
(snapshot the database, upload it to R2, prune old backups).

Stdlib only, on purpose - a cron job's whole footprint should be as small
and dependency-free as possible.

Required env vars (set these on the Cron Job service, not the web service):
  BACKUP_URL        e.g. https://moundhq.com/internal/backup-db
  PHX_BACKUP_TOKEN   same shared-secret value set on the web service
"""
import json
import os
import sys
import urllib.request
import urllib.error

BACKUP_URL = os.environ.get("BACKUP_URL", "")
BACKUP_TOKEN = os.environ.get("PHX_BACKUP_TOKEN", "")


def main():
    if not BACKUP_URL or not BACKUP_TOKEN:
        print("Missing BACKUP_URL or PHX_BACKUP_TOKEN - nothing to do.")
        sys.exit(1)

    req = urllib.request.Request(
        BACKUP_URL,
        method="POST",
        headers={"X-Backup-Token": BACKUP_TOKEN},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"Backup failed: HTTP {e.code} - {e.read().decode()}")
        sys.exit(1)
    except Exception as e:
        print(f"Backup failed: {e}")
        sys.exit(1)

    if not body.get("ok"):
        print(f"Backup failed: {body.get('error', 'unknown error')}")
        sys.exit(1)

    print(f"Backup OK: {body['key']} (pruned {body.get('pruned', 0)} old snapshot(s))")


if __name__ == "__main__":
    main()
