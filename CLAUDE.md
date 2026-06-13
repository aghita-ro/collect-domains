# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Python scraper (`scraper.py`) that collects expiring `.ro` domain names from the eureg.ro auction platform. It uses Selenium with Chrome to handle login (which requires manual CAPTCHA/email verification), paginates through a DataTables-based auction list, and saves results both to text files and a PostgreSQL database.

It runs as a single self-contained Docker container: a Flask webhook (`webhook.py`) triggers a scrape on demand (called by an external cron), and manual re-login is done through a browser visible via noVNC. Chrome runs headful on a virtual display (Xvfb `:99`) so the same browser serves both scraping and login.

Local dev still works without Docker via the venv. Tests cover the webhook (`tests/test_webhook.py`); the scraper itself has no automated tests.

## Configuration

All credentials are in `.env` (not committed). See `.env.example` for the full template. In the container, `.env` lives on the mounted `/data` volume (loaded via `DATA_DIR`).
- `SCRAPER_USERNAME` / `SCRAPER_PASSWORD` — auction platform login
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` — PostgreSQL connection
- `MAILGUN_DOMAIN`, `MAILGUN_API_KEY`, `EMAIL_FROM`, `EMAIL_TO` — Mailgun email alerts (EU endpoint: `api.eu.mailgun.net`)
- `WEBHOOK_TOKEN` — secret token required to call `/run`, `/status`, `/login`
- `WEBHOOK_ALLOWED_IPS` — comma-separated IP allowlist for `/run` (empty = allow all)
- `VNC_PASSWORD` — password for the noVNC console
- `RANDOM_DELAY_MAX_MIN` — max random pre-scrape delay in minutes (0 = disabled)
- `DATA_DIR`, `CHROME_BIN`, `CHROMEDRIVER_PATH` — set by the Docker image; override only for custom setups

## Running

The app runs as a single Docker container. A scrape is triggered by an authenticated webhook (`POST /run`); manual login is done via `POST /login` watched through noVNC.

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

Local dev without Docker still works:

```bash
source venv/bin/activate
python scraper.py          # run a scrape (opens Chrome window)
python scraper.py --cron   # scrape non-interactively, email alerts on failure
python scraper.py --login  # open the browser for manual login
pytest                     # run the webhook tests
```

## Installing Dependencies

```bash
pip install -r requirements.txt
```

## Deployment

The container is host-agnostic. The daily run is triggered externally by cron.2l.ro, which issues `POST http://HOST:8000/run?token=...` on a schedule.

- Webhook (`/run`) is protected by `WEBHOOK_TOKEN` + `WEBHOOK_ALLOWED_IPS` (the cron server's IP).
- noVNC (port 7900) MUST stay private — bind it to localhost/VPN only, never expose it publicly (it controls a logged-in browser).
- Session persistence (`cookies.json`, `chrome_profile/`) and output (`domains*.txt`) live on the mounted `/data` volume and survive restarts.

**When the session expires:** an email alert is sent. Re-login with `POST /login` and complete verification in noVNC.

**Endpoints:** `/run` (public, token+IP), `/health` (public), `/status` (token), `/login` (token).

The `setup-ec2.sh`, `run.sh`, and `systemd/` files describe the previous (pre-container) systemd-timer deployment and are retained for reference only.

## Architecture

The scraping/browser logic lives in `scraper.py` within the `DomainsScrapperSelenium` class. The web layer lives in `webhook.py`. The container is assembled by `Dockerfile` + `supervisord.conf` + `entrypoint.sh`.

- **Container processes** (`supervisord.conf`): Xvfb (`:99`), fluxbox, x11vnc, websockify/noVNC (port 7900), and gunicorn serving `webhook.py` (port 8000). Chrome runs headful on `:99`.
- **Webhook** (`webhook.py`): gunicorn runs a single worker; a global `threading.Lock` ensures only one browser operation runs at a time. `POST /run` validates token + IP allowlist, returns `202` and runs the scrape in a background thread (after an optional random delay), or `409` if one is already running. `POST /login` runs the manual-login flow synchronously (up to 10 min). `/status` reports in-memory state (running flag, last run, last error, last known session validity); it does NOT spin up a browser to probe login, to avoid contention. `/health` is unauthenticated.
- **Session management**: Cookies are saved to `cookies.json` after successful login and restored before each run. The Chrome profile (`chrome_profile/`) is also used but cookies.json is the primary session persistence mechanism (Chrome drops session cookies on exit). All paths are rooted in `DATA_DIR` (the `/data` volume in the container).
- **Login flow**: `login_manual(wait_seconds=...)` fills credentials then waits for the user to complete email verification/CAPTCHA in the browser (watched via noVNC). Credential fields are filled via the `_fill_field()` helper (JS `value=''` + Ctrl+A + Delete + `send_keys`) and Chrome autofill/password manager is disabled via `prefs` — plain `clear()` + `send_keys()` is not enough because the persistent Chrome profile re-autofills the field on focus, causing the typed value to be appended to the autofilled one.
- **Job functions**: `run_scrape_job(cron=...)` and `run_login_job(wait_seconds=...)` are module-level entry points called by both `webhook.py` and the `scraper.py` CLI (`--cron` / `--login`). Chrome runs headful (no `--headless`); the `--cron` flag now only controls non-interactive behavior + email alerts, not headlessness.
- **Domain collection**: `get_all_auction_domains()` navigates to the auction page with `?filter=today`, resets DataTables pagination via JavaScript, then loops through pages parsing the table with BeautifulSoup/lxml.
- **Storage**: Domains are saved to `domains.txt` (overwritten each run), a timestamped `domains_YYYYMMDD_HHMMSS.txt` backup, and upserted into a PostgreSQL `domains` table with the current date as `expiry_date`.

## Database Schema

The PostgreSQL `domains` table has a `domain` column (unique) and an `expiry_date` column. The upsert uses `ON CONFLICT (domain) DO UPDATE SET expiry_date`.

## Key Dependencies

- `selenium` (Chrome automation); `webdriver-manager` only for local dev — in the container the system chromedriver is used via `CHROMEDRIVER_PATH`
- `flask` + `gunicorn` (webhook web server)
- `beautifulsoup4` + `lxml` (HTML parsing)
- `psycopg2-binary` (PostgreSQL, optional — gracefully degrades if unavailable)
- `python-dotenv` (loads `.env` configuration)
- `requests` (Mailgun API calls)
- `pytest` (webhook tests)

## Output Files

- `domains.txt` — latest run's domain list (one per line)
- `domains_YYYYMMDD_HHMMSS.txt` — timestamped archive of each run
- `cookies.json` — saved browser cookies for session persistence
- `chrome_profile/` — persistent Chrome browser profile

In the container these all live under the `/data` volume, alongside `.env` and the generated `.vncpass`.
