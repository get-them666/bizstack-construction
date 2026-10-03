"""Automated SBA microloan outreach: cadence scheduler + AI-driven email replies.

The scheduler fires the Day 1 / Day 3 / Day 7 / Day 14 touchpoints from
docs/OUTREACH_EMAILS.md. Lender replies arrive on the Resend inbound webhook
(POST /api/email/inbound), which hands allowlisted senders to `handle_inbound`:
the AI assistant drafts a reply from the reply playbook and it goes out through
the app mailer. All activity is logged to `outreach_touches` / `outreach_replies`.
"""

import asyncio
import datetime as _dt
import os
from datetime import datetime, timezone
from email.utils import parseaddr

import psycopg
from psycopg.rows import dict_row

from ai_agent import BusinessAIAgent
from documents_service import send_email as _shared_send_email, smtp_config_from_env

CAMPAIGN_START_KEY = "loan_campaign_start"
FROM_ADDR = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "hello@bizstackperks.com"))
FROM_NAME = os.getenv("SMTP_NAME", "BizStack")

# A touchpoint whose copy is time-relative ("I emailed you yesterday") must not
# fire days late; the scheduler marks it 'missed' past this window instead.
TOUCH_GRACE_DAYS = max(0, int(os.getenv("OUTREACH_TOUCH_GRACE_DAYS", "2") or 2))
REPLY_RETRY_EVERY_SECONDS = max(60, int(os.getenv("OUTREACH_RETRY_SECONDS", "600") or 600))

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
    # Every lender is email-only, so every touchpoint must be an email. Days 1
    # and 7 used to be tagged "text" and were therefore skipped by the channel
    # check below, which meant the lenders only ever received the Day 3 email.
    if day == 1:
        return {
            "kind": "email", "subject": "$50K microloan — BizStack",
            "body": (
                "Hi [First Name], this is Shaun O'Leary with BizStack in Williamsburg. I'm looking "
                "for a $50K SBA microloan for my company — short-term rental construction plus "
                "cleaning and co-hosting. Happy to send the full package and documents today. "
                "Thanks — 252-665-5891 · bizstackperks.com\n\n"
                "Shaun O'Leary\nBizStack · 252-665-5891 · hello@bizstackperks.com"
            ),
        }
    if day == 3:
        return {
            "kind": "email", "subject": "Re: $50K microloan — BizStack",
            "body": (
                "Hi [First Name],\n\n"
                "Following up with one thing I didn't include when I first reached out: the reason I "
                "think this is a strong file despite being a new entity is that the hard part already "
                "exists — the operating system. Quotes generate from property data in minutes, every "
                "call and text is answered 24/7 by AI, crews clock in with GPS and upload "
                "photo-verified work, and payments and payroll run through Stripe.\n\n"
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
            "kind": "email", "subject": "Re: $50K microloan — BizStack",
            "body": (
                "Hi [First Name],\n\n"
                "Checking in on the $50K microloan request. Is there anything you need from me to "
                "move it forward? I can get the full package and any documents over same-day.\n\n"
                "Thanks,\nShaun O'Leary\nBizStack · 252-665-5891 · hello@bizstackperks.com"
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


def _mailer_cfg() -> dict:
    """Sender config for lender mail.

    Reuses the app mailer so outreach rides the same delivery ladder as every
    other email, and pins the sender name to BizStack so lender mail never
    inherits a per-service name like "Broom Service".
    """
    cfg = smtp_config_from_env()
    if FROM_ADDR:
        cfg["SMTP_FROM"] = FROM_ADDR
    cfg["SMTP_NAME"] = FROM_NAME
    return cfg


async def send_email(to: str, cc: str, subject: str, body: str, run_in_thread: bool = True) -> bool:
    """Send lender mail through the shared mailer (Resend -> SES -> SMTP ladder).

    This used to talk smtplib directly, which only works while the host has
    outbound SMTP egress. Railway firewalls that, so the cadence silently
    failed there; the HTTPS paths keep working.
    """
    try:
        sent = await _shared_send_email(_mailer_cfg(), to, subject, body, cc=cc or "")
    except Exception as e:
        print(f"[outreach] send failure to {to}: {e}")
        return False
    if not sent:
        print(f"[outreach] mailer reported failure for {to}")
    return bool(sent)


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


def mail_ready() -> bool:
    """Is there any transport that can actually deliver right now?

    Checked without sending anything. The operator retired SES/Zoho/Resend, so
    this asks whether a Gmail OAuth token exists for a sender address. A false
    here means the cadence must not consume touchpoints.
    """
    if _MAIL_READY_CACHE.get("value") is not None:
        return bool(_MAIL_READY_CACHE["value"])
    ok = False
    detail = ""
    try:
        sender = (FROM_ADDR or "").strip()
        if not sender:
            detail = "no SMTP_FROM set"
        else:
            import google_oauth
            services = (os.getenv("GMAIL_SEND_SERVICES", "construction,broom") or "")
            for svc in [s.strip() for s in services.split(",") if s.strip()]:
                try:
                    if google_oauth.get_valid_access_token(svc, sender):
                        ok = True
                        detail = f"Gmail token valid for {svc}"
                        break
                except Exception as exc:
                    detail = f"{svc}: {exc}"
    except Exception as exc:
        detail = str(exc)

    if not ok and not detail:
        detail = "no Gmail token for the sender address"
    _MAIL_READY_CACHE["value"] = ok
    _MAIL_READY_CACHE["detail"] = detail
    _MAIL_READY_CACHE["checked_at"] = datetime.now(timezone.utc).isoformat()
    print(f"[outreach] mail preflight: {'OK' if ok else 'BROKEN'} — {detail}", flush=True)
    return ok


def mail_status() -> dict:
    """Current deliverability, for the owner-facing campaign panel."""
    ok = mail_ready()
    return {
        "ok": ok,
        "detail": _MAIL_READY_CACHE.get("detail", ""),
        "checked_at": _MAIL_READY_CACHE.get("checked_at", ""),
    }


def _record_touch(conn, lender: dict, day: int, spec: dict, status: str, dry: bool = False, body: str = "", subject: str = "") -> None:
    """Upsert one cadence touchpoint. `dry` records a 'missed' row without mail."""
    if dry:
        body = subject = ""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO outreach_touches (lender, recipient, day, kind, subject, body, status, sent_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (lender, recipient, day) DO UPDATE SET status = EXCLUDED.status, "
            "sent_at = EXCLUDED.sent_at, kind = EXCLUDED.kind, subject = EXCLUDED.subject, body = EXCLUDED.body",
            (lender["name"], lender["to"], day, spec["kind"], subject, body,
             status, _dt.datetime.now(_dt.timezone.utc)),
        )
    conn.commit()


# A 'sending' row older than this is assumed to be an abandoned claim (process
# killed mid-send) and is taken over by the next pass.
STALE_SENDING_MINUTES = 30

# Deliverability probe result. Refreshed by mail_ready() once per process.
_MAIL_READY_CACHE: dict = {}


def _claim_touch(conn, lender: dict, day: int) -> bool:
    """Atomically claim a touchpoint for sending. Returns False if another
    worker already holds it.

    Both apps run this scheduler against the same database, so a plain
    SELECT-then-INSERT lets both see 'nothing sent yet' and email the lender
    twice. Flipping status to 'sending' inside the statement makes the claim
    itself the test: exactly one caller sees rowcount == 1.
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE outreach_touches SET status = 'sending', sent_at = %s "
            "WHERE lender = %s AND recipient = %s AND day = %s "
            "  AND status <> 'sending' "
            "  AND (sent_at IS NULL OR sent_at > %s) "
            "RETURNING id;",
            (now, lender["name"], lender["to"], day,
             now - _dt.timedelta(minutes=STALE_SENDING_MINUTES)),
        )
        if cur.fetchone() is not None:
            conn.commit()
            return True
        cur.execute(
            "INSERT INTO outreach_touches (lender, recipient, day, kind, subject, body, status, sent_at) "
            "VALUES (%s, %s, %s, 'email', '', '', 'sending', %s) "
            "ON CONFLICT (lender, recipient, day) DO NOTHING RETURNING id;",
            (lender["name"], lender["to"], day, now),
        )
        claimed = cur.fetchone() is not None
    conn.commit()
    return claimed


async def _run_cadence(conn) -> None:
    start = _campaign_start(conn)
    today = _dt.date.today()
    elapsed = (today - start).days

    # Log deliverability on every pass, before any early return. This preflight
    # used to live only inside the overdue branch, so on day 0 -- and any day
    # with nothing due -- it never ran and a dead campaign looked identical to a
    # healthy one. That silence is what hid the undeliverable mail for weeks.
    ready = mail_ready()
    if elapsed <= 0:
        print(f"[outreach] day {elapsed} of campaign; nothing due yet (mail {'ok' if ready else 'BROKEN'})",
              flush=True)
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
            if row and row["status"] in ("sent", "missed"):
                continue

            spec = _day_body(day)
            if not spec:
                continue
            if spec["kind"] not in lender["channels"]:
                continue

            # Never send stale, time-relative copy late. A touchpoint that fell
            # outside its window is recorded as 'missed' (terminal) so the
            # scheduler stops considering it, rather than mailing a lender
            # "I emailed you yesterday" a week after the fact.
            #
            # This must NOT run while mail is undeliverable. `missed` is
            # terminal, so a dead mail transport used to permanently burn the
            # whole cadence: every touchpoint got marked missed while nothing
            # could send, and the campaign then looked healthy in the log
            # forever while it had in fact already given up. When no transport
            # can send, leave the touchpoint untouched so the sequence recovers
            # the moment mail is fixed.
            if not row and elapsed - day > TOUCH_GRACE_DAYS:
                if not mail_ready():
                    print(
                        f"[outreach] mail is undeliverable; day {day} -> {lender['name']} "
                        f"held open (NOT marked missed) so it can send once mail works"
                    )
                    continue
                _record_touch(conn, lender, day, spec, "missed", dry=True)
                print(
                    f"[outreach] day {day} -> {lender['name']}: MISSED "
                    f"(was due {(elapsed - day)} days ago, grace {TOUCH_GRACE_DAYS}d)"
                )
                continue

            body = spec["body"].replace("[First Name]", "there")
            body = body.replace("[Lender Name]", lender["name"])
            subject = spec["subject"]

            if not _claim_touch(conn, lender, day):
                print(f"[outreach] day {day} -> {lender['name']}: claimed by another worker, skipping")
                continue

            ok = await send_email(lender["to"], lender["cc"], subject, body)
            _record_touch(conn, lender, day, spec, "sent" if ok else "failed", body=body, subject=subject)
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
    """True when the sender's domain is an allowlisted lender domain.

    Matches on the parsed domain, exact or subdomain. A plain substring/suffix
    test let 'notsba.gov' match 'sba.gov', so any lookalike domain would have
    been auto-replied to.
    """
    addr = parseaddr(sender or "")[1].strip().lower()
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else addr
    if not domain or "." not in domain:
        return False
    return any(domain == d or domain.endswith("." + d) for d in REPLY_ALLOWLIST_DOMAINS)


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


def _record_reply(conn, msg_id: str, sender: str, subject: str, body: str, reply: str, status: str) -> bool:
    """Insert a lender reply + our response. ON CONFLICT DO NOTHING on message_id
    so a webhook redelivery can never double-send."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO outreach_replies (message_id, sender, subject, body, reply_body, status, replied_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (message_id) DO NOTHING RETURNING id;",
            (msg_id, sender, subject, body, reply, status, _dt.datetime.now(_dt.timezone.utc)),
        )
        inserted = cur.fetchone() is not None
    conn.commit()
    return inserted


def _reply_subject(subject: str) -> str:
    """Lender subjects usually already start with 'Re:', so blindly
    prepending produced 'Re: Re: ...'."""
    s = (subject or "").strip()
    return s if s.lower().startswith("re:") else f"Re: {s}"


async def _reply_to_lender(sender: str, subject: str, body: str, msg_id: str) -> dict:
    """Draft and send one reply to an allowlisted lender.

    Claims the message_id *before* sending. The unique index makes the claim
    atomic, so a webhook redelivery (or the second app, which shares this DB)
    cannot send a second reply to the same message.

    This is a coroutine, not a sync function wrapping asyncio.run: the only
    caller is the webhook, which is itself already inside a running event loop,
    where asyncio.run() raises.
    """
    with psycopg.connect(os.getenv("DATABASE_URL", ""), row_factory=dict_row) as conn:
        claimed = _record_reply(conn, msg_id, sender, subject, body[:8000], "", "pending")
        if not claimed:
            print(f"[outreach] {msg_id} already handled; not replying again")
            return {"sender": sender, "subject": subject, "status": "duplicate",
                    "recorded": False, "duplicate": True}
        reply = await asyncio.to_thread(_draft_reply, subject, body)
        ok = await send_email(sender, "", _reply_subject(subject)[:250], reply)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE outreach_replies SET reply_body = %s, status = %s, replied_at = %s WHERE message_id = %s",
                (reply, "sent" if ok else "failed", _dt.datetime.now(_dt.timezone.utc), msg_id),
            )
        conn.commit()
    return {"sender": sender, "subject": subject, "status": "sent" if ok else "failed",
            "recorded": True, "duplicate": False}


def _webhook_records(payload) -> list:
    """Flatten a Resend 'email.received' payload into plain message dicts."""
    records = payload if isinstance(payload, list) else [payload]
    out = []
    for rec in records:
        if isinstance(rec, dict) and rec.get("type") == "email.received" and isinstance(rec.get("data"), dict):
            rec = rec["data"]
        if not isinstance(rec, dict):
            continue
        out.append(rec)
    return out


def _reply_body_for(rec: dict) -> str:
    text = rec.get("text") or rec.get("html") or ""
    if not text:
        email_id = rec.get("email_id") or rec.get("id") or ""
        if email_id:
            try:
                from inbound_email import _resend_fetch_body

                text = _resend_fetch_body(email_id)
            except Exception as e:
                print(f"[outreach] body fetch failed for {email_id}: {e}")
    return str(text or "").strip()


async def handle_inbound(payload) -> list:
    """Entry point for the inbound-email webhook.

    Lead ingest only matches senders that exist in `leads`, so lender mail was
    being dropped as 'skipped' before it ever reached the outreach logic. This
    claims allowlisted senders first and answers them.

    Async because the webhook route is already inside a running event loop.
    """
    results = []
    for rec in _webhook_records(payload):
        sender = parseaddr(str(rec.get("from") or ""))[1].strip().lower()
        if not sender or not _is_reply_from_lender(sender):
            continue
        subject = str(rec.get("subject") or "").strip() or "(no subject)"
        if _is_auto_message({"From": sender, "Subject": subject}):
            print(f"[outreach] skipped auto-reply from {sender}")
            continue
        email_id = rec.get("email_id") or rec.get("id") or ""
        msg_id = f"wh|{email_id}" if email_id else f"{sender}|{subject}|{rec.get('created_at') or rec.get('date') or ''}"
        try:
            results.append(await _reply_to_lender(sender, subject, await asyncio.to_thread(_reply_body_for, rec), msg_id))
        except Exception as e:
            print(f"[outreach] reply to {sender} failed: {e}")
    return results


async def retry_failed_replies() -> list:
    """Re-send replies whose send failed (transient mailer/quota errors)."""
    try:
        with psycopg.connect(os.getenv("DATABASE_URL", ""), row_factory=dict_row) as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT message_id, sender, subject, reply_body FROM outreach_replies "
                    "WHERE status IN ('failed', 'pending') ORDER BY id LIMIT 10;"
                )
                pending = cur.fetchall()
            out = []
            for row in pending:
                ok = await send_email(row["sender"], "", _reply_subject(row["subject"])[:250],
                                      row["reply_body"] or "")
                with conn.cursor() as cur:
                    cur.execute("UPDATE outreach_replies SET status = %s, replied_at = %s WHERE message_id = %s",
                                ("sent" if ok else "failed", _dt.datetime.now(_dt.timezone.utc),
                                 row["message_id"]))
                conn.commit()
                out.append({"sender": row["sender"], "status": "sent" if ok else "failed"})
            return out
    except Exception as e:
        print(f"[outreach] reply retry failure: {e}")
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
    """Retries failed sends. Lender replies themselves arrive on the webhook
    (see handle_inbound)."""
    print("[outreach] reply retry loop started")
    while True:
        try:
            for r in await retry_failed_replies():
                print(f"[outreach] retried reply to {r['sender']}: {r['status']}")
        except Exception as e:
            print(f"[outreach] reply pass failure: {e}")
        await asyncio.sleep(REPLY_RETRY_EVERY_SECONDS)


