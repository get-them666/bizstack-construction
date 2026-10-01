"""Open-data permit sources: normalization, lead filtering, and no-network guards.

The live endpoints are exercised only when PERMIT_LIVE=1, so this runs offline
by default and asserts the pure logic that broke before:

- VB's ArcGIS layer needs a `where` clause and a `/query` suffix, and publishes
  IssueDate as 'YYYY/MM/DD'. Omitting any of those returns 0 features *without
  an error*, which is the failure that shipped.
- Neither feed publishes a contractor or owner contact. Mapping VB's opaque
  `CreatedBy` (a PUBLICUSER<n> account) into contractor_name would make every
  row look already-engaged and silently suppress leads.
"""
import os
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

FAILS = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


import permit_service as ps  # noqa: E402

print("[1] endpoint shapes")
check("VB layer URL targets /query when queried",
      ps.VB_FS.endswith("/FeatureServer/0"),
      f"VB_FS={ps.VB_FS}")
check("Norfolk dataset is a Socrata resource URL",
      ps.NORFOLK_SOCRATA.startswith("https://data.norfolk.gov/resource/"),
      ps.NORFOLK_SOCRATA)

print("\n[2] date normalisation handles VB's YYYY/MM/DD")
for raw, want in (("2026/09/13", "2026-09-13"), ("2026/1/3", "2026-01-03"),
                  ("2026-09-13T00:00:00", "2026-09-13"), ("", ""), (None, "")):
    got = ps._iso_date(raw)
    check(f"_iso_date({raw!r}) == {want!r}", got == want, f"got {got!r}")

print("\n[3] normalized rows match the shape ingest_permits() expects")
row = ps._norm_record(permit_number="2026-BDRA-00001", address="1 Test St",
                      city="virginia beach", state="va", zip_code="23451",
                      work_type="Deck", description="Build deck", contractor="",
                      issue_date="2026/09/01", value=0.0, source="vb_open_data")
for field in ("permit_number", "property_address", "city", "state", "work_type",
              "job_description", "contractor_name", "issue_date", "estimated_value",
              "property_type", "_demo"):
    check(f"row has {field}", field in row)
check("address is composed with city/state/zip",
      row["property_address"] == "1 Test St, Virginia Beach, VA 23451", row["property_address"])
check("city is title-cased", row["city"] == "Virginia Beach", row["city"])
check("state is upper-cased", row["state"] == "VA", row["state"])
check("issue_date converted", row["issue_date"] == "2026-09-01", row["issue_date"])
check("_demo is False", row["_demo"] is False)
check("estimated_value is a float", isinstance(row["estimated_value"], float))

print("\n[4] an empty contractor makes a permit a lead candidate")
deck = ps._norm_record(permit_number="X1", address="2 Oak Ave", city="Norfolk",
                       work_type="Residential Alteration",
                       description="Build 21x15 sunroom deck, replace roof",
                       contractor="", issue_date="2026-09-01")
check("no contractor + trade scope => wants_lead", ps.wants_lead(deck) is True,
      f"text={ps._permit_text(deck)!r}")

engaged = dict(deck, contractor_name="Smith Roofing LLC")
check("named contractor => not a lead", ps.wants_lead(engaged) is False)

self_diy = dict(deck, contractor_name="Owner")
check("self/owner applicant => still a lead", ps.wants_lead(self_diy) is True)

combo = ps._norm_record(permit_number="X2", address="3 Elm", city="Norfolk",
                        work_type="Tenant Buildout", description="NW tenant buildout",
                        contractor="", issue_date="2026-09-01",
                        property_type="Commercial")
check("commercial is rejected", ps.wants_lead(combo) is False)

print("\n[4b] a finalised or cancelled permit is a job that is over")
# Neither open feed publishes a contractor, so "already engaged" is unknowable.
# Status is the one free signal that separates live work from finished work, and
# it was being dropped entirely: _norm_record() had no status argument, so a
# Final permit imported beside an Active one and both looked like open leads.
for dead in ("Final", "Expired", "Void", "finaled", "C/O Issued", "Cancelled",
             "Abandoned", "Withdrawn", "Denied", "Revoked", "Completed"):
    row = dict(deck, status=dead)
    check(f"{dead!r} is not a lead", ps.wants_lead(row) is False)

