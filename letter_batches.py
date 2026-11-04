"""Letter print batches, and the owner's one-click "I delivered these".

Rendering a letter is not mailing it, which is why `mail_letters.py` writes only
PDFs and a manifest and never touches `comms_logs`. That is correct, but it left
the opposite gap: after the owner actually prints, walks and delivers a batch,
nothing recorded it -- so the door stayed "new", stayed eligible for the same
letter, and the contact policy never saw the touch.

A batch is created when the PDFs are written. Delivery is a separate, explicit
act: `mark_delivered` is what writes the `letter` touch (the channel
`auto_reply.record_manual_touch` already understands) and moves the lead to
`contacted`. Pressing it twice records nothing twice.

No money moves here and nothing is sent; this is bookkeeping for work the owner
did by hand.
"""
from __future__ import annotations

import auto_reply


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS letter_batches (
                id SERIAL PRIMARY KEY,
                company VARCHAR(40) DEFAULT 'construction',
                name VARCHAR(120) NOT NULL,
                out_dir TEXT,
                letter_count INTEGER NOT NULL DEFAULT 0,
                status VARCHAR(20) NOT NULL DEFAULT 'printed',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                delivered_at TIMESTAMP WITH TIME ZONE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS letter_batch_items (
                id SERIAL PRIMARY KEY,
                batch_id INTEGER NOT NULL REFERENCES letter_batches(id) ON DELETE CASCADE,
                lead_id INTEGER NOT NULL,
                address TEXT,
                pdf VARCHAR(255),
                delivered_at TIMESTAMP WITH TIME ZONE
            )
        """)
        # One door belongs to one batch. Postage is the expensive part, and the
        # duplicate-permit problem means the same house can be several leads.
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_letter_batch_lead "
                    "ON letter_batch_items(lead_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_letter_batch_items_batch "
                    "ON letter_batch_items(batch_id, delivered_at)")
    conn.commit()


def create_batch(conn, name: str, out_dir: str, items: list[dict],
                 company: str = "construction") -> int:
    """Record a printed batch. `items`: [{lead_id, address, pdf}]."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO letter_batches (company, name, out_dir, letter_count) "
            "VALUES (%s,%s,%s,%s) RETURNING id;",
            (company, (name or "")[:120], out_dir, len(items)))
        batch_id = cur.fetchone()["id"]
        for it in items:
            cur.execute(
                "INSERT INTO letter_batch_items (batch_id, lead_id, address, pdf) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (lead_id) DO NOTHING;",
                (batch_id, int(it["lead_id"]), (it.get("address") or "")[:1000],
                 (it.get("pdf") or "")[:255]))
    conn.commit()
    return batch_id


def batched_lead_ids(conn, company: str = "construction") -> set[int]:
    """Every lead already in a batch, delivered or not."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT i.lead_id FROM letter_batch_items i "
            "JOIN letter_batches b ON b.id = i.batch_id WHERE b.company = %s;", (company,))
        return {int(r["lead_id"]) for r in cur.fetchall()}


def list_batches(conn, company: str = "construction") -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT b.id, b.name, b.out_dir, b.status, b.created_at, b.delivered_at,
                   COUNT(i.id) AS item_count,
                   COUNT(i.delivered_at) AS delivered_count
              FROM letter_batches b
              LEFT JOIN letter_batch_items i ON i.batch_id = b.id
             WHERE b.company = %s
             GROUP BY b.id
             ORDER BY b.created_at DESC, b.id DESC
        """, (company,))
        return cur.fetchall()


def batch_items(conn, batch_id: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, lead_id, address, pdf, delivered_at FROM letter_batch_items "
            "WHERE batch_id = %s ORDER BY id;", (batch_id,))
        return cur.fetchall()


def mark_delivered(conn, batch_id: int, company: str = "construction") -> dict:
    """Owner confirms the batch was delivered. Writes one letter touch per door.

    Idempotent: items already stamped are skipped, so a double click cannot
    double-count a touch or push a lead through the contact policy twice.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM letter_batches WHERE id = %s AND company = %s;",
                    (batch_id, company))
        batch = cur.fetchone()
        if not batch:
            return {"ok": False, "error": "No such batch."}
        cur.execute(
            "SELECT id, lead_id, address FROM letter_batch_items "
            "WHERE batch_id = %s AND delivered_at IS NULL ORDER BY id;", (batch_id,))
        items = cur.fetchall()

    recorded = failed = 0
    for it in items:
        detail = f"Delivered letter batch {batch_id}"
        if it.get("address"):
            detail += f" — {it['address']}"
        result = auto_reply.record_manual_touch(conn, int(it["lead_id"]), "letter", detail)
        if not result.get("ok"):
            failed += 1
            continue
        with conn.cursor() as cur:
            cur.execute("UPDATE letter_batch_items SET delivered_at = now() "
                        "WHERE id = %s AND delivered_at IS NULL;", (it["id"],))
        conn.commit()
        recorded += 1

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM letter_batch_items "
                    "WHERE batch_id = %s AND delivered_at IS NULL;", (batch_id,))
        remaining = int(cur.fetchone()["n"])
        if remaining == 0:
            cur.execute("UPDATE letter_batches SET status = 'delivered', "
                        "delivered_at = COALESCE(delivered_at, now()) WHERE id = %s;",
                        (batch_id,))
        else:
            cur.execute("UPDATE letter_batches SET status = 'partial' WHERE id = %s;",
                        (batch_id,))
    conn.commit()
    return {"ok": True, "batch_id": batch_id, "recorded": recorded,
            "failed": failed, "remaining": remaining}


def delete_batch(conn, batch_id: int, company: str = "construction") -> dict:
    """Remove a batch that was printed wrong. Refuses once anything is delivered."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM letter_batches WHERE id = %s AND company = %s;",
                    (batch_id, company))
        if not cur.fetchone():
            return {"ok": False, "error": "No such batch."}
        cur.execute("SELECT COUNT(*) AS n FROM letter_batch_items "
                    "WHERE batch_id = %s AND delivered_at IS NOT NULL;", (batch_id,))
        if int(cur.fetchone()["n"]):
            return {"ok": False, "error": "Part of this batch is already delivered; "
                                          "it cannot be deleted."}
        cur.execute("DELETE FROM letter_batches WHERE id = %s;", (batch_id,))
    conn.commit()
    return {"ok": True, "batch_id": batch_id}
