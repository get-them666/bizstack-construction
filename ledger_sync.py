"""Feed real application events into the ledger.

The ledger is correct but inert until something posts to it. This module is
that something: it reads the actual rows the app just wrote -- an invoice, a
cost line, a payroll run -- and turns them into balanced journal entries.

WHY A QUEUE EXISTS
------------------
There are two things that can go wrong: the app write, and the books write.
If the books write fails and the app write is rolled back with it, the owner
loses a real invoice because of an accounting problem. That is the wrong
trade. So the app write stands, the ledger attempt is made separately, and a
failure is queued rather than swallowed.

The queue is visible on /books and retryable from the same page. A books
failure is never silent -- it is visible and it is fixed by re-running -- but
it also never blocks the work.

Every post is idempotent on (source_type, source_id), so re-syncing a record
after a fix updates nothing rather than double counting. That is what makes
the retry button safe to press more than once.
"""
from __future__ import annotations

from datetime import date

import ledger
import ledger_bridge as bridge


class Sync:
    """Result of one sync attempt: what posted, what queued, what was a no-op."""

    def __init__(self):
        self.posted = 0
        self.queued = 0
        self.skipped = 0
        self.errors: list[str] = []

    def __repr__(self):
        return (f"<Sync posted={self.posted} queued={self.queued} "
                f"skipped={self.skipped} errors={len(self.errors)}>")

    def as_dict(self):
        return {"posted": self.posted, "queued": self.queued,
                "skipped": self.skipped, "errors": self.errors}


def ensure_schema(conn) -> None:
    ledger.ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ledger_pending (
                id SERIAL PRIMARY KEY,
                kind VARCHAR(40) NOT NULL,
                ref_id VARCHAR(80),
                company VARCHAR(40) DEFAULT 'construction',
                detail TEXT,
                attempts INTEGER DEFAULT 0,
                last_error TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP WITH TIME ZONE
            )
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_ledger_pending_open
                ON ledger_pending(company, kind, ref_id)
                WHERE resolved_at IS NULL
        """)
    conn.commit()


def _queue(conn, kind: str, ref_id, detail: str, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ledger_pending (kind, ref_id, detail, last_error)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (company, kind, ref_id) WHERE resolved_at IS NULL
            DO UPDATE SET detail = EXCLUDED.detail, last_error = EXCLUDED.last_error,
                          attempts = ledger_pending.attempts + 1
        """, (kind, str(ref_id) if ref_id is not None else None,
              (detail or "")[:1000] or None, (error or "")[:1000] or None))
    conn.commit()


def pending(conn, company: str = "construction") -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM ledger_pending WHERE company = %s AND resolved_at IS NULL "
                    "ORDER BY created_at", (company,))
        return cur.fetchall()


def _guard(conn, kind, ref_id, detail, fn, s: Sync) -> dict | None:
    """Run one post. Queue on failure rather than propagating."""
    try:
        result = fn()
        if result and result.get("posted"):
            s.posted += 1
            with conn.cursor() as cur:
                cur.execute("UPDATE ledger_pending SET resolved_at = now() "
                            "WHERE company = 'construction' AND kind = %s AND ref_id = %s "
                            "AND resolved_at IS NULL", (kind, str(ref_id)))
            conn.commit()
        else:
            s.skipped += 1
            if result and result.get("reason"):
                s.skipped -= 1
        return result
    except Exception as e:
        _queue(conn, kind, ref_id, detail, str(e))
        s.queued += 1
        s.errors.append(f"{kind}:{ref_id} {e}"[:200])
        return None


