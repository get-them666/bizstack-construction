"""Google Maps Platform integration for BizStack.
Geocoding, Places Autocomplete, Distance Matrix.

Transport note: a key restricted to HTTP referrers CANNOT be called from a
backend -- Google answers `REQUEST_DENIED / API keys with referer restrictions
cannot be used with this API`. Every call here therefore goes through _api_get,
which surfaces the real status instead of quietly returning None. That silent
None is why this module sat unused for months looking merely dormant.

Set GOOGLE_MAPS_SERVER_KEY to an unrestricted or IP-restricted key for
server-side use; GOOGLE_MAPS_API_KEY remains the browser key.
"""

import os
import json
import urllib.parse
import urllib.request
from typing import Optional, List, Dict, Any


# Prefer the server-side key. Resolved per call rather than at import so a
# Railway variable change does not require a redeploy to take effect.
MAPS_API_KEY = (
    os.getenv("GOOGLE_MAPS_SERVER_KEY")
    or os.getenv("GOOGLE_MAPS_API_KEY")
    or ""
)
GEOCODING_BASE = "https://maps.googleapis.com/maps/api/geocode/json"
PLACES_BASE = "https://maps.googleapis.com/maps/api/place/autocomplete/json"
PLACES_DETAILS_BASE = "https://maps.googleapis.com/maps/api/place/details/json"
DISTANCE_BASE = "https://maps.googleapis.com/maps/api/distancematrix/json"

# Distance Matrix hard-caps destinations per request.
DISTANCE_MAX_DESTINATIONS = 25

_LAST_ERROR = {"endpoint": "", "status": "", "message": ""}


def maps_status() -> dict:
    """Why Maps is or isn't usable. Cheap: no network call.

    The operator hit a real outage where every function returned None and the
    cause was invisible. Anything integrating this should check this first and
    surface the message rather than pretending the data does not exist.
    """
    key = (os.getenv("GOOGLE_MAPS_SERVER_KEY") or os.getenv("GOOGLE_MAPS_API_KEY") or "").strip()
    if not key:
        return {"ok": False, "reason": "no key set",
                "hint": "set GOOGLE_MAPS_SERVER_KEY to an unrestricted or IP-restricted key"}
    return {"ok": True, "reason": "key present",
            "using": "GOOGLE_MAPS_SERVER_KEY" if os.getenv("GOOGLE_MAPS_SERVER_KEY") else "GOOGLE_MAPS_API_KEY",
            "last_error": _LAST_ERROR or None}


