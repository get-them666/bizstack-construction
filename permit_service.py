"""Building-permit job finder.

Primary provider: Shovels.ai (a developer-friendly permits API). Without a key,
we seed realistic demo permits so the admin UX works during development and
smoke tests are deterministic.
"""

import os
import json
import urllib.request
import urllib.parse
from datetime import date, timedelta


DEMO = [
    {
        "permit_number": "VA-WB-2026-04117",
        "property_address": "4120 Longhill Road, Williamsburg, VA",
        "city": "Williamsburg", "state": "VA",
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
        "permit_number": "VA-NN-2026-08217",
        "property_address": "512 River Road, Newport News, VA",
        "city": "Newport News", "state": "VA",
        "work_type": "Structural Repair",
        "job_description": "Foundation and floor repair, interior rework",
        "contractor_name": "",
        "issue_date": "2026-09-08",
        "estimated_value": 62000,
    },
    {
        "permit_number": "VA-EC-2026-00512",
        "property_address": "178 Sowers Street, Elizabeth City, NC",
        "city": "Elizabeth City", "state": "NC",
        "work_type": "Residential Alteration",
        "job_description": "Full gut rehab, add rental unit",
        "contractor_name": "",
        "issue_date": "2026-09-09",
        "estimated_value": 98000,
    },
    {
        "permit_number": "NC-CURR-2026-1077",
        "property_address": "36 Duck Road, Corolla, NC",
        "city": "Corolla", "state": "NC",
        "work_type": "Additions",
        "job_description": "Deck rebuild and room addition at STR rental",
        "contractor_name": "",
        "issue_date": "2026-09-11",
        "estimated_value": 54000,
    },
]


def is_configured() -> bool:
    return bool(os.getenv("SHOVELS_API_KEY", "").strip())


def fetch_permits(city: str, state: str = "", days: int = 21, limit: int = 25):
    """Fetch recent permits for a city/state. Returns a list of permit dicts."""
    city = (city or "").strip().title()
    if not city or not is_configured():
        return _seed_demo(city or "Williamsburg", state or "VA")

    from_date = (date.today() - timedelta(days=max(int(days), 1))).isoformat()
    params = {"state": state, "min_date": from_date, "limit": int(limit)}
    url = f"https://api.shovels.ai/permits/{urllib.parse.quote(city)}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {os.environ['SHOVELS_API_KEY']}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"❌ Shovels.ai fetch failed for {city}: {e}")
        return []

    rows = data if isinstance(data, list) else data.get("permits", [])
    out = []
    for p in rows:
        addr = p.get("property_address") or p.get("address") or ""
        out.append({
            "permit_number": p.get("permit_number") or p.get("id") or "",
            "property_address": addr,
            "city": city,
            "state": (p.get("state") or state or "").upper(),
            "work_type": p.get("work_type") or p.get("job_type") or "",
            "job_description": p.get("job_type_description") or p.get("description") or "",
            "contractor_name": p.get("contractor_name") or "",
            "issue_date": p.get("issue_date") or "",
            "estimated_value": float(p.get("estimated_value") or p.get("value") or 0),
        })
    return out


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