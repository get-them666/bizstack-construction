"""Tests for the RentCast AVM/value path.

No live calls. `_request` is monkeypatched so the network is never touched, and
the budget counter is asserted rather than trusted -- an AVM lookup that fails to
charge the shared budget is exactly the bug that would let three features overrun
one 50-call month.
"""

import pytest

import property_service as ps
import rentcast_avm_service as avm


class FakeConn:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.rows = {}

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
            self.row = {"value": self.conn.settings[key]} if key in self.conn.settings else None
            return
        if "INSERT INTO app_settings" in self.sql:
            key = params[0]
            self.conn.settings[key] = str(int(self.conn.settings.get(key) or 0) + 1)
            return
        if "SELECT payload, fetched_at" in self.sql:
            key = params[0]
            self.row = self.conn.rows.get(key)
            return
        if "INSERT INTO property_avm_cache" in self.sql:
            self.conn.rows[params[0]] = {"payload": params[1],
                                         "fetched_at": _now()}
            return

    def fetchone(self):
        return self.row


def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    ps._mem_cache.clear()
    ps._db_checked = False
    avm._avm_mem_cache.clear()
    monkeypatch.setenv("RENTCAST_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    yield
    ps._mem_cache.clear()
    ps._db_checked = False
    avm._avm_mem_cache.clear()


def _use_fake_db(monkeypatch, settings=None):
    conn = FakeConn(settings)
    monkeypatch.setattr(ps, "_db", lambda: conn)
    return conn


VALUE_RESPONSE = {
    "price": 250000,
    "priceRangeLow": 240000,
    "priceRangeHigh": 260000,
    "subjectProperty": {
        "formattedAddress": "5500 Grand Lake Dr, San Antonio, TX 78244",
        "squareFootage": 1878, "bedrooms": 3, "bathrooms": 2, "yearBuilt": 1973,
        "propertyType": "Single Family", "lastSalePrice": 270000,
    },
    "comparables": [{"id": "a"}, {"id": "b"}],
}


def _spy(monkeypatch, response=None, calls=None):
    def _req(path, params, timeout=25):
        if calls is not None:
            calls.append((path, dict(params)))
        return response if response is not None else VALUE_RESPONSE
    monkeypatch.setattr(ps, "_request", _req)


# --- parsing -----------------------------------------------------------------

def test_parses_value_estimate(monkeypatch):
    _use_fake_db(monkeypatch)
    _spy(monkeypatch)
    out = avm.estimate_value("5500 Grand Lake Dr, San Antonio, TX 78244")
    assert out["estimate"] == 250000
    assert out["range_low"] == 240000
    assert out["kind"] == "value"
    assert out["sqft"] == 1878
    assert out["comparable_count"] == 2


def test_parses_rent_estimate_shape(monkeypatch):
    """A rent response must not be read as a zero-valued estimate."""
    _use_fake_db(monkeypatch)
    _spy(monkeypatch, response={"rent": 1620, "rentRangeLow": 1550, "rentRangeHigh": 1690,
                               "subjectProperty": {}, "comparables": []})
    out = avm.estimate_value("5500 Grand Lake Dr", kind="rent")
    assert out["estimate"] == 1620
    assert out["kind"] == "rent"


def test_confidence_flags_a_wide_range(monkeypatch):
    """A wide spread means the comps disagree -- do not present it as firm."""
    _use_fake_db(monkeypatch)
    _spy(monkeypatch, response={"price": 250000, "priceRangeLow": 150000,
                               "priceRangeHigh": 350000, "subjectProperty": {}})
    assert avm.estimate_value("x")["confidence"] == "wide"


def test_empty_response_is_not_an_estimate(monkeypatch):
    _use_fake_db(monkeypatch)
    _spy(monkeypatch, response={})
    assert avm.estimate_value("x") == {}


# --- budget ------------------------------------------------------------------

def test_estimate_is_charged_once(monkeypatch):
    conn = _use_fake_db(monkeypatch)
    _spy(monkeypatch)
    avm.estimate_value("5500 Grand Lake Dr")
    assert ps.calls_remaining() == ps._month_limit() - 1


def test_avm_and_lookup_share_one_counter(monkeypatch):
    """Two features, one 50-call month. This is the assertion that matters."""
    conn = _use_fake_db(monkeypatch)
    _spy(monkeypatch)
    monkeypatch.setattr(ps, "_api_call", lambda a: {"formattedAddress": a, "squareFootage": 1000})
    ps.lookup_address("1 Real Rd")
    avm.estimate_value("5500 Grand Lake Dr")
    assert ps.calls_remaining() == ps._month_limit() - 2


def test_avm_skipped_when_budget_spent(monkeypatch):
    limit = ps._month_limit()
    _use_fake_db(monkeypatch, {f"rentcast_monthly_calls:{ps._month_key()}": str(limit)})
    _spy(monkeypatch, calls=[])
    calls = []
    _spy(monkeypatch, calls=calls)
    assert avm.estimate_value("5500 Grand Lake Dr") == {}
    assert calls == [], "a request fired with the budget already spent"


def test_failed_avm_is_still_charged(monkeypatch):
    """A 429 or a parse failure reached RentCast and cost a real call."""
    _use_fake_db(monkeypatch)

    def _boom(path, params, timeout=25):
        raise ps.RentCastError("RentCast HTTP 429", reached_api=True)

    monkeypatch.setattr(ps, "_request", _boom)
    assert avm.estimate_value("5500 Grand Lake Dr") == {}
    assert ps.calls_remaining() == ps._month_limit() - 1


def test_connection_failure_is_not_charged(monkeypatch):
    _use_fake_db(monkeypatch)

    def _boom(path, params, timeout=25):
        raise ps.RentCastError("Could not reach RentCast", reached_api=False)

    monkeypatch.setattr(ps, "_request", _boom)
    avm.estimate_value("5500 Grand Lake Dr")
    assert ps.calls_remaining() == ps._month_limit()


def test_no_key_makes_no_call(monkeypatch):
    monkeypatch.delenv("RENTCAST_API_KEY", raising=False)
    _use_fake_db(monkeypatch)
    calls = []
    _spy(monkeypatch, calls=calls)
    assert avm.estimate_value("5500 Grand Lake Dr") == {}
    assert calls == []


# --- caching -----------------------------------------------------------------

def test_second_estimate_is_free(monkeypatch):
    _use_fake_db(monkeypatch)
    calls = []
    _spy(monkeypatch, calls=calls)
    avm.estimate_value("5500 Grand Lake Dr")
    avm.estimate_value("5500 Grand Lake Dr")
    assert len(calls) == 1
    assert ps.calls_remaining() == ps._month_limit() - 1


def test_force_refetches_and_charges_again(monkeypatch):
    """An owner asking for a fresh number mid-negotiation pays for it."""
    _use_fake_db(monkeypatch)
    calls = []
    _spy(monkeypatch, calls=calls)
    avm.estimate_value("5500 Grand Lake Dr")
    avm.estimate_value("5500 Grand Lake Dr", force=True)
    assert len(calls) == 2
    assert ps.calls_remaining() == ps._month_limit() - 2


def test_stale_cache_is_refetched(monkeypatch):
    """A valuation is a market reading; a stale one must not be served."""
    conn = _use_fake_db(monkeypatch)
    monkeypatch.setattr(avm, "AVM_TTL_HOURS", 1)
    calls = []
    _spy(monkeypatch, calls=calls)

    avm.estimate_value("5500 Grand Lake Dr")
    assert len(calls) == 1

    # Age the stored row past the TTL.
    row = conn.rows[ps._key("5500 Grand Lake Dr")]
    import datetime
    row["fetched_at"] = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=48)
    avm._avm_mem_cache.clear()

    avm.estimate_value("5500 Grand Lake Dr")
    assert len(calls) == 2


# --- request shape -----------------------------------------------------------

def test_sends_documented_comp_defaults(monkeypatch):
    """Omitting these changes the answer vs. RentCast's own website."""
    _use_fake_db(monkeypatch)
    calls = []
    _spy(monkeypatch, calls=calls)
    avm.estimate_value("5500 Grand Lake Dr")
    path, params = calls[0]
    assert path == "/avm/value"
    assert params["compCount"] == 20
    assert params["maxRadius"] == 5
    assert params["daysOld"] == 270


def test_rent_uses_the_rent_path(monkeypatch):
    _use_fake_db(monkeypatch)
    calls = []
    _spy(monkeypatch, response={"rent": 1600, "subjectProperty": {}}, calls=calls)
    avm.estimate_value("5500 Grand Lake Dr", kind="rent")
    assert calls[0][0] == "/avm/rent/long-term"