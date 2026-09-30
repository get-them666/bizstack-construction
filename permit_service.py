"""Building-permit job finder.

Providers, in priority order:

1. Shovels.ai API v2 (https://api.shovels.ai/v2). Needs SHOVELS_API_KEY. Without
   a key this path returns nothing.
2. Municipal open data, which needs no key and is published for reuse:
     - Virginia Beach: ArcGIS FeatureServer (Building Permits Applications)
     - Norfolk: Socrata dataset fahm-yuh4
   These are the reliable defaults, so the pipeline works with zero config.
3. With neither available we seed demo permits so the admin UX still renders.

Neither the open feeds nor Shovels publish applicant phone/email: the cities
pseudonymize the applicant (Virginia Beach exposes `CreatedBy` as an opaque
`PUBLICUSER<n>` account id). Owner name + mailing address for a permit address
comes from each city's public property-tax search, which refuses direct API
access -- see docs/records-request.md for the public-records route instead.

Service area defaults to Chesapeake / Virginia Beach / Norfolk (Hampton Roads).
"""

import os
import json
import re
import urllib.request
import urllib.parse
from datetime import date, timedelta


API_BASE = "https://api.shovels.ai/v2"

DEFAULT_GEO_IDS = (
    "23320,23321,23322,23323,23324,23325,23326,23327,23328,"
    "23451,23452,23453,23454,23455,23456,23457,23458,23459,23460,"
    "23461,23462,23463,23464,"
    "23502,23503,23504,23505,23507,23508,23509,23510,23511,23513,"
    "23517,23518,23519,23523,23529"
)

CITY_ZIPS = {
    "chesapeake": ["23320", "23321", "23322", "23323", "23324", "23325", "23326", "23327", "23328"],
    "virginia beach": ["23451", "23452", "23453", "23454", "23455", "23456", "23457", "23458",
                       "23459", "23460", "23461", "23462", "23463", "23464"],
    "norfolk": ["23502", "23503", "23504", "23505", "23507", "23508", "23509", "23510", "23511",
                "23513", "23517", "23518", "23519", "23523", "23529"],
}

SELF_APPLICANTS = {"self", "owner", "homeowner", "owner-builder", "property owner", "n/a", ""}

TRADE_RE = re.compile(
    r"(roof(ing| repair| replacement)?|siding|exterior( paint| stucco| vinyl)?|window(s)?|door(s)?|"
    r"deck|porch|patio|fence|railing|gutter|kitchen|bath(room)?|remodel|renovat|addition|add.?on|"
    r"garage|basement|finish( es|ing)?|foundation|concrete|driveway|paint|stucco|vinyl|shingle|"
    r"shed|ADU|accessory dwelling|solar|carport)",
    re.I,
)

DEMO = [
    {
        "permit_number": "VA-CHE-2026-04117",
        "property_address": "4120 Longhill Road, Chesapeake, VA",
        "city": "Chesapeake", "state": "VA",
        "work_type": "Additions",
        "job_description": "Two-story addition with primary suite",
        "contractor_name": "Self",
        "issue_date": "2026-09-02",
        "estimated_value": 185000,
    },
    {
        "permit_number": "VA-VB-2026-03341",
        "property_address": "889 Shore Drive, Virginia Beach, VA",
        "city": "Virginia Beach", "state": "VA",
        "work_type": "Residential Alteration",
        "job_description": "Whole-home renovation, kitchen + baths, roof replacement",
        "contractor_name": "",
        "issue_date": "2026-09-05",
        "estimated_value": 120000,
    },
    {
        "permit_number": "VA-NFK-2026-08217",
        "property_address": "512 Granby Street, Norfolk, VA",
        "city": "Norfolk", "state": "VA",
        "work_type": "Structural Repair",
        "job_description": "Foundation and floor repair, interior rework",
        "contractor_name": "",
        "issue_date": "2026-09-08",
        "estimated_value": 62000,
    },
    {
        "permit_number": "VA-CHE-2026-00512",
        "property_address": "178 Sowers Street, Chesapeake, VA",
        "city": "Chesapeake", "state": "VA",
        "work_type": "Residential Alteration",
        "job_description": "Full gut rehab, add rental unit",
        "contractor_name": "",
        "issue_date": "2026-09-09",
        "estimated_value": 98000,
    },
    {
        "permit_number": "VA-VB-2026-1077",
        "property_address": "36 Duck Road, Virginia Beach, VA",
        "city": "Virginia Beach", "state": "VA",
        "work_type": "Additions",
        "job_description": "Deck rebuild and room addition",
        "contractor_name": "",
        "issue_date": "2026-09-11",
        "estimated_value": 54000,
    },
]


