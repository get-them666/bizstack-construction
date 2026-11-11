"""Money-path tests for the Zoho Payments webhook.

Three bugs lived on this path, all of which either invented money or put it on
the wrong customer:

1. The amount was converted by guessing units from its size. $500 sent in
   minor units (50000) became $50,000, and $120,000 sent in major units
   (120000) became $1,200.
2. An unmatched reference fell back to SELECT id FROM payments ... and used
   that payments.id as a leads.id, attributing the money to a different lead.
3. Zoho retries webhooks; every retry inserted another payments row and
   posted another ledger entry.

These tests call the real handler with a fake db/cursor and the real
today.record_payment; only the ledger writes are stubbed (they are not what is
under test). No database and no network.
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import construction_main  # noqa: E402
import today  # noqa: E402

KEY = "test-zoho-webhook-key"


class FakeCursor:
    """Just enough SQL understanding for the webhook + today.record_payment."""

    def __init__(self, db):
        self.db = db
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        sql_norm = " ".join(sql.split())
        low = sql_norm.lower()
        params = tuple(params or ())
        self.db.queries.append((sql_norm, params))
        self._row = None

        if low.startswith("insert into payments"):
            self.db.inserts += 1
            row = {
                "id": self.db.next_payment_id,
                "lead_id": params[0],
                "amount_cents": params[1],
                "company": params[2],
                "method": params[3],
                "kind": params[4],
                "memo": params[5],
            }
            self.db.next_payment_id += 1
            self.db.payments.append(row)
            self._row = {"id": row["id"]}
        elif "strpos(coalesce(memo" in low:
            marker = params[0]
            for payment in self.db.payments:
                if marker in (payment.get("memo") or ""):
                    self._row = {"id": payment["id"]}
                    break
        elif "from leads where id = %s" in low:
            lead = self.db.leads.get(int(params[0]))
            self._row = dict(lead) if lead else None
        elif "from quotes" in low and "quote_no" in low:
            lead_id = self.db.quotes.get(str(params[0]).upper())
            self._row = {"lead_id": lead_id} if lead_id else None
        elif "from bid_invoices" in low:
            kind = None
            if "invoice_number = %s" in low:
                kind = self.db.invoices.get(str(params[0]))
            elif "id = %s" in low:
                kind = self.db.invoices_by_id.get(int(params[0]))
            self._row = {"kind": kind} if kind else None
        elif low.startswith("update leads"):
            self.db.lead_updates.append(params)

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row] if self._row else []


class FakeDB:
    def __init__(self, leads=None, quotes=None, invoices=None, invoices_by_id=None):
        self.leads = {lead["id"]: lead for lead in (leads or [])}
        self.quotes = quotes or {}
        self.invoices = invoices or {}
        self.invoices_by_id = invoices_by_id or {}
        self.payments = []
        self.inserts = 0
        self.next_payment_id = 100
        self.lead_updates = []
        self.queries = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _lead(lead_id=7):
    return {
        "id": lead_id,
        "name": "Amy",
        "email": "amy@example.com",
        "status": "new",
        "deposit_cents": None,
    }


def _request(payload: dict) -> Request:
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(KEY.encode(), body, hashlib.sha256).hexdigest()
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/payments/zoho/webhook",
        "raw_path": b"/api/payments/zoho/webhook",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"x-zoho-webhook-signature", signature.encode()),
        ],
    }
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _post(payload, db):
    response = asyncio.run(
        construction_main.zoho_payments_webhook(_request(payload), db=db)
    )
    return response, json.loads(response.body)


@pytest.fixture
def ledger_calls(monkeypatch):
    monkeypatch.setattr(construction_main, "ZOHO_PAYMENTS_WEBHOOK_KEY", KEY)
    # record_payment's DDL is not what these tests exercise.
    monkeypatch.setattr(today, "_SCHEMA_READY", True)
    calls = {"deposit": 0, "ledger": 0}

    def fake_deposit(*args, **kwargs):
        calls["deposit"] += 1
        return {"posted": True}

    def fake_post(*args, **kwargs):
        calls["ledger"] += 1
        return {"posted": True}

    monkeypatch.setattr(today.ledger_bridge, "post_deposit", fake_deposit)
    monkeypatch.setattr(today.ledger, "post", fake_post)
    return calls


def test_explicit_minor_units_are_not_multiplied_again(ledger_calls):
    """50000 minor units is $500, not $50,000."""
    db = FakeDB(leads=[_lead(7)])
    payload = {
        "event": "payment.captured",
        "data": {
            "payment_id": "zp_minor_1",
            "amount": 50000,
            "amount_cents": 50000,
            "reference": "DEP-00007",
        },
    }

    response, body = _post(payload, db)

    assert response.status_code == 200
    assert body == {"received": True}
    assert db.inserts == 1
    assert db.payments[0]["amount_cents"] == 50000
    assert db.payments[0]["lead_id"] == 7


def test_major_units_are_scaled_once_even_when_large(ledger_calls):
    """$120,000.00 is 12,000,000 cents, not 120,000 cents ($1,200)."""
    db = FakeDB(leads=[_lead(7)])
    payload = {
        "event": "payment.captured",
        "data": {
            "payment_id": "zp_major_1",
            "amount": "$120,000.00",
            "reference": "DEP-00007",
        },
    }

    response, body = _post(payload, db)

    assert response.status_code == 200
    assert body == {"received": True}
    assert db.inserts == 1
    assert db.payments[0]["amount_cents"] == 12_000_000


def test_unknown_reference_records_unattributed_and_marks_no_lead(ledger_calls):
    """A payments.id must never become a leads.id."""
    db = FakeDB(leads=[_lead(7)])
    # A payment row exists whose id (9) is not a lead. The old fallback would
    # find this row by memo and use 9 as the lead id.
    db.payments.append({"id": 9, "memo": "MYSTERY-42 deposit", "lead_id": 7})
    payload = {
        "event": "payment.captured",
        "data": {
            "payment_id": "zp_unknown_1",
            "amount": 250,
            "reference": "MYSTERY-42",
        },
    }

    response, body = _post(payload, db)

    assert response.status_code == 200
    assert body == {"received": True}
    assert db.inserts == 1
    recorded = db.payments[-1]
    assert recorded["lead_id"] is None
    assert recorded["amount_cents"] == 25000
    assert db.lead_updates == [], "no lead may be marked paid when attribution is unknown"


def test_deposit_invoice_paid_through_inv_link_stays_a_deposit(ledger_calls):
    """An INV- reference alone used to book a held deposit as progress revenue."""
    db = FakeDB(leads=[_lead(7)], invoices={"INV-2026-0001": "deposit"})
    payload = {
        "event": "payment.captured",
        "data": {
            "payment_id": "zp_invoice_1",
            "amount": "500.00",
            "reference": "INV-2026-0001",
        },
    }

    response, body = _post(payload, db)

    assert response.status_code == 200
    assert body == {"received": True}
    assert db.payments[0]["kind"] == "deposit"
    assert db.payments[0]["amount_cents"] == 50000
    assert ledger_calls["deposit"] == 1


def test_replayed_webhook_inserts_once_and_posts_once(ledger_calls):
    """Zoho retries. The second delivery must not mint another payment."""
    db = FakeDB(leads=[_lead(7)])
    payload = {
        "event": "payment.captured",
        "data": {
            "payment_id": "zp_dupe_1",
            "amount": "500.00",
            "reference": "DEP-00007",
        },
    }

    first, first_body = _post(payload, db)
    second, second_body = _post(payload, db)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first_body == second_body == {"received": True}
    assert db.inserts == 1
    assert len(db.payments) == 1
    assert len(db.lead_updates) == 1
    assert ledger_calls["deposit"] == 1
    assert ledger_calls["ledger"] == 0
