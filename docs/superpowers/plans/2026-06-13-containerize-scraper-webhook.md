# Containerize Scraper + Webhook Trigger — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package `scraper.py` into a single self-contained Docker container that runs autonomously, triggered by an authenticated webhook (`POST /run`) called by an external cron, with manual re-login performed through a browser visible via noVNC.

**Architecture:** One container runs `supervisord`, which manages Xvfb (virtual display `:99`), fluxbox, x11vnc, noVNC/websockify (port 7900), and gunicorn serving a Flask app (`webhook.py`, port 8000). Chrome runs headful on the virtual display so the same browser is used for both scraping and manual login (watchable via noVNC). `scraper.py` is refactored to expose callable job functions; the Flask app enforces single-run concurrency with an in-process lock and runs scrapes in a background thread (`202 Accepted`, `409` if busy).

**Tech Stack:** Python 3.12, Selenium + Chromium/chromedriver, Flask + gunicorn, supervisord, Xvfb/x11vnc/noVNC, Docker.

**Reference spec:** `docs/superpowers/specs/2026-06-13-containerize-scraper-webhook-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `scraper.py` (modify) | Scraping/login logic; new module-level job functions `run_scrape_job()` / `run_login_job()`; config via `DATA_DIR`, `CHROME_BIN`, `CHROMEDRIVER_PATH` env |
| `webhook.py` (create) | Flask app: endpoints `/run` `/health` `/status` `/login`; token + IP auth; singleton lock; background scrape thread; random delay |
| `tests/test_webhook.py` (create) | Fast unit tests for auth, `/health`, and 409-concurrency (no browser) |
| `supervisord.conf` (create) | Process definitions: Xvfb, fluxbox, x11vnc, websockify, gunicorn |
| `entrypoint.sh` (create) | Prepare `/data` perms, generate VNC password file, exec supervisord |
| `Dockerfile` (create) | Image: python:3.12-slim + chromium + chromedriver + X stack + supervisor |
| `.dockerignore` (create) | Exclude venv, chrome_profile, archives, git |
| `requirements.txt` (modify) | + flask, gunicorn, pytest |
| `.gitignore` (modify) | + `data/`, `*.vncpass` |
| `.env.example` (create) | Document all env vars (new + existing) without secrets |
| `CLAUDE.md` (modify) | Replace systemd deployment section with Docker model |

---

## Task 1: Add Python dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: View current requirements**

Run: `cat requirements.txt`
Note the existing pinned packages so the new ones follow the same style (pinned or unpinned).

- [ ] **Step 2: Append new dependencies**

Add these lines to `requirements.txt` (match existing pin style; unpinned shown here):

```
flask
gunicorn
pytest
```

- [ ] **Step 3: Install into the local venv**

Run: `source venv/bin/activate && pip install -r requirements.txt`
Expected: flask, gunicorn, pytest install successfully.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "deps: add flask, gunicorn, pytest for webhook + container"
```

---

## Task 2: Refactor scraper.py config & driver for container

Makes file paths and the Chrome/chromedriver binaries configurable via env so the same code runs locally and in the container.

**Files:**
- Modify: `scraper.py:18-20` (dotenv loading), `scraper.py:72-112` (`__init__`), `scraper.py:341` (`login_manual` signature), `scraper.py:378` (wait loop)

- [ ] **Step 1: Replace the dotenv-loading block**

Replace `scraper.py:20`:

```python
load_dotenv()
```

with:

```python
# Data directory: a mounted volume in the container, the script dir for local dev.
# Holds .env, cookies.json, chrome_profile/, and domains*.txt output.
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))

# Load .env from DATA_DIR if present, otherwise fall back to default search.
_dotenv_path = os.path.join(DATA_DIR, ".env")
if os.path.exists(_dotenv_path):
    load_dotenv(_dotenv_path)
else:
    load_dotenv()
```

