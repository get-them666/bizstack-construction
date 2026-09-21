"""Inbound email ingestion for lead replies.

Two paths feed this:
1. IMAP poller (env INBOUND_POLL=on): reads the owner mailbox and matches
   sender addresses against leads. Marked messages are ingested once.
2. Resend-style webhook (POST /api/email/inbound): idempotent via external_id.

Every ingested reply writes a comms_logs row, flags the lead (status new ->
contacted, last_reply_at set) and appends the message to the lead notes.
"""

import hashlib
import imaplib
import json
import os
import re
import time
from email.header import decode_header
from email.parser import BytesParser
from email.utils import parseaddr

import psycopg
from psycopg.rows import dict_row

_TAG_RE = re.compile(r"<[^>]+>")
_ENT_RE = re.compile(r"&(?:amp|lt|gt|quot|#39|nbsp);")
_MULTI_WS = re.compile(r"[ \t\r\f\v]+")
_NEWLINES = re.compile(r"\n{3,}")


def _clean_addr(raw):
    name, addr = parseaddr((raw or "").strip())
    addr = addr.strip().lower()
    addr = addr.lstrip("<").rstrip(">. ").rstrip(".")
    if "@" not in addr or "." not in addr.split("@", 1)[1]:
        return ""
    return addr


def _norm(n):
    return (n or "").strip().lower()


def _decode_part(part):
    raw = part.get_payload(decode=True)
    if raw is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, "replace")
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


