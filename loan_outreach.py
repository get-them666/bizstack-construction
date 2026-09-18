"""Automated SBA microloan outreach: cadence scheduler + AI-driven email replies.

The scheduler fires the Day 1 / Day 3 / Day 7 / Day 14 touchpoints from
docs/OUTREACH_EMAILS.md. A separate poller reads inbound mail from the lender
mailbox, has the AI assistant draft a reply from the reply playbook, and sends it.
All activity is logged to `outreach_touches` / `outreach_replies`.
"""

import asyncio
import datetime as _dt
import email as _email
import imaplib
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import format_datetime

import psycopg
from psycopg.rows import dict_row

from ai_agent import BusinessAIAgent

CAMPAIGN_START_KEY = "loan_campaign_start"
FROM_ADDR = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "hello@bizstackperks.com"))
FROM_NAME = os.getenv("SMTP_NAME", "BizStack")

SMTP_HOST = os.getenv("SMTP_HOST", "mail.privateemail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465") or 465)
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_USE_TLS = os.getenv("SMTP_TLS", "ssl").lower() == "ssl"

IMAP_HOST = os.getenv("IMAP_HOST", "mail.privateemail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993") or 993)
IMAP_USER = os.getenv("IMAP_USER", SMTP_USER)
IMAP_PASS = os.getenv("IMAP_PASS", SMTP_PASS)

CHANNEL_EMAIL = "email"
CHANNEL_TEXT = "text"

# --- Cadence ---------------------------------------------------------------
LENDERS = [
    {
        "name": "LISC",
        "to": "smallbusiness@lisc.org",
        "cc": "wmartin@lisc.org",
        "domain": "lisc.org",
        "channels": [CHANNEL_EMAIL],
    },
    {
        "name": "VCC",
        "to": "jbarnes@vccva.org",
        "cc": "",
        "domain": "vccva.org",
        "channels": [CHANNEL_EMAIL],
    },
    {
        "name": "VSBFA",
        "to": "VSBFA@sbsd.virginia.gov",
        "cc": "",
        "domain": "virginia.gov",
        "channels": [CHANNEL_EMAIL],
    },
]

REPLY_ALLOWLIST_DOMAINS = (
    "lisc.org",
    "vccva.org",
    "virginia.gov",
    "sbsd.virginia.gov",
    "hrchamber.com",
    "sba.gov",
)


def _day_body(day: int) -> dict:
    if day == 1:
        return {
            "kind": "text", "subject": "Re: $50K microloan — BizStack",
            "body": (
                "Hi [First Name], this is Shaun O'Leary with BizStack in Williamsburg. I emailed "
                "yesterday about a $50K microloan for my company (STR construction + cleaning/"
                "co-hosting). Wanted to make sure it landed — happy to send everything by email "
                "today. Thanks — 252-665-5891 · bizstackperks.com"
            ),
        }
    if day == 3:
        return {
            "kind": "email", "subject": "Re: $50K microloan — BizStack",
            "body": (
                "Hi [First Name],\n\n"
                "Following up with one thing I didn't include yesterday: the reason I think this "
                "is a strong file despite being a new entity is that the hard part already exists "
                "— the operating system. Quotes generate from property data in minutes, every call "
                "and text is answered 24/7 by AI, crews clock in with GPS and upload photo-verified "
                "work, and payments and payroll run through Stripe.\n\n"
                "And it's two revenue lines on one platform: guest-funded cleaning fees collected "
                "at booking (pre-paid, predictable) plus fixed-price construction scopes. Loan "
                "dollars go straight into jobs and turnover volume — not into figuring out how to "
                "run the office.\n\n"
                "Happy to do a short phone call at whatever time suits you. What else would you "
                "like to see?\n\n"
                "Thanks,\nShaun O'Leary\nBizStack · 252-665-5891 · hello@bizstackperks.com"
            ),
        }
    if day == 7:
        return {
            "kind": "text", "subject": "Re: $50K microloan — BizStack",
            "body": (
                "Hi [First Name], Shaun with BizStack. Just checking in on the $50K microloan "
                "request — is there anything you need from me to move it forward? I can get "
                "documents over same-day. Thanks — 252-665-5891 · bizstackperks.com"
            ),
        }
    if day == 14:
        return {
            "kind": "email", "subject": "Re: $50K microloan — checking in",
            "body": (
                "Hi [First Name],\n\n"
                "I know you're busy, so I'll leave it here: my $50K microloan request and complete "
                "package are ready whenever you are. If this isn't the right fit at [Lender Name], "
                "could you point me to who handles startups in your shop — or the SBDC — so I "
                "don't bother you further?\n\n"
                "Either way, thank you for your time.\n\n"
                "Shaun O'Leary\nBizStack · 252-665-5891\nhello@bizstackperks.com · bizstackperks.com"
            ),
        }
    return {}


