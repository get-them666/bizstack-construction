"""Batch outbound AI calls to leads that were never dialled.

Why this exists
---------------
``_follow_up_new_lead`` in ``construction_main`` dials a lead once, at the
moment it is created, inside calling hours. Anything created before that path
existed -- or created outside hours, or skipped while SignalWire was down --
has never been rung and will never be rung by that code. This is the catch-up:
the same SWML voice agent, pointed at a bounded batch of existing leads.

Every gate is reused rather than re-implemented:

* ``suppression.check_outbound`` -- DNC/internal suppression, the federal
  source block, the per-lead touch policy and the channel's daily cap. It fails
  closed, so a database problem means no calls.
* ``bot_calls`` / ``comms_logs`` -- a number is dialled once. There is no retry
  loop here; a call that was placed counts whether or not it was answered.
* Calling hours, weekdays only, America/New_York.

Dial once is deliberate, not an oversight
-----------------------------------------
A lead with any ``bot_calls`` row is never auto-dialled again -- not because the
first call succeeded, but because it was *placed*. An unanswered AI call is not
a failed call; ringing it again tomorrow is a different, more aggressive
campaign (and the whole point of this module is one catch-up first touch, not a
dialler). Repeat attempts belong to a person: the crew call list in
``call_list`` or an explicit owner action. Only a dial the carrier *refused*
produces no ``call_sid``, and only that leaves the lead eligible for the next
run.

Two processes must not dial the same lead
-----------------------------------------
The daily scheduler thread and a manual run (Copilot tool, CLI, API route) can
overlap. ``candidates`` is only a read, so both would see the same lead. The
guard is ``outbound_call_claims``: one row per lead, taken with
``INSERT .. ON CONFLICT DO UPDATE .. WHERE <claim is stale> RETURNING`` and
committed before the dial. A second worker's INSERT matches the conflict and
the WHERE fails, so it gets no row back and skips the lead. ``SKIP LOCKED`` /
``FOR UPDATE`` on ``leads`` was the other option and is wrong here: the
suppression gate and the claim both commit, so a row lock taken in
``candidates`` would be released before the dial and protect nothing.

A claim goes stale after ``_CLAIM_TTL_SECONDS`` so a process that dies between
claiming and dialling does not strand the lead forever. That is the one
remaining double-dial window: if a worker is alive but wedged for longer than
the TTL mid-dial, another worker can reclaim the lead. The post-claim
``bot_calls`` re-check closes it for every case except "call placed but the
process died before recording it".

There is a second dialler, and it is not this module: ``_follow_up_new_lead``
in ``construction_main`` rings a lead from its own thread the moment it is
created. It takes no claim, so ``candidates`` ignores leads younger than
``_FRESH_LEAD_GRACE_MINUTES`` and leaves them to that path. Without the grace
period the two paths could both dial the same brand-new lead on the same
afternoon.

The daily cap counts placed calls only
---------------------------------------
``calls_made_today`` counts ``bot_calls`` rows and ignores the failure statuses
(``failed``/``rejected``/``error``/``undialed``). A carrier rejection writes
nothing at all, so it neither blocks the lead nor consumes the cap. The counter
is not atomic across processes: concurrent runs can each pass the check on the
same remaining budget, so the cap is re-read after every successful dial, which
bounds the overshoot to the number of workers in flight.

What it will not do
-------------------
By default it only dials leads whose source says the person contacted us
(quote form, site chat, phone). Permit records are addresses, not requests, and
cold-dialling them is a different legal risk from returning an enquiry; they
need ``include_permits=True`` explicitly. The National DNC registry is *not*
loaded into this app -- ``suppression_list`` only holds entries someone added --
so treat the DNC check as "not on our suppression list", not as a registry
scrub. Load a scrub result into ``suppression_list`` before a large campaign.

    python outbound_campaign.py --limit 5              # dry run, lists them
    python outbound_campaign.py --limit 5 --apply      # places the calls

Nothing here is started by a background thread: a campaign is an explicit
owner action, from the Copilot or the CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime

import suppression

# A lead is worth dialling when there is a real number, the job is still open,
# and nobody has already put this lead in the voice log.
_OPEN_STATUSES = ("won", "lost", "closed", "cancelled", "canceled", "archived")

# Sources that mean the person asked us to contact them. Anything else
# (permit_finder, backlog, scraped lists) needs an explicit opt-in.
_INBOUND_SOURCES = (
    "quotes_page", "instant_quote", "embed_quote", "photo_quote",
    "ai-assistant", "website", "website_chat", "contact", "call",
)

# Returned by :func:`_sources` when a configuration names only cold sources and
# cold calling is held. It matches no lead, which is the point: an empty list
# means "any source" and would re-open the gate.
_NO_SOURCE = "__cold_calling_held__"

_DEFAULT_LIMIT = 5
_HARD_PER_RUN = 25
_MAX_DAILY = 30          # matches the voice channel's hard ceiling
_CALL_TZ = "America/New_York"
# A DNC scrub is only a safe harbour while it is recent. The national registry
# changes constantly; 31 days is the TCPA window a scrub is normally relied on.
_SCRUB_MAX_AGE_DAYS = 31

# bot_calls statuses that mean "no call was placed". A dial the carrier refused
# never writes a row at all; these cover a row written elsewhere and then marked
# as a failure. They must not count against the daily cap.
_FAILED_CALL_STATUSES = ("failed", "rejected", "error", "undialed")

# How long one worker owns a lead before another may take it over. Long enough
# for a SignalWire call-create to finish (seconds), short enough that a crashed
# process does not park a lead for the rest of the day.
_CLAIM_TTL_SECONDS = 300

# A lead created moments ago is the immediate-follow-up path's business:
# ``construction_main._follow_up_new_lead`` dials it from its own thread. That
# thread does not take a claim (this module cannot edit it), so the campaign
# stays off the lead until the other dialer has had time to finish. Nothing is
# lost: if that path declined or was down, the campaign picks the lead up on a
# later run.
_FRESH_LEAD_GRACE_MINUTES = 15

# The daily loop: check every 5 minutes, and only inside the first 20 minutes
# of the configured hour, so a restart at 09:10 still runs the day's batch.
_SCHEDULER_INTERVAL_SECONDS = 300
_SCHEDULER_WINDOW_MINUTES = 20

_OPTOUT_RE = re.compile(
    r"(?i)\b(stop calling|do not call|don'?t call|no more calls|remove me|"
    r"take me off|unsubscribe|do not contact|never call|stop)\b")


def wants_stop(text: str) -> bool:
    """True when the person on the call asked us to stop.

    Bare "stop" counts only when it is the whole utterance: "stop by Tuesday"
    is a scheduling sentence, "stop" on its own is an opt-out.
    """
    s = (text or "").strip()
    if not s:
        return False
    if re.search(r"(?i)\b(stop calling|do not call|don'?t call|no more calls|"
                 r"remove me|take me off|unsubscribe|do not contact|never call)\b", s):
        return True
    return bool(re.fullmatch(r"(?i)\s*(please\s+)?stop(\s+it)?(\s+please)?[.!]?\s*", s))


# --------------------------------------------------------------------------
# configuration (explicit parsing: the string "false" is truthy)
# --------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _flag(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(_env(name) or default)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def enabled() -> bool:
    """Live dialling needs an explicit opt-in *and* no kill switch."""
    if _flag("DISABLE_OUTBOUND_CAMPAIGN"):
        return False
    return _flag("OUTBOUND_CAMPAIGN_ENABLED")


def per_run_limit(requested=None) -> int:
    cap = _int_env("OUTBOUND_CAMPAIGN_MAX", _DEFAULT_LIMIT, 1, _HARD_PER_RUN)
    if requested is None:
        return cap
    try:
        want = int(requested)
    except (TypeError, ValueError):
        return cap
    return max(0, min(cap, want))


def daily_limit() -> int:
    return _int_env("OUTBOUND_CAMPAIGN_DAILY", _MAX_DAILY, 1, _MAX_DAILY)


def _sources(include_permits: bool) -> list:
    """Source allow-list. Empty means "any source" (permits included).

    ``OUTBOUND_CAMPAIGN_SOURCES`` chooses *which* sources to dial; it can never
    widen the list past the cold-calling hold. That order used to be the other
    way round, so a configured ``permit_finder`` survived the downgrade in
    :func:`run` and cold residential permits were dialled while the report said
    the DNC gate was holding them.

    When permits are held, the configured list is therefore intersected with
    ``_INBOUND_SOURCES``. A configuration that names only cold sources yields a
    sentinel that matches nothing rather than ``[]`` -- empty here means "any
    source", which is exactly the bypass being guarded against.
    """
    configured = _env("OUTBOUND_CAMPAIGN_SOURCES")
    wanted = [s.strip().lower() for s in configured.split(",") if s.strip()] if configured else []
    if not wanted:
        return [] if include_permits else list(_INBOUND_SOURCES)
    if include_permits:
        return wanted
    inbound = {s.lower() for s in _INBOUND_SOURCES}
    kept = [s for s in wanted if s in inbound]
    return kept or [_NO_SOURCE]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def e164(phone) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    return ""


def _plausible(number: str) -> bool:
    """Reject placeholders that still have ten digits (000..., 555...)."""
    if not number:
        return False
    digits = re.sub(r"\D", "", number)[-10:]
    if len(set(digits)) <= 2:
        return False
    if digits[:3] in ("000", "111", "555", "999"):
        return False
    return True


def in_call_window(now=None) -> tuple:
    """(ok, reason). Weekday business hours in the leads' timezone."""
    try:
        from zoneinfo import ZoneInfo
        current = now or datetime.now(ZoneInfo(_CALL_TZ))
        if current.tzinfo is None:
            current = current.replace(tzinfo=ZoneInfo(_CALL_TZ))
        current = current.astimezone(ZoneInfo(_CALL_TZ))
    except Exception:
        return False, "timezone unavailable"
    if current.weekday() == 6:
        return False, "Sunday — no calling"
    if current.weekday() == 5:              # Saturday, shorter day
        start, end = 10, 16
    else:
        start, end = 9, 18
    if not (start <= current.hour < end):
        return False, f"outside calling hours ({start}:00–{end}:00 ET)"
    return True, ""


