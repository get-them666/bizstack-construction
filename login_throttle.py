"""Brute-force lockout for crew phone+PIN login.

A 4-6 digit PIN is guessable in minutes without this. Failures are counted per
phone number AND per client IP; after MAX_FAILS the key is locked for
LOCK_MINUTES, doubling on each further lockout. A successful login clears the
phone key. Stored in Postgres so a restart or redeploy does not reset it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

MAX_FAILS = 5
LOCK_MINUTES = 15
MAX_LOCK_MINUTES = 24 * 60


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS login_throttle (
                key VARCHAR(120) PRIMARY KEY,
                fails INTEGER NOT NULL DEFAULT 0,
                lockouts INTEGER NOT NULL DEFAULT 0,
                locked_until TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
    conn.commit()


def _now():
    return datetime.now(timezone.utc)


def locked(conn, *keys: str) -> int:
    """Seconds remaining on the longest active lock across keys, 0 if none."""
    keys = [k for k in keys if k]
    if not keys:
        return 0
    with conn.cursor() as cur:
        cur.execute("SELECT locked_until FROM login_throttle WHERE key = ANY(%s) AND locked_until > now();", (keys,))
        rows = cur.fetchall()
    if not rows:
        return 0
    latest = max(r["locked_until"] for r in rows)
    return max(1, int((latest - _now()).total_seconds()))


def fail(conn, *keys: str) -> int:
    """Record a failed attempt on each key. Returns lock seconds if one just engaged."""
    engaged = 0
    with conn.cursor() as cur:
        for k in keys:
            if not k:
                continue
            cur.execute("""
                INSERT INTO login_throttle (key, fails) VALUES (%s, 1)
                ON CONFLICT (key) DO UPDATE SET
                    fails = CASE WHEN login_throttle.updated_at < now() - interval '1 hour' THEN 1 ELSE login_throttle.fails + 1 END,
                    updated_at = now()
                RETURNING fails, lockouts;
            """, (k,))
            row = cur.fetchone()
            if row["fails"] >= MAX_FAILS:
                minutes = min(MAX_LOCK_MINUTES, LOCK_MINUTES * (2 ** int(row["lockouts"])))
                cur.execute("UPDATE login_throttle SET locked_until = now() + make_interval(mins => %s), "
                            "lockouts = lockouts + 1, fails = 0 WHERE key = %s;", (minutes, k))
                engaged = max(engaged, minutes * 60)
    conn.commit()
    return engaged


def clear(conn, *keys: str) -> None:
    keys = [k for k in keys if k]
    if not keys:
        return
    with conn.cursor() as cur:
        cur.execute("DELETE FROM login_throttle WHERE key = ANY(%s);", (keys,))
    conn.commit()


def human(seconds: int) -> str:
    m = max(1, (seconds + 59) // 60)
    return f"{m} minute{'s' if m != 1 else ''}" if m < 120 else f"{m // 60} hours"