def _ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS outreach_touches (
                id SERIAL PRIMARY KEY,
                lender VARCHAR(120) NOT NULL,
                recipient TEXT NOT NULL,
                day INTEGER NOT NULL,
                kind VARCHAR(20) NOT NULL,
                subject TEXT,
                body TEXT,
                status VARCHAR(20) NOT NULL DEFAULT 'due',
                sent_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (lender, recipient, day)
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS outreach_replies (
                id SERIAL PRIMARY KEY,
                message_id TEXT UNIQUE,
                sender TEXT NOT NULL,
                subject TEXT,
                body TEXT,
                reply_body TEXT,
                status VARCHAR(20) NOT NULL DEFAULT 'sent',
                replied_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
    conn.commit()


async def send_email(to: str, cc: str, subject: str, body: str, run_in_thread: bool = True) -> bool:
    msg = EmailMessage()
    msg["From"] = f"{FROM_NAME} <{FROM_ADDR}>"
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = format_datetime(_dt.datetime.now(_dt.timezone.utc))
    msg.set_content(body)

    def _send() -> bool:
        try:
            if SMTP_USE_TLS:
                ctx = ssl.create_default_context()
                with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=60) as s:
                    s.login(SMTP_USER, SMTP_PASS)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as s:
                    s.starttls()
                    s.login(SMTP_USER, SMTP_PASS)
                    s.send_message(msg)
            return True
        except Exception as e:
            print(f"[outreach] SMTP send failure: {e}")
            return False

    if run_in_thread:
        return await asyncio.to_thread(_send)
    return _send()


def _campaign_start(conn) -> _dt.date:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (CAMPAIGN_START_KEY,))
        row = cur.fetchone()
    if row:
        try:
            return _dt.date.fromisoformat(row["value"].split("T")[0])
        except (ValueError, TypeError):
            pass
    today = _dt.date.today()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (CAMPAIGN_START_KEY, today.isoformat()),
        )
    conn.commit()
    return today


async def _run_cadence(conn) -> None:
    start = _campaign_start(conn)
    today = _dt.date.today()
    elapsed = (today - start).days
    if elapsed <= 0:
        return

    for lender in LENDERS:
        for day in (1, 3, 7, 14):
            if elapsed < day:
                continue
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, sent_at FROM outreach_touches WHERE lender = %s AND recipient = %s AND day = %s",
                    (lender["name"], lender["to"], day),
                )
                row = cur.fetchone()
            if row and row["status"] == "sent":
                continue

            spec = _day_body(day)
            if not spec:
                continue
            if spec["kind"] not in lender["channels"]:
                continue

            body = spec["body"].replace("[First Name]", "there")
            body = body.replace("[Lender Name]", lender["name"])
            subject = spec["subject"]

            ok = await send_email(lender["to"], lender["cc"], subject, body)

            with conn.cursor() as cur:
                if row:
                    cur.execute(
                        "UPDATE outreach_touches SET status = %s, sent_at = %s, kind = %s, subject = %s, body = %s WHERE lender = %s AND recipient = %s AND day = %s",
                        ("sent" if ok else "failed", _dt.datetime.now(_dt.timezone.utc), spec["kind"], subject, body, lender["name"], lender["to"], day),
                    )
                else:
                    cur.execute(
                        "INSERT INTO outreach_touches (lender, recipient, day, kind, subject, body, status, sent_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (lender["name"], lender["to"], day, spec["kind"], subject, body,
                         "sent" if ok else "failed", _dt.datetime.now(_dt.timezone.utc)),
                    )
            conn.commit()
            print(f"[outreach] day {day} -> {lender['name']} ({lender['to']}): {'sent' if ok else 'FAILED'}")


REPLY_SYSTEM_PROMPT = """\
You are the BizStack loan outreach assistant running the $50K SBA microloan application
for owner Shaun O'Leary. A lender (LISC, VCC, VSBFA, or the SBDC) just replied to his
email. Reply concisely and professionally on his behalf using ONLY the approved
responses below — pick the closest match and personalize lightly. Never invent numbers,
dates, or facts not in the reply options. End with "— Shaun".

APPROVED RESPONSES:
- If they ask for documents: "Great — thank you. Sending now from hello@bizstackperks.com. If
  anything is missing, just reply here and I'll have it to you within the hour. — Shaun"
- If they ask about credit (low 600s): "Happy to address it. No judgments, no liens, no
  bankruptcies. I'm actively paying down utilization now — the score is on the way up, and the
  file's strength is 25 years of trade experience, two revenue lines, and a projected DSCR well
  above the 1.10 requirement. — Shaun"
- If they ask for revenue history: "Understood — that's exactly why I'm applying for a microloan
  rather than a bank term loan. The SBA's 2026 small-loan rules allow projected cash flow for
  startups, and my projections show the payment covered in year one. Would the SBDC-prepared
  package help you take a second look? — Shaun"
- If they ask to schedule a call or zoom: propose a time, give 252-665-5891 and
  hello@bizstackperks.com, ask what works for them.
- If they request the full package: confirm it will be sent same-day from hello@bizstackperks.com.
- If they approve: "Thank you — I appreciate it. Send me the next steps and I'll return
  everything same-day. — Shaun"
- If they decline or pass: "I appreciate you looking at it. Two asks: who else should I talk to,
  and what one thing would change your answer? Thank you — Shaun"
- Anything else: a brief, warm reply that keeps the door open and restates 252-665-5891 and
  hello@bizstackperks.com.
"""


