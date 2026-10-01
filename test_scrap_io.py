"""Scrap.io lead source: offline assertions on the traps that silently corrupt
results. No network: SCRAP_IO_API_KEY is unset and urlopen is blocked.

What this pins down, from failures observed against the live API:

- `admin1` is ignored; only `admin1_code` scopes state. Passing the wrong name
  is not an error, it just returns other states' businesses.
- `city` is a fuzzy name match. `city=Arlington` returned TX, MA and VA in one
  page, so a state filter that trusts the query string ships wrong-state leads.
- Type lists are capped at 5 per plan tier; a longer list fails the whole query.
- Enrichment costs a credit each, so it must be capped and must only fire on
  rows that actually lack an email.
"""
import os
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

FAILS = []
NET_CALLS = []


def check(name, ok, detail=""):
    detail = str(detail)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


class _Blocked(Exception):
    pass


def _no_network(*a, **k):
    NET_CALLS.append(a[0] if a else "?")
    raise _Blocked("network blocked in tests")


urllib.request.urlopen = _no_network
for _v in ("SCRAP_IO_API_KEY", "LEAD_SOURCES_SCRAP_IO", "SCRAP_IO_CITIES",
           "SCRAP_IO_TYPES", "SCRAP_IO_PAGES", "SCRAP_IO_ENRICH",
           "SCRAP_IO_ENRICH_MAX", "SCRAP_IO_STATES"):
    os.environ.pop(_v, None)

import scrap_io as s  # noqa: E402

print("[1] disabled by default (it costs credits)")
check("enabled() is False with no env", s.enabled() is False)
check("no key -> scan returns disabled, not a crash",
      s.scan_scrap_io().get("disabled") == ["scrap-io"])
check("no key -> subscription reports the reason",
      s.subscription().get("ok") is False)

print("\n[2] state filter is applied client-side, not trusted from the query")
os.environ["SCRAP_IO_API_KEY"] = "test-key-not-real"
os.environ["SCRAP_IO_CITIES"] = "Arlington,VA"
os.environ["SCRAP_IO_TYPES"] = "general-contractor"
os.environ["SCRAP_IO_STATES"] = "VA"

# Mirrors the live response for city=Arlington: three states on one page.
ROWS = [
    {"google_id": "va1", "name": "M-R Custom Homes", "phone": "(703) 376-4883",
     "website": "https://www.mrcustomhomes.com", "location_state": "Virginia",
     "location_admin1_code": "VA", "location_city": "Arlington",
     "location_street_1": "1 Main St", "location_postal_code": "22201",
     "types": [{"type": "general-contractor", "is_main": True}],
     "reviews_count": 12, "reviews_rating": 4.5},
    {"google_id": "tx1", "name": "Stovall Construction Inc", "phone": "(817) 572-1331",
     "website": "http://www.stovallconstructioninc.com", "location_state": "Texas",
     "location_admin1_code": "TX", "location_city": "Arlington",
     "location_street_1": "2 Main St", "location_postal_code": "76010",
     "types": [{"type": "general-contractor", "is_main": True}]},
    {"google_id": "ma1", "name": "RB Farina Roofing Co", "phone": "(781) 648-5446",
     "website": "http://www.farinaroof.com", "location_state": "Massachusetts",
     "location_admin1_code": "MA", "location_city": "Arlington",
     "location_street_1": "3 Main St", "location_postal_code": "02474",
     "types": [{"type": "roofing-contractor", "is_main": True}]},
]

SEEN_PARAMS = []


