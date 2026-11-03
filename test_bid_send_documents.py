"""Sending the package to the PNC / Bill.com document inbox.

It is deliberately a confirmed action, not a background side effect: the card
names the recipient and the revision, and the route refuses a malformed address
instead of silently mailing a customer's paperwork nowhere.

No network: the SMTP send is faked; the address lookup is env/settings only.
"""

import asyncio
import inspect
from pathlib import Path

import documents_service
import construction_main


def test_the_recipient_comes_from_settings_then_environment(monkeypatch):
    monkeypatch.setattr(construction_main, "_get_setting",
                        lambda db, key: "shaunoleary@bill.com")
    assert construction_main._docs_recipient(None) == "shaunoleary@bill.com"

    monkeypatch.setattr(construction_main, "_get_setting", lambda db, key: "")
    monkeypatch.setenv("PNC_DOCS_EMAIL", "pnc@example.com")
    assert construction_main._docs_recipient(None) == "pnc@example.com"


def test_send_email_accepts_several_attachments():
    params = inspect.signature(documents_service.send_email).parameters
    assert "attachments" in params
    src = Path("documents_service.py").read_text(encoding="utf-8")
    assert "for extra, extra_name in (attachments or [])" in src


def test_the_route_emails_the_package_to_pnc(monkeypatch):
    sent = []

    async def fake_send(cfg, to, subject, body, **kwargs):
        sent.append({"to": to, "subject": subject, "body": body,
                     "attachments": kwargs.get("attachments")})
        return True

    monkeypatch.setattr(construction_main, "require_admin", lambda request: None)
    monkeypatch.setattr(construction_main, "_docs_recipient",
                        lambda db: "shaunoleary@bill.com")
    monkeypatch.setattr(construction_main.bid_opportunities, "get_bid",
                        lambda db, bid_id, company: {
                            "id": bid_id, "name": "520 King George",
                            "location": "520 King George Rd", "revision": 2,
                            "bid_amount": 2980.64})
    monkeypatch.setattr(construction_main.bid_costing, "ensure_accounting_schema",
                        lambda db: None)
    monkeypatch.setattr(construction_main.bid_costing, "cost_lines",
                        lambda *a, **k: [])
    monkeypatch.setattr(construction_main, "_bid_email_attachments",
                        lambda *a, **k: [(b"%PDF-1.4", "BuildStack-SOW-Bid-18350.pdf")])
    monkeypatch.setattr(construction_main.documents_service, "smtp_config_from_env",
                        lambda: {"SMTP_FROM": "hello@bizstackperks.com"})
    monkeypatch.setattr(construction_main.documents_service, "send_email", fake_send)

    response = asyncio.run(construction_main.bid_send_documents(
        18350, object(), to_email="", db=None))
    assert response.status_code == 303
    assert "documents_sent=shaunoleary%40bill.com" in response.headers["location"]
    assert sent[0]["to"] == "shaunoleary@bill.com"
    assert "Revision 2" in sent[0]["subject"]
    assert sent[0]["attachments"][0][1].endswith(".pdf")


def test_a_bad_address_is_refused_before_anything_is_sent(monkeypatch):
    sent = []

    async def fake_send(*a, **k):
        sent.append(a)
        return True

    monkeypatch.setattr(construction_main, "require_admin", lambda request: None)
    monkeypatch.setattr(construction_main.bid_opportunities, "get_bid",
                        lambda db, bid_id, company: {"id": bid_id, "revision": 0})
    monkeypatch.setattr(construction_main, "_docs_recipient", lambda db: "not an email")
    monkeypatch.setattr(construction_main.documents_service, "send_email", fake_send)

    response = asyncio.run(construction_main.bid_send_documents(
        18350, object(), to_email="", db=None))
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert sent == []


def test_the_card_and_settings_expose_the_send_action():
    bid = Path("templates/construction/bid_detail.html").read_text(encoding="utf-8")
    settings = Path("templates/construction/settings.html").read_text(encoding="utf-8")
    assert "/api/bids/{{ b.id }}/send-documents" in bid
    assert "Send documents to PNC" in bid
    assert "return confirm('Send Bid #" in bid
    assert 'name="pnc_docs_email"' in settings
    assert "Documents email (PNC)" in settings
