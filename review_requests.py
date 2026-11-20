"""Ask a finished customer for a review, once.

Reviews are the cheapest lead source a contractor has, and until now nothing sent
the ask — `marketing/review_automation.py` was a stub with a placeholder link and
no caller. This module is the real one: it looks for leads that reached
`completed` or `won`, sends one request per customer, and never repeats.

The link must be the business's own Google review URL. Without it a "leave a
review" email lands on a search page and gets ignored, so if the env var is
missing we fall back to the search link but mark it in the log — and the owner
page says so.
"""

from __future__ import annotations

import os

REVIEW_URL_ENV = "GOOGLE_REVIEW_URL"
FALLBACK = "https://www.google.com/search?q=BuildStack+Construction+LLC+reviews"


def review_url() -> tuple[str, bool]:
    """(url, configured). False means we are asking people to hunt for the page."""
    url = (os.getenv(REVIEW_URL_ENV) or "").strip()
    if url.startswith("http"):
        return url, True
    return FALLBACK, False


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS review_requests ("
            " lead_id INTEGER PRIMARY KEY,"
            " channel TEXT NOT NULL,"
            " recipient TEXT,"
            " sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " message TEXT);"
        )
    conn.commit()


def due(conn, limit: int = 25) -> list[dict]:
    """Completed customers who have never been asked."""
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT l.id, l.name, l.email, l.phone, l.project_type, l.status
                 FROM leads l
                 LEFT JOIN review_requests r ON r.lead_id = l.id
                WHERE l.company = 'construction'
                  AND COALESCE(l.status, '') IN ('completed', 'won')
                  AND r.lead_id IS NULL
                  AND l.opted_out_at IS NULL
                  AND COALESCE(l.do_not_contact, false) = false
                  AND (COALESCE(l.email, '') <> '' OR COALESCE(l.phone, '') <> '')
                  AND l.created_at >= NOW() - INTERVAL '180 days'
                ORDER BY l.id DESC
                LIMIT %s;""",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def _first_name(name: str) -> str:
    return (str(name or "").strip().split(" ")[0]) or "there"


def email_body(lead: dict) -> tuple[str, str]:
    url, _ = review_url()
    first = _first_name(lead.get("name"))
    subject = f"Quick favour, {first}?"
    body = (
        f"Hi {first},\n\n"
        f"Thank you for trusting us with your {lead.get('project_type') or 'project'}. "
        f"If you have thirty seconds, a short Google review genuinely helps another "
        f"homeowner in Hampton Roads decide whether to call us.\n\n"
        f"{url}\n\n"
        f"If anything is not right with the work, reply to this email instead and I "
        f"will come look at it myself.\n\n"
        f"— Shaun O'Leary\n"
        f"BuildStack Construction LLC\n"
        f"(757) 846-9275"
    )
    return subject, body


def sms_body(lead: dict) -> str:
    url, _ = review_url()
    return (
        f"Hi {_first_name(lead.get('name'))}, Shaun at BuildStack. If you have a "
        f"minute, a short Google review really helps: {url} "
        f"Reply STOP to opt out."
    )


def _record(conn, lead_id: int, channel: str, recipient: str, message: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO review_requests (lead_id, channel, recipient, message) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (lead_id) DO NOTHING;",
            (lead_id, channel, recipient, message[:2000]),
        )
    conn.commit()


def run_once(conn, *, send_email, send_sms, channel_allowed) -> dict:
    """One pass. `send_email(to, subject, body) -> bool`, `send_sms(to, body) -> bool`."""
    summary = {"due": 0, "sent": 0, "skipped": []}
    for lead in due(conn):
        summary["due"] += 1
        email = (lead.get("email") or "").strip()
        phone = "".join(ch for ch in str(lead.get("phone") or "") if ch.isdigit())

        sent_channel = None
        if email and not email.lower().endswith("@lead.local"):
            ok, why = channel_allowed("email")
            if ok:
                subject, body = email_body(lead)
                try:
                    if send_email(email, subject, body):
                        _record(conn, lead["id"], "email", email, f"{subject}\n\n{body}")
                        sent_channel = "email"
                    else:
                        summary["skipped"].append(f"#{lead['id']}: email transport refused")
                except Exception as exc:  # noqa: BLE001
                    summary["skipped"].append(f"#{lead['id']}: email {exc}"[:160])
            else:
                summary["skipped"].append(f"#{lead['id']}: email {why}")

        if not sent_channel and len(phone) >= 10 and send_sms is not None:
            ok, why = channel_allowed("text")
            if ok:
                to = "+" + (phone if len(phone) == 11 else "1" + phone)
                try:
                    if send_sms(to, sms_body(lead)):
                        _record(conn, lead["id"], "sms", to, sms_body(lead))
                        sent_channel = "sms"
                    else:
                        summary["skipped"].append(f"#{lead['id']}: sms transport refused")
                except Exception as exc:  # noqa: BLE001
                    summary["skipped"].append(f"#{lead['id']}: sms {exc}"[:160])
            else:
                summary["skipped"].append(f"#{lead['id']}: text {why}")

        if sent_channel:
            summary["sent"] += 1

    return summary
