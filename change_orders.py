"""Change orders: the priced, numbered document for work added mid-job.

A change order is not another bid. It is the one record that explains why the
contract total moved, who asked for it, how many days it adds, and who
approved it. The number is sequential per company and year (``CO-2026-0001``)
so the office, the client and the books all refer to the same document.

Approval does two things, once:

1. adds the amount to ``bid_opportunities.contract_value`` (the number the
   invoices and the job cost sheet use), and
2. posts the revenue to the ledger on account 4020 — Change Order Revenue.

Both are idempotent through the ledger's ``(company, source_type, source_id)``
key, so pressing Approve twice cannot double the contract or the revenue.
"""
from __future__ import annotations

from datetime import date

from fpdf import FPDF

import ledger
from bid_package_pdf import (
    ADDRESS,
    COMPANY,
    EMAIL,
    PHONE,
    BidPackagePDF,
    _heading,
    _labelled,
    _money,
    _txt,
)

STATUSES = ("draft", "sent", "approved", "rejected", "void")


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS change_orders (
                id SERIAL PRIMARY KEY,
                bid_id INTEGER NOT NULL REFERENCES bid_opportunities(id) ON DELETE CASCADE,
                company VARCHAR(40) NOT NULL DEFAULT 'construction',
                number VARCHAR(40) NOT NULL,
                title VARCHAR(255) NOT NULL,
                description TEXT,
                amount NUMERIC(14,2) NOT NULL DEFAULT 0,
                cost_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
                schedule_days INTEGER NOT NULL DEFAULT 0,
                status VARCHAR(20) NOT NULL DEFAULT 'draft',
                requested_by VARCHAR(255),
                approved_at TIMESTAMPTZ,
                approved_by VARCHAR(255),
                signature_name VARCHAR(160),
                notes TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_change_orders_number
                ON change_orders(company, number);
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_change_orders_bid
                ON change_orders(bid_id, created_at DESC);
        """)
    conn.commit()


def next_number(conn, company: str = "construction") -> str:
    """CO-YYYY-0001, sequential within the company and year."""
    year = date.today().year
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM change_orders "
            "WHERE company = %s AND number LIKE %s",
            (company, f"CO-{year}-%"))
        n = int(cur.fetchone()["n"] or 0) + 1
    return f"CO-{year}-{n:04d}"


def create(conn, bid_id: int, *, title: str, description: str = "",
           amount=0, cost_amount=0, schedule_days: int = 0,
           requested_by: str = "", notes: str = "",
           company: str = "construction") -> dict:
    ensure_schema(conn)
    name = (title or "").strip()
    if not name:
        raise ValueError("A change order needs a title.")
    try:
        amt = round(float(str(amount).replace("$", "").replace(",", "") or 0), 2)
        cost = round(float(str(cost_amount).replace("$", "").replace(",", "") or 0), 2)
    except (TypeError, ValueError):
        raise ValueError("Amounts must be numbers.")
    if amt < 0 or cost < 0:
        raise ValueError("Use a negative change order to reduce the contract; "
                         "amounts themselves cannot be negative here.")
    number = next_number(conn, company)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO change_orders
                (bid_id, company, number, title, description, amount, cost_amount,
                 schedule_days, requested_by, notes)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *;
        """, (int(bid_id), company, number, name[:255],
              (description or "").strip() or None, amt, cost,
              int(schedule_days or 0), (requested_by or "").strip()[:255] or None,
              (notes or "").strip() or None))
        row = cur.fetchone()
    conn.commit()
    return row


def list_for_bid(conn, bid_id: int, company: str = "construction") -> list[dict]:
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM change_orders
             WHERE bid_id = %s AND company = %s
             ORDER BY id;
        """, (int(bid_id), company))
        return cur.fetchall()


def get(conn, co_id: int, company: str = "construction"):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM change_orders WHERE id = %s AND company = %s;",
                    (int(co_id), company))
        return cur.fetchone()


def approved_total(conn, bid_id: int, company: str = "construction") -> float:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(amount), 0) AS amt FROM change_orders
             WHERE bid_id = %s AND company = %s AND status = 'approved';
        """, (int(bid_id), company))
        return float(cur.fetchone()["amt"] or 0)


