"""Client-portal credentials, invoice money maths and payment references.

None of these touch a database or the network. They are the parts that decide
whether a payment is considered paid, and what confirmation number a customer
is given, so they must be right without a running Postgres.
"""
import os

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/nonexistent")

import portal_service
import zoho_payments


def test_temporary_password_is_readable_and_strong_enough():
    for _ in range(20):
        pw = portal_service.temporary_password()
        assert len(pw) >= 12
        assert any(c.isdigit() for c in pw)
        assert any(c.isalpha() for c in pw)
        # Characters that get misread when a password is spoken aloud.
        assert not set(pw) & set("0O1lI")


def test_amount_due_never_goes_negative_or_counts_a_void():
    assert portal_service.amount_due({"amount": 3000, "collected": 1000}) == 2000.0
    assert portal_service.amount_due({"amount": 100, "collected": 150}) == 0.0
    assert portal_service.amount_due({"amount": 100, "collected": 100, "status": "paid"}) == 0.0
    assert portal_service.amount_due({"amount": 100, "collected": 0, "status": "void"}) == 0.0
    assert portal_service.amount_due({}) == 0.0


def test_open_invoices_are_the_ones_a_client_can_still_pay():
    invoices = [
        {"id": 1, "amount": 100, "collected": 100, "status": "paid"},
        {"id": 2, "amount": 100, "collected": 0, "status": "sent"},
        {"id": 3, "amount": 100, "collected": 0, "status": "void"},
        {"id": 4, "amount": 100, "collected": 40, "status": "partial"},
    ]
    assert [i["id"] for i in portal_service.open_invoices(invoices)] == [2, 4]


def test_confirmation_number_is_short_and_stable():
    ref = portal_service.confirmation_number("1987000000724215")
    assert ref == "PAY-724215"
    assert portal_service.confirmation_number("") == "PAY-000000"


def test_zoho_success_and_amount_parsing():
    assert zoho_payments.payment_succeeded({"status": "succeeded"})
    assert zoho_payments.payment_succeeded({"status": "CAPTURED"})
    assert not zoho_payments.payment_succeeded({"status": "failed"})
    assert zoho_payments.paid_amount({"amount": "1,234.50"}) == 1234.5
    assert zoho_payments.paid_amount({"total": 500}) == 500.0
    assert zoho_payments.paid_amount({}) == 0.0


def test_payment_reference_round_trips():
    import construction_main as cm
    ref = cm._payment_reference(42, 7)
    assert ref == "BID-42-INV-7"
    assert cm._parse_payment_reference(ref) == (42, 7)
    assert cm._parse_payment_reference("DEP-00042") is None
    assert cm._parse_payment_reference("") is None