def is_configured() -> bool:
    return bool(os.getenv("SHOVELS_API_KEY", "").strip())


def _geo_ids_for(city: str = "") -> list:
    city = (city or "").strip().lower()
    zips = CITY_ZIPS.get(city)
    if not city:
        zips = [z.strip() for z in (os.getenv("PERMIT_GEO_IDS", "") or "").split(",") if z.strip()]
    if not zips:
        zips = DEFAULT_GEO_IDS.split(",")
    return zips


def _request(path: str, params: dict):
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "X-API-Key": os.environ["SHOVELS_API_KEY"],
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _contractor_name(p: dict) -> str:
    c = p.get("contractor")
    if isinstance(c, dict):
        return (c.get("name") or c.get("company") or c.get("company_name") or "").strip()
    if isinstance(c, str):
        return c.strip()
    return (p.get("contractor_name") or "").strip()


def _normalize(p: dict, geo_id: str = "") -> dict:
    addr_obj = p.get("address")
    addr = ""
    if isinstance(addr_obj, dict):
        parts = []
        if addr_obj.get("street_no"):
            parts.append(str(addr_obj.get("street_no")))
        if addr_obj.get("street"):
            parts.append(str(addr_obj.get("street")))
        addr = " ".join(parts).strip()
    if not addr:
        addr = (p.get("full_address") or p.get("street_address")
                or p.get("property_address") or "").strip()
    city = ""
    state = ""
    if isinstance(addr_obj, dict):
        city = (addr_obj.get("city") or "").title()
        state = (addr_obj.get("state") or "").upper()
        if city and addr:
            addr = f"{addr}, {city}, {state} {' '.join(str(x) for x in [addr_obj.get('zip_code')] if x)}".strip()
    return {
        "permit_number": (p.get("number") or p.get("permit_number") or p.get("permit")
                          or p.get("id") or ""),
        "property_address": addr,
        "city": city or (p.get("city") or "").title(),
        "state": state or (p.get("state") or "").upper(),
        "work_type": (p.get("type") or p.get("work_type") or p.get("job_type")
                      or p.get("permit_type") or ""),
        "job_description": (p.get("job_type_description") or p.get("job_description")
                            or p.get("description_derived") or p.get("description")
                            or p.get("work_description") or "").strip(),
        "contractor_name": _contractor_name(p),
        "issue_date": (p.get("file_date") or p.get("issue_date") or p.get("issued_date")
                       or p.get("issued") or p.get("permit_date") or ""),
        "estimated_value": _as_money(p.get("job_value") or p.get("estimated_value")
                                     or p.get("estimated_job_value") or p.get("value")
                                     or p.get("valuation") or p.get("project_value")),
        "property_type": (p.get("property_type") or p.get("property_use") or ""),
        "tags": list(p.get("tags") or []),
        "_id": p.get("id") or "",
        "_geo": geo_id,
        "_demo": False,
    }


