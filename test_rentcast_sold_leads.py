"""Tests for the recently-sold lead sweep.

The network is never touched: `property_service._request` is monkeypatched. The
assertions concentrate on two things that would cost real money if wrong --
exactly one call per sweep, and a spent month refusing the request rather than
making it.
"""

import pytest

import property_service as ps
import rentcast_sold_leads as sold


class FakeConn:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.rows = set()

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
        if "CREATE TABLE" in self.sql or "CREATE INDEX" in self.sql:
            return
        if self.sql.startswith("SELECT value FROM app_settings"):
            key = params[0]
            self.row = {"value": self.conn.settings[key]} if key in self.conn.settings else None
            return
        if "INSERT INTO app_settings" in self.sql:
            key = params[0]
            self.conn.settings[key] = str(int(self.conn.settings.get(key) or 0) + 1)
            return
        if self.sql.startswith("SELECT 1 FROM sold_lead_candidates"):
            self.row = {"1": 1} if params[0] in self.conn.rows else None
            return
        if "INSERT INTO sold_lead_candidates" in self.sql:
            self.conn.rows.add(params[0])
            return

    def fetchone(self):
        return self.row


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    ps._mem_cache.clear()
    ps._db_checked = False
    sold._sweep_cache.clear()
    monkeypatch.setenv("RENTCAST_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    yield
    ps._mem_cache.clear()
    ps._db_checked = False
    sold._sweep_cache.clear()


def _use_fake_db(monkeypatch, settings=None):
    conn = FakeConn(settings)
    monkeypatch.setattr(ps, "_db", lambda: conn)
    return conn


def _spy(monkeypatch, response, calls):
    def _req(path, params, timeout=25):
        calls.append((path, dict(params)))
        return response
    monkeypatch.setattr(ps, "_request", _req)


def _prop(**over):
    base = {
        "id": "5500-Grand-Lake-Dr,-San-Antonio,-TX-78244",
        "formattedAddress": "5500 Grand Lake Dr, San Antonio, TX 78244",
        "city": "San Antonio", "state": "TX", "zipCode": "78244",
        "propertyType": "Single Family",
        "bedrooms": 3, "bathrooms": 2, "squareFootage": 1878, "yearBuilt": 1973,
        "lastSaleDate": "2026-08-01T00:00:00.000Z", "lastSalePrice": 270000,
        "ownerOccupied": True,
        "owner": {"names": ["Rolando Villarreal"], "type": "Individual",
                  "mailingAddress": {"formattedAddress": "1 Elsewhere Rd, San Antonio, TX 78201"}},
    }
    base.update(over)
    return base


# --- candidate shaping -------------------------------------------------------

def test_extracts_name_and_addresses():
    c = sold.to_candidate(_prop())
    assert c["owner_name"] == "Rolando Villarreal"
    assert c["address"] == "5500 Grand Lake Dr, San Antonio, TX 78244"
    assert c["mailing_address"] == "1 Elsewhere Rd, San Antonio, TX 78201"
    assert c["sale_price"] == 270000


def test_email_and_phone_are_explicitly_empty():
    """RentCast has no contact endpoint. The fields exist so nothing assumes."""
    c = sold.to_candidate(_prop())
    assert c["email"] == "" and c["phone"] == ""


def test_skips_records_with_no_owner():
    """A door with no owner of record is not a canvas target."""
    assert sold.to_candidate(_prop(owner={"names": []})) is None


def test_skips_non_residential():
    assert sold.to_candidate(_prop(propertyType="Land")) is None
    assert sold.to_candidate(_prop(propertyType="Apartment")) is None


def test_skips_homes_with_no_equity():
    assert sold.to_candidate(_prop(lastSalePrice=40000)) is None


def test_old_homes_score_higher():
    old = sold.to_candidate(_prop(yearBuilt=1948))
    new = sold.to_candidate(_prop(yearBuilt=2015))
    assert old["score"] > new["score"]
    assert any("built 1948" in r for r in old["reasons"])


def test_owner_occupied_scores_higher():
    occupied = sold.to_candidate(_prop(ownerOccupied=True))
    renter = sold.to_candidate(_prop(ownerOccupied=False))
    assert occupied["score"] > renter["score"]


def test_mailing_address_same_as_property_is_not_a_signal():
    same = sold.to_candidate(_prop(owner={"names": ["A B"],
                    "mailingAddress": {"formattedAddress": "5500 Grand Lake Dr, San Antonio, TX 78244"}}))
    assert not any("mailing address differs" in r for r in same["reasons"])


# --- the sweep ---------------------------------------------------------------

def test_one_call_returns_many_leads(monkeypatch):
    """The economics: 500 records per call, not one call per record."""
    _use_fake_db(monkeypatch)
    calls = []
    _spy(monkeypatch, [_prop(id=f"p{i}", formattedAddress=f"{i} Main St, Austin, TX 73301")
                       for i in range(50)], calls)
    out = sold.find_recent_sales({"zipCode": "73301"})
    assert out["ok"] and len(out["leads"]) == 50
    assert len(calls) == 1
    assert out["calls_spent"] == 1


def test_sweep_sends_the_recent_sale_window(monkeypatch):
    _use_fake_db(monkeypatch)
    calls = []
    _spy(monkeypatch, [_prop()], calls)
    sold.find_recent_sales({"zipCode": "78244"}, days=90)
    _path, params = calls[0]
    assert params["saleDateRange"] == 90
    assert params["limit"] == 500
    assert "Single Family" in params["propertyType"]


def test_sweep_is_charged_to_the_shared_budget(monkeypatch):
    _use_fake_db(monkeypatch)
    _spy(monkeypatch, [_prop()], [])
    sold.find_recent_sales({"zipCode": "78244"})
    assert ps.calls_remaining() == ps._month_limit() - 1


def test_repeat_sweep_is_free(monkeypatch):
    _use_fake_db(monkeypatch)
    calls = []
    _spy(monkeypatch, [_prop()], calls)
    sold.find_recent_sales({"zipCode": "78244"})
    sold.find_recent_sales({"zipCode": "78244"})
    assert len(calls) == 1
    assert ps.calls_remaining() == ps._month_limit() - 1


def test_different_area_is_a_separate_call(monkeypatch):
    _use_fake_db(monkeypatch)
    calls = []
    _spy(monkeypatch, [_prop()], calls)
    sold.find_recent_sales({"zipCode": "78244"})
    sold.find_recent_sales({"zipCode": "73301"})
    assert len(calls) == 2


def test_spent_budget_refuses_before_calling(monkeypatch):
    limit = ps._month_limit()
    _use_fake_db(monkeypatch, {f"rentcast_monthly_calls:{ps._month_key()}": str(limit)})
    calls = []
    _spy(monkeypatch, [_prop()], calls)
    out = sold.find_recent_sales({"zipCode": "78244"})
    assert out["ok"] is False
    assert calls == [], "a sweep fired with the budget already spent"
    assert "budget" in out["reason"].lower()


def test_no_key_is_a_clean_no_op(monkeypatch):
    monkeypatch.delenv("RENTCAST_API_KEY", raising=False)
    _use_fake_db(monkeypatch)
    calls = []
    _spy(monkeypatch, [_prop()], calls)
    out = sold.find_recent_sales({"zipCode": "78244"})
    assert out["ok"] is False and out["leads"] == [] and calls == []


def test_failed_sweep_is_still_charged(monkeypatch):
    _use_fake_db(monkeypatch)

    def _boom(path, params, timeout=25):
        raise ps.RentCastError("RentCast HTTP 429", reached_api=True)

    monkeypatch.setattr(ps, "_request", _boom)
    out = sold.find_recent_sales({"zipCode": "78244"})
    assert out["ok"] is False
    assert ps.calls_remaining() == ps._month_limit() - 1


def test_network_failure_is_not_charged(monkeypatch):
    _use_fake_db(monkeypatch)

    def _boom(path, params, timeout=25):
        raise ps.RentCastError("Could not reach RentCast", reached_api=False)

    monkeypatch.setattr(ps, "_request", _boom)
    sold.find_recent_sales({"zipCode": "78244"})
    assert ps.calls_remaining() == ps._month_limit()


def test_empty_response_is_not_an_error(monkeypatch):
    _use_fake_db(monkeypatch)
    _spy(monkeypatch, [], [])
    out = sold.find_recent_sales({"zipCode": "73301"})
    assert out["ok"] and out["leads"] == []


def test_results_are_sorted_by_score(monkeypatch):
    _use_fake_db(monkeypatch)
    _spy(monkeypatch, [_prop(id="a", yearBuilt=2015, lastSalePrice=160000),
                       _prop(id="b", yearBuilt=1940, lastSalePrice=400000)], [])
    leads = sold.find_recent_sales({"zipCode": "78244"})["leads"]
    assert leads[0]["rentcast_id"] == "b"


# --- persistence -------------------------------------------------------------

def test_save_dedupes_on_rentcast_id(monkeypatch):
    conn = _use_fake_db(monkeypatch)
    leads = [sold.to_candidate(_prop())]
    assert sold.save_candidates(conn, leads)["added"] == 1
    assert sold.save_candidates(conn, leads)["skipped"] == 1
    assert len(conn.rows) == 1


# --- contact enrichment ------------------------------------------------------

def test_enrich_uses_the_existing_skip_sherpa_wiring(monkeypatch):
    """Email comes from Skip Sherpa, never from RentCast."""
    import skip_sherpa_service

    calls = []

    def _fake_trace(street, city, state, zipcode):
        calls.append((street, city, state, zipcode))
        return {"ok": True, "owner": "Rolando Villarreal", "email": "r@example.com",
                "emails": ["r@example.com"], "preferred_phone": "+18005551234",
                "raw_phone": "+18005551234"}

    monkeypatch.setattr(skip_sherpa_service, "trace", _fake_trace)
    out = sold.enrich_contact(sold.to_candidate(_prop()))
    assert out["email"] == "r@example.com"
    assert out["phone"] == "+18005551234"
    assert calls[0][3] == "78244"  # full address incl. ZIP, per Skip Sherpa's rule


def test_enrich_refuses_without_zip(monkeypatch):
    """No ZIP means a skip trace can match the wrong door -- refuse it."""
    out = sold.enrich_contact({"address": "5500 Grand Lake Dr", "city": "San Antonio",
                               "state": "TX", "postal_code": ""})
    assert out["ok"] is False