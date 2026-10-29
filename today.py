"""The owner's home screen: what needs a human today, and what the bot did.

One screen instead of six. The design the owner asked for is that the bot
runs outreach end to end and a person only steps in when someone books,
replies, or pays -- so this page is built around exceptions and money, not
around columns to drag cards between.

Everything here is read-only except record_payment(), which is how a cash,
check, Zelle, Venmo or ACH payment gets onto the books. The owner rarely uses
Stripe; a money view that only knew about Stripe was reporting a business
that did not exist.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

import ledger
import ledger_bridge

PAYMENT_METHODS = ("cash", "check", "zelle", "venmo", "ach", "card", "other")
PAYMENT_KINDS = ("deposit", "progress", "final")

_SCHEMA_READY = False


def ensure_schema(conn, force: bool = False) -> None:
    """payments gains the columns a non-Stripe payment needs."""
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    with conn.cursor() as cur:
        for ddl in (
            "ALTER TABLE payments ADD COLUMN IF NOT EXISTS method VARCHAR(20) NOT NULL DEFAULT 'card';",
            "ALTER TABLE payments ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'deposit';",
            "ALTER TABLE payments ADD COLUMN IF NOT EXISTS memo TEXT;",
            "ALTER TABLE payments ADD COLUMN IF NOT EXISTS received_at DATE;",
            "ALTER TABLE payments ADD COLUMN IF NOT EXISTS recorded_by VARCHAR(160);",
            "ALTER TABLE payments ALTER COLUMN stripe_session_id DROP NOT NULL;",
            "ALTER TABLE payments ALTER COLUMN stripe_payment_intent_id DROP NOT NULL;",
        ):
            try:
                cur.execute(ddl)
            except Exception:
                # DROP NOT NULL on a column that is already nullable, or a
                # table shape that predates one of these -- never fatal.
                conn.rollback()
    conn.commit()
    _SCHEMA_READY = True


# --- money ---------------------------------------------------------------------

def money_this_month(conn, company: str = "construction") -> dict:
    """Received this month, by method, from the payments table; plus the ledger's
    income statement for the same window when the books are open."""
    first = _dt.date.today().replace(day=1)
    out = {"received_cents": 0, "by_method": [], "count": 0,
           "revenue_cents": None, "expenses_cents": None, "net_cents": None,
           "deposits_held_cents": 0}
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COALESCE(method, 'card') AS method, COUNT(*)::int AS c,
                      COALESCE(SUM(amount_cents), 0)::bigint AS cents
                 FROM payments
                WHERE company = %s AND status IN ('paid', 'succeeded', 'complete', 'completed')
                  AND COALESCE(received_at, created_at::date) >= %s
             GROUP BY 1 ORDER BY cents DESC;""",
            (company, first),
        )
        for r in cur.fetchall():
            out["by_method"].append({"method": r["method"], "count": r["c"], "cents": int(r["cents"])})
            out["received_cents"] += int(r["cents"])
            out["count"] += r["c"]
        cur.execute(
            """SELECT COALESCE(SUM(deposit_cents), 0)::bigint AS cents FROM leads
                WHERE company = %s AND deposit_status = 'paid'
                  AND status NOT IN ('won', 'lost', 'cancelled', 'canceled', 'closed', 'archived');""",
            (company,),
        )
        out["deposits_held_cents"] = int((cur.fetchone() or {}).get("cents") or 0)
    try:
        # income_statement reports dollars (floats) in total_revenue /
        # total_expense / net_income; everything else on this page is cents.
        stmt = ledger.income_statement(conn, start=first, end=_dt.date.today(), company=company)
        out["revenue_cents"] = int(round(float(stmt.get("total_revenue") or 0) * 100))
        out["expenses_cents"] = int(round(float(stmt.get("total_expense") or 0) * 100))
        out["net_cents"] = int(round(float(stmt.get("net_income") or 0) * 100))
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
    return out


