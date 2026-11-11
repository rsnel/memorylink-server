#!/usr/bin/env bash
#
# Install or upgrade the MemoryLink server on a systemd Linux host.
#
# Run as root from a checkout of https://github.com/rsnel/memorylink-server:
#   sudo deploy/install.sh
#
# Data in /var/lib/memorylink is preserved across upgrades. The service is
# enabled but not started, so you can edit /etc/memorylink.env first.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this script as root." >&2
    exit 1
fi

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
INSTALL_DIR=/opt/memorylink
DATA_DIR=/var/lib/memorylink
SERVICE_USER=memorylink
SERVICE_GROUP=memorylink
PYTHON=${PYTHON:-python3}

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Python 3 not found; install python3 and python3-venv." >&2
    exit 1
fi
if ! "$PYTHON" -c 'import venv' >/dev/null 2>&1; then
    echo "The Python venv module is missing; install python3-venv." >&2
    exit 1
fi

if ! getent group "$SERVICE_GROUP" >/dev/null; then
    groupadd --system "$SERVICE_GROUP"
fi
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --gid "$SERVICE_GROUP" \
        --home-dir "$INSTALL_DIR" --no-create-home \
        --shell /usr/sbin/nologin "$SERVICE_USER"
fi

install -d -m 0755 "$INSTALL_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$DATA_DIR"

# Replace the application code but never the data directory.
rm -rf "$INSTALL_DIR/server"
install -d -m 0755 "$INSTALL_DIR/server"
tar -C "$REPO_ROOT" \
    --exclude='./data' --exclude='./.venv' --exclude='./.git' \
    --exclude='__pycache__' \
    -cf - . | tar -C "$INSTALL_DIR/server" -xf -

"$PYTHON" -m venv "$INSTALL_DIR/server/.venv"
"$INSTALL_DIR/server/.venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/server/.venv/bin/pip" install -r "$INSTALL_DIR/server/requirements.txt"

chown -R root:root "$INSTALL_DIR"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$DATA_DIR"
chmod 0750 "$DATA_DIR"

if [ ! -e /etc/memorylink.env ]; then
    install -m 0640 -g "$SERVICE_GROUP" \
        "$REPO_ROOT/deploy/memorylink.env.example" /etc/memorylink.env
fi

install -m 0644 "$REPO_ROOT/deploy/memorylink.service" \
    /etc/systemd/system/memorylink.service
systemctl daemon-reload
systemctl enable memorylink.service

cat <<EOF

MemoryLink server installed.

  Code:  $INSTALL_DIR/server
  Data:  $DATA_DIR
  Env:   /etc/memorylink.env
  Unit:  /etc/systemd/system/memorylink.service

Next steps:
  1. Review /etc/memorylink.env (data path, optional MEMORYLINK_GITHUB_REPO).
  2. Install the Apache site from deploy/apache2-memorylink.conf and obtain a
     TLS certificate for your domain.
  3. Start the service:  systemctl start memorylink
  4. Check logs:         journalctl -u memorylink -f
  5. Health check:       curl http://127.0.0.1:8000/api/health

The service listens on 127.0.0.1:8000 only; expose it through Apache.
EOF
