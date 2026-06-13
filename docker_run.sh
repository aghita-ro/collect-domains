#!/usr/bin/env bash
# Start (or restart) the collect-domains container.
#
# Defaults (override via env vars):
#   IMAGE=collect-domains          image to run
#   CONTAINER=collect-domains      container name
#   DATA_DIR=<repo>/data           host dir mounted at /data (holds .env, cookies.json, chrome_profile/, domains*.txt)
#   WEB_PORT=8000                  host port for the webhook (/run, /health, /status, /login)
#   NOVNC_PORT=7900                host port for the noVNC console
#   NOVNC_BIND=127.0.0.1           interface noVNC binds to — keep it private! (VPN/tunnel only)
#
# Example: DATA_DIR=/opt/scraper-data WEB_PORT=8080 ./docker_run.sh
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="${IMAGE:-collect-domains}"
CONTAINER="${CONTAINER:-collect-domains}"
DATA_DIR="${DATA_DIR:-$(pwd)/data}"
WEB_PORT="${WEB_PORT:-8000}"
NOVNC_PORT="${NOVNC_PORT:-7900}"
NOVNC_BIND="${NOVNC_BIND:-127.0.0.1}"

mkdir -p "$DATA_DIR"

if [ ! -f "$DATA_DIR/.env" ]; then
    echo "ERROR: $DATA_DIR/.env not found."
    echo "Create it from the template, e.g.:"
    echo "  cp .env.example \"$DATA_DIR/.env\" && \${EDITOR:-nano} \"$DATA_DIR/.env\""
    echo "It must set at least SCRAPER_USERNAME/PASSWORD, WEBHOOK_TOKEN and VNC_PASSWORD."
    exit 1
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "ERROR: image '$IMAGE' not found. Build it first with ./docker_build.sh"
    exit 1
fi

# Make re-running idempotent: drop any existing container with the same name.
if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "Removing existing container '$CONTAINER'..."
    docker rm -f "$CONTAINER" >/dev/null
fi

echo "Starting '$CONTAINER' (image '$IMAGE', data '$DATA_DIR')..."
docker run -d \
    --name "$CONTAINER" \
    --restart unless-stopped \
    -p "${WEB_PORT}:8000" \
    -p "${NOVNC_BIND}:${NOVNC_PORT}:7900" \
    -v "$DATA_DIR:/data" \
    --shm-size=1g \
    "$IMAGE" >/dev/null

echo
echo "Started. Next steps:"
echo "  Logs:     docker logs -f $CONTAINER"
echo "  Health:   curl http://localhost:${WEB_PORT}/health"
echo "  Login:    curl -X POST \"http://localhost:${WEB_PORT}/login?token=\$WEBHOOK_TOKEN\""
echo "            (then watch noVNC at http://localhost:${NOVNC_PORT}/vnc.html via VPN/tunnel)"
echo "  Trigger:  curl -X POST \"http://localhost:${WEB_PORT}/run?token=\$WEBHOOK_TOKEN\""