# --- syncs ------------------------------------------------------------------
def sync_invoice(conn, invoice_id: int, company: str = "construction") -> Sync:
    """A raised invoice is revenue and a receivable."""
    s = Sync()
    with conn.cursor() as cur:
        cur.execute("SELECT i.*, b.id AS bid_id, b.name AS bid_name, b.trade "
                    "FROM bid_invoices i JOIN bid_opportunities b ON b.id = i.bid_id "
                    "WHERE i.id = %s AND i.company = %s", (invoice_id, company))
        inv = cur.fetchone()
    if not inv:
        s.skipped += 1
        return s
    bid = {"id": inv["bid_id"], "name": inv["bid_name"]}
    _guard(conn, "invoice", invoice_id, f"invoice {inv.get('invoice_number')}",
           lambda: bridge.post_invoice(conn, bid, inv, company), s)
    return s


def sync_invoice_status(conn, invoice_id: int, company: str = "construction") -> Sync:
    """Marking an invoice paid is what actually relieves the receivable."""
    s = Sync()
    with conn.cursor() as cur:
        cur.execute("SELECT i.*, b.id AS bid_id, b.name AS bid_name "
                    "FROM bid_invoices i JOIN bid_opportunities b ON b.id = i.bid_id "
                    "WHERE i.id = %s AND i.company = %s", (invoice_id, company))
        inv = cur.fetchone()
    if not inv:
        s.skipped += 1
        return s
    bid = {"id": inv["bid_id"], "name": inv["bid_name"]}
    collected = float(inv.get("collected") or 0)
    if collected > 0:
        _guard(conn, "payment", f"{invoice_id}:{collected:.2f}",
               f"payment on {inv.get('invoice_number')}",
               lambda: bridge.post_payment(conn, bid, {
                   "id": f"{invoice_id}:{collected:.2f}",
                   "amount": collected,
                   "date": inv.get("issued_date"),
                   "method": "ach"}, company), s)
    return s


def sync_cost_line(conn, cost_line_id: int, company: str = "construction") -> Sync:
    """An incurred direct cost is an expense and a payable."""
    s = Sync()
    with conn.cursor() as cur:
        cur.execute("SELECT c.*, b.id AS bid_id, b.name AS bid_name "
                    "FROM bid_costs c JOIN bid_opportunities b ON b.id = c.bid_id "
                    "WHERE c.id = %s AND c.company = %s", (cost_line_id, company))
        cl = cur.fetchone()
    if not cl:
        s.skipped += 1
        return s
    bid = {"id": cl["bid_id"], "name": cl["bid_name"]}
    _guard(conn, "cost", cost_line_id, cl.get("description"),
           lambda: bridge.post_cost_line(conn, bid, cl, company), s)
    return s


def sync_bid_outcome(conn, bid_id: int, company: str = "construction") -> Sync:
    """A won bid becomes a contract; revenue is recognised when billed, not now.

    Deliberately does not post revenue on win. Under accrual accounting the
    job is not earned until it is billed, and booking the whole contract at
    award would show a margin on work nobody has done yet.
    """
    s = Sync()
    s.skipped += 1
    return s


def sync_payroll_paid(conn, run_id: int, company: str = "construction") -> Sync:
    """Money sent to crew relieves what was accrued when hours were approved."""
    s = Sync()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM payroll_runs WHERE id = %s", (run_id,))
        run = cur.fetchone()
    if not run:
        s.skipped += 1
        return s
    lines = _payroll_lines(conn, run, company)
    _guard(conn, "payroll_paid", run_id, f"payroll run {run_id}",
           lambda: bridge.pay_payroll(conn, run, lines, company), s)
    return s


