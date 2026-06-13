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