def _as_money(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _permit_text(p: dict) -> str:
    return f"{p.get('work_type') or ''} {p.get('job_description') or ''}"


def wants_lead(p: dict) -> bool:
    """Heuristic: is this permit a homeowner-driven job worth a follow-up lead?"""
    if p.get("_demo"):
        return False
    pt = (p.get("property_type") or "").lower()
    if "commercial" in pt:
        return False
    if not TRADE_RE.search(_permit_text(p)):
        return False
    try:
        min_value = float(os.getenv("PERMIT_MIN_VALUE", "10000") or 10000)
        tiny = float(os.getenv("PERMIT_MIN_JOB", "2000") or 2000)
    except (TypeError, ValueError):
        min_value, tiny = 10000, 2000
    val = p.get("estimated_value") or 0
    if 0 < val < tiny:
        return False
    name = (p.get("contractor_name") or "").lower().strip() or ""
    if name and name not in SELF_APPLICANTS:
        return False
    if val and val < min_value:
        return False
    return True


def search_permits(geo_ids=None, days: int = 21, per_geo: int = 4):
    """Hit Shovels v2 /permits/search for each geo and normalize results."""
    if not is_configured():
        return []
    geo_ids = [g.strip() for g in (geo_ids or _geo_ids_for()) if g.strip()]
    if not geo_ids:
        return []
    try:
        max_per_geo = min(int(os.getenv("PERMIT_MAX_PER_GEO", "4") or 4), 100)
        max_total = int(os.getenv("PERMIT_MAX_TOTAL", "120") or 120)
    except (TypeError, ValueError):
        max_per_geo, max_total = 4, 120
    if not per_geo:
        per_geo = max_per_geo
    per_geo = max(1, min(int(per_geo), max_per_geo))

    from_date = (date.today() - timedelta(days=max(int(days), 1))).isoformat()
    to_date = date.today().isoformat()
    out = []
    errored = []
    for geo in geo_ids:
        if len(out) >= max_total:
            errored.append(f"hit {max_total}-permit cap; remaining geos skipped")
            break
        try:
            data = _request("/permits/search", {
                "geo_id": geo,
                "permit_from": from_date,
                "permit_to": to_date,
                "property_type": "residential",
                "size": per_geo,
            })
        except Exception as exc:
            errored.append(f"geo {geo}: {exc}"[:160])
            continue
        items = data.get("items") if isinstance(data, dict) else data
        if not isinstance(items, list):
            continue
        for p in items:
            out.append(_normalize(p, geo))
            if len(out) >= max_total:
                break
    return out


def fetch_permits(city: str, state: str = "", days: int = 21, limit: int = 25):
    """Fetch recent permits for the service area. Returns a list of permit dicts.

    Order: Shovels when configured, else the city's open data feed. Demo permits
    are only a last resort, so a missing Shovels key still yields real leads.
    """
    city = (city or "").strip().title()
    if not city:
        return []

    if is_configured():
        per_geo = 4
        try:
            per_geo = min(int(limit or 4), 10)
        except (TypeError, ValueError):
            pass
        rows = search_permits(
            geo_ids=_geo_ids_for(city),
            days=max(int(days or 21), 1),
            per_geo=max(per_geo, 1),
        )
        if rows:
            return rows

    # Municipal open data is keyless and published for reuse, so prefer it over
    # seeding demo rows: real permits beat fake ones even if a key is missing.
    open_rows = fetch_open_data(city, days=max(int(days or 21), 1), limit=max(int(limit or 25), 25) * 4)
    if open_rows:
        return open_rows

    return _seed_demo(city, state or "VA")


def _seed_demo(city: str, state: str):
    seeded = []
    for d in DEMO:
        row = dict(d)
        if city.lower() not in ("", "demo", "any"):
            row["city"] = city
            row["state"] = state
        row["_demo"] = True
        seeded.append(row)
    return seeded


# --- Municipal open data ----------------------------------------------------
# Both of these are public datasets published for reuse and need no key or
# registration, so they are the default source when Shovels is unconfigured.

# City of Virginia Beach -- "Building Permits Applications", ArcGIS FeatureServer.
# https://data.virginiabeach.gov/datasets/VBgov::building-permits-applications-
VB_FS = ("https://services2.arcgis.com/CyVvlIiUfRBmMQuu/arcgis/rest/services/"
         "Building_Permits_Applications_view/FeatureServer/0")

# City of Norfolk -- "Permits" (Planning DSC), Socrata dataset fahm-yuh4.
NORFOLK_SOCRATA = "https://data.norfolk.gov/resource/fahm-yuh4.json"

_USER_AGENT = "bizstack-construction/1.0 (permit lead research)"


def _http_json(url: str, params: dict | None = None, timeout: int = 30):
    """GET a JSON endpoint. Raises on transport/HTTP failure."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _iso_date(v) -> str:
    """Normalise the several date shapes these feeds use to YYYY-MM-DD."""
    s = str(v or "").strip()
    if not s:
        return ""
    m = re.match(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return s


def _norm_record(
    permit_number="", address="", city="", state="", zip_code="",
    work_type="", description="", contractor="", issue_date="",
    value=0.0, property_type="", source="",
) -> dict:
    """Build a permit dict in the same shape _normalize() produces."""
    address = (address or "").strip()
    city = (city or "").strip().title()
    state = (state or "VA").strip().upper()
    full = address
    if address and city:
        full = f"{address}, {city}, {state} {zip_code}".strip()
    return {
        "permit_number": (permit_number or "").strip(),
        "property_address": full,
        "city": city,
        "state": state,
        "work_type": (work_type or "").strip(),
        "job_description": (description or "").strip()[:2000],
        "contractor_name": (contractor or "").strip(),
        "issue_date": _iso_date(issue_date),
        "estimated_value": _as_money(value),
        "property_type": (property_type or "").strip(),
        "tags": [],
        "_id": "",
        "_geo": zip_code,
        "_source": source,
        "_demo": False,
    }


def fetch_virginia_beach(days: int = 45, limit: int = 200) -> list:
    """Recent VB building permits from the city's ArcGIS FeatureServer.

    IssueDate is published as a 'YYYY/MM/DD' string, so a server-side range
    filter compares wrongly (and would silently return nothing). The feed is
    ordered newest-first, so we pull a bounded page and filter locally.
    """
    since = (date.today() - timedelta(days=max(int(days or 45), 1))).isoformat()
    params = {
        # The layer rejects a query with no where clause. A real date range
        # would be wrong here (IssueDate is 'YYYY/MM/DD', so a lexical compare
        # against 'YYYY-MM-DD' misbehaves), so bound the page and filter below.
        "where": "1=1",
        "outFields": ("PermitNumber,PermitType,ConstructionType,WorkType,ApplicationDate,"
                      "IssueDate,Status,WorkDesc,GPIN,StreetAddress,AddressUnit,City,State,Zip"),
        "orderByFields": "IssueDate DESC",
        "resultRecordCount": max(1, min(int(limit or 200), 2000)),
        "returnGeometry": "false",
        "f": "json",
    }
    data = _http_json(f"{VB_FS}/query", params)
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"VB ArcGIS error: {data['error']}")

    out = []
    for feat in (data or {}).get("features", []):
        a = feat.get("attributes") or {}
        issued = _iso_date(a.get("IssueDate") or a.get("ApplicationDate"))
        if issued and issued < since:
            continue
        addr = (str(a.get("StreetAddress") or "")).strip()
        unit = (str(a.get("AddressUnit") or "")).strip()
        if unit and not re.search(re.escape(unit), addr, re.I):
            addr = f"{addr} {unit}".strip()
        out.append(_norm_record(
            permit_number=str(a.get("PermitNumber") or ""),
            address=addr,
            city=str(a.get("City") or ""),
            state=str(a.get("State") or "VA"),
            zip_code=str(a.get("Zip") or ""),
            work_type=str(a.get("WorkType") or a.get("PermitType") or a.get("ConstructionType") or ""),
            description=str(a.get("WorkDesc") or ""),
            # CreatedBy is an opaque portal account (PUBLICUSER<n>), never a
            # person. Treating it as a contractor name would make every row look
            # "already engaged", so it is deliberately not mapped here.
            contractor="",
            issue_date=issued,
            value=0.0,  # VB publishes no declared value on this layer
            property_type=str(a.get("ConstructionType") or ""),
            source="vb_open_data",
        ))
    return out


def fetch_norfolk(days: int = 45, limit: int = 200) -> list:
    """Recent Norfolk permits from the city's Socrata dataset."""
    since = (date.today() - timedelta(days=max(int(days or 45), 1))).isoformat()
    params = {
        "$select": ("permit_number,address,type,use_class,work_type,use_type,structure,"
                    "status,application_date,square_footage"),
        "$where": f"application_date >= '{since}T00:00:00'",
        "$order": "application_date DESC",
        "$limit": max(1, min(int(limit or 200), 50000)),
    }
    rows = _http_json(NORFOLK_SOCRATA, params)
    out = []
    for r in rows or []:
        out.append(_norm_record(
            permit_number=str(r.get("permit_number") or ""),
            address=(str(r.get("address") or "")).split(" : ")[0].strip(),
            city="Norfolk",
            state="VA",
            work_type=str(r.get("work_type") or r.get("use_type") or r.get("type") or ""),
            description=str(r.get("use_type") or r.get("type") or ""),
            contractor="",  # this feed publishes no contractor field
            issue_date=str(r.get("application_date") or ""),
            value=0.0,
            property_type=str(r.get("use_class") or ""),
            source="norfolk_open_data",
        ))
    return out


OPEN_DATA_SOURCES = {
    "virginia beach": [fetch_virginia_beach],
    "norfolk": [fetch_norfolk],
    # Chesapeake is deliberately absent. Its ArcGIS "Development Tracking" layer
    # looks like a permit feed but is land-use actions (use permits,
    # subdivisions, rezoning), newest entry April 2022, no applicant fields --
    # and its Parcels/Address Points layers carry no owner name. Importing it
    # would fill /leads with 2019 rezoning records that look like live jobs.
    # Chesapeake building permits live in eBUILD (Accela), behind session-only
    # access; the records request in docs/records-request.md is the way in.
    "chesapeake": [],
}


def fetch_open_data(city: str = "", days: int = 45, limit: int = 200) -> list:
    """Fetch permits from municipal open data for the given city.

    Returns [] for cities with no open feed rather than raising, so a single
    bad source cannot take down the whole scan.
    """
    city = (city or "").strip().lower()
    fetchers = OPEN_DATA_SOURCES.get(city)
    if not fetchers:
        return []
    out = []
    for fn in fetchers:
        try:
            out.extend(fn(days=days, limit=limit))
        except Exception as exc:
            print(f"[permit-scan] open data {fn.__name__} ({city}): {exc}"[:200], flush=True)
    return out