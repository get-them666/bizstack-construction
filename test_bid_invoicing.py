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
