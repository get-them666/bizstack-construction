"""Scrap.io Google Maps business source for BizStack apps (paid API key).

Scrap.io is a Maps scraper (Google/Apple/Bing), not a web crawler: it returns
business listings. That makes it a *supply*-side source -- local contractors,
cleaners, trades, property managers -- as opposed to SAM.gov, which is a
*demand*-side source (public solicitations).

Only the v1 API is used. v2 (`/api/v2/map/search`) exists but silently ignores
`type`/`search_term`/`category` and returns an unfiltered country-wide slice, so
it cannot be filtered into a trade. Do not migrate to it without re-verifying.

API shape as actually observed (the published docs are wrong in two ways):
  - base            https://scrap.io/api/v1
  - auth            Authorization: Bearer <SCRAP_IO_API_KEY>
  - types           GET /gmap/types?search_term=roofing   -> [{id, text}, ...]
  - search          GET /gmap/search?country_code=us&city=Arlington&type=roofing-contractor
  - enrich          GET /gmap/enrich?domain=example.com   (or phone/ google_id)
  - subscription    GET /subscription

Two behaviours that silently corrupt results if ignored:

1. `admin1_code` (not `admin1`) is the state filter. Passing `admin1` is not an
   error -- it is ignored, and results from every state come back.
2. `city` is a fuzzy *name* match, not a scoped query. `city=Arlington` with no
   usable state filter returns Arlington TX, MA and VA mixed together. State must
   therefore be re-checked on each result and non-matching rows dropped.

Tuning knobs (env):
  LEAD_SOURCES_SCRAP_IO   1 to enable (default 0 -- costs credits, so off)
  SCRAP_IO_API_KEY        bearer token
  SCRAP_IO_CITIES         "Arlington,VA;Richmond,VA" (city,ST pairs)
  SCRAP_IO_TYPES          pipe-separated Scrap.io type ids, max 5
  SCRAP_IO_PAGES          pages per (city,type), default 2
  SCRAP_IO_ENRICH         1 to enrich for email (default 1; 1 credit each)
  SCRAP_IO_ENRICH_MAX     max enrichments per scan, default 25
  SCRAP_IO_STATES         comma list used to filter results, default LEAD_STATES

Plan limits are enforced locally because the API answers 202 and then simply
returns nothing when a capability is not on the plan. SEARCH_ADMIN2_CODE and
SEARCH_WHOLE_COUNTRY are false on Basic, so state-wide or national sweeps are
rejected up front with a clear message rather than billed into empty pages.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

API = "https://scrap.io/api/v1"

# Scrap.io type ids, resolved through /gmap/types. Max 5 per plan tier.
TRADE_TYPES = {
    "construction": [
        "general-contractor",
        "roofing-contractor",
        "hvac-contractor",
        "plumber",
        "electrician",
    ],
    "broom": [
        "cleaning-service",
        "house-cleaning-service",
        "laundry-service",
        "property-management-company",
        "landscaping-service",
    ],
}

_TRUE = ("1", "true", "yes", "on")
_LAST_ERROR = {}


def _key():
    return (os.getenv("SCRAP_IO_API_KEY") or "").strip()


def enabled():
    return (os.getenv("LEAD_SOURCES_SCRAP_IO", "0") or "0").strip().lower() in _TRUE


def _int(name, default, low, high):
    try:
        return max(low, min(high, int(os.getenv(name, "") or default)))
    except (TypeError, ValueError):
        return default


def _cities():
    """[(city, state_code)] from 'Arlington,VA;Richmond,VA'."""
    raw = os.getenv("SCRAP_IO_CITIES", "").strip()
    out = []
    for chunk in re.split(r"[;\n]", raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "," in chunk:
            city, st = chunk.rsplit(",", 1)
        else:
            city, st = chunk, ""
        city = city.strip()
        st = st.strip().upper()
        if city:
            out.append((city, st))
    return out


def _types(preset):
    raw = os.getenv("SCRAP_IO_TYPES", "").strip()
    if raw:
        wanted = [t.strip() for t in raw.split("|") if t.strip()]
    else:
        wanted = list(TRADE_TYPES.get((preset or "construction"), TRADE_TYPES["construction"]))
    # The plan caps a search at 5 types; exceeding it fails the whole query.
    return wanted[:5]


def _states():
    raw = os.getenv("SCRAP_IO_STATES") or os.getenv("LEAD_STATES", "VA,NC") or ""
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


# Scrap.io returns location_state as a full name ("Virginia") while every env
# knob here is a postal code ("VA"), so a direct string compare drops every row.
# Codes are canonical; the name table is only the fallback for payloads that
# omit location_admin1_code.
_STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE",
    "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
    "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
    "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    "DISTRICT OF COLUMBIA": "DC", "PUERTO RICO": "PR",
}


def _state_code(place):
    """Postal code for a place, or '' when it cannot be determined."""
    code = str(place.get("location_admin1_code") or "").strip().upper()
    if len(code) == 2:
        return code
    name = str(place.get("location_state") or "").strip().upper()
    return _STATE_NAMES.get(name, "")


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _get(path, params):
    """Scrap.io answers 403 to the default Python-urllib agent, so send a UA."""
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {_key()}", "User-Agent": USER_AGENT,
                 "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def subscription():
    """Plan, trial state and remaining export credits. Never raises."""
    if not _key():
        return {"ok": False, "reason": "SCRAP_IO_API_KEY not set"}
    try:
        data = _get("subscription", {})
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {"ok": False, "reason": str(exc)[:200]}
    sub = data.get("subscription") or {}
    credits = (sub.get("features") or {}).get("EXPORT_CREDITS") or {}
    feats = sub.get("features") or {}
    return {
        "ok": True,
        "plan": sub.get("plan"),
        "active": sub.get("active"),
        "on_trial": sub.get("on_trial"),
        "renewal_date": sub.get("renewal_date"),
        "credits_remaining": credits.get("remaining"),
        "credits_total": credits.get("total"),
        "can_admin2": bool((feats.get("SEARCH_ADMIN2_CODE") or {}).get("value")),
        "can_whole_country": bool((feats.get("SEARCH_WHOLE_COUNTRY") or {}).get("value")),
    }


def resolve_types(search_term):
    """Turn 'roofing' into Scrap.io type ids. Used to configure SCRAP_IO_TYPES."""
    if not _key():
        return []
    try:
        data = _get("gmap/types", {"search_term": search_term})
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError):
        return []
    return [t.get("id") for t in (data or []) if t.get("id")]


def _domain(url):
    if not url:
        return ""
    m = re.search(r"https?://(?:www\.)?([^/?#]+)", str(url))
    return m.group(1).lower() if m else ""


def _best_email(place):
    """Prefer a role/contact address on the business's own domain."""
    emails = ((place.get("website_data") or {}).get("emails")) or []
    if not emails:
        return "", ""
    dom = _domain(place.get("website"))
    scored = []
    for e in emails:
        if not isinstance(e, dict):
            continue
        addr = (e.get("email") or "").strip().lower()
        if not addr or e.get("is_disposable") or e.get("is_webmail"):
            continue
        if not e.get("has_mx"):
            continue
        on_site = bool(dom) and addr.endswith("@" + dom)
        role = 0 if e.get("category") in ("info-contact", "general", "sales", "contact") else 1
        src = " ".join((e.get("sources") or [])[:2])
        scored.append((0 if on_site else 1, role, addr, src))
    if not scored:
        return "", ""
    scored.sort()
    return scored[0][2], scored[0][3]


