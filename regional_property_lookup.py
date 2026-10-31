"""Verified public parcel lookups for the core Hampton Roads cities.

Only endpoints that were checked against live data are listed here. The old
version of this module guessed at ten city GIS hosts; most of those hostnames
did not resolve at all, and the one that did returned 404. A quote form that
promises to prefill square footage and then silently does nothing is worse than
one that asks for it.

Supported today:

    Norfolk       Socrata "Property Assessment and Sales" (FY27)
    Portsmouth    ArcGIS Parcels_Real_Estate_View
    Newport News  ArcGIS Operational/Parcel

Every other locality returns ``None``. The caller then falls back to RentCast
when it is configured, and finally to the manual square-footage field. No
result is ever invented from a parcel polygon or a building-value field.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Optional

import requests

# A lookup is cheap and the public endpoints are not rate-limit friendly when a
# visitor types an address one character at a time. Cache both hits and misses.
_CACHE_TTL = 60 * 60
_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
_cache_lock = threading.Lock()

_TIMEOUT = 8

_SUFFIXES = {
    "ROAD": "RD", "RD": "RD",
    "STREET": "ST", "ST": "ST",
    "AVENUE": "AVE", "AVE": "AVE", "AV": "AVE",
    "BOULEVARD": "BLVD", "BLVD": "BLVD",
    "DRIVE": "DR", "DR": "DR",
    "LANE": "LN", "LN": "LN",
    "COURT": "CT", "CT": "CT",
    "CIRCLE": "CIR", "CIR": "CIR",
    "PLACE": "PL", "PL": "PL",
    "TERRACE": "TER", "TER": "TER",
    "TRAIL": "TRL", "TRL": "TRL",
    "PARKWAY": "PKWY", "PKWY": "PKWY",
    "HIGHWAY": "HWY", "HWY": "HWY",
    "WAY": "WAY",
}

_DIRECTIONS = {
    "NORTH": "N", "N": "N",
    "SOUTH": "S", "S": "S",
    "EAST": "E", "E": "E",
    "WEST": "W", "W": "W",
    "NORTHEAST": "NE", "NE": "NE",
    "NORTHWEST": "NW", "NW": "NW",
    "SOUTHEAST": "SE", "SE": "SE",
    "SOUTHWEST": "SW", "SW": "SW",
}

_LOCALITIES = (
    ("NEWPORT NEWS", "newport_news"),
    ("NORFOLK", "norfolk"),
    ("PORTSMOUTH", "portsmouth"),
)

_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def _normalize(text: str) -> str:
    """Uppercase, collapse punctuation, and squash whitespace."""
    return " ".join(re.sub(r"[^A-Z0-9 ]+", " ", (text or "").upper()).split())


def parse_address(address: str) -> Optional[dict[str, str]]:
    """Split a US street address into the pieces the parcel services use.

    Returns ``None`` when there is no usable house number + street name. The
    city comes from the comma-separated part when present, then from a scan of
    the whole string, so both "8494 Lynn River Road, Norfolk, VA 23503" and a
    ZIP-less address still work.
    """
    raw = " ".join((address or "").split())
    if not raw:
        return None

    parts = [p.strip() for p in raw.split(",")]
    street = parts[0]
    match = re.match(r"^(\d+[A-Za-z]?)\s+(.+)$", street)
    if not match:
        return None

    number = match.group(1).upper()
    tokens = _normalize(match.group(2)).split()
    if not tokens:
        return None

    direction = ""
    if tokens[0] in _DIRECTIONS:
        direction = _DIRECTIONS[tokens.pop(0)]
    if not tokens:
        return None

    suffix = ""
    if tokens[-1] in _SUFFIXES:
        suffix = _SUFFIXES[tokens.pop()]
    if not tokens:
        return None

    name = " ".join(tokens)

    city = ""
    if len(parts) > 1:
        city = _normalize(parts[1])
    locality = ""
    for needle, key in _LOCALITIES:
        if needle == city or needle in _normalize(raw):
            locality = key
            city = needle
            break

    zip_match = _ZIP_RE.search(raw)
    return {
        "number": number,
        "direction": direction,
        "street": name,
        "suffix": suffix,
        "city": city,
        "locality": locality,
        "zip": zip_match.group(1) if zip_match else "",
    }


def _get_json(url: str, params: dict[str, Any]) -> Any:
    response = requests.get(url, params=params, timeout=_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> Optional[int]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(round(number)) if number else None


def _sane_sqft(value: Any) -> Optional[int]:
    sqft = _as_float(value)
    if 100 <= sqft <= 100_000:
        return int(round(sqft))
    return None


def _clean_result(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize numeric strings from Socrata/ArcGIS into real numbers.

    The templates format these with ``{:,.0f}``; a string year built would
    raise at render time and blank the whole quote result card.
    """
    row.pop("_address", None)
    row["address"] = " ".join(str(row.get("address") or "").split())
    row["sqft"] = _sane_sqft(row.get("sqft"))
    row["lot_sqft"] = _as_int(row.get("lot_sqft"))
    row["beds"] = _as_int(row.get("beds"))
    row["baths"] = _as_int(row.get("baths"))
    row["year_built"] = _as_int(row.get("year_built"))
    row["stories"] = _as_int(row.get("stories"))
    return row


