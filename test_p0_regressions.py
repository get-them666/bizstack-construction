"""Regressions for the P0 production fixes (2026-11-23 audit).

Covers the four defects that had live user impact:

1. inbound call forwarding reused a closed psycopg cursor, so every call fell
   through to the AI and the dashboard toggle did nothing;
2. a successful card payment raised ``NameError: new_collected`` after the
   money was already recorded;
3. inbound webhooks could only carry the secret in a header, while SignalWire's
   LaML configuration has no custom-header field;
4. project-sourced portal accounts linked the project id as a bid id.

Everything here is offline: fake DB cursors and monkeypatched service calls.
"""

import asyncio
import json
import os

import pytest
from starlette.requests import Request

import construction_main as cm
import job_documents
import portal_service


class _ClosedAwareCursor:
    """A cursor that behaves like psycopg's: unusable after its with-block."""

    def __init__(self, rows):
        self.rows = rows
        self.closed = True

    def __enter__(self):
        self.closed = False
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def execute(self, sql, params=()):
        if self.closed:
            raise RuntimeError("the cursor is closed")
        return None

    def fetchone(self):
        if self.closed:
            raise RuntimeError("the cursor is closed")
        return self.rows[0] if self.rows else None


class _FakeDB:
    def __init__(self, mapping):
        self.mapping = mapping

    def cursor(self):
        # Each call returns a fresh cursor, exactly like psycopg.
        return _ClosedAwareCursor(self.mapping)


def _request(query: bytes = b"", headers=None):
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/comms/sms-webhook",
        "query_string": query,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    return Request(scope)


def test_voice_forwarding_uses_a_fresh_cursor_and_dials_the_owner(monkeypatch):
    monkeypatch.setenv("CALL_FORWARDING_ALWAYS", "1")

    class _TwoQueryDB:
        def __init__(self):
            self.calls = 0

        def cursor(self):
            self.calls += 1
            if self.calls == 1:
                return _ClosedAwareCursor([{"value": "true"}])
            return _ClosedAwareCursor([{"value": "+12526655891"}])

    response = asyncio.run(cm.voice_swml(db=_TwoQueryDB()))
    body = json.loads(response.body)
    dial = body["sections"]["main"][2]["dial"]
    assert dial["number"] == "+12526655891"
    assert "ai" not in body["sections"]["main"][1]


def test_voice_forwarding_falls_back_to_ai_when_disabled(monkeypatch):
    monkeypatch.setenv("CALL_FORWARDING_ALWAYS", "1")
    monkeypatch.setenv("VAPI_INBOUND_SIP_URI", "off")
    db = _FakeDB([{"value": "false"}])
    response = asyncio.run(cm.voice_swml(db=db))
    body = json.loads(response.body)
    assert "ai" in body["sections"]["main"][1]


def test_apply_card_payment_returns_the_new_collected_total(monkeypatch):
    invoice = {"id": 7, "amount": 1000.0, "collected": 400.0, "status": "partial"}
    calls = {}

    monkeypatch.setattr(cm.portal_service, "record_payment",
                        lambda *a, **k: ({}, True))
    monkeypatch.setattr(cm.bid_costing, "ensure_accounting_schema", lambda db: None)
    monkeypatch.setattr(cm.bid_costing, "list_invoices",
                        lambda db, bid, company: [invoice])

    def _set_status(db, invoice_id, status, company, pay):
        calls.update(invoice_id=invoice_id, status=status, pay=pay)

    monkeypatch.setattr(cm.bid_costing, "set_invoice_status", _set_status)
    monkeypatch.setattr(cm.ledger_sync, "ensure_schema", lambda db: None)
    monkeypatch.setattr(cm.ledger_sync, "sync_invoice_status", lambda *a, **k: None)
    monkeypatch.setattr(cm.zoho_books_sync, "record_payment_in_zoho", lambda *a, **k: None)
    monkeypatch.setattr(cm.portal_service, "mark_zoho_pushed", lambda *a, **k: None)

    out = cm._apply_card_payment(
        object(), bid_id=42, invoice_id=7, payment_id="pay_1",
        amount_cents=60000, payments_session_id="sess_1", raw="")

    assert out == {"ok": True, "duplicate": False, "paid": 600.0,
                   "invoice_status": "paid", "collected": 1000.0}
    # set_invoice_status receives the delta, not the cumulative total.
    assert calls == {"invoice_id": 7, "status": "paid", "pay": 600.0}


def test_inbound_webhook_accepts_the_secret_in_the_query_string(monkeypatch):
    monkeypatch.setenv("INBOUND_WEBHOOK_SECRET", "s3cret")
    cm._verify_inbound_webhook(_request(query=b"secret=s3cret"))


def test_inbound_webhook_still_accepts_the_header(monkeypatch):
    monkeypatch.setenv("INBOUND_WEBHOOK_SECRET", "s3cret")
    cm._verify_inbound_webhook(_request(headers={"X-Inbound-Secret": "s3cret"}))


def test_inbound_webhook_rejects_a_wrong_secret(monkeypatch):
    monkeypatch.setenv("INBOUND_WEBHOOK_SECRET", "s3cret")
    with pytest.raises(Exception) as exc:
        cm._verify_inbound_webhook(_request(query=b"secret=wrong"))
    assert getattr(exc.value, "status_code", None) == 403


def test_review_requests_is_a_module_global():
    # The old local import inside the review-loop setup made the module-level
    # _run_review_requests() raise NameError on every pass.
    assert cm.review_requests is not None
    assert hasattr(cm.review_requests, "ensure_schema")


class _PortalDB:
    def __init__(self, value):
        self.value = value
        self.linked = []

    def cursor(self):
        outer = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=()):
                assert "app_settings" in sql
                assert params == ("project_35_bid_id",)

            def fetchone(self):
                return {"value": outer.value} if outer.value is not None else None

        return _Cur()


def test_project_portal_links_the_synthetic_bid_not_the_project(monkeypatch):
    db = _PortalDB("999")
    monkeypatch.setattr(
        portal_service, "ensure_client_for_bid",
        lambda conn, bid: {"client_id": 5, "email": "client@example.com",
                           "created": True, "password": "pw"})
    monkeypatch.setattr(
        portal_service, "link_bid",
        lambda conn, client_id, bid_id: db.linked.append((client_id, bid_id)))

    result = job_documents.provision_portal_account(db, {
        "id": 35, "source": "project", "contact_email": "client@example.com",
        "contact_name": "Test Client", "name": "Kitchen and Bath",
    })

    assert result["email"] == "client@example.com"
    assert db.linked == [(5, 999)]


def test_project_portal_refuses_when_there_is_no_signing_request(monkeypatch):
    db = _PortalDB(None)
    monkeypatch.setattr(
        portal_service, "link_bid",
        lambda conn, client_id, bid_id: db.linked.append((client_id, bid_id)))

    result = job_documents.provision_portal_account(db, {
        "id": 35, "source": "project", "contact_email": "client@example.com",
        "contact_name": "Test Client", "name": "Kitchen and Bath",
    })

    assert result["created"] is False
    assert "signing request" in result["error"]
    assert db.linked == []