def _subject_header(raw):
    if not raw:
        return ""
    parts = decode_header(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                text = text.decode(enc or "utf-8", "replace")
            except LookupError:
                text = text.decode("utf-8", "replace")
        out.append(text)
    return "".join(out)


def _body_text(msg):
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and not text:
                text = _decode_part(part)
            elif ctype == "text/html" and not text:
                html = _decode_part(part)
                text = _TAG_RE.sub(" ", html)
    else:
        ctype = msg.get_content_type()
        if ctype == "text/html":
            text = _TAG_RE.sub(" ", _decode_part(msg))
        else:
            text = _decode_part(msg)
    text = _ENT_RE.sub(" ", text)
    text = _MULTI_WS.sub(" ", text).replace(" > ", "> ").replace(" < ", "< ")
    text = _NEWLINES.sub("\n\n", text)
    return text.strip()[:8000]


def _ensure_schema(cur):
    cur.execute("ALTER TABLE comms_logs ADD COLUMN IF NOT EXISTS external_id VARCHAR(255);")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_comms_logs_external_id ON comms_logs(external_id);")
    cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_reply_at TIMESTAMP WITH TIME ZONE;")
    cur.execute("UPDATE leads SET email = NULLIF(rtrim(email, '.'), '') WHERE email LIKE '%.';")


def _ingest(db, company_key, msgs, mailbox_from):
    """Insert inbound email comms rows. Returns match/insert/dedup counts."""
    matched = inserted = deduped = skipped = 0
    with db.cursor() as cur:
        _ensure_schema(cur)
        for m in msgs:
            sender = _clean_addr(m.get("from") or "")
            if not sender:
                skipped += 1
                continue
            cur.execute(
                "SELECT id, email, name, notes, status FROM leads "
                "WHERE LOWER(email) = %s AND company = %s;",
                (_norm(sender), company_key),
            )
            lead = cur.fetchone()
            if not lead:
                skipped += 1
                continue
            text = (m.get("text") or "").strip()
            subject = (m.get("subject") or "").strip() or "(no subject)"
            ext_id = (m.get("external_id") or "").strip() or _make_ext_id(sender, subject, m.get("date") or "")
            body = subject
            if text:
                body = f"{subject}\n\n{text[:7000]}"
            cur.execute(
                "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body, external_id) "
                "VALUES ('inbound', 'email', %s, %s, %s, %s) "
                "ON CONFLICT (external_id) DO NOTHING RETURNING id;",
                (sender, mailbox_from, body[:10000], ext_id),
            )
            row = cur.fetchone()
            if not row:
                deduped += 1
                continue
            inserted += 1
            matched += 1
            cur.execute("SELECT id, email, name, notes, status FROM leads WHERE id = %s;", (lead["id"],))
            lead = cur.fetchone()
            notes = lead.get("notes") or ""
            stamp = time.strftime("%Y-%m-%d %H:%M")
            note = f"[reply {stamp} from {sender}] {subject}: {text[:500]}"
            notes = (notes.strip() + "\n" + note).strip() if notes else note
            notes = notes[:4000]
            cur.execute(
                "UPDATE leads SET notes = %s, last_reply_at = CURRENT_TIMESTAMP, "
                "status = CASE WHEN status IN ('new', 'contacted') THEN 'contacted' ELSE status END "
                "WHERE id = %s;",
                (notes, lead["id"]),
            )
        db.commit()
    return {"matched": matched, "inserted": inserted, "deduped": deduped, "skipped": skipped}


def _make_ext_id(sender, subject, date):
    h = hashlib.sha1(f"{sender}|{subject}|{date}".encode("utf-8", "replace")).hexdigest()[:24]
    return f"imap|{h}"


def _fetch_imap_messages(host, user, password, ssl_flag=True):
    msgs = []
    if ssl_flag:
        M = imaplib.IMAP4_SSL(host, 993, timeout=30)
    else:
        M = imaplib.IMAP4(host, 143, timeout=30)
    try:
        M.login(user, password)
        M.select("INBOX")
        typ, data = M.search(None, "UNSEEN")
        ids = (data[0] or b"").split()
        for b in ids[-200:]:
            raw_id = b.decode("ascii") or "0"
            t, mdata = M.fetch(raw_id, "(RFC822)")
            if not mdata or not mdata[0] or not isinstance(mdata[0], tuple):
                continue
            msg = BytesParser().parsebytes(mdata[0][1])
            mid = msg.get("Message-ID") or ""
            ext = ("raw|" + mid) if mid else _make_ext_id(
                msg.get("From") or "", msg.get("Subject") or "", msg.get("Date") or "")
            msgs.append({
                "from": msg.get("From") or "",
                "to": msg.get("To") or "",
                "subject": _subject_header(msg.get("Subject")),
                "date": msg.get("Date") or "",
                "text": _body_text(msg),
                "external_id": ext,
            })
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return msgs


def poll_inbox():
    """Poll the configured business mailbox once for lead replies; returns ingest results.

    Only runs against an explicit INBOUND_IMAP_HOST/USER/PASS mailbox. Never
    falls back to personal credentials."""
    host = (os.getenv("INBOUND_IMAP_HOST", "") or "").strip() or "imap.privateemail.com"
    user = (os.getenv("INBOUND_IMAP_USER", "") or "").strip()
    password = (os.getenv("INBOUND_IMAP_PASS", "") or "").strip()
    company_key = (os.getenv("INBOUND_COMPANY_KEY", "") or "").strip() or "construction"
    mailbox_from = (os.getenv("SMTP_FROM", "") or "").strip() or "hello@bizstackperks.com"
    if not user or not password:
        return {"error": "no INBOUND_IMAP_USER/PASS configured - polling disabled"}
    msgs = _fetch_imap_messages(host, user, password)
    if not msgs:
        return {"matched": 0, "inserted": 0, "deduped": 0, "skipped": 0, "checked": 0}
    db = psycopg.connect(os.getenv("DATABASE_URL", ""), row_factory=dict_row)
    db.autocommit = False
    try:
        result = _ingest(db, company_key, msgs, mailbox_from)
        result["checked"] = len(msgs)
        return result
    finally:
        try:
            db.close()
        except Exception:
            pass


def poll_loop():
    """Daemon loop. Runs every INBOUND_POLL_SECONDS while INBOUND_POLL is on."""
    if not (os.getenv("INBOUND_IMAP_USER", "") or "").strip() or not (os.getenv("INBOUND_IMAP_PASS", "") or "").strip():
        print("[email-inbound] INBOUND_POLL=on but INBOUND_IMAP_USER/PASS not set - not polling", flush=True)
        return
    interval = 180
    try:
        interval = max(30, int(os.getenv("INBOUND_POLL_SECONDS", "180") or 180))
    except (TypeError, ValueError):
        interval = 180
    print(f"[email-inbound] poller started (every {interval}s)", flush=True)
    while True:
        try:
            result = poll_inbox()
            print(f"[email-inbound] polled: {result}", flush=True)
        except Exception as exc:
            print(f"[email-inbound] poll error: {exc}", flush=True)
        time.sleep(interval)


def handle_webhook_payload(payload, company_key, mailbox_from):
    """Ingest a Resend-style 'email.received' array or a single object."""
    records = payload if isinstance(payload, list) else [payload]
    msgs = []
    for rec in records:
        if isinstance(rec, dict):
            msgs.append({
                "from": rec.get("from") or "",
                "to": (rec.get("to") or ""),
                "subject": rec.get("subject") or "",
                "date": rec.get("date") or "",
                "text": rec.get("text") or rec.get("html") or "",
                "external_id": "wh|" + str(rec.get("id") or "") or "",
            })
    db = psycopg.connect(os.getenv("DATABASE_URL", ""), row_factory=dict_row)
    db.autocommit = False
    try:
        return _ingest(db, company_key, msgs, mailbox_from)
    finally:
        try:
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    print(json.dumps(poll_inbox()), flush=True)