def start_outreach_tasks() -> list:
    return [asyncio.create_task(_outreach_loop()), asyncio.create_task(_reply_loop())]


# --- Deliberate operator actions -------------------------------------------

CATCHUP_DAY = 0  # day 0 keeps the catch-up out of the 1/3/7/14 cadence
CATCHUP_SUBJECT = "Re: $50K microloan — BizStack"
CATCHUP_BODY = (
    "Hi there,\n\n"
    "Checking back in on the $50K SBA microloan request. I reached out earlier this month and "
    "then went quiet on you, which I don't like — so apologies for the gap.\n\n"
    "Short version of where things stand: BizStack is a short-term rental construction and "
    "cleaning/co-hosting business in Williamsburg. Quotes generate from property data in minutes, "
    "every call and text is answered 24/7 by AI, crews clock in with GPS and upload photo-verified "
    "work, and payments and payroll run through Stripe. Two revenue lines on one platform: "
    "guest-funded cleaning fees collected at booking, plus fixed-price construction scopes.\n\n"
    "My package and projections are ready now and I can send documents same-day. If this isn't a "
    "fit at [Lender Name], I'd genuinely appreciate a pointer to whoever handles startups in your "
    "shop, or to the SBDC.\n\n"
    "Happy to do a short call whenever suits you.\n\n"
    "Shaun O'Leary\nBizStack · 252-665-5891\nhello@bizstackperks.com · bizstackperks.com"
)