def fake_get(path, params):
    SEEN_PARAMS.append((path, dict(params)))
    if path == "subscription":
        return {"subscription": {"plan": "Basic plan", "active": True, "on_trial": True,
                                 "features": {"EXPORT_CREDITS": {"remaining": 9980, "total": 10000},
                                              "SEARCH_ADMIN2_CODE": {"value": False},
                                              "SEARCH_WHOLE_COUNTRY": {"value": False}}}}
    if path == "gmap/search":
        return {"meta": {"status": "completed", "count": "293", "has_more_pages": False,
                         "next_cursor": None, "per_page": 10}, "data": ROWS}
    if path == "gmap/place":
        return {"meta": {"status": "completed", "count": "1", "has_more_pages": False},
                "data": [dict(ROWS[0], website_data={"emails": [
                    {"email": "info@mrcustomhomes.com", "has_mx": True, "is_webmail": False,
                     "is_disposable": False, "category": "info-contact", "sources": ["https://mrcustomhomes.com"]},
                    {"email": "bob@gmail.com", "has_mx": True, "is_webmail": True,
                     "is_disposable": False, "category": "general", "sources": ["x"]},
                    {"email": "dead@mrcustomhomes.com", "has_mx": False, "is_webmail": False,
                     "is_disposable": False, "category": "general", "sources": ["y"]},
                ]})]}
    return {}


s._get = fake_get
res = s.scan_scrap_io()
names = [m["title"] for m in res["matches"]]

check("only the in-state row survives", names == ["M-R Custom Homes"], f"got {names}")
check("search sends admin1_code, not admin1",
      all("admin1_code" in p for _, p in SEEN_PARAMS if _ == "gmap/search")
      and not any("admin1" in p for _, p in SEEN_PARAMS if _ == "gmap/search"))
check("enrichment filled a real email", res["matches"][0]["email"] == "info@mrcustomhomes.com",
      res["matches"][0]["email"])

print("\n[3] webmail / no-MX addresses are rejected")
check("webmail filtered", "bob@gmail.com" not in str(res))
check("no-MX filtered", "dead@mrcustomhomes.com" not in str(res))

print("\n[4] plan caps and credit safety")
os.environ["SCRAP_IO_TYPES"] = "|".join(["a", "b", "c", "d", "e", "f", "g"])
check("type list truncated to 5", len(s._types("construction")) == 5, s._types("construction"))
os.environ["SCRAP_IO_TYPES"] = "general-contractor"
os.environ["SCRAP_IO_ENRICH"] = "0"
SEEN_PARAMS.clear()
res2 = s.scan_scrap_io()
check("enrich disabled makes no /gmap/place calls",
      not any(p == "gmap/place" for p, _ in SEEN_PARAMS))

os.environ["SCRAP_IO_ENRICH"] = "1"
os.environ["SCRAP_IO_ENRICH_MAX"] = "1"
call_count = {"n": 0}
base_get = fake_get


def counting_get(path, params):
    if path == "gmap/place":
        call_count["n"] += 1
    return base_get(path, params)


s._get = counting_get
s.scan_scrap_io()
check("SCRAP_IO_ENRICH_MAX is honoured", call_count["n"] <= 1, f"{call_count['n']} calls")
s._get = base_get

print("\n[5] normalization matches lead_sources._normalize's shape")
m = res["matches"][0]
for key in ("external_id", "title", "contact_name", "phone", "email", "service",
            "address", "state", "description", "url", "source"):
    check(f"key {key!r} present", key in m)
check("source is scrap-io", m["source"] == "scrap-io")
check("state is the postal code, not 'Virginia'", m["state"] == "VA", m["state"])
check("state name -> code fallback works",
      s._state_code({"location_state": "Texas"}) == "TX"
      and s._state_code({"location_admin1_code": "ma", "location_state": "Massachusetts"}) == "MA")

import lead_sources  # noqa: E402
sam_keys = set(lead_sources._normalize({
    "title": "x", "pointOfContacts": [{"email": "a@b.co", "phone": "1", "fullName": "n"}],
    "placeOfPerformance": [{"streetAddress": "1 A", "city": "B", "state": "VA", "zipCode": "2"}],
    "naics": [{"value": "236118"}],
}).keys())
check("sam-gov and scrap-io agree on the ingest contract", set(m.keys()) == sam_keys,
      f"scrap={sorted(set(m.keys()))} sam={sorted(sam_keys)}")

print("\n[6] city parsing")
os.environ["SCRAP_IO_CITIES"] = "Arlington,VA;Richmond, VA\nFalls Church"
check("multi-city parse", s._cities() == [("Arlington", "VA"), ("Richmond", "VA"), ("Falls Church", "")],
      s._cities())

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): {FAILS}")
    sys.exit(1)
print("All scrap_io offline tests passed.")