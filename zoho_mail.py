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


# --- reading the inbox ------------------------------------------------------------
#
# Replies land in Zoho because that is where MX points. IMAP is switched off on
# the account ("You are yet to enable IMAP"), but the Mail REST API reads the
# same inbox with the ZohoMail.messages.READ scope the connection now carries.
# Messages are flattened into the dict shape inbound_email._ingest consumes, so
# matching, STOP handling and the reply bot are shared with every other path.

def _html_to_text(html_body: str) -> str:
    """Same cleaning the Gmail/IMAP paths get, so STOP detection sees one shape."""
    try:
        import email.message
        import inbound_email
        msg = email.message.EmailMessage()
        msg.set_content(html_body or "", subtype="html")
        return inbound_email._body_text(msg)
    except Exception:
        import html as _h
        import re
        text = re.sub(r"<[^>]+>", " ", html_body or "")
        return re.sub(r"\s+", " ", _h.unescape(text)).strip()[:8000]


def inbox_status() -> dict:
    """Can the API read mail right now? One accounts call + a one-message list.

    Deliberately no folders call: /folders needs ZohoMail.folders.READ, which
    the grant does not carry, and messages/view works without a folderId --
    every row names its own folderId, which is all the content call needs.
    """
    out = {"ok": False, "reason": "", "mailbox": "", "via": "Zoho Mail API"}
    if not available():
        out["reason"] = "connection lacks the mail scope"
        return out
    try:
        account_id, address = _account(os.getenv("SMTP_FROM") or "")
        resp = zoho_client.api_request("GET", f"{MAIL_BASE}/accounts/{account_id}/messages/view",
                                       params={"limit": 1})
        if resp is None or resp.status_code != 200:
            raise RuntimeError(f"Zoho Mail list failed: {getattr(resp, 'status_code', '?')} "
                               f"{getattr(resp, 'text', '')[:120]}")
        out.update(ok=True, mailbox=address)
    except Exception as exc:
        out["reason"] = str(exc)[:200]
    return out


def list_recent(days: int = 7, limit: int = 30) -> list:
    """Newest messages across folders as [{id, folder_id, from, to, subject, date, received_ms, summary}]."""
    account_id, address = _account(os.getenv("SMTP_FROM") or "")
    resp = zoho_client.api_request(
        "GET", f"{MAIL_BASE}/accounts/{account_id}/messages/view",
        params={"limit": max(1, min(int(limit), 200)), "sortorder": "false"})
    if resp is None or resp.status_code != 200:
        raise RuntimeError(f"Zoho Mail list failed: {getattr(resp, 'status_code', '?')} {getattr(resp, 'text', '')[:160]}")
    cutoff_ms = (time.time() - max(1, days) * 86400) * 1000
    out = []
    for m in resp.json().get("data") or []:
        try:
            received = float(m.get("receivedTime") or 0)
        except (TypeError, ValueError):
            received = 0
        if received and received < cutoff_ms:
            continue
        out.append({
            "id": str(m.get("messageId") or ""),
            "folder_id": str(m.get("folderId") or ""),
            "account_id": account_id,
            "from": str(m.get("fromAddress") or m.get("sender") or ""),
            "to": str(m.get("toAddress") or address),
            "subject": str(m.get("subject") or ""),
            "received_ms": received,
            "date": time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(received / 1000)) if received else "",
            "summary": str(m.get("summary") or ""),
        })
    return out


def message_text(account_id: str, folder_id: str, message_id: str) -> str:
    resp = zoho_client.api_request(
        "GET", f"{MAIL_BASE}/accounts/{account_id}/folders/{folder_id}/messages/{message_id}/content")
    if resp is None or resp.status_code != 200:
        raise RuntimeError(f"Zoho Mail content failed: {getattr(resp, 'status_code', '?')}")
    data = resp.json().get("data") or {}
    return _html_to_text(str(data.get("content") or ""))


def poll_inbox(db, company_key: str = "construction", mailbox_from: str = "",
               max_results: int = 30, days: int = 7) -> dict:
    """Fetch new inbox messages and hand them to inbound_email._ingest.

    Only messages not already in comms_logs have their body fetched, so a
    15-minute poll costs one list call plus one content call per genuinely
    new message. Our own outbound (sender == mailbox) is skipped.
    """
    import inbound_email
    st = inbox_status()
    if not st["ok"]:
        return {"error": st["reason"] or "Zoho Mail inbox unavailable", "checked": 0}
    try:
        heads = list_recent(days=days, limit=max_results)
    except Exception as exc:
        return {"error": str(exc)[:200], "checked": 0}
    mailbox = (mailbox_from or st.get("mailbox") or "").strip().lower()
    heads = [h for h in heads if h["id"] and h["from"].strip().lower() != mailbox]
    if not heads:
        return {"matched": 0, "inserted": 0, "deduped": 0, "skipped": 0, "unmatched": 0, "checked": 0}
    ids = [f"zoho|{h['id']}" for h in heads]
    seen = set()
    try:
        with db.cursor() as cur:
            cur.execute("SELECT external_id FROM comms_logs WHERE external_id = ANY(%s);", (ids,))
            seen = {r["external_id"] if isinstance(r, dict) else r[0] for r in cur.fetchall()}
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    msgs = []
    for h in heads:
        ext = f"zoho|{h['id']}"
        if ext in seen:
            continue
        try:
            text = message_text(h["account_id"], h["folder_id"], h["id"])
        except Exception as exc:
            print(f"[zoho-mail] content fetch failed for {h['id']}: {exc}", flush=True)
            text = h.get("summary") or ""
        msgs.append({"from": h["from"], "to": h["to"], "subject": h["subject"], "date": h["date"],
                     "text": text, "external_id": ext})
    if not msgs:
        return {"matched": 0, "inserted": 0, "deduped": len(heads), "skipped": 0, "unmatched": 0, "checked": len(heads)}
    result = inbound_email._ingest(db, company_key, msgs, mailbox or "hello@bizstackperks.com")
    result["checked"] = len(heads)
    return result
