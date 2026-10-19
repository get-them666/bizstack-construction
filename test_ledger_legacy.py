"""Ledger writes against a database that predates the double-entry rewrite.

``ledger_entries.tx_type`` survived in production as NOT NULL with no default
because CREATE TABLE IF NOT EXISTS never drops a column. Every write to the
ledger failed there. These tests rebuild that legacy shape deliberately, so the
migration and the derived value are exercised rather than assumed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

import ledger

pytest.importorskip("psycopg")


def _conn():
    import psycopg
    from psycopg.rows import dict_row
    import os
    url = os.getenv("TEST_LEDGER_DB")
    if not url:
        pytest.skip("set TEST_LEDGER_DB to a scratch postgres")
    return psycopg.connect(url, row_factory=dict_row)


@pytest.fixture
def legacy_db():
    """A ledger_entries shaped the way the live database is."""
    conn = _conn()
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS ledger_lines, ledger_entries CASCADE;")
        cur.execute("""
            CREATE TABLE ledger_entries (
                id          SERIAL PRIMARY KEY,
                entry_no    VARCHAR(40) NOT NULL,
                entry_date  DATE NOT NULL,
                memo        TEXT,
                tx_type     VARCHAR(20) NOT NULL,
                company     VARCHAR(40),
                created_at  TIMESTAMPTZ DEFAULT NOW()
            );
        """)
    conn.commit()
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture
def db():
    conn = _conn()
    ledger.ensure_schema(conn)
    yield conn
    conn.rollback()
    conn.close()


def test_migration_gives_tx_type_a_default(legacy_db):
    """The whole failure was a NOT NULL column with no default."""
    ledger.ensure_schema(legacy_db)
    with legacy_db.cursor() as cur:
        cur.execute("""
            SELECT column_default, is_nullable FROM information_schema.columns
             WHERE table_name = 'ledger_entries' AND column_name = 'tx_type';
        """)
        row = cur.fetchone()
    assert row["column_default"] is not None, "tx_type still has no default"
    assert row["is_nullable"] == "NO"


def test_migration_backfills_existing_nulls(legacy_db):
    with legacy_db.cursor() as cur:
        cur.execute("ALTER TABLE ledger_entries ALTER COLUMN tx_type DROP NOT NULL;")
        cur.execute("""INSERT INTO ledger_entries (entry_no, entry_date, tx_type, company)
                       VALUES ('JE-OLD', CURRENT_DATE, NULL, 'construction');""")
    legacy_db.commit()
    ledger.ensure_schema(legacy_db)
    with legacy_db.cursor() as cur:
        cur.execute("SELECT tx_type FROM ledger_entries WHERE entry_no = 'JE-OLD';")
        assert cur.fetchone()["tx_type"] == "journal"


def test_write_succeeds_on_a_legacy_schema(legacy_db):
    ledger.ensure_schema(legacy_db)
    res = ledger.post(legacy_db, [
        {"account": "1010", "debit": 100000, "description": "Cash"},
        {"account": "3010", "credit": 100000, "description": "Capital"},
    ], memo="Opening balances", source_type="open", source_id="1")
    assert res["posted"] is True


def test_opening_the_books_works_on_a_legacy_schema(legacy_db):
    """The exact call that 500'd on /books/open."""
    ledger.ensure_schema(legacy_db)
    res = ledger.open_books(legacy_db, 250000)
    assert res["posted"] is True


def test_opening_balance_is_not_income(legacy_db):
    ledger.ensure_schema(legacy_db)
    ledger.open_books(legacy_db, 250000)
    with legacy_db.cursor() as cur:
        cur.execute("SELECT tx_type FROM ledger_entries WHERE source_type = 'open';")
        assert cur.fetchone()["tx_type"] == "journal"


def test_an_expense_entry_is_labelled_expense(legacy_db):
    ledger.ensure_schema(legacy_db)
    import ledger_bridge
    ledger_bridge.record_operating_expense(legacy_db, "6020", 4500, "fuel")
    with legacy_db.cursor() as cur:
        cur.execute("SELECT tx_type FROM ledger_entries WHERE source_type = 'opex';")
        assert cur.fetchone()["tx_type"] == "expense"


def test_a_revenue_entry_is_labelled_income(legacy_db):
    ledger.ensure_schema(legacy_db)
    with legacy_db.cursor() as cur:
        cur.execute("SELECT count(*) n FROM ledger_accounts WHERE company='construction';")
        if not cur.fetchone()["n"]:
            ledger.seed_chart(legacy_db, "construction")
    ledger.post(legacy_db, [
        {"account": "1010", "debit": 50000, "description": "Cash"},
        {"account": "4010", "credit": 50000, "description": "Contract revenue"},
    ], memo="Invoice", source_type="invoice", source_id="inv-1")
    with legacy_db.cursor() as cur:
        cur.execute("SELECT tx_type FROM ledger_entries WHERE source_type = 'invoice';")
        assert cur.fetchone()["tx_type"] == "income"


def test_revenue_entries_are_findable_by_tx_type(legacy_db):
    """tx_type was left NULL on every entry, so anything filtering on it saw
    nothing. Revenue is now reachable without summing a balanced entry."""
    ledger.ensure_schema(legacy_db)
    with legacy_db.cursor() as cur:
        cur.execute("SELECT count(*) n FROM ledger_accounts WHERE company='construction';")
        if not cur.fetchone()["n"]:
            ledger.seed_chart(legacy_db, "construction")
    ledger.post(legacy_db, [
        {"account": "1010", "debit": 75000, "description": "Cash"},
        {"account": "4010", "credit": 75000, "description": "Revenue"},
    ], memo="Invoice", source_type="invoice", source_id="inv-9")
    with legacy_db.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(SUM(l.credit_cents),0) AS revenue
              FROM ledger_lines l JOIN ledger_entries e ON e.id = l.entry_id
             WHERE e.tx_type IN ('income','revenue') AND l.account_code LIKE '4%%';
        """)
        assert cur.fetchone()["revenue"] == 75000


def test_tx_type_cannot_disagree_with_its_own_lines():
    """It is derived, never passed in, so a caller cannot mislabel an entry."""
    prepared = [{"account": "5010", "debit": 100, "credit": 0},
                {"account": "1100", "credit": 100}]
    assert ledger._tx_type(prepared) == "expense"
    assert ledger._tx_type([{"account": "4010", "debit": 0, "credit": 100},
                            {"account": "1010", "debit": 100, "credit": 0}]) == "income"
    assert ledger._tx_type([{"account": "2100", "debit": 100, "credit": 0},
                            {"account": "1010", "credit": 100}]) == "journal"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))