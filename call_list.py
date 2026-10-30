"""One crew member's call list for one day, opened on a phone.

Why this exists
---------------
The bot now fills the pipeline, writes the first touch and answers replies, so
the only work left that a person has to do is *talk* to people. That work had
no home in the app: the owner was reading a board of 1,200 leads and deciding
by hand who to ring, and a crew member had no way to see the list at all.

A call list is therefore an assignment, not a query. `assign()` freezes a set of
leads to a named crew member for a date, and the page he opens shows exactly
those, with the phone number as a tel: link and a one-tap way to record what
happened. Freezing matters: a live query would change under him as the bot
touches leads, and a call he made yesterday would vanish from today's list.

The page is reached by an HMAC token rather than a login, matching /book/{token}
in followups.py. Ryan is a 1099 laborer with a phone and no email; making him
create an account to see a list of people to ring is how the list never gets
opened.

Nothing here sends anything. Sending the text is the caller's job.
"""

import base64
import datetime as _dt
import hashlib
import hmac
import os
import re

# What a crew member can report. Anything else is refused so the counts stay
# meaningful -- 'called' with no outcome is the state that makes a call log
# useless.
OUTCOMES = {
    "called": "Spoke to them",
    "booked": "Booked a walkthrough",
    "callback": "Asked for a callback",
    "no_answer": "No answer",
    "voicemail": "Left a voicemail",
    "wrong_number": "Wrong number",
    "not_interested": "Not interested",
    "skip": "Skip this one",
}

_SCHEMA_READY = False


def ensure_schema(conn, force: bool = False) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS call_assignments ("
            "  id SERIAL PRIMARY KEY,"
            "  company TEXT NOT NULL DEFAULT 'construction',"
            "  crew_id INTEGER NOT NULL,"
            "  lead_id INTEGER NOT NULL,"
            "  for_date DATE NOT NULL,"
            "  state TEXT NOT NULL DEFAULT 'assigned',"
            "  note TEXT,"
            "  position INTEGER NOT NULL DEFAULT 0,"
            "  created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,"
            "  updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,"
            "  UNIQUE (company, crew_id, for_date, lead_id));"
        )
    conn.commit()
    _SCHEMA_READY = True


# --- the list ---------------------------------------------------------------------

# A lead is worth ringing when there is a number that is not a placeholder and
# the job is not closed. `phone NOT IN (...)` uses lower() because the permit
# imports wrote "Unknown" and "N/A" as well as the empty string.
_CALLABLE = """
    COALESCE(TRIM(l.phone), '') <> ''
    AND LOWER(TRIM(l.phone)) NOT IN ('unknown', 'n/a', 'na', 'none', '-')
    AND LENGTH(REGEXP_REPLACE(l.phone, '[^0-9]', '', 'g')) >= 10
    AND LOWER(COALESCE(l.status, 'new')) NOT IN
        ('won', 'lost', 'closed', 'cancelled', 'canceled', 'archived')
"""


def _priority_sql() -> str:
    """Hottest first, and each row says WHY it is on the list.

    The three reasons are the only ones that change what the call is for:
    a reply that is waiting on us, a brand-new permit nobody has rung, and a
    lead the bot already wrote to that has gone quiet. Sorting by id instead
    would put a 2023 permit above a reply from this morning.
    """
    return f"""
        SELECT l.id, l.name, l.phone, l.address, l.email, l.source, l.status,
               l.project_type, l.created_at,
               (SELECT MAX(c.created_at) FROM comms_logs c
                 WHERE c.lead_id = l.id AND c.direction = 'inbound') AS last_reply,
               (SELECT COUNT(*) FROM comms_logs c
                 WHERE c.lead_id = l.id AND c.direction = 'outbound') AS touches,
               (SELECT MAX(c.created_at) FROM comms_logs c
                 WHERE c.lead_id = l.id AND c.direction = 'outbound') AS last_touch,
               CASE
                 WHEN EXISTS (SELECT 1 FROM comms_logs c
                               WHERE c.lead_id = l.id AND c.direction = 'inbound')
                      THEN 'replied'
                 WHEN NOT EXISTS (SELECT 1 FROM comms_logs c WHERE c.lead_id = l.id)
                      THEN 'new'
                 ELSE 'follow_up'
               END AS reason
        FROM leads l
        WHERE l.company = %(company)s
          AND {_CALLABLE}
          AND NOT EXISTS (
                SELECT 1 FROM call_assignments a
                 WHERE a.company = %(company)s AND a.lead_id = l.id
                   AND a.for_date = %(for_date)s)
        ORDER BY
          CASE
            WHEN EXISTS (SELECT 1 FROM comms_logs c
                          WHERE c.lead_id = l.id AND c.direction = 'inbound') THEN 0
            WHEN NOT EXISTS (SELECT 1 FROM comms_logs c WHERE c.lead_id = l.id) THEN 1
            ELSE 2
          END,
          l.id DESC
        LIMIT %(limit)s
    """