for live in ("Active", "Issued", "Authorized", "Pending", "New",
             "Reviews in progress", "Documents submitted"):
    row = dict(deck, status=live)
    check(f"{live!r} is still a lead", ps.wants_lead(row) is True)

# A source with no status vocabulary must not be silently dropped: Shovels and
# the demo seed publish none, and treating absent as dead would empty both.
check("no status is treated as live", ps.wants_lead(deck) is True)
check("permit_is_live is True when status is blank", ps.permit_is_live(deck) is True)
check("permit_is_live is False when finalised",
      ps.permit_is_live(dict(deck, status="Finaled")) is False)
# Case and whitespace come from the feed, not from us.
check("status normalisation is case/space insensitive",
      ps._permit_status("  C/O   Issued ") == "c/o issued",
      ps._permit_status("  C/O   Issued "))
# 'cancelled' and 'abandoned' read as live to a substring check but are not.
check("cancelled is not caught by an 'active' substring",
      ps.permit_is_live(dict(deck, status="Cancelled")) is False)
check("row carries a normalised status",
      ps._norm_record(permit_number="X3", address="4 Fir", city="Norfolk",
                      work_type="Deck", status="Active")["status"] == "active")

print("\n[5] Chesapeake is not wired to its stale land-use layer")
# Its "Development Tracking" ArcGIS layer is land-use actions with a 2022
# newest entry and no applicant fields. Importing it would fill /leads with
# rezoning records that read like live jobs, so it must stay unregistered.
check("Chesapeake has no open-data fetcher",
      not ps.OPEN_DATA_SOURCES.get("chesapeake"),
      f"unexpectedly registered: {ps.OPEN_DATA_SOURCES.get('chesapeake')}")
che = ps.fetch_permits("Chesapeake", "VA", days=90, limit=25)
check("Chesapeake falls back to clearly-marked demo rows",
      che and all(r.get("_demo") for r in che),
      f"got {len(che)} rows, demo={sum(1 for r in che if r.get('_demo'))}")

print("\n[6] live endpoints return usable leads (PERMIT_LIVE=1)")
if os.getenv("PERMIT_LIVE") == "1":
    for name, fn in (("Virginia Beach", ps.fetch_virginia_beach), ("Norfolk", ps.fetch_norfolk)):
        try:
            rows = fn(days=90, limit=200)
            leads = [r for r in rows if ps.wants_lead(r)]
            check(f"{name} returns permits", len(rows) > 0, f"got {len(rows)}")
            check(f"{name} yields leads", len(leads) > 0, f"got {len(leads)}")
            check(f"{name} rows are not demo", not any(r.get("_demo") for r in rows))
            dated = [r for r in rows if r["issue_date"]]
            check(f"{name} rows carry a date", len(dated) == len(rows),
                  f"{len(dated)}/{len(rows)}")
            bad = [r for r in rows if r["permit_number"].lower().startswith("publicuser")]
            check(f"{name} never maps CreatedBy into a permit number", not bad,
                  f"{len(bad)} rows leaked PUBLICUSER")
            # The status field must survive the fetch, or the new filter is
            # reading '' everywhere and treating every permit as live.
            check(f"{name} rows carry a status", all(r.get("status") for r in rows),
                  f"{sum(1 for r in rows if not r.get('status'))}/{len(rows)} blank")
            dead = [r for r in leads if not ps.permit_is_live(r)]
            check(f"{name} yields no dead permits as leads", not dead,
                  f"{len(dead)} dead: {sorted({r['status'] for r in dead})[:6]}")
        except Exception as e:
            check(f"{name} fetch", False, repr(e))
else:
    print("  SKIP  set PERMIT_LIVE=1 to hit the live feeds")

print("\n" + "=" * 60)
if FAILS:
    print(f"{len(FAILS)} FAILING:")
    for f in FAILS:
        print(f"  - {f}")
    raise SystemExit(1)
print("open permit data sources are wired correctly")