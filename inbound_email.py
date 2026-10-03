"""Inbound email ingestion for lead replies.

Two paths feed this:
1. Gmail API poller (env GMAIL_INBOUND=on): reads the connected Google mailbox
   and matches sender addresses against leads. This is the live path -- mail is
   on Google, and outbound is the Gmail API too.
2. Resend-style webhook (POST /api/email/inbound): idempotent via external_id.

Every ingested reply writes a comms_logs row, flags the lead (status new ->
contacted, last_reply_at set) and appends the message to the lead notes.
"""

import hashlib
import json
import os
import re
import time
import urllib.request
import urllib.error
from email.header import decode_header
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


def _safe_ddl(cur, statements, label=""):
    """Run DDL in its own savepoint, retrying once on lock contention.

    CREATE INDEX needs an AccessExclusiveLock, which deadlocks against the
    scheduler threads already writing to `leads`. Without this the whole startup
    transaction aborts and the tables after this point are never created. A
    savepoint keeps the failure local so the rest of startup still commits."""
    for attempt in range(2):
        sp = f"safe_ddl_{label or 'x'}"
        try:
            cur.execute(f"SAVEPOINT {sp};")
            cur.execute("SET LOCAL lock_timeout = '5s';")
            for stmt in statements:
                cur.execute(stmt)
            cur.execute(f"RELEASE SAVEPOINT {sp};")
            return True
        except Exception as exc:
            try:
                cur.execute(f"ROLLBACK TO SAVEPOINT {sp};")
            except Exception:
                pass
            if attempt == 0 and ("deadlock" in str(exc).lower() or "lock" in str(exc).lower()):
                time.sleep(1.0)
                continue
            print(f"[email-inbound] schema step '{label}' skipped: {exc}", flush=True)
            return False
    return False


def _ensure_schema(cur):
    _safe_ddl(cur, [
        "ALTER TABLE comms_logs ADD COLUMN IF NOT EXISTS external_id VARCHAR(255);",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comms_logs_external_id ON comms_logs(external_id);",
        "ALTER TABLE comms_logs ADD COLUMN IF NOT EXISTS lead_id INTEGER;",
        "ALTER TABLE comms_logs ADD COLUMN IF NOT EXISTS sent_at TIMESTAMP WITH TIME ZONE;",
        "CREATE INDEX IF NOT EXISTS idx_comms_recipient ON comms_logs(LOWER(recipient));",
        "CREATE INDEX IF NOT EXISTS idx_comms_direction_channel_created "
        "ON comms_logs(direction, channel, created_at);",
    ], "comms")
    # This runs before `leads` is created on a fresh database; do not abort the
    # whole startup transaction if it is not there yet.
    _safe_ddl(cur, [
        "ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_reply_at TIMESTAMP WITH TIME ZONE;",
        "UPDATE leads SET email = NULLIF(rtrim(email, '.'), '') WHERE email LIKE '%.';",
    ], "leads")


def _ingest(db, company_key, msgs, mailbox_from):
    """Insert inbound email comms rows. Returns match/insert/dedup counts."""
    matched = inserted = deduped = skipped = unmatched = 0
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
            text = (m.get("text") or "").strip()
            subject = (m.get("subject") or "").strip() or "(no subject)"
            ext_id = (m.get("external_id") or "").strip() or _make_content_id(sender, subject, m.get("date") or "")
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
            if not lead:
                # Still recorded in comms_logs above, but there is no lead row to
                # hang the reply note on. Previously this `continue`d and the
                # message was never recorded at all.
                unmatched += 1
                db.commit()
                continue
            matched += 1
            lead_id = lead["id"]
            cur.execute("SELECT id, email, name, notes, status, phone FROM leads WHERE id = %s;", (lead_id,))
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
                (notes, lead_id),
            )
            # Commit the inbound row + reply note before any AI work, so a slow or
            # failing draft call can never roll the recorded reply away.
            db.commit()
            try:
                import auto_reply
                auto_reply.ensure_lead_reply(
                    db,
                    company_key,
                    name=lead.get("name") or "",
                    phone=lead.get("phone") or "",
                    email=sender,
                    service="",
                    message=f"Previous reply: {subject}\n{text[:500]}",
                    source="inbound-email",
                    lead_id=lead_id,
                )
            except Exception as exc:
                print(f"[email-inbound] auto-reply failed for lead {lead_id}: {exc}", flush=True)
        db.commit()
    return {"matched": matched, "inserted": inserted, "deduped": deduped,
            "skipped": skipped, "unmatched": unmatched}


def _make_content_id(sender, subject, date):
    """Stable synthetic id for a message that carries none of its own.

    Used as a last-resort dedup key, so it must be derived from content rather
    than arrival time -- the same message arriving twice has to hash the same
    way or dedup fails. Prefixed `content|` so it cannot collide with the
    `gmail|`, `wh|`, or `raw|` namespaces used by the paths that do have a real
    provider id.
    """
    h = hashlib.sha1(f"{sender}|{subject}|{date}".encode("utf-8", "replace")).hexdigest()[:24]
    return f"content|{h}"


