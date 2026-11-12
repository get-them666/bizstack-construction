"""The shared mail transport must refuse to print a bank routing/account pair.

The 2026-11 deposit incident sent the PNC routing and account number to multiple
clients, repeatedly, in both the email body and the attached PDF. The builder
now refuses those lines and the transport has its own hard stop, so a direct
``send_email`` call cannot bypass the app-level guard.
"""

import documents_service as ds


def test_detects_routing_then_account():
    assert ds.contains_bank_details(
        "Or send an ACH credit to BuildStack Construction LLC: PNC Bank, "
        "routing 051000017, account 000123456789."
    )


def test_detects_account_then_routing():
    assert ds.contains_bank_details(
        "Account number 000123456789 · routing number 051000017"
    )


def test_ignores_ordinary_deposit_text():
    assert not ds.contains_bank_details(
        "Zelle the deposit to hello@bizstackperks.com and put invoice INV-2026-0007 "
        "in the memo. The amount is $777.56, due 2026-11-18."
    )


def test_ignores_a_lone_number():
    assert not ds.contains_bank_details("Call 757-908-7121 about invoice INV-2026-0007.")
