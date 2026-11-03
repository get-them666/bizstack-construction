"""The contract package and the job total must follow a revised quote.

Two failures made a revision invisible:

1. ``bid_opportunities.list_documents`` never returned ``document_type``, so the
   bid card saw every upload as "other". Uploaded SOW/quote/contract could
   never replace the generated templates, which is the whole point of the
   Contract package card.
2. Nothing recomputed ``bid_amount`` from the cost lines. A revised scope could
   change every line while the card, the generated documents and the deposit
   schedule kept quoting the old total.

No network or real database: the cursor and the money writers are faked.
"""

import asyncio
from pathlib import Path

import bid_costing
import bid_opportunities
import construction_main


def test_list_documents_returns_the_type_the_card_groups_on():
    class _Cursor:
        sql = ""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=()):
            self.sql = " ".join(sql.split())

        def fetchall(self):
            return []

    class _Conn:
        def __init__(self):
            self.cur = _Cursor()

        def cursor(self):
            return self.cur

    conn = _Conn()
    bid_opportunities.list_documents(conn, 7)
    assert "document_type" in conn.cur.sql
    assert "ORDER BY uploaded_at DESC" in conn.cur.sql


def test_quote_total_is_cost_lines_plus_markup(monkeypatch):
    monkeypatch.setattr(
        bid_costing, "cost_summary",
        lambda conn, bid_id, company: {"estimated_cost": 2591.86,
                                       "bid": {"markup_pct": 15}},
    )
    total, base, markup = bid_costing.quote_total(None, 1)
    assert base == 2591.86
    assert markup == 15.0
    assert total == 2980.64


def test_recalculate_writes_the_total_from_the_lines(monkeypatch):
    writes = []
    monkeypatch.setattr(construction_main, "require_admin", lambda request: None)
    monkeypatch.setattr(construction_main.bid_opportunities, "get_bid",
                        lambda db, bid_id, company: {"id": bid_id})
    monkeypatch.setattr(construction_main.bid_costing, "quote_total",
                        lambda db, bid_id, company: (2980.64, 2591.86, 15.0))
    monkeypatch.setattr(construction_main.bid_opportunities, "set_bid_amount",
                        lambda db, bid_id, amount, company: writes.append((bid_id, amount)) or True)

    response = asyncio.run(construction_main.bid_recalculate(18350, object(), back="", db=None))
    assert response.status_code == 303
    assert writes == [(18350, 2980.64)]
    assert "recalculated=2980.64" in response.headers["location"]


def test_recalculate_refuses_when_there_are_no_cost_lines(monkeypatch):
    writes = []
    monkeypatch.setattr(construction_main, "require_admin", lambda request: None)
    monkeypatch.setattr(construction_main.bid_opportunities, "get_bid",
                        lambda db, bid_id, company: {"id": bid_id})
    monkeypatch.setattr(construction_main.bid_costing, "quote_total",
                        lambda db, bid_id, company: (0.0, 0.0, 0.0))
    monkeypatch.setattr(construction_main.bid_opportunities, "set_bid_amount",
                        lambda *args: writes.append(args) or True)

    response = asyncio.run(construction_main.bid_recalculate(18350, object(), back="", db=None))
    assert response.status_code == 303
    assert "Add+cost+lines" in response.headers["location"]
    assert writes == []


def test_blank_price_amount_is_computed_from_the_lines(monkeypatch):
    writes = []
    monkeypatch.setattr(construction_main, "require_admin", lambda request: None)
    monkeypatch.setattr(construction_main.bid_opportunities, "_set_markup",
                        lambda db, bid_id, mp, company: True)
    monkeypatch.setattr(construction_main.bid_costing, "quote_total",
                        lambda db, bid_id, company: (2980.64, 2591.86, 15.0))
    monkeypatch.setattr(construction_main.bid_opportunities, "set_bid_amount",
                        lambda db, bid_id, amount, company: writes.append((bid_id, amount)) or True)

    asyncio.run(construction_main.bid_set_price(18350, object(), markup_pct="15",
                                                bid_amount="", back="", db=None))
    assert writes == [(18350, 2980.64)]


def test_an_explicit_price_is_not_overwritten(monkeypatch):
    writes = []
    monkeypatch.setattr(construction_main, "require_admin", lambda request: None)
    monkeypatch.setattr(construction_main.bid_opportunities, "_set_markup",
                        lambda db, bid_id, mp, company: True)
    monkeypatch.setattr(construction_main.bid_costing, "quote_total",
                        lambda db, bid_id, company: (2980.64, 2591.86, 15.0))
    monkeypatch.setattr(construction_main.bid_opportunities, "set_bid_amount",
                        lambda db, bid_id, amount, company: writes.append((bid_id, amount)) or True)

    asyncio.run(construction_main.bid_set_price(18350, object(), markup_pct="15",
                                                bid_amount="2750", back="", db=None))
    assert writes == [(18350, 2750.0)]


def test_the_card_offers_recalculate_and_prefers_uploaded_pricing():
    html = Path("templates/construction/bid_detail.html").read_text(encoding="utf-8")
    assert "/api/bids/{{ b.id }}/recalculate" in html
    assert "Recalculate job total" in html
    # Uploaded itemized pricing must be selectable, not only an uploaded quote.
    assert "{% set price_doc = item_doc or quote_doc %}" in html
    assert "price_doc.url" in html
    assert "amount_stale" in html
