"""Budget accounting for RentCast lookups.

The whole point of this file is the 50-calls-per-month ceiling. A free-tier key
cannot be spent on tests, so nothing here touches the network: `_api_call` is
monkeypatched and the Postgres cache is faked. Every assertion below is about
whether a real RentCast charge was recorded, never about live data.

The regression these lock down: previously `_record_call` ran only on the happy
path. A 404, a 429, a 500, and an empty result set all reached RentCast and were
all billed, but none of them incremented the counter. The module would happily
fire 200 live requests while reporting that it had used none.
"""

import json

import pytest

import property_service as ps


class FakeConn:
    """Minimal stand-in for a psycopg connection.

    Records the budget counter in memory so the month limit can actually be
    reached inside a test, which is the only way to prove the ceiling holds.
    """

    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.cache = {}

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())
        if "CREATE TABLE" in self.sql:
            return
        if self.sql.startswith("SELECT value FROM app_settings"):
            key = params[0]
            if key in self.conn.settings:
                self.row = {"value": self.conn.settings[key]}
            else:
                self.row = None
            return
        if "INSERT INTO app_settings" in self.sql:
            key = params[0]
            current = int(self.conn.settings.get(key) or 0)
            self.conn.settings[key] = str(current + 1)
            return
        if "SELECT payload FROM" in self.sql:
            key = params[0]
            self.row = {"payload": self.conn.cache.get(key)}
            return
        if "INSERT INTO property_lookup_cache" in self.sql:
            self.conn.cache[params[0]] = params[1]
            return

    def fetchone(self):
        return self.row


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """No key by default, no shared cache, no cross-test leakage."""
    ps._mem_cache.clear()
    ps._db_checked = False
    monkeypatch.setenv("RENTCAST_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    yield
    ps._mem_cache.clear()
    ps._db_checked = False


def _use_fake_db(monkeypatch, settings=None):
    conn = FakeConn(settings)
    monkeypatch.setattr(ps, "_db", lambda: conn)
    return conn


def calls_this_month(conn):
    return ps._calls_this_month(conn)


def _never_called(monkeypatch):
    """Fail loudly if a test accidentally reaches the network."""
    def _boom(address):
        raise AssertionError(f"live RentCast call attempted for {address!r}")
    monkeypatch.setattr(ps, "_api_call", _boom)


# --- the regression ---------------------------------------------------------

def test_empty_result_is_counted(monkeypatch):
    """A 200 with no match is billed by RentCast and must be counted."""
    conn = _use_fake_db(monkeypatch)
    monkeypatch.setattr(ps, "_api_call", lambda address: None)

    assert ps.lookup_address("1 Nowhere Rd, Chesapeake, VA 23320") is None
    assert calls_this_month(conn) == 1


def test_http_error_is_counted(monkeypatch):
    """A 429 means the quota is already blown -- not that this call was free."""
    conn = _use_fake_db(monkeypatch)

    def _boom(address):
        raise ps.RentCastError("RentCast HTTP 429", reached_api=True)

    monkeypatch.setattr(ps, "_api_call", _boom)
    assert ps.lookup_address("1 Nowhere Rd, Chesapeake, VA 23320") is None
    assert calls_this_month(conn) == 1


def test_unreadable_body_is_counted(monkeypatch):
    """HTTP 200 with a broken body is still a billed request."""
    conn = _use_fake_db(monkeypatch)

    def _boom(address):
        raise ps.RentCastError("unreadable body", reached_api=True)

    monkeypatch.setattr(ps, "_api_call", _boom)
    assert ps.lookup_address("1 Nowhere Rd, Chesapeake, VA 23320") is None
    assert calls_this_month(conn) == 1


def test_connection_failure_is_not_counted(monkeypatch):
    """Nothing left the box, so nothing was charged."""
    conn = _use_fake_db(monkeypatch)

    def _boom(address):
        raise ps.RentCastError("Could not reach RentCast", reached_api=False)

    monkeypatch.setattr(ps, "_api_call", _boom)
    assert ps.lookup_address("1 Nowhere Rd, Chesapeake, VA 23320") is None
    assert calls_this_month(conn) == 0


def test_success_is_counted(monkeypatch):
    conn = _use_fake_db(monkeypatch)
    monkeypatch.setattr(ps, "_api_call", lambda address: {
        "formattedAddress": address, "squareFootage": 1878, "bedrooms": 3,
    })
    assert ps.lookup_address("1 Real Rd, Chesapeake, VA 23320") is not None
    assert calls_this_month(conn) == 1


# --- misses must not be re-bought ------------------------------------------

def test_miss_is_cached_and_free_on_repeat(monkeypatch):
    """A bad address must not cost a call on every quote attempt."""
    conn = _use_fake_db(monkeypatch)
    calls = []

    def _api(address):
        calls.append(address)
        return None

    monkeypatch.setattr(ps, "_api_call", _api)

    for _ in range(5):
        assert ps.lookup_address("1 Nowhere Rd, Chesapeake, VA 23320") is None

    assert len(calls) == 1, "the miss was re-bought instead of served from cache"
    assert calls_this_month(conn) == 1


def test_cached_miss_does_not_leak_into_a_later_hit(monkeypatch):
    """A cached miss must never be mistaken for a cached property."""
    conn = _use_fake_db(monkeypatch)
    responses = [None, {"formattedAddress": "1 Nowhere Rd", "squareFootage": 1500}]
    monkeypatch.setattr(ps, "_api_call", lambda address: responses.pop(0))

    assert ps.lookup_address("1 Nowhere Rd") is None
    assert ps.lookup_address("1 Nowhere Rd") is None


def test_hit_is_served_from_cache(monkeypatch):
    conn = _use_fake_db(monkeypatch)
    calls = []

    def _api(address):
        calls.append(address)
        return {"formattedAddress": address, "squareFootage": 1878}

    monkeypatch.setattr(ps, "_api_call", _api)
    ps.lookup_address("1 Real Rd")
    ps.lookup_address("1 Real Rd")
    assert len(calls) == 1
    assert calls_this_month(conn) == 1


# --- the ceiling itself -----------------------------------------------------

def test_budget_stops_further_calls(monkeypatch):
    """With the budget exhausted, a fresh address must not reach RentCast."""
    limit = ps._month_limit()
    conn = _use_fake_db(monkeypatch, {f"rentcast_monthly_calls:{ps._month_key()}": str(limit)})

    def _api(address):
        raise AssertionError("call fired past the monthly budget")

    monkeypatch.setattr(ps, "_api_call", _api)
    assert ps.lookup_address("5500 Grand Lake Dr, San Antonio, TX 78244") is None


def test_budget_allows_the_final_call(monkeypatch):
    """50 spent of 50 means stop; 49 spent of 50 means the 50th still runs."""
    limit = ps._month_limit()
    conn = _use_fake_db(monkeypatch, {f"rentcast_monthly_calls:{ps._month_key()}": str(limit - 1)})
    monkeypatch.setattr(ps, "_api_call", lambda address: {
        "formattedAddress": address, "squareFootage": 1878,
    })
    assert ps.lookup_address("5500 Grand Lake Dr") is not None
    assert calls_this_month(conn) == limit


def test_no_key_configured_makes_no_call(monkeypatch):
    monkeypatch.delenv("RENTCAST_API_KEY", raising=False)
    _use_fake_db(monkeypatch)
    _never_called(monkeypatch)
    assert ps.lookup_address("5500 Grand Lake Dr") is None
    assert ps.is_configured() is False


# --- field mapping ----------------------------------------------------------

def test_lot_size_reads_the_documented_field(monkeypatch):
    """lotSize is the documented field; lotSizeSqFt never existed."""
    _use_fake_db(monkeypatch)
    monkeypatch.setattr(ps, "_api_call", lambda address: {
        "formattedAddress": address, "lotSize": 8843, "squareFootage": 1878,
    })
    assert ps.lookup_address("5500 Grand Lake Dr")["lot_sqft"] == 8843