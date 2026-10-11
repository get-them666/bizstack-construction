"""Tests for the owner-facing price book (materials_service catalog editing).

These exist because this is where real money enters the system: a wrong price
here becomes a wrong quote becomes a wrong deposit request. The price-parsing
rules are pure and always run; the database paths skip cleanly without a
scratch DB.
"""

import os

import pytest

import materials_service as M

DB_URL = os.getenv("CATALOG_TEST_DB", "postgresql://localhost/bz_scratch")


# ------------------------------------------------------------ price parsing

@pytest.mark.parametrize("raw,expected", [
    ("2.47", 247),
    ("$2.47", 247),
    ("1,234.56", 123456),
    ("12", 1200),
    (" 3.28 ", 328),
    ("(5.00)", -500),          # accounting negative
    ("0", 0),
])
def test_to_cents(raw, expected):
    assert M._to_cents(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "abc", "12abc", "$"])
def test_to_cents_rejects_garbage(raw):
    assert M._to_cents(raw) is None


# --------------------------------------------------------------- fixtures

@pytest.fixture()
def db():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    try:
        conn = psycopg.connect(DB_URL, row_factory=dict_row, connect_timeout=5)
    except Exception as exc:
        pytest.skip(f"no test database at {DB_URL}: {exc}")
    M.seed_materials_catalog(conn)
    conn.execute("DELETE FROM materials_catalog WHERE name LIKE 'zztest%';")
    conn.commit()
    yield conn
    conn.execute("DELETE FROM materials_catalog WHERE name LIKE 'zztest%';")
    conn.commit()
    conn.close()


# ------------------------------------------------------------------ upsert

def test_upsert_stores_dollars_as_cents(db):
    res = M.upsert_catalog_item(db, name="zztest elbow", price_dollars="2.47",
                                store="Kempsville", category="plumbing")
    assert res["ok"] is True
    assert res["price_cents"] == 247
    with db.cursor() as cur:
        cur.execute("SELECT price_cents, store FROM materials_catalog WHERE id=%s;",
                    (res["id"],))
        row = cur.fetchone()
    assert row["price_cents"] == 247
    assert row["store"] == "Kempsville"


def test_blank_price_is_rejected_not_stored_as_zero(db):
    """The regression: a forgotten price defaulting to 0 becomes a real $0 line
    on a customer's quote."""
    res = M.upsert_catalog_item(db, name="zztest no price", price_dollars="")
    assert res["ok"] is False
    with db.cursor() as cur:
        cur.execute("SELECT count(*) c FROM materials_catalog WHERE name='zztest no price';")
        assert cur.fetchone()["c"] == 0


def test_negative_price_is_rejected(db):
    assert M.upsert_catalog_item(db, name="zztest neg", price_dollars="-5")["ok"] is False


def test_upsert_updates_in_place(db):
    first = M.upsert_catalog_item(db, name="zztest update", price_dollars="10.00")
    second = M.upsert_catalog_item(db, name="zztest update", price_dollars="12.50",
                                   item_id=first["id"])
    assert second["ok"] is True
    assert second["id"] == first["id"]
    with db.cursor() as cur:
        cur.execute("SELECT price_cents FROM materials_catalog WHERE id=%s;", (first["id"],))
        assert cur.fetchone()["price_cents"] == 1250


# ------------------------------------------------------------ bulk import

def test_bulk_import_with_header(db):
    text = ("name,price,store,brand,category\n"
            "zztest stud,3.28,Kempsville,Southern Pine,lumber\n"
            "zztest hanger,1.15,Lowe's,Simpson,hardware")
    res = M.bulk_import_catalog(db, text)
    assert res["imported"] == 2
    assert res["errors"] == []


def test_bulk_import_without_header(db):
    res = M.bulk_import_catalog(db, "zztest a,3.28,Kempsville,x,lumber\n"
                                    "zztest b,1.15,Lowe's,y,hardware")
    assert res["imported"] == 2


def test_bulk_import_tab_separated(db):
    res = M.bulk_import_catalog(db, "zztest tab\t4.50\tKempsville")
    assert res["imported"] == 1


def test_bulk_import_uses_default_supplier(db):
    res = M.bulk_import_catalog(db, "zztest default,7.25", default_store="Kempsville")
    assert res["imported"] == 1
    with db.cursor() as cur:
        cur.execute("SELECT store FROM materials_catalog WHERE name='zztest default';")
        assert cur.fetchone()["store"] == "Kempsville"


def test_bulk_import_rejects_whole_batch_on_one_bad_row(db):
    """All-or-nothing. A half-imported price book is worse than no import: the
    rows that landed look authoritative and the ones that failed do not exist."""
    text = ("zztest good,3.28,Kempsville\n"
            "zztest bad,NOT_A_PRICE,Kempsville")
    res = M.bulk_import_catalog(db, text)
    assert res["imported"] == 0
    assert res["errors"]
    with db.cursor() as cur:
        cur.execute("SELECT count(*) c FROM materials_catalog "
                    "WHERE name IN ('zztest good','zztest bad');")
        assert cur.fetchone()["c"] == 0


def test_bulk_import_skips_blank_and_comments(db):
    res = M.bulk_import_catalog(db, "\n# a comment\nzztest real,5.00,Kempsville\n\n")
    assert res["imported"] == 1


def test_bulk_import_empty_input(db):
    assert M.bulk_import_catalog(db, "")["imported"] == 0


# ------------------------------------------------------------------ delete

def test_delete_disables_rather_than_removes(db):
    """A price that turned out wrong should not erase the fact that the item
    was ever quoted."""
    item = M.upsert_catalog_item(db, name="zztest delete me", price_dollars="3.00")
    assert M.delete_catalog_item(db, item["id"])["ok"] is True
    with db.cursor() as cur:
        cur.execute("SELECT enabled FROM materials_catalog WHERE id=%s;", (item["id"],))
        assert cur.fetchone()["enabled"] is False


# ------------------------------------------------------------------ reads

def test_catalog_rows_search(db):
    M.upsert_catalog_item(db, name="zztest findable widget", price_dollars="9.99",
                          brand="Zebra")
    rows = M.catalog_rows(db, search="findable")
    assert any(r["name"] == "zztest findable widget" for r in rows)


def test_catalog_facets(db):
    M.upsert_catalog_item(db, name="zztest facet", price_dollars="1.00",
                          store="Kempsville", category="zzcat")
    facets = M.catalog_facets(db)
    assert "Kempsville" in facets["stores"]
    assert "zzcat" in facets["categories"]