# --------------------------------------------------------------------------
# candidates
# --------------------------------------------------------------------------

def _safe_ddl(db, statement: str) -> None:
    """Run one idempotent DDL statement; losing a concurrent CREATE is fine.

    Two workers can both pass the ``IF NOT EXISTS`` catalog check and one then
    fails with a duplicate relation error. ``inbound_email._safe_ddl`` pins the
    same pattern for the same reason.
    """
    try:
        with db.cursor() as cur:
            cur.execute(statement)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


# bot_calls is created here as well as in construction_main because either path
# can be the first to run. The lead index makes the "already dialled?" checks
# cheap; the claims table is the concurrency guard documented at module level.
_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS bot_calls (call_sid TEXT PRIMARY KEY, lead_id INTEGER, "
    "direction TEXT, status TEXT, turns INTEGER DEFAULT 0, "
    "created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);",
    "CREATE INDEX IF NOT EXISTS bot_calls_lead_idx ON bot_calls (lead_id);",
    "CREATE TABLE IF NOT EXISTS outbound_call_claims ("
    "  lead_id BIGINT PRIMARY KEY,"
    "  claimed_by TEXT NOT NULL DEFAULT '',"
    "  claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW());",
)


def _ensure_tables(db) -> None:
    for statement in _TABLE_DDL:
        _safe_ddl(db, statement)


