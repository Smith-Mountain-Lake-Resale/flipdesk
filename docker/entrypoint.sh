#!/bin/sh
set -e
# Run as PUID:PGID so files written to the host-mapped /data and /config are owned by you.
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
mkdir -p "$DATA_DIR/uploads" "$DATA_DIR/backups" "$CONFIG_DIR"

if [ "$(id -u)" = "0" ]; then
  if ! getent group "$PGID" >/dev/null 2>&1; then groupadd -o -g "$PGID" flipdesk; fi
  if ! getent passwd "$PUID" >/dev/null 2>&1; then useradd -o -u "$PUID" -g "$PGID" -M -d /app -s /usr/sbin/nologin flipdesk; fi
  chown -R "$PUID:$PGID" "$DATA_DIR" "$CONFIG_DIR" 2>/dev/null || echo "[entrypoint] warning: could not chown data/config (read-only or NFS?)"
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups \
    python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}" \
      --proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-*}" --workers 1
fi

exec python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}" \
  --proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-*}" --workers 1
