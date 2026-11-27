"""Tell the owner when outbound email and calling go silent.

Why this exists
---------------
``transport_health`` notices a transport that *fails*. It cannot notice a
pipeline that simply stops producing work. Between 2026-11-23 and 2026-11-26
the daily campaign ran and dialled nobody, the enquiry follow-ups had nobody
due, and the enrichment provider was out of credits. No channel recorded a
failure, because no send was attempted. The only evidence was an absence, and
nothing in the app was watching for one.

This adds an ``outbound`` row to the same ``transport_health`` table, so the
existing one-email-on-transition alert fires. The signal is the latest outbound
email OR voice entry in ``comms_logs``. Manual sends count: a send is a send.

The alert detail names the likely causes -- a daily campaign that keeps
dialling zero and the enrichment balance -- so it is actionable rather than
just "quiet".
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

import transport_health

_TZ = ZoneInfo("America/New_York")
DEFAULT_SILENCE_HOURS = 48
CAMPAIGN_ZERO_RUNS = 3


def _connect():
    return psycopg.connect(os.getenv("DATABASE_URL", ""), row_factory=dict_row,
                           connect_timeout=5)


def silence_hours() -> int:
    try:
        return int(os.getenv("OUTBOUND_SILENCE_HOURS", str(DEFAULT_SILENCE_HOURS))
                   or DEFAULT_SILENCE_HOURS)
    except (TypeError, ValueError):
        return DEFAULT_SILENCE_HOURS


def _fmt(value) -> str:
    if not value:
        return "never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_TZ).strftime("%Y-%m-%d %H:%M ET")


def evaluate(*, last_email=None, last_call=None, now=None, hours=None, runs=()) -> dict:
    """Pure decision for the watchdog. ``runs`` is newest-first campaign rows."""
    now = now or datetime.now(timezone.utc)
    hours = silence_hours() if hours is None else int(hours)
    if hours <= 0:
        return {"ok": True, "disabled": True, "age_hours": None,
                "zero_streak": 0, "detail": "OUTBOUND_SILENCE_HOURS is 0"}

    latest = max((d for d in (last_email, last_call) if d is not None), default=None)
    age = None if latest is None else (now - latest).total_seconds() / 3600.0
    ok = latest is not None and age < hours

    zero_streak = 0
    for run in runs or ():
        try:
            if int(run.get("dialed") or 0) == 0:
                zero_streak += 1
            else:
                break
        except (TypeError, ValueError, AttributeError):
            break

    if ok:
        detail = (f"last outbound email {_fmt(last_email)}; "
                  f"last outbound call {_fmt(last_call)}")
    else:
        if age is None:
            detail = "no outbound email or call has ever been recorded"
        else:
            detail = (f"no outbound email or call for {age:.0f}h "
                      f"(last email {_fmt(last_email)}; last call {_fmt(last_call)})")
        if zero_streak >= CAMPAIGN_ZERO_RUNS:
            detail += (f"; the daily campaign dialled 0 on its last {zero_streak} runs — "
                       f"check lead contact data and enrichment credits")
        else:
            detail += "; check the enrichment credit balance and the daily campaign"
    return {"ok": ok, "disabled": False, "age_hours": age,
            "zero_streak": zero_streak, "detail": detail[:500]}


def last_sends(db) -> dict:
    with db.cursor() as cur:
        cur.execute(
            "SELECT channel, MAX(created_at) AS last_at FROM comms_logs "
            "WHERE direction = 'outbound' AND channel IN ('email','voice') "
            "GROUP BY channel;")
        rows = cur.fetchall() or []
    return {r["channel"]: r["last_at"] for r in rows}


def recent_runs(db, limit: int = CAMPAIGN_ZERO_RUNS) -> list:
    """Newest campaign rows, or [] when the table does not exist yet."""
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT run_date, considered, dialed FROM outbound_campaign_runs "
                "ORDER BY run_date DESC LIMIT %s;", (limit,))
            return list(cur.fetchall() or [])
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return []


def check_and_record() -> dict:
    """Read activity and mirror the verdict into transport_health.

    Never raises: a monitoring read must not break the loop that also alerts on
    every other transport. A database failure leaves the stored state alone
    rather than claiming the channel is healthy.
    """
    try:
        with _connect() as db:
            sends = last_sends(db)
            runs = recent_runs(db)
    except Exception as exc:  # noqa: BLE001
        print(f"[outbound-watchdog] could not read activity: {exc}", flush=True)
        return {"ok": True, "error": str(exc)}
    result = evaluate(last_email=sends.get("email"), last_call=sends.get("voice"),
                      runs=runs)
    transport_health.record("outbound", result["ok"], result["detail"])
    return result
