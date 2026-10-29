"""Inbound-lead follow-ups and self-serve walkthrough booking.

Scope: leads who contacted US -- website/quote forms, financing, get-started,
the embed widget, the materials picker, the phone bot, chat. They asked to
hear back, so the bot follows up on its own schedule (default day 1, 3, 7
after the lead came in) until one of four things happens: they book a
walkthrough, they reply, they opt out, or the lead closes. Cold permit leads
are NOT in scope here; they stay under auto_reply's contact-once policy.

Every message carries a booking link. /book/{token} lets the homeowner pick a
morning / midday / afternoon window on a weekday; booking stamps
leads.walkthrough_at, tells the owner, confirms to the homeowner, and (when
Google is connected) drops the event on the calendar. That removes the
phone-tag round trip that was the owner's job.

Opt-out: a reply whose whole message is STOP / unsubscribe / remove me marks
leads.opted_out_at, and nothing automated contacts that lead again. Until now
the emails said "reply STOP" and nothing processed it -- a STOP reply set
last_reply_at, which the contact policy read as permission for more touches.

The token in the booking URL is an HMAC of the lead id with the app secret:
unguessable, no extra table, and it survives a redeploy.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import os
import re
import threading
import time
from zoneinfo import ZoneInfo

import brand

LOCAL_TZ = ZoneInfo(os.getenv("BUSINESS_TZ", "America/New_York"))

# Sources that mean "this person contacted us". Anything machine-sourced
# (permit_finder, scrap-io, reddit, backlog, sold-leads...) is excluded.
DEFAULT_INBOUND_SOURCES = {
    "website", "quotes_page", "financing", "finance", "acquisition", "get-started",
    "embed_quote", "instant_quote", "sandbox", "materials", "ai-assistant", "phone",
    "voice", "sms", "text", "chat", "webchat", "referral", "contact",
}

WINDOWS = (("morning", 8, 11, "8–11 am"), ("midday", 11, 14, "11 am–2 pm"), ("afternoon", 14, 17, "2–5 pm"))
BOOKING_DAYS_AHEAD = 10

_OPT_OUT = re.compile(
    r"^\s*(?:please\s+)?(?:stop(?:\s+(?:all|texting|emailing|contacting\s+me))?|unsubscribe|"
    r"remove\s+me|take\s+me\s+off(?:\s+(?:your|the)\s+list)?|opt\s*out|do\s+not\s+contact(?:\s+me)?|"
    r"no\s+thanks?|not\s+interested|cancel|quit|end)\s*[.!]*\s*$", re.I)

_SCHEMA_READY = False


# --- config --------------------------------------------------------------------

def inbound_sources() -> set[str]:
    raw = (os.getenv("INBOUND_SOURCES") or "").strip()
    if raw:
        return {s.strip().lower() for s in raw.split(",") if s.strip()}
    return set(DEFAULT_INBOUND_SOURCES)


def schedule_days() -> list[int]:
    raw = (os.getenv("INBOUND_FOLLOWUP_DAYS") or "1,3,7").strip()
    days = []
    for part in raw.split(","):
        try:
            d = int(part)
            if d > 0:
                days.append(d)
        except ValueError:
            continue
    return sorted(set(days)) or [1, 3, 7]


def disabled() -> bool:
    return (os.getenv("DISABLE_FOLLOWUPS") or "").strip().lower() in ("1", "true", "yes", "on")


# --- schema --------------------------------------------------------------------

def ensure_schema(conn, force: bool = False) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS opted_out_at TIMESTAMP WITH TIME ZONE;")
        cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS walkthrough_at TIMESTAMP WITH TIME ZONE;")
        cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS walkthrough_note TEXT;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lead_followups (
                id SERIAL PRIMARY KEY,
                lead_id INTEGER NOT NULL,
                step INTEGER NOT NULL,
                channel VARCHAR(10) NOT NULL,
                sent_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (lead_id, step, channel)
            );
        """)
    conn.commit()
    _SCHEMA_READY = True


# --- opt-out -------------------------------------------------------------------

def is_opt_out(text: str) -> bool:
    """True when the whole message is an opt-out, not when STOP appears in a sentence."""
    body = (text or "").strip()
    if not body:
        return False
    # Strip quoted reply tails and signatures for email.
    body = re.split(r"\n\s*(?:>|On .* wrote:|-- ?\n|Sent from)", body, maxsplit=1)[0].strip()
    return bool(_OPT_OUT.match(body)) and len(body) <= 60


