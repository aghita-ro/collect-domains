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
# Login after start:
#   (default)     auto — start the manual-login flow only when no saved session
#                 (cookies.json) exists yet.
#   --login       force the login flow even if a session exists (re-login).
#   --no-login    never start the login flow.
#   LOGIN=1|0     same as --login / --no-login via env var.
#
# Example: DATA_DIR=/opt/scraper-data WEB_PORT=8080 ./docker_run.sh
#          ./docker_run.sh --login          # restart and re-login
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="${IMAGE:-collect-domains}"
CONTAINER="${CONTAINER:-collect-domains}"
DATA_DIR="${DATA_DIR:-$(pwd)/data}"
WEB_PORT="${WEB_PORT:-8000}"
NOVNC_PORT="${NOVNC_PORT:-7900}"
NOVNC_BIND="${NOVNC_BIND:-127.0.0.1}"

LOGIN_OPT="${LOGIN:-auto}"
case "${1:-}" in
    --login)    LOGIN_OPT=1 ;;
    --no-login) LOGIN_OPT=0 ;;
    "")         ;;
    *) echo "Unknown argument: $1 (use --login or --no-login)"; exit 1 ;;
esac

mkdir -p "$DATA_DIR"

if [ ! -f "$DATA_DIR/.env" ]; then
    echo "ERROR: $DATA_DIR/.env not found."
    echo "Create it from the template, e.g.:"
    echo "  cp .env.example \"$DATA_DIR/.env\" && \${EDITOR:-nano} \"$DATA_DIR/.env\""
    echo "It must set at least SCRAPER_USERNAME/PASSWORD, WEBHOOK_TOKEN, and"
    echo "GMAIL_IMAP_USER/PASSWORD (for automatic 2FA login)."
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

# Pull the token straight from the .env so the printed commands are
# copy-paste ready and don't depend on the caller's shell env.
TOKEN="$(sed -n 's/^WEBHOOK_TOKEN=//p' "$DATA_DIR/.env" | head -n1)"

if [ -z "$TOKEN" ]; then
    echo "WARNING: WEBHOOK_TOKEN is empty in $DATA_DIR/.env — /run, /login and /status will reject every call."
fi

# Wait for the webhook to answer /health before doing anything with it.
printf 'Waiting for the webhook to come up'
ready=0
for _ in $(seq 1 30); do
    if curl -fsS "http://localhost:${WEB_PORT}/health" >/dev/null 2>&1; then
        ready=1; break
    fi
    printf '.'; sleep 1
done
echo
if [ "$ready" != "1" ]; then
    echo "Webhook did not become healthy in time. Check: docker logs -f $CONTAINER"
    exit 1
fi
echo "Webhook is up."

echo
echo "Started. Useful commands (token already filled in):"
echo "  Logs:     docker logs -f $CONTAINER"
echo "  Health:   curl http://localhost:${WEB_PORT}/health"
echo "  Login:    curl -X POST \"http://localhost:${WEB_PORT}/login?token=${TOKEN}\""
echo "  Trigger:  curl -X POST \"http://localhost:${WEB_PORT}/run?token=${TOKEN}\""
echo "  Status:   curl \"http://localhost:${WEB_PORT}/status?token=${TOKEN}\""
echo "  In your shell:  export WEBHOOK_TOKEN=${TOKEN}"

# Decide whether to start the manual-login flow.
need_login=0
case "$LOGIN_OPT" in
    1) need_login=1 ;;
    0) need_login=0 ;;
    auto) [ -f "$DATA_DIR/cookies.json" ] || need_login=1 ;;
esac

if [ "$need_login" = "1" ] && [ -z "$TOKEN" ]; then
    echo
    echo "Skipping login: no WEBHOOK_TOKEN set. Fix $DATA_DIR/.env and re-run with --login."
    need_login=0
fi

if [ "$need_login" = "1" ]; then
    echo
    if [ "$LOGIN_OPT" = "auto" ]; then
        echo "No saved session (cookies.json) found — starting the manual login flow."
    else
        echo "Starting the manual login flow (--login)."
    fi
    echo "  1) Open the noVNC console:  http://localhost:${NOVNC_PORT}/vnc.html"
    echo "     (no VNC password — keep this port private: VPN/localhost only)"
    echo "  2) In that browser, complete the CAPTCHA / email verification."
    echo
    echo "Login started — it waits up to ~10 min for you to finish. Leave this running..."
    # 504 (timeout) returns non-zero; don't let it abort the script.
    curl -sS -X POST "http://localhost:${WEB_PORT}/login?token=${TOKEN}" || true
    echo
    echo "Login flow finished (check the JSON above for \"success\": true)."
else
    echo
    echo "To log in / re-login later:  ./docker_run.sh --login"
fi
