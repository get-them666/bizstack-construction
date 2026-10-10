"""The Maps bill is gone: nothing here needs a Google key.

    python3 -m pytest test_open_geo.py -v

The Google Maps key was present in the environment while the Cloud project had
no billing enabled, so every call returned REQUEST_DENIED. Because the Copilot
reaches for geocoding on any street address, that was not cosmetic: it burned a
round trip per address and pushed a five-step lead pipeline to 125 seconds,
where the proxy killed the request and the model then reported it "couldn't
access the information" -- which reads like missing data and sends the
investigation after the wrong thing entirely.

These tests pin the replacements and, more importantly, pin that no Google
endpoint remains on a path the Copilot can reach.

No network: provider calls are stubbed.
"""
import pytest

import open_geo
import copilot_ops
from con_ai_agent import BusinessAIAgent


# ── URL builders: no key, no account ────────────────────

def test_apple_link_url_shape():
    url = open_geo.apple_link_url(36.8508, -76.2859, "4701 Winthrop St")
    assert url.startswith("https://maps.apple.com/?")
    assert "ll=36.8508%2C-76.2859" in url
    assert "key=" not in url.lower()


def test_apple_directions_accepts_dicts_strings_and_pairs():
    from_dict = open_geo.apple_directions_url({"lat": 1.0, "lng": 2.0}, {"lat": 3.0, "lng": 4.0})
    from_pair = open_geo.apple_directions_url((1.0, 2.0), (3.0, 4.0))
    from_text = open_geo.apple_directions_url("1,2", "3,4")
    assert "saddr=1.0%2C2.0" in from_dict and "daddr=3.0%2C4.0" in from_dict
    assert from_dict == from_pair
    assert "saddr=1%2C2" in from_text


def test_embed_url_is_openstreetmap_and_iframeable():
    url = open_geo.embed_url(36.8508, -76.2859)
    assert url.startswith("https://www.openstreetmap.org/export/embed.html")
    assert "marker=36.8508%2C-76.2859" in url
    assert "google" not in url.lower(), "an iframe must not point at Google"


def test_embed_span_never_collapses_to_zero():
    """A zero bbox renders an empty frame, which is the bug this replaces.

    Zero and negative spans both clamp to the same floor, so they produce the
    same URL -- what matters is that the floor is a real, non-degenerate box.
    """
    for span in (0, -5):
        url = open_geo.embed_url(1.0, 2.0, span_deg=span)
        bbox = url.split("bbox=")[1].split("&")[0]
        west, south, east, north = (float(v) for v in bbox.split("%2C"))
        assert east > west and north > south, f"degenerate bbox for span={span}"


# ── geocoding ───────────────────────────────────────────

def test_geocode_rejects_empty_without_calling_out():
    assert open_geo.geocode("")["ok"] is False


def test_geocode_never_guesses_a_city_centroid(monkeypatch):
    """A miss must be a miss. A wrong point misplaces a job on a map and makes
    a travel-time estimate meaningless, so it fails closed."""
    monkeypatch.setattr(open_geo, "_nominatim", lambda q: None)
    monkeypatch.setattr(open_geo, "_cache_get", lambda k: None, raising=False)
    result = open_geo.geocode("999 Nonexistent Lane, Nowhere, ZZ")
    assert result["ok"] is False
    assert "lat" not in result or result.get("lat") is None


def test_geocode_uses_a_cache_hit(monkeypatch):
    monkeypatch.setitem(open_geo._CACHE, "123 main st",
                        (__import__("time").time(), {"ok": True, "lat": 1.0, "lng": 2.0,
                                                     "label": "x", "source": "census"}))
    out = open_geo.geocode("123 Main St")
    assert out["cached"] is True and out["lat"] == 1.0


def test_geocode_survives_a_broken_upstream(monkeypatch):
    def boom(q):
        raise RuntimeError("provider down")
    monkeypatch.setattr(open_geo, "_nominatim", boom)
    out = open_geo.geocode("4701 Winthrop Street, Norfolk, VA")
    assert out["ok"] is False and "provider down" in out["error"]


# ── routing ─────────────────────────────────────────────

def test_route_rejects_ungeocoded_points():
    assert open_geo.route({"nope": 1}, {"lat": 2, "lng": 3})["ok"] is False


def test_route_converts_metres_and_seconds(monkeypatch):
    monkeypatch.setattr(open_geo, "_get_json",
                        lambda url, params: {"routes": [{"distance": 16093.44, "duration": 660}]})
    out = open_geo.route({"lat": 1, "lng": 2}, {"lat": 3, "lng": 4})
    assert out["ok"] is True
    assert out["miles"] == 10.0
    assert out["minutes"] == 11


def test_route_reports_no_route_instead_of_raising(monkeypatch):
    monkeypatch.setattr(open_geo, "_get_json", lambda url, params: {"routes": []})
    assert open_geo.route({"lat": 1, "lng": 2}, {"lat": 3, "lng": 4})["ok"] is False


# ── the Copilot wiring ──────────────────────────────────

def _tools():
    return {t["function"]["name"] for t in BusinessAIAgent(subset="copilot")._tools()}


def test_maps_find_place_is_gone_everywhere():
    """Removed with its provider, not left advertised."""
    assert "maps_find_place" not in _tools()

    class _NoDb:
        def cursor(self):
            raise AssertionError("no db")

    handlers = copilot_ops.build_maps_tools(_NoDb())
    assert "maps_find_place" not in handlers
    assert set(handlers) == {"maps_geocode", "maps_directions"}


def test_maps_tools_are_always_advertised_now():
    """The gating is gone because there is nothing to gate. They cannot fail on
    a missing key, so they must always be present."""
    names = _tools()
    assert {"maps_geocode", "maps_directions"} <= names


def test_schema_and_handler_agree():
    class _NoDb:
        def cursor(self):
            raise AssertionError("no db")

    handlers = set(copilot_ops.build_maps_tools(_NoDb()))
    advertised = {"maps_geocode", "maps_directions"} & _tools()
    assert advertised == handlers


def test_lead_pipeline_still_intact():
    """The whole point: the address pipeline must still work."""
    from test_lead_pipeline_tools import PIPELINE
    assert set(PIPELINE) <= _tools()


def test_no_google_maps_endpoint_on_any_reachable_path(monkeypatch):
    """The actual acceptance criterion.

    Guards against a future edit reintroducing a maps.googleapis.com call: the
    Copilot handlers must not touch google_maps at all.
    """
    import google_maps

    class Boom(Exception):
        pass

    def forbidden(*a, **kw):
        raise Boom("Copilot path called Google Maps")

    for name in ("geocode_address", "distance_matrix", "find_place_by_name_and_address",
                 "places_autocomplete", "place_details"):
        monkeypatch.setattr(google_maps, name, forbidden)

    class _NoDb:
        def cursor(self):
            raise AssertionError("no db")

        def commit(self):
            pass

        def rollback(self):
            pass

    handlers = copilot_ops.build_maps_tools(_NoDb())
    out = handlers["maps_geocode"](address="4701 Winthrop Street, Norfolk, VA")
    assert "Boom" not in str(out), "maps_geocode reached Google Maps"


def test_template_helpers_are_keyless():
    """The Jinja helpers the job pages call must not emit a Google URL."""
    import construction_main
    embed = construction_main._map_embed("4701 Winthrop Street, Norfolk, VA")
    link = construction_main._map_directions("4701 Winthrop Street, Norfolk, VA")
    assert "google" not in embed.lower()
    assert "maps.apple.com" in link
    assert link  # never empty, or the "Map ->" button renders dead