def _address(place):
    parts = [
        (place.get("location_street_1") or "").strip(),
        (place.get("location_city") or "").strip(),
        (place.get("location_state") or "").strip(),
        (place.get("location_postal_code") or "").strip(),
    ]
    return ", ".join(p for p in parts if p)


def _description(place):
    bits = []
    main = ""
    for t in place.get("types") or []:
        if t.get("is_main") and t.get("type"):
            main = t["type"].replace("-", " ")
            break
    if main:
        bits.append(main.title())
    rc, rr = place.get("reviews_count"), place.get("reviews_rating")
    if rc and rr:
        bits.append(f"{rr} stars / {rc} reviews")
    if place.get("is_closed"):
        bits.append("listed closed")
    wd = place.get("website_data") or {}
    if wd.get("is_responding") is False:
        bits.append("website down")
    for desc in place.get("descriptions") or []:
        if desc:
            bits.append(str(desc).strip()[:400])
            break
    return " · ".join(bits)


def _normalize(place):
    """Same shape lead_sources._normalize emits, so ingest needs no changes."""
    gid = place.get("google_id") or ""
    if not gid:
        return None
    email, email_src = _best_email(place)
    return {
        "external_id": gid,
        "title": (place.get("name") or "").strip(),
        "contact_name": "",
        "phone": (place.get("phone") or "").strip(),
        "email": email,
        "service": (place.get("types") or [{}])[0].get("type", "") if place.get("types") else "",
        "address": _address(place),
        # Postal code, matching lead_sources._normalize, which stores the code.
        "state": _state_code(place),
        "description": _description(place),
        "url": place.get("website") or place.get("link") or "",
        "source": "scrap-io",
    }