def candidates(db, limit: int = _DEFAULT_LIMIT, include_permits: bool = False,
               company: str = "construction") -> list:
    """Open, real-numbered leads that were never dialled, best first.

    Excluded beyond the obvious: a lead another worker is dialling right now
    (live row in ``outbound_call_claims``), and a lead created in the last
    ``_FRESH_LEAD_GRACE_MINUTES``, which belongs to the immediate follow-up
    thread instead.

    The per-lead suppression/cadence gate is NOT applied here: it is applied
    one row at a time by :func:`run`, so a dry run reports exactly what the
    live run would refuse and why.
    """
    _ensure_tables(db)
    sources = _sources(include_permits)
    params = {
        "company": company,
        "limit": max(1, int(limit)),
        "sources": sources,
        "closed": list(_OPEN_STATUSES),
        "claim_ttl": int(_CLAIM_TTL_SECONDS),
        "fresh_seconds": int(_FRESH_LEAD_GRACE_MINUTES * 60),
    }
    source_clause = ""
    if sources:
        source_clause = "AND lower(coalesce(l.source,'')) = ANY(%(sources)s)"
    sql = f"""
        SELECT l.id, l.name, l.phone, l.email, l.source, l.status, l.project_type,
               l.address
          FROM leads l
         WHERE l.company = %(company)s
           AND length(regexp_replace(coalesce(l.phone,''), '\\D', '', 'g')) >= 10
           AND lower(coalesce(l.status,'new')) <> ALL(%(closed)s)
           AND (l.created_at IS NULL
                OR l.created_at < NOW() - (%(fresh_seconds)s * INTERVAL '1 second'))
           AND NOT EXISTS (SELECT 1 FROM bot_calls b WHERE b.lead_id = l.id)
           AND NOT EXISTS (
                 SELECT 1 FROM comms_logs c
                  WHERE c.channel = 'voice' AND c.direction = 'outbound'
                    AND right(regexp_replace(coalesce(c.recipient,''), '\\D', '', 'g'), 10)
                        = right(regexp_replace(coalesce(l.phone,''), '\\D', '', 'g'), 10))
           AND NOT EXISTS (
                 SELECT 1 FROM outbound_call_claims k
                  WHERE k.lead_id = l.id
                    AND k.claimed_at >= NOW() - (%(claim_ttl)s * INTERVAL '1 second'))
           {source_clause}
         ORDER BY CASE
                    WHEN lower(coalesce(l.status,'new')) IN ('engaged','replied','contacted') THEN 0
                    WHEN lower(coalesce(l.status,'new')) = 'new' THEN 1
                    ELSE 2
                  END,
                  l.id DESC
         LIMIT %(limit)s
    """
    with db.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall() or [])