def _street_matches(address: str, street: str) -> bool:
    """True only for a whole-word street-name match.

    ``"IVY" in "928 IVYSTONE WAY"`` is true, which would let a larger house on
    a different street win the tie-break. Word boundaries keep Ivy Ave and
    Ivystone Way apart.
    """
    return bool(re.search(rf"\b{re.escape(street)}\b", address))


def _pick(candidates: list[dict[str, Any]], parsed: dict[str, str]) -> Optional[dict[str, Any]]:
    """Prefer an exact street-name match, then a ZIP match, then a real sqft."""
    def score(row: dict[str, Any]) -> tuple:
        address = _normalize(row.get("_address", ""))
        exact = 1 if _street_matches(address, parsed["street"]) else 0
        zip_match = 1 if parsed["zip"] and address.endswith(parsed["zip"]) else 0
        has_sqft = 1 if _sane_sqft(row.get("sqft")) else 0
        return (has_sqft, exact, zip_match)

    with_sqft = [r for r in candidates if _sane_sqft(r.get("sqft"))]
    if not with_sqft:
        return None
    return max(with_sqft, key=score)


def _norfolk(parsed: dict[str, str]) -> Optional[dict[str, Any]]:
    where = [
        f"property_street_number='{parsed['number']}'",
        f"upper(property_street_name) like '%{parsed['street']}%'",
    ]
    if parsed["direction"]:
        where.append(f"upper(property_street_direction)='{parsed['direction']}'")
    if parsed["zip"]:
        where.append(f"property_zip like '{parsed['zip']}%'")

    rows = _get_json(
        "https://data.norfolk.gov/resource/qva7-tzrf.json",
        {
            "$where": " AND ".join(where),
            "$limit": 20,
            "$order": "residential_finished_living DESC",
        },
    )
    if not isinstance(rows, list):
        return None

    candidates = []
    for row in rows:
        address = " ".join(
            str(row.get(key) or "")
            for key in (
                "property_street_number",
                "property_street_direction",
                "property_street_name",
                "property_street_type",
            )
        )
        candidates.append({
            "_address": address,
            "address": address.title(),
            "city": "Norfolk",
            "state": "VA",
            "zip": str(row.get("property_zip") or parsed["zip"]).split("-")[0],
            "sqft": row.get("residential_finished_living") or row.get("commercial_building_area"),
            "lot_sqft": row.get("land_square_footage"),
            "beds": None,
            "baths": None,
            "year_built": row.get("improvement_year_built"),
            "property_type": row.get("property_use") or "",
            "source": "norfolk_socrata",
        })

    chosen = _pick(candidates, parsed)
    if not chosen:
        return None
    return _clean_result(chosen)


