import importlib
import os
import threading

import pytest


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("WEBHOOK_TOKEN", "secret123")
    monkeypatch.setenv("WEBHOOK_ALLOWED_IPS", "")  # allowlist disabled for most tests
    monkeypatch.setenv("RANDOM_DELAY_MAX_MIN", "0")
    import webhook
    importlib.reload(webhook)
    # Never actually run a scrape during tests.
    monkeypatch.setattr(webhook.scraper_mod, "run_scrape_job",
                        lambda cron=True: {"status": "ok", "domains_count": 0,
                                           "session_valid": True, "error": None})
    webhook.app.config["TESTING"] = True
    return webhook.app.test_client(), webhook


def test_health_no_auth(client):
    c, _ = client
    assert c.get("/health").status_code == 200


def test_run_requires_token(client):
    c, _ = client
    assert c.post("/run").status_code == 401


def test_run_accepts_with_token(client):
    c, mod = client
    r = c.post("/run", headers={"X-Webhook-Token": "secret123"})
    assert r.status_code == 202
    # Let the background thread finish so it releases the lock.
    for t in threading.enumerate():
        if t.name == "scrape-job":
            t.join(timeout=5)


def test_run_conflict_when_locked(client):
    c, mod = client
    mod._lock.acquire()  # simulate an in-progress run
    try:
        r = c.post("/run", headers={"X-Webhook-Token": "secret123"})
        assert r.status_code == 409
    finally:
        mod._lock.release()


def test_run_ip_allowlist(monkeypatch):
    monkeypatch.setenv("WEBHOOK_TOKEN", "secret123")
    monkeypatch.setenv("WEBHOOK_ALLOWED_IPS", "203.0.113.5")
    monkeypatch.setenv("RANDOM_DELAY_MAX_MIN", "0")
    import webhook
    importlib.reload(webhook)
    c = webhook.app.test_client()
    # Default test client remote_addr (127.0.0.1) is not in the allowlist.
    r = c.post("/run", headers={"X-Webhook-Token": "secret123"})
    assert r.status_code == 403