def calls_made_today(db) -> int:
    """Outbound calls actually placed today (America/New_York), or -1.

    ``-1`` means the count could not be read. It used to return 0 there, which
    is fail-open: a database blip reset the daily budget and bought another full
    cap of calls. Callers must treat -1 as "do not dial".

    A refused dial writes no ``bot_calls`` row, and any row that someone marked
    as a failure is excluded here too: the cap is about calls the carrier
    placed, not attempts we made.
    """
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM bot_calls "
                "WHERE created_at >= (date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s) "
                "  AND direction = 'outbound' "
                "  AND lower(coalesce(status,'')) <> ALL(%s);",
                (_CALL_TZ, _CALL_TZ, list(_FAILED_CALL_STATUSES)))
            row = cur.fetchone() or {}
        return int(row.get("n") or 0)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return -1


# --------------------------------------------------------------------------
# one dial at a time per lead
# --------------------------------------------------------------------------

def _worker_id() -> str:
    """Who holds a claim. pid makes a stuck claim traceable in the database."""
    return f"{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _claim(db, lead_id: int, worker: str,
           ttl_seconds: int = _CLAIM_TTL_SECONDS) -> bool:
    """Take the dial lock for one lead. True when this worker may dial it.

    The row is committed before the dial, so the guard works across processes
    and does not depend on a transaction staying open over a network call. A
    live claim belongs to somebody else; a stale one is taken over in place.
    """
    _ensure_tables(db)
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO outbound_call_claims (lead_id, claimed_by, claimed_at) "
                "VALUES (%s, %s, NOW()) "
                "ON CONFLICT (lead_id) DO UPDATE "
                "   SET claimed_by = EXCLUDED.claimed_by, claimed_at = NOW() "
                " WHERE outbound_call_claims.claimed_at "
                "       < NOW() - (%s * INTERVAL '1 second') "
                "RETURNING lead_id;",
                (int(lead_id), str(worker), int(ttl_seconds)))
            row = cur.fetchone()
        db.commit()
        return row is not None
    except Exception as exc:
        print(f"📞[campaign] could not claim lead {lead_id}: {exc}", flush=True)
        try:
            db.rollback()
        except Exception:
            pass
        return False