def _draft_reply(subject: str, body: str) -> str:
    try:
        agent = BusinessAIAgent(subset="guest", tool_handlers=None)
        response = agent.client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[
                {"role": "system", "content": REPLY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Lender email subject: {subject}\n\n"
                        f"Lender email body:\n{body[:2000]}\n\n"
                        "Draft my reply per the approved responses."
                    ),
                },
            ],
            max_tokens=400,
            temperature=0.4,
        )
        text = (response.choices[0].message.content or "").strip()
        return text or (
            "Thanks for your note — I'll have everything you need over by email today. "
            "252-665-5891 · hello@bizstackperks.com. — Shaun"
        )
    except Exception as e:
        print(f"[outreach] AI draft failure: {e}")
        return (
            "Thanks for your note — I'll have everything you need over by email today. "
            "252-665-5891 · hello@bizstackperks.com. — Shaun"
        )


def _is_reply_from_lender(sender: str) -> bool:
    sender = (sender or "").lower()
    return any(f"@{d}" in sender or sender.endswith(d) for d in REPLY_ALLOWLIST_DOMAINS)


def _is_auto_message(msg) -> bool:
    auto = str(msg.get("Auto-Submitted") or "").lower()
    if auto and auto != "no":
        return True
    if str(msg.get("X-Autoreply") or msg.get("X-Autorespond") or ""):
        return True
    local = str(msg.get("From") or "").lower().split("@")[0].strip()
    for token in ("noreply", "no-reply", "do-not-reply", "donotreply", "mailer-daemon",
                  "postmaster", "bounce", "notifications"):
        if token in local:
            return True
    subj = str(msg.get("Subject") or "").lower()
    for token in ("out of office", "automatic reply", "auto-reply", "auto reply",
                  "undeliverable", "delivery status", "read receipt"):
        if token in subj:
            return True
    return False


def _parse_inbound(raw) -> tuple:
    if not isinstance(raw, bytes):
        raw = bytes(raw)
    msg = _email.message_from_bytes(raw)
    sender = str(msg["From"] or "")
    subject = str(msg["Subject"] or "")
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                else:
                    body = str(payload or "")
                break
    else:
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            try:
                body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
            except Exception:
                body = payload.decode("utf-8", errors="replace")
        else:
            body = str(payload or "")
    return sender, subject, body.strip()


def _dequeue_pending_replies() -> list:
    """Fetch unseen lender mail, log it, reply, and return a summary list."""
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        M.login(IMAP_USER, IMAP_PASS)
        M.select("INBOX")
        typ, data = M.search(None, "UNSEEN")
        ids = data[0].split() if data and data[0] else []
        results = []
        for num in ids:
            typ, msg_data = M.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1] if isinstance(msg_data[0][1], bytes) else bytes(msg_data[0][1])
            sender, subject, body = _parse_inbound(raw)
            if not _is_reply_from_lender(sender):
                continue
            msg = _email.message_from_bytes(raw)
            try:
                msg_id = str(msg.get("Message-ID") or "") or f"{num}_nomid"
            except Exception:
                msg_id = f"{num}_nomid"
            if _is_auto_message(msg):
                M.store(num, "+FLAGS", "\\Seen")
                print(f"[outreach] skipped auto-reply from {sender}")
                continue

            reply = _draft_reply(subject, body)

            with psycopg.connect(os.getenv("DATABASE_URL", ""), row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM outreach_replies WHERE message_id = %s", (msg_id,))
                    if cur.fetchone():
                        continue
                ok = asyncio.run(send_email(sender, "", f"Re: {subject}"[:250], reply, run_in_thread=False))
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO outreach_replies (message_id, sender, subject, body, reply_body, status, replied_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (msg_id, sender, subject, body, reply, "sent" if ok else "failed",
                         _dt.datetime.now(_dt.timezone.utc)),
                    )
                conn.commit()
            results.append({"sender": sender, "subject": subject, "status": "sent" if ok else "failed"})
            M.store(num, "+FLAGS", "\\Seen")
        M.logout()
        return results
    except Exception as e:
        print(f"[outreach] inbox poll failure: {e}")
        return []


async def _outreach_loop() -> None:
    print("[outreach] scheduler started")
    while True:
        try:
            with psycopg.connect(os.getenv("DATABASE_URL", ""), row_factory=dict_row) as conn:
                _ensure_schema(conn)
                await _run_cadence(conn)
        except Exception as e:
            print(f"[outreach] cadence pass failure: {e}")
        await asyncio.sleep(3600)


async def _reply_loop() -> None:
    print("[outreach] reply poller started")
    while True:
        try:
            pending = await asyncio.to_thread(_dequeue_pending_replies)
            for r in pending:
                print(f"[outreach] replied to {r['sender']}: {r['status']}")
        except Exception as e:
            print(f"[outreach] reply pass failure: {e}")
        await asyncio.sleep(600)


def start_outreach_tasks() -> list:
    return [asyncio.create_task(_outreach_loop()), asyncio.create_task(_reply_loop())]