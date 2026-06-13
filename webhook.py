import os
import random
import threading
import time

from flask import Flask, request, jsonify

import scraper as scraper_mod

# scraper imports load_dotenv already; reuse its env.
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "")
WEBHOOK_ALLOWED_IPS = [ip.strip() for ip in os.getenv("WEBHOOK_ALLOWED_IPS", "").split(",") if ip.strip()]
RANDOM_DELAY_MAX_MIN = int(os.getenv("RANDOM_DELAY_MAX_MIN", "0"))

app = Flask(__name__)

# Single global lock: only one browser operation (scrape OR login) at a time.
_lock = threading.Lock()
_state = {
    "running": False,
    "last_run_started": None,
    "last_result": None,
    "last_error": None,
    "session_valid": None,
}


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or ""


def _token_ok():
    token = request.headers.get("X-Webhook-Token") or request.args.get("token", "")
    return bool(WEBHOOK_TOKEN) and token == WEBHOOK_TOKEN


def _ip_ok():
    if not WEBHOOK_ALLOWED_IPS:
        return True
    return _client_ip() in WEBHOOK_ALLOWED_IPS


def _background_scrape():
    try:
        if RANDOM_DELAY_MAX_MIN > 0:
            delay = random.randint(0, RANDOM_DELAY_MAX_MIN * 60)
            print(f"[webhook] random pre-scrape delay: {delay}s")
            time.sleep(delay)
        result = scraper_mod.run_scrape_job(cron=True)
        _state["last_result"] = result
        _state["last_error"] = result.get("error")
        _state["session_valid"] = result.get("session_valid")
    except Exception as e:  # defensive: never leave the lock held
        _state["last_error"] = str(e)
    finally:
        _state["running"] = False
        _lock.release()


@app.route("/health")
def health():
    return "OK", 200


@app.route("/run", methods=["POST"])
def run():
    if not _ip_ok():
        return jsonify(error="forbidden"), 403
    if not _token_ok():
        return jsonify(error="unauthorized"), 401
    if not _lock.acquire(blocking=False):
        return jsonify(error="already running"), 409
    _state["running"] = True
    _state["last_run_started"] = time.strftime("%Y-%m-%d %H:%M:%S")
    threading.Thread(target=_background_scrape, name="scrape-job", daemon=True).start()
    return jsonify(status="accepted"), 202


@app.route("/login", methods=["POST"])
def login():
    if not _token_ok():
        return jsonify(error="unauthorized"), 401
    if not _lock.acquire(blocking=False):
        return jsonify(error="busy"), 409
    try:
        result = scraper_mod.run_login_job(wait_seconds=600)
        _state["session_valid"] = result.get("success")
        return jsonify(result), (200 if result.get("success") else 504)
    finally:
        _lock.release()


@app.route("/status")
def status():
    if not _token_ok():
        return jsonify(error="unauthorized"), 401
    return jsonify(_state), 200