def record_payment(conn, *, lead_id: Optional[int], amount_cents: int, method: str,
                   kind: str, memo: str = "", received_on: Optional[_dt.date] = None,
                   recorded_by: str = "", company: str = "construction") -> dict:
    """A customer paid by any method. One call: payments row, ledger entry, lead state.

    deposit  -> held (2100), lead.deposit_status='paid', lead moves to won
    progress -> Contract Revenue (4010)
    final    -> Contract Revenue (4010), lead marked won
    """
    ensure_schema(conn)
    method = method if method in PAYMENT_METHODS else "other"
    kind = kind if kind in PAYMENT_KINDS else "progress"
    if amount_cents <= 0:
        raise ValueError("Amount must be more than zero")
    received_on = received_on or _dt.date.today()
    lead = None
    if lead_id:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, email, status, deposit_cents FROM leads WHERE id = %s;", (lead_id,))
            lead = cur.fetchone()
        if not lead:
            raise ValueError("Lead not found")
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO payments (lead_id, amount_cents, currency, company, status, method, kind,
                                     memo, received_at, recorded_by)
               VALUES (%s, %s, 'usd', %s, 'paid', %s, %s, %s, %s, %s) RETURNING id;""",
            (lead_id, amount_cents, company, method, kind, (memo or "")[:500], received_on, recorded_by[:160]),
        )
        payment_id = cur.fetchone()["id"]
        if lead:
            if kind == "deposit":
                cur.execute(
                    "UPDATE leads SET deposit_status = 'paid', deposit_cents = %s, "
                    "status = CASE WHEN status IN ('won','lost','cancelled','canceled','closed','archived') "
                    "THEN status ELSE 'won' END WHERE id = %s;",
                    (amount_cents, lead_id),
                )
            else:
                cur.execute(
                    "UPDATE leads SET status = CASE WHEN status IN ('lost','cancelled','canceled','closed','archived') "
                    "THEN status ELSE 'won' END WHERE id = %s;",
                    (lead_id,),
                )
    conn.commit()
    who = (lead or {}).get("name") or (lead or {}).get("email") or "customer"
    cash = "1020" if method == "card" else "1010"
    ledger_result = {"posted": False}
    try:
        if kind == "deposit":
            ledger_result = ledger_bridge.post_deposit(conn, lead or {"id": payment_id, "name": who},
                                                       amount_cents, company=company)
        else:
            ledger_result = ledger.post(conn, [
                {"account": cash, "debit": amount_cents, "description": f"{kind.title()} payment ({method})"},
                {"account": "4010", "credit": amount_cents, "description": "Contract revenue"},
            ], entry_date=received_on, memo=f"{kind.title()} payment — {str(who)[:120]} ({method})",
                source_type="payment", source_id=str(payment_id), company=company)
    except Exception as exc:
        ledger_result = {"posted": False, "reason": str(exc)[:200]}
    return {"payment_id": payment_id, "lead_id": lead_id, "amount_cents": amount_cents,
            "method": method, "kind": kind, "ledger": ledger_result}


# --- what needs a person ----------------------------------------------------------

def deposits_pending(conn, company: str = "construction", limit: int = 8) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, name, phone, email, project_type, deposit_cents, created_at
                 FROM leads WHERE company = %s AND deposit_status = 'pending'
             ORDER BY created_at DESC LIMIT %s;""", (company, limit))
        return [dict(r) for r in cur.fetchall()]


def quotes_out(conn, company: str = "construction", limit: int = 8) -> list[dict]:
    """Quotes sent and not yet answered, oldest first (those need a nudge)."""
    with conn.cursor() as cur:
        try:
            cur.execute(
                """SELECT q.id, q.quote_no, q.lead_id, q.job_price_cents, q.status, q.created_at,
                          l.name AS lead_name, l.phone, l.email
                     FROM quotes q LEFT JOIN leads l ON l.id = q.lead_id
                    WHERE q.status IN ('sent', 'draft')
                 ORDER BY q.created_at ASC LIMIT %s;""", (limit,))
            return [dict(r) for r in cur.fetchall()]
        except Exception:
            conn.rollback()
            return []


def bids_due(conn, days: int = 10, limit: int = 8) -> list[dict]:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """SELECT id, name, owner, trade, due_date, bid_amount, status
                     FROM bid_opportunities
                    WHERE due_date IS NOT NULL AND due_date >= CURRENT_DATE
                      AND due_date <= CURRENT_DATE + %s
                      AND COALESCE(status, '') NOT IN ('submitted', 'won', 'lost', 'declined', 'no_bid')
                 ORDER BY due_date ASC LIMIT %s;""", (days, limit))
            return [dict(r) for r in cur.fetchall()]
        except Exception:
            conn.rollback()
            return []


def jobs_this_week(conn, limit: int = 8) -> list[dict]:
    with conn.cursor() as cur:
        try:
            cur.execute(
                """SELECT id, name, address, start_date, end_date, status
                     FROM projects
                    WHERE COALESCE(status, '') NOT IN ('completed', 'cancelled', 'canceled', 'closed')
                      AND (start_date IS NULL OR start_date <= CURRENT_DATE + 7)
                 ORDER BY COALESCE(start_date, CURRENT_DATE) ASC LIMIT %s;""", (limit,))
            return [dict(r) for r in cur.fetchall()]
        except Exception:
            conn.rollback()
            return []


# --- what the bot did -------------------------------------------------------------

def bot_activity(conn, hours: int = 24) -> dict:
    out = {"emails": 0, "texts": 0, "calls": 0, "inbound": 0, "new_leads": 0, "by_source": []}
    with conn.cursor() as cur:
        try:
            cur.execute(
                """SELECT channel, direction, COUNT(*)::int AS c FROM comms_logs
                    WHERE created_at >= NOW() - (%s || ' hours')::interval GROUP BY 1, 2;""", (str(hours),))
            for r in cur.fetchall():
                ch, d, c = (r["channel"] or "").lower(), (r["direction"] or "").lower(), r["c"]
                if d == "inbound":
                    out["inbound"] += c
                elif ch in ("email",):
                    out["emails"] += c
                elif ch in ("sms", "text"):
                    out["texts"] += c
                elif ch in ("voice", "call", "phone"):
                    out["calls"] += c
        except Exception:
            conn.rollback()
        try:
            cur.execute(
                """SELECT COALESCE(NULLIF(source, ''), 'unknown') AS source, COUNT(*)::int AS c
                     FROM leads WHERE company = 'construction'
                      AND created_at >= NOW() - (%s || ' hours')::interval
                 GROUP BY 1 ORDER BY c DESC;""", (str(hours),))
            rows = cur.fetchall()
            out["by_source"] = [dict(r) for r in rows]
            out["new_leads"] = sum(r["c"] for r in rows)
        except Exception:
            conn.rollback()
    return out
