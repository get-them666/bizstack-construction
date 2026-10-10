"""Keyless map services, so the Maps bill can go to zero.

    Geocoding   US Census geocoder -> ArcGIS World Geocoding -> Nominatim
    Routing     OSRM
    Links       Apple Maps URL scheme (no key, no account, ever)
    Embeds      OpenStreetMap export/embed (the only free iframe option)

Why this module exists
----------------------
The Google Maps key was present in the environment but the Cloud project had no
billing enabled, so every call returned REQUEST_DENIED. The Copilot reaches for
geocoding on any street address, so this was not a cosmetic failure: it burned a
round trip per address and pushed a five-step lead pipeline to 125 seconds,
where the proxy killed the request.

Why not Apple for everything
----------------------------
Apple has no server-side geocoding API. `CLGeocoder` is iOS/macOS only, so there
is nothing to call from a Linux container. Apple is therefore used for exactly
what it is good at -- opening a map in a native app -- and everything that runs
server-side comes from a keyless provider.

Deliberately absent: a place/business search. Overpass was tried and the public
demo instances either returned nothing for a populated area or timed out
entirely. That is not reliable enough to put behind a tool the model trusts, so
`maps_find_place` was dropped rather than shipped flaky. The model is told to
use web_search for suppliers, which suits those leads anyway -- they are
business contacts.

Caching: geocodes go through zip_lookup's 30-day in-process cache, keyed on the
normalised query, so a repeat render is a dict read.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 12
USER_AGENT = "bizstack-construction/1.0 (construction lead lookup)"

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OSRM = "https://router.project-osrm.org/route/v1/driving"
OSM_EMBED = "https://www.openstreetmap.org/export/embed.html"

_CACHE = {}
_CACHE_TTL = 30 * 24 * 3600  # 30 days, same reasoning as zip_lookup


def _get_json(url, params):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _cache_get(key):
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]
    return None


def _cache_put(key, value):
    _CACHE[key] = (time.time(), value)


# ── geocoding ───────────────────────────────────────────

def _nominatim(query):
    body = _get_json(NOMINATIM, {"q": query, "format": "json", "limit": 1})
    hits = body if isinstance(body, list) else []
    if not hits:
        return None
    hit = hits[0]
    try:
        lat, lng = float(hit["lat"]), float(hit["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    return {"lat": lat, "lng": lng, "label": hit.get("display_name", ""),
            "source": "nominatim"}


def geocode(address: str) -> dict:
    """Latitude/longitude for a free-text street address.

    Always has an "ok" key. Never guesses: no match returns ok=False rather than
    a city centroid, because a wrong point here silently misplaces a job on a
    map and makes a travel-time estimate meaningless.
    """
    raw = str(address or "").strip()
    if not raw:
        return {"ok": False, "error": "An address is required."}

    key = " ".join(raw.lower().split())
    cached = _cache_get(key)
    if cached is not None:
        return dict(cached, cached=True)

    # Census and ArcGIS are the same providers zip_lookup already runs, and
    # already has a 30-day cache. Reach through its public entry point so a
    # single address is not geocoded twice under two caches.
    try:
        import skiptrace_service as sts
        import zip_lookup

        parsed = sts.parse_address(raw)
        if parsed.get("ok"):
            try:
                hit = zip_lookup.lookup_zip(parsed["street"], parsed["city"], parsed["state"])
            except zip_lookup.ZipLookupError:
                hit = {}
            if hit.get("ok") and hit.get("lat") is not None:
                result = {"ok": True, "lat": float(hit["lat"]), "lng": float(hit["lng"]),
                          "label": hit.get("matched_address") or raw,
                          "zipcode": hit.get("zipcode", ""), "source": hit.get("source", "")}
                _cache_put(key, result)
                return dict(result, cached=False)
    except Exception:
        pass  # fall through to Nominatim on any shape of failure

    try:
        hit = _nominatim(raw)
    except Exception as exc:
        return {"ok": False, "error": f"Geocoding failed: {type(exc).__name__}: {exc}"}

    if not hit:
        return {"ok": False, "error": f"No map match for {raw!r}."}

    result = {"ok": True, "lat": hit["lat"], "lng": hit["lng"],
              "label": hit.get("label") or raw, "source": "nominatim"}
    _cache_put(key, result)
    return dict(result, cached=False)


# ── routing ─────────────────────────────────────────────

def route(origin: dict, destination: dict) -> dict:
    """Driving distance and minutes between two geocoded points."""
    try:
        o, d = (origin["lat"], origin["lng"]), (destination["lat"], destination["lng"])
    except (KeyError, TypeError):
        return {"ok": False, "error": "Both origin and destination must be geocoded first."}

    coords = f"{o[1]},{o[0]};{d[1]},{d[0]}"
    try:
        body = _get_json(f"{OSRM}/{coords}", {"overview": "false"})
    except Exception as exc:
        return {"ok": False, "error": f"Routing failed: {type(exc).__name__}: {exc}"}

    routes = body.get("routes") or []
    if not routes:
        return {"ok": False, "error": "No driving route found between those points."}
    leg = routes[0]
    return {"ok": True,
            "miles": round(float(leg.get("distance", 0)) / 1609.344, 1),
            "minutes": round(float(leg.get("duration", 0)) / 60),
            "source": "osrm"}


# ── URLs ────────────────────────────────────────────────

def apple_link_url(lat: float, lng: float, label: str = "") -> str:
    """Open a location in Apple Maps. No key, no account, works from any browser."""
    params = {"ll": f"{lat},{lng}", "z": "16"}
    if label:
        params["q"] = label
    return "https://maps.apple.com/?" + urllib.parse.urlencode(params)


def apple_directions_url(origin, destination) -> str:
    """Driving directions in Apple Maps. Points may be lat/lng pairs or strings."""
    def fmt(point):
        if isinstance(point, dict):
            return f"{point['lat']},{point['lng']}"
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            return f"{point[0]},{point[1]}"
        return str(point or "")

    params = {"saddr": fmt(origin), "daddr": fmt(destination), "dirflg": "d"}
    return "https://maps.apple.com/?" + urllib.parse.urlencode(params)


def embed_url(lat: float, lng: float, span_deg: float = 0.004) -> str:
    """An iframeable map centred on a point.

    OpenStreetMap's export/embed, because Apple publishes no embeddable endpoint
    and Google gated its one behind billing.
    """
    half = max(float(span_deg), 1e-4) / 2
    params = {
        "bbox": f"{lng - half},{lat - half},{lng + half},{lat + half}",
        "layer": "mapnik",
        "marker": f"{lat},{lng}",
    }
    return f"{OSM_EMBED}?" + urllib.parse.urlencode(params)


def embed_url_for_address(address: str) -> str:
    """iframe map for an address, or an Apple Maps link if it will not geocode.

    Callers put this straight into an <iframe src>, so a failed geocode must
    still yield something that works rather than an empty frame. Apple Maps
    accepts a free-text ?q= search, which needs no coordinates and no key.
    """
    found = geocode(address)
    if not found.get("ok"):
        return "https://maps.apple.com/?" + urllib.parse.urlencode({"q": address})
    return embed_url(found["lat"], found["lng"])


def directions_url_for_address(origin: str, destination: str) -> str:
    """Apple Maps directions between two addresses, as plain text.

    Apple Maps accepts an address in saddr/daddr, so no geocoding round trip is
    needed for a link. Server-side geocoding is only required when an actual
    driving TIME is wanted, which is what route() is for.
    """
    return apple_directions_url(origin, destination)


# ── diagnostics ─────────────────────────────────────────

def status() -> dict:
    """Which providers are configured in. Cheap, no network."""
    return {
        "google_required": False,
        "geocoding": ["census", "arcgis", "nominatim"],
        "routing": "osrm",
        "links": "apple",
        "embeds": "openstreetmap",
        "place_search": None,
        "note": "All keyless, so there is nothing to disable and nothing to bill.",
    }