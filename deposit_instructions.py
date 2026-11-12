"""Deposit requests paid by Zelle, or ACH into the business checking account.

No payment processor sits in this path. The owner still sends the same link
from the lead board, but the link now opens a public page that says what is
due and how to pay it:

  1. **Zelle** to the business handle -- instant, no fee, no card.
  2. **ACH** to the PNC business checking account, quoting the reference, for
     a client whose bank limits or does not offer Zelle.

Both land in the same account. Every value printed comes from `/settings`:
an unset handle or account renders as "the office will send the details"
rather than a blank or a guess. The account number is stored Fernet-encrypted
under ``BANK_ENCRYPTION_KEY`` (the same key that protects crew bank details)
and is shown only on the client-facing deposit page.

Links are signed with ``auth_service.sign_token`` and expire, so a forwarded
link stops working instead of quoting an old price forever.
"""
from __future__ import annotations

import os
import re
import time

from cryptography.fernet import Fernet, InvalidToken

import auth_service

KEY_ZELLE = "pay_zelle_handle"
KEY_ROUTING = "pay_ach_routing"
KEY_LAST4 = "pay_ach_account_last4"
KEY_ACCOUNT_ENC = "pay_ach_account_enc"

LINK_DAYS = 60
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class NotConfigured(Exception):
    pass


# --- storage -----------------------------------------------------------------
def _fernet() -> Fernet:
    key = (os.getenv("BANK_ENCRYPTION_KEY") or "").strip()
    if not key:
        raise NotConfigured("BANK_ENCRYPTION_KEY is not set, so the account number cannot be stored")
    return Fernet(key.encode())


def normalize_zelle(raw: str) -> str:
    """A Zelle handle is an email address or a phone number, nothing else."""
    value = (raw or "").strip()
    if not value:
        return ""
    if "@" in value:
        return value.lower() if _EMAIL_RE.match(value) else ""
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits if len(digits) >= 10 else ""


def format_phone(digits: str) -> str:
    d = "".join(ch for ch in (digits or "") if ch.isdigit())
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return f"({d[:3]}) {d[3:6]}-{d[6:10]}" if len(d) == 10 else digits


def save(put, zelle: str, routing: str, account: str) -> dict:
    """Store the payment details. ``put(key, value)`` writes to app_settings."""
    zelle = normalize_zelle(zelle)
    routing = "".join(ch for ch in (routing or "") if ch.isdigit())
    account = "".join(ch for ch in (account or "") if ch.isdigit())
    if zelle:
        put(KEY_ZELLE, zelle)
    if routing:
        put(KEY_ROUTING, routing)
    if account:
        put(KEY_ACCOUNT_ENC, _fernet().encrypt(account.encode()).decode())
        put(KEY_LAST4, account[-4:])
    return {"ok": True}


def details(get) -> dict:
    """What is on file, without decrypting the account number."""
    zelle = (get(KEY_ZELLE) or "").strip()
    routing = "".join(ch for ch in (get(KEY_ROUTING) or "") if ch.isdigit())
    last4 = "".join(ch for ch in (get(KEY_LAST4) or "") if ch.isdigit())[-4:]
    has_account = bool(get(KEY_ACCOUNT_ENC))
    return {
        "zelle": zelle,
        "zelle_display": format_phone(zelle) if zelle and "@" not in zelle else zelle,
        "routing": routing,
        "account_last4": last4,
        "has_account": has_account,
        "ach_ready": bool(routing and has_account),
        "ready": bool(zelle or (routing and has_account)),
    }


def ach_details(get) -> dict:
    """Routing + decrypted account number for the client-facing page."""
    out = details(get)
    enc = get(KEY_ACCOUNT_ENC) or ""
    account = ""
    if enc:
        try:
            account = _fernet().decrypt(enc.encode()).decode()
        except InvalidToken as e:
            raise NotConfigured("BANK_ENCRYPTION_KEY no longer matches the stored account number") from e
    out["account"] = account
    return out


# --- links -------------------------------------------------------------------
def reference(lead_id: int) -> str:
    return f"DEP-{int(lead_id):05d}"


def link(lead_id: int, days: int = LINK_DAYS) -> str:
    token = auth_service.sign_token({"t": "deposit", "lead": int(lead_id),
                                     "exp": time.time() + days * 86400})
    return f"/pay/deposit/{token}"


def lead_from_token(token: str):
    payload = auth_service.verify_token(token or "")
    if not payload or payload.get("t") != "deposit":
        return None
    try:
        return int(payload["lead"])
    except (KeyError, TypeError, ValueError):
        return None


# --- printed instructions ----------------------------------------------------
def payment_lines(company_name: str, get, *, phone: str = "",
                  reference: str = "the invoice number above",
                  include_ach: bool = False) -> list[str]:
    """Payment paragraph for the deposit PDF and email.

    Zelle first because it is what the client can do in two minutes. ACH bank
    details are deliberately **not** included by default: an emailed PDF and an
    email body are forwarded, archived and indexed, and a routing/account pair
    must never travel that way. The authenticated ``/pay/deposit`` page is the
    only surface that may decrypt and show the account.

    ``include_ach=True`` exists for a future non-email surface. Callers that
    render email or attach a PDF must leave it false. Returns a single "we will
    send details" line when nothing is configured.
    """
    d = ach_details(get) if include_ach else details(get)
    ref = reference
    lines = []
    if d["zelle"]:
        lines.append(f"Zelle the deposit to {d['zelle_display']} and put {ref} in the memo.")
    if include_ach and d.get("ach_ready"):
        lines.append(f"Or send an ACH credit to {company_name}: PNC Bank, routing {d['routing']}, "
                     f"account {d['account']}. Put {ref} in the memo.")
    if not lines:
        lines.append("Reply to this email" + (f" or call {phone}" if phone else "")
                     + f" and we will send payment details; quote {ref} either way.")
    return lines
