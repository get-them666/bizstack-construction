#!/usr/bin/env python3
"""Tests for skiptrace_service.

The bug this module exists to prevent: RentCast answers 404 for "address not
in my database" and 400 for "cannot parse this address", and the original
local version turned both into a 502 that aborted the whole waterfall. For a
tier-1 provider that is most of Virginia, so the addresses most in need of a
fallback were exactly the ones that threw. These tests pin 400/404 as answers.

No network: fetch_rentcast is monkeypatched. The point is the waterfall's
control flow, not RentCast's uptime.

    python3 -m pytest test_skiptrace_service.py -v
"""

import json

import skiptrace_service as svc


class FakeCursor:
    """Minimal dict-row cursor, mirroring get_db()'s row_factory=dict_row."""

    def __init__(self, found_row=None):
        self.rows = [found_row] if found_row else []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def sql_for(self, fragment):
        return [sql for sql, _ in self.executed if fragment in sql]


# --- parse_address ------------------------------------------------------------

def test_parse_address_full():
    p = svc.parse_address("1501 VANCE CIR, Chesapeake, VA 23320")
    assert p["ok"] is True
    assert p["street"] == "1501 VANCE CIR"
    assert p["state"] == "VA"
    assert p["zipcode"] == "23320"


def test_parse_address_glued_state_zip():
    """'Chesapeake VA 23320' as one chunk -- common in permit feeds."""
    p = svc.parse_address("1501 VANCE CIR, Chesapeake VA 23320")
    assert p["ok"] is True
    assert p["city"] == "CHESAPEAKE"
    assert p["state"] == "VA"
    assert p["zipcode"] == "23320"


def test_parse_address_bare_state_chunk_is_not_the_city():
    """'1 VANCE CIR, VA 23320' must not resolve to city='VA'.

    Sending 'VA, VA 23320' returns a miss that looks identical to the property
    not being in the database, which is how a whole address quietly disappears.
    """
    p = svc.parse_address("1501 VANCE CIR, VA 23320")
    assert p["ok"] is True
    assert p["city"] == ""
    assert p["state"] == "VA"


def test_parse_address_no_commas_at_all():
    p = svc.parse_address("1 A ST NORFOLK VA 23510")
    assert p["ok"] is True
    assert p["state"] == "VA"
    assert p["zipcode"] == "23510"
    assert "NORFOLK" in p["street"]


def test_parse_address_zip_plus_four():
    assert svc.parse_address("1 A St, Norfolk, VA 23510-1234")["zipcode"] == "23510-1234"


def test_parse_address_missing_zip_is_reported_not_raised():
    p = svc.parse_address("1501 VANCE CIR, Chesapeake, VA")
    assert p["ok"] is False
    assert p["why"] == "no ZIP"


def test_parse_address_missing_state_is_reported():
    p = svc.parse_address("1501 VANCE CIR, Chesapeake, 23320")
    assert p["ok"] is False
    assert p["why"] == "no state"


def test_parse_address_empty():
    assert svc.parse_address("")["ok"] is False
    assert svc.parse_address(None)["ok"] is False


def test_parse_address_normalizes_newlines():
    p = svc.parse_address("1501 VANCE CIR\nChesapeake, VA 23320")
    assert p["ok"] is True


# --- normalize_address: the cache key ---------------------------------------

def test_normalize_collapses_case_punctuation_and_spacing():
    a = svc.normalize_address("151  Battle Green Dr.", "Virginia Beach", "va", "23451")
    b = svc.normalize_address("151 battle green dr", "virginia beach", "VA", "23451")
    assert a == b, "same house must produce one cache key, or quota is spent twice"


def test_normalize_distinguishes_different_houses():
    a = svc.normalize_address("151 Battle Green Dr", "Virginia Beach", "VA", "23451")
    b = svc.normalize_address("152 Battle Green Dr", "Virginia Beach", "VA", "23451")
    assert a != b


def test_normalize_without_zip_is_still_a_key():
    assert svc.normalize_address("701 Dana Dr", "Chesapeake", "VA", "")


# --- looks_like_entity --------------------------------------------------------

def test_entity_detection():
    assert svc.looks_like_entity("Bayside Holdings LLC")
    assert svc.looks_like_entity("ACME INC")
    assert not svc.looks_like_entity("Jane Smith")
    # None must not raise: .upper() on None is a TypeError.
    assert not svc.looks_like_entity(None)
    assert not svc.looks_like_entity("")


# --- the 400/404 regression --------------------------------------------------

