"""Server-side conversation memory for the AI assistants.

Both the owner Copilot and the public web widget were previously stateless: the
page POSTed only the current message and `process_inbound_text` built
`[system, user]` from it every time. The transcript on screen was browser-only
decoration, so the model received exactly one turn per request and could not
remember what had just been said -- hence "it asked a question, I answered, and
it asked what I was talking about."

History now lives in Postgres and is replayed into the model on every turn, the
same approach the Vapi voice path uses via `_vapi_messages_to_openai`.

Two audiences, deliberately kept in one table with separate retention:
- ``copilot``  — the owner. Keyed by signed-in email, retained (see RETENTION_DAYS).
- ``webchat``  — anonymous site visitors. Keyed by an opaque session cookie.
  Visitors type real addresses, names and phone numbers, so these rows carry a
  hard expiry and are swept on boot and on every write.

Follows the same self-initializing `CREATE TABLE IF NOT EXISTS` pattern as
`loan_outreach.py` and `materials_service.py`.
"""

from datetime import datetime, timedelta, timezone

# Roughly MAX_HISTORY_MESSAGES/2 exchanges of context. Kept modest because the
# system prompt already carries the full knowledge base.
MAX_HISTORY_MESSAGES = 40

# Anonymous visitor transcripts. The owner chose 30 days -- long enough to pick a
# conversation back up, short enough that a stranger's home address and phone
# number are not held indefinitely.
RETENTION_DAYS = {"webchat": 30, "copilot": 3650}

PURGE_BATCH = 5000


def retention_days(kind: str) -> int:
    return RETENTION_DAYS.get(kind, 30)


def _ensure_schema(conn) -> None:
    """Create the conversation table. Safe to call on every boot."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS copilot_messages (
                id SERIAL PRIMARY KEY,
                kind VARCHAR(20) NOT NULL DEFAULT 'copilot',
                session_key VARCHAR(255) NOT NULL,
                role VARCHAR(20) NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                expires_at TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_copilot_messages_session "
            "ON copilot_messages (kind, session_key, id);"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_copilot_messages_expiry "
            "ON copilot_messages (expires_at);"
        )
    conn.commit()


def load_history(conn, kind: str, session_key: str, limit: int = MAX_HISTORY_MESSAGES) -> list:
    """Return the most recent messages as OpenAI-format dicts, oldest first.

    Trims to the newest `limit` rows then walks forward dropping any leading
    orphan assistant reply, so a replayed conversation always starts with a user
    turn rather than an assistant message the model has no cause for.
    """
    if not session_key:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT role, content FROM copilot_messages "
            "WHERE kind = %s AND session_key = %s "
            "  AND (expires_at IS NULL OR expires_at > NOW()) "
            "ORDER BY id DESC LIMIT %s;",
            (kind, session_key, limit),
        )
        rows = cur.fetchall()

    history = [{"role": r["role"], "content": r["content"] or ""} for r in reversed(rows)]
    while history and history[0]["role"] != "user":
        history.pop(0)
    return history


def append_turn(conn, kind: str, session_key: str, role: str, content: str) -> None:
    """Persist one message, stamped with its retention expiry. No-ops when empty."""
    if not session_key or not (content or "").strip():
        return
    expires_at = datetime.now(timezone.utc) + timedelta(days=retention_days(kind))
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO copilot_messages (kind, session_key, role, content, expires_at) "
            "VALUES (%s, %s, %s, %s, %s);",
            (kind, session_key, role, content, expires_at),
        )
    conn.commit()


def clear_history(conn, kind: str, session_key: str) -> int:
    """Delete one conversation. Returns the number of rows removed."""
    if not session_key:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM copilot_messages WHERE kind = %s AND session_key = %s;",
            (kind, session_key),
        )
        removed = cur.rowcount or 0
    conn.commit()
    return removed


def purge_expired(conn, limit: int = PURGE_BATCH) -> int:
    """Delete rows past their expiry. Returns the number of rows removed.

    Called on boot and after each visitor turn so retention holds even if the
    process never restarts.
    """
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM copilot_messages WHERE id IN ("
            "  SELECT id FROM copilot_messages "
            "  WHERE expires_at IS NOT NULL AND expires_at <= NOW() "
            "  LIMIT %s"
            ");",
            (limit,),
        )
        removed = cur.rowcount or 0
    conn.commit()
    return removed