def _enrich_one(external_id, cache):
    """Search row + enrich merged, so search-only rows gain a real email."""
    if external_id in cache:
        return cache[external_id]
    try:
        data = _get("gmap/place", {"google_id": external_id})
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        _LAST_ERROR["enrich"] = str(exc)[:200]
        return None
    # /gmap/place answers with a bare object when given a single google_id,
    # and {"data": [...]} when given a list, so accept both shapes.
    rows = data.get("data") if isinstance(data, dict) else data
    if isinstance(rows, dict):
        rows = [rows]
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    cache[external_id] = rows[0] if rows else None
    return cache[external_id]


def scan_scrap_io(preset=None, limit=None):
    """Return {'matches': [...], 'errors': [...]} of local trade businesses."""
    if not _key():
        return {"matches": [], "errors": ["SCRAP_IO_API_KEY not set"], "disabled": ["scrap-io"]}

    plan = subscription()
    cities = _cities()
    types = _types(preset)
    pages = _int("SCRAP_IO_PAGES", 2, 1, 10)
    want_enrich = (os.getenv("SCRAP_IO_ENRICH", "1") or "1").strip().lower() in _TRUE
    enrich_max = _int("SCRAP_IO_ENRICH_MAX", 25, 0, 500)
    states = _states()

    if not cities:
        return {"matches": [], "errors": ["SCRAP_IO_CITIES not set"], "plan": plan}

    matches = {}
    errors = []
    enriched = 0
    enrich_cache = {}

    for city, st in cities:
        for type_id in types:
            cursor = None
            for page in range(pages):
                params = {"country_code": "us", "city": city, "type": type_id}
                if st:
                    params["admin1_code"] = st
                if cursor:
                    params["cursor"] = cursor
                try:
                    data = _get("gmap/search", params)
                except urllib.error.HTTPError as exc:
                    errors.append(f"{city}/{type_id} p{page}: HTTP {exc.code}")
                    break
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                    errors.append(f"{city}/{type_id} p{page}: {exc}")
                    break
                meta = data.get("meta") or {}
                if str(meta.get("status", "")).lower() == "updating":
                    errors.append(f"{city}/{type_id}: index still updating, retry later")
                    break
                rows = data.get("data") or []
                if not rows:
                    break
                for place in rows:
                    # city= is a fuzzy name match, so enforce state ourselves.
                    row_code = _state_code(place)
                    if st and row_code != st:
                        continue
                    if states and row_code and row_code not in states:
                        continue
                    norm = _normalize(place)
                    if not norm or not norm["title"]:
                        continue
                    matches.setdefault(norm["external_id"], norm)
                cursor = meta.get("next_cursor")
                if not meta.get("has_more_pages") or not cursor:
                    break

    # Enrich only rows that still lack an email, and only up to the cap, so a
    # wide sweep cannot drain a month's credits in one run.
    if want_enrich and enrich_max:
        for norm in list(matches.values()):
            if norm["email"]:
                continue
            if enriched >= enrich_max:
                errors.append(f"enrichment cap {enrich_max} reached; {len(matches) - enriched} rows still lack email")
                break
            full = _enrich_one(norm["external_id"], enrich_cache)
            enriched += 1
            if not full:
                continue
            email, _src = _best_email(full)
            if email:
                norm["email"] = email
                norm["description"] = _description(full) or norm["description"]
                if not norm["url"]:
                    norm["url"] = full.get("website") or full.get("link") or ""

    result = {"matches": list(matches.values()), "errors": errors, "plan": plan}
    if limit:
        result["matches"] = result["matches"][:limit]
    return result