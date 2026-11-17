"""A generated bid document must render the same bytes every time.

fpdf2 stamps ``datetime.now()`` into ``/CreationDate`` and the document ``/ID``,
and the documents themselves used to print ``date.today()`` in the header. So the
same untouched bid produced a different file on every request and a different
file again the next morning.

That quietly emptied the hash stored on every signing request: it could show that
a document was rendered, never that the PDF the client signed was the PDF that was
sent. These tests pin both halves of the fix -- the stamp comes from the bid row,
and it is what the documents actually print.
"""
from datetime import date, datetime, timezone
import io

import pypdf

import bid_package_pdf
import bid_proposal
import build_bid_documents
import pdf_stamp

UPDATED = datetime(2026, 11, 17, 19, 24, 34, tzinfo=timezone.utc)
CREATED = datetime(2026, 11, 13, 4, 9, 1, tzinfo=timezone.utc)

BID = {
    "id": 18393,
    "name": "Chase Bank - 606 First Colonial Rd, Virginia Beach",
    "location": "606 First Colonial Road, Virginia Beach, VA 23451",
    "contact_name": "Brooke White",
    "contact_email": "bmastro@pro-vost.com",
    "bid_amount": 100828.00,
    "revision": 2,
    "due_date": "2026-12-02",
    "trade": "Flooring & Demolition",
    "role": "sub",
    "updated_at": UPDATED,
    "created_at": CREATED,
}

COSTS = [
    {"id": 1, "category": "labor", "description": "Selective demolition", "qty": 3450,
     "unit": "sf", "unit_cost": 5.75, "estimated": 19837.50},
    {"id": 2, "category": "sub", "description": "Dumpsters, haul-off", "qty": 6,
     "unit": "ea", "unit_cost": 725, "estimated": 4350.00},
]


def _text(raw) -> str:
    return "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(raw)).pages)


# --- the stamp itself ------------------------------------------------------

def test_uses_when_the_bid_was_last_edited():
    assert pdf_stamp.document_date(BID) == UPDATED


def test_falls_back_to_created_when_never_edited():
    assert pdf_stamp.document_date({"created_at": CREATED}) == CREATED


def test_reads_the_iso_strings_a_form_or_a_test_fixture_supplies():
    assert pdf_stamp.document_date({"updated_at": "2026-11-17T19:24:34+00:00"}) == UPDATED


def test_a_row_with_no_timestamps_still_renders_deterministically():
    first = pdf_stamp.document_date({})
    second = pdf_stamp.document_date(None)
    assert first == second
    assert first.year == 1970


def test_day_text_is_the_date_that_gets_printed():
    assert pdf_stamp.day_text(BID) == "November 17, 2026"


# --- documents are reproducible -------------------------------------------

def test_every_bid_document_renders_identical_bytes_twice():
    builders = [
        lambda: build_bid_documents.build_contract(dict(BID), COSTS, []),
        lambda: build_bid_documents.build_scope_of_work(dict(BID), COSTS, []),
        lambda: build_bid_documents.build_itemized_pricing(dict(BID), COSTS),
        lambda: bid_package_pdf.generate_bid_package_pdf(dict(BID), COSTS, 0),
        lambda: bid_proposal.generate_proposal_pdf(dict(BID), COSTS, {}, 0),
    ]
    for build in builders:
        assert build() == build()


def test_a_revision_is_still_a_different_document():
    # Reproducible must not mean frozen: editing the bid moves the stamp, so the
    # contract re-issued after a re-bid cannot hash the same as the one before it.
    before = build_bid_documents.build_contract(dict(BID), COSTS, [])
    edited = dict(BID)
    edited["updated_at"] = datetime(2026, 12, 1, 9, 0, 0, tzinfo=timezone.utc)
    after = build_bid_documents.build_contract(edited, COSTS, [])
    assert before != after


def test_price_change_changes_the_document():
    before = build_bid_documents.build_contract(dict(BID), COSTS, [])
    raised = dict(BID)
    raised["bid_amount"] = 120000.00
    assert build_bid_documents.build_contract(raised, COSTS, []) != before


# --- the printed date, not the render date --------------------------------

def test_contract_is_dated_by_the_bid_not_by_the_clock():
    # A contract that re-dates itself every morning is a different document each
    # morning, which is exactly what makes the stored hash useless.
    text = _text(build_bid_documents.build_contract(dict(BID), COSTS, []))
    assert "Date: November 17, 2026" in text


def test_package_is_dated_by_the_bid_not_by_the_clock():
    text = _text(bid_package_pdf.generate_bid_package_pdf(dict(BID), COSTS, 0))
    assert "Proposal date: November 17, 2026" in text


def test_emailed_proposal_is_dated_by_the_bid_not_by_the_clock():
    text = _text(bid_proposal.generate_proposal_pdf(dict(BID), COSTS, {}, 0))
    assert "Proposal Date: November 17, 2026" in text
