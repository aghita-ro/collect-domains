# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Single-script Python scraper (`scraper.py`) that collects expiring `.ro` domain names from the eureg.ro auction platform. It uses Selenium with Chrome to handle login (which requires manual CAPTCHA/email verification), paginates through a DataTables-based auction list, and saves results both to text files and a PostgreSQL database.

There are no tests, linters, or build steps. The project is a single Python script with a venv.

## Configuration

All credentials are in `.env` (not committed):
- `SCRAPER_USERNAME` / `SCRAPER_PASSWORD` — auction platform login
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` — PostgreSQL connection
- `MAILGUN_DOMAIN`, `MAILGUN_API_KEY`, `EMAIL_FROM`, `EMAIL_TO` — Mailgun email alerts (EU endpoint: `api.eu.mailgun.net`)

## Running

```bash
source venv/bin/activate

# Interactive mode — opens Chrome window, supports manual login
python scraper.py

# Cron mode — headless, no manual login, sends email alerts on failure
python scraper.py --cron
```

## Installing Dependencies

```bash
pip install -r requirements.txt
```

## Deployment (EC2)

Deployed to `/opt/scraper` on an Ubuntu 24.04 EC2 instance (also the WireGuard VPN server).

- `setup-ec2.sh` — one-shot setup script (installs deps, clones repo, installs systemd timer)
- `run.sh` — wrapper script that does `git pull --ff-only` then runs `python scraper.py --cron`
- `systemd/domains-scrapper.timer` — runs daily at a random time (base 6AM + up to 14h random delay) to avoid detection patterns
- `systemd/domains-scrapper.service` — oneshot service, runs as `ubuntu` user

**Manual login on EC2** (when session expires):
```bash
ssh -X ubuntu@ec2-host
cd /opt/scraper && source venv/bin/activate && python scraper.py
```
Requires X11 forwarding (`X11Forwarding yes` in sshd_config) and a local X server.

**Useful commands on EC2:**
```bash
systemctl list-timers domains-scrapper.timer       # next scheduled run
journalctl -u domains-scrapper.service --no-pager  # view logs
sudo systemctl start domains-scrapper.service      # manual trigger
```

## Architecture

Everything lives in `scraper.py` within the `DomainsScrapperSelenium` class:

- **Session management**: Cookies are saved to `cookies.json` after successful login and restored before each run. The Chrome profile (`chrome_profile/`) is also used but cookies.json is the primary session persistence mechanism (Chrome drops session cookies on exit).
- **Login flow**: `login_manual()` fills credentials then waits up to 3 minutes for the user to complete email verification/CAPTCHA in the browser. Only runs in interactive mode.
- **Cron mode** (`--cron`): Runs headless (`--headless=new`). If the session is expired, sends an email alert via Mailgun and exits. Also alerts on 0 domains collected or unexpected errors.
- **Domain collection**: `get_all_auction_domains()` navigates to the auction page with `?filter=today`, resets DataTables pagination via JavaScript, then loops through pages parsing the table with BeautifulSoup/lxml.
- **Storage**: Domains are saved to `domains.txt` (overwritten each run), a timestamped `domains_YYYYMMDD_HHMMSS.txt` backup, and upserted into a PostgreSQL `domains` table with the current date as `expiry_date`.

## Database Schema

The PostgreSQL `domains` table has a `domain` column (unique) and an `expiry_date` column. The upsert uses `ON CONFLICT (domain) DO UPDATE SET expiry_date`.

## Key Dependencies

- `selenium` + `webdriver-manager` (Chrome automation)
- `beautifulsoup4` + `lxml` (HTML parsing)
- `psycopg2-binary` (PostgreSQL, optional — gracefully degrades if unavailable)
- `python-dotenv` (loads `.env` configuration)
- `requests` (Mailgun API calls)

## Output Files

- `domains.txt` — latest run's domain list (one per line)
- `domains_YYYYMMDD_HHMMSS.txt` — timestamped archive of each run
- `cookies.json` — saved browser cookies for session persistence
- `chrome_profile/` — persistent Chrome browser profile