def set_status(conn, co_id: int, status: str, *, actor: str = "",
               signature_name: str = "", company: str = "construction") -> dict:
    """Move a change order through draft → sent → approved/rejected.

    Approval is the only transition with accounting consequences, and it
    happens once. A rejected or void change order never changes the contract.
    """
    status = (status or "").strip().lower()
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    row = get(conn, co_id, company)
    if not row:
        raise ValueError("Change order not found.")
    if row["status"] == "approved" and status != "approved":
        raise ValueError("An approved change order cannot be reversed. "
                         "Issue a new negative change order instead.")
    amount = float(row.get("amount") or 0)
    if status == "approved" and row["status"] != "approved":
        # 1) revenue and receivable
        ledger.ensure_schema(conn)
        ledger.post(conn, [
            {"account": "1100", "debit": int(round(amount * 100)),
             "description": f"{row['number']} — {row['title']}", "bid_id": int(row["bid_id"])},
            {"account": "4020", "credit": int(round(amount * 100)),
             "description": f"{row['number']} — {row['title']}", "bid_id": int(row["bid_id"])},
        ], entry_date=date.today(),
            memo=f"Change order {row['number']} approved",
            source_type="change_order", source_id=str(co_id), company=company)
        # 2) the contract total the invoices and cost sheet use
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE bid_opportunities
                   SET contract_value = COALESCE(contract_value, bid_amount, 0) + %s,
                       updated_at = now()
                 WHERE id = %s AND company = %s;
            """, (amount, int(row["bid_id"]), company))
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE change_orders
               SET status = %s,
                   approved_at = CASE WHEN %s = 'approved' AND approved_at IS NULL
                                      THEN now() ELSE approved_at END,
                   approved_by = CASE WHEN %s = 'approved' THEN %s ELSE approved_by END,
                   signature_name = CASE WHEN %s = 'approved' THEN %s ELSE signature_name END,
                   updated_at = now()
             WHERE id = %s AND company = %s
             RETURNING *;
        """, (status, status, status, (actor or "")[:255] or None,
              status, (signature_name or "")[:160] or None,
              int(co_id), company))
        out = cur.fetchone()
    conn.commit()
    return out


def _mc(pdf: FPDF, w: float, h: float, text: str, **kw) -> None:
    kw.setdefault("new_x", "LMARGIN")
    kw.setdefault("new_y", "NEXT")
    FPDF.multi_cell(pdf, w, h, text, **kw)


def build_pdf(co: dict, bid: dict) -> bytes:
    """A one-page change order the client can sign and file."""
    co = dict(co or {})
    bid = dict(bid or {})
    pdf = BidPackagePDF(format="letter")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(15, 15, 15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, "CHANGE ORDER", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 4.6, f"{_txt(co.get('number') or '')}   |   "
                     f"{_txt(co.get('created_at') or date.today().isoformat())}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    _heading(pdf, "Project")
    _labelled(pdf, "Job", bid.get("name") or "")
    _labelled(pdf, "Property", bid.get("location") or "")
    _labelled(pdf, "Customer", bid.get("contact_name") or "")
    _labelled(pdf, "Original contract", _money(bid.get("contract_value") or bid.get("bid_amount") or 0))
    pdf.ln(2)

    _heading(pdf, "Change")
    _labelled(pdf, "Title", co.get("title") or "")
    pdf.set_font("Helvetica", "", 10)
    _mc(pdf, 0, 5, _txt(co.get("description") or "No description supplied."))
    pdf.ln(2)
    _labelled(pdf, "Change order amount", _money(co.get("amount") or 0))
    _labelled(pdf, "Schedule impact",
              f"{int(co.get('schedule_days') or 0)} calendar day(s)")
    _labelled(pdf, "Status", str(co.get("status") or "draft").title())

    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8.5)
    pdf.set_text_color(90, 90, 90)
    _mc(pdf, 0, 4.4, _txt(
        "Approving this change order adds the amount above to the contract "
        "total and authorises the described work. Work outside this document "
        "requires a separate change order."))
    pdf.set_text_color(0, 0, 0)
    pdf.ln(8)

    _heading(pdf, "Signatures")
    for label in ("Owner", f"For {COMPANY}"):
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(90, 5, label)
        pdf.cell(90, 5, "Date", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(9)
        y = pdf.get_y()
        pdf.line(pdf.l_margin, y, pdf.l_margin + 82, y)
        pdf.line(pdf.l_margin + 92, y, pdf.l_margin + 174, y)
        pdf.ln(3)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(110, 110, 110)
        pdf.cell(90, 4, "Signature")
        pdf.cell(90, 4, co.get("signature_name") or "Printed name and date")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(10)

    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(110, 110, 110)
    _mc(pdf, 0, 4, _txt(f"{COMPANY} · {ADDRESS} · {PHONE} · {EMAIL}"))
    return bytes(pdf.output())
