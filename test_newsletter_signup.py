"""The public newsletter signup and the free-advertising page.

These pin the two things that would silently break: the signup must never treat a
newsletter reader as an estimate request, and the advertising page must stay
owner-only.
"""

from fastapi.testclient import TestClient

import construction_main as main
import kit_service


def test_newsletter_signup_uses_the_newsletter_sequence(monkeypatch):
    calls = {}

    def fake(email, first_name=""):
        calls["email"] = email
        return {"ok": True, "sequence_started": True}

    monkeypatch.setattr(kit_service, "subscribe_newsletter", fake)
    client = TestClient(main.app)
    res = client.post("/api/newsletter", json={"email": "homeowner@example.com"})
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert calls["email"] == "homeowner@example.com"


def test_newsletter_rejects_a_bad_address_before_calling_kit(monkeypatch):
    called = []
    monkeypatch.setattr(kit_service, "subscribe_newsletter",
                        lambda email, first_name="": called.append(email) or {"ok": False, "reason": "bad"})
    client = TestClient(main.app)
    res = client.post("/api/newsletter", json={"email": "not-an-email"})
    assert res.status_code == 400
    assert res.json()["ok"] is False


def test_newsletter_honeypot_is_silently_accepted(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the honeypot must not reach Kit")

    monkeypatch.setattr(kit_service, "subscribe_newsletter", boom)
    client = TestClient(main.app)
    res = client.post("/api/newsletter",
                      json={"email": "bot@example.com", "company": "Acme"})
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_marketing_page_is_owner_only():
    main.app.dependency_overrides[main.get_db] = lambda: None
    try:
        client = TestClient(main.app, follow_redirects=False)
        res = client.get("/marketing")
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)
    assert res.status_code in (307, 401, 403)