- [ ] **Step 2: Make the top-level `ChromeDriverManager` import lazy**

Replace `scraper.py:8`:

```python
from webdriver_manager.chrome import ChromeDriverManager
```

with:

```python
# webdriver-manager is only needed for local dev (auto-downloads chromedriver).
# In the container we use the system chromedriver via CHROMEDRIVER_PATH, so the
# import is deferred to __init__ to avoid a hard dependency in the image.
```

- [ ] **Step 3: Update `__init__` signature and paths**

Replace `scraper.py:72`:

```python
    def __init__(self, username, password, headless=False):
```

with:

```python
    def __init__(self, username, password, headless=False, work_dir=None):
```

Then replace `scraper.py:78-79`:

```python
        # Create a persistent profile directory
        self.profile_dir = os.path.join(os.getcwd(), "chrome_profile")
```

with:

```python
        # Create a persistent profile directory under the data dir
        self.work_dir = work_dir or DATA_DIR
        self.profile_dir = os.path.join(self.work_dir, "chrome_profile")
```

- [ ] **Step 4: Set the Chrome binary location**

After `scraper.py:85` (`chrome_options = Options()`), add:

```python
        chrome_bin = os.getenv("CHROME_BIN", "")
        if chrome_bin:
            chrome_options.binary_location = chrome_bin
```

- [ ] **Step 5: Use system chromedriver when available**

Replace `scraper.py:107-109`:

```python
        print("Initializing Chrome with persistent profile...")
        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=chrome_options)
```

with:

```python
        print("Initializing Chrome with persistent profile...")
        chromedriver_path = os.getenv("CHROMEDRIVER_PATH", "")
        if chromedriver_path:
            service = Service(executable_path=chromedriver_path)
        else:
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=chrome_options)
```

- [ ] **Step 6: Update cookies_file path**

Replace `scraper.py:112`:

```python
        self.cookies_file = os.path.join(os.getcwd(), "cookies.json")
```

with:

```python
        self.cookies_file = os.path.join(self.work_dir, "cookies.json")
```

- [ ] **Step 7: Parametrize login timeout**

Replace `scraper.py:341`:

```python
    def login_manual(self):
```

with:

```python
    def login_manual(self, wait_seconds=180):
```

Then replace `scraper.py:374` and `scraper.py:378`:

```python
            print("\nScript will wait up to 3 minutes...")
```
```python
            for i in range(180):
```

with:

```python
            print(f"\nScript will wait up to {wait_seconds // 60} minutes...")
```
```python
            for i in range(wait_seconds):
```

- [ ] **Step 8: Verify the module still imports**

Run: `source venv/bin/activate && python -c "import scraper; print('ok')"`
Expected: prints `ok` (the lazy import means no webdriver-manager call at import time).

- [ ] **Step 9: Commit**

```bash
git add scraper.py
git commit -m "refactor: make scraper paths and chrome binaries env-configurable"
```

---

## Task 3: Extract scrape & login orchestration into callable functions

Moves the `__main__` body into reusable functions that the webhook can call and return structured results.

**Files:**
- Modify: `scraper.py` (add two module-level functions before `if __name__ == "__main__":` at line 516; replace the `__main__` block)

- [ ] **Step 1: Add `run_scrape_job` and `run_login_job` functions**

Insert the following immediately before `scraper.py:516` (`# Main execution`):