def candidates(conn, company: str = "construction", limit: int = 25,
               for_date=None) -> list[dict]:
    """Leads worth ringing that are not already on that day's list."""
    ensure_schema(conn)
    day = for_date or _dt.date.today()
    with conn.cursor() as cur:
        cur.execute(_priority_sql(), {"company": company, "for_date": day,
                                      "limit": max(1, int(limit))})
        rows = cur.fetchall() or []
    out = []
    for row in rows:
        ld = dict(row) if isinstance(row, dict) else dict(row)
        ld["reason_text"] = {
            "replied": "They replied - answer them",
            "new": "New lead, nobody has called yet",
            "follow_up": "Bot wrote to them, no answer yet",
        }.get(ld.get("reason"), "")
        out.append(ld)
    return out


def assign(conn, crew_id: int, company: str = "construction", limit: int = 25,
           for_date=None) -> dict:
    """Freeze today's (or tomorrow's) list to one crew member. Idempotent.

    Re-running does not duplicate rows and does not silently reshuffle an
    existing list -- the UNIQUE constraint and the NOT EXISTS above mean a
    second call only tops the list up to `limit`.
    """
    ensure_schema(conn)
    day = for_date or _dt.date.today()
    picks = candidates(conn, company, limit, day)
    added = 0
    with conn.cursor() as cur:
        for i, ld in enumerate(picks):
            cur.execute(
                "INSERT INTO call_assignments (company, crew_id, lead_id, for_date, position) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (company, crew_id, for_date, lead_id) DO NOTHING;",
                (company, int(crew_id), int(ld["id"]), day, i),
            )
            added += cur.rowcount or 0
    conn.commit()
    return {"date": day.isoformat(), "crew_id": int(crew_id), "added": added,
            "total": len(list_for(conn, crew_id, company, day))}


def list_for(conn, crew_id: int, company: str = "construction", for_date=None) -> list[dict]:
    """The frozen list, in the order it was assigned."""
    ensure_schema(conn)
    day = for_date or _dt.date.today()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.id AS assignment_id, a.state, a.note, a.position,"
            "       l.id AS lead_id, l.name, l.phone, l.address, l.email, l.source,"
            "       l.status, l.project_type"
            "  FROM call_assignments a JOIN leads l ON l.id = a.lead_id"
            " WHERE a.company = %s AND a.crew_id = %s AND a.for_date = %s"
            " ORDER BY a.position, a.id;",
            (company, int(crew_id), day),
        )
        rows = cur.fetchall() or []
    out = []
    for row in rows:
        d = dict(row)
        d["dial"] = re.sub(r"[^0-9+]", "", str(d.get("phone") or ""))
        out.append(d)
    return out


def mark(conn, assignment_id: int, state: str, note: str = "") -> dict:
    """Record what happened on one call. Refuses an outcome that is not real."""
    ensure_schema(conn)
    if state not in OUTCOMES:
        return {"ok": False, "error": f"unknown outcome {state!r}"}
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE call_assignments SET state = %s, note = %s, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = %s RETURNING lead_id, crew_id, for_date;",
            (state, note[:500], int(assignment_id)),
        )
        row = cur.fetchone()
    if not row:
        conn.rollback()
        return {"ok": False, "error": "that call is not on the list"}
    conn.commit()
    lead_id = row["lead_id"] if isinstance(row, dict) else row[0]
    # A call is a real touch: it belongs in comms_logs next to the emails and
    # texts, so the contact policy and the lead's history see it. The call
    # itself is not automated, so this records rather than sends.
    try:
        import auto_reply
        auto_reply.record_manual_touch(conn, int(lead_id), "phone",
                                       f"{OUTCOMES[state]}{(': ' + note) if note else ''}")
    except Exception:
        pass
    return {"ok": True, "lead_id": int(lead_id), "state": state}


def progress(conn, crew_id: int, company: str = "construction", for_date=None) -> dict:
    day = for_date or _dt.date.today()
    rows = list_for(conn, crew_id, company, day)
    done = [r for r in rows if r["state"] not in ("assigned", "skip")]
    return {
        "total": len(rows),
        "done": len(done),
        "left": len(rows) - len(done),
        "booked": sum(1 for r in rows if r["state"] == "booked"),
    }