def test_404_is_not_found_not_an_exception(monkeypatch):
    monkeypatch.setattr(svc, "_get", lambda *a, **k: (404, None))
    monkeypatch.setenv("RENTCAST_API_KEY", "test")
    assert svc.fetch_rentcast("1 Nowhere Rd") is svc.NOT_FOUND


def test_400_is_not_found_not_an_exception(monkeypatch):
    monkeypatch.setattr(svc, "_get", lambda *a, **k: (400, None))
    monkeypatch.setenv("RENTCAST_API_KEY", "test")
    assert svc.fetch_rentcast("garbage input") is svc.NOT_FOUND


def test_401_surfaces_as_provider_error(monkeypatch):
    monkeypatch.setattr(svc, "_get", lambda *a, **k: (401, None))
    monkeypatch.setenv("RENTCAST_API_KEY", "test")
    try:
        svc.fetch_rentcast("1 Real St")
        assert False, "a bad key must not look like a miss"
    except svc.ProviderError as exc:
        assert "RENTCAST_API_KEY" in exc.message


def test_429_surfaces_and_marks_itself_spendable(monkeypatch):
    monkeypatch.setattr(svc, "_get", lambda *a, **k: (429, None))
    monkeypatch.setenv("RENTCAST_API_KEY", "test")
    try:
        svc.fetch_rentcast("1 Real St")
        assert False, "quota exhaustion must not look like a miss"
    except svc.ProviderError as exc:
        assert exc.status == 429
        assert exc.spendable is True


def test_5xx_surfaces_as_provider_error(monkeypatch):
    monkeypatch.setattr(svc, "_get", lambda *a, **k: (503, None))
    monkeypatch.setenv("RENTCAST_API_KEY", "test")
    try:
        svc.fetch_rentcast("1 Real St")
        assert False
    except svc.ProviderError as exc:
        assert exc.status == 502


def test_network_failure_does_not_claim_to_have_spent_quota(monkeypatch):
    def boom(*a, **k):
        raise svc.ProviderError("network", 502, spendable=False)
    monkeypatch.setattr(svc, "_get", boom)
    monkeypatch.setenv("RENTCAST_API_KEY", "test")
    try:
        svc.fetch_rentcast("1 Real St")
        assert False
    except svc.ProviderError as exc:
        # Caching a timeout as a definitive miss would poison the cache: the
        # address would never be retried, and it may well have had an owner.
        assert exc.spendable is False


def test_empty_result_list_is_not_found(monkeypatch):
    monkeypatch.setattr(svc, "_get", lambda *a, **k: (200, {"properties": []}))
    monkeypatch.setenv("RENTCAST_API_KEY", "test")
    assert svc.fetch_rentcast("1 Real St") is svc.NOT_FOUND


def test_record_without_owner_name_is_not_found(monkeypatch):
    """A property with no owner is a miss, not a record with owner_name=None."""
    monkeypatch.setattr(svc, "_get", lambda *a, **k: (200, {"properties": [{"owner": {"names": []}}]}))
    monkeypatch.setenv("RENTCAST_API_KEY", "test")
    assert svc.fetch_rentcast("1 Real St") is svc.NOT_FOUND


def test_missing_key_skips_rather_than_errors(monkeypatch):
    monkeypatch.delenv("RENTCAST_API_KEY", raising=False)
    assert svc.fetch_rentcast("1 Real St") is None


# --- trace_address ------------------------------------------------------------

def _good_body():
    return {"properties": [{
        "owner": {"names": ["Craig K Searles"], "type": "Individual",
                  "mailingAddress": {"city": "Virginia Beach"}},
        "ownerOccupied": True, "assessorID": "24055117630000",
        "county": "Virginia Beach City",
        "taxAssessments": {"2024": {"value": 722400}},
        "yearBuilt": 2009, "squareFootage": 2591,
    }]}


def test_trace_success_marks_individual_owner_as_residential(monkeypatch):
    monkeypatch.setenv("RENTCAST_API_KEY", "test")
    monkeypatch.setattr(svc, "_get", lambda *a, **k: (200, _good_body()))
    out = svc.trace_address("151 Battle Green Dr", "Virginia Beach", "VA", "23451")
    assert out["found"] is True
    assert out["owner_of_record"] == "Craig K Searles"
    # The business-contacts-only exception, made visible in the payload.
    assert out["is_residential"] is True
    assert out["owner_is_entity"] is False
    assert out["registered_agent"] is None
    assert out["property"]["assessed_value"] == 722400


