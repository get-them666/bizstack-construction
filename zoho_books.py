"""
Zoho Books integration - Customers, Vendors, Estimates, Invoices, Items
"""
import os
from datetime import date
from typing import Dict, Any, Optional
try:
    from . import zoho_client
except Exception:
    import zoho_client

DC_SUFFIX = os.getenv("ZOHO_DC_SUFFIX", "com")
BOOKS_BASE = f"https://www.zohoapis.{DC_SUFFIX}/books/v3"
ORG_ID = os.getenv("ZOHO_BOOKS_ORG_ID", "")


def get_headers_with_org():
    headers = zoho_client.get_headers()
    if not headers:
        return None
    if ORG_ID:
        headers["X-com-zoho-books-organizationid"] = ORG_ID
    return headers


def create_customer(customer_data: Dict[str, Any]) -> Optional[Dict]:
    """Create/update a customer in Zoho Books."""
    if not zoho_client.is_configured():
        return None
    if not ORG_ID:
        print("[zoho_books] ZOHO_BOOKS_ORG_ID not set", flush=True)
        return None
    
    headers = get_headers_with_org()
    if not headers:
        return None
    
    name = customer_data.get("name") or customer_data.get("company_name") or "Customer"
    
    zoho_payload = {
        "contact_name": name,
        "company_name": customer_data.get("company_name", "") or name,
        "email": customer_data.get("email", ""),
        "phone": customer_data.get("phone", ""),
        "billing_address": {
            "address": customer_data.get("address", ""),
            "city": customer_data.get("city", ""),
            "state": customer_data.get("state", ""),
            "zip": customer_data.get("zip_code", ""),
            "country": "USA"
        }
    }
    
    zoho_payload = {k: v for k, v in zoho_payload.items() if v}
    
    resp = zoho_client.api_request("POST", f"{BOOKS_BASE}/contacts", json=zoho_payload, headers=headers)
    if not resp:
        return None
    if resp.status_code not in (200, 201):
        print(f"[zoho_books] create_customer failed: {resp.status_code} {resp.text[:200]}", flush=True)
        return None
    
    try:
        return resp.json()
    except Exception as e:
        print(f"[zoho_books] create_customer parse error: {e}", flush=True)
        return None


def create_vendor(vendor_data: Dict[str, Any]) -> Optional[Dict]:
    """Create/update a vendor (for crew/subcontractors) in Zoho Books."""
    if not zoho_client.is_configured():
        return None
    if not ORG_ID:
        return None
    
    headers = get_headers_with_org()
    if not headers:
        return None
    
    name = vendor_data.get("name") or "Vendor"
    
    zoho_payload = {
        "contact_name": name,
        "company_name": vendor_data.get("company_name", "") or name,
        "email": vendor_data.get("email", ""),
        "phone": vendor_data.get("phone", ""),
        "contact_type": "vendor"
    }
    
    zoho_payload = {k: v for k, v in zoho_payload.items() if v}
    
    resp = zoho_client.api_request("POST", f"{BOOKS_BASE}/contacts", json=zoho_payload, headers=headers)
    if not resp:
        return None
    if resp.status_code not in (200, 201):
        print(f"[zoho_books] create_vendor failed: {resp.status_code}", flush=True)
        return None
    
    try:
        return resp.json()
    except Exception as e:
        print(f"[zoho_books] create_vendor parse error: {e}", flush=True)
        return None


def create_estimate(estimate_data: Dict[str, Any]) -> Optional[Dict]:
    """Create an estimate in Zoho Books."""
    if not zoho_client.is_configured():
        return None
    if not ORG_ID:
        return None
    
    headers = get_headers_with_org()
    if not headers:
        return None
    
    customer_id = estimate_data.get("customer_id")
    if not customer_id:
        # Try to create/find customer first
        cust = create_customer(estimate_data)
        if cust and cust.get("contact"):
            customer_id = cust["contact"]["contact_id"]
    
    if not customer_id:
        return None
    
    line_items = []
    if estimate_data.get("line_items"):
        for item in estimate_data["line_items"]:
            line_items.append({
                "name": item.get("name", "Service"),
                "rate": item.get("rate", 0),
                "quantity": item.get("quantity", 1)
            })
    else:
        # Create a generic line item from estimate total
        amount = estimate_data.get("amount") or estimate_data.get("total")
        if amount:
            line_items.append({
                "name": estimate_data.get("description") or "Construction Services",
                "rate": amount,
                "quantity": 1
            })
    
    zoho_payload = {
        "customer_id": customer_id,
        "line_items": line_items,
        "reference_number": estimate_data.get("reference") or estimate_data.get("lead_id"),
        "notes": estimate_data.get("notes", "")
    }
    
    resp = zoho_client.api_request("POST", f"{BOOKS_BASE}/estimates", json=zoho_payload, headers=headers)
    if not resp:
        return None
    if resp.status_code not in (200, 201):
        print(f"[zoho_books] create_estimate failed: {resp.status_code} {resp.text[:200]}", flush=True)
        return None
    
    try:
        return resp.json()
    except Exception as e:
        print(f"[zoho_books] create_estimate parse error: {e}", flush=True)
        return None