def _api_get(url: str, params: dict) -> Dict[str, Any]:
    """GET a Maps JSON endpoint, recording the real failure reason.

    Raises RuntimeError on transport/permission problems rather than returning
    None, so callers can distinguish "no data" from "you are not configured".
    """
    params = {k: v for k, v in params.items() if v is not None}
    full = url + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(full, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:
        _LAST_ERROR.update({"endpoint": url, "status": "EXC", "message": str(exc)})
        raise RuntimeError(f"Google Maps call failed ({url}): {exc}") from exc

    status = data.get("status")
    if status not in ("OK", "ZERO_RESULTS"):
        _LAST_ERROR.update({"endpoint": url, "status": str(status),
                            "message": str(data.get("error_message", ""))[:300]})
        raise RuntimeError(
            f"Google Maps {url.rsplit('/', 1)[-1]} returned {status}: "
            f"{data.get('error_message', '')[:200]}"
        )
    return data



# ──────────────────────────────────────────────
# Geocoding
# ──────────────────────────────────────────────

def geocode_address(address: str) -> Optional[Dict[str, Any]]:
    """Convert address to lat/lng.

    Raises RuntimeError when Maps is unreachable or the key is refused, so a
    misconfigured key is never mistaken for "that address could not be found".
    Returns None only for a genuine ZERO_RESULTS (address not found).
    """
    if not MAPS_API_KEY:
        raise RuntimeError("Google Maps not configured: set GOOGLE_MAPS_SERVER_KEY")
    data = _api_get(GEOCODING_BASE, {"address": address, "key": MAPS_API_KEY})
    if not data.get("results"):
        return None
    result = data["results"][0]
    loc = result["geometry"]["location"]
    return {
        "lat": loc["lat"],
        "lng": loc["lng"],
        "formatted_address": result["formatted_address"],
        "place_id": result.get("place_id"),
        "address_components": result.get("address_components", []),
    }


def reverse_geocode(lat: float, lng: float) -> Optional[str]:
    """Convert lat/lng to formatted address."""
    if not MAPS_API_KEY:
        raise RuntimeError("Google Maps not configured: set GOOGLE_MAPS_SERVER_KEY")
    data = _api_get(GEOCODING_BASE, {"latlng": f"{lat},{lng}", "key": MAPS_API_KEY})
    if not data.get("results"):
        return None
    return data["results"][0]["formatted_address"]


# ──────────────────────────────────────────────
# Places Autocomplete
# ──────────────────────────────────────────────

def places_autocomplete(input_text: str, session_token: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get place predictions for autocomplete."""
    if not MAPS_API_KEY:
        return []
    params = {
        "input": input_text,
        "key": MAPS_API_KEY,
        "types": "address",
        "components": "country:us",
    }
    if session_token:
        params["sessiontoken"] = session_token
    url = PLACES_BASE + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read().decode())
    if data["status"] != "OK":
        return []
    return [
        {
            "place_id": p["place_id"],
            "description": p["description"],
            "structured_formatting": p.get("structured_formatting", {}),
        }
        for p in data["predictions"]
    ]


def place_details(place_id: str, fields: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Get detailed place info including lat/lng and contact info.
    
    fields: Comma-separated list of fields to return. Default includes contact info.
    See: https://developers.google.com/maps/documentation/places/web-service/place-details
    """
    if not MAPS_API_KEY:
        return None
    if fields is None:
        fields = (
            "formatted_address,geometry,name,place_id,address_component,"
            "formatted_phone_number,international_phone_number,website,"
            "url,rating,user_ratings_total,opening_hours,business_status,"
            "type,price_level,editorial_summary"
        )
    params = {
        "place_id": place_id,
        "key": MAPS_API_KEY,
        "fields": fields,
    }
    url = PLACES_DETAILS_BASE + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read().decode())
    if data["status"] != "OK":
        return None
    result = data["result"]
    loc = result.get("geometry", {}).get("location", {})
    return {
        "place_id": result["place_id"],
        "name": result.get("name"),
        "formatted_address": result.get("formatted_address"),
        "lat": loc.get("lat"),
        "lng": loc.get("lng"),
        "address_components": result.get("address_components", []),
        "phone": result.get("formatted_phone_number"),
        "phone_international": result.get("international_phone_number"),
        "website": result.get("website"),
        "google_maps_url": result.get("url"),
        "rating": result.get("rating"),
        "reviews_count": result.get("user_ratings_total"),
        "opening_hours": result.get("opening_hours"),
        "business_status": result.get("business_status"),
        "types": result.get("types", []),
        "price_level": result.get("price_level"),
        "description": result.get("editorial_summary", {}).get("overview"),
    }


def find_place_by_name_and_address(name: str, address: str = "") -> Optional[Dict[str, Any]]:
    """Find a place by name and optional address, return full details with contact info.
    
    Uses Places Text Search (requires Places API enabled).
    Returns None gracefully if API not enabled or fails.
    """
    if not MAPS_API_KEY:
        return None
    query = name
    if address:
        query += f" {address}"
    params = {
        "query": query,
        "key": MAPS_API_KEY,
        "fields": "place_id",
    }
    url = "https://maps.googleapis.com/maps/api/place/textsearch/json?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    if data.get("status") != "OK" or not data.get("results"):
        return None
    place_id = data["results"][0]["place_id"]
    return place_details(place_id)


def enrich_lead_contact_info(lead: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich a lead dict with contact info from Google Places.
    
    Expects lead dict with: name, address, phone (optional).
    Returns enriched dict with: phone, website, google_maps_url, rating, etc.
    Gracefully returns original lead if API unavailable.
    """
    name = lead.get("name") or lead.get("company_name") or ""
    address = lead.get("address") or lead.get("property_address") or ""
    if not name:
        return lead
    
    # Try to find place by name + address
    place = find_place_by_name_and_address(name, address)
    if not place:
        return lead
    
    # Merge contact info (don't overwrite existing)
    enriched = lead.copy()
    if place.get("phone") and not enriched.get("phone"):
        enriched["phone"] = place["phone"]
    if place.get("phone_international") and not enriched.get("phone_international"):
        enriched["phone_international"] = place["phone_international"]
    if place.get("website") and not enriched.get("website"):
        enriched["website"] = place["website"]
    if place.get("google_maps_url") and not enriched.get("google_maps_url"):
        enriched["google_maps_url"] = place["google_maps_url"]
    if place.get("rating") and not enriched.get("rating"):
        enriched["rating"] = place["rating"]
    if place.get("reviews_count") and not enriched.get("reviews_count"):
        enriched["reviews_count"] = place["reviews_count"]
    if place.get("formatted_address") and not enriched.get("address"):
        enriched["address"] = place["formatted_address"]
    if place.get("types"):
        enriched["place_types"] = place["types"]
    if place.get("business_status"):
        enriched["business_status"] = place["business_status"]
    
    return enriched


# ──────────────────────────────────────────────
# Distance Matrix
# ──────────────────────────────────────────────

def distance_matrix(
    origins: List[str],
    destinations: List[str],
    mode: str = "driving",
    departure_time: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Get travel distance/time between origins and destinations.

    origins/destinations: list of "lat,lng" or addresses. Raises on failure so
    a refused key is never read as "no route found".
    """
    if not MAPS_API_KEY:
        raise RuntimeError("Google Maps not configured: set GOOGLE_MAPS_SERVER_KEY")
    if len(destinations) > DISTANCE_MAX_DESTINATIONS:
        raise ValueError(
            f"Distance Matrix takes at most {DISTANCE_MAX_DESTINATIONS} destinations, got "
            f"{len(destinations)}; use batch_travel_minutes()"
        )
    return _api_get(DISTANCE_BASE, {
        "origins": "|".join(origins),
        "destinations": "|".join(destinations),
        "mode": mode,
        "key": MAPS_API_KEY,
        "departure_time": departure_time,
    })


def batch_travel_minutes(
    origin: str,
    destinations: List[str],
    mode: str = "driving",
) -> Dict[str, int]:
    """Driving minutes from one origin to many destinations.

    Distance Matrix caps a request at 25 destinations, so this chunks. Permits
    arrive in the hundreds, and a single oversized request returns nothing at
    all rather than a partial result -- which would look like "no jobs nearby".
    Returns {destination: minutes}; unreachable entries are simply absent.
    """
    out: Dict[str, int] = {}
    if not destinations or not MAPS_API_KEY:
        return out
    for i in range(0, len(destinations), DISTANCE_MAX_DESTINATIONS):
        chunk = destinations[i:i + DISTANCE_MAX_DESTINATIONS]
        try:
            data = distance_matrix([origin], chunk, mode=mode)
        except Exception as exc:
            print(f"[maps] travel-time batch failed at offset {i}: {exc}", flush=True)
            continue
        if not data:
            continue
        elements = (data.get("rows") or [{}])[0].get("elements") or []
        for dest, el in zip(chunk, elements):
            if el.get("status") == "OK":
                try:
                    out[dest] = int(el["duration"]["value"] / 60)
                except (KeyError, TypeError, ValueError):
                    continue
    return out


def travel_time_minutes(origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float) -> Optional[int]:
    """Get driving time in minutes between two points."""
    result = distance_matrix(
        [f"{origin_lat},{origin_lng}"],
        [f"{dest_lat},{dest_lng}"],
        mode="driving",
    )
    if not result or not result["rows"]:
        return None
    element = result["rows"][0]["elements"][0]
    if element["status"] != "OK":
        return None
    return int(element["duration"]["value"] / 60)


# ──────────────────────────────────────────────
# Embed URLs (for iframes)
# ──────────────────────────────────────────────

def maps_embed_url(lat: float, lng: float, zoom: int = 16) -> str:
    """Generate Google Maps embed URL."""
    return f"https://www.google.com/maps/embed/v1/place?key={MAPS_API_KEY}&q={lat},{lng}&zoom={str(zoom)}"


def maps_directions_url(origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float) -> str:
    """Generate Google Maps directions URL."""
    return f"https://www.google.com/maps/dir/?api=1&origin={origin_lat},{origin_lng}&destination={dest_lat},{dest_lng}&travelmode=driving"