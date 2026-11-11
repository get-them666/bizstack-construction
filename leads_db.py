"""SQLite lead mirror — no homeowner inquiry is ever lost.

Why this exists
---------------
The production database is Postgres on Render. If Postgres is unreachable or the
Render deploy is mid-roll, a lead form submission could be lost. This module
writes every lead to a local SQLite file (`leads.db`) as a secondary record, so
even if the main pipeline fails, the inquiry is on disk.

It is intentionally simple: one table, one insert, no ORM. The admin page reads
from it directly.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leads.db")
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def ensure_schema():
    """Create the leads table if it doesn't exist. Safe to call at every boot."""
    with _lock:
        conn = _conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'website',
                    name TEXT NOT NULL DEFAULT '',
                    phone TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    address TEXT NOT NULL DEFAULT '',
                    city TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT '',
                    zip TEXT NOT NULL DEFAULT '',
                    project_type TEXT NOT NULL DEFAULT '',
                    goal TEXT NOT NULL DEFAULT '',
                    budget TEXT NOT NULL DEFAULT '',
                    timeline TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'new',
                    ip TEXT NOT NULL DEFAULT '',
                    user_agent TEXT NOT NULL DEFAULT ''
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at DESC);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
            """)
            conn.commit()
        finally:
            conn.close()


def insert_lead(
    *,
    name: str = "",
    phone: str = "",
    email: str = "",
    address: str = "",
    project_type: str = "",
    goal: str = "",
    budget: str = "",
    timeline: str = "",
    message: str = "",
    source: str = "website",
    ip: str = "",
    user_agent: str = "",
) -> int:
    """Write one lead to SQLite. Returns the new row id.

    Parses city/state/zip from the address when possible, so the admin page
    can filter by location without a separate field on the form."""
    city, state, zip_code = _parse_location(address)
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                """INSERT INTO leads
                   (created_at, source, name, phone, email, address,
                    city, state, zip, project_type, goal, budget,
                    timeline, message, status, ip, user_agent)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    source,
                    (name or "").strip(),
                    (phone or "").strip(),
                    (email or "").strip().lower(),
                    (address or "").strip(),
                    city, state, zip_code,
                    (project_type or "").strip(),
                    (goal or "").strip(),
                    (budget or "").strip(),
                    (timeline or "").strip(),
                    (message or "").strip(),
                    "new",
                    ip, user_agent,
                ),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def list_leads(*, status: str | None = None, limit: int = 200, offset: int = 0) -> list:
    """Return leads ordered newest-first. Pass status='new' for unread."""
    with _lock:
        conn = _conn()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM leads WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?;",
                    (status, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM leads ORDER BY created_at DESC LIMIT ? OFFSET ?;",
                    (limit, offset),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def count_leads(*, status: str | None = None) -> int:
    with _lock:
        conn = _conn()
        try:
            if status:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM leads WHERE status = ?;", (status,),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS c FROM leads;").fetchone()
            return int(row["c"]) if row else 0
        finally:
            conn.close()


def mark_read(lead_id: int):
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE leads SET status = 'read' WHERE id = ?;", (lead_id,))
            conn.commit()
        finally:
            conn.close()


def mark_all_read():
    with _lock:
        conn = _conn()
        try:
            conn.execute("UPDATE leads SET status = 'read' WHERE status = 'new';")
            conn.commit()
        finally:
            conn.close()


def _parse_location(address: str) -> tuple:
    """Pull city, state, zip from a free-form address string.

    Handles '123 Main St, Virginia Beach, VA 23451' and similar US formats.
    Returns ('', '', '') when nothing parses."""
    addr = (address or "").strip()
    if not addr:
        return "", "", ""
    # Zip: 5 digits at the end, possibly after a state abbreviation.
    zip_code = ""
    m = _ZIP_RE.search(addr)
    if m:
        zip_code = m.group(1)
        addr = addr[: m.start()].rstrip(" ,")
    # State: last 2-letter token after a comma.
    state = ""
    city = ""
    parts = [p.strip() for p in addr.split(",")]
    if len(parts) >= 2:
        candidate = parts[-1].upper()
        if len(candidate) == 2 and candidate.isalpha():
            state = candidate
            city = parts[-2]
        else:
            city = parts[-1]
    elif len(parts) == 1:
        city = parts[0]
    return city, state, zip_code


import re as _re
_ZIP_RE = _re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")
