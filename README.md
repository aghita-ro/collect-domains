# collect-domains

Scraper care colectează zilnic domeniile `.ro` care expiră de pe platforma de
licitații **eureg.ro** și le salvează în fișiere text și într-o bază de date
PostgreSQL.

Rulează ca un singur container Docker self-contained: un webhook Flask
(`webhook.py`) declanșează un scrape la cerere, iar Chrome rulează headful pe un
display virtual (Xvfb `:99`), vizibil prin noVNC pentru login manual la nevoie.

## Cum funcționează

- **Login automat.** Profilul Chrome persistent trece de reCAPTCHA-ul paginii de
  login, iar codul **2FA primit pe email** este citit direct din Gmail prin IMAP
  și completat automat. Nu e nevoie de intervenție manuală în mod normal.
- **Scrape.** Navighează la lista de licitații „care se încheie astăzi",
  parcurge paginile (DataTables) și extrage domeniile.
- **Salvare.** Scrie în `domains.txt`, o arhivă `domains_YYYYMMDD_HHMMSS.txt` și
  face upsert în tabelul PostgreSQL `domains` (cu data curentă ca `expiry_date`).
- **Email de rezumat.** După fiecare rulare reușită trimite un email (prin Gmail
  SMTP) cu numărul de domenii, statusul salvării în DB și acoperirea anuală.

## Configurare

Toate credențialele stau în `.env` (necommitat). Vezi `.env.example` pentru
șablonul complet. În container, `.env` se află pe volumul montat `/data`.

| Variabilă | Rol |
|---|---|
| `SCRAPER_USERNAME` / `SCRAPER_PASSWORD` | login platformă licitații |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | PostgreSQL |
| `GMAIL_IMAP_USER` / `GMAIL_IMAP_PASSWORD` | **App Password** Google (citire cod 2FA prin IMAP + trimitere email prin SMTP) |
| `WEBHOOK_TOKEN` | token secret pentru `/run`, `/status`, `/login` |
| `WEBHOOK_ALLOWED_IPS` | listă IP-uri permise pentru `/run` (gol = toate) |
| `MAILGUN_*`, `EMAIL_FROM`, `EMAIL_TO` | fallback de email dacă Gmail nu e configurat |
| `RANDOM_DELAY_MAX_MIN` | întârziere aleatoare pre-scrape (min); 0 = dezactivat |

> `GMAIL_IMAP_PASSWORD` trebuie să fie o **parolă-aplicație** Google (necesită
> verificare în 2 pași pe cont), nu parola obișnuită de Gmail.

## Rulare cu Docker

```bash
# Build
./docker_build.sh

# Pornește containerul (data dir ține .env, cookies.json, chrome_profile/, domains*.txt)
./docker_run.sh

# Declanșează un scrape
curl -X POST "http://localhost:8000/run?token=$WEBHOOK_TOKEN"
```

Implicit `docker_run.sh` pornește containerul cu `--restart no` (NU pornește la
boot). Pentru un server non-stop folosește `RESTART=unless-stopped ./docker_run.sh`.

## Rulare automată zilnică (cron)

Pentru un calculator pornit/oprit imprevizibil, `daily_run.sh` se rulează la
fiecare 5 minute prin cron și asigură **o singură rulare reușită pe zi**:

```cron
*/5 * * * * /path/to/collect-domains/daily_run.sh >/dev/null 2>&1
```

La fiecare tick: dacă ziua curentă a reușit deja (marker `data/.last_success_date`)
iese imediat; altfel pornește containerul, declanșează `/run`, așteaptă
finalul, marchează ziua (la `session_valid: true`) și **oprește containerul la
loc**. Efect: o rulare pe zi, în max ~5 min de la pornirea calculatorului,
containerul fiind oprit în rest. Log: `data/daily_run.log`.

## Login & 2FA

Login-ul se completează automat (cookies → pagina MFA → cod 2FA citit din Gmail →
trimis). Doar dacă login-ul automat eșuează (ex. reCAPTCHA cere imagini la un
cold-start total) primești un email de alertă și faci un login manual o singură
dată:

```bash
curl -X POST "http://localhost:8000/login?token=$WEBHOOK_TOKEN"
# apoi urmărește noVNC: http://localhost:7900/vnc.html (privat — doar localhost/VPN)
```

noVNC rulează **fără parolă** și TREBUIE ținut privat (port 7900 legat pe
localhost; pentru acces remote folosește un tunel SSH).

## Endpoints

- `POST /run` — declanșează un scrape (token + IP allowlist); `202` acceptat, `409` dacă unul rulează deja
- `POST /login` — flux de login manual (token), urmărit prin noVNC
- `GET /status` — stare în memorie (token)
- `GET /health` — healthcheck (public)

## Dezvoltare locală (fără Docker)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python scraper.py            # un scrape (deschide fereastra Chrome)
python scraper.py --cron     # scrape non-interactiv, email pe finalizare
python scraper.py --login    # login manual
pytest                       # testele webhook
```

## Fișiere de ieșire (pe volumul `/data`)

- `domains.txt` — lista de domenii din ultima rulare
- `domains_YYYYMMDD_HHMMSS.txt` — arhivă cu timestamp per rulare
- `cookies.json`, `chrome_profile/` — persistența sesiunii
- `.last_success_date`, `daily_run.log` — starea/logul rulării zilnice
