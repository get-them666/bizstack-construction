"""Property lookup for instant ballpark quotes.

Primary provider: RentCast (real API, cheap, has a free tier). If no key is
configured, the caller falls back to a manual square-footage entry so the
feature still works during development / smoke tests.
"""

import os
import json
import urllib.request
import urllib.parse

RENTCAST_BASE = "https://api.rentcast.io/v1"

# RentCast sometimes returns numbers as {"value": X, "units": "sqft"}.
def _num(val):
    if isinstance(val, dict):
        v = val.get("value")
        try:
            return 0 if v is None else float(v)
        except (TypeError, ValueError):
            return 0
    try:
        return 0 if val is None else float(val)
    except (TypeError, ValueError):
        return 0


def is_configured() -> bool:
    return bool(os.getenv("RENTCAST_API_KEY", "").strip())


def lookup_address(address: str):
    """Look up a US property by street address. Returns a normalized dict or None."""
    addr = (address or "").strip()
    if not addr or not is_configured():
        return None
    query = urllib.parse.urlencode({"address": addr})
    url = f"{RENTCAST_BASE}/properties?{query}"
    req = urllib.request.Request(
        url,
        headers={"X-Api-Key": os.environ["RENTCAST_API_KEY"], "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"❌ RentCast lookup failed for {addr!r}: {e}")
        return None
    results = data if isinstance(data, list) else data.get("properties", [])
    if not results:
        return None
    p = results[0]
    return {
        "address": p.get("formattedAddress") or addr,
        "city": p.get("city") or "",
        "state": p.get("state") or "",
        "zip": p.get("zipCode") or "",
        "sqft": _num(p.get("squareFootage")),
        "lot_sqft": _num(p.get("lotSizeSqFt")),
        "beds": _num(p.get("bedrooms")),
        "baths": _num(p.get("bathrooms")),
        "year_built": _num(p.get("yearBuilt")),
        "stories": max(int(_num(p.get("stories")) or 1), 1),
        "property_type": p.get("propertyType") or "",
        "source": "rentcast",
    }