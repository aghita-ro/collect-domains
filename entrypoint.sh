#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR/chrome_profile"

# Generate the VNC password file from VNC_PASSWORD (required for x11vnc -rfbauth).
if [ -n "${VNC_PASSWORD:-}" ]; then
    x11vnc -storepasswd "$VNC_PASSWORD" "$DATA_DIR/.vncpass" >/dev/null 2>&1
else
    echo "WARNING: VNC_PASSWORD not set — generating a random one (noVNC unusable until set)"
    x11vnc -storepasswd "$(head -c 12 /dev/urandom | base64)" "$DATA_DIR/.vncpass" >/dev/null 2>&1
fi

exec supervisord -c /app/supervisord.conf
