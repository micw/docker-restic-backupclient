FROM restic/restic:0.19.1@sha256:136600b6ff6843d61d355f7f71f460a166429f35de6fd11b568fece3c9a4d510 AS restic
FROM rclone/rclone:1.75.2@sha256:2687085f718d3c628f7fdfb77c52a1d344332aed543110bb88088f2c70d43eb5 AS rclone
FROM alpine:3.24.3@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a

# Native Alpine database clients avoid copying obsolete glibc MongoDB binaries.
RUN apk add --no-cache \
      tini bash ca-certificates curl tzdata python3 py3-pip py3-requests py3-yaml \
      gzip findutils util-linux npm mariadb-client mariadb-connector-c \
      postgresql18-client mongodb-tools && \
    python3 -m venv --system-site-packages /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir crontab==1.0.5 && \
    npm install --global elasticdump@6.125.1 && \
    npm cache clean --force

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