```python
def _save_domains_to_files(domains):
    """Write domains to domains.txt + a timestamped archive under DATA_DIR."""
    output_file = os.path.join(DATA_DIR, "domains.txt")
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    timestamped_file = os.path.join(DATA_DIR, f"domains_{timestamp}.txt")
    for path in (output_file, timestamped_file):
        with open(path, 'w', encoding='utf-8') as f:
            for domain in domains:
                f.write(f"{domain}\n")
    print(f"\n✓ Saved {len(domains)} domains to:\n  - {output_file}\n  - {timestamped_file}")
    return output_file, timestamped_file


def run_scrape_job(cron=True):
    """Run one full scrape. Returns a result dict. Does NOT do interactive login;
    if the session is expired it emails an alert and returns session_valid=False."""
    username = os.getenv("SCRAPER_USERNAME", "")
    password = os.getenv("SCRAPER_PASSWORD", "")
    result = {"status": "error", "domains_count": 0, "session_valid": None, "error": None}
    scraper = None
    try:
        scraper = DomainsScrapperSelenium(username, password, headless=False)
        db_connected = scraper.connect_db()

        if not scraper.is_logged_in():
            result["session_valid"] = False
            result["status"] = "login_required"
            print("\n✗ Session expired - manual login required")
            if cron:
                send_alert_email(
                    "Domains Scrapper: login required",
                    "The session has expired and manual login is needed.\n\n"
                    "Open the noVNC console (private/VPN), then trigger POST /login\n"
                    "and complete the email verification / CAPTCHA in the browser.\n\n"
                    "The next scheduled run will work again afterwards."
                )
            return result

        result["session_valid"] = True
        domains = scraper.get_all_auction_domains()

        if domains and db_connected:
            scraper.save_domains_to_db(domains)
        elif domains and not db_connected:
            print("\n⚠ Database not connected - saving to files only")

        if domains:
            _save_domains_to_files(domains)
            summary = scraper.print_yearly_summary() if db_connected else None
            result["status"] = "ok"
            result["domains_count"] = len(domains)
            if cron:
                body = (
                    f"Successfully collected {len(domains)} domains.\n"
                    f"{'Database: saved' if db_connected else 'Database: not connected (files only)'}"
                )
                if db_connected and summary:
                    body += (
                        f"\n\n--- {summary['year']} coverage ---\n"
                        f"Days covered: {summary['days_covered']}/{summary['days_in_year']}\n"
                        f"Days remaining: {summary['days_remaining']}"
                    )
                send_alert_email(f"Domains Scrapper: {len(domains)} domains collected", body)
        else:
            result["status"] = "no_domains"
            print("\n✗ No domains collected")
            if db_connected:
                scraper.print_yearly_summary()
            if cron:
                send_alert_email(
                    "Domains Scrapper: no domains collected",
                    "The scraper ran but collected 0 domains.\n"
                    "This may indicate a page structure change or a session issue."
                )
        return result

    except Exception as e:
        result["error"] = str(e)
        print(f"\n✗ Unexpected error: {str(e)}")
        import traceback
        traceback.print_exc()
        if cron:
            send_alert_email(
                "Domains Scrapper: unexpected error",
                f"The scraper crashed with an error:\n\n{str(e)}"
            )
        return result
    finally:
        if scraper:
            scraper.close()


def run_login_job(wait_seconds=600):
    """Open the browser for manual login (watch via noVNC). Returns a result dict."""
    username = os.getenv("SCRAPER_USERNAME", "")
    password = os.getenv("SCRAPER_PASSWORD", "")
    result = {"success": False, "error": None}
    scraper = None
    try:
        scraper = DomainsScrapperSelenium(username, password, headless=False)
        if scraper.is_logged_in():
            print("\n✓ Already logged in - nothing to do")
            result["success"] = True
            return result
        result["success"] = scraper.login_manual(wait_seconds=wait_seconds)
        return result
    except Exception as e:
        result["error"] = str(e)
        import traceback
        traceback.print_exc()
        return result
    finally:
        if scraper:
            scraper.close()
```

- [ ] **Step 2: Replace the `__main__` block with a thin dispatcher**

Replace everything from `scraper.py:517` (`    parser = argparse.ArgumentParser(...)`) through the end of file (`scraper.py:644`) with:

```python
    parser = argparse.ArgumentParser(description="Domains Scrapper")
    parser.add_argument("--cron", action="store_true",
                        help="Run a scrape non-interactively, email alerts on failure")
    parser.add_argument("--login", action="store_true",
                        help="Open the browser for manual login (watch via noVNC)")
    args = parser.parse_args()

    try:
        if args.login:
            run_login_job(wait_seconds=600)
        else:
            # Default and --cron both run a scrape; interactive login is no longer
            # done here (use --login or the /login endpoint instead).
            run_scrape_job(cron=args.cron)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
```

- [ ] **Step 3: Verify the module imports and exposes the functions**

Run:
```bash
source venv/bin/activate && python -c "import scraper; print(scraper.run_scrape_job, scraper.run_login_job)"
```
Expected: prints two `<function ...>` references, no error.

- [ ] **Step 4: Commit**

```bash
git add scraper.py
git commit -m "refactor: expose run_scrape_job/run_login_job; thin __main__ dispatcher"
```

---

## Task 4: Create the Flask webhook app (TDD)

**Files:**
- Create: `tests/test_webhook.py`
- Create: `webhook.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_webhook.py`:

```python
import importlib
import os
import threading

import pytest


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WEBHOOK_TOKEN", "secret123")
    monkeypatch.setenv("WEBHOOK_ALLOWED_IPS", "")  # allowlist disabled for most tests
    monkeypatch.setenv("RANDOM_DELAY_MAX_MIN", "0")
    import webhook
    importlib.reload(webhook)
    # Never actually run a scrape during tests.
    monkeypatch.setattr(webhook.scraper_mod, "run_scrape_job",
                        lambda cron=True: {"status": "ok", "domains_count": 0,
                                           "session_valid": True, "error": None})
    webhook.app.config["TESTING"] = True
    return webhook.app.test_client(), webhook


def test_health_no_auth(client):
    c, _ = client
    assert c.get("/health").status_code == 200


def test_run_requires_token(client):
    c, _ = client
    assert c.post("/run").status_code == 401


def test_run_accepts_with_token(client):
    c, mod = client
    r = c.post("/run", headers={"X-Webhook-Token": "secret123"})
    assert r.status_code == 202
    # Let the background thread finish so it releases the lock.
    for t in threading.enumerate():
        if t.name == "scrape-job":
            t.join(timeout=5)


def test_run_conflict_when_locked(client):
    c, mod = client
    mod._lock.acquire()  # simulate an in-progress run
    try:
        r = c.post("/run", headers={"X-Webhook-Token": "secret123"})
        assert r.status_code == 409
    finally:
        mod._lock.release()


def test_run_ip_allowlist(monkeypatch):
    monkeypatch.setenv("WEBHOOK_TOKEN", "secret123")
    monkeypatch.setenv("WEBHOOK_ALLOWED_IPS", "203.0.113.5")
    monkeypatch.setenv("RANDOM_DELAY_MAX_MIN", "0")
    import webhook
    importlib.reload(webhook)
    c = webhook.app.test_client()
    # Default test client remote_addr (127.0.0.1) is not in the allowlist.
    r = c.post("/run", headers={"X-Webhook-Token": "secret123"})
    assert r.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `source venv/bin/activate && pytest tests/test_webhook.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webhook'`.

- [ ] **Step 3: Create `webhook.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `source venv/bin/activate && pytest tests/test_webhook.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add webhook.py tests/test_webhook.py
git commit -m "feat: add Flask webhook app with token+IP auth and singleton lock"
```

---

## Task 5: Create supervisord.conf

**Files:**
- Create: `supervisord.conf`

- [ ] **Step 1: Write the supervisor config**

```ini
[supervisord]
nodaemon=true
user=root
logfile=/dev/stdout
logfile_maxbytes=0
pidfile=/tmp/supervisord.pid

[program:xvfb]
command=Xvfb :99 -screen 0 1920x1080x24 -ac
autorestart=true
priority=100
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0

[program:fluxbox]
command=fluxbox
environment=DISPLAY=":99"
autorestart=true
priority=200
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0

