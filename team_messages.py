"""Team messages: owner <-> crew DMs and owner announcements.

The crew PWA has been able to send a DM "to the office" since the fork
(POST /api/crew/app/messages), but nothing on the office side ever read the
`messages` table, so every crew message disappeared. This module gives the
owner a /messages page and gives both sides one visibility rule, one
serializer and one authenticated WebSocket.

Independent of the hosts app on purpose: its own table (created here, not
inherited from the shared-database days), two roles only -- `admin` (the
owner, from the admin session) and `crew` (the `crew` table, from the
worker_session cookie) -- and no company scope. Rows that predate this
module may carry author_role='worker' or company='broom'; they are read
tolerantly and never written.

Visibility:
  announcement  -> everyone (the only scope is "the whole crew")
  dm            -> its author, its recipient, and -- when addressed to the
                   office (recipient_role='admin') -- every admin. Single
                   owner app: the office inbox is shared by all admins.
"""
from __future__ import annotations

import datetime as _dt

ADMIN = "admin"
CREW = "crew"
BODY_MAX = 4000

_SCHEMA_READY = False


def ensure_schema(conn, force: bool = False) -> None:
    """Idempotent DDL for the messages table. Safe on the forked live table.

    On the production database `messages` already exists (forked from the
    shared DB) with company DEFAULT 'broom'; the ALTERs below add any column
    this module needs and move the default to 'construction'. On a fresh
    database the CREATE builds the whole thing.
    """
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                author_role VARCHAR(20) NOT NULL,
                author_id INTEGER,
                author_name VARCHAR(160),
                audience VARCHAR(20) NOT NULL DEFAULT 'office',
                body TEXT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        for ddl in (
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'announcement';",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS recipient_role VARCHAR(20);",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS recipient_id INTEGER;",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS recipient_name VARCHAR(160);",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS scope VARCHAR(20) NOT NULL DEFAULT 'all';",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS company VARCHAR(20) NOT NULL DEFAULT 'construction';",
            "ALTER TABLE messages ALTER COLUMN company SET DEFAULT 'construction';",
            "CREATE INDEX IF NOT EXISTS idx_messages_id ON messages(id);",
            "CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient_role, recipient_id);",
        ):
            cur.execute(ddl)
    conn.commit()
    _SCHEMA_READY = True


# --- actors ------------------------------------------------------------------

def normalize_actor(session_actor: dict | None, worker_actor: dict | None) -> dict | None:
    """Collapse the app's two session types into {role, id, name}.

    session_actor is current_actor(request) (admin/customer cookie);
    worker_actor is current_worker(request) (crew cookie, role 'worker').
    Only admins and crew can message; a customer session gets None.
    """
    if session_actor and session_actor.get("role") == ADMIN:
        return {
            "role": ADMIN,
            "id": int(session_actor.get("id") or 0),
            "name": (session_actor.get("name") or session_actor.get("email") or "Office").strip() or "Office",
        }
    if worker_actor and worker_actor.get("role") in ("worker", CREW):
        return {
            "role": CREW,
            "id": int(worker_actor.get("id") or 0),
            "name": (worker_actor.get("name") or "Crew").strip() or "Crew",
        }
    return None


def _same_role(a: str | None, b: str | None) -> bool:
    """'worker' rows written before this module count as crew."""
    norm = {"worker": CREW}
    return norm.get(a or "", a) == norm.get(b or "", b)


def visible(m: dict, actor: dict | None) -> bool:
    if not actor:
        return False
    kind = m.get("kind") or "announcement"
    if kind != "dm":
        return True
    if _same_role(m.get("author_role"), actor["role"]) and (m.get("author_id") or 0) == actor["id"]:
        return True
    rrole, rid = m.get("recipient_role"), m.get("recipient_id")
    if _same_role(rrole, actor["role"]):
        if actor["role"] == ADMIN:
            # Any admin reads the office inbox, addressed or not.
            return True
        return rid == actor["id"]
    # Admins also see DMs other admins sent, so the thread with a crew
    # member reads whole from the office side.
    return actor["role"] == ADMIN and _same_role(m.get("author_role"), ADMIN)


