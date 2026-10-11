"""Tests for suppression.py.

Split deliberately:

- The normalisation and channel-matching rules are pure functions, so they are
  tested with no database at all. They always run, and they are where the
  compliance-relevant logic lives.
- The persistence paths need a real Postgres. They run against
  ``bz_scratch`` by default and skip cleanly if there is not one, so this file
  never becomes the thing that takes the suite down.
"""

import os

import pytest

import suppression as S

DB_URL = os.getenv("SUPPRESSION_TEST_DB", "postgresql://localhost/bz_scratch")


# ---------------------------------------------------------------- pure rules

@pytest.mark.parametrize("raw,expected", [
    ("  Foo.Bar@Example.COM ", "foo.bar@example.com"),
    ("a.b+c@gmail.com", "a.b+c@gmail.com"),
    ("Lead@Permit-Finder.gov", "lead@permit-finder.gov"),
    ("nope", ""),
    ("@example.com", ""),
    ("", ""),
    (None, ""),
])
def test_normalize_email(raw, expected):
    assert S.normalize_email(raw) == expected


def test_normalize_email_does_not_strip_dots_or_plus_tags():
    """A plus tag is not a different person for suppression purposes, but the
    local part is opaque -- stripping dots would merge real addresses."""
    assert S.normalize_email("a.b+c@x.com") != S.normalize_email("ab@x.com")


@pytest.mark.parametrize("raw,expected", [
    ("(757) 908-7121", "+17579087121"),
    ("757-908-7121", "+17579087121"),
    ("+1 757 908 7121", "+17579087121"),
    ("17579087121", "+17579087121"),
    ("123", ""),
    ("", ""),
])
def test_normalize_phone(raw, expected):
    assert S.normalize_phone(raw) == expected


def test_normalize_name_zip():
    assert S.normalize_name_zip("Shaun  O'Leary", "23321-1234") == "shaun oleary 23321"
    assert S.normalize_name_zip("", "23321") == ""
    assert S.normalize_name_zip("Smith", "abc") == ""


@pytest.mark.parametrize("entry_channel,channel,expected", [
    ("any", "email", True),
    ("any", "sms", True),
    ("email", "email", True),
    ("email", "sms", False),
    ("phone", "sms", True),        # DNC is a phone registry
    ("phone", "voice", True),
    ("phone", "email", False),     # but it is not an email opt-out
    ("sms", "sms", True),
    ("sms", "voice", False),
])
def test_channel_blocked(entry_channel, channel, expected):
    assert S._channel_blocked(entry_channel, channel) is expected


def test_dnc_blocks_text_not_just_calls():
    """The regression that matters: a DNC hit on a mobile number must stop the
    SMS path too. Blocking voice while letting the text through is not
    compliance, it is just a different channel."""
    assert S._channel_blocked("phone", "sms") is True


def test_unknown_kind_is_rejected():
    assert S.KIND_DNC in S.KIND_BLOCKS_CHANNEL


# ------------------------------------------------------------- persistence

@pytest.fixture()
def db():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    try:
        conn = psycopg.connect(DB_URL, row_factory=dict_row, connect_timeout=5)
    except Exception as exc:            # no local scratch DB on this machine
        pytest.skip(f"no test database at {DB_URL}: {exc}")
    S.ensure_schema(conn)
    conn.execute("DELETE FROM suppression_audit;")
    conn.execute("DELETE FROM suppression_list;")
    conn.commit()
    yield conn
    conn.rollback()
    conn.close()


def test_add_is_idempotent(db):
    first = S.add(db, "dnc", "phone", "(757) 908-7121", channel="phone", reason="n")
    second = S.add(db, "dnc", "phone", "7579087121", channel="phone", reason="n")
    assert first["ok"] and second["ok"]
    assert first["id"] == second["id"]
    assert len(S.listing(db)) == 1


def test_add_rejects_unnormalisable(db):
    assert S.add(db, "dnc", "phone", "123")["ok"] is False
    assert S.add(db, "nonsense", "email", "a@b.com")["ok"] is False


def test_channel_scoped_suppression(db):
    S.add(db, "dnc", "phone", "7579087121", channel="phone", reason="registry")
    assert S.is_suppressed(db, phone="7579087121", channel="sms")[0] is True
    assert S.is_suppressed(db, phone="7579087121", channel="voice")[0] is True
    assert S.is_suppressed(db, phone="7579087121", channel="email")[0] is False


def test_internal_suppression_blocks_every_channel(db):
    S.add(db, "internal", "email", "stop@x.com", reason="owner asked")
    for channel in ("email", "sms", "mail", "voice"):
        assert S.is_suppressed(db, email="stop@x.com", channel=channel)[0] is True


def test_honor_opt_out_blocks_both_identifiers(db):
    S.honor_opt_out(db, email="go@x.com", phone="7575550100", reason="replied STOP")
    assert S.is_suppressed(db, email="go@x.com", channel="email")[0] is True
    assert S.is_suppressed(db, phone="7575550100", channel="sms")[0] is True


def test_remove_restores(db):
    S.add(db, "internal", "email", "stop@x.com")
    assert S.is_suppressed(db, email="stop@x.com", channel="email")[0] is True
    S.remove(db, "email", "stop@x.com", actor="test", reason="consent")
    assert S.is_suppressed(db, email="stop@x.com", channel="email")[0] is False


def test_remove_keeps_the_row_for_audit(db):
    entry = S.add(db, "internal", "email", "stop@x.com")
    S.remove(db, "email", "stop@x.com", actor="test")
    with db.cursor() as cur:
        cur.execute("SELECT active FROM suppression_list WHERE id = %s;", (entry["id"],))
        assert cur.fetchone()["active"] is False
        cur.execute("SELECT action FROM suppression_audit ORDER BY id;")
        assert [r["action"] for r in cur.fetchall()] == ["add", "remove"]


def test_lookup_does_not_poison_the_transaction(db):
    """A row-constructor IN list cannot be bound by psycopg. When that regressed
    it raised inside an active transaction and every later statement in the
    request failed with 'current transaction is aborted' -- a suppression check
    that breaks the send it was meant to protect."""
    S.add(db, "internal", "email", "a@b.com")
    S.is_suppressed(db, email="a@b.com", channel="email")
    with db.cursor() as cur:
        cur.execute("SELECT 1 AS ok;")
        assert cur.fetchone()["ok"] == 1


# ------------------------------------------------------------------- the gate

def test_check_outbound_fails_closed_without_a_database():
    allowed, reason = S.check_outbound(None, "email", "a@b.com")
    assert allowed is False
    assert "fail" in reason.lower() or "no database" in reason.lower()


def test_check_outbound_blocks_a_suppressed_address(db):
    S.add(db, "dma", "email", "blocked@x.com", channel="email", reason="DMAchoice")
    allowed, reason = S.check_outbound(db, "email", "blocked@x.com")
    assert allowed is False
    assert "suppressed" in reason.lower()