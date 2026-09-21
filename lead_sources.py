"""Scheduled public lead sources for BizStack apps (no API keys).

Source: SAM.gov Contract Opportunities public search endpoint (keyless; needs
the `application/hal+json` Accept header). We query by business-relevant NAICS
codes, page through recent postings, keep only those whose place of performance
is in the operator's target states, drop award/archival notices, and return a
normalized list the app turns into leads (with the posted point-of-contact).

Presets:
  construction  residential / light-commercial remodel & trade work (default)
  broom         janitorial, grounds, and building-services contracts

Tuning knobs (env):
  LEAD_SOURCE_PRESET     "construction" | "broom" (default construction)
  LEAD_STATES            comma list, default "VA,NC"
  LEAD_LOOKBACK_DAYS     default 30
  LEAD_NAICS             pipe-separated override for the NAICS list
  LEAD_SAM_PER_QUERY     page size per query, default 50 (max 100)
  LEAD_SAM_PAGES         pages per NAICS query, default 1
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

SAM_SEARCH = "https://sam.gov/api/prod/sgs/v1/search/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

PRESETS = {
    "construction": {
        # Residential / light-commercial GC and specialty trades.
        "naics": [
            "236118",  # Residential Remodelers
            "236115",  # New Single-Family Housing Construction
            "238160",  # Roofing Contractors
            "238210",  # Electrical Contractors
            "238220",  # Plumbing, Heating, and Air-Conditioning
            "238310",  # Drywall and Insulation
            "238330",  # Flooring Contractors
            "238340",  # Tile and Terrazzo
            "238350",  # Finish Carpentry
            "238130",  # Framing Contractors
            "238110",  # Poured Concrete Foundation
            "238910",  # Site Preparation
            "238990",  # All Other Specialty Trade (fence/deck/etc.)
        ],
        "per_query": 50,
        "pages": 1,
    },
    "broom": {
        # Short-term-rental turnover cleaning & co-hosting; building services.
        "naics": [
            "561720",  # Janitorial Services
            "561790",  # Other Services to Buildings and Dwellings (power washing)
            "561730",  # Landscaping Services (groundskeeping)
            "561210",  # Facilities Support Services
            "561740",  # Carpet and Upholstery Cleaning
        ],
        "per_query": 100,
        "pages": 2,
    },
}

_SKIP_TYPE_BITS = ("award", "justification")


def _preset(name=None):
    name = (name or os.getenv("LEAD_SOURCE_PRESET", "construction") or "construction").strip().lower()
    return PRESETS.get(name) or PRESETS["construction"], name


def _target_states():
    raw = os.getenv("LEAD_STATES", "VA,NC")
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


def _lookback_days():
    try:
        return max(1, int(os.getenv("LEAD_LOOKBACK_DAYS", "30")))
    except (TypeError, ValueError):
        return 30


def _spec(preset):
    cfg, _name = _preset(preset)
    raw = os.getenv("LEAD_NAICS", "").strip()
    naics = [c.strip() for c in raw.split("|") if c.strip()] if raw else list(cfg["naics"])
    try:
        per_query = max(1, min(100, int(os.getenv("LEAD_SAM_PER_QUERY", str(cfg["per_query"])))))
    except (TypeError, ValueError):
        per_query = cfg["per_query"]
    try:
        pages = max(1, min(10, int(os.getenv("LEAD_SAM_PAGES", str(cfg["pages"])))))
    except (TypeError, ValueError):
        pages = cfg["pages"]
    return naics, per_query, pages


def _request(params):
    url = f"{SAM_SEARCH}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/hal+json",
            "User-Agent": USER_AGENT,
            "Referer": "https://sam.gov/search/",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _clean_email(raw):
    e = re.sub(r"\s+", "", str(raw or ""))
    e = e.lstrip("<").rstrip(">").rstrip(".")
    if "@" not in e or "." not in e.split("@", 1)[1]:
        return ""
    return e


def _poc(result):
    for c in result.get("pointOfContacts") or []:
        email = _clean_email(c.get("email"))
        phone = (c.get("phone") or "").strip()
        if email or phone:
            return {"name": (c.get("fullName") or "").strip(), "email": email, "phone": phone}
    return {"name": "", "email": "", "phone": ""}


def _address(result):
    pops = result.get("placeOfPerformance") or []
    if not pops:
        return "", ""
    pop = pops[0] or {}
    street = (pop.get("streetAddress") or "").replace("\r", " ").replace("\n", " ").strip()
    city = (pop.get("city") or "").strip()
    state = (pop.get("state") or "").strip().upper()
    zipc = (pop.get("zip") or "").strip()
    parts = [p for p in (street, city, state, zipc) if p]
    return ", ".join(parts), state


def _naics_label(result):
    naics = result.get("naics") or []
    if isinstance(naics, list) and naics:
        return (naics[0] or {}).get("value") or ""
    return ""


def _description(result):
    bits = []
    sol = result.get("solicitationNumber")
    if sol:
        bits.append(f"Solicitation {sol}")
    typ = (result.get("type") or {}).get("value")
    if typ:
        bits.append(typ)
    archive = result.get("archiveDate") or result.get("responseDeadLine")
    if archive:
        bits.append(f"closes {str(archive)[:10]}")
    descs = result.get("descriptions") or []
    if isinstance(descs, list) and descs:
        body = (descs[0] or {}).get("content") or ""
        if body:
            bits.append(body.strip()[:600])
    return " · ".join(bits)


def _normalize(result):
    title = (result.get("title") or "").strip()
    address, state = _address(result)
    poc = _poc(result)
    ext_id = result.get("_id") or result.get("cleanSolicitationNumber") or title
    return {
        "external_id": str(ext_id),
        "title": title,
        "contact_name": poc["name"],
        "phone": poc["phone"],
        "email": poc["email"],
        "service": _naics_label(result) or "Public sector services contract",
        "address": address,
        "state": state,
        "description": _description(result),
        "url": f"https://sam.gov/opp/{ext_id}/view" if ext_id else "",
        "source": "sam-gov",
    }


def scan_sam_gov(preset=None):
    """Return {'matches': [...], 'errors': [...]} of recent local public contracts."""
    states = _target_states()
    naics_list, per_query, pages = _spec(preset)
    today = date.today()
    posted_from = (today - timedelta(days=_lookback_days())).strftime("%m/%d/%Y")
    posted_to = today.strftime("%m/%d/%Y")
    matches = {}
    errors = []
    for naics in naics_list:
        for page in range(pages):
            params = {
                "index": "opp",
                "naics": naics,
                "size": per_query,
                "page": page,
                "postedFrom": posted_from,
                "postedTo": posted_to,
                "sort": "-modifiedDate",
            }
            try:
                data = _request(params)
            except urllib.error.HTTPError as exc:
                errors.append(f"naics {naics} p{page}: HTTP {exc.code}")
                continue
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                errors.append(f"naics {naics} p{page}: {exc}")
                continue
            results = (data.get("_embedded", {}) or {}).get("results", []) or []
            if not results:
                break
            for r in results:
                typ = ((r.get("type") or {}).get("value") or "").lower()
                if any(bit in typ for bit in _SKIP_TYPE_BITS):
                    continue
                norm = _normalize(r)
                if not norm["title"]:
                    continue
                # Local-only: require a confirmed target-state place of performance.
                if states and norm["state"] not in states:
                    continue
                matches[norm["external_id"]] = norm
    return {"matches": list(matches.values()), "errors": errors}


def scan(limit=None, preset=None):
    """Aggregate all enabled public sources."""
    result = scan_sam_gov(preset)
    if limit:
        result["matches"] = result["matches"][:limit]
    return result
