"""Verified parcel lookups: parse addresses, map fields, and fail closed.

No network: every provider response is stubbed. The point of these tests is not
that the live city services are up today; it is that a miss never becomes an
invented square footage, and that a supported city maps the fields the quote
card actually renders.
"""

import pytest

import regional_property_lookup as rpl


@pytest.fixture(autouse=True)
def clear_cache():
    rpl._cache.clear()
    yield
    rpl._cache.clear()


def test_parses_a_full_norfolk_address():
    parsed = rpl.parse_address("8494 Lynn River Road, Norfolk, VA 23503")
    assert parsed == {
        "number": "8494",
        "direction": "",
        "street": "LYNN RIVER",
        "suffix": "RD",
        "city": "NORFOLK",
        "locality": "norfolk",
        "zip": "23503",
    }


def test_parses_direction_and_two_word_city():
    parsed = rpl.parse_address("1056 E Bayview Blvd, Newport News, VA 23503")
    assert parsed["number"] == "1056"
    assert parsed["direction"] == "E"
    assert parsed["street"] == "BAYVIEW"
    assert parsed["suffix"] == "BLVD"
    assert parsed["locality"] == "newport_news"


def test_unsupported_city_has_no_locality():
    parsed = rpl.parse_address("123 Main St, Virginia Beach, VA 23451")
    assert parsed["locality"] == ""
    assert parsed["street"] == "MAIN"
    assert parsed["suffix"] == "ST"


def test_address_without_a_house_number_is_rejected():
    assert rpl.parse_address("Main Street, Norfolk, VA") is None
    assert rpl.parse_address("") is None


def test_unsupported_city_never_calls_the_network(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("unsupported locality must not make an HTTP call")

    monkeypatch.setattr(rpl, "_get_json", explode)
    assert rpl.lookup_property_regional("123 Main St, Virginia Beach, VA 23451") is None


def test_norfolk_maps_the_public_record(monkeypatch):
    calls = []

    def fake_get_json(url, params):
        calls.append((url, params))
        return [{
            "property_street_number": "8494",
            "property_street_direction": "",
            "property_street_name": "Lynn River",
            "property_street_type": "RD",
            "property_zip": "23503-3247",
            "residential_finished_living": "1787",
            "land_square_footage": "11137",
            "improvement_year_built": "1957",
            "property_use": "Single Family - Detached",
        }]

    monkeypatch.setattr(rpl, "_get_json", fake_get_json)
    result = rpl.lookup_property_regional("8494 Lynn River Road, Norfolk, VA 23503")

    assert result["address"] == "8494 Lynn River Rd"
    assert result["city"] == "Norfolk"
    assert result["state"] == "VA"
    assert result["zip"] == "23503"
    assert result["sqft"] == 1787
    assert result["lot_sqft"] == 11137
    assert result["year_built"] == 1957
    assert result["property_type"] == "Single Family - Detached"
    assert result["source"] == "norfolk_socrata"
    assert calls and calls[0][0].endswith("/qva7-tzrf.json")
    where = calls[0][1]["$where"]
    assert "property_street_number='8494'" in where
    assert "LYNN RIVER" in where
    assert "23503" in where


def test_norfolk_returns_none_when_no_record_has_finished_area(monkeypatch):
    monkeypatch.setattr(rpl, "_get_json", lambda url, params: [{
        "property_street_number": "8494",
        "property_street_name": "Lynn River",
        "property_street_type": "RD",
        "property_zip": "23503",
        "residential_finished_living": "0",
        "commercial_building_area": "0",
    }])
    assert rpl.lookup_property_regional("8494 Lynn River Road, Norfolk, VA 23503") is None


def test_portsmouth_maps_the_public_record(monkeypatch):
    monkeypatch.setattr(rpl, "_get_json", lambda url, params: {
        "features": [{"attributes": {
            "SITE_ADDRE": "4773 RIVER SHORE RD",
            "SITE_ZIP": "23703",
            "TOT_SQ_FT": 3493,
            "STORY": 1,
            "BEDROOMS": 0,
            "BATH": 4,
            "BLDG_DATE": 1980,
            "TYPE_PROP": "RESIDENTIAL",
        }}],
    })
    result = rpl.lookup_property_regional("4773 River Shore Rd, Portsmouth, VA 23703")
    assert result["sqft"] == 3493
    assert result["beds"] is None  # 0 means "not recorded", not a studio
    assert result["baths"] == 4
    assert result["year_built"] == 1980
    assert result["stories"] == 1
    assert result["source"] == "portsmouth_arcgis"


def test_newport_news_maps_the_public_record(monkeypatch):
    monkeypatch.setattr(rpl, "_get_json", lambda url, params: {
        "features": [{"attributes": {
            "SITEADDRESS": "928 IVY AVE",
            "RESFLRAREA": 1056,
            "RESYRBLT": 2001,
            "FLOORCOUNT": 1,
            "LIVUNIT": 1,
            "USEDSCRP": "SINGLE FAMILY DETACHED",
        }}],
    })
    result = rpl.lookup_property_regional("928 Ivy Ave, Newport News, VA 23607")
    assert result["sqft"] == 1056
    assert result["year_built"] == 2001
    assert result["stories"] == 1
    assert result["property_type"] == "Single Family Detached"
    assert result["source"] == "newport_news_arcgis"


def test_newport_news_prefers_the_exact_street_over_a_longer_name(monkeypatch):
    monkeypatch.setattr(rpl, "_get_json", lambda url, params: {
        "features": [
            {"attributes": {"SITEADDRESS": "928 IVYSTONE WAY", "RESFLRAREA": 2500}},
            {"attributes": {"SITEADDRESS": "928 IVY AVE", "RESFLRAREA": 1056}},
        ],
    })
    result = rpl.lookup_property_regional("928 Ivy Ave, Newport News, VA 23607")
    assert result["address"] == "928 Ivy Ave"
    assert result["sqft"] == 1056


def test_a_provider_error_is_a_miss_not_a_500(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(rpl, "_get_json", boom)
    assert rpl.lookup_property_regional("8494 Lynn River Road, Norfolk, VA 23503") is None


def test_repeated_addresses_are_cached(monkeypatch):
    calls = []

    def fake_get_json(url, params):
        calls.append(1)
        return [{
            "property_street_number": "8494",
            "property_street_name": "Lynn River",
            "property_street_type": "RD",
            "property_zip": "23503",
            "residential_finished_living": "1787",
        }]

    monkeypatch.setattr(rpl, "_get_json", fake_get_json)
    first = rpl.lookup_property_regional("8494 Lynn River Road, Norfolk, VA 23503")
    second = rpl.lookup_property_regional("8494 Lynn River Road, Norfolk, VA 23503")
    assert first == second
    assert len(calls) == 1
