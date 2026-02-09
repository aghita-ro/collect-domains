#!/bin/bash
# Run this script on the EC2 instance (Ubuntu 24.04) as ubuntu user
set -e

echo "=== Installing system dependencies ==="
sudo apt update
sudo apt install -y chromium-browser python3 python3-venv python3-pip xauth git

echo "=== Cloning repository ==="
sudo mkdir -p /opt/scraper
sudo chown ubuntu:ubuntu /opt/scraper
git clone git@github.com:aghita-ro/collect-domains.git /opt/scraper

echo "=== Setting up Python venv ==="
cd /opt/scraper
python3 -m venv venv
source venv/bin/activate
pip install selenium webdriver-manager beautifulsoup4 lxml psycopg2-binary requests python-dotenv

echo "=== Creating .env ==="
echo "Create /opt/scraper/.env with your credentials:"
echo "  nano /opt/scraper/.env"
echo ""
echo "Required variables:"
echo "  SCRAPER_USERNAME=..."
echo "  SCRAPER_PASSWORD=..."
echo "  DB_HOST=..."
echo "  DB_PORT=..."
echo "  DB_NAME=..."
echo "  DB_USER=..."
echo "  DB_PASSWORD=..."
echo "  MAILGUN_DOMAIN=..."
echo "  MAILGUN_API_KEY=..."
echo "  EMAIL_FROM=..."
echo "  EMAIL_TO=..."

echo ""
echo "=== Installing systemd timer ==="
sudo cp /opt/scraper/systemd/domains-scrapper.service /etc/systemd/system/
sudo cp /opt/scraper/systemd/domains-scrapper.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable domains-scrapper.timer
sudo systemctl start domains-scrapper.timer

echo ""
echo "=== Setup complete ==="
echo ""
echo "Timer status:"
systemctl status domains-scrapper.timer --no-pager
echo ""
echo "Next steps:"
echo "  1. Create /opt/scraper/.env with your credentials"
echo "  2. Do initial manual login:  ssh -X ubuntu@this-host && cd /opt/scraper && source venv/bin/activate && python scraper.py"
echo "  3. Check timer:              systemctl list-timers domains-scrapper.timer"
echo "  4. View logs:                journalctl -u domains-scrapper.service"
