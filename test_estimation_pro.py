"""EstimationPro adapter: offline, patching the single network entry point.

The cases that matter are the ones a silent fallback would hide: a payload
without the ``data`` envelope, a missing multiplier, and a ZIP the index does
not cover. Each must raise rather than produce a plausible number.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

import estimation_pro as ep

COSTS_PAYLOAD = {
    "data": {
        "trade": "roofing",
        "location": "VA - Hampton Roads",
        "multiplier": 0.97,
        "itemCount": 2,
        "items": [
            {"id": "3tab-material", "description": "3-tab asphalt shingles, material per square",
             "unit": "square", "low": 70, "high": 200, "typical": 100,
             "volatility": "standard", "lastVerified": "2026-06-11"},
            {"id": "architectural-installed", "description": "Architectural shingles, installed per sq ft",
             "unit": "sq ft", "low": 3.88, "high": 6.79, "typical": 5.34,
             "volatility": "standard", "lastVerified": "2026-06-11"},
        ],
    },
    "meta": {},
}

INDEX_PAYLOAD = {
    "data": {
        "meta": {
            "version": 1,
            "generated": "2026-03-16T19:11:20.517Z",
            "methodology": "EPCI = 100 * regional multiplier.",
            "projectTypes": {
                "kitchen_remodel": {"itemId": "kitchen-midrange-project",
                                    "trade": "kitchen-remodel", "unit": "sq ft",
                                    "description": "Mid-range kitchen remodel, per sq ft"},
                "roof_replacement": {"itemId": "roof-complete-project",
                                     "trade": "roofing", "unit": "project",
                                     "description": "Complete roof replacement project"},
            },
        },
        "index": {
            "state:VA": {"name": "VA", "type": "state", "epci": 97, "multiplier": 0.97,
                         "laborMultiplier": 0.93,
                         "projects": {"roof_replacement": {"low": 8500, "typical": 12500, "high": 18000},
                                      "kitchen_remodel": {"low": 95, "typical": 165, "high": 260}}},
        },
    },
    "meta": {},
}


@pytest.fixture(autouse=True)
def _no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ep, "CACHE_DIR", tmp_path)


def _stub(monkeypatch, payloads):
    def fake(url, timeout=None):
        for needle, body in payloads.items():
            if needle in url:
                return json.dumps(body)
        raise ep.EstimationProUnavailable(f"unstubbed url {url}")
    monkeypatch.setattr(ep, "_http_get", fake)


def test_costs_unwraps_the_data_envelope(monkeypatch):
    _stub(monkeypatch, {"costs": COSTS_PAYLOAD})
    out = ep.fetch_costs("roofing", "23464")
    assert out["multiplier"] == 0.97
    assert out["location"] == "VA - Hampton Roads"
    assert out["item_count"] == 2
    assert len(out["items"]) == 2


def test_costs_converts_to_cents_exactly(monkeypatch):
    _stub(monkeypatch, {"costs": COSTS_PAYLOAD})
    items = ep.fetch_costs("roofing", "23464")["items"]
    by_id = {i["item_id"]: i for i in items}
    assert by_id["3tab-material"]["typical_cents"] == 10000
    assert by_id["3tab-material"]["low_cents"] == 7000
    assert by_id["architectural-installed"]["typical_cents"] == 534


def test_payload_without_data_envelope_raises(monkeypatch):
    """The bug that produced a silent multiplier of 1.0."""
    _stub(monkeypatch, {"costs": {"multiplier": 1.4, "items": []}})
    with pytest.raises(ep.EstimationProUnavailable) as e:
        ep.fetch_costs("roofing", "23464")
    assert "data" in str(e.value)


def test_top_level_multiplier_is_not_mistaken_for_the_real_one(monkeypatch):
    """A multiplier sitting at the top level must not be picked up."""
    _stub(monkeypatch, {"index": INDEX_PAYLOAD,
                        "costs": {"multiplier": 1.4,
                                  "data": dict(COSTS_PAYLOAD["data"], multiplier=None)}})
    with pytest.raises(ep.EstimationProUnavailable, match="regional multiplier"):
        ep.project_estimate("roofing", "23464")


def test_non_json_raises(monkeypatch):
    monkeypatch.setattr(ep, "_http_get", lambda url, timeout=None: "<html>502</html>")
    with pytest.raises(ep.EstimationProUnavailable, match="not JSON"):
        ep.fetch_costs("roofing", "23464")


def test_network_error_is_wrapped(monkeypatch):
    def boom(url, timeout=None):
        raise OSError("connection reset")
    monkeypatch.setattr(ep, "_http_get", boom)
    with pytest.raises(ep.EstimationProUnavailable):
        ep.fetch_costs("roofing", "23464")


def test_unknown_trade_is_rejected_before_any_request(monkeypatch):
    def explode(url, timeout=None):
        raise AssertionError("must not hit the network for an unknown trade")
    monkeypatch.setattr(ep, "_http_get", explode)
    with pytest.raises(ep.EstimationProUnavailable, match="unknown trade"):
        ep.fetch_costs("wizardry", "23464")


@pytest.mark.parametrize("bad", ["2346", "234644", "23a64", ""])
def test_bad_zip_is_rejected(monkeypatch, bad):
    monkeypatch.setattr(ep, "_http_get", lambda url, timeout=None: "{}")
    with pytest.raises(ep.EstimationProUnavailable, match="5 digits"):
        ep.fetch_costs("roofing", bad)


def test_items_without_typical_are_skipped_not_zeroed(monkeypatch):
    payload = {"data": {"trade": "roofing", "multiplier": 0.97, "items": [
        {"id": "priced", "description": "d", "unit": "each", "typical": 12.5},
        {"id": "blank", "description": "no price", "unit": "each"},
    ]}}
    _stub(monkeypatch, {"costs": payload})
    items = ep.fetch_costs("roofing", "23464")["items"]
    assert [i["item_id"] for i in items] == ["priced"]


def test_index_exposes_trades_and_regions(monkeypatch):
    _stub(monkeypatch, {"index": INDEX_PAYLOAD})
    idx = ep.fetch_index()
    assert idx["trades"] == ["kitchen-remodel", "roofing"]
    assert idx["regions"]["state:VA"]["laborMultiplier"] == 0.93
    assert "EPCI" in idx["methodology"]


def test_project_estimate_regionalises_and_multiplies_sqft(monkeypatch):
    _stub(monkeypatch, {"index": INDEX_PAYLOAD, "costs": COSTS_PAYLOAD})
    roof = ep.project_estimate("roofing", "23464")
    assert roof["epci"] == 97
    assert roof["typical_cents"] == int(round(12500 * 0.97 * 100))

    kitchen = ep.project_estimate("kitchen-remodel", "23464", sqft=2000)
    assert kitchen["typical_cents"] == int(round(165 * 0.97 * 2000 * 100))


def test_per_sqft_trade_without_sqft_raises(monkeypatch):
    _stub(monkeypatch, {"index": INDEX_PAYLOAD, "costs": COSTS_PAYLOAD})
    with pytest.raises(ep.EstimationProUnavailable, match="supply sqft"):
        ep.project_estimate("kitchen-remodel", "23464")


def test_unmatched_location_raises(monkeypatch):
    _stub(monkeypatch, {"index": INDEX_PAYLOAD, "costs":
                        {"data": dict(COSTS_PAYLOAD["data"], location="ZZ - Nowhere")}})
    with pytest.raises(ep.EstimationProUnavailable, match="matches none"):
        ep.project_estimate("roofing", "23464")


def test_missing_location_raises_rather_than_guessing_a_region(monkeypatch):
    _stub(monkeypatch, {"index": INDEX_PAYLOAD,
                        "costs": {"data": dict(COSTS_PAYLOAD["data"], location=None)}})
    with pytest.raises(ep.EstimationProUnavailable, match="no 'location'"):
        ep.project_estimate("roofing", "23464")


def test_supplier_rows_match_the_prices_table_shape(monkeypatch):
    _stub(monkeypatch, {"costs": COSTS_PAYLOAD})
    rows = ep.supplier_rows(ep.fetch_costs("roofing", "23464"), "roofing")
    assert rows[0]["item_number"] == "3tab-material"
    assert rows[0]["uom"] == "square"
    assert rows[0]["price_cents"] == 10000
    assert set(rows[0]) >= {"item_number", "description", "uom", "price_cents"}


def test_region_matching_defers_to_the_provider_location():
    regions = INDEX_PAYLOAD["data"]["index"]
    assert ep._region_for(regions, "VA")["name"] == "VA"
    assert ep._region_for(regions, "VA - Tidewater")["name"] == "VA"


def test_a_metro_named_in_the_location_beats_the_state():
    regions = {"state:VA": {"name": "VA", "type": "state"},
               "metro:hampton-roads": {"name": "Hampton Roads", "type": "metro"}}
    assert ep._region_for(regions, "VA - Hampton Roads")["type"] == "metro"


def test_unknown_location_raises():
    with pytest.raises(ep.EstimationProUnavailable, match="matches none"):
        ep._region_for({"state:VA": {"name": "VA", "type": "state"}}, "ZZ - Nowhere")


def test_stale_cache_is_served_but_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(ep, "CACHE_DIR", tmp_path)
    calls = []

    def fake(url, timeout=None):
        calls.append(url)
        return json.dumps(COSTS_PAYLOAD)

    monkeypatch.setattr(ep, "_http_get", fake)
    assert ep.fetch_costs("roofing", "23464")["stale"] is False
    assert ep.fetch_costs("roofing", "23464")["stale"] is False
    assert len(calls) == 1, "second call should have been served from cache"

    monkeypatch.setattr(ep, "CACHE_TTL", -1)
    monkeypatch.setattr(ep, "_http_get",
                        lambda url, timeout=None: (_ for _ in ()).throw(
                            ep.EstimationProUnavailable("down")))
    out = ep.fetch_costs("roofing", "23464")
    assert out["stale"] is True, "a cached price served while down must be flagged"
    assert out["multiplier"] == 0.97


def test_stale_cache_with_no_backup_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(ep, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(ep, "_http_get",
                        lambda url, timeout=None: (_ for _ in ()).throw(
                            ep.EstimationProUnavailable("down")))
    with pytest.raises(ep.EstimationProUnavailable):
        ep.fetch_costs("roofing", "23464")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))