FROM restic/restic:0.19.1@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510 AS restic
FROM rclone/rclone:1.76.0@sha256:7cbeec24a44f206067597c0368be4de90ae19a64d5113c7d1938bd737a81e466 AS rclone
FROM ghcr.io/siderolabs/talosctl:v1.14.5@sha256:0b852e35c1f513ee5eacc22183d6193486012420569fde39fcd5260c51938443 AS talosctl
FROM alpine:3.24.4@sha256:93b41ef40b1677fce8fcc64ff4af09daddfacf5040afde618bc46c1e2d1cc7d4

# Native Alpine database clients avoid copying obsolete glibc MongoDB binaries.
RUN apk add --no-cache \
      tini bash ca-certificates curl tzdata python3 py3-pip py3-requests py3-yaml \
      gzip findutils util-linux npm mariadb-client mariadb-connector-c \
      postgresql18-client mongodb-tools && \
    python3 -m venv --system-site-packages /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir crontab==1.0.5 && \
    npm install --global elasticdump@6.125.1 && \
    npm cache clean --force

COPY --from=talosctl /talosctl /usr/local/bin/talosctl
COPY --from=restic /usr/bin/restic /usr/bin/restic
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
ENV PATH="/opt/venv/bin:${PATH}" \
    BACKUP_ROOT=/backup
WORKDIR /scripts
COPY *.py /scripts/
VOLUME /backup

# Keep this entrypoint when overriding arguments (Kubernetes: use args, not command).
ENTRYPOINT ["/sbin/tini", "-g", "--"]
CMD ["/scripts/backup_client.py", "schedule", "@daily"]
