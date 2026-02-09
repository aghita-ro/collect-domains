#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "$(date) - Pulling latest changes..."
git pull --ff-only origin main

echo "$(date) - Running scraper..."
"$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/scraper.py" --cron

echo "$(date) - Done"