def test_trace_miss_returns_clean_result_not_exception(monkeypatch):
    monkeypatch.setenv("RENTCAST_API_KEY", "test")
    monkeypatch.setattr(svc, "_get", lambda *a, **k: (404, None))
    out = svc.trace_address("701 Dana Dr", "Chesapeake", "VA", "23321")
    assert out["found"] is False
    assert any("no record" in line for line in out["layers_tried"])


def test_trace_rejects_bad_state_without_calling_provider(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("provider must not be called on invalid input")
    monkeypatch.setattr(svc, "_get", fail)
    try:
        svc.trace_address("151 Battle Green Dr", "Virginia Beach", "Virginia", "23451")
        assert False
    except svc.ProviderError as exc:
        assert exc.status == 400


def test_trace_rejects_missing_city():
    try:
        svc.trace_address("151 Battle Green Dr", "", "VA", "23451")
        assert False
    except svc.ProviderError as exc:
        assert exc.status == 400


# --- caching ------------------------------------------------------------------

def test_cache_put_and_get_roundtrip():
    cur = FakeCursor()
    result = {"found": True, "owner_of_record": "Jane Smith",
              "property": {"_source": "rentcast"}}
    svc.cache_put(cur, "key1", result)
    assert cur.sql_for("skiptrace_cache"), "cache_put must write to the cache table"
    assert cur.sql_for("ON CONFLICT"), "must upsert, not fail on a repeat address"

    read = FakeCursor(found_row={"found": True,
                                 "payload": json.dumps({"owner_of_record": "Jane Smith"}),
                                 "source": "rentcast"})
    got = svc.cache_get(read, "key1")
    assert got["owner_of_record"] == "Jane Smith"
    assert got["_cached"] is True


def test_cache_get_returns_none_on_miss():
    assert svc.cache_get(FakeCursor(), "nope") is None


def test_cache_get_handles_dict_rows():
    """get_db() uses row_factory=dict_row; positional indexing raises TypeError."""
    read = FakeCursor(found_row={"found": False, "payload": "{}", "source": None})
    assert svc.cache_get(read, "k")["found"] is False


def test_cache_get_survives_corrupt_payload():
    read = FakeCursor(found_row={"found": True, "payload": "{not json", "source": "rentcast"})
    assert svc.cache_get(read, "k") is None


def test_cache_caches_misses_too():
    """A known-dead address must never be re-spent against 50 calls/month."""
    cur = FakeCursor()
    svc.cache_put(cur, "dead", {"found": False, "layers_tried": ["rentcast: no record"]})
    params = [p for sql, p in cur.executed if p][0]
    assert params[1] is False, "a miss must be stored with found=False"


def test_ensure_schema_creates_both_tables():
    cur = FakeCursor()
    svc._SCHEMA_READY = False  # the once-per-process guard would skip the DDL
    svc.ensure_schema(cur)
    assert cur.sql_for("CREATE TABLE IF NOT EXISTS skiptrace_cache")
    assert cur.sql_for("CREATE TABLE IF NOT EXISTS skiptrace_audit")


def test_ensure_schema_is_once_per_process():
    """DDL must not run on every cached lookup."""
    svc._SCHEMA_READY = False
    first = FakeCursor()
    svc.ensure_schema(first)
    assert first.executed, "first call must create the tables"

    second = FakeCursor()
    svc.ensure_schema(second)
    assert not second.executed, "second call must be a no-op, not more DDL"


def test_audit_records_requester_and_residential_flag():
    cur = FakeCursor()
    svc.audit(cur, "shaun@example.com", "key1",
              {"found": True, "owner_of_record": "Jane Smith", "is_residential": True,
               "property": {"_source": "rentcast"}}, cached=False)
    params = [p for sql, p in cur.executed if p][0]
    assert params[0] == "shaun@example.com", "every lookup must name who asked"
    assert params[3] == "Jane Smith"
    assert params[4] is True


def test_audit_tolerates_miss_without_owner():
    cur = FakeCursor()
    svc.audit(cur, "shaun@example.com", "key1", {"found": False}, cached=True)
    params = [p for sql, p in cur.executed if p][0]
    assert params[3] == "", "a miss has no owner; it must not raise"


# --- no hardcoded secrets -----------------------------------------------------

def test_no_api_key_literal_in_source():
    """Keys must come from the environment, never from source."""
    with open("skiptrace_service.py") as fh:
        body = fh.read()
    for marker in ("os.getenv", "RENTCAST_API_KEY"):
        assert marker in body
    # A 32-hex-char literal is the shape of the key that was hardcoded in the
    # local tool. None should appear here.
    import re
    assert not re.search(r'"[0-9a-f]{32}"', body), "API key literal found in source"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
