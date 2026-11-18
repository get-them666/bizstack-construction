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

_DEFAULT_LIMIT = 5
_HARD_PER_RUN = 25
_MAX_DAILY = 30          # matches the voice channel's hard ceiling
_CALL_TZ = "America/New_York"
# A DNC scrub is only a safe harbour while it is recent. The national registry
# changes constantly; 31 days is the TCPA window a scrub is normally relied on.
_SCRUB_MAX_AGE_DAYS = 31

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
    """Source allow-list. Empty means "any source" (permits included)."""
    configured = _env("OUTBOUND_CAMPAIGN_SOURCES")
    if configured:
        return [s.strip() for s in configured.split(",") if s.strip()]
    if include_permits:
        return []
    return list(_INBOUND_SOURCES)


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

def _ensure_tables(db) -> None:
    with db.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS bot_calls (call_sid TEXT PRIMARY KEY, lead_id INTEGER, "
            "direction TEXT, status TEXT, turns INTEGER DEFAULT 0, "
            "created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);"
        )
    db.commit()


def candidates(db, limit: int = _DEFAULT_LIMIT, include_permits: bool = False,
               company: str = "construction") -> list:
    """Open, real-numbered leads that were never dialled, best first.

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
           AND NOT EXISTS (SELECT 1 FROM bot_calls b WHERE b.lead_id = l.id)
           AND NOT EXISTS (
                 SELECT 1 FROM comms_logs c
                  WHERE c.channel = 'voice' AND c.direction = 'outbound'
                    AND right(regexp_replace(coalesce(c.recipient,''), '\\D', '', 'g'), 10)
                        = right(regexp_replace(coalesce(l.phone,''), '\\D', '', 'g'), 10))
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
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM bot_calls "
                "WHERE created_at >= (date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s) "
                "  AND direction = 'outbound';", (_CALL_TZ, _CALL_TZ))
            row = cur.fetchone() or {}
        return int(row.get("n") or 0)
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return 0


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
              "skipped": [], "considered": 0, "configured": False}

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

        sid = _dial(wire, to, lead_id, base)
        if not sid:
            report["skipped"].append({**label, "reason": "carrier did not accept the call"})
            continue
        _record(db, sid, lead_id, to)
        report["dialed"].append({**label, "call_sid": sid})
        if len(report["dialed"]) >= want:
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


def start_scheduler():
    """One batch a day, inside calling hours, if the owner turned it on.

    Off unless BOTH ``OUTBOUND_CAMPAIGN_ENABLED`` and
    ``OUTBOUND_CAMPAIGN_SCHEDULED`` are true. The date row in
    ``outbound_campaign_runs`` makes a restart at 09:10 harmless.
    """
    if not (enabled() and _flag("OUTBOUND_CAMPAIGN_SCHEDULED")):
        return None
    import threading
    import time

    def _loop():
        hour = _int_env("OUTBOUND_CAMPAIGN_HOUR", 9, 0, 23)
        while True:
            try:
                from zoneinfo import ZoneInfo
                now = datetime.now(ZoneInfo(_CALL_TZ))
                if now.weekday() != 6 and now.hour == hour and now.minute < 20:
                    db_url = (os.getenv("DATABASE_URL") or "").strip()
                    if db_url:
                        import psycopg
                        from psycopg.rows import dict_row
                        with psycopg.connect(db_url, row_factory=dict_row) as db:
                            if not ran_today(db):
                                result = run(
                                    db, limit=daily_limit(), apply=True,
                                    include_permits=_flag("OUTBOUND_CAMPAIGN_INCLUDE_PERMITS"))
                                _log_run(db, result.get("considered", 0),
                                         len(result.get("dialed") or []),
                                         json.dumps(result.get("skipped") or [], default=str))
                                print(f"📞[campaign] daily run: dialed {len(result.get('dialed') or [])} "
                                      f"of {result.get('considered', 0)} considered", flush=True)
            except Exception as exc:
                print(f"📞[campaign] daily run failed: {exc}", flush=True)
            time.sleep(300)

    thread = threading.Thread(target=_loop, daemon=True, name="outbound-campaign")
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
