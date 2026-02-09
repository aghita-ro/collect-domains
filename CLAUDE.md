# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Single-script Python scraper (`scraper.py`) that collects expiring `.ro` domain names from an auction platform. It uses Selenium with a persistent Chrome profile to handle login (which requires manual CAPTCHA/email verification), paginates through a DataTables-based auction list, and saves results both to text files and a PostgreSQL database.

## Running

```bash
# Activate virtualenv
source venv/bin/activate

# Run the scraper (opens Chrome, may require manual login)
python scraper.py
```

The script runs interactively (non-headless by default) and waits for user input before closing the browser. First run requires manual login; subsequent runs reuse the session from `chrome_profile/`.

## Architecture

Everything lives in `scraper.py` within the `DomainsScrapperSelenium` class:

- **Session management**: Persistent Chrome profile (`chrome_profile/`) preserves login cookies across runs. `is_logged_in()` checks session validity by navigating to dashboard.
- **Login flow**: `login_manual()` fills credentials then waits up to 3 minutes for the user to complete email verification/CAPTCHA in the browser.
- **Domain collection**: `get_all_auction_domains()` navigates to the auction page with `?filter=today`, resets DataTables pagination via JavaScript, then loops through pages parsing the table with BeautifulSoup/lxml.
- **Storage**: Domains are saved to `domains.txt` (overwritten each run), a timestamped `domains_YYYYMMDD_HHMMSS.txt` backup, and upserted into a PostgreSQL `domains` table with the current date as `expiry_date`.

## Key Dependencies

- `selenium` + `webdriver-manager` (Chrome automation)
- `beautifulsoup4` + `lxml` (HTML parsing)
- `psycopg2-binary` (PostgreSQL, optional — gracefully degrades if unavailable)

## Output Files

- `domains.txt` — latest run's domain list (one per line)
- `domains_YYYYMMDD_HHMMSS.txt` — timestamped archive of each run
- `chrome_profile/` — persistent Chrome browser profile
- `eureg_session.pkl` — legacy session pickle (unused)
