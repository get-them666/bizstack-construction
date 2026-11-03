"""A revision changes the card, and the old version stays visible as history.

The owner revises a price because a customer asked for a change or a new find
turned up. Two rules follow from that:

* The total, the line list and every generated document must change with it,
  without a second button press.
* A removed line or superseded document is *history*, not garbage: it stays on
  the card marked as removed, while every total and PDF ignores it.

No network or real database: cursors are faked and the money writers patched.
"""

import asyncio
from pathlib import Path

import bid_costing
import bid_opportunities
import construction_main


class _Cursor:
    sql = ""
    params = ()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=()):
        self.sql = " ".join(sql.split())
        self.params = params

    def fetchone(self):
        return {"id": 5}

    def fetchall(self):
        return []


class _Conn:
    def __init__(self):
        self.cur = _Cursor()

    def cursor(self):
        return self.cur

    def commit(self):
        pass


def test_cost_lines_hide_removed_rows_by_default():
    conn = _Conn()
    bid_costing.cost_lines(conn, 1)
    assert "removed_at IS NULL" in conn.cur.sql

    bid_costing.cost_lines(conn, 1, include_removed=True)
    assert "removed_at" not in conn.cur.sql


def test_removing_a_line_marks_it_instead_of_deleting_it():
    conn = _Conn()
    assert bid_costing.delete_cost_line(conn, 5, 1) is True
    assert conn.cur.sql.startswith("UPDATE bid_costs SET removed_at")
    assert "RETURNING id" in conn.cur.sql
    assert "DELETE" not in conn.cur.sql


def test_a_removed_line_can_be_restored():
    conn = _Conn()
    assert bid_costing.restore_cost_line(conn, 5, 1) is True
    assert conn.cur.sql.startswith("UPDATE bid_costs SET removed_at = NULL")
    assert "removed_at IS NOT NULL" in conn.cur.sql


def test_a_line_can_be_corrected_in_place():
    conn = _Conn()
    assert bid_costing.update_cost_line(
        conn, 5, 1, category="material", description="new find", vendor="ABC",
        estimated="450", actual="") is True
    assert conn.cur.sql.startswith("UPDATE bid_costs SET")
    assert "removed_at IS NULL" in conn.cur.sql
    assert "new find" in conn.cur.params
    assert "ABC" in conn.cur.params


def test_the_total_follows_the_lines_automatically(monkeypatch):
    writes = []
    monkeypatch.setattr(construction_main.bid_opportunities, "get_bid",
                        lambda db, bid_id, company: {"id": bid_id, "status": "drafting",
                                                     "contract_value": None})
    monkeypatch.setattr(construction_main.bid_costing, "quote_total",
                        lambda db, bid_id, company: (2980.64, 2591.86, 15.0))
    monkeypatch.setattr(construction_main.bid_opportunities, "set_bid_amount",
                        lambda db, bid_id, amount, company: writes.append((bid_id, amount)) or True)

    total = construction_main._recalc_bid_amount(None, 18350)
    assert total == 2980.64
    assert writes == [(18350, 2980.64)]


def test_a_signed_contract_value_is_never_overwritten(monkeypatch):
    writes = []
    monkeypatch.setattr(construction_main.bid_opportunities, "get_bid",
                        lambda db, bid_id, company: {"id": bid_id, "status": "won",
                                                     "contract_value": 2591.86})
    monkeypatch.setattr(construction_main.bid_costing, "quote_total",
                        lambda db, bid_id, company: (9999.0, 8000.0, 15.0))
    monkeypatch.setattr(construction_main.bid_opportunities, "set_bid_amount",
                        lambda *args: writes.append(args) or True)

    construction_main._recalc_bid_amount(None, 18350)
    assert writes == []


