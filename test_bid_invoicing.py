"""Bid invoice totals.

    venv/bin/python -m pytest test_bid_invoicing.py -v

A draft has never been sent, so it is not billed and nothing is owed on it.
Before this, a duplicated custom invoice sat in "Billed"/"Outstanding" forever
($11,511 billed on a $5,005 contract) and could never be collected.
"""
from datetime import date, timedelta

import bid_costing


def _rows():
    due = date.today() + timedelta(days=15)
    return [
        {"id": 1, "kind": "deposit", "status": "sent", "amount": 1501.0,
         "collected": 0.0, "due_date": due, "invoice_number": "INV-1"},
        {"id": 2, "kind": "invoice", "status": "draft", "amount": 5005.0,
         "collected": 0.0, "due_date": due, "invoice_number": "INV-2"},
        {"id": 3, "kind": "invoice", "status": "paid", "amount": 5005.0,
         "collected": 5005.0, "due_date": due, "invoice_number": "INV-3"},
    ]


def _summary(monkeypatch, rows):
    monkeypatch.setattr(bid_costing, "list_invoices",
                        lambda conn, bid_id, company="construction": rows)
    return bid_costing.invoice_summary(None, 1)


def test_a_draft_never_counts_as_billed(monkeypatch):
    s = _summary(monkeypatch, _rows())
    assert s["billed"] == 6506.0, "deposit + the sent/paid invoice only"
    assert s["collected"] == 5005.0
    assert s["outstanding"] == 1501.0
    assert (s["draft_count"], s["draft_total"]) == (1, 5005.0)


def test_void_invoices_are_ignored_entirely(monkeypatch):
    rows = _rows() + [{"id": 4, "kind": "invoice", "status": "void", "amount": 999.0,
                       "collected": 0.0, "due_date": None, "invoice_number": "INV-4"}]
    s = _summary(monkeypatch, rows)
    assert s["billed"] == 6506.0
    assert s["draft_count"] == 1


def test_no_drafts_reports_zero(monkeypatch):
    s = _summary(monkeypatch, [r for r in _rows() if r["status"] != "draft"])
    assert (s["draft_count"], s["draft_total"]) == (0, 0)
    assert s["billed"] == 6506.0


def test_partial_payments_accumulate_instead_of_overwriting():
    """Paying 4,000 then 3,000 on a 10,000 invoice must leave 7,000 collected.

    The old code SET collected to the amount of each click, so the second
    payment erased the first (7,000 received, 3,000 on the books), and the
    next "mark paid" posted the full 10,000 to the ledger -- 17,000 against a
    10,000 invoice.
    """
    class Cur:
        def __init__(self):
            self.params = None
            self.row = {"amount": 10000.0, "collected": 4000.0, "bid_id": 1}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if sql.strip().upper().startswith("UPDATE"):
                self.params = params

        def fetchone(self):
            return self.row

    class Conn:
        def __init__(self):
            self.cur = Cur()

        def cursor(self):
            return self.cur

        def commit(self):
            pass

    conn = Conn()
    assert bid_costing.set_invoice_status(conn, 1, "partial", "construction", 3000) is True
    # UPDATE params: (derived_status, collected, invoice_id, company)
    assert conn.cur.params[1] == 7000.0, conn.cur.params
    assert conn.cur.params[0] == "partial"


def test_paying_the_balance_marks_it_paid_and_keeps_the_full_total():
    class Cur:
        def __init__(self):
            self.params = None
            self.row = {"amount": 10000.0, "collected": 7000.0, "bid_id": 1}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            if sql.strip().upper().startswith("UPDATE"):
                self.params = params

        def fetchone(self):
            return self.row

    class Conn:
        def __init__(self):
            self.cur = Cur()

        def cursor(self):
            return self.cur

        def commit(self):
            pass

    conn = Conn()
    assert bid_costing.set_invoice_status(conn, 1, "paid", "construction", 3000) is True
    assert conn.cur.params[1] == 10000.0
    assert conn.cur.params[0] == "paid"
