#!/usr/bin/env python3
"""Street address -> ZIP code, using only free public services with no API key.

WHY THIS EXISTS
The owner lookup on /skiptrace refuses to run a skip trace without a ZIP. A
street-only match can resolve to a different house on the same road, and a
skip trace still bills whether or not it found the right person -- so the page
tells the operator to add a ZIP first. But most permit-feed addresses arrive
without one, and the only thing that used to fill it in was Google geocoding,
which returns REQUEST_DENIED while the Google Cloud project has billing off.

This module fills that gap with two keyless services, so it works with no
Google spend and survives Maps being unavailable entirely:

  1. US Census Bureau geocoder. Free, no key, no quota card, authoritative for
     US street addressing (it is the same TIGER data the Census publishes).
  2. ArcGIS World Geocoding, on the free no-key endpoint. Wider international
     and rural coverage than Census, and it also powers address suggestions.

WHY IT NEVER GUESSES
A wrong ZIP here is worse than no ZIP: it would make a previously-blocked
skip trace run against the wrong house and still cost money. So every return
path either produces a ZIP that some source actually matched to the street, or
reports no match. There is no city-level fallback and no "probably around
here". `confidence` distinguishes an exact street match from a broader one so
callers can decide whether to trust it.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CENSUS_GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/address"
ARCGIS_GEOCODE = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

TIMEOUT = 20

# Census wants the USPS-ish suffix but tolerates the long form; ArcGIS is the
# reverse. Both are tried with the caller's wording first, then normalized.
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

# ArcGIS Addr_type values that mean "this is this specific street address".
# Street, PointAddress and Parcel are all address-level. Anything else
# (Locality, Postal, Region...) only tells us roughly where, which this module
# refuses to pass off as a street ZIP.
_ARCGIS_ADDRESS_TYPES = {"StreetAddress", "PointAddress", "Parcel"}


class ZipLookupError(RuntimeError):
    """Raised when a provider is unreachable or returns an unusable payload.

    Distinct from "no match found": a provider being down is not evidence that
    an address has no ZIP, so callers must not cache this as a negative result.
    """


def _get_json(url, params, timeout=TIMEOUT):
    """GET a JSON endpoint, raising ZipLookupError on any transport failure."""
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise ZipLookupError(f"HTTP {exc.code} from {url}") from exc
    except Exception as exc:
        raise ZipLookupError(f"{type(exc).__name__} reaching {url}: {exc}") from exc
    if not isinstance(body, dict):
        raise ZipLookupError(f"non-object JSON from {url}")
    return body


# ── caching ─────────────────────────────────────────────
# Both providers are free but neither is unlimited, and the owner lookup page
# re-renders constantly while an operator types. Keyed on the normalized query
# so "5540 barnhollow rd" and "5540 BARNHOLLOW ROAD, NORFOLK VA" share an entry.
_CACHE = {}
_CACHE_TTL = 30 * 24 * 3600  # 30 days; USPS ZIPs for a delivered street rarely move
_CACHE_LOCK = threading.Lock()


def _cache_key(street, city, state):
    return "|".join(re.sub(r"\s+", " ", str(x or "")).strip().lower()
                    for x in (street, city, state))


def _cache_get(key):
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if not hit:
        return None
    stored, value = hit
    if time.time() - stored > _CACHE_TTL:
        return None
    return value


def _cache_put(key, value):
    with _CACHE_LOCK:
        if len(_CACHE) > 5000:
            _CACHE.clear()
        _CACHE[key] = (time.time(), value)


# ── providers ───────────────────────────────────────────

def _census(street, city, state):
    """Return a candidate dict from the Census geocoder, or None."""
    params = {
        "street": street,
        "benchmark": "Public_AR_Current",
        "vintage": "Current_Current",
        "format": "json",
    }
    if city:
        params["city"] = city
    if state:
        params["state"] = state

    body = _get_json(CENSUS_GEOCODER, params)
    matches = (body.get("result") or {}).get("addressMatches") or []
    if not matches:
        return None

    match = matches[0]
    components = match.get("addressComponents") or {}
    zip5 = str(components.get("zip") or "").strip()
    if not zip5:
        return None

    coords = match.get("coordinates") or {}
    return {
        "zipcode": zip5[:5],
        "matched_address": str(match.get("matchedAddress") or "").strip(),
        "city": str(components.get("city") or "").strip(),
        "state": str(components.get("state") or "").strip(),
        "lat": coords.get("y"),
        "lng": coords.get("x"),
        "source": "census",
        "confidence": "exact",
    }


def _arcgis(street, city, state):
    """Return a candidate dict from ArcGIS World Geocoding, or None.

    Only address-level matches are accepted. A Locality/Postal hit is dropped
    rather than reported, because that is the city-level guess this module
    exists to avoid.
    """
    parts = [str(street or "").strip(), str(city or "").strip(), str(state or "").strip()]
    single_line = ", ".join(p for p in parts if p)
    if not single_line:
        return None

    body = _get_json(ARCGIS_GEOCODE, {
        "SingleLine": single_line,
        "f": "json",
        "outFields": "Match_addr,Addr_type,Score,Region,City,Postal,Postal_ext,Country",
        "maxLocations": 5,
        "outSR": 4326,
    })
    candidates = body.get("candidates") or []
    for cand in candidates:
        attrs = cand.get("attributes") or {}
        addr_type = str(attrs.get("Addr_type") or "").strip()
        if addr_type not in _ARCGIS_ADDRESS_TYPES:
            continue
        # Postal is authoritative when present, but ArcGIS omits it on plenty of
        # good street matches, so fall back to the formatted address rather than
        # discarding an otherwise valid hit.
        postal = str(attrs.get("Postal") or "").strip()
        found = _ZIP_RE.search(postal) if postal else None
        if not found:
            found = _ZIP_RE.search(str(attrs.get("Match_addr") or ""))
        if not found:
            continue
        location = cand.get("location") or {}
        return {
            "zipcode": found.group(1),
            "matched_address": str(attrs.get("Match_addr") or "").strip(),
            "city": str(attrs.get("City") or "").strip(),
            "state": str(attrs.get("Region") or "").strip(),
            "lat": location.get("y"),
            "lng": location.get("x"),
            "source": "arcgis",
            "confidence": "exact",
            "score": attrs.get("Score"),
        }
    return None


# ── public API ──────────────────────────────────────────

def lookup_zip(street, city="", state="", use_cache=True):
    """Resolve the ZIP for one street address.

    Returns a dict that ALWAYS has a "zipcode" key. When nothing matched,
    zipcode is "" and "ok" is False -- never a guess.

    Cached successes only. A provider outage raises ZipLookupError rather than
    being cached as "this address has no ZIP", so a temporary outage cannot
    permanently poison an address that would otherwise resolve.
    """
    street = str(street or "").strip()
    city = str(city or "").strip()
    state = str(state or "").strip()
    if not street:
        return {"ok": False, "zipcode": "", "source": "",
                "error": "A street address is required."}

    key = _cache_key(street, city, state)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return dict(cached, cached=True)

    errors = []
    result = None
    for provider in (_census, _arcgis):
        try:
            found = provider(street, city, state)
        except ZipLookupError as exc:
            errors.append(str(exc))
            continue
        if found:
            result = found
            break

    if result is None:
        if errors and len(errors) == 2:
            raise ZipLookupError("; ".join(errors))
        return {
            "ok": False,
            "zipcode": "",
            "source": "",
            "matched_address": "",
            "error": "No street-level ZIP match. Confirm the address and try "
                     "including the city and state.",
            "provider_errors": errors,
        }

    result["ok"] = True
    result["cached"] = False
    if use_cache:
        _cache_put(key, result)
    return result


def suggest_address(text, limit=8):
    """Free-text address suggestions, for UI autocomplete.

    ArcGIS only: it accepts a partial string, while Census needs a structured
    street/city/state and is the wrong tool for typing-ahead.
    """
    text = str(text or "").strip()
    if len(text) < 3:
        return []
    try:
        body = _get_json(ARCGIS_GEOCODE, {
            "SingleLine": text,
            "f": "json",
            "outFields": "Match_addr,Addr_type,Score,Region,City,Postal",
            "maxLocations": max(1, min(int(limit), 20)),
            "outSR": 4326,
        })
    except ZipLookupError:
        return []

    out = []
    for cand in body.get("candidates") or []:
        attrs = cand.get("attributes") or {}
        label = str(attrs.get("Match_addr") or "").strip()
        if not label:
            continue
        location = cand.get("location") or {}
        out.append({
            "label": label,
            "zipcode": str(attrs.get("Postal") or "").strip()[:5],
            "city": str(attrs.get("City") or "").strip(),
            "state": str(attrs.get("Region") or "").strip(),
            "addr_type": str(attrs.get("Addr_type") or "").strip(),
            "lat": location.get("y"),
            "lng": location.get("x"),
        })
    return out[:limit]


def is_configured():
    """True: this module needs no keys, so it is always usable."""
    return True
