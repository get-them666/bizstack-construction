"""Auth0 login bridge: who is allowed in, and what role they carry.

Auth0 proves identity. It does not know who your crew are -- that lives in
`crew` and in whatever you use for owner accounts. This module is the
question Auth0 asks on every login: "is this person one of ours, and are
they an owner or crew?"

The answer comes back as {"allowed": bool, "role": ...} and an Auth0 Action
turns that into either a custom claim or a denied login.

WHY A SHARED SECRET AND NOT A JWT
----------------------------------
An Action could call this with a client-credentials token and have it
verified properly. That is more correct, and it is not what is used here:
the caller is one fixed Action in your own tenant, the secret never leaves
Auth0's secret store or this process, and a constant-time string compare is
a far smaller thing to get wrong than JWKS fetching, key rotation and
clock-skew handling in the middle of a login.

What that trade costs, stated plainly: the secret is shared, so it cannot
prove *which* Action called -- only that someone holding it did. It is a
membership test, not an identity assertion. If you later add a second
caller (a second app, a script), give it its own secret rather than
reusing this one, so the log tells you which one called.

And the thing that actually matters: this endpoint must never be able to
mint a session. It answers a question. It does not log anyone in.
"""
from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone

ROLE_ADMIN = "admin"
ROLE_CREW = "crew"

HEADER = "Authorization"
TOKEN_PREFIX = "Bearer "


def _secret() -> str:
    return (os.getenv("AUTH0_API_SECRET") or "").strip()


def configured() -> bool:
    return bool(_secret())


def authorized(provided: str | None) -> bool:
    """Constant-time check of the shared secret.

    Returns False for a missing header rather than falling back to a default.
    A default secret would mean an unconfigured deploy silently accepts
    anyone, which is the worst possible failure for a login endpoint.
    """
    want = _secret()
    if not want or not provided:
        return False
    token = provided[len(TOKEN_PREFIX):].strip() if provided.startswith(TOKEN_PREFIX) else provided.strip()
    if not token:
        return False
    return hmac.compare_digest(token, want)


# --- storage ----------------------------------------------------------------
def ensure_schema(conn) -> None:
    conn.cursor().execute("""
        CREATE TABLE IF NOT EXISTS auth_logins (
            id SERIAL PRIMARY KEY,
            auth0_sub VARCHAR(120),
            email VARCHAR(255),
            name VARCHAR(255),
            role VARCHAR(40),
            allowed BOOLEAN DEFAULT TRUE,
            reason TEXT,
            user_agent TEXT,
            ip VARCHAR(64),
            seen_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.cursor().execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_logins_email ON auth_logins(lower(email))")
    conn.cursor().execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_logins_seen ON auth_logins(seen_at DESC)")
    conn.commit()


def record(conn, email: str, auth0_sub: str = "", name: str = "", role: str = "",
           allowed: bool = True, reason: str = "", user_agent: str = "",
           ip: str = "") -> None:
    """Every login attempt lands, allowed or not.

    Denied attempts are the ones worth keeping. A spike in them is either
    someone guessing at the allowlist or someone you removed who has not
    noticed yet.
    """
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO auth_logins
                (auth0_sub, email, name, role, allowed, reason, user_agent, ip)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (str(auth0_sub or "")[:120], (email or "").strip().lower()[:255],
              (name or "")[:255], role or None, bool(allowed), (reason or "")[:500] or None,
              (user_agent or "")[:300] or None, (ip or "")[:64] or None))
    conn.commit()


# --- the allowlist ----------------------------------------------------------
def allowlist() -> dict:
    """Owner emails, from env. Comma separated.

    Kept out of the database on purpose: this is the list that decides who
    reaches the books. It should be visible in one place in a deploy
    variable, not a table someone can edit by accident.
    """
    raw = os.getenv("AUTH0_ADMIN_EMAILS", "") or ""
    owners = {e.strip().lower() for e in raw.split(",") if e.strip()}
    return {"owners": owners, "crew_from_table": os.getenv("AUTH0_CREW_VIA_TABLE", "true").lower() in ("1", "true", "yes")}


def is_owner(email: str) -> bool:
    return bool(email) and email.strip().lower() in allowlist()["owners"]


def crew_role_for(conn, email: str) -> str | None:
    """Match a login against the crew table. Only active crew count."""
    if not email or not email.strip():
        return None
    if not allowlist()["crew_from_table"]:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT role FROM crew
                WHERE lower(email) = lower(%s)
                  AND (is_active IS NULL OR is_active)
                LIMIT 1
            """, (email.strip(),))
            row = cur.fetchone()
    except Exception:
        # A missing crew table must not lock you out of your own site.
        return None
    if not row:
        return None
    r = (row["role"] or "crew").strip().lower()
    return ROLE_CREW if r in ("crew", "worker", "employee") else r


def decide(conn, email: str, name: str = "", auth0_sub: str = "",
           user_agent: str = "", ip: str = "") -> dict:
    """The answer Auth0 acts on. Always records, then answers."""
    email = (email or "").strip().lower()
    if not email:
        record(conn, "", auth0_sub, name, "", allowed=False,
               reason="no email supplied", user_agent=user_agent, ip=ip)
        return {"allowed": False, "role": None, "reason": "no email supplied"}

    if is_owner(email):
        record(conn, email, auth0_sub, name, ROLE_ADMIN, allowed=True,
               user_agent=user_agent, ip=ip)
        return {"allowed": True, "role": ROLE_ADMIN, "name": name or email}

    role = crew_role_for(conn, email)
    if role:
        record(conn, email, auth0_sub, name, role, allowed=True,
               user_agent=user_agent, ip=ip)
        return {"allowed": True, "role": role, "name": name or email}

    record(conn, email, auth0_sub, name, "", allowed=False,
           reason="not on the owner list and not active crew",
           user_agent=user_agent, ip=ip)
    return {"allowed": False, "role": None,
            "reason": "not on the owner list and not active crew"}


def recent(conn, limit: int = 50) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT email, name, role, allowed, reason, seen_at "
                    "FROM auth_logins ORDER BY seen_at DESC LIMIT %s", (limit,))
        return cur.fetchall()


def denied_recent(conn, limit: int = 25) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT email, reason, ip, seen_at FROM auth_logins "
                    "WHERE NOT allowed ORDER BY seen_at DESC LIMIT %s", (limit,))
        return cur.fetchall()