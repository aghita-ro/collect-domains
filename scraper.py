from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
# webdriver-manager is only needed for local dev (auto-downloads chromedriver).
# In the container we use the system chromedriver via CHROMEDRIVER_PATH, so the
# import is deferred to __init__ to avoid a hard dependency in the image.
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import time
import os
import json
import argparse
import re
import imaplib
import smtplib
import email
from email.utils import parsedate_to_datetime
from email.mime.text import MIMEText
import requests as http_requests
from datetime import date
import calendar
from dotenv import load_dotenv

# Data directory: a mounted volume in the container, the script dir for local dev.
# Holds .env, cookies.json, chrome_profile/, and domains*.txt output.
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))

# Load .env from DATA_DIR if present, otherwise fall back to default search.
_dotenv_path = os.path.join(DATA_DIR, ".env")
if os.path.exists(_dotenv_path):
    load_dotenv(_dotenv_path)
else:
    load_dotenv()

# Mailgun configuration
MAILGUN_DOMAIN = os.getenv("MAILGUN_DOMAIN", "")
MAILGUN_API_KEY = os.getenv("MAILGUN_API_KEY", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")

# Database configuration
DB_HOST = os.getenv("DB_HOST", "")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "")
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")

# Gmail IMAP — used to auto-read the eureg.ro 2FA login code (sent by email).
# Requires a Google "App Password" (account must have 2-Step Verification on).
GMAIL_IMAP_USER = os.getenv("GMAIL_IMAP_USER", "")
GMAIL_IMAP_PASSWORD = os.getenv("GMAIL_IMAP_PASSWORD", "")
GMAIL_IMAP_HOST = os.getenv("GMAIL_IMAP_HOST", "imap.gmail.com")

# Try to import psycopg2
try:
    import psycopg2
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False
    print("Warning: psycopg2 not installed. Database features disabled.")


def _send_via_gmail_smtp(subject, body, to):
    """Send a plain-text email through Gmail SMTP using the IMAP app password."""
    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = GMAIL_IMAP_USER
    msg["To"] = to
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as s:
        s.starttls()
        s.login(GMAIL_IMAP_USER, GMAIL_IMAP_PASSWORD)
        s.sendmail(GMAIL_IMAP_USER, [to], msg.as_string())


def send_alert_email(subject, body, to=None):
    """Send an email. Prefers Gmail SMTP (reuses the IMAP app password, reliable);
    falls back to the Mailgun API if Gmail isn't configured. Returns True on success.
    The default recipient is the Gmail account itself (where the 2FA codes arrive)."""
    if GMAIL_IMAP_USER and GMAIL_IMAP_PASSWORD:
        recipient = to or GMAIL_IMAP_USER
        try:
            _send_via_gmail_smtp(subject, body, recipient)
            print(f"✓ Email sent to {recipient} (Gmail SMTP)")
            return True
        except Exception as e:
            print(f"✗ Gmail SMTP error: {str(e)} - falling back to Mailgun")

    if not MAILGUN_API_KEY or not MAILGUN_DOMAIN:
        print("✗ No working email transport (Gmail SMTP failed, Mailgun not configured)")
        return False
    try:
        response = http_requests.post(
            f"https://api.eu.mailgun.net/v3/{MAILGUN_DOMAIN}/messages",
            auth=("api", MAILGUN_API_KEY),
            data={
                "from": EMAIL_FROM,
                "to": to or EMAIL_TO,
                "subject": subject,
                "text": body,
            },
        )
        if response.status_code == 200:
            print(f"✓ Alert email sent to {to or EMAIL_TO} (Mailgun)")
            return True
        else:
            print(f"✗ Mailgun error {response.status_code}: {response.text}")
            return False
    except Exception as e:
        print(f"✗ Failed to send alert email: {str(e)}")
        return False


