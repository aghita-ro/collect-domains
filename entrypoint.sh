#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
mkdir -p "$DATA_DIR/chrome_profile"

# noVNC runs without a password (x11vnc -nopw). The console is bound to
# localhost/VPN only — never expose port 7900 publicly.

exec supervisord -c /app/supervisord.conf
