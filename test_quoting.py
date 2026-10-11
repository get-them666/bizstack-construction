"""Tests for quoting.py.

The money rules are the point of this module, so they are tested first and
without a database. Persistence tests skip cleanly when there is no scratch DB.
"""

import os

import pytest

import quoting as Q

DB_URL = os.getenv("QUOTE_TEST_DB", "postgresql://localhost/bz_scratch")


# ------------------------------------------------------------------- money

def test_deposit_is_thirty_percent():
    assert Q.deposit_for(100_00) == 30_00
    assert Q.deposit_for(12_500_00) == 3_750_00
    assert Q.deposit_for(40_000_00) == 12_000_00


def test_deposit_has_no_flat_default():
    """The regression this module exists to fix: an earlier build fell back to a
    hard STRIPE_DEPOSIT_PRICE of 500.00, so a $2,000 job and a $40,000 job both
    got a $500 deposit."""
    assert Q.deposit_for(0) == 0
    assert Q.deposit_for(200_00) == 60_00          # not 500_00
    assert Q.deposit_for(200_00) != 500_00


def test_deposit_scales_with_price():
    """Two jobs must never receive the same deposit just because one is small."""
    small = Q.deposit_for(1_000_00)
    large = Q.deposit_for(40_000_00)
    assert large > small


def test_band_total_is_monotonic():
    low = Q.band_total_cents("hvac", 1800, Q.BAND_LOW)
    mid = Q.band_total_cents("hvac", 1800, Q.BAND_MID)
    high = Q.band_total_cents("hvac", 1800, Q.BAND_HIGH)
    assert low < mid < high


def test_sqft_band_scales_with_area():
    small = Q.band_total_cents("whole-home", 1000, Q.BAND_MID)
    large = Q.band_total_cents("whole-home", 2000, Q.BAND_MID)
    assert large == pytest.approx(small * 2, rel=0.01)


def test_fixed_trade_ignores_sqft():
    """Kitchen and bath are priced per project, not per foot."""
    a = Q.band_total_cents("bath", 900, Q.BAND_MID)
    b = Q.band_total_cents("bath", 2200, Q.BAND_MID)
    assert a == b


# ------------------------------------------------------------- itemization

def test_lines_sum_exactly_to_the_total():
    """A quote whose parts do not add up to the whole is the first thing a
    customer notices. Rounding drift goes into the largest line, not into a
    rounding error nobody reconciles."""
    for model in Q.TRADE_SPLIT:
        for total in (100_01, 243_000_00, 9_250_00, 7_77):
            parts = Q.split_cents(total, model)
            assert sum(p[2] for p in parts) == total, model


def test_split_preserves_categories():
    parts = Q.split_cents(100_00, "electrical")
    kinds = {p[1] for p in parts}
    assert "labor" in kinds and "material" in kinds and "permit" in kinds


def test_split_of_zero_is_still_safe():
    assert sum(p[2] for p in Q.split_cents(0, "electrical")) == 0


def test_split_of_unknown_model_does_not_explode():
    parts = Q.split_cents(50_00, "no-such-model")
    assert sum(p[2] for p in parts) == 50_00


# ------------------------------------------------------------------ trades

@pytest.mark.parametrize("raw,expected", [
    ("Electrical residential", "electrical"),
    ("Roofing & Siding", "roof"),
    ("Kitchen", "kitchen"),
    ("Flooring Contractors", "refresh"),
    ("Plumbing, Heating, and Air-Conditioning Contractors", "hvac"),
    ("Bathroom remodel", "bath"),
])
def test_normalize_trade_maps_permit_strings(raw, expected):
    assert Q.normalize_trade(raw) == expected


def test_normalize_trade_passes_model_keys_through():
    assert Q.normalize_trade("whole-home") == "whole-home"


@pytest.mark.parametrize("raw", [
    "Utilities/right-of-way",
    "Civil construction",
])
def test_unquotable_trades_are_refused(raw):
    """A utility right-of-way permit is not work this company can quote.
    Returning a confident number for one is worse than returning none."""
    assert Q.is_quotable(raw) is False


def test_quotable_trade_is_accepted():
    assert Q.is_quotable("Electrical residential") is True


# -------------------------------------------------------------- build_quote

def test_build_quote_commits_to_one_number():
    q = Q.build_quote("electrical", 1800, band="mid")
    assert q["job_price_cents"] > 0
    assert q["deposit_cents"] == Q.deposit_for(q["job_price_cents"])
    assert len(q["lines"]) > 1


def test_build_quote_deposit_is_thirty_percent_of_price():
    q = Q.build_quote("whole-home", 1500, band="mid")
    assert q["deposit_cents"] == pytest.approx(q["job_price_cents"] * 0.30, abs=1)


def test_build_quote_falls_back_to_whole_home_for_unknown_trade():
    q = Q.build_quote("something unrecognised", 1200)
    assert q["trade"] == "whole-home"


# ------------------------------------------------------------- persistence

@pytest.fixture()
def db():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    try:
        conn = psycopg.connect(DB_URL, row_factory=dict_row, connect_timeout=5)
    except Exception as exc:
        pytest.skip(f"no test database at {DB_URL}: {exc}")
    Q.ensure_schema(conn)
    conn.execute("DELETE FROM quote_lines; DELETE FROM quotes;")
    conn.commit()
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture()
def lead_id(db):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO leads (name, phone, company, status, source) "
            "VALUES ('quote test','0000000000','construction','new','test') RETURNING id;"
        )
        lid = cur.fetchone()["id"]
    db.commit()
    return lid


def test_create_quote_persists_lines_that_sum_to_the_price(db, lead_id):
    created = Q.create_quote(db, lead_id, "electrical", 1800, band="mid")
    assert created["ok"] is True
    got = Q.get_quote(db, created["id"])
    assert got["lead_id"] == lead_id
    assert sum(l["total_cents"] for l in got["lines"]) == got["job_price_cents"]
    assert got["deposit_cents"] == Q.deposit_for(got["job_price_cents"])


def test_set_status_roundtrip(db, lead_id):
    created = Q.create_quote(db, lead_id, "bath", None)
    assert Q.set_status(db, created["id"], Q.STATUS_PENDING_WALKTHROUGH)["ok"]
    assert Q.get_quote(db, created["id"])["status"] == Q.STATUS_PENDING_WALKTHROUGH
    assert Q.set_status(db, created["id"], "nonsense")["ok"] is False


def test_quotes_for_lead(db, lead_id):
    Q.create_quote(db, lead_id, "roof", 1500)
    Q.create_quote(db, lead_id, "kitchen", None)
    assert len(Q.quotes_for_lead(db, lead_id)) == 2