# eureg.ro 2FA emails arrive from noreply@eureg.ro and contain a code formatted
# like "RW-667076" — the MFA field only accepts the 6 digits (the 2-letter prefix
# is shown on the page only so the user can confirm the right email).
_MFA_CODE_RE = re.compile(r"[A-Z]{2}-(\d{6})")
_MFA_DIGITS_RE = re.compile(r"\b(\d{6})\b")


def _email_body_text(msg):
    """Return the concatenated decoded text of all parts of an email.Message."""
    chunks = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload:
                    chunks.append(payload.decode(part.get_content_charset() or "utf-8", "ignore"))
            except Exception:
                continue
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                chunks.append(payload.decode(msg.get_content_charset() or "utf-8", "ignore"))
        except Exception:
            pass
    return "\n".join(chunks)


def fetch_2fa_code(since_epoch, timeout=180, poll_interval=5):
    """Poll Gmail (IMAP) for an eureg.ro 2FA code newer than since_epoch.

    Returns the 6-digit code string, or None if not configured / not found in time.
    """
    if not GMAIL_IMAP_USER or not GMAIL_IMAP_PASSWORD:
        print("  ✗ Gmail IMAP not configured (GMAIL_IMAP_USER/PASSWORD) - cannot auto-read 2FA code")
        return None

    deadline = time.time() + timeout
    # IMAP SINCE is date-granular; subtract a day to avoid timezone edge cases.
    since_str = time.strftime("%d-%b-%Y", time.gmtime(since_epoch - 86400))
    print(f"  Reading 2FA code from {GMAIL_IMAP_USER} via IMAP...")

    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST)
            M.login(GMAIL_IMAP_USER, GMAIL_IMAP_PASSWORD)
            M.select("INBOX")
            typ, data = M.search(None, f'(FROM "eureg.ro" SINCE {since_str})')
            best = None  # (epoch, code)
            if typ == "OK" and data and data[0]:
                for mid in reversed(data[0].split()[-15:]):
                    typ, msgdata = M.fetch(mid, "(RFC822)")
                    if typ != "OK" or not msgdata or not msgdata[0]:
                        continue
                    msg = email.message_from_bytes(msgdata[0][1])
                    try:
                        ep = parsedate_to_datetime(msg["Date"]).timestamp()
                    except Exception:
                        ep = 0
                    if ep < since_epoch - 90:
                        continue  # older than our login attempt — skip stale codes
                    body = _email_body_text(msg)
                    m = _MFA_CODE_RE.search(body) or _MFA_DIGITS_RE.search(body)
                    if m and (best is None or ep > best[0]):
                        best = (ep, m.group(1))
            M.logout()
            if best:
                print(f"  ✓ 2FA code retrieved from email")
                return best[1]
        except Exception as e:
            print(f"  IMAP error: {e}")
        time.sleep(poll_interval)

    print("  ✗ No fresh 2FA code arrived within timeout")
    return None