def _release(db, lead_id: int, worker: str) -> None:
    """Drop this worker's claim. Never removes somebody else's."""
    try:
        with db.cursor() as cur:
            cur.execute(
                "DELETE FROM outbound_call_claims "
                "WHERE lead_id = %s AND claimed_by = %s;", (int(lead_id), str(worker)))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _already_dialed(db, lead_id: int) -> bool:
    """True when a bot_calls row exists. Fails closed: unknown means yes.

    Re-checked after the claim is taken, because a claim can be taken over once
    it is stale and that is exactly when a half-finished worker may have placed
    the call without recording it yet.
    """
    try:
        with db.cursor() as cur:
            cur.execute("SELECT 1 FROM bot_calls WHERE lead_id = %s LIMIT 1;",
                        (int(lead_id),))
            return cur.fetchone() is not None
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return True


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

def run(db, limit=None, apply: bool = False, include_permits: bool = False,
        force_window: bool = False, company: str = "construction") -> dict:
    """Dial a bounded batch. ``apply=False`` only reports who would be called.

    Returns a report rather than raising: the Copilot and the CLI both need to
    show the owner exactly what happened, including every skip reason.
    """
    want = per_run_limit(limit)
    report = {"ok": True, "applied": False, "would_call": [], "dialed": [],
              "skipped": [], "unrecorded": [], "considered": 0, "configured": False}

    if apply:
        if not enabled():
            report.update(ok=False, reason=(
                "Outbound campaigns are off. Set OUTBOUND_CAMPAIGN_ENABLED=1 in "
                "construction.env (and unset DISABLE_OUTBOUND_CAMPAIGN) to dial."))
            return report
        if not force_window:
            ok, why = in_call_window()
            if not ok:
                report.update(ok=False, reason=why)
                return report
        used = calls_made_today(db)
        if used < 0:
            report.update(ok=False, reason=(
                "could not read today's call count; not dialling (fail closed)"))
            return report
        cap = daily_limit()
        if used >= cap:
            report.update(ok=False, reason=f"daily call cap reached ({used}/{cap})")
            return report
        want = min(want, cap - used)

    if want <= 0:
        report.update(ok=False, reason="nothing to do (limit is 0)")
        return report

    cold_note = ""
    effective_permits = bool(include_permits)
    if include_permits:
        allowed, why = cold_calling_allowed(db)
        cold_note = why
        if not allowed:
            # Keep dialling the people who contacted us; only the cold segment
            # is held. This is why a missing scrub does not stop the campaign.
            effective_permits = False
            report["cold_calling_blocked"] = why

    rows = candidates(db, limit=max(want, 1), include_permits=effective_permits,
                      company=company)
    report["considered"] = len(rows)

    import signalwire_service
    wire = signalwire_service.SignalWireService()
    report["configured"] = wire.is_configured()
    if apply and not report["configured"]:
        report.update(ok=False, reason="SignalWire is not configured; no call placed.")
        return report

    base = (os.getenv("APP_BASE_URL")
            or f"https://{os.getenv('COMPANY_DOMAIN', 'construction.bizstackperks.com')}").rstrip("/")

    for row in rows:
        lead_id = int(row.get("id"))
        to = e164(row.get("phone"))
        label = {"lead_id": lead_id, "name": row.get("name") or "",
                 "phone": to, "source": row.get("source") or ""}
        if not to or not _plausible(to):
            report["skipped"].append({**label, "reason": "no usable phone"})
            continue
        try:
            allowed, why = suppression.check_outbound(
                db, suppression.CHANNEL_VOICE, to, lead_id=lead_id,
                name=row.get("name") or "", zipcode=(row.get("address") or "")[-5:]
                if (row.get("address") or "")[-5:].isdigit() else "",
                source=row.get("source") or "")
        except Exception as exc:
            report["skipped"].append({**label, "reason": f"suppression check failed: {exc}"})
            continue
        if not allowed:
            report["skipped"].append({**label, "reason": why})
            continue

        if not apply:
            report["would_call"].append(label)
            if len(report["would_call"]) >= want:
                break
            continue

        # The scheduler thread and a manual run are separate connections. The
        # claim is a committed row rather than a row lock: check_outbound() and
        # _claim() both commit, so anything SELECT ... FOR UPDATE took in
        # candidates() would already be released before the dial.
        worker = _worker_id()
        if not _claim(db, lead_id, worker):
            report["skipped"].append({
                **label, "reason": "another worker is already dialling this lead"})
            continue
        keep_claim = False
        try:
            if _already_dialed(db, lead_id):
                report["skipped"].append({
                    **label, "reason": "already dialled by another worker"})
                continue
            sid = _dial(wire, to, lead_id, base)
            if not sid:
                # Nothing is written and the claim is released: a carrier
                # rejection is not a call, so the lead stays eligible and the
                # daily cap is untouched.
                report["skipped"].append({**label, "reason": "carrier did not accept the call"})
                continue
            try:
                _record(db, sid, lead_id, to)
            except Exception as exc:
                # The call is out, so the claim must NOT be released -- that
                # would allow an immediate second dial. Let it go stale instead
                # and report the lead as placed-but-unrecorded.
                keep_claim = True
                try:
                    db.rollback()
                except Exception:
                    pass
                report["unrecorded"].append({**label, "call_sid": sid, "reason": str(exc)})
                report.update(ok=False, reason=(
                    f"call to lead {lead_id} was placed but could not be recorded: {exc}"))
                break
            report["dialed"].append({**label, "call_sid": sid})
        finally:
            if not keep_claim:
                _release(db, lead_id, worker)

        if len(report["dialed"]) >= want:
            break
        # Re-read the cap after every dial. The check at the top is not atomic,
        # so two workers can both think there is one call left; this bounds the
        # overshoot instead of letting each worker spend the whole remainder.
        used_now = calls_made_today(db)
        if used_now < 0:
            report["cap_unknown"] = True
            break
        if used_now >= daily_limit():
            report["daily_cap_reached"] = True
            break

    report["applied"] = bool(apply)
    return report


