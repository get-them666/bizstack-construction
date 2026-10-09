"""Tests for the PDL contact layer.

enrichment._enrich_address is monkeypatched throughout, so no PDL request is ever
made and no credit is spent. The assertions concentrate on the two things that
would cause real harm: contacting the wrong person, and re-billing a dead
address.
"""

import pytest

import pdl_contact_service as pdl
import skiptrace_service as sts


class FakeCursor:
    def __init__(self):
        self.sql = None
        self.params = None
        self.row = None
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())
        self.params = params

    def fetchone(self):
        return self.row


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    pdl.SCHEMA_READY = False
    monkeypatch.setenv("PDL_API_KEY", "test-key")
    yield
    pdl.SCHEMA_READY = False


def _stub_pdl(monkeypatch, response, calls=None):
    import enrichment

    def _fake(address):
        if calls is not None:
            calls.append(address)
        return response

    monkeypatch.setattr(enrichment, "_enrich_address", _fake)


# --- the ZIP rule ------------------------------------------------------------

def test_refuses_without_zip(monkeypatch):
    """A city-level match can return a different person's details."""
    calls = []
    _stub_pdl(monkeypatch, {"email": "x@example.com"}, calls)
    out = pdl.lookup("5500 Grand Lake Dr", "San Antonio", "TX", "")
    assert out["found"] is False
    assert calls == [], "a PDL call fired without a ZIP"
    assert out["credit_spent"] is False


def test_full_address_calls_pdl(monkeypatch):
    calls = []
    _stub_pdl(monkeypatch, {"email": "r@example.com", "phone": "+18005551234",
                            "name": "Rolando Villarreal"}, calls)
    out = pdl.lookup("5500 Grand Lake Dr", "San Antonio", "TX", "78244")
    assert out["found"] is True
    assert out["email"] == "r@example.com"
    assert calls == ["5500 Grand Lake Dr, San Antonio, TX 78244"]


# --- no key ------------------------------------------------------------------

def test_no_key_makes_no_call(monkeypatch):
    monkeypatch.delenv("PDL_API_KEY", raising=False)
    calls = []
    _stub_pdl(monkeypatch, {"email": "x@example.com"}, calls)
    out = pdl.lookup("5500 Grand Lake Dr", "San Antonio", "TX", "78244")
    assert out["found"] is False and calls == []


# --- hits and misses ---------------------------------------------------------

def test_miss_is_reported_not_raised(monkeypatch):
    """A miss is a real answer, not an exception."""
    _stub_pdl(monkeypatch, {})
    out = pdl.lookup("5500 Grand Lake Dr", "San Antonio", "TX", "78244")
    assert out["found"] is False
    assert "No contact" in out["message"]


def test_hit_carries_the_dnc_unknown_flag(monkeypatch):
    """PDL has no DNC signal, and the caller must be able to see that."""
    _stub_pdl(monkeypatch, {"email": "r@example.com", "phone": "+1800"})
    out = pdl.lookup("5500 Grand Lake Dr", "San Antonio", "TX", "78244")
    assert out["dnc_unknown"] is True


def test_provider_exception_is_contained(monkeypatch):
    """A PDL fault must not 500 the page."""
    import enrichment

    def _boom(address):
        raise RuntimeError("PDL is down")

    monkeypatch.setattr(enrichment, "_enrich_address", _boom)
    out = pdl.lookup("5500 Grand Lake Dr", "San Antonio", "TX", "78244")
    assert out["found"] is False
    assert "PDL lookup failed" in out["message"]


# --- caching -----------------------------------------------------------------

def test_repeat_address_is_free(monkeypatch):
    """The whole point: re-running the queue must not re-bill."""
    calls = []
    _stub_pdl(monkeypatch, {"email": "r@example.com"}, calls)
    cur = FakeCursor()

    def _cache_get(c, key):
        return {"found": True, "email": "r@example.com", "source": "pdl", "_cached": True}

    monkeypatch.setattr(pdl, "cache_get", _cache_get)
    out = pdl.lookup("5500 Grand Lake Dr", "San Antonio", "TX", "78244", cur=cur)
    assert out["found"] is True and calls == []


def test_miss_is_cached_too(monkeypatch):
    """Otherwise a dead address is re-billed on every retry."""
    calls = []
    _stub_pdl(monkeypatch, {}, calls)
    cur = FakeCursor()

    stored = {}

    def _put(c, key, result):
        stored[key] = result

    def _get(c, key):
        return stored.get(key)

    monkeypatch.setattr(pdl, "cache_put", _put)
    monkeypatch.setattr(pdl, "cache_get", _get)
    pdl.lookup("1 Nowhere Rd", "Nowhere", "VA", "23320", cur=cur)
    pdl.lookup("1 Nowhere Rd", "Nowhere", "VA", "23320", cur=cur)
    assert len(calls) == 1, "the miss was re-billed"


def test_cache_survives_a_db_failure(monkeypatch):
    """A cache outage must not cost a lookup."""
    calls = []
    _stub_pdl(monkeypatch, {"email": "r@example.com"}, calls)

    def _boom(cur):
        raise RuntimeError("table missing")

    monkeypatch.setattr(pdl, "ensure_schema", _boom)
    out = pdl.lookup("5500 Grand Lake Dr", "San Antonio", "TX", "78244", cur=FakeCursor())
    assert out["found"] is True
    assert len(calls) == 1


# --- address normalisation ---------------------------------------------------

def test_address_variations_share_one_cache_key(monkeypatch):
    """'151 battle green dr' and '151 Battle Green Dr' are one house."""
    a = sts.normalize_address("151 battle green dr.", "norfolk", "va", "23510")
    b = sts.normalize_address("151 Battle Green Dr", "Norfolk", "VA", "23510")
    assert a == b


def test_different_houses_are_different_keys(monkeypatch):
    a = sts.normalize_address("151 Battle Green Dr", "Norfolk", "VA", "23510")
    b = sts.normalize_address("152 Battle Green Dr", "Norfolk", "VA", "23510")
    assert a != b


# --- audit -------------------------------------------------------------------

def test_lookup_writes_an_audit_row(monkeypatch):
    """Contact details are sensitive; the trail is what makes this defensible."""
    _stub_pdl(monkeypatch, {"email": "r@example.com", "name": "R V"})
    cur = FakeCursor()
    audited = {}
    monkeypatch.setattr(pdl, "audit",
                        lambda c, by, key, res, cached: audited.update(
                            {"by": by, "found": res.get("found")}))
    pdl.lookup("5500 Grand Lake Dr", "San Antonio", "TX", "78244",
               cur=cur, requested_by="shaun@bizstackperks.com")
    assert audited["by"] == "shaun@bizstackperks.com"
    assert audited["found"] is True