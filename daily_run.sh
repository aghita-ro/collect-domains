#!/usr/bin/env bash
# Run the collect-domains scrape at most once per *successful* day.
#
# Designed to be called every ~5 minutes by cron: it exits immediately if today
# already succeeded, so on a machine with unpredictable up-time the scrape runs
# within ~5 min of boot and then not again until the next day.
#
# "Success" = the run logged in and completed (session_valid == true), regardless
# of how many domains were found — otherwise a genuinely empty auction day would
# make cron retry every 5 min all day long.
set -uo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin

REPO_DIR="/home/aghita/112tech/collect-domains"
DATA_DIR="$REPO_DIR/data"
CONTAINER="collect-domains"
WEB_PORT="8000"
MARKER="$DATA_DIR/.last_success_date"
LOCK="$DATA_DIR/.daily_run.lock"
LOG="$DATA_DIR/daily_run.log"
TODAY="$(date +%F)"

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# 0) Fast path: already succeeded today.
[ "$(cat "$MARKER" 2>/dev/null)" = "$TODAY" ] && exit 0

# Prevent overlapping invocations (a run waiting on MFA can exceed 5 min).
exec 9>"$LOCK"
flock -n 9 || exit 0

# Re-check after acquiring the lock.
[ "$(cat "$MARKER" 2>/dev/null)" = "$TODAY" ] && exit 0

TOKEN="$(sed -n 's/^WEBHOOK_TOKEN=//p' "$DATA_DIR/.env" | head -n1)"
if [ -z "$TOKEN" ]; then
    log "ERROR: WEBHOOK_TOKEN empty in $DATA_DIR/.env"
    exit 1
fi

# 1) Ensure the container is up. It does NOT auto-start at boot (restart policy
#    "no") — this script owns its lifecycle and stops it again after success.
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    log "container not running -> starting"
    docker start "$CONTAINER" >/dev/null 2>&1 \
        || DATA_DIR="$DATA_DIR" "$REPO_DIR/docker_run.sh" --no-login >/dev/null 2>&1
fi

# 2) Wait for the webhook to be healthy (~60s).
healthy=0
for _ in $(seq 1 30); do
    curl -fsS "http://localhost:$WEB_PORT/health" >/dev/null 2>&1 && { healthy=1; break; }
    sleep 2
done
if [ "$healthy" != 1 ]; then
    log "webhook not healthy yet -> retry next tick"
    exit 0
fi

status_json() { curl -s "http://localhost:$WEB_PORT/status?token=$TOKEN"; }
top()  { python3 -c "import sys,json; print((json.load(sys.stdin) or {}).get('$1',''))" 2>/dev/null; }
res()  { python3 -c "import sys,json; print(((json.load(sys.stdin) or {}).get('last_result') or {}).get('$1',''))" 2>/dev/null; }

# Baseline so we can tell this run's completion apart from a previous one.
old_started="$(status_json | top last_run_started)"

# 3) Trigger the scrape (async; returns 202).
log "triggering /run (resp: $(curl -s -X POST "http://localhost:$WEB_PORT/run?token=$TOKEN"))"

# 4) Poll until a NEW run finishes (max ~10 min).
final=""
for _ in $(seq 1 120); do
    sleep 5
    s="$(status_json)"
    [ "$(printf '%s' "$s" | top running)" = "False" ] \
        && [ "$(printf '%s' "$s" | top last_run_started)" != "$old_started" ] \
        && { final="$s"; break; }
done

if [ -z "$final" ]; then
    log "run did not finish in time -> retry next tick"
    exit 0
fi

sv="$(printf '%s' "$final" | res session_valid)"
st="$(printf '%s' "$final" | res status)"
cnt="$(printf '%s' "$final" | res domains_count)"

if [ "$sv" = "True" ]; then
    echo "$TODAY" > "$MARKER"
    # Done for today: stop the container so it isn't left running idle.
    docker stop "$CONTAINER" >/dev/null 2>&1
    log "SUCCESS day=$TODAY status=$st domains=$cnt -> marked done, container stopped"
else
    log "finished but not logged in (status=$st) -> retry next tick"
fi