def _dial(wire, to: str, lead_id: int, base: str) -> str:
    swml_url = f"{base}/comms/outbound-voice.swml?to={to}&lead_id={int(lead_id)}"
    try:
        return wire.create_ai_outbound_call(to, swml_url) or ""
    except Exception as exc:
        print(f"📞[campaign] dial failed for lead {lead_id}: {exc}", flush=True)
        return ""


def _record(db, sid: str, lead_id: int, to: str) -> None:
    _ensure_tables(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO bot_calls (call_sid, lead_id, direction, status) "
            "VALUES (%s, %s, 'outbound', 'dialed') ON CONFLICT (call_sid) DO NOTHING;",
            (sid, lead_id))
        cur.execute(
            "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
            "VALUES ('outbound', 'voice', 'system', %s, %s);",
            (to, f"AI campaign call for lead {lead_id}"))
    db.commit()


# --------------------------------------------------------------------------
# daily run
# --------------------------------------------------------------------------

def _ensure_run_log(db) -> None:
    with db.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS outbound_campaign_runs ("
            "  run_date DATE PRIMARY KEY,"
            "  considered INTEGER NOT NULL DEFAULT 0,"
            "  dialed INTEGER NOT NULL DEFAULT 0,"
            "  detail TEXT NOT NULL DEFAULT '',"
            "  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS outbound_dnc_scrub ("
            "  scrubbed_at DATE PRIMARY KEY,"
            "  imported INTEGER NOT NULL DEFAULT 0,"
            "  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());")
    db.commit()


