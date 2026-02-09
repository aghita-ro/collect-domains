from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import time
import os
import argparse
import requests as http_requests
from datetime import date
from dotenv import load_dotenv

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

# Try to import psycopg2
try:
    import psycopg2
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False
    print("Warning: psycopg2 not installed. Database features disabled.")


def send_alert_email(subject, body):
    """Send an alert email via Mailgun API"""
    if not MAILGUN_API_KEY or not MAILGUN_DOMAIN:
        print("✗ Mailgun not configured - skipping email alert")
        return False
    try:
        response = http_requests.post(
            f"https://api.eu.mailgun.net/v3/{MAILGUN_DOMAIN}/messages",
            auth=("api", MAILGUN_API_KEY),
            data={
                "from": EMAIL_FROM,
                "to": EMAIL_TO,
                "subject": subject,
                "text": body,
            },
        )
        if response.status_code == 200:
            print(f"✓ Alert email sent to {EMAIL_TO}")
            return True
        else:
            print(f"✗ Mailgun error {response.status_code}: {response.text}")
            return False
    except Exception as e:
        print(f"✗ Failed to send alert email: {str(e)}")
        return False


class DomainsScrapperSelenium:
    def __init__(self, username, password, headless=False):
        self.username = username
        self.password = password
        self.base_url = "https://www.eureg.ro"
        self.db_conn = None
        
        # Create a persistent profile directory
        self.profile_dir = os.path.join(os.getcwd(), "chrome_profile")
        if not os.path.exists(self.profile_dir):
            os.makedirs(self.profile_dir)
            print(f"Created profile directory: {self.profile_dir}")
        
        # Setup Chrome options with persistent profile
        chrome_options = Options()
        chrome_options.add_argument(f"user-data-dir={self.profile_dir}")
        chrome_options.add_argument("--profile-directory=Default")
        
        if headless:
            chrome_options.add_argument("--headless")
        
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        
        # Initialize driver
        print("Initializing Chrome with persistent profile...")
        service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=service, options=chrome_options)
        self.wait = WebDriverWait(self.driver, 30)
        print(f"✓ Using profile: {self.profile_dir}")
    
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
        """Save domains to database with current date as expiry_date"""
        if not self.db_conn:
            print("✗ No database connection - skipping database save")
            return False
        
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
            
            return True
            
        except Exception as e:
            print(f"\n✗ Database error: {str(e)}")
            self.db_conn.rollback()
            return False
    
    def is_logged_in(self):
        """Check if we're currently logged in"""
        try:
            print("\nChecking login status...")
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
    
    def login_manual(self):
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
            username_field.clear()
            username_field.send_keys(self.username)
            
            password_field = self.driver.find_element(By.ID, "pass")
            password_field.clear()
            password_field.send_keys(self.password)
            
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
            print("\nScript will wait up to 3 minutes...")
            print("="*70 + "\n")
            
            # Wait for login completion
            for i in range(180):
                time.sleep(1)
                current_url = self.driver.current_url
                
                # Parse URL path only
                parsed = urlparse(current_url)
                url_path = parsed.path.lower()
                
                # Check if we're on dashboard (not login page)
                if '/dashboard' in url_path or ('/login' not in url_path and '/conectare' not in url_path and '/clienti/' in url_path):
                    print(f"\n✓ Login successful! (took {i+1} seconds)")
                    print(f"  Final URL: {current_url}")
                    print("\n✓ Session saved in Chrome profile!")
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


# Main execution
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Domains Scrapper")
    parser.add_argument("--cron", action="store_true",
                        help="Run in cron mode: headless, no manual login, email alert on failure")
    args = parser.parse_args()

    USERNAME = os.getenv("SCRAPER_USERNAME", "")
    PASSWORD = os.getenv("SCRAPER_PASSWORD", "")

    # Change to script directory so chrome_profile and output files
    # are always relative to the script, not the cron working directory
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    scraper = None

    try:
        # Initialize with persistent profile
        scraper = DomainsScrapperSelenium(USERNAME, PASSWORD, headless=args.cron)

        # Connect to database
        db_connected = scraper.connect_db()

        # Check if already logged in
        if scraper.is_logged_in():
            print("\n✓✓✓ Already logged in - skipping login! ✓✓✓")
        else:
            if args.cron:
                # Cron mode: can't do manual login, send alert and exit
                print("\n✗ Session expired - manual login required")
                send_alert_email(
                    "Domains Scrapper: login required",
                    "The session has expired and manual login is needed.\n\n"
                    "SSH into the EC2 instance with X11 forwarding and run:\n"
                    "  ssh -X user@ec2-host\n"
                    "  cd /path/to/collect-domains\n"
                    "  source venv/bin/activate\n"
                    "  python scraper.py\n\n"
                    "Complete the login in the browser, then the cron job will "
                    "work again on the next scheduled run."
                )
                exit(1)
            else:
                print("\n⚠ Not logged in - manual login required")
                if not scraper.login_manual():
                    print("\n✗ Login failed - exiting")
                    exit(1)

        # Collect domains
        print("\n" + "="*50)
        print("STARTING DOMAIN COLLECTION")
        print("="*50)

        domains = scraper.get_all_auction_domains()

        # Save to database
        if domains and db_connected:
            scraper.save_domains_to_db(domains)
        elif domains and not db_connected:
            print("\n⚠ Database not connected - saving to files only")

        # Save results to files (always, as backup)
        if domains:
            output_file = "domains.txt"
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            timestamped_file = f"domains_{timestamp}.txt"

            # Save to both files
            with open(output_file, 'w', encoding='utf-8') as f:
                for domain in domains:
                    f.write(f"{domain}\n")

            with open(timestamped_file, 'w', encoding='utf-8') as f:
                for domain in domains:
                    f.write(f"{domain}\n")

            print(f"\n✓ Saved {len(domains)} domains to:")
            print(f"  - {output_file}")
            print(f"  - {timestamped_file}")

            # Display sample
            print("\nFirst 20 domains:")
            for i, domain in enumerate(domains[:20], 1):
                print(f"  {i}. {domain}")

            if len(domains) > 20:
                print(f"  ... and {len(domains) - 20} more")
        else:
            print("\n✗ No domains collected")
            if args.cron:
                send_alert_email(
                    "Domains Scrapper: no domains collected",
                    "The scraper ran but collected 0 domains.\n"
                    "This may indicate a page structure change or a session issue."
                )

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    except Exception as e:
        print(f"\n✗ Unexpected error: {str(e)}")
        import traceback
        traceback.print_exc()
        if args.cron:
            send_alert_email(
                "Domains Scrapper: unexpected error",
                f"The scraper crashed with an error:\n\n{str(e)}"
            )
    finally:
        if scraper:
            if not args.cron:
                input("\nPress Enter to close browser...")
            scraper.close()
