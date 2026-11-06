"""Direct deposit: routing checksum, encryption round trip, NACHA file shape."""
import os
from datetime import date, datetime

import pytest
from cryptography.fernet import Fernet

import direct_deposit as dd


def test_routing_checksum():
    assert dd.valid_routing("043000096")   # PNC Pittsburgh
    assert dd.valid_routing("021000021")   # Chase
    assert not dd.valid_routing("043000097")
    assert not dd.valid_routing("12345678")
    assert not dd.valid_routing("")


def test_not_configured_without_key(monkeypatch):
    monkeypatch.delenv("BANK_ENCRYPTION_KEY", raising=False)
    assert dd.enabled() is False
    with pytest.raises(dd.NotConfigured):
        dd._fernet()


def test_nacha_shape(monkeypatch):
    monkeypatch.setenv("ACH_IMMEDIATE_DEST", "043000096")
    monkeypatch.setenv("ACH_IMMEDIATE_ORIGIN", "1123456789")
    monkeypatch.setenv("ACH_COMPANY_ID", "1123456789")
    monkeypatch.setenv("ACH_ODFI_ROUTING", "04300009")
    lines = [
        {"crew_id": 1, "name": "Ann Worker", "gross_cents": 123456, "has_bank": True,
         "account_holder": "Ann Worker", "routing_number": "021000021", "account_number": "9876543210", "account_type": "checking"},
        {"crew_id": 2, "name": "Bob Saver", "gross_cents": 50000, "has_bank": True,
         "account_holder": "Bob Saver", "routing_number": "043000096", "account_number": "1234", "account_type": "savings"},
        {"crew_id": 3, "name": "No Bank", "gross_cents": 10000, "has_bank": False},
    ]
    text, summary = dd.nacha(lines, company_name="BuildStack", created=datetime(2026, 11, 2, 9, 30))
    recs = text.split("\r\n")[:-1]
    assert all(len(r) == 94 for r in recs)
    assert len(recs) % 10 == 0
    assert recs[0].startswith("101 043000096")
    assert recs[1].startswith("5220BUILDSTACK")
    assert recs[2].startswith("622" + "02100002" + "1")
    assert recs[3].startswith("632" + "04300009" + "6")
    assert "0000123456" in recs[2] and "0000050000" in recs[3]
    assert recs[4].startswith("8220000002")
    assert recs[5].startswith("9000001")
    assert summary["entries"] == 2
    assert summary["total_cents"] == 173456
    assert summary["skipped"] == ["No Bank"]
    assert summary["missing_env"] == []
    assert summary["problems"] == []
    assert summary["effective"] == "2026-11-03"  # next business day after Monday


def test_nacha_header_destination_is_blank_plus_routing(monkeypatch):
    """A zero-padded destination is the bug that got a payroll file rejected."""
    monkeypatch.setenv("ACH_IMMEDIATE_DEST", "043000096")
    monkeypatch.setenv("ACH_IMMEDIATE_ORIGIN", "1201755044")
    monkeypatch.setenv("ACH_COMPANY_ID", "0134297210")
    monkeypatch.setenv("ACH_ODFI_ROUTING", "04300009")
    text, summary = dd.nacha(
        [{"crew_id": 1, "name": "Ann", "gross_cents": 20000, "has_bank": True,
          "account_holder": "Ann", "routing_number": "021000021",
          "account_number": "123456789", "account_type": "checking"}],
        company_name="BuildStack", created=datetime(2026, 11, 3, 9, 30))
    assert text[:13] == "101 043000096"
    assert summary["problems"] == [], summary["problems"]


def test_validate_nacha_catches_the_old_zero_padded_destination(monkeypatch):
    monkeypatch.setenv("ACH_IMMEDIATE_DEST", "043000096")
    monkeypatch.setenv("ACH_IMMEDIATE_ORIGIN", "1201755044")
    monkeypatch.setenv("ACH_COMPANY_ID", "0134297210")
    monkeypatch.setenv("ACH_ODFI_ROUTING", "04300009")
    text, _ = dd.nacha(
        [{"crew_id": 1, "name": "Ann", "gross_cents": 20000, "has_bank": True,
          "account_holder": "Ann", "routing_number": "021000021",
          "account_number": "123456789", "account_type": "checking"}],
        company_name="BuildStack", created=datetime(2026, 11, 3, 9, 30))
    broken = "1010043000096" + text[13:]
    problems = dd.validate_nacha(broken)
    assert any("blank" in p for p in problems), problems


def test_nacha_effective_skips_weekend():
    text, summary = dd.nacha([], company_name="X", created=datetime(2026, 11, 6, 17, 0))  # Friday
    assert summary["effective"] == "2026-11-09"


@pytest.mark.skipif(not os.getenv("DD_TEST_DATABASE_URL"), reason="set DD_TEST_DATABASE_URL to a scratch Postgres")
def test_save_roundtrip(monkeypatch):
    import psycopg
    from psycopg.rows import dict_row
    monkeypatch.setenv("BANK_ENCRYPTION_KEY", Fernet.generate_key().decode())
    conn = psycopg.connect(os.environ["DD_TEST_DATABASE_URL"], row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS crew_bank_accounts, payroll_lines, payroll_runs, crew CASCADE;")
        cur.execute("CREATE TABLE crew (id SERIAL PRIMARY KEY, name TEXT, bank_status TEXT);")
        cur.execute("CREATE TABLE payroll_runs (id SERIAL PRIMARY KEY, status TEXT);")
        cur.execute("CREATE TABLE payroll_lines (id SERIAL PRIMARY KEY, run_id INT, crew_id INT, gross_cents INT);")
        cur.execute("INSERT INTO crew (name) VALUES ('Ann') RETURNING id;")
        cid = cur.fetchone()["id"]
        cur.execute("INSERT INTO payroll_runs (status) VALUES ('closed') RETURNING id;")
        rid = cur.fetchone()["id"]
        cur.execute("INSERT INTO payroll_lines (run_id, crew_id, gross_cents) VALUES (%s, %s, 25000);", (rid, cid))
    conn.commit()
    dd.ensure_schema(conn)
    bad = dd.save(conn, cid, "Ann", "043000097", "123456", "checking")
    assert not bad["ok"]
    ok = dd.save(conn, cid, "Ann", "043000096", "000123456", "checking")
    assert ok["ok"] and ok["account_last4"] == "3456"
    with conn.cursor() as cur:
        cur.execute("SELECT account_enc, bank_status FROM crew_bank_accounts b JOIN crew c ON c.id = b.crew_id WHERE crew_id = %s;", (cid,))
        row = cur.fetchone()
    assert "000123456" not in row["account_enc"]
    assert row["bank_status"] == "connected"
    lines = dd.run_lines(conn, rid)
    assert lines[0]["account_number"] == "000123456"
    dd.remove(conn, cid)
    assert dd.masked(conn, cid) == {"on_file": False}
