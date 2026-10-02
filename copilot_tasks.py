"""Durable task memory for the owner Copilot.

`copilot_memory` holds the rolling conversation transcript (40 messages). That is
the wrong store for "remind me to call the Kelleys back" -- a task the owner
raises today may not come up again for months, and must survive being mentioned
once in a chat that has long since scrolled away.

This is that store: a persistent, searchable task list with due dates and
completion state, retained for a year.

Self-initializing `CREATE TABLE IF NOT EXISTS`, matching `loan_outreach.py`,
`materials_service.py` and `copilot_memory`.
"""

# One year, per owner instruction. Completed tasks stay searchable inside the
# window rather than being deleted on completion, so Copilot can still answer
# "did I ever send that COI?" months later.
RETENTION_DAYS = 365


def _ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS copilot_tasks (
                id SERIAL PRIMARY KEY,
                owner_email VARCHAR(255) NOT NULL,
                title TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                category VARCHAR(40) NOT NULL DEFAULT 'general',
                entity_type VARCHAR(40) NOT NULL DEFAULT '',
                entity_id VARCHAR(40) NOT NULL DEFAULT '',
                due_at TIMESTAMP WITH TIME ZONE,
                status VARCHAR(20) NOT NULL DEFAULT 'open',
                result TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP WITH TIME ZONE
            );
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_copilot_tasks_owner "
            "ON copilot_tasks (owner_email, status, due_at);"
        )
    conn.commit()


def add_task(
    conn,
    owner_email: str,
    title: str,
    detail: str = "",
    category: str = "general",
    due_at=None,
    entity_type: str = "",
    entity_id: str = "",
) -> int:
    """Create a task and return its id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO copilot_tasks "
            "  (owner_email, title, detail, category, entity_type, entity_id, due_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id;",
            (owner_email, title, detail, category, entity_type, entity_id, due_at),
        )
        task_id = cur.fetchone()["id"]
    conn.commit()
    return task_id


def update_task(conn, owner_email: str, task_id: int, status: str, result: str = "") -> bool:
    """Set status (open/done/dropped) and optionally record the outcome.

    Returns False when the id is not one of this owner's tasks, so the model gets
    a truthful "not found" instead of a silent no-op.
    """
    if status not in ("open", "done", "dropped"):
        raise ValueError("status must be open, done or dropped")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE copilot_tasks SET status = %s, result = %s, "
            "  completed_at = CASE WHEN %s = 'open' THEN NULL ELSE NOW() END "
            "WHERE id = %s AND owner_email = %s RETURNING id;",
            (status, result, status, task_id, owner_email),
        )
        row = cur.fetchone()
    conn.commit()
    return row is not None


def list_tasks(
    conn,
    owner_email: str,
    status: str = "open",
    category: str = "",
    limit: int = 50,
) -> list:
    """List this owner's tasks, soonest due first, then newest."""
    sql = (
        "SELECT id, title, detail, category, entity_type, entity_id, due_at, status, "
        "       result, created_at, completed_at "
        "FROM copilot_tasks WHERE owner_email = %s"
    )
    params: list = [owner_email]
    if status and status != "all":
        sql += " AND status = %s"
        params.append(status)
    if category:
        sql += " AND category = %s"
        params.append(category)
    sql += " ORDER BY due_at NULLS LAST, id DESC LIMIT %s;"
    params.append(limit)

    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()

    out = []
    for r in rows:
        d = dict(r)
        for field in ("due_at", "created_at", "completed_at"):
            d[field] = d[field].isoformat() if d.get(field) else ""
        out.append(d)
    return out


def purge_expired(conn, limit: int = 5000) -> int:
    """Drop tasks past the one-year retention window."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM copilot_tasks WHERE id IN ("
            "  SELECT id FROM copilot_tasks "
            "  WHERE created_at < NOW() - INTERVAL '%s days' LIMIT %s"
            ");",
            (RETENTION_DAYS, limit),
        )
        removed = cur.rowcount or 0
    conn.commit()
    return removed