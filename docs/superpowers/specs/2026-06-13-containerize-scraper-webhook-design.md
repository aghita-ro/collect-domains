# Containerizare scraper + trigger prin webhook

**Data:** 2026-06-13
**Status:** Aprobat pentru implementare

## Obiectiv

Containerizarea aplicației `scraper.py` într-un **singur container self-contained** care:
- rulează autonom, declanșat printr-un **endpoint web (webhook)** apelat de un cron extern (`cron.2l.ro`);
- permite **login manual periodic** (CAPTCHA + verificare email) printr-un browser vizibil via noVNC, fără X11 forwarding;
- persistă sesiunea și output-ul pe volume montate;
- este **agnostic de host** (rulat cu `docker run`, fără docker-compose).

## Context

Aplicația actuală (`scraper.py`, clasa `DomainsScrapperSelenium`) colectează domenii `.ro` expirate de pe eureg.ro:
- **Sesiune:** păstrată în `cookies.json` (mecanism primar) + `chrome_profile/` (secundar). Sesiunea **expiră periodic (zile/săptămâni)** → e nevoie de un mecanism comod de re-login, nu doar o singură dată.
- **Login:** `login_manual()` cere o fereastră Chrome vizibilă pentru CAPTCHA/verificare email.
- **Cron mode (`--cron`):** headless, alertă email pe eșec (Mailgun, endpoint EU).
- **Storage:** `domains.txt`, `domains_YYYYMMDD_HHMMSS.txt`, upsert în PostgreSQL.

Deployment-ul actual (systemd timer pe EC2) e înlocuit de acest model containerizat.

## Decizii de design

| Aspect | Decizie |
|---|---|
| Numărul de containere | **Unul singur**, self-contained (fără docker-compose) |
| Trigger | **Webhook** `POST /run`, apelat de cron.2l.ro |
| Răspuns webhook | **Asincron**: `202 Accepted` imediat, scrape în fundal; `409` dacă rulează deja |
| Auth webhook | **Token secret + IP allowlist** (IP-ul cron.2l.ro) |
| Login manual | **Endpoint privat `POST /login`** + watch prin noVNC |
| Anti-detecție | **Întârziere aleatorie configurabilă** înainte de scrape (`RANDOM_DELAY_MAX_MIN`, 0 = off) |
| Display Chrome | **Xvfb `:99`** (headful pe display virtual — mereu vizibil prin noVNC, un singur mod) |

## Arhitectură

### Proces model (supervisord)

Containerul pornește `supervisord` care administrează în paralel:

1. **Xvfb** — display virtual `:99`. Chrome rulează headful pe el (renunțăm la `--headless`); mereu inspectabil prin noVNC.
2. **fluxbox** — window manager minimal (Chrome se randează corect).
3. **x11vnc** — server VNC peste display-ul `:99`, protejat cu parolă.
4. **websockify / noVNC** — acces VNC prin browser, port `7900`.
5. **gunicorn (1 worker)** — web app-ul (`webhook.py`) în prim-plan.

**Un singur worker gunicorn** este intenționat: garantează un singur scrape concomitent printr-un lock în proces (`threading.Lock` / flag global) → al doilea `/run` primește `409`.

### Endpoint-uri web (`webhook.py`, Flask servit de gunicorn)

Toate endpoint-urile sunt servite de gunicorn pe portul `8000`. Diferența de „expunere" este la nivel de **auth**, nu de port — vezi nota de la *Securitate*.

| Metodă | Rută | Auth | Comportament |
|---|---|---|---|
| `POST` | `/run` | Token + IP allowlist | `202` + scrape în fundal (după delay random); `409` dacă rulează deja |
| `GET` | `/health` | — | `200 OK` (uptime/monitoring) |
| `GET` | `/status` | Token | Stare rulare curentă + ultima rulare + `is_logged_in()` |
| `POST` | `/login` | Token | Deschide Chrome la pagina de login pe `:99`, ține sesiunea ~10 min pentru completare manuală via noVNC, salvează `cookies.json` la succes |

### Flux scraping (`POST /run`)

1. Verifică auth (token + IP). Eșec → `401`/`403`.
2. Dacă un scrape rulează deja → `409`.
3. Răspunde `202 Accepted`. Pornește thread de fundal:
   a. Așteaptă delay random `0..RANDOM_DELAY_MAX_MIN` minute (dacă > 0).
   b. Pornește Chrome pe `:99`, încarcă `cookies.json`.
   c. Dacă redirect la login → alertă email „sesiune expirată, fă login via noVNC", iese.
   d. Altfel `get_all_auction_domains()` → salvează `domains.txt` + arhivă + upsert PostgreSQL.
   e. Alertă email pe 0 domenii / eroare neașteptată (logica actuală).
   f. Eliberează lock-ul, închide Chrome.