def cold_calling_allowed(db) -> tuple:
    """(ok, reason) for dialling permit records, which are not an enquiry.

    A cold call to a residential number is the regulated case: it needs a
    recent national/state DNC scrub. Importing a scrub with ``--import-dnc``
    records the date; this refuses cold dialling when that date is missing or
    older than the 31-day safe-harbour window. Calls to people who contacted us
    are not gated on this -- they supplied the number for this purpose.
    """
    try:
        _ensure_run_log(db)
        with db.cursor() as cur:
            cur.execute("SELECT scrubbed_at FROM outbound_dnc_scrub "
                        "ORDER BY scrubbed_at DESC LIMIT 1;")
            row = cur.fetchone()
    except Exception as exc:
        return False, f"could not read the DNC scrub date: {exc}"
    if not row:
        return False, (
            "no DNC scrub on file — cold permit calls are held. Load a national/"
            "state DNC scrub with `python outbound_campaign.py --import-dnc <file>` "
            "and cold calling unlocks for 31 days.")
    day = row.get("scrubbed_at") if isinstance(row, dict) else row[0]
    age = (datetime.now().date() - day).days
    if age > _SCRUB_MAX_AGE_DAYS:
        return False, (f"the DNC scrub is {age} days old (limit {_SCRUB_MAX_AGE_DAYS}); "
                       f"load a fresh scrub before cold calling.")
    return True, f"DNC scrub {age} day(s) old"


def import_dnc(db, path: str, actor: str = "owner") -> dict:
    """Load a DNC scrub result into the suppression list.

    Accepts one number per line or the first CSV column. The file is the result
    of a registry scrub (National DNC, Virginia, North Carolina); it is not a
    place to paste leads. Every number is stored as kind='dnc', channel='phone',
    so it blocks calls and texts and nothing else.
    """
    added = skipped = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            token = (line.split(",")[0] if "," in line else line).strip()
            digits = re.sub(r"\D", "", token)
            if len(digits) < 10:
                continue
            number = suppression.normalize_phone(digits)
            if not number:
                skipped += 1
                continue
            res = suppression.add(db, suppression.KIND_DNC, suppression.VALUE_PHONE,
                                  number, channel=suppression.CHANNEL_ANY,
                                  reason="DNC registry scrub", source="dnc-scrub",
                                  actor=actor)
            if res.get("ok"):
                added += 1
            else:
                skipped += 1
    _ensure_run_log(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO outbound_dnc_scrub (scrubbed_at, imported) "
            "VALUES ((now() AT TIME ZONE %s)::date, %s) "
            "ON CONFLICT (scrubbed_at) DO UPDATE SET imported = outbound_dnc_scrub.imported + EXCLUDED.imported;",
            (_CALL_TZ, added))
    db.commit()
    return {"ok": True, "added": added, "skipped": skipped, "file": path}


def ran_today(db) -> bool:
    try:
        _ensure_run_log(db)
        with db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM outbound_campaign_runs "
                "WHERE run_date = (now() AT TIME ZONE %s)::date LIMIT 1;", (_CALL_TZ,))
            return cur.fetchone() is not None
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return True       # unknown means "do not dial twice"


def _log_run(db, considered: int, dialed: int, detail: str = "") -> None:
    try:
        _ensure_run_log(db)
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO outbound_campaign_runs (run_date, considered, dialed, detail) "
                "VALUES ((now() AT TIME ZONE %s)::date, %s, %s, %s) "
                "ON CONFLICT (run_date) DO NOTHING;",
                (_CALL_TZ, considered, dialed, (detail or "")[:2000]))
        db.commit()
    except Exception as exc:
        print(f"📞[campaign] could not log the run: {exc}", flush=True)