def _portsmouth(parsed: dict[str, str]) -> Optional[dict[str, Any]]:
    direction = f" {parsed['direction']}" if parsed["direction"] else ""
    patterns = [f"{parsed['number']}{direction} {parsed['street']}%"]
    # Some records store the direction after the street name, and some omit
    # it entirely. Ask for a loose prefix and let _pick score the results.
    patterns.append(f"{parsed['number']}%{parsed['street']}%")

    where = " OR ".join(f"SITE_ADDRE LIKE '{p}'" for p in patterns)
    data = _get_json(
        "https://services1.arcgis.com/nGsguNiHLn7MU4R4/arcgis/rest/services/"
        "Parcels_Real_Estate_View/FeatureServer/3/query",
        {
            "where": where,
            "outFields": "SITE_ADDRE,SITE_ZIP,TOT_SQ_FT,STORY,BEDROOMS,BATH,BLDG_DATE,TYPE_PROP",
            "returnGeometry": "false",
            "resultRecordCount": 20,
            "f": "json",
        },
    )
    features = data.get("features", []) if isinstance(data, dict) else []
    candidates = []
    for feature in features:
        attr = feature.get("attributes", {})
        candidates.append({
            "_address": " ".join(
                str(attr.get(key) or "") for key in ("SITE_ADDRE", "SITE_ZIP")
            ),
            "address": str(attr.get("SITE_ADDRE") or "").title(),
            "city": "Portsmouth",
            "state": "VA",
            "zip": str(attr.get("SITE_ZIP") or parsed["zip"]),
            "sqft": attr.get("TOT_SQ_FT"),
            "lot_sqft": None,
            "beds": attr.get("BEDROOMS") or None,
            "baths": attr.get("BATH") or None,
            "year_built": attr.get("BLDG_DATE"),
            "stories": attr.get("STORY") or None,
            "property_type": str(attr.get("TYPE_PROP") or "").title(),
            "source": "portsmouth_arcgis",
        })

    chosen = _pick(candidates, parsed)
    if not chosen:
        return None
    return _clean_result(chosen)


def _newport_news(parsed: dict[str, str]) -> Optional[dict[str, Any]]:
    direction = f" {parsed['direction']}" if parsed["direction"] else ""
    patterns = [f"{parsed['number']}{direction} {parsed['street']}%"]
    patterns.append(f"{parsed['number']}%{parsed['street']}%")

    where = " OR ".join(f"SITEADDRESS LIKE '{p}'" for p in patterns)
    data = _get_json(
        "https://maps.nnva.gov/gis/rest/services/Operational/Parcel/MapServer/0/query",
        {
            "where": where,
            "outFields": "SITEADDRESS,RESFLRAREA,RESYRBLT,FLOORCOUNT,LIVUNIT,USEDSCRP",
            "returnGeometry": "false",
            "resultRecordCount": 20,
            "f": "json",
        },
    )
    features = data.get("features", []) if isinstance(data, dict) else []
    candidates = []
    for feature in features:
        attr = feature.get("attributes", {})
        candidates.append({
            "_address": " ".join(
                str(attr.get(key) or "")
                for key in ("SITEADDRESS", parsed["zip"])
            ),
            "address": str(attr.get("SITEADDRESS") or "").title(),
            "city": "Newport News",
            "state": "VA",
            "zip": parsed["zip"],
            "sqft": attr.get("RESFLRAREA"),
            "lot_sqft": None,
            "beds": None,
            "baths": None,
            "year_built": attr.get("RESYRBLT"),
            "stories": attr.get("FLOORCOUNT") or None,
            "property_type": str(attr.get("USEDSCRP") or "").title(),
            "source": "newport_news_arcgis",
        })

    chosen = _pick(candidates, parsed)
    if not chosen:
        return None
    return _clean_result(chosen)


_LOOKUPS = {
    "norfolk": _norfolk,
    "portsmouth": _portsmouth,
    "newport_news": _newport_news,
}


def lookup_property_regional(address: str) -> Optional[dict[str, Any]]:
    """Look up a supported city's public parcel record, or return ``None``.

    Never raises: the quote form treats a miss as "enter square footage".
    """
    parsed = parse_address(address)
    if not parsed or not parsed["locality"]:
        return None

    key = _normalize(address)
    now = time.time()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _CACHE_TTL:
            return cached[1]

    lookup = _LOOKUPS.get(parsed["locality"])
    result = None
    if lookup:
        try:
            result = lookup(parsed)
        except Exception:
            result = None

    with _cache_lock:
        _cache[key] = (now, result)
    return result
