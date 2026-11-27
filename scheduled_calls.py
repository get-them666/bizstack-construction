"""Durable scheduled AI calls.

`_follow_up_new_lead` fires once, immediately, from a thread. That is fine at the
moment a lead lands at 10am and useless for anything that has to happen later —
a call at 8am, a retry tomorrow, a second attempt after a no-answer. A thread
also dies with the container, so a redeploy silently cancels every pending call.

This table plus the loop in lifecycle() is the durable version: the time is
written to Postgres, and the loop picks it up whenever the app is running. A
restart delays a call by at most one tick instead of losing it.
"""

from __future__ import annotations

import datetime as dt
import os
import urllib.parse
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

CALL_TZ = ZoneInfo("America/New_York")
CALL_START_HOUR = 8   # a promised "we'll call you in the morning" is 8, not 9
CALL_END_HOUR = 19


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS scheduled_calls ("
            " id SERIAL PRIMARY KEY,"
            " lead_id INTEGER NOT NULL,"
            " due_at TIMESTAMPTZ NOT NULL,"
            " reason TEXT,"
            " status TEXT NOT NULL DEFAULT 'pending',"
            " call_sid TEXT,"
            " note TEXT,"
            " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " done_at TIMESTAMPTZ);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_calls_pending "
            "ON scheduled_calls (due_at) WHERE status = 'pending';"
        )
        # The dial guard reads this before every call; create it here so the
        # loop cannot fail on a fresh database before a lead has ever been dialled.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS bot_calls ("
            " call_sid TEXT PRIMARY KEY, lead_id INTEGER, direction TEXT, status TEXT,"
            " turns INTEGER DEFAULT 0, created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);"
        )
    conn.commit()


def schedule(conn, lead_id: int, due_at: dt.datetime, reason: str = "") -> int:
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO scheduled_calls (lead_id, due_at, reason) "
            "VALUES (%s, %s, %s) RETURNING id;",
            (lead_id, due_at, reason[:200]),
        )
        row_id = cur.fetchone()["id"]
    conn.commit()
    return row_id


def _finish(conn, row_id: int, status: str, note: str = "", sid: str = "") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE scheduled_calls SET status = %s, note = %s, call_sid = %s, done_at = NOW() "
            "WHERE id = %s;",
            (status, note[:400], sid or None, row_id),
        )
    conn.commit()


def _app_base() -> str:
    base = (os.getenv("APP_BASE_URL") or "").strip().rstrip("/")
    if base:
        return base
    domain = (os.getenv("COMPANY_DOMAIN") or "construction.bizstackperks.com").strip()
    return f"https://{domain}"


def _digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def run_due(conn) -> dict:
    """Dial every pending call whose time has come. Safe to run every minute."""
    ensure_schema(conn)
    now_local = dt.datetime.now(CALL_TZ)
    if not (CALL_START_HOUR <= now_local.hour < CALL_END_HOUR):
        return {"due": 0, "dialed": 0, "skipped": ["outside calling hours"]}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, lead_id, due_at, reason FROM scheduled_calls "
            "WHERE status = 'pending' AND due_at <= NOW() ORDER BY due_at LIMIT 10;"
        )
        rows = [dict(r) for r in cur.fetchall()]

    import signalwire_service
    import suppression
    import outbound_campaign

    signalwire = signalwire_service.SignalWireService()

    summary = {"due": len(rows), "dialed": 0, "skipped": []}
    for row in rows:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, phone, status, opted_out_at, source FROM leads "
                        "WHERE id = %s AND company = 'construction';", (row["lead_id"],))
            lead = cur.fetchone()

        if not lead:
            _finish(conn, row["id"], "skipped", "lead no longer exists")
            summary["skipped"].append(f"#{row['lead_id']}: gone")
            continue
        if (lead.get("status") or "new") not in ("new", "contacted"):
            _finish(conn, row["id"], "skipped", f"status {lead.get('status')}")
            summary["skipped"].append(f"#{row['lead_id']}: status {lead.get('status')}")
            continue
        if lead.get("opted_out_at"):
            _finish(conn, row["id"], "skipped", "opted out")
            summary["skipped"].append(f"#{row['lead_id']}: opted out")
            continue

        digits = _digits(lead.get("phone"))
        if len(digits) < 10:
            _finish(conn, row["id"], "skipped", "no usable phone")
            summary["skipped"].append(f"#{row['lead_id']}: no phone")
            continue
        to = ("+" + digits) if digits.startswith("1") else ("+1" + digits)

        if not outbound_campaign.is_inbound_source(lead.get("source")) \
                and not outbound_campaign.in_calling_area(to):
            _finish(conn, row["id"], "skipped", "outside the calling area")
            summary["skipped"].append(f"#{row['lead_id']}: outside the calling area")
            continue

        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM bot_calls WHERE lead_id = %s LIMIT 1;", (row["lead_id"],))
            if cur.fetchone():
                _finish(conn, row["id"], "skipped", "already dialled once")
                summary["skipped"].append(f"#{row['lead_id']}: already dialled")
                continue

        try:
            allowed, why = suppression.check_outbound(conn, suppression.CHANNEL_VOICE, to)
        except Exception as exc:  # noqa: BLE001 - never dial if the check fails
            _finish(conn, row["id"], "error", f"suppression check: {exc}"[:300])
            summary["skipped"].append(f"#{row['lead_id']}: suppression check failed")
            continue
        if not allowed:
            _finish(conn, row["id"], "skipped", why or "suppressed")
            summary["skipped"].append(f"#{row['lead_id']}: {why}")
            continue

        if not signalwire.is_configured():
            _finish(conn, row["id"], "error", "SignalWire not configured")
            summary["skipped"].append("SignalWire not configured")
            continue

        swml_url = (f"{_app_base()}/comms/outbound-voice.swml"
                    f"?to={urllib.parse.quote(to)}&lead_id={row['lead_id']}")
        try:
            sid = signalwire.create_ai_outbound_call(to, swml_url)
        except Exception as exc:  # noqa: BLE001 - record and move on
            _finish(conn, row["id"], "error", str(exc)[:300])
            summary["skipped"].append(f"#{row['lead_id']}: dial failed")
            continue

        if not sid:
            _finish(conn, row["id"], "error", "carrier did not accept the call")
            summary["skipped"].append(f"#{row['lead_id']}: carrier refused")
            continue

        with conn.cursor() as cur:
            cur.execute("INSERT INTO bot_calls (call_sid, lead_id, direction, status) "
                        "VALUES (%s, %s, 'outbound', 'dialed') ON CONFLICT (call_sid) DO NOTHING;",
                        (sid, row["lead_id"]))
            cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
                        "VALUES ('outbound', 'voice', 'system', %s, %s);",
                        (to, f"Scheduled AI call ({row.get('reason') or 'follow-up'}) for lead {row['lead_id']}"))
        conn.commit()
        _finish(conn, row["id"], "dialed", "", sid)
        summary["dialed"] += 1
        print(f"[scheduled-call] dialed lead {row['lead_id']} ({to}) sid={sid}", flush=True)

    return summary
