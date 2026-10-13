"""Database access for the shared lead pipeline.

Everything takes an explicit connection and a ``company`` where one applies, and
nothing imports an app or a web framework, so the same code answers the same
questions for Broom and Buildstack.

The one rule that keeps the denormalised queue state honest: **only**
:func:`recompute` writes ``next_action``/``next_action_at``/``last_*``/
``touch_count``. Every mutation funnels through it, so the cached columns can
never drift from the lifecycle rules in :mod:`bizstack_leads.lifecycle`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import lifecycle

# Fields callers may set on a lead. Filtered against the real table before use,
# so a column that only exists in one app's schema (``campaign``, ``listing_url``)
# is simply ignored on the side that lacks it.
WRITABLE_FIELDS = (
    "name", "phone", "email", "address", "description", "project_type",
    "source", "source_ref", "company", "status", "notes", "draft_reply",
    "campaign", "listing_url", "do_not_contact",
    # Business/estimate extras some apps set at intake. Filtered against the
    # real table before use, so a column one app's schema lacks is simply
    # ignored rather than failing the insert.
    "budget", "timeline", "referral_code", "partner_id", "created_at",
    "funding_needed", "funding_use", "funding_amount_cents", "analysis_json",
    "property_sqft", "estimate_low_cents", "estimate_high_cents", "estimate_json",
)


def _columns(cur, table: str) -> set[str]:
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s;",
        (table,),
    )
    return {r["column_name"] for r in cur.fetchall()}


def get_lead(db, lead_id, company=None):
    where, params = ["id = %s"], [lead_id]
    if company:
        where.append("company = %s")
        params.append(company)
    with db.cursor() as cur:
        cur.execute(f"SELECT * FROM leads WHERE {' AND '.join(where)};", tuple(params))
        return cur.fetchone()


def find_duplicate(db, company, *, source_ref=None, email=None, phone=None,
                   name=None, address=None):
    """Return an existing lead this intake matches, or ``None``.

    Match order, strongest first:

    1. ``source_ref`` -- the same permit/reddit post seen twice. Exact by design.
    2. ``email`` / ``phone`` -- the same human from two sources.
    3. ``(name, address)`` -- the same person at the same property, where a form
       filled in a phone one time and not the other.

    Scoped to ``company`` because the two businesses share one table and a
    contractor and a cleaner are different leads even at one address.
    """
    company = company or ""
    checks = []
    params = []
    if source_ref:
        checks.append("source_ref = %s")
        params.append(source_ref)
    for col, val in (("email", email), ("phone", phone)):
        if (val or "").strip():
            checks.append(f"LOWER({col}) = LOWER(%s)")
            params.append(val.strip())
    if (name or "").strip() and (address or "").strip():
        checks.append("(LOWER(name) = LOWER(%s) AND LOWER(address) = LOWER(%s))")
        params += [name.strip(), address.strip()]
    if not checks:
        return None
    with db.cursor() as cur:
        cur.execute(
            f"SELECT * FROM leads WHERE company = %s AND ({' OR '.join(checks)}) "
            "ORDER BY created_at ASC LIMIT 1;",
            (company, *params),
        )
        return cur.fetchone()


def insert_lead(db, fields: dict):
    with db.cursor() as cur:
        allowed = _columns(cur, "leads")
        data = {k: v for k, v in fields.items() if k in allowed and k in WRITABLE_FIELDS}
        if "name" not in data:
            data["name"] = ""
        cols_sql = ", ".join(data)
        placeholders = ", ".join(["%s"] * len(data))
        cur.execute(
            f"INSERT INTO leads ({cols_sql}) VALUES ({placeholders}) RETURNING id;",
            tuple(data.values()),
        )
        lead_id = cur.fetchone()["id"]
        db.commit()
    return lead_id


def update_lead_fields(db, lead_id, fields: dict) -> None:
    with db.cursor() as cur:
        allowed = _columns(cur, "leads")
        data = {k: v for k, v in fields.items() if k in allowed and k in WRITABLE_FIELDS}
        if not data:
            return
        assignments = ", ".join(f"{k} = %s" for k in data)
        cur.execute(
            f"UPDATE leads SET {assignments} WHERE id = %s;",
            tuple(data.values()) + (lead_id,),
        )
        db.commit()


def _touch_stats(db, lead_id):
    with db.cursor() as cur:
        cur.execute(
            "SELECT "
            "  MAX(created_at) FILTER (WHERE direction = 'outbound') AS last_out, "
            "  MAX(created_at) FILTER (WHERE direction = 'inbound') AS last_in, "
            "  COUNT(*) FILTER (WHERE direction = 'outbound') AS out_count "
            "FROM comms_logs WHERE lead_id = %s;",
            (lead_id,),
        )
        return cur.fetchone()


def recompute(db, lead_id, now=None) -> dict:
    """Re-derive and persist a lead's queue state. The single writer.

    Normalises a legacy ``status`` onto the canonical stage at the same time --
    ``analyzed`` becomes ``new``, ``in_progress`` becomes ``won``. Fine-grained
    business state (a project's own status) lives in its own table, not here.
    """
    now = now or datetime.now(timezone.utc)
    lead = get_lead(db, lead_id)
    if not lead:
        return {}
    stats = _touch_stats(db, lead_id) or {}
    lead = dict(lead)
    lead["last_outbound_at"] = stats.get("last_out")
    lead["last_inbound_at"] = stats.get("last_in")
    lead["touch_count"] = stats.get("out_count") or 0

    stage = lifecycle.to_stage(lead.get("status"))
    # A recorded outbound touch is itself proof the lead has been contacted.
    # Without this a send that advanced only comms_logs would leave the lead
    # sitting in "needs first touch", and the sweep would contact them again.
    if stage == lifecycle.NEW and lead["touch_count"]:
        stage = lifecycle.CONTACTED
    lead["status"] = stage
    action, due = lifecycle.next_action(lead, now=now)

    with db.cursor() as cur:
        cur.execute(
            "UPDATE leads SET status = %s, next_action = %s, next_action_at = %s, "
            "last_outbound_at = %s, last_inbound_at = %s, touch_count = %s "
            "WHERE id = %s;",
            (stage, action, due, lead["last_outbound_at"], lead["last_inbound_at"],
             lead["touch_count"], lead_id),
        )
        db.commit()

    lead["status"] = stage
    lead["next_action"] = action
    lead["next_action_at"] = due
    return lead


def backfill(db, company=None, batch=500) -> int:
    """Recompute every lead that has never been through the pipeline.

    Existing rows predate the queue columns, so their ``next_action`` is NULL and
    the SQL-backed counts would under-report them. This walks only the rows that
    still need it, in batches, and is a no-op once everything is tagged. Run it
    once at boot, after :func:`ensure_schema`.

    It also normalises each row's legacy ``status`` onto a canonical stage, which
    is the one-time migration both apps need.
    """
    done = 0
    while True:
        where, params = ["next_action IS NULL"], []
        if company:
            where.append("company = %s")
            params.append(company)
        with db.cursor() as cur:
            cur.execute(
                f"SELECT id FROM leads WHERE {' AND '.join(where)} LIMIT %s;",
                (*params, batch),
            )
            ids = [r["id"] for r in cur.fetchall()]
        if not ids:
            break
        for lead_id in ids:
            recompute(db, lead_id)
        done += len(ids)
    return done


def record_inbound(db, lead_id, channel, message, sender="") -> None:
    """Log an inbound message and move the lead to ``engaged``.

    This is the one place an inbound reply is acknowledged, so the queue flips
    "contacted" to "replied -- respond" without each app re-implementing it.
    """
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body, lead_id) "
            "VALUES ('inbound', %s, %s, '', %s, %s);",
            (channel, sender or "lead", (message or "")[:2000], lead_id),
        )
        db.commit()
    lead = get_lead(db, lead_id)
    if lead and lifecycle.to_stage(lead.get("status")) not in lifecycle.CLOSED_STAGES:
        with db.cursor() as cur:
            cur.execute("UPDATE leads SET status = %s WHERE id = %s;", (lifecycle.ENGAGED, lead_id))
            db.commit()
    recompute(db, lead_id)


def record_touch(db, lead_id, channel, detail="") -> dict:
    """Owner-initiated contact (letter, door knock, phone, sms, in person).

    Supersedes ``auto_reply.record_manual_touch``: same effect, but this lives in
    the shared package so both apps log a manual touch the same way and the
    contact policy sees it either way.
    """
    channel = (channel or "").strip().lower()
    if channel == "text":
        channel = "sms"
    allowed = {"letter", "door-knock", "phone", "sms", "in-person", "other"}
    if channel not in allowed:
        return {"ok": False, "error": f"channel must be one of {sorted(allowed)}"}
    if not lead_id or db is None:
        return {"ok": False, "error": "lead_id and db are required"}

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    note = (detail or "").strip()
    body = f"[owner {channel} {stamp}]" + (f" {note}" if note else "")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body, lead_id) "
            "VALUES ('outbound', %s, 'owner', %s, %s, %s);",
            (channel, f"lead:{lead_id}", body, lead_id),
        )
        cur.execute(
            "UPDATE leads SET notes = CASE WHEN COALESCE(notes, '') = '' THEN %s "
            "ELSE notes || chr(10) || %s END WHERE id = %s;",
            (body, body, lead_id),
        )
        db.commit()
    lead = get_lead(db, lead_id)
    if lead and lifecycle.to_stage(lead.get("status")) == lifecycle.NEW:
        with db.cursor() as cur:
            cur.execute("UPDATE leads SET status = %s WHERE id = %s;", (lifecycle.CONTACTED, lead_id))
            db.commit()
    recompute(db, lead_id)
    return {"ok": True, "channel": channel, "lead_id": lead_id, "at": stamp}


def set_stage(db, lead_id, stage) -> bool:
    stage = lifecycle.to_stage(stage)
    if stage not in lifecycle.STAGES:
        raise ValueError(f"unknown stage: {stage!r}")
    with db.cursor() as cur:
        cur.execute("UPDATE leads SET status = %s WHERE id = %s RETURNING id;", (stage, lead_id))
        row = cur.fetchone()
        db.commit()
    if row:
        recompute(db, lead_id)
    return bool(row)


def set_notes(db, lead_id, notes) -> bool:
    with db.cursor() as cur:
        cur.execute("UPDATE leads SET notes = %s WHERE id = %s RETURNING id;", (notes, lead_id))
        row = cur.fetchone()
        db.commit()
    return bool(row)


def set_do_not_contact(db, lead_id, flag=True) -> bool:
    with db.cursor() as cur:
        cur.execute(
            "UPDATE leads SET do_not_contact = %s WHERE id = %s RETURNING id;",
            (bool(flag), lead_id),
        )
        row = cur.fetchone()
        db.commit()
    if row:
        recompute(db, lead_id)
    return bool(row)


def stage_draft(db, lead_id, subject, body) -> bool:
    """Store a ready-to-send draft. Staging never sends.

    Kept as ``"subject\\nbody"`` so a sender recovers both by splitting on the
    first newline.
    """
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not subject or not body:
        raise ValueError("subject and body are both required")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE leads SET draft_reply = %s WHERE id = %s RETURNING id;",
            (f"{subject}\n{body}", lead_id),
        )
        row = cur.fetchone()
        db.commit()
    return bool(row)


def clear_draft(db, lead_id) -> bool:
    with db.cursor() as cur:
        cur.execute("UPDATE leads SET draft_reply = NULL WHERE id = %s RETURNING id;", (lead_id,))
        row = cur.fetchone()
        db.commit()
    return bool(row)


def list_leads(db, company=None, *, action=None, stage=None, q="", limit=500,
               include_closed=False, where_extra="", extra_params=()):
    """Open leads, logically first.

    ``where_extra`` / ``extra_params`` let an app layer its own policy (source
    visibility, a campaign, a partner scope) onto the query without the package
    having to know about it. ``where_extra`` is a bare SQL condition, ANDed on.
    """
    where, params = [], []
    if company:
        where.append("company = %s")
        params.append(company)
    if action:
        where.append("next_action = %s")
        params.append(action)
    if stage:
        where.append("status = %s")
        params.append(stage)
    elif not include_closed:
        where.append("status <> ALL(%s)")
        params.append(list(lifecycle.CLOSED_STAGES))
    term = (q or "").strip()
    if term:
        like = f"%{term}%"
        where.append(
            "(name ILIKE %s OR phone ILIKE %s OR email ILIKE %s "
            "OR address ILIKE %s)"
        )
        params += [like, like, like, like]
    if where_extra:
        where.append(where_extra)
        params += list(extra_params)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with db.cursor() as cur:
        cur.execute(
            f"SELECT * FROM leads {clause} "
            "ORDER BY next_action_at ASC NULLS LAST, created_at DESC LIMIT %s;",
            tuple(params) + (limit,),
        )
        return cur.fetchall()


def queue_counts(db, company=None) -> dict:
    """How many leads sit in each next-action bucket."""
    where, params = [], []
    if company:
        where.append("company = %s")
        params.append(company)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with db.cursor() as cur:
        cur.execute(
            f"SELECT next_action, COUNT(*)::int AS c FROM leads {clause} "
            "GROUP BY next_action;",
            tuple(params),
        )
        return {r["next_action"]: r["c"] for r in cur.fetchall()}


def stage_counts(db, company=None) -> dict:
    where, params = [], []
    if company:
        where.append("company = %s")
        params.append(company)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with db.cursor() as cur:
        cur.execute(
            f"SELECT status, COUNT(*)::int AS c FROM leads {clause} GROUP BY status;",
            tuple(params),
        )
        return {r["status"]: r["c"] for r in cur.fetchall()}


def draft_count(db, company=None, *, where_extra="", extra_params=()) -> int:
    where, params = [], []
    if company:
        where.append("company = %s")
        params.append(company)
    where.append("draft_reply IS NOT NULL AND LOWER(COALESCE(draft_reply, '')) <> ''")
    if where_extra:
        where.append(where_extra)
        params += list(extra_params)
    with db.cursor() as cur:
        cur.execute(f"SELECT COUNT(*)::int AS c FROM leads WHERE {' AND '.join(where)};", tuple(params))
        return cur.fetchone()["c"]


def activity(db, lead, limit=40):
    """Recent comms for a lead.

    Matched by ``lead_id`` first, then by the lead's email/phone, because rows
    logged before ``comms_logs.lead_id`` existed are still part of the story.
    """
    email = (lead.get("email") or "").strip()
    phone = (lead.get("phone") or "").strip()
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, direction, channel, sender, recipient, message_body, created_at "
            "FROM comms_logs WHERE lead_id = %s "
            "OR (%s <> '' AND (LOWER(recipient) = LOWER(%s) OR LOWER(sender) = LOWER(%s))) "
            "OR (%s <> '' AND (LOWER(recipient) = LOWER(%s) OR LOWER(sender) = LOWER(%s))) "
            "ORDER BY created_at DESC LIMIT %s;",
            (lead["id"], email, email, email, phone, phone, phone, limit),
        )
        return cur.fetchall()


def lead_payments(db, lead_id, limit=20):
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, amount_cents, currency, status, created_at "
            "FROM payments WHERE lead_id = %s ORDER BY created_at DESC LIMIT %s;",
            (lead_id, limit),
        )
        return cur.fetchall()
