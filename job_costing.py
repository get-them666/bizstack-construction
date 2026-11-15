"""One job's cost sheet: budget, actual, change orders and books in one view.

The rest of the app already records each piece — bid cost lines, timesheets,
invoices, change orders, ledger postings. This module reads them together for
one bid so the owner can answer the only three questions that matter on a job:

* what did we sell it for,
* what has it cost, and
* what is left.

It deliberately reports source records *and* ledger postings. If they disagree,
the ledger is the accounting truth and ``ledger_sync.pending`` explains why a
source record has not reached it yet.
"""
from __future__ import annotations

from datetime import date

import bid_costing
import change_orders
import ledger


def _money(v) -> float:
    try:
        return round(float(v or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def cost_sheet(conn, bid_id: int, company: str = "construction") -> dict | None:
    bid = None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM bid_opportunities WHERE id = %s AND company = %s;",
                    (int(bid_id), company))
        bid = cur.fetchone()
    if not bid:
        return None

    bid_costing.ensure_accounting_schema(conn)
    change_orders.ensure_schema(conn)
    ledger.ensure_schema(conn)

    summary = bid_costing.cost_summary(conn, bid_id, company)
    project = bid_costing.open_job_for_bid(conn, bid_id, company)
    invoices = bid_costing.list_invoices(conn, bid_id, company)

    billed = collected = 0.0
    open_invoices = []
    for inv in invoices:
        if str(inv.get("status") or "").lower() == "void":
            continue
        amt = _money(inv.get("amount"))
        paid = _money(inv.get("collected"))
        billed += amt
        collected += paid
        if str(inv.get("status") or "").lower() not in ("paid", "void"):
            open_invoices.append(inv)

    cos = change_orders.list_for_bid(conn, bid_id, company)
    approved_co = sum(_money(c.get("amount")) for c in cos
                      if str(c.get("status") or "").lower() == "approved")
    contract = _money(bid.get("contract_value") or bid.get("bid_amount"))
    # contract_value already includes approved change orders (set_status adds
    # them), so the original is what remains after removing those.
    original_contract = round(contract - approved_co, 2)

    by_cat = summary.get("by_category") or {}
    labor = summary.get("labor") or {}
    categories = []
    for cat in bid_costing.COST_CATEGORIES:
        est = _money((by_cat.get(cat) or {}).get("estimated"))
        if cat == "labor":
            actual = _money(labor.get("labor_cost"))
        else:
            actual = _money((by_cat.get(cat) or {}).get("actual"))
        categories.append({
            "category": cat,
            "label": {
                "labor": "Direct labor",
                "material": "Direct materials",
                "sub": "Subcontractors",
                "equipment": "Equipment rental",
                "permit": "Permits & fees",
                "other": "Other direct cost",
            }.get(cat, cat.title()),
            "estimated": est,
            "actual": actual,
            "variance": round(est - actual, 2),
        })
    estimated_total = sum(c["estimated"] for c in categories)
    actual_total = sum(c["actual"] for c in categories)

    # Posted books for this job, by account.
    with conn.cursor() as cur:
        cur.execute("""
            SELECT l.account_code, a.name, a.type,
                   COALESCE(SUM(l.debit_cents), 0)::bigint AS dr,
                   COALESCE(SUM(l.credit_cents), 0)::bigint AS cr
              FROM ledger_lines l
              JOIN ledger_entries e ON e.id = l.entry_id
              JOIN ledger_accounts a ON a.code = l.account_code AND a.company = e.company
             WHERE e.company = %s AND l.bid_id = %s
             GROUP BY l.account_code, a.name, a.type
             ORDER BY l.account_code;
        """, (company, int(bid_id)))
        book_rows = cur.fetchall()
    revenue_posted = sum(_money(r["cr"]) for r in book_rows if r["type"] == "revenue")
    direct_posted = sum(_money(r["dr"]) for r in book_rows
                        if r["type"] == "expense" and str(r["account_code"]).startswith("50"))
    overhead_posted = sum(_money(r["dr"]) for r in book_rows
                          if r["type"] == "expense" and str(r["account_code"]).startswith("60"))

    entries = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT e.id, e.entry_no, e.entry_date, e.memo, e.source_type, e.source_id,
                       COALESCE(SUM(l.debit_cents), 0)::bigint / 100.0 AS amount
                  FROM ledger_entries e
                  LEFT JOIN ledger_lines l ON l.entry_id = e.id
                 WHERE e.company = %s AND l.bid_id = %s
                 GROUP BY e.id
                 ORDER BY e.entry_date DESC, e.id DESC
                 LIMIT 25;
            """, (company, int(bid_id)))
            entries = cur.fetchall()
    except Exception:
        conn.rollback()

    timesheets = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id, t.work_date, t.hours, t.work_type, t.status,
                       c.name AS crew_name, p.name AS project_name
                  FROM timesheets t
                  JOIN crew c ON c.id = t.crew_id
                  JOIN projects p ON p.id = t.project_id
                 WHERE p.bid_id = %s AND p.company = %s
                 ORDER BY t.work_date DESC, t.id DESC
                 LIMIT 25;
            """, (int(bid_id), company))
            timesheets = cur.fetchall()
    except Exception:
        conn.rollback()

    job_number = f"JOB-{project['id']}" if project else f"BID-{bid_id}"
    return {
        "bid": bid,
        "project": project,
        "job_number": job_number,
        "invoices": invoices,
        "open_invoices": open_invoices,
        "billed": round(billed, 2),
        "collected": round(collected, 2),
        "balance": round(max(0.0, contract - collected), 2),
        "contract": contract,
        "original_contract": original_contract,
        "approved_change_orders": round(approved_co, 2),
        "change_orders": cos,
        "categories": categories,
        "estimated_total": round(estimated_total, 2),
        "actual_total": round(actual_total, 2),
        "estimated_margin": round(contract - estimated_total, 2),
        "actual_margin": round(contract - actual_total, 2),
        "actual_margin_pct": round((contract - actual_total) / contract * 100, 1) if contract else None,
        "labor_hours": _money(labor.get("hours")),
        "labor_cost": _money(labor.get("labor_cost")),
        "books": {
            "revenue": round(revenue_posted, 2),
            "direct_cost": round(direct_posted, 2),
            "overhead": round(overhead_posted, 2),
            "margin": round(revenue_posted - direct_posted, 2),
            "net": round(revenue_posted - direct_posted - overhead_posted, 2),
            "rows": book_rows,
        },
        "entries": entries,
        "timesheets": timesheets,
        "summary": summary,
        "today": date.today(),
    }
