"""Send mail through the Zoho Mail REST API (HTTPS), not SMTP.

Zoho's SMTP ports time out from Railway's egress on 25/465/587 -- that is why
EMAIL_TRANSPORT=legacy left the business with no outbound mail. The REST API
is plain HTTPS and works from anywhere. It needs the ZohoMail.messages.CREATE
and ZohoMail.accounts.READ scopes, granted by the Connect Zoho button.

The sending account is resolved once from /api/accounts: the account whose
primary or alias address matches SMTP_FROM, else the first account.
Attachments are uploaded first and referenced by the ids Zoho returns.
"""
from __future__ import annotations

import base64
import html as _html
import os
import time
from typing import Optional

try:
    import zoho_client
    import zoho_oauth
except Exception:  # pragma: no cover
    zoho_client = zoho_oauth = None  # type: ignore

DC = (os.getenv("ZOHO_DC_SUFFIX") or "com").strip()
MAIL_BASE = f"https://mail.zoho.{DC}/api"

_account_cache: dict = {"id": "", "address": "", "at": 0.0}


def available() -> bool:
    """Connected with mail scopes, per the stored scope string. No network."""
    try:
        return bool(zoho_oauth and zoho_oauth.status().get("mail"))
    except Exception:
        return False


def _account(sender: str) -> tuple[str, str]:
    now = time.time()
    if _account_cache["id"] and now - _account_cache["at"] < 3600:
        return _account_cache["id"], _account_cache["address"]
    resp = zoho_client.api_request("GET", f"{MAIL_BASE}/accounts")
    if resp is None or resp.status_code != 200:
        raise RuntimeError(f"Zoho Mail accounts lookup failed: {getattr(resp, 'status_code', '?')} "
                           f"{getattr(resp, 'text', '')[:160]}")
    accounts = resp.json().get("data") or []
    want = (sender or "").strip().lower()
    chosen = None
    for acc in accounts:
        addrs = {str(acc.get("primaryEmailAddress") or "").lower()}
        for alias in acc.get("emailAddress") or []:
            addrs.add(str(alias.get("mailId") or "").lower())
        addrs.discard("")
        if want and want in addrs:
            chosen = acc
            break
    if chosen is None and accounts:
        chosen = accounts[0]
    if not chosen:
        raise RuntimeError("no Zoho Mail account on this login")
    _account_cache.update({"id": str(chosen.get("accountId")), "address": str(chosen.get("primaryEmailAddress") or want),
                           "at": now})
    return _account_cache["id"], _account_cache["address"]


def send(to: str, subject: str, body_text: str, *, sender: str = "", sender_name: str = "",
         cc: str = "", attachment: Optional[bytes] = None, filename: str = "") -> bool:
    """Deliver one message. Returns True only when Zoho accepted it."""
    if not zoho_client or not zoho_client.is_configured():
        return False
    account_id, from_addr = _account(sender)
    from_addr = sender or from_addr
    payload = {
        "fromAddress": f"{sender_name} <{from_addr}>" if sender_name else from_addr,
        "toAddress": to,
        "subject": subject,
        "content": _html.escape(body_text).replace("\n", "<br>") if "<br>" not in body_text and "<" not in body_text else body_text,
        "mailFormat": "html",
        "askReceipt": "no",
    }
    if cc:
        payload["ccAddress"] = cc
    if attachment and filename:
        up = zoho_client.api_request(
            "POST", f"{MAIL_BASE}/accounts/{account_id}/messages/attachments",
            params={"fileName": filename, "isInline": "false"},
            data=attachment, headers={"Content-Type": "application/octet-stream"})
        if up is None or up.status_code not in (200, 201):
            raise RuntimeError(f"Zoho Mail attachment upload failed: {getattr(up, 'status_code', '?')}")
        info = (up.json().get("data") or [{}])
        info = info[0] if isinstance(info, list) else info
        payload["attachments"] = [{"storeName": info.get("storeName"), "attachmentPath": info.get("attachmentPath"),
                                   "attachmentName": info.get("attachmentName") or filename}]
    resp = zoho_client.api_request("POST", f"{MAIL_BASE}/accounts/{account_id}/messages", json=payload)
    if resp is None:
        return False
    if resp.status_code not in (200, 201):
        print(f"[zoho-mail] send failed: {resp.status_code} {resp.text[:200]}", flush=True)
        return False
    return True
