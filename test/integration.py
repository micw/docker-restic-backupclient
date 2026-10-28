#!/usr/bin/env python3
"""Run release smoke tests against the actual image; never uses production data."""
import json
import subprocess
import sys
import uuid

image = sys.argv[1] if len(sys.argv) > 1 else "restic-backupclient:test"
name = "restic-release-test-" + uuid.uuid4().hex[:12]

def docker(*args):
    return subprocess.check_output(["docker", *args], stderr=subprocess.STDOUT).decode()

try:
    docker("run", "-d", "--name", name, "-e", "RESTIC_REPOSITORY=/tmp/repository", "-e", "RESTIC_PASSWORD=release-test-only", "-e", "BACKUP_HOSTNAME=release-test", "-e", "BACKUP_CONFIG=/tmp/config.yaml", image)
    assert "tini" in docker("exec", name, "sh", "-c", "tr '\\000' ' ' </proc/1/cmdline")
    docker("exec", name, "sh", "-ec", "printf 'keep:\\n  last: 1\\n' >/tmp/config.yaml; printf 'original content\\n' >/backup/file.txt")
    for tool in (["restic", "version"], ["rclone", "version"], ["mongodump", "--version"], ["pg_dump", "--version"], ["mariadb-dump", "--version"], ["elasticdump", "--version"]):
        print(docker("exec", name, *tool), end="")
    print(docker("exec", name, "/scripts/backup_client.py", "run"))
    docker("exec", name, "sh", "-ec", "printf 'updated content\\n' >/backup/file.txt")
    print(docker("exec", name, "/scripts/backup_client.py", "run"))
    snapshots = json.loads(docker("exec", name, "restic", "snapshots", "--json"))
    assert len(snapshots) == 1, snapshots
    docker("exec", name, "restic", "restore", "latest", "--target", "/tmp/restored")
    docker("exec", name, "cmp", "/backup/file.txt", "/tmp/restored/backup/file.txt")
    docker("exec", name, "restic", "check")
    docker("stop", "--time", "10", name)
    code = int(docker("inspect", "--format", "{{.State.ExitCode}}", name).strip())
    assert code != 137, "Container had to be SIGKILLed instead of stopping"
    print("PASS: versions, scheduler, tini, incremental backup, retention, prune, restore, check and stop")
finally:
    # Only the container created with the exact unique name above is removed.
    subprocess.run(["docker", "rm", "-f", "-v", name], check=False, stdout=subprocess.DEVNULL)
