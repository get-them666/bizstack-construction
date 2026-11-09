"""Zoho Books sync: the website is the hub, Zoho Books is the accounting record.

When the owner raises an invoice, records a payment, or marks an invoice paid
on the site, this module pushes the same event to Zoho Books so the books in
Zoho match the books on the site. Everything is idempotent: a zoho_invoice_id
is stored on the bid_invoices row so a re-save never creates a duplicate.

Requires the Zoho OAuth connection (from /settings → Connect Zoho) with the
ZohoBooks.fullaccess.all scope, and ZOHO_BOOKS_ORG_ID in Railway.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import zoho_books


def _money_str(amount) -> str:
    try:
        return f"{float(amount or 0):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def customer_for_bid(db, bid: dict) -> Optional[dict]:
    """Return a customer dict for a bid, with name/email/phone/address."""
    return {
        "name": bid.get("contact_name") or bid.get("owner") or bid.get("name") or "Customer",
        "email": bid.get("contact_email") or "",
        "phone": bid.get("contact_phone") or "",
        "company_name": bid.get("owner") or bid.get("name") or "",
        "address": (bid.get("location") or "").split(",")[0].strip() if bid.get("location") else "",
        "city": "",
        "state": "",
        "zip_code": "",
    }


def customer_for_lead(db, lead: dict) -> Optional[dict]:
    """Return a customer dict for a lead."""
    return {
        "name": lead.get("name") or "Customer",
        "email": lead.get("email") or "",
        "phone": lead.get("phone") or "",
        "company_name": "",
        "address": lead.get("address") or "",
        "city": "",
        "state": "",
        "zip_code": "",
    }


def get_or_create_zoho_customer(db, customer: dict) -> Optional[str]:
    """Find or create a Zoho Books contact, return its contact_id.

    If the lead/bid already has a zoho_contact_id stored, reuse it.
    """
    zoho_contact_id = customer.get("_zoho_contact_id")
    if zoho_contact_id:
        return zoho_contact_id

    contact_id = zoho_books.get_or_create_customer(customer)
    if contact_id:
        customer["_zoho_contact_id"] = contact_id
    return contact_id


def push_invoice_to_zoho(db, bid_id: int, invoice: dict) -> Optional[str]:
    """Create the invoice in Zoho Books and return the Zoho invoice id.

    If the invoice already has a zoho_invoice_id, skip (idempotent).
    """
    if invoice.get("zoho_invoice_id"):
        return invoice["zoho_invoice_id"]

    if not zoho_books.zoho_client.is_configured():
        print("[zoho_books_sync] Zoho not configured, skipping invoice push", flush=True)
        return None

    org_id = __import__("os").getenv("ZOHO_BOOKS_ORG_ID", "")
    if not org_id:
        print("[zoho_books_sync] ZOHO_BOOKS_ORG_ID not set, skipping invoice push", flush=True)
        return None

    customer = customer_for_bid(db, bid_id)
    contact_id = get_or_create_zoho_customer(db, customer)
    if not contact_id:
        print("[zoho_books_sync] no Zoho Books contact, cannot create invoice", flush=True)
        return None

    line_items = [{
        "name": invoice.get("description") or "Construction services",
        "rate": float(invoice.get("amount") or 0),
        "quantity": 1,
    }]

    result = zoho_books.create_invoice({
        "customer_id": contact_id,
        "line_items": line_items,
        "reference_number": invoice.get("invoice_number") or f"bid-{bid_id}",
        "notes": invoice.get("description") or "",
    })

    if result and result.get("invoice"):
        zoho_invoice_id = result["invoice"].get("invoice_id")
        # invoice_url is the shareable page the client opens to view and pay.
        # Zoho only renders a Pay Now button there when a gateway is connected,
        # but the link is useful either way, so it is stored rather than dropped.
        zoho_invoice_url = result["invoice"].get("invoice_url") or ""
        # Store the Zoho invoice id on the local row
        try:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE bid_invoices SET zoho_invoice_id = %s, zoho_invoice_url = %s "
                    "WHERE id = %s AND company = 'construction';",
                    (zoho_invoice_id, zoho_invoice_url, invoice["id"]))
            db.commit()
        except Exception:
            db.rollback()
        return zoho_invoice_id

    print(f"[zoho_books_sync] invoice push returned no invoice id: {result}", flush=True)
    return None


def record_payment_in_zoho(db, invoice: dict, amount: float, method: str,
                           received_on: Optional[str] = None,
                           reference: str = "") -> Optional[dict]:
    """Record a payment against the Zoho Books invoice.

    Returns the Zoho Books payment response or None.
    """
    zoho_invoice_id = invoice.get("zoho_invoice_id")
    if not zoho_invoice_id:
        print("[zoho_books_sync] invoice has no zoho_invoice_id, cannot record payment", flush=True)
        return None

    # Map site payment method to Zoho Books payment mode
    mode_map = {
        "card": "card",
        "stripe": "card",
        "ach": "bank_transfer",
        "zelle": "check",
        "venmo": "check",
        "cash": "cash",
        "check": "check",
        "other": "check",
    }
    payment_mode = mode_map.get((method or "check").lower(), "check")

    result = zoho_books.record_payment({
        "invoice_id": zoho_invoice_id,
        "amount": amount,
        "date": received_on or date.today().isoformat(),
        "payment_mode": payment_mode,
        "description": f"Payment via {method} — {reference}".strip() or "Payment received",
        "reference_number": reference or invoice.get("invoice_number") or "",
    })
    return result


def sync_customer_to_zoho(db, lead: dict) -> Optional[str]:
    """Create or update a Zoho Books contact from a lead, store zoho_contact_id."""
    customer = customer_for_lead(db, lead)
    contact_id = get_or_create_zoho_customer(db, customer)
    if contact_id and lead.get("id"):
        try:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE leads SET zoho_contact_id = %s WHERE id = %s AND company = 'construction';",
                    (contact_id, lead["id"]))
            db.commit()
        except Exception:
            db.rollback()
    return contact_id
