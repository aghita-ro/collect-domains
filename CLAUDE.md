# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Python scraper (`scraper.py`) that collects expiring `.ro` domain names from the eureg.ro auction platform. It uses Selenium with Chrome to handle login, paginates through a DataTables-based auction list, and saves results both to text files and a PostgreSQL database. Login normally completes automatically: the persistent Chrome profile carries the session past the login-page reCAPTCHA, and the email-based 2FA code is read straight from Gmail via IMAP. A manual noVNC fallback exists for the rare case the login page raises a reCAPTCHA challenge.

It runs as a single self-contained Docker container: a Flask webhook (`webhook.py`) triggers a scrape on demand (called by an external cron), and manual re-login is done through a browser visible via noVNC. Chrome runs headful on a virtual display (Xvfb `:99`) so the same browser serves both scraping and login.

Local dev still works without Docker via the venv. Tests cover the webhook (`tests/test_webhook.py`); the scraper itself has no automated tests.

## Configuration

All credentials are in `.env` (not committed). See `.env.example` for the full template. In the container, `.env` lives on the mounted `/data` volume (loaded via `DATA_DIR`).
- `SCRAPER_USERNAME` / `SCRAPER_PASSWORD` — auction platform login
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` — PostgreSQL connection
- `MAILGUN_DOMAIN`, `MAILGUN_API_KEY`, `EMAIL_FROM`, `EMAIL_TO` — Mailgun email alerts (EU endpoint: `api.eu.mailgun.net`)
- `GMAIL_IMAP_USER`, `GMAIL_IMAP_PASSWORD`, `GMAIL_IMAP_HOST` — Gmail IMAP for auto-reading the eureg.ro 2FA login code. Use a Google **App Password** (needs 2-Step Verification), not the account password. When set, login completes the email 2FA step automatically (no manual code entry).
- `WEBHOOK_TOKEN` — secret token required to call `/run`, `/status`, `/login`
- `WEBHOOK_ALLOWED_IPS` — comma-separated IP allowlist for `/run` (empty = allow all)
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

The container is host-agnostic and supports two trigger models:

**Always-on server:** run with `RESTART=unless-stopped ./docker_run.sh` (auto-starts at boot) and have an external scheduler issue `POST http://HOST:8000/run?token=...` on a schedule (e.g. cron.2l.ro — put its IP in `WEBHOOK_ALLOWED_IPS`).

**Intermittent / workstation (default):** the container is NOT started at boot (`--restart no`). `daily_run.sh` owns its lifecycle and is run every ~5 min by cron:
```
*/5 * * * * /path/to/collect-domains/daily_run.sh >/dev/null 2>&1
```
It exits immediately if today already succeeded (marker file `data/.last_success_date`), otherwise starts the container, triggers `/run`, waits for it to finish, marks the day on `session_valid: true`, and stops the container again. Net effect: one successful scrape per day, within ~5 min of the machine being on, with the container off the rest of the time. A `flock` guards against overlapping ticks; activity is logged to `data/daily_run.log`.

- Webhook (`/run`) is protected by `WEBHOOK_TOKEN` + `WEBHOOK_ALLOWED_IPS`.
- noVNC (port 7900) MUST stay private — bind it to localhost/VPN only, never expose it publicly (it controls a logged-in browser).
- Session persistence (`cookies.json`, `chrome_profile/`) and output (`domains*.txt`) live on the mounted `/data` volume and survive restarts.

**When the session expires:** the next run re-logs in automatically (cookies → MFA page → 2FA code read from Gmail via IMAP → submitted). A Mailgun alert + manual `POST /login` in noVNC are only needed if the automatic login fails (e.g. a reCAPTCHA challenge on the login page).

**Endpoints:** `/run` (public, token+IP), `/health` (public), `/status` (token), `/login` (token).

The `setup-ec2.sh`, `run.sh`, and `systemd/` files describe the previous (pre-container) systemd-timer deployment and are retained for reference only.

## Architecture

The scraping/browser logic lives in `scraper.py` within the `DomainsScrapperSelenium` class. The web layer lives in `webhook.py`. The container is assembled by `Dockerfile` + `supervisord.conf` + `entrypoint.sh`.

- **Container processes** (`supervisord.conf`): Xvfb (`:99`), fluxbox, x11vnc, websockify/noVNC (port 7900), and gunicorn serving `webhook.py` (port 8000). Chrome runs headful on `:99`.
- **Webhook** (`webhook.py`): gunicorn runs a single worker; a global `threading.Lock` ensures only one browser operation runs at a time. `POST /run` validates token + IP allowlist, returns `202` and runs the scrape in a background thread (after an optional random delay), or `409` if one is already running. `POST /login` runs the manual-login flow synchronously (up to 10 min). `/status` reports in-memory state (running flag, last run, last error, last known session validity); it does NOT spin up a browser to probe login, to avoid contention. `/health` is unauthenticated.
- **Session management**: Cookies are saved to `cookies.json` after successful login and restored before each run. The Chrome profile (`chrome_profile/`) is also used but cookies.json is the primary session persistence mechanism (Chrome drops session cookies on exit). All paths are rooted in `DATA_DIR` (the `/data` volume in the container).
- **Login flow**: `ensure_logged_in()` is the automatic entry point used by both scrape and login jobs: it loads cookies, opens the dashboard, and — depending on where it lands — completes the email 2FA (`complete_mfa()`, reading the code via `fetch_2fa_code()` IMAP) or, if cookies fully expired, runs `_credential_login()` first. `is_logged_in()` treats the `/mfa` page as **not** logged in (the page has a "Deconectare" link, so the old logout-link check wrongly passed it, causing silent 0-domain runs). `login_manual(wait_seconds=...)` remains the manual fallback: it fills credentials then waits for the user to complete verification in the browser (watched via noVNC). Credential fields are filled via the `_fill_field()` helper (JS `value=''` + Ctrl+A + Delete + `send_keys`) and Chrome autofill/password manager is disabled via `prefs` — plain `clear()` + `send_keys()` is not enough because the persistent Chrome profile re-autofills the field on focus, causing the typed value to be appended to the autofilled one.
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

In the container these all live under the `/data` volume, alongside `.env`. The noVNC console runs without a password (`x11vnc -nopw`); keep port 7900 private (localhost/VPN only).