def create_invoice(invoice_data: Dict[str, Any]) -> Optional[Dict]:
    """Create an invoice in Zoho Books."""
    if not zoho_client.is_configured():
        return None
    if not ORG_ID:
        return None
    
    headers = get_headers_with_org()
    if not headers:
        return None
    
    customer_id = invoice_data.get("customer_id")
    if not customer_id:
        cust = create_customer(invoice_data)
        if cust and cust.get("contact"):
            customer_id = cust["contact"]["contact_id"]
    
    if not customer_id:
        return None
    
    line_items = []
    if invoice_data.get("line_items"):
        for item in invoice_data["line_items"]:
            line_items.append({
                "name": item.get("name", "Service"),
                "rate": item.get("rate", 0),
                "quantity": item.get("quantity", 1)
            })
    else:
        amount = invoice_data.get("amount") or invoice_data.get("total")
        if amount:
            line_items.append({
                "name": invoice_data.get("description") or "Construction Services",
                "rate": amount,
                "quantity": 1
            })
    
    zoho_payload = {
        "customer_id": customer_id,
        "line_items": line_items,
        "reference_number": invoice_data.get("reference") or invoice_data.get("lead_id"),
        "notes": invoice_data.get("notes", "")
    }
    
    resp = zoho_client.api_request("POST", f"{BOOKS_BASE}/invoices", json=zoho_payload, headers=headers)
    if not resp:
        return None
    if resp.status_code not in (200, 201):
        print(f"[zoho_books] create_invoice failed: {resp.status_code}", flush=True)
        return None
    
    try:
        return resp.json()
    except Exception as e:
        print(f"[zoho_books] create_invoice parse error: {e}", flush=True)
        return None


def find_customer(email: str = "", phone: str = "") -> Optional[Dict]:
    """Find a customer in Zoho Books."""
    if not zoho_client.is_configured() or not ORG_ID:
        return None
    
    headers = get_headers_with_org()
    if not headers:
        return None
    
    try:
        resp = zoho_client.api_request("GET", f"{BOOKS_BASE}/contacts", headers=headers)
        if resp and resp.status_code == 200:
            data = resp.json()
            contacts = data.get("contacts", [])
            for c in contacts:
                if email and c.get("email") == email:
                    return c
                if phone and c.get("phone") == phone:
                    return c
    except Exception as e:
        print(f"[zoho_books] find_customer error: {e}", flush=True)
    return None


def record_payment(payment_data: Dict[str, Any]) -> Optional[Dict]:
    """Record a payment against a Zoho Books invoice.

    Expects: invoice_id (Zoho Books invoice id), amount, date, payment_mode,
    description, reference_number (optional). Returns the Zoho Books response or None.
    """
    if not zoho_client.is_configured():
        return None
    if not ORG_ID:
        return None

    headers = get_headers_with_org()
    if not headers:
        return None

    invoice_id = payment_data.get("invoice_id")
    if not invoice_id:
        print("[zoho_books] record_payment: no invoice_id", flush=True)
        return None

    zoho_payload = {
        "invoice_id": invoice_id,
        "amount": payment_data.get("amount", 0),
        "date": payment_data.get("date", date.today().isoformat()),
        "payment_mode": payment_data.get("payment_mode", "check"),
        "description": payment_data.get("description", "Payment received"),
        "reference_number": payment_data.get("reference_number", ""),
    }
    zoho_payload = {k: v for k, v in zoho_payload.items() if v is not None}

    resp = zoho_client.api_request(
        "POST", f"{BOOKS_BASE}/invoices/{invoice_id}/payments",
        json=zoho_payload, headers=headers)
    if not resp:
        return None
    if resp.status_code not in (200, 201):
        print(f"[zoho_books] record_payment failed: {resp.status_code} {resp.text[:200]}", flush=True)
        return None

    try:
        return resp.json()
    except Exception as e:
        print(f"[zoho_books] record_payment parse error: {e}", flush=True)
        return None


def get_or_create_customer(customer_data: Dict[str, Any]) -> Optional[str]:
    """Find a Zoho Books contact by email/phone, or create one.

    Returns the Zoho Books contact_id or None. Customer data keys:
    name, email, phone, company_name, address, city, state, zip_code.
    """
    if not zoho_client.is_configured() or not ORG_ID:
        return None

    email = customer_data.get("email", "")
    phone = customer_data.get("phone", "")

    # Try to find existing contact
    if email or phone:
        existing = find_customer(email=email, phone=phone)
        if existing and existing.get("contact_id"):
            return existing["contact_id"]

    # Create new contact
    cust = create_customer(customer_data)
    if cust and cust.get("contact"):
        return cust["contact"].get("contact_id")
    return None