def test_superseding_a_document_keeps_it_as_history():
    class _DocCursor(_Cursor):
        def fetchone(self):
            return {"id": 9}

    class _DocConn(_Conn):
        def __init__(self):
            self.cur = _DocCursor()

    conn = _DocConn()
    assert bid_opportunities.delete_document(conn, 9) is True
    assert conn.cur.sql.startswith("UPDATE bid_documents SET superseded_at")
    assert "DELETE" not in conn.cur.sql


def test_a_superseded_document_is_not_used_in_the_package():
    html = Path("templates/construction/bid_detail.html").read_text(encoding="utf-8")
    assert "{% if not d.superseded_at %}" in html
    assert "superseded" in html
    assert "/api/bids/documents/{{ d.id }}/restore" in html
    # The removed-line history and the automatic total are on the card.
    assert "kept as history, not in the total" in html
    assert "/costs/{{ c.id }}/restore" in html
    assert "/costs/{{ c.id }}/update" in html


def test_revision_labels_start_at_original_then_count():
    assert bid_costing.revision_label({"revision": 0}) == "Original"
    assert bid_costing.revision_label({"revision": None}) == "Original"
    assert bid_costing.revision_label({"revision": 1}) == "Revision 1"
    assert bid_costing.revision_label({"revision": 4}) == "Revision 4"


def test_saving_a_revision_takes_the_next_number(monkeypatch):
    ctx = {"bid": {"revision": 2}, "lines": [{"category": "material", "description": "x",
                                               "estimated": 100, "actual": 0}],
           "summary": {"estimated_cost": 100.0}, "total": 115.0,
           "markup": 15.0, "fingerprint": "abc123"}
    monkeypatch.setattr(bid_costing, "_revision_context", lambda *a, **k: ctx)
    conn = _Conn()
    assert bid_costing.save_revision(conn, 7, note="customer add") == 3
    assert conn.cur.sql.startswith("UPDATE bid_opportunities SET revision = %s")
    assert conn.cur.params[0] == 3


def test_the_save_route_refuses_an_unchanged_revision(monkeypatch):
    monkeypatch.setattr(construction_main, "require_admin", lambda request: None)
    monkeypatch.setattr(construction_main.bid_opportunities, "get_bid",
                        lambda db, bid_id, company: {"id": bid_id})
    monkeypatch.setattr(construction_main.bid_costing, "list_revisions",
                        lambda db, bid_id, company, limit=50: [{"revision": 2}])
    monkeypatch.setattr(construction_main.bid_costing, "revision_dirty",
                        lambda db, bid_id, company: False)
    monkeypatch.setattr(construction_main.bid_costing, "save_revision",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not save")))

    response = asyncio.run(construction_main.bid_save_revision(
        7, object(), note="", back="", db=None))
    assert response.status_code == 303
    assert "changed+since+Revision+2" in response.headers["location"]


def test_the_save_route_numbers_the_first_revision(monkeypatch):
    monkeypatch.setattr(construction_main, "require_admin", lambda request: None)
    monkeypatch.setattr(construction_main.bid_opportunities, "get_bid",
                        lambda db, bid_id, company: {"id": bid_id})
    monkeypatch.setattr(construction_main.bid_costing, "list_revisions",
                        lambda db, bid_id, company, limit=50: [])
    monkeypatch.setattr(construction_main.bid_costing, "save_revision",
                        lambda db, bid_id, company, note="", created_by="": 1)

    response = asyncio.run(construction_main.bid_save_revision(
        7, object(), note="first change", back="", db=None))
    assert response.status_code == 303
    assert "revision=1" in response.headers["location"]


def test_every_generated_document_prints_the_revision():
    from pathlib import Path as _Path
    docs = _Path("build_bid_documents.py").read_text(encoding="utf-8")
    package = _Path("bid_package_pdf.py").read_text(encoding="utf-8")
    assert "Revision {number}" in docs and "Original" in docs
    assert "Revision {_number}" in package and "Original" in package
    html = _Path("templates/construction/bid_detail.html").read_text(encoding="utf-8")
    assert "Save as Revision {{ (b.revision or 0) + 1 }}" in html
    assert "{{ revision_label }}" in html

