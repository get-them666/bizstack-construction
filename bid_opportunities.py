"""Bid opportunities — solicitations BuildStack is pursuing.

Deliberately separate from `leads`. The `leads` table is inbound-customer intake:
creating a row there fires `auto_reply.auto_reply_to_lead` and the outbound voice
agent (`_follow_up_new_lead`). A GC contact is not a customer, so a bid must never
land in that table. Nothing in this module sends email, dials, or schedules follow-up.

Own module with its own `ensure_schema()` rather than growing construction_main.py,
which is already 8k lines. Nothing here is imported by the startup DDL block, so
the ALTER-before-CREATE ordering trap at :393-409 cannot be reached.
"""

from __future__ import annotations

import os
import re
from datetime import date

# Bid lifecycle. `drafting` -> `sent` -> `shortlist` -> `won` / `lost` / `withdrawn`.
STATUSES = [
    "new",
    "drafting",
    "sent",
    "shortlist",
    "won",
    "lost",
    "withdrawn",
]

# Which role we are playing. Determines bonding, risk, and who holds the RFI.
ROLES = ["prime", "sub"]

ROLE_NOTES = {
    "prime": "We hold the contract. Bond, RFI, insurance and references are ours.",
    "sub": "Another GC holds the contract. We price a trade package to them.",
}


def _safe(name: str) -> str:
    """Quote an identifier for interpolation. psycopg has no identifier params."""
    if not re.fullmatch(r"[a-z_]+", name or ""):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


def ensure_schema(conn) -> None:
    """Idempotent. Safe to call on every boot."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bid_opportunities (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                owner VARCHAR(255),
                contact_name VARCHAR(255),
                contact_email VARCHAR(255),
                contact_phone VARCHAR(50),
                reference VARCHAR(120),
                role VARCHAR(20) DEFAULT 'sub',
                trade VARCHAR(120),
                location VARCHAR(255),
                due_date DATE,
                bid_amount NUMERIC(14,2),
                status VARCHAR(30) DEFAULT 'new',
                notes TEXT,
                source VARCHAR(40) DEFAULT 'manual',
                company VARCHAR(40) DEFAULT 'construction',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # ADDs only. Nothing is referenced by a CREATE in this module, so there is
        # no ordering constraint to violate the way the startup block has one.
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS owner VARCHAR(255);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS contact_name VARCHAR(255);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS contact_email VARCHAR(255);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS contact_phone VARCHAR(50);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS reference VARCHAR(120);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS role VARCHAR(20) DEFAULT 'sub';")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS trade VARCHAR(120);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS location VARCHAR(255);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS due_date DATE;")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS bid_amount NUMERIC(14,2);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS status VARCHAR(30) DEFAULT 'new';")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS notes TEXT;")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS source VARCHAR(40) DEFAULT 'manual';")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS company VARCHAR(40) DEFAULT 'construction';")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bid_opportunities_company ON bid_opportunities(company);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bid_opportunities_status ON bid_opportunities(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bid_opportunities_due ON bid_opportunities(due_date);")
        # Dedupe key so one row per solicitation + trade per company. Kept
        # because it is cheap and makes an importer idempotent later; rows are
        # entered through the board, not seeded at boot, so nothing in this
        # module carries live pipeline into version control.
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_bid_opportunities_ref_trade
                ON bid_opportunities(company, COALESCE(reference,''), COALESCE(trade,''))
        """)
    conn.commit()


def _db_url() -> str:
    return os.getenv("DATABASE_URL", "")


def list_bids(conn, company: str = "construction", status: str = "") -> list[dict]:
    sql = ["SELECT * FROM bid_opportunities WHERE company = %s"]
    params: list = [company]
    if status:
        sql.append("AND status = %s")
        params.append(status)
    sql.append("ORDER BY due_date NULLS LAST, id DESC")
    with conn.cursor() as cur:
        cur.execute(" ".join(sql), params)
        return cur.fetchall()


def get_bid(conn, bid_id: int, company: str = "construction") -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM bid_opportunities WHERE id = %s AND company = %s;",
            (bid_id, company),
        )
        return cur.fetchone()


def create_bid(conn, company: str = "construction", **f) -> int:
    cols = [
        "name", "owner", "contact_name", "contact_email", "contact_phone",
        "reference", "role", "trade", "location", "due_date", "bid_amount",
        "status", "notes",
    ]
    use = [_safe(c) for c in cols if c in f]
    if not use:
        raise ValueError("no writable fields")
    vals = [f[c] for c in use]
    placeholders = ",".join(["%s"] * len(use))
    sql = (f"INSERT INTO bid_opportunities ({','.join(use)}, company) "
           f"VALUES ({placeholders}, %s) RETURNING id")
    with conn.cursor() as cur:
        cur.execute(sql, vals + [company])
        row = cur.fetchone()
    conn.commit()
    return row["id"]


def set_status(conn, bid_id: int, status: str, company: str = "construction") -> bool:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE bid_opportunities
               SET status = %s, updated_at = now()
             WHERE id = %s AND company = %s
        """, (status, bid_id, company))
        ok = cur.rowcount > 0
    conn.commit()
    return ok


def set_bid_amount(conn, bid_id: int, amount, company: str = "construction") -> bool:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE bid_opportunities
               SET bid_amount = %s, updated_at = now()
             WHERE id = %s AND company = %s
        """, (amount, bid_id, company))
        ok = cur.rowcount > 0
    conn.commit()
    return ok


def add_note(conn, bid_id: int, note: str, company: str = "construction") -> bool:
    """Append to notes. Bids accrue context; overwriting it loses the trail."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE bid_opportunities
               SET notes = CASE
                              WHEN notes IS NULL OR notes = '' THEN %s
                              ELSE notes || E'\\n---\\n' || %s
                          END,
                   updated_at = now()
             WHERE id = %s AND company = %s
        """, (note, note, bid_id, company))
        ok = cur.rowcount > 0
    conn.commit()
    return ok


def delete_bid(conn, bid_id: int, company: str = "construction") -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM bid_opportunities WHERE id = %s AND company = %s;",
            (bid_id, company),
        )
        ok = cur.rowcount > 0
    conn.commit()
    return ok


def stats(conn, company: str = "construction") -> dict:
    """Priced value by status. Counts only -- no contact data leaves this module."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT status,
                   COUNT(*)                AS n,
                   COALESCE(SUM(bid_amount), 0) AS total
              FROM bid_opportunities
             WHERE company = %s
          GROUP BY status
        """, (company,))
        rows = cur.fetchall()
    out = {
        "by_status": {r["status"]: {"count": r["n"], "total": float(r["total"])} for r in rows},
        "open": sum(r["n"] for r in rows if r["status"] not in ("won", "lost", "withdrawn")),
        "open_value": sum(float(r["total"]) for r in rows
                          if r["status"] not in ("won", "lost", "withdrawn")),
    }
    return out