def _scheduler_cycle(now=None, connect=None) -> dict:
    """One tick of the daily loop. Returns what it did; raises on real errors.

    The kill switch is re-read here on every call rather than once at startup:
    setting ``DISABLE_OUTBOUND_CAMPAIGN`` must stop tomorrow's batch without a
    restart, and clearing it must resume without one. ``connect`` is injectable
    so this can be exercised without a database.

    The day is only marked as run when the attempt was real (``ok`` or at least
    one call placed). A missing SignalWire configuration used to burn the day's
    row and silence the campaign until tomorrow even after it was fixed; now the
    next tick inside the 20-minute window retries.
    """
    if not (enabled() and _flag("OUTBOUND_CAMPAIGN_SCHEDULED")):
        return {"ran": False, "reason": "disabled"}
    try:
        from zoneinfo import ZoneInfo
        current = now or datetime.now(ZoneInfo(_CALL_TZ))
        if current.tzinfo is None:
            current = current.replace(tzinfo=ZoneInfo(_CALL_TZ))
        current = current.astimezone(ZoneInfo(_CALL_TZ))
    except Exception as exc:
        return {"ran": False, "reason": f"timezone unavailable: {exc}"}
    hour = _int_env("OUTBOUND_CAMPAIGN_HOUR", 9, 0, 23)
    if (current.weekday() == 6 or current.hour != hour
            or current.minute >= _SCHEDULER_WINDOW_MINUTES):
        return {"ran": False, "reason": "not due"}

    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if connect is None:
        if not db_url:
            return {"ran": False, "reason": "DATABASE_URL is not set"}
        import psycopg
        from psycopg.rows import dict_row

        def connect():
            return psycopg.connect(db_url, row_factory=dict_row)

    with connect() as db:
        if ran_today(db):
            return {"ran": False, "reason": "already ran today"}
        result = run(db, limit=daily_limit(), apply=True,
                     include_permits=_flag("OUTBOUND_CAMPAIGN_INCLUDE_PERMITS"))
        dialed = len(result.get("dialed") or [])
        if result.get("ok") or dialed:
            _log_run(db, result.get("considered", 0), dialed,
                     json.dumps(result.get("skipped") or [], default=str))
        print(f"📞[campaign] daily run: dialed {dialed} "
              f"of {result.get('considered', 0)} considered", flush=True)
        return {"ran": True, "ok": bool(result.get("ok")), "dialed": dialed,
                "considered": result.get("considered", 0),
                "reason": result.get("reason", "")}


def _scheduler_loop(interval=None, max_cycles=None) -> None:
    """Run :func:`_scheduler_cycle` forever; a bad cycle must not end the thread.

    If an exception escaped this loop the daemon thread would be dead and the
    daily batch would never run again until the process was restarted -- a
    silent stop for a job whose only symptom is calls that do not happen.
    """
    import time
    pause = _SCHEDULER_INTERVAL_SECONDS if interval is None else max(0, interval)
    cycles = 0
    while True:
        try:
            _scheduler_cycle()
        except Exception as exc:
            print(f"📞[campaign] daily run failed: {type(exc).__name__}: {exc}", flush=True)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
        time.sleep(pause)


def start_scheduler():
    """One batch a day, inside calling hours, if the owner turned it on.

    Off unless BOTH ``OUTBOUND_CAMPAIGN_ENABLED`` and
    ``OUTBOUND_CAMPAIGN_SCHEDULED`` are true. Both flags and the kill switch are
    re-read every cycle inside :func:`_scheduler_cycle`. The date row in
    ``outbound_campaign_runs`` makes a restart at 09:10 harmless.
    """
    if not (enabled() and _flag("OUTBOUND_CAMPAIGN_SCHEDULED")):
        return None
    import threading

    thread = threading.Thread(target=_scheduler_loop, daemon=True,
                              name="outbound-campaign")
    thread.start()
    print(f"📞[campaign] daily scheduler started (hour {_int_env('OUTBOUND_CAMPAIGN_HOUR', 9, 0, 23)} ET)",
          flush=True)
    return thread


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Batch outbound AI calls to existing leads.")
    ap.add_argument("--limit", type=int, default=None, help="max calls this run")
    ap.add_argument("--apply", action="store_true", help="actually place calls")
    ap.add_argument("--include-permits", action="store_true",
                    help="also dial permit records (not an inbound request)")
    ap.add_argument("--force-window", action="store_true",
                    help="ignore calling hours (testing only)")
    ap.add_argument("--import-dnc", default="",
                    help="load a DNC scrub result (one number per line / first CSV column)")
    args = ap.parse_args(argv)

    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if not db_url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(db_url, row_factory=dict_row) as db:
        if args.import_dnc:
            print(json.dumps(import_dnc(db, args.import_dnc), indent=2))
            return 0
        result = run(db, limit=args.limit, apply=args.apply,
                     include_permits=args.include_permits,
                     force_window=args.force_window)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