def poll_gmail_inbox(db, service: str, company_key: str, mailbox_from: str,
                     user_email: str = "", max_results: int = 25) -> dict:
    """Poll the connected Google mailbox for lead replies via the Gmail API.

    This is the primary inbound path now that mail is on Google. It reads
    `google_tokens` for a refresh token, lists unread/recent inbox messages,
    converts them to the same shape `_ingest` consumes, and lets the existing
    `external_id` dedup make re-polling harmless.

    Gmail is append-only per message id, so re-reading the same window is safe:
    duplicates are deduped on the `gmail|<id>` external_id.
    """
    import google_gmail
    import google_oauth

    owner = (user_email or os.getenv("GMAIL_OWNER", "") or os.getenv("SMTP_FROM", "")
             or "hello@bizstackperks.com").strip()
    try:
        tokens = google_oauth.get_google_tokens(service, owner)
    except Exception as exc:
        return {"error": f"token lookup failed: {exc}"}
    if not tokens or not tokens.get("refresh_token"):
        return {"error": f"no Google refresh token for {service}/{owner} — sign in with Google first"}

    # Only look at unread mail we have not already triaged. `is:unread` plus a
    # recency window keeps each poll cheap; dedup handles any overlap.
    query = os.getenv("GMAIL_INBOUND_QUERY", "in:inbox is:unread newer_than:7d")
    try:
        listing = google_gmail.list_messages(service, owner, query=query, max_results=max_results)
    except Exception as exc:
        return {"error": f"Gmail list failed: {exc}"}

    stubs = listing.get("messages") or []
    if not stubs:
        return {"matched": 0, "inserted": 0, "deduped": 0, "skipped": 0,
                "unmatched": 0, "checked": 0}

    msgs = []
    for stub in stubs:
        mid = stub.get("id")
        if not mid:
            continue
        try:
            full = google_gmail.get_message(service, owner, mid, format="full")
        except Exception as exc:
            print(f"[gmail-inbound] fetch {mid} failed: {exc}", flush=True)
            continue
        parsed = google_gmail._parse_message(full)
        msgs.append({
            "from": parsed.get("from") or "",
            "to": parsed.get("to") or "",
            "subject": parsed.get("subject") or "",
            "date": parsed.get("date") or "",
            "text": parsed.get("body_text") or parsed.get("snippet") or "",
            "external_id": f"gmail|{mid}",
        })
    if not msgs:
        return {"matched": 0, "inserted": 0, "deduped": 0, "skipped": 0,
                "unmatched": 0, "checked": 0}
    result = _ingest(db, company_key, msgs, mailbox_from)
    result["checked"] = len(msgs)
    return result


def handle_webhook_payload(payload, company_key, mailbox_from):
    """Ingest a Resend-style 'email.received' array or a single object.

    Supports both the raw Resend envelope ({"type": "email.received",
    "data": {...}}) and pre-flattened dicts. Resend webhooks only carry
    metadata, so the message body is fetched from the Receiving API when
    an email_id is present (RESEND_API_KEY required); otherwise the raw
    text/html fields are used.
    """
    records = payload if isinstance(payload, list) else [payload]
    msgs = []
    for rec in records:
        if isinstance(rec, dict) and rec.get("type") == "email.received" and isinstance(rec.get("data"), dict):
            rec = rec["data"]
        if not isinstance(rec, dict):
            continue
        email_id = rec.get("email_id") or rec.get("id") or ""
        text = rec.get("text") or rec.get("html") or ""
        if email_id and not text:
            text = _resend_fetch_body(email_id)
        msgs.append({
            "from": rec.get("from") or "",
            "to": (rec.get("to") or ""),
            "subject": rec.get("subject") or "",
            "date": rec.get("created_at") or rec.get("date") or "",
            "text": text,
            # Operator precedence bug: `"wh|" + str(x) or ""` is
            # `("wh|" + str(x)) or ""`, so an id-less payload collapsed onto the
            # constant "wh|" and every later one was silently deduped away.
            "external_id": (f"wh|{email_id}" if email_id
                            else _make_content_id(rec.get("from") or "",
                                              rec.get("subject") or "",
                                              rec.get("created_at") or rec.get("date") or "")),
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


def _resend_fetch_body(email_id):
    """Fetch an inbound email's text body from the Resend Receiving API."""
    key = (os.getenv("RESEND_API_KEY", "") or "").strip()
    if not key or not email_id:
        return ""
    url = f"https://api.resend.com/emails/receiving/{email_id}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "User-Agent": "bizstack-inbound/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError, OSError) as exc:
        print(f"[email-inbound] receiving.get failed for {email_id}: {exc}", flush=True)
        return ""
    text = (data.get("text") or data.get("html") or "").strip()
    if not text:
        return ""
    if not data.get("text"):
        text = _TAG_RE.sub(" ", text)
        text = _ENT_RE.sub(" ", text)
        text = _MULTI_WS.sub(" ", text)
    return text[:8000]


if __name__ == "__main__":
    # One-shot poll of the Google mailbox. Polling on a schedule is the
    # construction_main._gmail_inbound_loop thread's job, gated on GMAIL_INBOUND.
    import psycopg
    from psycopg.rows import dict_row

    _company = (os.getenv("INBOUND_COMPANY_KEY", "") or "").strip() or "construction"
    _mailbox = (os.getenv("SMTP_FROM", "") or "").strip() or "hello@bizstackperks.com"
    with psycopg.connect(os.getenv("DATABASE_URL", ""), row_factory=dict_row) as _db:
        _db.autocommit = False
        print(json.dumps(poll_gmail_inbox(_db, _company, _company, _mailbox)), flush=True)