async def send_catchup(force: bool = False) -> list:
    """One honest re-engagement to every lender.

    The cadence cannot do this job: Day 1 and Day 7 are legitimately 'missed'
    now, so the next scheduled touch is Day 14. This copy owns the gap instead
    of pretending the last note was yesterday. Recorded as day 0 so it does not
    disturb the 1/3/7/14 schedule, and it is a no-op once sent.
    """
    out = []
    for lender in LENDERS:
        with psycopg.connect(os.getenv("DATABASE_URL", ""), row_factory=dict_row) as conn:
            _ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM outreach_touches WHERE lender = %s AND recipient = %s AND day = %s",
                    (lender["name"], lender["to"], CATCHUP_DAY),
                )
                prior = cur.fetchone()
            if prior and prior["status"] == "sent" and not force:
                print(f"[outreach] catch-up already sent to {lender['name']}; skipping")
                out.append({"lender": lender["name"], "status": "already-sent"})
                continue

            body = CATCHUP_BODY.replace("[Lender Name]", lender["name"])
            spec = {"kind": "email", "subject": CATCHUP_SUBJECT}
            ok = await send_email(lender["to"], lender["cc"], CATCHUP_SUBJECT, body)
            _record_touch(conn, lender, CATCHUP_DAY, spec, "sent" if ok else "failed",
                          body=body, subject=CATCHUP_SUBJECT)
            print(f"[outreach] catch-up -> {lender['name']} ({lender['to']}): {'sent' if ok else 'FAILED'}")
            out.append({"lender": lender["name"], "to": lender["to"], "status": "sent" if ok else "failed"})
    return out