[program:x11vnc]
command=x11vnc -display :99 -forever -shared -rfbauth /data/.vncpass -rfbport 5900
autorestart=true
priority=300
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0

[program:novnc]
command=websockify --web=/usr/share/novnc 7900 localhost:5900
autorestart=true
priority=400
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0

[program:gunicorn]
command=gunicorn --workers 1 --threads 4 --timeout 900 --bind 0.0.0.0:8000 webhook:app
directory=/app
environment=DISPLAY=":99"
autorestart=true
priority=500
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
```

- [ ] **Step 2: Commit**

```bash
git add supervisord.conf
git commit -m "feat: supervisord config for Xvfb, x11vnc, noVNC, gunicorn"
```

---

## Task 6: Create entrypoint.sh

**Files:**
- Create: `entrypoint.sh`

- [ ] **Step 1: Write the entrypoint**

```bash
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
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x entrypoint.sh`

- [ ] **Step 3: Commit**

```bash
git add entrypoint.sh
git commit -m "feat: container entrypoint (VNC password + supervisord launch)"
```

---

## Task 7: Create the Dockerfile

**Files:**
- Create: `Dockerfile`

- [ ] **Step 1: Write the Dockerfile**

```dockerfile
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    DATA_DIR=/data \
    CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver \
    PYTHONUNBUFFERED=1

# Browser + matched driver, virtual display + VNC stack, process manager.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium chromium-driver \
        xvfb x11vnc fluxbox \
        novnc websockify \
        supervisor \
        fonts-liberation \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper.py webhook.py supervisord.conf entrypoint.sh ./
RUN chmod +x entrypoint.sh

VOLUME ["/data"]
EXPOSE 8000 7900

ENTRYPOINT ["/app/entrypoint.sh"]
```

- [ ] **Step 2: Verify novnc web path**

Some Debian `novnc` packages serve from `/usr/share/novnc` with the entry page at `vnc.html` (not `index.html`). After building (Task 10), confirm `http://host:7900/vnc.html` loads. If the package ships `index.html` instead, both work. No code change needed; this is a verification note.

- [ ] **Step 3: Commit**

```bash
git add Dockerfile
git commit -m "feat: Dockerfile (chromium + X/VNC stack + supervisor)"
```

---

## Task 8: Create .dockerignore and update .gitignore / .env.example

**Files:**
- Create: `.dockerignore`
- Create: `.env.example`
- Modify: `.gitignore`

- [ ] **Step 1: Write `.dockerignore`**

```
venv/
chrome_profile/
cookies.json
domains.txt
domains_*.txt
debug_*.html
debug_*.txt
__pycache__/
.git/
data/
docs/
*.pyc
```

- [ ] **Step 2: Append to `.gitignore`**

Add these lines to `.gitignore`:

```
data/
*.vncpass
```

- [ ] **Step 3: Write `.env.example`** (no real secrets — placeholders only)

```
# --- Auction platform login ---
SCRAPER_USERNAME=
SCRAPER_PASSWORD=

# --- PostgreSQL ---
DB_HOST=
DB_PORT=5432
DB_NAME=
DB_USER=
DB_PASSWORD=

# --- Mailgun (EU endpoint) ---
MAILGUN_DOMAIN=
MAILGUN_API_KEY=
EMAIL_FROM=
EMAIL_TO=

# --- Webhook / container ---
WEBHOOK_TOKEN=change-me-long-random-secret
WEBHOOK_ALLOWED_IPS=
VNC_PASSWORD=change-me
RANDOM_DELAY_MAX_MIN=0
```

- [ ] **Step 4: Commit**

```bash
git add .dockerignore .gitignore .env.example
git commit -m "chore: dockerignore, gitignore data/, .env.example"
```

---

## Task 9: Build and smoke-test the container

This task is verification-only (no new code). Requires Docker installed on the host.

**Files:** none