### Flux login manual (`POST /login`)

1. Verifică auth (privat).
2. Deschide Chrome pe `:99`, navighează la pagina de login, completează credențialele (`_fill_field()`), apasă login.
3. Ține sesiunea deschisă ~10 min, polling pe URL pentru detectarea dashboard-ului.
4. Utilizatorul deschide noVNC (`http://host:7900` prin VPN/tunel), completează CAPTCHA + verificarea email.
5. La detectarea login-ului: salvează `cookies.json`, închide Chrome, răspunde succes. Timeout → eșec.

Reutilizează logica din `login_manual()` actuală, fără prompturile `input()`.

## Modificări de cod în `scraper.py`

- **Driver:** Chrome local pe `DISPLAY=:99`, fără `webdriver-manager` la runtime (chromedriver instalat în imagine, cale fixă). Fără `--headless`.
- **Refactor login:** variantă fără `input()` apelabilă din `/login`.
- **Extragere funcție de scrape:** logica unei rulări într-o funcție apelabilă din web handler (returnează rezultat/eroare în loc de `sys.exit`).
- `--cron` rămâne disponibil pentru rulare manuală/debug din `docker exec`.

## Fișiere noi / modificate

| Fișier | Rol |
|---|---|
| `Dockerfile` | Imagine: Python + Chrome + chromedriver + Xvfb + fluxbox + x11vnc + noVNC + supervisord |
| `supervisord.conf` | Definește procesele (Xvfb, fluxbox, x11vnc, websockify, gunicorn) |
| `entrypoint.sh` | Inițializare (permisiuni volume, parolă VNC din env, lansare supervisord) |
| `webhook.py` | App Flask cu endpoint-urile; lock pentru singleton; thread de fundal |
| `.dockerignore` | Exclude `venv/`, `chrome_profile/`, `domains_*.txt`, etc. |
| `requirements.txt` | + `flask`, `gunicorn` |
| `scraper.py` | Modificările de mai sus |
| `CLAUDE.md` | Actualizare secțiune deployment (Docker în loc de systemd) |

## Configurație (`.env`) — variabile noi

```
WEBHOOK_TOKEN=<token secret lung>
WEBHOOK_ALLOWED_IPS=<ip cron.2l.ro>[,ip2,...]
VNC_PASSWORD=<parola noVNC>
RANDOM_DELAY_MAX_MIN=<int, 0 = dezactivat>
```

Variabilele existente (`SCRAPER_*`, `DB_*`, `MAILGUN_*`, `EMAIL_*`) rămân.

## Volume montate (persistență)

- `.env` (config + credențiale)
- `cookies.json` (sesiune — mecanism primar)
- `chrome_profile/` (profil Chrome — secundar)
- director output `domains*.txt`

## Securitate / expunere porturi

Două porturi se publică din container:

| Port | Rute | Protecție |
|---|---|---|
| `8000` | `/run`, `/health`, `/status`, `/login` | Toate cer token (mai puțin `/health`); `/run` în plus IP allowlist. Portul poate fi expus public — auth-ul e per rută. |
| `7900` | noVNC | Parolă VNC; legat **doar de interfața VPN/localhost** sau accesat prin tunel SSH; **niciodată public**. |

**Recomandare de hardening:** deși auth-ul e per rută, `/login` și `/status` declanșează acțiuni sensibile (deschid browser-ul logat, expun starea). Ideal, portul `8000` se publică doar pentru IP-ul cron.2l.ro la nivel de firewall/VPN al host-ului, iar `/login`/`/status` se accesează prin tunel — token-ul rămâne a doua linie de apărare.

noVNC public = oricine cu URL-ul controlează un browser logat → interdicție strictă.

## Non-obiective (YAGNI)

- Fără docker-compose.
- Fără Selenium Grid / containere multiple.
- Fără UI web dincolo de cele 4 endpoint-uri.
- Fără rotație/curățare automată a arhivelor `domains_*.txt`.
- Fără HTTPS terminat în container (se presupune reverse proxy / VPN pe host dacă e nevoie).

## Operare

1. `docker run` cu porturile și volumele montate (publici: 8000; privat prin VPN: 7900).
2. Login inițial: `POST /login` → watch noVNC → completare manuală.
3. Setare cronjob pe cron.2l.ro: `POST http://host:8000/run` cu token, zilnic.
4. La expirarea sesiunii: alertă email → re-login prin `/login` + noVNC.
