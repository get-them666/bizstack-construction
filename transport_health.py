"""Track outbound transport health and notice when one dies silently.

Why this exists
---------------
SignalWire SMS stopped working on 2026-10-29 with

    422 inactive_campaign: From number must belong to an active campaign

and nothing raised it. The bot simply stopped texting, follow-ups were skipped
(`{'due': 4, 'sent': 0}`), and the only evidence was a line buried in the logs.
A transport that fails is not the same as a transport that is quiet, but from the
outside they look identical -- which is exactly how three weeks of silence went
unnoticed.

So every send records its outcome here, and a loop compares the stored state to
what it has already alerted on. One email on the way down, one on the way back up.
It is deliberately not one email per failure: the point is to be told once, not
to be buried.

Design notes
------------
* ``record`` opens its own short-lived connection from DATABASE_URL rather than
  taking one, because its callers (signalwire_service) are transport clients
  that hold no database handle. SMS volume here is single digits per day, so the
  cost of a connect on send is irrelevant.
* Two consecutive failures before alerting, so one bad phone number does not
  page anyone. Provider-level faults fail every send in a row, so they cross the
  threshold within minutes.
* ``maybe_alert`` takes a notifier callable instead of importing the email layer,
  which keeps this module testable and free of a circular import.
"""

from __future__ import annotations

import os

import psycopg
from psycopg.rows import dict_row

FAILURES_BEFORE_ALERT = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS transport_health (
    channel             TEXT PRIMARY KEY,
    ok                  BOOLEAN NOT NULL DEFAULT TRUE,
    detail              TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    first_failure_at    TIMESTAMPTZ,
    checked_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    alerted_state       TEXT
);
"""


def _connect():
    return psycopg.connect(os.getenv("DATABASE_URL", ""), row_factory=dict_row,
                           connect_timeout=5)


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()


def record(channel: str, ok: bool, detail: str = "") -> None:
    """Note the outcome of one send. Never raises -- a monitoring failure must
    not be allowed to break the send it was only supposed to observe."""
    try:
        with _connect() as conn:
            ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO transport_health
                        (channel, ok, detail, consecutive_failures, first_failure_at, checked_at)
                    VALUES (%s, %s, %s, %s, CASE WHEN %s THEN now() END, now())
                    ON CONFLICT (channel) DO UPDATE SET
                        ok = EXCLUDED.ok,
                        detail = EXCLUDED.detail,
                        consecutive_failures = CASE WHEN EXCLUDED.ok THEN 0
                                                    ELSE transport_health.consecutive_failures + 1 END,
                        first_failure_at = CASE WHEN EXCLUDED.ok THEN NULL
                                                ELSE COALESCE(transport_health.first_failure_at, now()) END,
                        checked_at = now();
                """, (channel, ok, (detail or "")[:500], 0 if ok else 1, not ok))
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - monitoring must never propagate
        print(f"[transport-health] record failed: {exc}", flush=True)


def status(conn) -> list[dict]:
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM transport_health ORDER BY channel;")
        return cur.fetchall()


def maybe_alert(notify) -> list[dict]:
    """Email once on the transition down and once on the way back up.

    ``notify(channel, ok, detail)`` is called only when the state actually
    changes from what the owner was last told, so repeated failures stay quiet.
    """
    sent: list[dict] = []
    try:
        with _connect() as conn:
            ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM transport_health ORDER BY channel;")
                rows = cur.fetchall()
                for r in rows:
                    down = (not r["ok"]) and r["consecutive_failures"] >= FAILURES_BEFORE_ALERT
                    if down and r["alerted_state"] != "down":
                        try:
                            notify(r["channel"], False, r.get("detail") or "")
                            sent.append({**r, "alert": "down"})
                        except Exception as exc:  # noqa: BLE001
                            print(f"[transport-health] alert failed: {exc}", flush=True)
                            continue
                        cur.execute("UPDATE transport_health SET alerted_state='down' WHERE channel=%s;",
                                    (r["channel"],))
                    elif r["ok"] and r["alerted_state"] == "down":
                        try:
                            notify(r["channel"], True, "")
                            sent.append({**r, "alert": "up"})
                        except Exception as exc:  # noqa: BLE001
                            print(f"[transport-health] recovery alert failed: {exc}", flush=True)
                            continue
                        cur.execute("UPDATE transport_health SET alerted_state='up' WHERE channel=%s;",
                                    (r["channel"],))
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"[transport-health] maybe_alert failed: {exc}", flush=True)
    return sent
