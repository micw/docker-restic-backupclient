#!/usr/bin/env python3
"""Exercise actual restic -> rclone -> HTTP WebDAV without external credentials."""
import subprocess
import sys
import uuid

image = sys.argv[1] if len(sys.argv) > 1 else "restic-backupclient:test"
prefix = "restic-webdav-test-" + uuid.uuid4().hex[:12]
network, server, client = prefix, prefix + "-server", prefix + "-client"

def docker(*args):
    return subprocess.check_output(["docker", *args], stderr=subprocess.STDOUT).decode()

try:
    docker("network", "create", network)
    docker("run", "-d", "--name", server, "--network", network, image,
           "rclone", "serve", "webdav", "/tmp/webdav", "--addr", ":8080",
           "--user", "test", "--pass", "test-only")
    password = docker("run", "--rm", image, "rclone", "obscure", "test-only").strip()
    script = r'''set -eu
printf 'keep:\n  last: 1\n' >/tmp/config.yaml
printf 'first version\n' >/backup/sample
/scripts/backup_client.py run
printf 'changed version\n' >/backup/sample
/scripts/backup_client.py run
restic restore latest --target /tmp/restore
cmp /backup/sample /tmp/restore/backup/sample
restic check
restic snapshots --json | python3 -c 'import sys,json; assert len(json.load(sys.stdin)) == 1'
'''
    print(docker("run", "--name", client, "--network", network,
        "-e", "RESTIC_REPOSITORY=rclone:webdav:restic",
        "-e", "RESTIC_PASSWORD=test-only",
        "-e", "BACKUP_HOSTNAME=test-webdav",
        "-e", "BACKUP_CONFIG=/tmp/config.yaml",
        "-e", "RCLONE_CONFIG_WEBDAV_TYPE=webdav",
        "-e", "RCLONE_CONFIG_WEBDAV_VENDOR=other",
        "-e", "RCLONE_CONFIG_WEBDAV_URL=http://" + server + ":8080/",
        "-e", "RCLONE_CONFIG_WEBDAV_USER=test",
        "-e", "RCLONE_CONFIG_WEBDAV_PASS=" + password,
        image, "sh", "-ec", script))
    print("PASS: actual rclone WebDAV backup, incremental, retention, prune, restore and check")
finally:
    # Exact UUID names identify only resources owned by this test.
    for container in (client, server):
        subprocess.run(["docker", "rm", "-f", "-v", container], check=False, stdout=subprocess.DEVNULL)
    subprocess.run(["docker", "network", "rm", network], check=False, stdout=subprocess.DEVNULL)
