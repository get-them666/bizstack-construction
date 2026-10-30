"""Read homeowner replies from the Zoho mailbox over IMAP.

The domain's MX records point at Zoho (`mx.zoho.com`), so a reply to any
BuildStack email lands in Zoho Mail. The Gmail poller cannot see it, which
means the contact policy never learns that someone answered, a STOP is never
honoured, and the reply bot stays silent. This closes that hole.

Why IMAP and not the Mail API: Railway blocks Zoho's SMTP ports (25, 465 and
587 all time out from the container) but IMAP on 993 is reachable and the
mailbox password authenticates. The Mail REST API remains the better path for
*sending* and takes over automatically once the connection carries
`ZohoMail.messages.CREATE`; reading works today with no OAuth scope at all.

One prerequisite the owner controls: Zoho Mail -> Settings -> Mail Accounts ->
IMAP Access must be enabled. Until it is, login answers `[ALERT] You are yet to
enable IMAP` and this module reports that verbatim instead of pretending to
work.

Dedupe rides on `zoho|<Message-ID>`, the same external_id mechanism the Gmail
poller uses, so re-reading a message is a no-op and marking it read is safe.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
import time
from email.header import decode_header
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_HOST = "imap.zoho.com"
DEFAULT_PORT = 993

_STATUS: Dict[str, Any] = {"checked_at": 0.0, "ok": False, "reason": "not checked"}
_TAG_RE = re.compile(r"<[^>]+>")
_MSGID_RE = re.compile(r"<[^>]+>")


def _flag(name: str, default: str = "") -> str:
    """Explicit boolean parsing: Railway stores an unset flag as the *string*
    "false", which is truthy. Same trap `test_flag_semantics.py` guards."""
    return (os.getenv(name) or default).strip().lower()


def enabled() -> bool:
    if _flag("ZOHO_IMAP_INBOUND", "on") in ("0", "false", "no", "off"):
        return False
    return bool(_user() and _password())


def _user() -> str:
    return (os.getenv("ZOHO_IMAP_USER") or os.getenv("SMTP_USER") or "").strip()


def _password() -> str:
    return (os.getenv("ZOHO_IMAP_PASS") or os.getenv("SMTP_PASS") or "").strip()


def host() -> str:
    return (os.getenv("ZOHO_IMAP_HOST") or DEFAULT_HOST).strip()


def port() -> int:
    try:
        return int(os.getenv("ZOHO_IMAP_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def _connect() -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(host(), port(), timeout=30)
    conn.login(_user(), _password())
    return conn


def status(force: bool = False) -> Dict[str, Any]:
    """Can we log in and select the inbox? Cached for a minute -- the poller
    and the Settings card both call this."""
    now = time.time()
    if not force and now - float(_STATUS.get("checked_at") or 0) < 60:
        return dict(_STATUS)
    out: Dict[str, Any] = {"configured": bool(_user() and _password()), "ok": False,
                           "imap_enabled": False, "reason": "", "mailbox": _user(),
                           "host": f"{host()}:{port()}", "checked_at": now}
    if not out["configured"]:
        out["reason"] = "no mailbox credentials (ZOHO_IMAP_USER/SMTP_USER)"
    else:
        try:
            conn = _connect()
            try:
                typ, _ = conn.select("INBOX", readonly=True)
                out["ok"] = typ == "OK"
                out["imap_enabled"] = out["ok"]
                if not out["ok"]:
                    out["reason"] = "INBOX could not be selected"
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except imaplib.IMAP4.error as exc:
            msg = _clean_error(exc)
            out["reason"] = msg[:200]
            out["imap_enabled"] = "enable imap" not in msg.lower()
        except Exception as exc:
            out["reason"] = f"{type(exc).__name__}: {exc}"[:200]
    _STATUS.update(out)
    return dict(out)


def _clean_error(exc: Exception) -> str:
    """imaplib raises with bytes, so str(exc) reads `b'[ALERT] ...'`. Show the
    owner the sentence, not the repr."""
    raw = str(exc)
    if raw[:2] in ("b'", 'b"'):
        try:
            import ast
            val = ast.literal_eval(raw)
            raw = val.decode("utf-8", "replace") if isinstance(val, bytes) else str(val)
        except Exception:
            raw = raw[2:-1]
    return raw.strip()[:200]


def _since(days: int) -> str:
    return time.strftime("%d-%b-%Y", time.localtime(time.time() - max(1, days) * 86400))


def _decode_header(raw: str) -> str:
    if not raw:
        return ""
    out = []
    for text, enc in decode_header(raw):
        if isinstance(text, bytes):
            try:
                text = text.decode(enc or "utf-8", "replace")
            except LookupError:
                text = text.decode("utf-8", "replace")
        out.append(text)
    return "".join(out).strip()


def _body_text(msg) -> str:
    """Reuse the inbound_email parser so Zoho and Gmail messages flatten the
    same way (and STOP detection sees the same text)."""
    try:
        import inbound_email
        return inbound_email._body_text(msg)
    except Exception:
        text = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not text:
                    text = (part.get_payload(decode=True) or b"").decode(
                        part.get_content_charset() or "utf-8", "replace")
        else:
            text = (msg.get_payload(decode=True) or b"").decode(
                msg.get_content_charset() or "utf-8", "replace")
        return _TAG_RE.sub(" ", text).strip()[:8000]


def fetch_recent(days: int = 7, limit: int = 30) -> List[Dict[str, str]]:
    """Return recent inbox messages in the shape inbound_email._ingest wants."""
    out: List[Dict[str, str]] = []
    conn = _connect()
    try:
        conn.select("INBOX", readonly=True)
        typ, data = conn.search(None, f'(SINCE "{_since(days)}")')
        if typ != "OK":
            return out
        ids = (data[0] or b"").split()
        for num in ids[-max(1, limit):]:
            try:
                typ, raw = conn.fetch(num, "(RFC822)")
                if typ != "OK" or not raw or not isinstance(raw[0], tuple):
                    continue
                msg = email.message_from_bytes(raw[0][1])
            except Exception as exc:
                print(f"[zoho-imap] fetch {num!r} failed: {exc}", flush=True)
                continue
            msgid = (msg.get("Message-ID") or "").strip()
            mid = _MSGID_RE.search(msgid)
            external = f"zoho|{(mid.group(0) if mid else msgid) or num.decode(errors='replace')}"
            sender = _decode_header(msg.get("From") or "")
            try:
                import inbound_email
                sender = inbound_email._clean_addr(sender) or sender
            except Exception:
                pass
            out.append({
                "from": sender,
                "to": _decode_header(msg.get("To") or ""),
                "subject": _decode_header(msg.get("Subject") or ""),
                "date": (msg.get("Date") or "").strip(),
                "text": _body_text(msg),
                "external_id": external,
            })
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return out


def poll_zoho_inbox(db, company_key: str = "construction", mailbox_from: str = "",
                    max_results: int = 30, days: int = 7) -> Dict[str, Any]:
    """Fetch and ingest. Matching, STOP handling and reply drafting all live in
    inbound_email._ingest -- this only supplies the messages."""
    import inbound_email
    st = status()
    if not st.get("ok"):
        return {"error": st.get("reason") or "Zoho IMAP unavailable", "checked": 0}
    try:
        msgs = fetch_recent(days=days, limit=max_results)
    except imaplib.IMAP4.error as exc:
        return {"error": str(exc)[:200], "checked": 0}
    if not msgs:
        return {"matched": 0, "inserted": 0, "deduped": 0, "skipped": 0,
                "unmatched": 0, "checked": 0}
    result = inbound_email._ingest(db, company_key, msgs,
                                   mailbox_from or _user() or "hello@bizstackperks.com")
    result["checked"] = len(msgs)
    return result


def start(get_db, company_key: str = "construction", interval_seconds: int = 900) -> Optional[Any]:
    """Daemon poller. Mirrors the Gmail loop's shape; never raises out."""
    import threading

    def _loop():
        while True:
            try:
                with get_db() as db:
                    res = poll_zoho_inbox(db, company_key=company_key)
                if res.get("error"):
                    print(f"[zoho-imap] {res['error']}", flush=True)
                elif res.get("inserted"):
                    print(f"[zoho-imap] ingested {res['inserted']} reply/replies "
                          f"(matched {res.get('matched', 0)}, unmatched {res.get('unmatched', 0)})", flush=True)
            except Exception as exc:
                print(f"[zoho-imap] poll failed: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(max(300, int(interval_seconds or 900)))

    t = threading.Thread(target=_loop, name="zoho-imap-inbound", daemon=True)
    t.start()
    return t