- [ ] **Step 1: Build the image**

Run: `docker build -t collect-domains .`
Expected: build succeeds; final image tagged `collect-domains`.

- [ ] **Step 2: Prepare a data dir with config**

Run:
```bash
mkdir -p /tmp/scraper-data
cp .env /tmp/scraper-data/.env   # a real .env with credentials + WEBHOOK_TOKEN + VNC_PASSWORD
```
Ensure `/tmp/scraper-data/.env` sets `WEBHOOK_TOKEN`, `VNC_PASSWORD`, and (optionally) `RANDOM_DELAY_MAX_MIN`.

- [ ] **Step 3: Run the container**

Run:
```bash
docker run -d --name collect-domains \
  -p 8000:8000 -p 127.0.0.1:7900:7900 \
  -v /tmp/scraper-data:/data \
  --shm-size=1g \
  collect-domains
```
Note: `--shm-size=1g` avoids Chrome crashes; noVNC bound to localhost only (private).

- [ ] **Step 4: Check processes started**

Run: `docker logs collect-domains | tail -n 30`
Expected: supervisord shows xvfb, fluxbox, x11vnc, novnc, gunicorn entered RUNNING.

- [ ] **Step 5: Health check**

Run: `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/health`
Expected: `200`

- [ ] **Step 6: Auth checks on /run**

Run:
```bash
curl -s -o /dev/null -w "no-token:%{http_code}\n" -X POST http://localhost:8000/run
curl -s -o /dev/null -w "with-token:%{http_code}\n" -X POST "http://localhost:8000/run?token=YOUR_TOKEN"
```
Expected: `no-token:401` and `with-token:202`.

- [ ] **Step 7: Concurrency check**

Immediately re-run the token request while the first scrape is still running:
Run: `curl -s -o /dev/null -w "%{http_code}\n" -X POST "http://localhost:8000/run?token=YOUR_TOKEN"`
Expected: `409` (already running). If the first run finished too fast, this may be `202`; set `RANDOM_DELAY_MAX_MIN` to a small value to widen the window when testing.

- [ ] **Step 8: noVNC reachable**

Run: `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:7900/vnc.html`
Expected: `200` (or try `http://localhost:7900/` — see Task 7 Step 2). Open it in a browser, enter `VNC_PASSWORD`, confirm you see the fluxbox desktop.

- [ ] **Step 9: Manual login end-to-end**

Run: `curl -s -X POST "http://localhost:8000/login?token=YOUR_TOKEN"` (this blocks up to 10 min).
While it blocks: open noVNC, watch Chrome open the login page, complete email verification / CAPTCHA. On success the call returns `{"success": true}` and `/tmp/scraper-data/cookies.json` is written/updated.

- [ ] **Step 10: Full scrape with a valid session**

Run: `curl -s -X POST "http://localhost:8000/run?token=YOUR_TOKEN"` then watch logs:
Run: `docker logs -f collect-domains`
Expected: scrape runs, `domains.txt` + a `domains_*.txt` appear in `/tmp/scraper-data`, success email sent.

- [ ] **Step 11: Tear down test container**

Run: `docker rm -f collect-domains`

---

## Task 10: Update CLAUDE.md deployment docs

**Files:**
- Modify: `CLAUDE.md` (Running, Deployment, Configuration sections)

- [ ] **Step 1: Replace the "Running" section**

Replace the `## Running` section body with:

````markdown
The app runs as a single Docker container. A scrape is triggered by an
authenticated webhook (`POST /run`); manual login is done via `POST /login`
watched through noVNC.

```bash
# Build
docker build -t collect-domains .

# Run (data dir holds .env, cookies.json, chrome_profile/, domains*.txt)
docker run -d --name collect-domains \
  -p 8000:8000 -p 127.0.0.1:7900:7900 \
  -v /opt/scraper-data:/data --shm-size=1g \
  collect-domains

# Trigger a scrape
curl -X POST "http://HOST:8000/run?token=$WEBHOOK_TOKEN"

# Manual login (then watch noVNC at http://localhost:7900/vnc.html via VPN/tunnel)
curl -X POST "http://HOST:8000/login?token=$WEBHOOK_TOKEN"
```