def mark_opted_out(conn, lead_id: int, channel: str = "", body: str = "") -> bool:
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE leads SET opted_out_at = COALESCE(opted_out_at, CURRENT_TIMESTAMP), "
            "notes = CONCAT_WS(E'\\n', NULLIF(notes, ''), %s) WHERE id = %s;",
            (f"[opt-out via {channel or 'reply'} {_dt.datetime.now(_dt.timezone.utc):%Y-%m-%d}] {body[:120]}", lead_id),
        )
        n = cur.rowcount
    conn.commit()
    return n > 0


def opted_out(conn, lead_id: int) -> bool:
    """Fail closed: an unreadable row counts as opted out."""
    try:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT opted_out_at FROM leads WHERE id = %s;", (lead_id,))
            row = cur.fetchone()
        return bool(row and row["opted_out_at"])
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return True


# --- booking link ----------------------------------------------------------------

def _secret() -> bytes:
    return (os.getenv("SESSION_COOKIE_SECRET") or os.getenv("APP_SECRET") or os.getenv("SECRET_KEY")
            or "dev-insecure-secret").encode()


def booking_token(lead_id: int) -> str:
    sig = hmac.new(_secret(), f"book:{int(lead_id)}".encode(), hashlib.sha256).hexdigest()[:24]
    return base64.urlsafe_b64encode(f"{int(lead_id)}.{sig}".encode()).decode().rstrip("=")


def verify_booking_token(token: str) -> int | None:
    try:
        pad = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + pad).decode()
        lid_s, sig = raw.split(".", 1)
        lid = int(lid_s)
    except Exception:
        return None
    expected = hmac.new(_secret(), f"book:{lid}".encode(), hashlib.sha256).hexdigest()[:24]
    return lid if hmac.compare_digest(sig, expected) else None


def booking_url(lead_id: int) -> str:
    """BOOKING_URL (e.g. a Google appointment page) wins when set; else ours."""
    ext = (os.getenv("BOOKING_URL") or "").strip()
    if ext:
        return ext
    domain = (os.getenv("COMPANY_DOMAIN") or "construction.bizstackperks.com").strip()
    return f"https://{domain}/book/{booking_token(lead_id)}"


# --- slots ---------------------------------------------------------------------

def slots(days_ahead: int = BOOKING_DAYS_AHEAD, now: _dt.datetime | None = None) -> list[dict]:
    """Weekday windows starting tomorrow, in the business time zone."""
    now = (now or _dt.datetime.now(_dt.timezone.utc)).astimezone(LOCAL_TZ)
    out = []
    day = now.date() + _dt.timedelta(days=1)
    while len({s["date"] for s in out}) < days_ahead:
        if day.weekday() < 5:
            for key, start_h, end_h, label in WINDOWS:
                start = _dt.datetime(day.year, day.month, day.day, start_h, tzinfo=LOCAL_TZ)
                out.append({"date": day.isoformat(), "day_label": day.strftime("%a %b %-d"),
                            "window": key, "label": label, "iso": start.isoformat(),
                            "end_iso": start.replace(hour=end_h).isoformat()})
        day += _dt.timedelta(days=1)
    return out


def parse_slot(iso: str) -> _dt.datetime | None:
    try:
        dt = _dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    # Only a window we actually offer, and only in the future.
    local = dt.astimezone(LOCAL_TZ)
    if local.weekday() >= 5 or local.hour not in {w[1] for w in WINDOWS} or local.minute:
        return None
    if local <= _dt.datetime.now(LOCAL_TZ):
        return None
    return dt


def window_label(dt: _dt.datetime) -> str:
    local = dt.astimezone(LOCAL_TZ)
    label = next((w[3] for w in WINDOWS if w[1] == local.hour), local.strftime("%-I %p"))
    return f"{local.strftime('%A, %B %-d')} · {label}"


# --- messages --------------------------------------------------------------------

def _first_name(name: str) -> str:
    raw = (name or "").strip().split()
    if not raw or raw[0].lower() in ("permit", "project", "unknown", "owner", "homeowner"):
        return "there"
    return raw[0]


