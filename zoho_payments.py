"""Zoho Payments: create a payment session and verify the result server-side.

The client portal pays by card *inside* the portal, not by being sent to a
hosted Zoho invoice page. Zoho's Embedded Checkout is a JavaScript widget:
the browser holds no card data and our server never sees a card number, which
keeps this on PCI SAQ-A terms. The flow is:

1. Server creates a short-lived (15 minute) payment session for the exact
   amount due on one bid invoice and returns its id.
2. The portal renders Zoho's ``zpayments.js`` widget with that session id.
   The customer enters the card on Zoho's secure widget on our page.
3. The widget returns a ``payment_id``. The browser posts it back to us and we
   **retrieve it from Zoho's API** before touching the invoice. A browser can
   lie about a payment id; Zoho cannot lie about a payment it captured.

Configuration (Railway / .env):
    ZOHO_PAYMENTS_ACCOUNT_ID   from Zoho Payments > Settings
    ZOHO_PAYMENTS_API_KEY      from Zoho Payments > Settings > Developer Space
                               (public widget key; it is expected in the page)
    ZOHO_PAYMENTS_DOMAIN       US (default)

The server-side calls use the same OAuth access token as the other Zoho
products. The Zoho client must have been re-authorized after ``ZohoPay`` was
added to ``zoho_oauth.SCOPES``; an old token answers INVALID_OAUTHSCOPE and
:func:`create_session` surfaces that instead of pretending it worked.
"""
from __future__ import annotations

import os
from decimal import Decimal, ROUND_HALF_UP

import zoho_client

API_ROOT = "https://payments.zoho.com/api/v1"


class ZohoPaymentsError(RuntimeError):
    """Raised with a customer-safe message; callers show ``str(exc)``."""


def _value(setting, setting_key: str, env_key: str) -> str:
    """Environment wins; then the value saved on Settings.

    The widget key is public (it can create a session, not move money), so it
    is safe to keep in app_settings and let the owner paste it in the app
    instead of opening Render.
    """
    raw = (os.getenv(env_key) or "").strip()
    if raw:
        return raw
    if setting:
        try:
            raw = (setting(setting_key) or "").strip()
        except Exception:
            raw = ""
    return raw


def account_id(setting=None) -> str:
    return _value(setting, "zoho_payments_account_id", "ZOHO_PAYMENTS_ACCOUNT_ID")


def api_key(setting=None) -> str:
    """The public widget key. Safe to put in a page; it cannot move money."""
    return _value(setting, "zoho_payments_api_key", "ZOHO_PAYMENTS_API_KEY")


def domain(setting=None) -> str:
    return (_value(setting, "zoho_payments_domain", "ZOHO_PAYMENTS_DOMAIN") or "US").upper()


def configured(setting=None) -> bool:
    """Enough configuration to create a session and render the widget."""
    return bool(account_id(setting) and api_key(setting) and zoho_client.is_configured())


def widget_configured(setting=None) -> bool:
    """The browser-side half. Account + key are enough to render."""
    return bool(account_id(setting) and api_key(setting))


def _amount(value) -> str:
    return str(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _call(method: str, path: str, *, account_id_value: str = "", **kwargs) -> dict:
    headers = zoho_client.get_headers()
    if not headers:
        raise ZohoPaymentsError(
            "Zoho is not connected. Reconnect Zoho in Settings so card payments can be created.")
    url = f"{API_ROOT}{path}"
    params = kwargs.pop("params", {}) or {}
    params.setdefault("account_id", account_id_value or account_id())
    resp = zoho_client.api_request(method, url, headers=headers, params=params, **kwargs)
    if resp is None:
        raise ZohoPaymentsError("Zoho Payments did not answer. Try again in a moment.")
    try:
        body = resp.json()
    except Exception:
        body = {}
    code = body.get("code")
    if resp.status_code >= 400 or (code not in (None, 0, "0")):
        message = body.get("message") or f"Zoho Payments returned HTTP {resp.status_code}"
        raise ZohoPaymentsError(str(message)[:300])
    return body


def create_session(amount, *, description: str, invoice_number: str = "",
                   reference: str = "", email: str = "", name: str = "",
                   setting=None) -> dict:
    """Create a 15-minute payment session for one invoice.

    Returns the Zoho patch (``payments_session_id``, ``amount``, ...). Amounts
    are major units, which is what the Zoho API expects.
    """
    if not configured(setting):
        raise ZohoPaymentsError(
            "Card payments are not set up yet. Add the Zoho Payments account id and widget key.")
    payload = {
        "amount": float(_amount(amount)),
        "currency": "USD",
        "expires_in": 900,
        "description": (description or "BuildStack Construction payment")[:500],
        "invoice_number": (invoice_number or "")[:50],
        "reference_number": (reference or "")[:50],
        "configurations": {
            "allowed_payment_methods": ["card"],
            # The customer is already on our page; the hosted-checkout URLs are
            # only used by Zoho for its own redirect fallback.
            "hosted_checkout_parameters": {
                "name": (name or "")[:120],
                "email": (email or "")[:200],
                "description": (description or "")[:500],
            },
        },
    }
    body = _call("POST", "/paymentsessions", json=payload,
                 account_id_value=account_id(setting))
    session = body.get("payments_session") or {}
    if not session.get("payments_session_id"):
        raise ZohoPaymentsError("Zoho did not return a payment session.")
    return session


def retrieve_payment(payment_id: str, setting=None) -> dict:
    """The authoritative record of one payment, straight from Zoho."""
    pid = (payment_id or "").strip()
    if not pid:
        raise ZohoPaymentsError("No payment id was supplied.")
    body = _call("GET", f"/payments/{pid}", account_id_value=account_id(setting))
    return body.get("payment") or body


def retrieve_session(session_id: str, setting=None) -> dict:
    body = _call("GET", f"/paymentsessions/{(session_id or '').strip()}",
                 account_id_value=account_id(setting))
    return body.get("payments_session") or body


def payment_succeeded(payment: dict) -> bool:
    status = str((payment or {}).get("status") or "").strip().lower()
    return status in ("succeeded", "success", "captured", "paid", "completed")


def paid_amount(payment: dict) -> float:
    """Major-unit amount from a Zoho payment record."""
    for field in ("amount", "total", "total_amount"):
        value = (payment or {}).get(field)
        if value not in (None, ""):
            try:
                return float(Decimal(str(value).replace(",", "").replace("$", "")))
            except Exception:
                continue
    return 0.0