def serialize(m: dict) -> dict:
    """One payload for both the office page and the crew PWA.

    The PWA reads `author` ('crew'|'office'), `author_id`, `author_name`,
    `body`, `at`, `id`, `kind`; the office page reads the role/recipient
    fields and `created_at`. Superset, so neither client needs a translator.
    """
    created = m.get("created_at")
    if isinstance(created, _dt.datetime):
        created_iso = created.isoformat()
    else:
        created_iso = str(created or "")
    author_role = CREW if (m.get("author_role") in ("worker", CREW)) else (m.get("author_role") or ADMIN)
    return {
        "id": m["id"],
        "kind": m.get("kind") or "announcement",
        "author_role": author_role,
        "author": "crew" if author_role == CREW else "office",
        "author_id": m.get("author_id"),
        "author_name": m.get("author_name") or ("Office" if author_role == ADMIN else "Crew"),
        "recipient_role": CREW if m.get("recipient_role") == "worker" else m.get("recipient_role"),
        "recipient_id": m.get("recipient_id"),
        "recipient_name": m.get("recipient_name"),
        "scope": "all",
        "body": m.get("body") or "",
        "created_at": created_iso,
        "at": created_iso,
    }


# --- storage -----------------------------------------------------------------

def list_after(conn, after_id: int, actor: dict, limit: int = 500) -> list[dict]:
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM messages WHERE id > %s ORDER BY id ASC LIMIT %s;",
            (int(after_id or 0), int(limit)),
        )
        rows = cur.fetchall()
    return [serialize(m) for m in rows if visible(m, actor)]


def roster(conn) -> list[dict]:
    """Who the owner can DM: every admin account plus every active crew member."""
    out: list[dict] = []
    with conn.cursor() as cur:
        cur.execute("SELECT id, email, COALESCE(name, '') AS name FROM users WHERE role = 'admin' ORDER BY id;")
        for u in cur.fetchall():
            out.append({"role": ADMIN, "id": u["id"], "name": u["name"] or u["email"]})
        cur.execute("SELECT id, name FROM crew WHERE is_active = TRUE ORDER BY name;")
        for c in cur.fetchall():
            out.append({"role": CREW, "id": c["id"], "name": c["name"]})
    return out


def crew_name(conn, crew_id: int) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM crew WHERE id = %s LIMIT 1;", (int(crew_id),))
        row = cur.fetchone()
    return row["name"] if row else None


def send(conn, actor: dict, body: str, *, kind: str = "dm",
         to_role: str = "", to_id: str | int = "") -> dict:
    """Write one message and return its serialized row.

    Crew can only DM the office. Admins may announce to everyone or DM one
    crew member (or the office inbox, which other admins read).
    """
    ensure_schema(conn)
    text = (body or "").strip()[:BODY_MAX]
    if not text:
        raise ValueError("Message is empty")
    kind = kind if kind in ("dm", "announcement") else "dm"
    if actor["role"] != ADMIN:
        kind, to_role, to_id = "dm", ADMIN, ""
    recipient_role = recipient_id = recipient_name = None
    if kind == "dm":
        if to_role in ("", ADMIN):
            recipient_role, recipient_name = ADMIN, "Office"
        elif to_role in (CREW, "worker"):
            rid = int(to_id) if str(to_id).isdigit() else None
            if rid is None:
                raise ValueError("Pick a crew member")
            recipient_role, recipient_id = CREW, rid
            recipient_name = crew_name(conn, rid) or "Crew"
        else:
            raise ValueError("Unknown recipient")
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO messages (author_role, author_id, author_name, company, kind, audience, scope,
                                     recipient_role, recipient_id, recipient_name, body)
               VALUES (%s, %s, %s, 'construction', %s, 'office', 'all', %s, %s, %s, %s)
               RETURNING *;""",
            (actor["role"], actor["id"], actor["name"], kind,
             recipient_role, recipient_id, recipient_name, text),
        )
        row = cur.fetchone()
    conn.commit()
    return serialize(row)


def clear(conn) -> int:
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM messages;")
        n = cur.rowcount
    conn.commit()
    return n