class DomainsScrapperSelenium:
    def __init__(self, username, password, headless=False, work_dir=None):
        self.username = username
        self.password = password
        self.base_url = "https://www.eureg.ro"
        self.db_conn = None
        
        # Create a persistent profile directory under the data dir
        self.work_dir = work_dir or DATA_DIR
        self.profile_dir = os.path.join(self.work_dir, "chrome_profile")
        if not os.path.exists(self.profile_dir):
            os.makedirs(self.profile_dir)
            print(f"Created profile directory: {self.profile_dir}")
        
        # Setup Chrome options with persistent profile
        chrome_options = Options()
        chrome_bin = os.getenv("CHROME_BIN", "")
        if chrome_bin:
            chrome_options.binary_location = chrome_bin
        chrome_options.add_argument(f"user-data-dir={self.profile_dir}")
        chrome_options.add_argument("--profile-directory=Default")
        
        if headless:
            chrome_options.add_argument("--headless=new")
        
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_argument("--disable-autofill")
        chrome_options.add_argument("--disable-save-password-bubble")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        chrome_options.add_experimental_option("prefs", {
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
            "autofill.profile_enabled": False,
            "autofill.credit_card_enabled": False,
        })
        
        # Initialize driver
        print("Initializing Chrome with persistent profile...")
        chromedriver_path = os.getenv("CHROMEDRIVER_PATH", "")
        if chromedriver_path:
            service = Service(executable_path=chromedriver_path)
        else:
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=chrome_options)
        self.wait = WebDriverWait(self.driver, 30)
        print(f"✓ Using profile: {self.profile_dir}")
        self.cookies_file = os.path.join(self.work_dir, "cookies.json")

    def save_cookies(self):
        """Save browser cookies to file"""
        try:
            cookies = self.driver.get_cookies()
            with open(self.cookies_file, 'w') as f:
                json.dump(cookies, f)
            print(f"  ✓ Cookies saved ({len(cookies)} cookies)")
        except Exception as e:
            print(f"  ✗ Failed to save cookies: {str(e)}")

    def load_cookies(self):
        """Load cookies from file into browser"""
        if not os.path.exists(self.cookies_file):
            return False
        try:
            with open(self.cookies_file, 'r') as f:
                cookies = json.load(f)
            self.driver.get(self.base_url)
            time.sleep(2)
            self.driver.delete_all_cookies()
            for cookie in cookies:
                cookie.pop('sameSite', None)
                try:
                    self.driver.add_cookie(cookie)
                except Exception as e:
                    print(f"    cookie skip: {cookie.get('name')}: {e}")
            print(f"  ✓ Cookies loaded ({len(cookies)} cookies)")
            return True
        except Exception as e:
            print(f"  ✗ Failed to load cookies: {str(e)}")
            return False

    def connect_db(self):
        """Connect to PostgreSQL database"""
        if not PSYCOPG2_AVAILABLE:
            print("✗ psycopg2 not available - skipping database connection")
            return False
        
        try:
            print("\nConnecting to database...")
            self.db_conn = psycopg2.connect(
                host=DB_HOST,
                port=DB_PORT,
                dbname=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD
            )
            print("✓ Database connection successful")
            return True
        except Exception as e:
            print(f"✗ Database connection failed: {str(e)}")
            self.db_conn = None
            return False
    
    def save_domains_to_db(self, domains):
        """Save domains to database with current date as expiry_date.
        Returns a dict {inserted, updated, errors} on success, or None on failure."""
        if not self.db_conn:
            print("✗ No database connection - skipping database save")
            return None
        
        print("\n" + "="*50)
        print("SAVING TO DATABASE")
        print("="*50)
        
        today = date.today()
        inserted = 0
        updated = 0
        errors = 0
        
        try:
            cursor = self.db_conn.cursor()
            
            for domain in domains:
                try:
                    # UPSERT: Insert or update expiry_date if exists
                    cursor.execute("""
                        INSERT INTO domains (domain, expiry_date)
                        VALUES (%s, %s)
                        ON CONFLICT (domain)
                        DO UPDATE SET expiry_date = EXCLUDED.expiry_date
                        RETURNING (xmax = 0) AS inserted
                    """, (domain, today))
                    
                    result = cursor.fetchone()
                    if result[0]:  # xmax = 0 means INSERT
                        inserted += 1
                    else:  # xmax != 0 means UPDATE
                        updated += 1
                    
                except Exception as e:
                    print(f"  ✗ Error saving {domain}: {str(e)}")
                    errors += 1
            
            self.db_conn.commit()
            cursor.close()
            
            print(f"\n  ✓ Inserted: {inserted} new domains")
            print(f"  ✓ Updated:  {updated} existing domains")
            if errors > 0:
                print(f"  ✗ Errors:   {errors}")
            print(f"  Date used:  {today}")

            return {"inserted": inserted, "updated": updated, "errors": errors}

        except Exception as e:
            print(f"\n✗ Database error: {str(e)}")
            self.db_conn.rollback()
            return None
    
    def get_yearly_summary(self):
        """Get a summary of days covered in the current year from the database"""
        if not self.db_conn:
            return None
        try:
            cursor = self.db_conn.cursor()
            year = date.today().year
            days_in_year = 366 if calendar.isleap(year) else 365
            cursor.execute("""
                SELECT expiry_date, COUNT(*) AS domain_count
                FROM domains
                WHERE expiry_date >= %s AND expiry_date <= %s
                GROUP BY expiry_date
                ORDER BY expiry_date
            """, (f"{year}-01-01", f"{year}-12-31"))
            rows = cursor.fetchall()
            days_covered = len(rows)
            cursor.close()
            return {
                "year": year,
                "days_in_year": days_in_year,
                "days_covered": days_covered,
                "days_remaining": days_in_year - days_covered,
                "daily_counts": rows,
            }
        except Exception as e:
            print(f"  ✗ Error getting yearly summary: {str(e)}")
            return None

    def print_yearly_summary(self):
        """Display the yearly coverage summary on screen"""
        summary = self.get_yearly_summary()
        if not summary:
            return
        print("\n" + "="*50)
        print(f"YEARLY COVERAGE — {summary['year']}")
        print("="*50)
        for expiry_date, count in summary["daily_counts"]:
            print(f"  {expiry_date}  —  {count} domains")
        print("-"*50)
        pct = summary['days_covered'] / summary['days_in_year'] * 100
        print(f"  Days covered:   {summary['days_covered']}/{summary['days_in_year']} ({pct:.1f}%)")
        print(f"  Days remaining: {summary['days_remaining']}")
        print("="*50)
        return summary

    def is_logged_in(self):
        """Check if we're currently logged in"""
        try:
            print("\nChecking login status...")
            self.load_cookies()
            self.driver.get(f"{self.base_url}/ro/clienti/dashboard")
            time.sleep(3)
            
            current_url = self.driver.current_url
            print(f"  Current URL: {current_url}")
            
            # Parse the URL path (ignore query parameters)
            parsed = urlparse(current_url)
            url_path = parsed.path.lower()
            
            # Check the PATH only (not query string)
            if '/login' in url_path or '/conectare' in url_path:
                print("  ✗ Not logged in (on login page)")
                return False

            # The MFA page also has a "Deconectare" link, so it must be checked
            # BEFORE the logout-link fallback — otherwise we'd report a half-logged-in
            # (code not yet entered) session as valid and the scrape would get 0 rows.
            if '/mfa' in url_path:
                print("  ✗ Not fully logged in (on MFA / 2FA page)")
                return False

            if '/dashboard' in url_path:
                print("  ✓ Already logged in (on dashboard)")
                return True

            # Fallback: check for logout link
            try:
                logout_link = self.driver.find_element(By.PARTIAL_LINK_TEXT, "Deconectare")
                print("  ✓ Already logged in (found logout link)")
                return True
            except:
                pass

            print("  ✗ Login status unclear")
            return False

        except Exception as e:
            print(f"  ✗ Error checking login: {str(e)}")
            return False

    def complete_mfa(self, since_epoch):
        """On the eureg MFA page: read the emailed code and submit it.

        `since_epoch` is when we navigated to the MFA page (the code is sent on
        page load); only codes newer than that are accepted. Returns True on success.
        """
        print("\n  MFA / 2FA step detected — completing automatically...")
        code = fetch_2fa_code(since_epoch=since_epoch - 30, timeout=180)
        if not code:
            return False
        try:
            field = self.wait.until(EC.presence_of_element_located((By.NAME, "passcode")))
            self.driver.execute_script("arguments[0].value = '';", field)
            field.click()
            field.send_keys(code)
            self.driver.find_element(By.CSS_SELECTOR, "button[type=submit]").click()
            for _ in range(20):
                time.sleep(1)
                if '/mfa' not in urlparse(self.driver.current_url).path.lower():
                    print(f"  ✓ MFA completed — now at {self.driver.current_url}")
                    self.save_cookies()
                    return True
            print("  ✗ MFA code submitted but still on MFA page (expired/invalid?)")
            return False
        except Exception as e:
            print(f"  ✗ MFA completion error: {str(e)}")
            return False

    def _credential_login(self):
        """Fill username/password and submit. The login page has an invisible
        reCAPTCHA that usually passes silently (persistent profile + stable IP);
        if Google raises a challenge this returns False and manual login is needed.
        Returns the resulting URL path, or None on failure."""
        print("\n  Performing credential login...")
        self.driver.get(f"{self.base_url}/ro/clienti/login")
        time.sleep(2)
        self.accept_cookies()
        username_field = self.wait.until(EC.presence_of_element_located((By.ID, "login")))
        self._fill_field(username_field, self.username)
        self._fill_field(self.driver.find_element(By.ID, "pass"), self.password)
        time.sleep(1)
        self.driver.find_element(By.ID, "login-button").click()
        for _ in range(20):
            time.sleep(1)
            path = urlparse(self.driver.current_url).path.lower()
            if '/mfa' in path or '/dashboard' in path:
                return path
        return urlparse(self.driver.current_url).path.lower()

    def ensure_logged_in(self):
        """Make sure we end up on the dashboard, automatically handling the MFA
        email-code step (and a credential login if cookies have fully expired).
        Returns True if logged in, False if manual intervention is required."""
        try:
            nav = time.time()
            self.load_cookies()
            self.driver.get(f"{self.base_url}/ro/clienti/dashboard")
            time.sleep(3)
            path = urlparse(self.driver.current_url).path.lower()
            print(f"  Current URL: {self.driver.current_url}")

            if '/dashboard' in path:
                print("  ✓ Already logged in (dashboard)")
                return True

            # Cookies expired entirely → try a full credential login first.
            if '/login' in path or '/conectare' in path:
                nav = time.time()
                path = self._credential_login()

            # Either the cookies landed us on MFA, or the credential login did.
            if '/mfa' in path:
                if self.complete_mfa(since_epoch=nav):
                    return True
                return False

            if '/dashboard' in path:
                return True

            print("  ✗ Could not reach dashboard (likely reCAPTCHA challenge on login)")
            return False
        except Exception as e:
            print(f"  ✗ ensure_logged_in error: {str(e)}")
            return False

    def accept_cookies(self):
        """Accept cookie consent if present"""
        try:
            time.sleep(2)
            cookie_selectors = [
                "a.cc-btn.cc-dismiss",
                "button.cc-btn.cc-dismiss",
                ".cc-dismiss"
            ]
            
            for selector in cookie_selectors:
                try:
                    cookie_btn = self.driver.find_element(By.CSS_SELECTOR, selector)
                    if cookie_btn.is_displayed():
                        print("  Accepting cookies...")
                        cookie_btn.click()
                        time.sleep(1)
                        return
                except:
                    continue
        except:
            pass
    
    def _fill_field(self, element, value):
        # Chrome autofill from the persistent profile can repopulate the field
        # after .clear(), causing send_keys to append to the autofilled value.
        # Force-clear via JS + Ctrl+A/Delete before typing.
        self.driver.execute_script("arguments[0].value = '';", element)
        element.click()
        element.send_keys(Keys.CONTROL, "a")
        element.send_keys(Keys.DELETE)
        element.send_keys(value)

    def login_manual(self, wait_seconds=180):
        """Login with manual intervention for CAPTCHA/email verification"""
        try:
            print("\n" + "="*70)
            print("MANUAL LOGIN REQUIRED")
            print("="*70)
            
            print("\nOpening login page...")
            self.driver.get(f"{self.base_url}/ro/clienti/login")
            time.sleep(2)
            
            self.accept_cookies()
            
            print("Filling in credentials...")
            username_field = self.wait.until(
                EC.presence_of_element_located((By.ID, "login"))
            )
            self._fill_field(username_field, self.username)

            password_field = self.driver.find_element(By.ID, "pass")
            self._fill_field(password_field, self.password)
            
            print("Clicking login button...\n")
            login_button = self.driver.find_element(By.ID, "login-button")
            login_button.click()
            
            print("="*70)
            print("⚠️  PLEASE COMPLETE THE LOGIN MANUALLY:")
            print("="*70)
            print("  1. Check your email for verification code")
            print("  2. Enter the code in the browser")
            print("  3. Complete any CAPTCHA if needed")
            print("  4. Wait for redirect to dashboard")
            print(f"\nScript will wait up to {wait_seconds // 60} minutes...")
            print("="*70 + "\n")
            
            # Wait for login completion
            for i in range(wait_seconds):
                time.sleep(1)
                current_url = self.driver.current_url
                
                # Parse URL path only
                parsed = urlparse(current_url)
                url_path = parsed.path.lower()
                
                # Check if we're on dashboard (not login page)
                if '/dashboard' in url_path or ('/login' not in url_path and '/conectare' not in url_path and '/clienti/' in url_path):
                    print(f"\n✓ Login successful! (took {i+1} seconds)")
                    print(f"  Final URL: {current_url}")
                    self.save_cookies()
                    print("  Next run will skip login automatically.")
                    return True
                
                if (i + 1) % 15 == 0:
                    print(f"  ... waiting ({i+1}s elapsed) ...")
            
            print("\n✗ Login timeout after 3 minutes")
            return False
            
        except Exception as e:
            print(f"\n✗ Error during login: {str(e)}")
            import traceback
            traceback.print_exc()
            return False
    
    def get_all_auction_domains(self):
        """Get all domain names from all pages of auctions"""
        print("\n" + "="*50)
        print("COLLECTING DOMAINS")
        print("="*50)
        
        all_domains = []
        page_num = 0
        
        try:
            print("\nNavigating to auction page...")
            self.driver.get(f"{self.base_url}/ro/clienti/licitatii/index?filter=today")
            time.sleep(3)
            
            current_url = self.driver.current_url
            print(f"  URL: {current_url}")
            
            # Parse URL path only
            parsed = urlparse(current_url)
            url_path = parsed.path.lower()
            
            # Check if redirected to login
            if '/login' in url_path or '/conectare' in url_path:
                print("\n✗ Session expired - redirected to login")
                print("  Please run the script again to re-login")
                return []
            
            print(f"  Title: {self.driver.title}")
            
            # Wait for DataTable to initialize
            time.sleep(2)
            
            # Reset DataTable to page 1 (clear saved state and go to first page)
            print("  Resetting to page 1...")
            self.driver.execute_script("""
                // Clear DataTables saved state
                localStorage.removeItem('DataTables_auctions-table');
                // Go to first page
                if (typeof $table !== 'undefined' && $table) {
                    $table.page(0).draw(false);
                } else if ($('#auctions-table').length) {
                    $('#auctions-table').DataTable().page(0).draw(false);
                }
            """)
            time.sleep(2)
            
            while True:
                page_num += 1
                print(f"\n--- Page {page_num} ---")
                time.sleep(2)
                
                # Parse page
                soup = BeautifulSoup(self.driver.page_source, 'lxml')
                table = soup.find('table', {'id': 'auctions-table'})
                
                if not table:
                    print("✗ Auction table not found")
                    break
                
                # Extract domains
                tbody = table.find('tbody')
                if tbody:
                    rows = tbody.find_all('tr', {'data-id': True})
                    page_count = 0
                    
                    for row in rows:
                        domain_link = row.find('a', href=lambda x: x and '/clienti/licitatii/' in x)
                        if domain_link:
                            domain_name = domain_link.text.strip()
                            all_domains.append(domain_name)
                            page_count += 1
                    
                    print(f"  Collected {page_count} domains from this page")
                
                # Try to go to next page
                try:
                    next_button = self.driver.find_element(
                        By.CSS_SELECTOR, 
                        "li.paginate_button.next:not(.disabled) a"
                    )
                    print("  → Moving to next page...")
                    self.driver.execute_script("arguments[0].click();", next_button)
                    time.sleep(3)
                except:
                    print("  ✓ No more pages - done!")
                    break
            
            print(f"\n{'='*50}")
            print(f"COLLECTION COMPLETE")
            print(f"  Total pages: {page_num}")
            print(f"  Total domains: {len(all_domains)}")
            print(f"{'='*50}")
            
            return all_domains
            
        except Exception as e:
            print(f"\n✗ Error collecting domains: {str(e)}")
            import traceback
            traceback.print_exc()
            return all_domains

    def close(self):
        """Close the browser and database connection"""
        if self.db_conn:
            self.db_conn.close()
            print("✓ Database connection closed")
        if self.driver:
            self.driver.quit()


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


