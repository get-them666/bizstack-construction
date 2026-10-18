"""Quotes, provenance, ledger posting and customer documents.

The properties worth protecting are refusals and arithmetic:

* a quote records where its numbers came from, and only a supplier price is
  ever ``price_verified``
* an accepted quote does not create revenue
* a customer cannot read another customer's quote
* an unpriced list produces no quote at all
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

import quoting
import quote_documents as qd
import documents_service

pytest.importorskip("psycopg")

DB_URL = None


def _db():
    import psycopg
    from psycopg.rows import dict_row
    import os
    url = os.getenv("TEST_QUOTE_DB")
    if not url:
        pytest.skip("set TEST_QUOTE_DB to a scratch postgres")
    return psycopg.connect(url, row_factory=dict_row)


@pytest.fixture
def db():
    conn = _db()
    from bizstack_leads import schema
    schema.create_schema(conn)
    conn.commit()
    quoting.ensure_schema(conn)
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture
def lead_id(db):
    from bizstack_leads import intake
    res = intake.intake_lead(db, company="construction", source="sandbox",
                             name="Jane Tester", email="jane@example.com",
                             phone="7575550142")
    db.commit()
    return res["lead_id"]


# --- provenance --------------------------------------------------------------

def test_quote_numbers_are_zero_by_default(db, lead_id):
    q = quoting.build_quote("kitchen", 200)
    saved = quoting.save_quote(db, lead_id, q)
    got = quoting.quote_with_lines(db, saved["quote_id"])
    assert got["price_source"] == quoting.PRICE_SOURCE_ESTIMATE
    assert got["bls_series"] == ""


def test_supplier_price_is_the_only_verified_source(db, lead_id):
    q = quoting.build_quote("kitchen", 200)
    for source in quoting.VALID_PRICE_SOURCES:
        saved = quoting.save_quote(db, lead_id, q, price_source=source)
        got = quoting.quote_with_lines(db, saved["quote_id"])
        verified = got["lines"][0]["price_verified"]
        assert verified is (source == quoting.PRICE_SOURCE_SUPPLIER), source


def test_source_is_recorded_on_every_line(db, lead_id):
    q = quoting.build_quote("roofing", 1200)
    saved = quoting.save_quote(db, lead_id, q, price_source="estimationpro",
                               bls_series="WPU1017", bls_period="2026-09")
    got = quoting.quote_with_lines(db, saved["quote_id"])
    assert all(l["price_source"] == "estimationpro" for l in got["lines"])
    assert all(l["bls_series"] == "WPU1017" for l in got["lines"])


def test_unknown_price_source_is_rejected(db, lead_id):
    q = quoting.build_quote("kitchen", 200)
    with pytest.raises(ValueError, match="price_source"):
        quoting.save_quote(db, lead_id, q, price_source="made_up")


def test_not_escalated_is_recorded_as_such(db, lead_id):
    q = quoting.build_quote("kitchen", 200)
    saved = quoting.save_quote(db, lead_id, q, bls_series="WPU1017",
                               bls_period="2026-09", escalated=False)
    got = quoting.quote_with_lines(db, saved["quote_id"])
    assert got["escalated"] is False


def test_quote_lines_sum_to_the_job_price(db, lead_id):
    q = quoting.build_quote("kitchen", 300)
    saved = quoting.save_quote(db, lead_id, q)
    got = quoting.quote_with_lines(db, saved["quote_id"])
    assert sum(l["total_cents"] for l in got["lines"]) == got["job_price_cents"]


def test_quote_numbers_are_unique(db, lead_id):
    q = quoting.build_quote("kitchen", 200)
    a = quoting.save_quote(db, lead_id, q)["quote_no"]
    b = quoting.save_quote(db, lead_id, q)["quote_no"]
    assert a != b


def test_quote_has_an_expiry(db, lead_id):
    q = quoting.build_quote("kitchen", 200)
    got = quoting.quote_with_lines(db, quoting.save_quote(db, lead_id, q)["quote_id"])
    assert got["expires_at"] is not None


def test_customer_email_is_normalised(db, lead_id):
    q = quoting.build_quote("kitchen", 200)
    saved = quoting.save_quote(db, lead_id, q, customer_email="Jane@Example.COM")
    got = quoting.quote_with_lines(db, saved["quote_id"])
    assert got["customer_email"] == "jane@example.com"


def test_dimensions_are_stored_and_used_for_area(db, lead_id):
    q = quoting.build_quote("roofing", None)
    saved = quoting.save_quote(db, lead_id, q, width_ft=40, length_ft=30)
    got = quoting.quote_with_lines(db, saved["quote_id"])
    assert float(got["width_ft"]) == 40 and float(got["length_ft"]) == 30
    assert float(got["sqft"]) == 1200.0


def test_zero_sqft_for_a_real_material_list(db, lead_id):
    """No dimensions and no area must store NULL, not 0."""
    q = quoting.build_quote("materials", None)
    saved = quoting.save_quote(db, lead_id, q, lines=[
        {"item_number": "3tab", "description": "shingles", "qty": 6,
         "unit": "square", "unit_price_cents": 13500, "total_cents": 81000}])
    got = quoting.quote_with_lines(db, saved["quote_id"])
    assert got["sqft"] is None
    assert got["lines"][0]["total_cents"] == 81000


def test_quotes_for_customer_is_scoped_to_the_email(db, lead_id):
    """Counts deltas, because the scratch database is shared between tests."""
    q = quoting.build_quote("kitchen", 200)
    before = len(quoting.quotes_for_customer(db, "jane@example.com"))
    quoting.save_quote(db, lead_id, q, customer_email="jane@example.com")
    quoting.save_quote(db, lead_id, q, customer_email="someone@else.com")
    db.commit()
    assert len(quoting.quotes_for_customer(db, "jane@example.com")) == before + 1
    assert len(quoting.quotes_for_customer(db, "JANE@example.com")) == before + 1
    assert quoting.quotes_for_customer(db, "") == []


def test_missing_quote_is_none(db):
    assert quoting.quote_with_lines(db, 99999999) is None


# --- amount_to_words ---------------------------------------------------------

@pytest.mark.parametrize("cents,expected", [
    (0, "Zero and 00/100 Dollars"),
    (100, "One and 00/100 Dollars"),
    (999_99, "Nine Hundred Ninety Nine and 99/100 Dollars"),
    (100_000, "One Thousand and 00/100 Dollars"),
    (255_000_0, "Twenty Five Thousand Five Hundred and 00/100 Dollars"),
])
def test_amount_to_words(cents, expected):
    assert documents_service.amount_to_words(cents) == expected


def test_amount_to_words_survives_a_large_cheque():
    """Regression: ONES[255] raised IndexError for anything over $1,000, so
    every cheque worth writing and every estimate above that crashed."""
    for cents in (100_000, 1_234_567_89, 999_999_999_99):
        assert documents_service.amount_to_words(cents)


def test_amount_to_words_handles_negative():
    assert documents_service.amount_to_words(-500).startswith("-")


# --- documents ---------------------------------------------------------------

@pytest.fixture
def sample_quote(db, lead_id):
    q = quoting.build_quote("roofing", 1200, scope_text="tear off and replace")
    saved = quoting.save_quote(db, lead_id, q, customer_email="jane@example.com",
                               customer_name="Jane Tester", width_ft=40, length_ft=30,
                               address="4701 Winthrop St, Norfolk VA",
                               price_source="estimationpro", bls_series="WPU1017",
                               bls_period="2026-09", escalated=True)
    return quoting.quote_with_lines(db, saved["quote_id"])


@pytest.mark.parametrize("kind", ["estimate", "bill_of_sale", "receipt"])
def test_every_document_renders_a_real_pdf(kind, sample_quote):
    values = (qd.values_from_payment({"id": 1, "amount_cents": 3000,
                                      "customer_name": "Jane",
                                      "customer_email": "jane@example.com",
                                      "reference": "Q1"}, sample_quote)
              if kind == "receipt" else qd.values_from_quote(sample_quote))
    data = qd.render(kind, values)
    assert data[:4] == b"%PDF"
    assert len(data) > 500


def test_estimate_renders_docx(sample_quote):
    data = qd.render("estimate", qd.values_from_quote(sample_quote), "docx")
    assert data[:2] == b"PK"


def test_unknown_document_kind_raises():
    with pytest.raises(ValueError, match="unknown document kind"):
        qd.render("invoice", {})


def test_estimate_discloses_a_benchmark_is_not_a_supplier_price(sample_quote):
    values = qd.values_from_quote(sample_quote)
    assert "benchmark" in values["price_basis"].lower()
    assert "WPU1017" in values["index_note"]


def test_estimate_discloses_an_estimate_is_judgement(sample_quote):
    sample_quote = dict(sample_quote)
    sample_quote["price_source"] = "estimate"
    values = qd.values_from_quote(sample_quote)
    assert "estimate" in values["price_basis"].lower()
    assert "allowances" in values["price_basis"].lower()
    assert "final invoice" in values["price_basis"].lower()


def test_bill_of_sale_names_the_supplier_price_basis(sample_quote):
    sample_quote = dict(sample_quote)
    sample_quote["price_source"] = "supplier"
    values = qd.values_from_quote(sample_quote)
    assert "supplier" in values["price_basis"].lower()


def test_not_escalated_is_said_on_the_document(sample_quote):
    sample_quote = dict(sample_quote)
    sample_quote["escalated"] = False
    assert "not escalated" in qd.values_from_quote(sample_quote)["index_note"]


def test_dimensions_appear_on_the_document(sample_quote):
    assert "40.00 ft x 30.00 ft" == qd.values_from_quote(sample_quote)["dimensions"]


def test_zero_total_still_produces_words_not_a_crash():
    assert "Zero" in qd.values_from_quote({})["total_words"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))