def followup_email(lead: dict, step: int, link: str) -> tuple[str, str]:
    first = _first_name(lead.get("name"))
    project = (lead.get("project_type") or "your project").strip().lower()
    phone = (os.getenv("COMPANY_PHONE") or "+1 (757) 908-7121").strip()
    co = brand.short_name()
    if step == 0:
        subject = f"Your free walkthrough — pick a time that works"
        body = (f"Hi {first},\n\nThanks again for reaching out to {co} about {project}. "
                f"The fastest next step is a free on-site walkthrough — about 30 minutes, no pressure, "
                f"and you'll have a written fixed-price scope within a day or two after.\n\n"
                f"Pick a time here: {link}\n\nOr just reply to this email, or call/text {phone}.")
    elif step == 1:
        subject = f"Still want a look at {project}?"
        body = (f"Hi {first},\n\nJust checking back — we'd be glad to come out and look at {project}. "
                f"Walkthroughs are free and take about half an hour.\n\n"
                f"Grab a slot: {link}\n\nIf the timing isn't right, reply and tell us when is.")
    else:
        subject = f"Last note from {co}"
        body = (f"Hi {first},\n\nWe don't want to crowd your inbox, so this is the last note we'll send "
                f"about {project} unless you'd like to go ahead.\n\n"
                f"If you'd still like a free walkthrough, pick a time here: {link}\n\n"
                f"Either way, thanks for considering us — call or text {phone} any time.")
    body += f"\n\nBest regards,\nShaun O'Leary\n{brand.name()}\n\nReply STOP to opt out of further messages."
    return subject, body


def followup_sms(lead: dict, step: int, link: str) -> str:
    first = _first_name(lead.get("name"))
    co = brand.short_name()
    if step == 0:
        return f"Hi {first}, it's {co}. Pick a time for your free walkthrough: {link} — or reply here. Reply STOP to opt out."
    if step == 1:
        return f"Hi {first}, {co} checking back — still want us to take a look? Book a time: {link}. Reply STOP to opt out."
    return f"Hi {first}, last note from {co}. If you'd still like a free walkthrough: {link}. Thanks either way. Reply STOP to opt out."


# --- the sweep ---------------------------------------------------------------------