def _build_summary_email(day, domains_count, db_connected, db_result, saved_files, summary):
    """Build the plain-text daily run summary emailed after a successful scrape."""
    lines = [
        f"Rezumat execuție scraper domenii — {day}",
        "",
        f"Domenii colectate azi: {domains_count}",
    ]
    if not db_connected:
        lines.append("Bază de date: NECONECTATĂ (salvat doar în fișiere)")
    elif db_result is None:
        lines.append("Bază de date: EROARE la salvare")
    else:
        line = (f"Bază de date: salvat cu succes — {db_result['inserted']} noi, "
                f"{db_result['updated']} actualizate")
        if db_result.get("errors"):
            line += f", {db_result['errors']} erori"
        lines.append(line)

    if saved_files:
        lines.append("Fișiere: " + ", ".join(os.path.basename(p) for p in saved_files))

    if summary:
        pct = summary["days_covered"] / summary["days_in_year"] * 100
        lines += [
            "",
            f"Acoperire {summary['year']}: {summary['days_covered']}/{summary['days_in_year']} "
            f"zile ({pct:.1f}%), rămase {summary['days_remaining']}",
        ]

    lines += ["", "— Scraper domenii eureg.ro (rulare automată)"]
    return "\n".join(lines)


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

        if not scraper.ensure_logged_in():
            result["session_valid"] = False
            result["status"] = "login_required"
            print("\n✗ Session expired - automatic login failed, manual login required")
            if cron:
                send_alert_email(
                    "Domains Scrapper: login required",
                    "Automatic login (incl. email 2FA) failed — likely a reCAPTCHA\n"
                    "challenge on the login page.\n\n"
                    "Open the noVNC console (private/VPN), then trigger POST /login\n"
                    "and complete the verification in the browser.\n\n"
                    "The next scheduled run will work again afterwards."
                )
            return result

        result["session_valid"] = True
        domains = scraper.get_all_auction_domains()

        db_result = None
        if domains and db_connected:
            db_result = scraper.save_domains_to_db(domains)
        elif domains and not db_connected:
            print("\n⚠ Database not connected - saving to files only")

        if domains:
            saved_files = _save_domains_to_files(domains)
            summary = scraper.print_yearly_summary() if db_connected else None
            result["status"] = "ok"
            result["domains_count"] = len(domains)
            if cron:
                send_alert_email(
                    f"Scraper domenii — {date.today()}: {len(domains)} domenii",
                    _build_summary_email(date.today(), len(domains), db_connected,
                                         db_result, saved_files, summary),
                )
        else:
            result["status"] = "no_domains"
            print("\n✗ No domains collected")
            summary = scraper.print_yearly_summary() if db_connected else None
            if cron:
                send_alert_email(
                    f"Scraper domenii — {date.today()}: 0 domenii",
                    _build_summary_email(date.today(), 0, db_connected,
                                         db_result, [], summary),
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
        # Try the fully automatic path first (cookies + email 2FA). Only fall back
        # to the manual noVNC flow if that fails (e.g. a reCAPTCHA challenge).
        if scraper.ensure_logged_in():
            print("\n✓ Logged in automatically - nothing to do")
            result["success"] = True
            return result
        print("\n⚠ Automatic login failed - falling back to manual login (watch via noVNC)")
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


# Main execution
if __name__ == "__main__":
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