def status() -> dict:
    with psycopg.connect(os.getenv("DATABASE_URL", ""), row_factory=dict_row) as conn:
        _ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = %s", (CAMPAIGN_START_KEY,))
            row = cur.fetchone()
            start = _dt.date.fromisoformat(row["value"].split("T")[0]) if row else None
            cur.execute("SELECT lender, day, kind, status, sent_at FROM outreach_touches ORDER BY lender, day")
            touches = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT sender, subject, status, replied_at FROM outreach_replies ORDER BY id DESC LIMIT 20")
            replies = [dict(r) for r in cur.fetchall()]
    elapsed = (_dt.date.today() - start).days if start else None
    return {"campaign_start": str(start) if start else None, "elapsed_days": elapsed,
            "touches": touches, "replies": replies}


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="BizStack SBA microloan outreach")
    ap.add_argument("--catchup", action="store_true", help="send the re-engagement to every lender")
    ap.add_argument("--force", action="store_true", help="with --catchup, resend even if already sent")
    ap.add_argument("--once", action="store_true", help="run one cadence pass")
    ap.add_argument("--status", action="store_true", help="print campaign state as JSON")
    args = ap.parse_args()
    did = False
    if args.status:
        print(json.dumps(status(), indent=2, default=str))
        did = True
    if args.catchup:
        print(json.dumps(asyncio.run(send_catchup(force=args.force)), indent=2))
        did = True
    if args.once or not did:
        with psycopg.connect(os.getenv("DATABASE_URL", ""), row_factory=dict_row) as _c:
            _ensure_schema(_c)
            asyncio.run(_run_cadence(_c))
