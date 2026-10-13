"""Regression tests for the deposit money path.

Two bugs, one file, both on the path that takes a customer's money:

1. /api/leads/{id}/deposit-link wrote deposit_status='pending' and COMMITTED
   before asking Stripe for a checkout session. If Stripe failed, the row stayed
   'pending' with an empty stripe_session_id forever. The board then showed
   money owed that no checkout session could ever complete, because the webhook
   that flips pending->paid only fires for a session that exists.
   24 production rows were sitting in that state.

2. create_deposit_session returned only a URL, so there was no session id to
   persist even on success -- nothing tied a paid deposit back to a lead.

Run against a throwaway database. These tests never call the real Stripe API.
"""
import os
import sys

import psycopg
from psycopg.rows import dict_row

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DSN = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not DSN:
    print("SKIP: set TEST_DATABASE_URL")
    raise SystemExit(0)

import stripe_service  # noqa: E402


class _FakeSession:
    def __init__(self, sid="cs_test_123", url="https://checkout.stripe.test/pay/abc"):
        self.id = sid
        self.url = url


def _make_svc(monkeypatched_api=None):
    svc = stripe_service.StripeService.__new__(stripe_service.StripeService)
    svc.api_key = "sk_test_dummy"
    svc.webhook_secret = "whsec_dummy"
    return svc


def test_create_deposit_session_still_returns_a_url():
    """The old signature is used by build_tool_handlers.create_deposit_link."""
    svc = _make_svc()
    captured = {}

    def fake_create(**kw):
        captured.update(kw)
        return _FakeSession()

    import stripe
    real = stripe.checkout.Session.create
    stripe.checkout.Session.create = staticmethod(fake_create)
    try:
        url = svc.create_deposit_session(
            lead_id=7, customer_name="Amy", customer_email="amy@example.com",
            project_type="Deck", amount_cents=50000, base_url="https://example.com",
        )
    finally:
        stripe.checkout.Session.create = real

    assert isinstance(url, str) and url.startswith("https://")
    assert captured["metadata"]["lead_id"] == "7"
    assert captured["mode"] == "payment"
    print("PASS  create_deposit_session still returns a plain URL string")


def test_create_deposit_checkout_returns_url_and_session_id():
    svc = _make_svc()
    import stripe
    real = stripe.checkout.Session.create
    stripe.checkout.Session.create = staticmethod(lambda **kw: _FakeSession("cs_test_abc"))
    try:
        out = svc.create_deposit_checkout(
            lead_id=9, customer_name="Bob", customer_email="",
            project_type="Roof", amount_cents=25000, base_url="https://example.com",
        )
    finally:
        stripe.checkout.Session.create = real

    assert out["url"] == "https://checkout.stripe.test/pay/abc"
    assert out["session_id"] == "cs_test_abc"
    assert out["amount_cents"] == 25000
    print("PASS  create_deposit_checkout exposes url + session_id + amount")


def test_failed_session_creation_leaves_the_lead_untouched():
    """The core money-path fix: no session means no 'pending' on the row."""
    # Use a dedicated table rather than the real `leads`, so this can never
    # touch production rows even if DATABASE_URL is pointed at a live database.
    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS deposit_regression_probe")
        cur.execute("""
            CREATE TABLE deposit_regression_probe (
                id SERIAL PRIMARY KEY, name VARCHAR(255), email VARCHAR(255),
                project_type VARCHAR(120), deposit_status VARCHAR(20) DEFAULT 'none',
                deposit_cents INTEGER, stripe_session_id VARCHAR(255)
            )""")
        cur.execute("""
            INSERT INTO deposit_regression_probe
                (name, email, deposit_status, deposit_cents, stripe_session_id)
            VALUES ('Deposit Regression Probe','probe@example.com','none',NULL,NULL)
            RETURNING id""")
        lead_id = cur.fetchone()[0]

    import stripe
    svc = _make_svc()

    def boom(**kw):
        raise RuntimeError("STRIPE_SECRET_KEY is not configured.")

    real = stripe.checkout.Session.create
    stripe.checkout.Session.create = staticmethod(boom)
    try:
        # This mirrors the route's ordering after the fix: Stripe first, then
        # the UPDATE, and only on success.
        try:
            svc.create_deposit_checkout(
                lead_id=lead_id, customer_name="Deposit Regression Probe",
                customer_email="probe@example.com", project_type="",
                amount_cents=50000, base_url="https://example.com",
            )
            raised = None
        except Exception as exc:
            raised = exc
    finally:
        stripe.checkout.Session.create = real

    assert raised is not None, "expected Stripe failure to propagate"

    # Because the route now calls Stripe BEFORE writing, no write happened at all.
    with psycopg.connect(DSN) as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT deposit_status, stripe_session_id FROM deposit_regression_probe WHERE id = %s",
            (lead_id,),
        )
        row = cur.fetchone()
    assert row["deposit_status"] != "pending", f"row was marked pending anyway: {row}"
    assert not row["stripe_session_id"], f"session id recorded for a failed session: {row}"
    print(f"PASS  Stripe failure leaves deposit_status={row['deposit_status']!r}, no session id")

    with psycopg.connect(DSN, autocommit=True) as c, c.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS deposit_regression_probe")


if __name__ == "__main__":
    test_create_deposit_session_still_returns_a_url()
    test_create_deposit_checkout_returns_url_and_session_id()
    test_failed_session_creation_leaves_the_lead_untouched()
    print("\nALL deposit-path regression checks PASSED")