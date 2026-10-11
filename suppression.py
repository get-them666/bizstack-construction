"""Suppression list + the single gate every outbound contact must pass.

Why this module exists
----------------------
The app had guardrails, but they were scattered and one send path had none:
``construction_main.py:1088`` calls ``signalwire.send_sms(to, body)`` straight
from a Copilot tool handler with no rate limit, no dedupe, no opt-out check and
no registry scrub. ``send_email_message`` had the same shape until 2026-10-03,
when it looped and sent three identical emails before being pulled.

The fix is not another per-path check. It is one choke point, so that adding a
send path without adding a guard fails visibly in review instead of silently
in production. Everything that decides "may we contact this person" lives here.

Two layers, deliberately separate:

``suppression_list``
    *Who must never be contacted at all.* DNC (telemarketing Do Not Call),
    DMA (DMAchoice / commercial email opt-out), internal owner requests, and
    opt-outs harvested from replies. These are permanent until someone removes
    them, and they override every other rule including an explicit manual send.

``check_outbound``
    *Whether we may contact them right now.* Combines the suppression list with
    the existing cadence policy in ``auto_reply`` (touch counting, cooldown,
    daily caps, federal-domain block).

Fail-closed throughout: if the database cannot be reached, the answer is "do not
send". A guardrail that fails open is not a guardrail.

Legal context, briefly, because this is compliance surface and not a style
preference:
  - TCPA/National DNC: pre-recorded voice and SMS to a listed number require
    prior express written consent, or a recorded legitimate-business-purpose
    basis. Scrubbing the list is the cheapest way to stay defensible.
  - CAN-SPAM: a commercial email to an address that opted out must stop within
    10 business days. :func:`honor_opt_out` makes a reply-based stop instant.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

# Kinds, and what each one blocks. "any" means every channel.
KIND_DNC = "dnc"           # National Do Not Call registry
KIND_DMA = "dma"           # DMAchoice / commercial email opt-out
KIND_INTERNAL = "internal"  # owner asked us to leave someone alone
KIND_UNSUBSCRIBE = "unsubscribe"  # harvested from a reply

KIND_BLOCKS_CHANNEL = {
    KIND_DNC: {"phone", "sms", "mail"},   # DNC is a *phone* registry, not email
    KIND_DMA: {"email", "mail"},
    KIND_INTERNAL: {"any"},
    KIND_UNSUBSCRIBE: {"any"},
}

VALUE_EMAIL = "email"
VALUE_PHONE = "phone"
VALUE_NAME_ZIP = "name_zip"

CHANNEL_ANY = "any"
CHANNEL_EMAIL = "email"
CHANNEL_SMS = "sms"
CHANNEL_MAIL = "mail"
CHANNEL_VOICE = "voice"

# DNC is a *telephony* registry: a hit has to stop the call and the text. An
# entry filed against "phone" therefore blocks every way of reaching them by
# telephone, not just a channel literally named "phone". Without this a DNC hit
# on a mobile number still let the SMS path through, which is the exact failure
# the list exists to prevent.
TELEPHONY_CHANNELS = {"phone", "sms", "voice", "whatsapp", "tel", "mms"}

_DDL = """
CREATE TABLE IF NOT EXISTS suppression_list (
    id           BIGSERIAL PRIMARY KEY,
    kind         VARCHAR(20)  NOT NULL,
    value_type   VARCHAR(20)  NOT NULL,
    value        TEXT         NOT NULL,
    channel      VARCHAR(12)  NOT NULL DEFAULT 'any',
    reason       TEXT         NOT NULL DEFAULT '',
    source       VARCHAR(40)  NOT NULL DEFAULT '',
    active       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_by   VARCHAR(120) NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS suppression_lookup
    ON suppression_list (value_type, value) WHERE active;
CREATE INDEX IF NOT EXISTS suppression_kind_idx
    ON suppression_list (kind) WHERE active;

-- One active entry per identifier. Re-suppressing updates the existing row
-- instead of stacking duplicates, which is what makes ON CONFLICT meaningful
-- below and keeps `find()` from returning the same person four times.
CREATE UNIQUE INDEX IF NOT EXISTS suppression_uniq_active
    ON suppression_list (value_type, value) WHERE active;

-- Audit trail. A suppression that can be silently deleted is not auditable,
-- and "did they honour the opt-out" is the first question in a TCPA complaint.
CREATE TABLE IF NOT EXISTS suppression_audit (
    id          BIGSERIAL PRIMARY KEY,
    entry_id    BIGINT,
    action      VARCHAR(16) NOT NULL,
    kind        VARCHAR(20) NOT NULL DEFAULT '',
    value_type  VARCHAR(20) NOT NULL DEFAULT '',
    value       TEXT         NOT NULL DEFAULT '',
    actor       VARCHAR(120) NOT NULL DEFAULT '',
    detail      TEXT         NOT NULL DEFAULT '',
    at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS suppression_audit_at ON suppression_audit (at DESC);
"""


# --------------------------------------------------------------------------
# normalisation
# --------------------------------------------------------------------------

def normalize_email(email: str) -> str:
    """Lowercase and trim. Deliberately does NOT strip Gmail dots or +tags.

    Those are Gmail-specific delivery tricks, not canonical addresses, and
    treating ``a.b+c@`` as equal to ``ab@`` would silently drop suppression on
    a real person who happens to use a plus tag.
    """
    e = (email or "").strip().lower()
    if "@" not in e:
        return ""
    local, _, domain = e.rpartition("@")
    local = local.strip()
    domain = domain.strip().strip(".")
    if not local or not domain or " " in e:
        return ""
    return f"{local}@{domain}"


def normalize_phone(phone: str) -> str:
    """Digits only, with a leading US country code.

    Ten-digit local numbers are the common case here (Hampton Roads), so they
    are promoted to E.164. Anything that cannot be read as a 10-11 digit US
    number is rejected rather than guessed at -- a wrong normalisation means a
    suppression entry that never matches.
    """
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return ""


def normalize_name_zip(name: str, zipcode: str) -> str:
    """``smith #####`` -- the key form used by both the DNC and DMA lists."""
    n = re.sub(r"[^a-z ]", "", (name or "").strip().lower())
    n = re.sub(r"\s+", " ", n).strip()
    z = re.sub(r"\D", "", str(zipcode or ""))[:5]
    if not n or len(z) != 5:
        return ""
    return f"{n} {z}"


def _norm(value_type: str, value: str, zipcode: str = "") -> str:
    if value_type == VALUE_EMAIL:
        return normalize_email(value)
    if value_type == VALUE_PHONE:
        return normalize_phone(value)
    if value_type == VALUE_NAME_ZIP:
        return normalize_name_zip(value, zipcode or value)
    return ""


def _channel_blocked(entry_channel: str, channel: str) -> bool:
    """Does an entry filed against ``entry_channel`` stop ``channel``?

    ``any`` stops everything. ``phone`` stops every telephony channel, because
    DNC/TCPA suppression is about the *telephone number*, not the transport --
    blocking the call while letting the text through is not compliance.
    """
    entry_channel = (entry_channel or CHANNEL_ANY).strip().lower()
    channel = (channel or "").strip().lower()
    if entry_channel == CHANNEL_ANY or entry_channel == channel:
        return True
    if entry_channel == "phone":
        return channel in TELEPHONY_CHANNELS
    return False


def ensure_schema(db) -> None:
    """Idempotent. Called from the lifespan alongside the main DDL block."""
    with db.cursor() as cur:
        for stmt in _DDL.strip().split(";"):
            if stmt.strip():
                cur.execute(stmt)
    db.commit()


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------

def find(db, *, email="", phone="", name="", zipcode="") -> list[dict]:
    """Active suppression entries matching any supplied identifier.

    Returns ``[]`` rather than raising when the table is missing: callers are
    send paths, and a missing table must not become a crash *or* a silent allow.
    :func:`check_outbound` treats "could not read" as deny.
    """
    keys = []
    e = normalize_email(email)
    if e:
        keys.append((VALUE_EMAIL, e))
    p = normalize_phone(phone)
    if p:
        keys.append((VALUE_PHONE, p))
    nz = normalize_name_zip(name, zipcode)
    if nz:
        keys.append((VALUE_NAME_ZIP, nz))
    if not keys:
        return []
    # An explicit OR chain rather than "(value_type, value) IN (...)": psycopg
    # cannot bind a row constructor to an IN list ("input of anonymous composite
    # types is not implemented"), and it silently poisoned the transaction.
    clause = " OR ".join(["(value_type = %s AND value = %s)"] * len(keys))
    params = [v for pair in keys for v in pair]
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, kind, value_type, value, channel, reason, source, created_at "
                f"FROM suppression_list WHERE active AND ({clause}) ORDER BY created_at DESC;",
                params,
            )
            return list(cur.fetchall() or [])
    except Exception as exc:
        print(f"[suppression] lookup failed: {exc}", flush=True)
        try:
            db.rollback()
        except Exception:
            pass
        return []


def is_suppressed(db, *, email="", phone="", name="", zipcode="",
                  channel: str = CHANNEL_ANY) -> tuple:
    """``(blocked, reason)`` for this identifier on this channel.

    The matching rule is deliberately simple: the *entry's* channel is what
    decides, not the kind. A DNC entry filed as ``channel='phone'`` blocks a
    call and an SMS and nothing else; an internal stop-to-all entry is filed
    ``channel='any'`` and blocks everything. `KIND_BLOCKS_CHANNEL` documents the
    convention used when writing entries, it is not a second filter at read
    time -- applying both would let a badly-filed entry silently stop blocking.

    ``channel='any'`` is the "is this person suppressed at all?" question, which
    is what owner-facing views want; it matches any active entry.
    """
    channel = (channel or CHANNEL_ANY).strip().lower()
    rows = find(db, email=email, phone=phone, name=name, zipcode=zipcode)
    for row in rows:
        entry_channel = (row.get("channel") or CHANNEL_ANY).strip().lower()
        if channel != CHANNEL_ANY and not _channel_blocked(entry_channel, channel):
            continue
        kind = (row.get("kind") or "").strip().lower()
        why = row.get("reason") or kind or "suppressed"
        return True, f"{kind.upper() if kind else 'SUPPRESSED'}: {why}"
    return False, ""


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------

def add(db, kind: str, value_type: str, value: str, *, channel: str = CHANNEL_ANY,
        reason: str = "", source: str = "", actor: str = "",
        zipcode: str = "") -> dict:
    """Add an entry. Re-adding an existing active entry refreshes it in place."""
    kind = (kind or "").strip().lower()
    value_type = (value_type or "").strip().lower()
    norm = _norm(value_type, value, zipcode)
    if not norm:
        return {"ok": False, "error": f"cannot normalise {value_type}: {value!r}"}
    channel = (channel or CHANNEL_ANY).strip().lower()
    if kind not in KIND_BLOCKS_CHANNEL:
        return {"ok": False, "error": f"unknown kind {kind!r}"}
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO suppression_list
                    (kind, value_type, value, channel, reason, source, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (value_type, value) WHERE active
                DO UPDATE SET kind = EXCLUDED.kind,
                              channel = EXCLUDED.channel,
                              reason = EXCLUDED.reason,
                              source = EXCLUDED.source,
                              updated_at = NOW()
                RETURNING id;
                """,
                (kind, value_type, norm, channel, reason or "", source or "", actor or ""),
            )
            row = cur.fetchone()
            entry_id = row["id"] if row else None
            cur.execute(
                "INSERT INTO suppression_audit (entry_id, action, kind, value_type, value, actor, detail) "
                "VALUES (%s, 'add', %s, %s, %s, %s, %s);",
                (entry_id, kind, value_type, norm, actor or "", reason or ""),
            )
        db.commit()
        return {"ok": True, "id": entry_id, "value": norm, "kind": kind}
    except Exception as exc:
        db.rollback()
        print(f"[suppression] add failed: {exc}", flush=True)
        return {"ok": False, "error": str(exc)}


def remove(db, value_type: str, value: str, *, actor: str = "", reason: str = "",
           zipcode: str = "") -> dict:
    """Deactivate. Rows are kept -- the audit trail needs them."""
    value_type = (value_type or "").strip().lower()
    norm = _norm(value_type, value, zipcode)
    if not norm:
        return {"ok": False, "error": f"cannot normalise {value_type}: {value!r}"}
    try:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE suppression_list SET active = FALSE, updated_at = NOW() "
                "WHERE value_type = %s AND value = %s RETURNING id, kind;",
                (value_type, norm),
            )
            rows = list(cur.fetchall() or [])
            for r in rows:
                cur.execute(
                    "INSERT INTO suppression_audit (entry_id, action, kind, value_type, value, actor, detail) "
                    "VALUES (%s, 'remove', %s, %s, %s, %s, %s);",
                    (r["id"], r["kind"], value_type, norm, actor or "", reason or ""),
                )
        db.commit()
        return {"ok": True, "removed": len(rows), "value": norm}
    except Exception as exc:
        db.rollback()
        return {"ok": False, "error": str(exc)}


def honor_opt_out(db, *, email="", phone="", name="", zipcode="",
                  reason: str = "requested to stop", actor: str = "auto") -> dict:
    """Apply an opt-out. Safe to call repeatedly for the same person.

    Wired to inbound replies so CAN-SPAM's 10-business-day clock starts at the
    reply rather than whenever someone gets round to reading it.
    """
    out = {}
    for vtype, val in ((VALUE_EMAIL, email), (VALUE_PHONE, phone)):
        if _norm(vtype, val):
            out[vtype] = add(db, KIND_UNSUBSCRIBE, vtype, val,
                             reason=reason, source="inbound_reply", actor=actor)
    if normalize_name_zip(name, zipcode):
        out[VALUE_NAME_ZIP] = add(db, KIND_UNSUBSCRIBE, VALUE_NAME_ZIP,
                                   f"{name} {zipcode}", reason=reason,
                                   source="inbound_reply", actor=actor)
    return {"ok": any(r.get("ok") for r in out.values()), "results": out}


def listing(db, *, kind: str = "", active_only: bool = True, limit: int = 200) -> list[dict]:
    where, params = [], []
    if kind:
        where.append("kind = %s")
        params.append(kind.strip().lower())
    if active_only:
        where.append("active")
    sql = "SELECT id, kind, value_type, value, channel, reason, source, created_by, created_at " \
          "FROM suppression_list"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT %s;"
    params.append(int(limit))
    try:
        with db.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall() or [])
    except Exception as exc:
        print(f"[suppression] listing failed: {exc}", flush=True)
        return []


# --------------------------------------------------------------------------
# the one gate
# --------------------------------------------------------------------------

def check_outbound(db, channel: str, recipient: str, *, lead_id=None,
                   source: str = "", email: str = "", phone: str = "",
                   name: str = "", zipcode: str = "") -> tuple:
    """``(allowed, reason)``. The only sanctioned pre-send check.

    Order matters. Suppression first, because it is absolute and cheap; then
    the source/federal block; then the cadence policy. A denial is always
    returned with a human-readable reason so the owner sees *why* nothing went
    out instead of a silent nothing.
    """
    channel = (channel or CHANNEL_EMAIL).strip().lower()
    email = email or (recipient if channel == CHANNEL_EMAIL else "")
    phone = phone or (recipient if channel in (CHANNEL_SMS, CHANNEL_VOICE) else "")

    if db is None:
        return False, "no database: failing closed"

    # 1. Suppression list. DNC and DMA are the registry checks.
    try:
        ensure_schema(db)
    except Exception as exc:
        print(f"[suppression] ensure_schema failed: {exc}", flush=True)
    blocked, why = is_suppressed(db, email=email, phone=phone, name=name,
                                 zipcode=zipcode, channel=channel)
    if blocked:
        return False, f"suppressed — {why}"

    # 2. Source / federal domain (existing policy, unchanged).
    try:
        import auto_reply
    except Exception as exc:
        return False, f"guardrail unavailable: {exc}"
    blocked_reason = auto_reply.blocked_reason(email, source)
    if blocked_reason:
        return False, f"blocked — {blocked_reason}"

    # 3. Cadence policy: touch count, cooldown, per-recipient limit.
    ok, reason = auto_reply.contact_policy_allows(db, recipient, channel, lead_id=lead_id)
    if not ok:
        return False, f"cadence — {reason}"

    # 4. Daily cap for the channel.
    ok, reason = auto_reply._channel_allowed(db, channel)
    if not ok:
        return False, reason

    return True, ""