Local dev without Docker still works: `python scraper.py` (scrape) or
`python scraper.py --login` (manual login window).
````

- [ ] **Step 2: Replace the "Deployment (EC2)" section**

Replace it with a Docker-based description:

````markdown
## Deployment

The container is host-agnostic. The daily run is triggered externally by
cron.2l.ro, which issues `POST http://HOST:8000/run?token=...` on a schedule.

- Webhook (`/run`) is protected by `WEBHOOK_TOKEN` + `WEBHOOK_ALLOWED_IPS`
  (the cron server's IP).
- noVNC (port 7900) MUST stay private — bind it to localhost/VPN only, never
  expose it publicly (it controls a logged-in browser).
- Session persistence (`cookies.json`, `chrome_profile/`) and output
  (`domains*.txt`) live on the mounted `/data` volume and survive restarts.

**When the session expires:** an email alert is sent. Re-login with
`POST /login` and complete verification in noVNC.

**Endpoints:** `/run` (public, token+IP), `/health` (public), `/status`
(token), `/login` (token).
````

- [ ] **Step 3: Add the new env vars to the "Configuration" section**

Under the `.env` list, add:

```markdown
- `WEBHOOK_TOKEN` — secret token required to call `/run`, `/status`, `/login`
- `WEBHOOK_ALLOWED_IPS` — comma-separated IP allowlist for `/run` (empty = allow all)
- `VNC_PASSWORD` — password for the noVNC console
- `RANDOM_DELAY_MAX_MIN` — max random pre-scrape delay in minutes (0 = disabled)
```

- [ ] **Step 4: Note the obsolete systemd files**

Add a short note that `setup-ec2.sh`, `run.sh`, and `systemd/` describe the
previous (pre-container) deployment and are retained for reference only.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for Docker + webhook deployment"
```

---

## Self-Review

**Spec coverage:**
- Single self-contained container → Tasks 5–8 (supervisord, entrypoint, Dockerfile).
- Webhook `POST /run`, async 202, 409 if busy → Task 4.
- Token + IP allowlist → Task 4 (`_token_ok`, `_ip_ok`), tested.
- `/health`, `/status`, `/login` → Task 4.
- Manual login via noVNC → Tasks 3 (`run_login_job`), 5 (x11vnc/novnc), 9 (Step 9).
- Random delay (`RANDOM_DELAY_MAX_MIN`) → Task 4 (`_background_scrape`).
- Xvfb `:99` headful Chrome → Task 2 (no `--headless`, `DISPLAY` env), Task 5/7.
- Persistence volumes → Tasks 2 (`DATA_DIR` paths), 7 (`VOLUME`), 9 (`-v`).
- Config env vars → Task 8 (`.env.example`), Task 10 (docs).
- Code changes in scraper.py (driver, login refactor, scrape extraction, `--cron`) → Tasks 2, 3.

**Placeholder scan:** No TBD/TODO; all code blocks complete. `YOUR_TOKEN`/`HOST` in Task 9/10 are intentional runtime values, not plan placeholders.

**Type consistency:** `run_scrape_job(cron=...)` returns `{status, domains_count, session_valid, error}` (Task 3) and is consumed in `_background_scrape` via `.get("error")`/`.get("session_valid")` (Task 4) — consistent. `run_login_job(wait_seconds=...)` returns `{success, error}` (Task 3), consumed in `/login` via `.get("success")` (Task 4) — consistent. `_lock` / `_state` names match between `webhook.py` and `tests/test_webhook.py`. `login_manual(wait_seconds=...)` signature (Task 2) matches the call in `run_login_job` (Task 3).