# --- the link ---------------------------------------------------------------------

def _secret() -> bytes:
    return (os.getenv("SESSION_COOKIE_SECRET") or os.getenv("APP_SECRET")
            or os.getenv("SECRET_KEY") or "dev-insecure-secret").encode()


def call_token(crew_id: int, for_date=None) -> str:
    """`crew_id|YYYY-MM-DD` + HMAC. A token for one day cannot read another."""
    day = (for_date or _dt.date.today()).isoformat()
    payload = f"{int(crew_id)}|{day}"
    sig = hmac.new(_secret(), f"calllist:{payload}".encode(), hashlib.sha256).hexdigest()[:24]
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode().rstrip("=")


def verify_call_token(token: str):
    """-> (crew_id, date) or None."""
    try:
        pad = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(str(token) + pad).decode()
        crew_s, day_s, sig = raw.split("|", 2)
        crew_id = int(crew_s)
        day = _dt.date.fromisoformat(day_s)
    except Exception:
        return None
    payload = f"{crew_id}|{day.isoformat()}"
    expected = hmac.new(_secret(), f"calllist:{payload}".encode(), hashlib.sha256).hexdigest()[:24]
    return (crew_id, day) if hmac.compare_digest(sig, expected) else None


def call_url(crew_id: int, for_date=None) -> str:
    domain = (os.getenv("COMPANY_DOMAIN") or "construction.bizstackperks.com").strip()
    return f"https://{domain}/calllist/{call_token(crew_id, for_date)}"


def sms_text(crew_name: str, crew_id: int, count: int, for_date=None) -> str:
    """The message that actually gets a list opened: short, dated, one link."""
    day = for_date or _dt.date.today()
    when = "today" if day == _dt.date.today() else day.strftime("%a %b %-d")
    first = (crew_name or "").split()[0] or "there"
    return (f"{first}, your {when} call list is ready - {count} people to ring, "
            f"hottest first. Tap to call and log each one: {call_url(crew_id, day)}")


# --- the daily plan -----------------------------------------------------------------

def crew_ids_from_env() -> list:
    """CALL_LIST_CREW_IDS=7,9 -> [7, 9]. Empty by default: nobody is auto-texted
    until the owner names who should be, which is the difference between a
    useful nudge and ten people getting a text they never asked for."""
    raw = os.getenv("CALL_LIST_CREW_IDS") or ""
    out = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.append(int(chunk))
    return out


def auto_plan(conn, company: str = "construction", limit: int = 20,
              for_date=None, text: bool = True) -> dict:
    """Build tomorrow's list for each configured crew member and text the link.

    Runs unattended, so every failure is reported per person rather than raised:
    one crew member with a dead number must not stop the rest of the plan.
    """
    day = for_date or (_dt.date.today() + _dt.timedelta(days=1))
    ids = crew_ids_from_env()
    result = {"date": day.isoformat(), "planned": [], "errors": []}
    if not ids:
        result["skipped"] = "CALL_LIST_CREW_IDS is not set"
        return result
    sender = None
    if text:
        try:
            from signalwire_service import SignalWireService
            sender = SignalWireService()
            if not sender.is_configured():
                sender = None
                result["errors"].append("SMS is not configured; links were built but not sent")
        except Exception as exc:
            result["errors"].append(f"SMS unavailable: {type(exc).__name__}: {exc}")
    for cid in ids:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, name, phone FROM crew WHERE id = %s AND is_active;", (cid,))
                crew = cur.fetchone()
            if not crew:
                result["errors"].append(f"crew {cid} is not an active crew member")
                continue
            res = assign(conn, cid, company, limit, day)
            entry = {"crew_id": cid, "name": crew.get("name"), "count": res["total"],
                     "url": call_url(cid, day), "texted": False}
            phone = (crew.get("phone") or "").strip()
            if sender and phone and res["total"]:
                try:
                    entry["texted"] = bool(sender.send_sms(
                        phone, sms_text(crew.get("name") or "", cid, res["total"], day)))
                except Exception as exc:
                    result["errors"].append(f"text to {crew.get('name')}: {exc}")
            result["planned"].append(entry)
        except Exception as exc:
            result["errors"].append(f"crew {cid}: {type(exc).__name__}: {exc}")
            try:
                conn.rollback()
            except Exception:
                pass
    return result