def _payroll_lines(conn, run: dict, company: str) -> list[dict]:
    """Gross per person for a run, from approved timesheets and crew pay rates."""
    if not ledger._table_exists(conn, "payroll_lines") or not ledger._table_exists(conn, "crew"):
        return []
    out: dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pl.crew_id, COALESCE(SUM(pl.gross_cents), 0)/100.0 AS gross
            FROM payroll_lines pl WHERE pl.run_id = %s GROUP BY pl.crew_id
        """, (run.get("id"),))
        rows = cur.fetchall()
        if not rows:
            return []
        for r in rows:
            name = r["crew_id"]
            cur.execute("SELECT name FROM crew WHERE id = %s", (r["crew_id"],))
            c = cur.fetchone()
            name = (c["name"] if c else f"crew {r['crew_id']}")
            out[name] = {"crew": name, "gross": r["gross"]}
    return list(out.values())


def sync_payroll_accrual(conn, run_id: int, company: str = "construction") -> Sync:
    """Approved hours become wages owed, whether or not the money has moved."""
    s = Sync()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM payroll_runs WHERE id = %s", (run_id,))
        run = cur.fetchone()
    if not run:
        s.skipped += 1
        return s
    lines = _payroll_accrual_lines(conn, run, company)
    _guard(conn, "payroll_accrual", run_id, f"payroll run {run_id}",
           lambda: bridge.accrue_payroll(conn, run, lines, company), s)
    return s


def _payroll_accrual_lines(conn, run: dict, company: str) -> list[dict]:
    if not ledger._table_exists(conn, "payroll_lines") or not ledger._table_exists(conn, "crew"):
        return []
    out = []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT pl.crew_id, c.name, c.pay_rate,
                   COALESCE(SUM(pl.regular_hours), 0) + COALESCE(SUM(pl.overtime_hours), 0) AS hours
            FROM payroll_lines pl JOIN crew c ON c.id = pl.crew_id
            WHERE pl.run_id = %s GROUP BY pl.crew_id, c.name, c.pay_rate
        """, (run.get("id"),))
        for r in cur.fetchall():
            hours = float(r["hours"] or 0)
            if hours <= 0:
                continue
            out.append({"crew": r["name"], "hours": hours,
                        "rate": float(r["pay_rate"] or 0)})
    return out


def sync_all_for_bid(conn, bid_id: int, company: str = "construction") -> Sync:
    """Re-sync every ledger-backed record on one job. The /books repair button."""
    total = Sync()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM bid_invoices WHERE bid_id = %s AND company = %s "
                    "AND status <> 'void'", (bid_id, company))
        invs = [r["id"] for r in cur.fetchall()]
        cur.execute("SELECT id FROM bid_costs WHERE bid_id = %s AND company = %s",
                    (bid_id, company))
        costs = [r["id"] for r in cur.fetchall()]
    for i in invs:
        _merge(total, sync_invoice(conn, i, company))
        _merge(total, sync_invoice_status(conn, i, company))
    for c in costs:
        _merge(total, sync_cost_line(conn, c, company))
    return total


def _merge(a: Sync, b: Sync) -> None:
    a.posted += b.posted
    a.queued += b.queued
    a.skipped += b.skipped
    a.errors.extend(b.errors)


def retry_all(conn, company: str = "construction") -> Sync:
    """Re-attempt everything queued. Safe to run repeatedly."""
    total = Sync()
    for row in pending(conn, company):
        kind, ref = row["kind"], row["ref_id"]
        if kind == "invoice":
            _merge(total, sync_invoice(conn, int(ref), company))
        elif kind == "payment":
            _merge(total, sync_invoice_status(conn, int(str(ref).split(":")[0]), company))
        elif kind == "cost":
            _merge(total, sync_cost_line(conn, int(ref), company))
        elif kind == "payroll_paid":
            _merge(total, sync_payroll_paid(conn, int(ref), company))
        elif kind == "payroll_accrual":
            _merge(total, sync_payroll_accrual(conn, int(ref), company))
    return total


def books_status(conn, company: str = "construction") -> dict:
    """What the owner needs to know at a glance: are the books straight?"""
    tb = ledger.trial_balance(conn, company=company)
    bs = ledger.balance_sheet(conn, company=company)
    queue = pending(conn, company)
    return {"trial_balance": tb, "balance_sheet": bs,
            "pending_count": len(queue), "pending": queue[:20],
            "clean": tb["balanced"] and bs["balanced"] and not queue}