def due_followups(conn, now: _dt.datetime | None = None) -> list[dict]:
    """Inbound leads whose next scheduled touch has come due."""
    ensure_schema(conn)
    now = now or _dt.datetime.now(_dt.timezone.utc)
    days = schedule_days()
    sources = sorted(inbound_sources())
    with conn.cursor() as cur:
        cur.execute(
            """SELECT l.id, l.name, l.email, l.phone, l.project_type, l.source, l.status, l.created_at,
                      COALESCE((SELECT MAX(step) FROM lead_followups f WHERE f.lead_id = l.id), -1) AS last_step
                 FROM leads l
                WHERE l.company = 'construction'
                  AND LOWER(COALESCE(l.source, '')) = ANY(%s)
                  AND l.created_at >= %s
                  AND COALESCE(l.status, 'new') IN ('new', 'contacted')
                  AND l.last_reply_at IS NULL
                  AND l.opted_out_at IS NULL
                  AND l.walkthrough_at IS NULL
                  AND (COALESCE(l.phone, '') <> '' OR (COALESCE(l.email, '') <> '' AND l.email NOT LIKE '%%@lead.local'))
             ORDER BY l.created_at ASC;""",
            (sources, now - _dt.timedelta(days=max(days) + 7)),
        )
        rows = [dict(r) for r in cur.fetchall()]
    due = []
    for r in rows:
        created = r["created_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=_dt.timezone.utc)
        age_days = (now - created).total_seconds() / 86400.0
        next_step = int(r["last_step"]) + 1
        if next_step < len(days) and age_days >= days[next_step]:
            r["step"] = next_step
            due.append(r)
    return due


def _record(conn, lead_id: int, step: int, channel: str, recipient: str, body: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lead_followups (lead_id, step, channel) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING;",
            (lead_id, step, channel),
        )
        cur.execute(
            "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body, lead_id) "
            "VALUES ('outbound', %s, 'bot', %s, %s, %s);",
            ("email" if channel == "email" else "sms", recipient, body[:4000], lead_id),
        )
        cur.execute("UPDATE leads SET status = 'contacted' WHERE id = %s AND COALESCE(status, 'new') = 'new';", (lead_id,))
    conn.commit()


def send_followup(conn, lead: dict, step: int, *, send_email, send_sms, channel_allowed) -> dict:
    """One touch for one lead. Email when there is an email; SMS when there is a
    phone and texting is live. Each channel is capped by auto_reply's daily
    limits. Returns which channels went out."""
    link = booking_url(lead["id"])
    out = {"lead_id": lead["id"], "step": step, "email": False, "sms": False, "why": []}
    email = (lead.get("email") or "").strip()
    if email and not email.lower().endswith("@lead.local"):
        ok, why = channel_allowed("email")
        if ok:
            subject, body = followup_email(lead, step, link)
            try:
                if send_email(email, subject, body):
                    _record(conn, lead["id"], step, "email", email, f"{subject}\n{body}")
                    out["email"] = True
                else:
                    out["why"].append("email transport refused")
            except Exception as exc:
                out["why"].append(f"email: {exc}"[:120])
        else:
            out["why"].append(f"email: {why}")
    phone = re.sub(r"\D", "", lead.get("phone") or "")
    if 10 <= len(phone) <= 11 and send_sms is not None:
        ok, why = channel_allowed("text")
        if ok:
            to = "+" + (phone if len(phone) == 11 else "1" + phone)
            text = followup_sms(lead, step, link)
            try:
                if send_sms(to, text):
                    _record(conn, lead["id"], step, "sms", to, text)
                    out["sms"] = True
                else:
                    out["why"].append("sms transport refused")
            except Exception as exc:
                out["why"].append(f"sms: {exc}"[:120])
        else:
            out["why"].append(f"sms: {why}")
    if not (out["email"] or out["sms"]) and not out["why"]:
        out["why"].append("no reachable channel")
    return out


def run_once(conn, *, send_email, send_sms, channel_allowed, review_mode: bool) -> dict:
    summary = {"due": 0, "sent": 0, "skipped": []}
    if disabled():
        summary["skipped"].append("DISABLE_FOLLOWUPS")
        return summary
    if review_mode:
        summary["skipped"].append("review mode (LEAD_AUTO_SEND unset)")
        return summary
    for lead in due_followups(conn):
        summary["due"] += 1
        res = send_followup(conn, lead, lead["step"], send_email=send_email, send_sms=send_sms,
                            channel_allowed=channel_allowed)
        if res["email"] or res["sms"]:
            summary["sent"] += 1
        elif res["why"]:
            summary["skipped"].append(f"#{lead['id']}: {'; '.join(res['why'])}")
    return summary


# --- booking -------------------------------------------------------------------------

def lead_for_booking(conn, lead_id: int) -> dict | None:
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, email, phone, address, project_type, status, walkthrough_at, opted_out_at "
            "FROM leads WHERE id = %s AND company = 'construction';", (lead_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def book(conn, lead_id: int, when: _dt.datetime, note: str = "", phone: str = "", email: str = "") -> dict:
    """Stamp the walkthrough on the lead and move it forward. Contact details
    the homeowner types on the booking page fill blanks, never overwrite."""
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE leads
                  SET walkthrough_at = %s,
                      walkthrough_note = %s,
                      status = CASE WHEN COALESCE(status,'new') IN ('new','contacted') THEN 'engaged' ELSE status END,
                      phone = CASE WHEN COALESCE(phone,'') = '' THEN %s ELSE phone END,
                      email = CASE WHEN COALESCE(email,'') = '' OR email LIKE '%%@lead.local' THEN %s ELSE email END
                WHERE id = %s
            RETURNING id, name, email, phone, address, project_type;""",
            (when, (note or "")[:1000], (phone or "")[:50], (email or "")[:255], lead_id),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body, lead_id) "
                "VALUES ('inbound', 'booking', %s, 'owner', %s, %s);",
                (row["email"] or row["phone"] or f"lead:{lead_id}",
                 f"Walkthrough booked: {window_label(when)}" + (f" — {note[:200]}" if note else ""), lead_id),
            )
    conn.commit()
    return dict(row) if row else {}


# --- daemon ----------------------------------------------------------------------------

def start(run, interval_seconds: int = 3600) -> threading.Thread | None:
    """`run` is a zero-arg callable that opens a connection and calls run_once."""
    if disabled():
        print("[followups] disabled by DISABLE_FOLLOWUPS", flush=True)
        return None

    def _loop():
        time.sleep(120)
        while True:
            try:
                res = run()
                if res.get("due") or res.get("sent"):
                    print(f"[followups] {res}", flush=True)
            except Exception as exc:
                print(f"[followups] pass failed: {exc}", flush=True)
            time.sleep(interval_seconds)

    t = threading.Thread(target=_loop, name="followups", daemon=True)
    t.start()
    return t
