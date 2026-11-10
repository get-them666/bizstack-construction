"""Tidy webhook receiver for Broom Service.

Why this exists
---------------
Tidy pushes job events to a URL so a system can react to a cleaning being
scheduled, started, completed, cancelled or failed. The Broom app had no such
endpoint, so the webhook form in Tidy had nothing to point at.

Tidy does not sign its deliveries. Their docs are explicit: the endpoint
receives "unauthenticated POST requests". There is therefore no HMAC to check,
and anyone who learns the URL can post fake job events. Two consequences:

* **The URL carries a secret.** The path is /api/webhooks/tidy/<token>, and the
  token comes from TIDY_WEBHOOK_TOKEN. Unset means the route 404s, so a
  forgotten deploy fails closed instead of accepting the world.
* **Treat a delivery as a claim, not a fact.** Nothing here should move money or
  close a customer's job purely because a POST arrived; store it and act on it
  deliberately.

Retries: Tidy retries a failed delivery 10 times, backing off to about a week,
so the handler must answer 2xx immediately and do any work afterwards. The read
below is a single INSERT, which is why it is done inline.

Event shape (their docs and sample):
    {"uuid": "...", "name": "job.completed",
     "data": {"object": {...}, "object_previous_attributes": {...}}}
"""

from __future__ import annotations

import hmac
import json
import os

EVENTS = (
    "job.scheduled",
    "job.in_progress",
    "job.completed",
    "job.cancelled",
    "job.failed",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tidy_events (
    id          SERIAL PRIMARY KEY,
    uuid        TEXT,
    event_name  TEXT NOT NULL,
    job_ref     TEXT,
    payload     JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed   BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_tidy_events_name ON tidy_events(event_name, received_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tidy_events_uuid ON tidy_events(uuid) WHERE uuid IS NOT NULL;
"""


def token() -> str:
    return (os.getenv("TIDY_WEBHOOK_TOKEN") or "").strip()


def token_ok(given: str) -> bool:
    """Constant-time compare, and fail closed when unconfigured."""
    expected = token()
    if not expected:
        return False
    return hmac.compare_digest((given or "").strip(), expected)


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()


def record(conn, payload: dict) -> dict:
    """Store one delivery.

    Tidy may deliver the same uuid twice (a retry after a slow response), so the
    uuid index is unique and a repeat is a no-op rather than a second row.
    """
    name = str(payload.get("name") or payload.get("type") or "unknown")
    uuid = (str(payload.get("uuid") or "").strip() or None)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    obj = data.get("object") if isinstance(data, dict) else {}
    job_ref = ""
    if isinstance(obj, dict):
        job_ref = str(obj.get("id") or obj.get("job_id") or obj.get("jobId") or "")[:80]

    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tidy_events (uuid, event_name, job_ref, payload)
            VALUES (%s, %s, %s, %s::jsonb)
            ON CONFLICT (uuid) WHERE uuid IS NOT NULL DO NOTHING
            RETURNING id;
            """,
            (uuid, name, job_ref, json.dumps(payload)),
        )
        row = cur.fetchone()
    conn.commit()
    return {"ok": True, "stored": bool(row), "event": name}
