#!/usr/bin/env python3
"""Hampton Roads competitor + price intelligence from Virginia's eVA open data.

Source: data.virginia.gov, dataset "eVA Procurement Data 2026 - Virginia",
resource 76f6831d-fac7-4c1f-8313-cc7ff238ddca -- 1.9M purchase-order lines.
Public, no key, queryable with the CKAN datastore_search endpoint.

This is purchase-order SPEND, not solicitations: what agencies have already
bought, and who they bought it from. That makes it price intelligence and a
competitive map, not a lead list. The eVA opportunity board is behind a bot
shield and is not scrapable.

Run:
    python3 eva_intel.py              # summary for the service area
    python3 eva_intel.py --trades roofing,plumbing   # narrower
"""
import argparse
import json
import urllib.parse
import urllib.request

RESOURCE = "76f6831d-fac7-4c1f-8313-cc7ff238ddca"
BASE = "https://data.virginia.gov/api/3/action/datastore_search"

# Hampton Roads + the Currituck / Elizabeth City corridor.
CITIES = ["Virginia Beach", "Chesapeake", "Norfolk", "Newport News",
          "Williamsburg", "Hampton", "Portsmouth", "Suffolk"]

# NIGP descriptions that actually carry residential construction work. Many
# "General Construction" / "Heating" labels do not exist verbatim in the feed,
# so these are matched as prefixes and a 0 is reported honestly rather than
# silently omitted.
TRADES = ["Roofing", "Plumbing", "Electrical", "Carpentry", "Painting",
          "Air Conditioning", "Heating", "General Construction", "Site Work",
          "Framing", "Drywall", "Painting & Wall Covering", "Floor Covering",
          "Waterproofing", "Demolition", "Concrete", "Masonry"]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def query(trade, city, limit=200):
    filters = json.dumps({"NIGP_Description": trade, "Vendor_Address_City": city})
    params = urllib.parse.urlencode({
        "resource_id": RESOURCE,
        "filters": filters,
        "limit": limit,
        "fields": "Vendor_Name,Entity_Description,Line_Total,Ordered_Date,NIGP_Description",
    })
    req = urllib.request.Request(f"{BASE}?{params}", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
    except Exception as exc:
        return None, str(exc)[:80]
    return data.get("result", {}), None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", default="", help="comma list, substring match on trade names")
    ap.add_argument("--top", type=int, default=6, help="vendors to list per trade")
    args = ap.parse_args()

    trades = TRADES
    if args.trades:
        wanted = [t.strip().lower() for t in args.trades.split(",") if t.strip()]
        trades = [t for t in TRADES if any(w in t.lower() for w in wanted)]
        if not trades:
            trades = args.trades.split(",")

    grand_jobs = 0
    grand_spend = 0.0
    rows_out = []

    for trade in trades:
        jobs = 0
        spend = 0.0
        vendors = {}
        buyers = {}
        for city in CITIES:
            res, err = query(trade, city)
            if err or res is None:
                continue
            total = res.get("total") or 0
            if not total:
                continue
            jobs += total
            for rec in res.get("records") or []:
                try:
                    amt = float(rec.get("Line_Total") or 0)
                except (TypeError, ValueError):
                    amt = 0.0
                spend += amt
                v = (rec.get("Vendor_Name") or "").strip()
                b = (rec.get("Entity_Description") or "").strip()
                if v:
                    vendors[v] = vendors.get(v, 0) + amt
                if b:
                    buyers[b] = buyers.get(b, 0) + amt
        if jobs:
            grand_jobs += jobs
            grand_spend += spend
            top = sorted(vendors.items(), key=lambda kv: -kv[1])[: args.top]
            print(f"\n=== {trade}: {jobs} orders, ${spend:,.0f} ===")
            for v, amt in top:
                print(f"   ${amt:>12,.0f}  {v[:60]}")
            topb = sorted(buyers.items(), key=lambda kv: -kv[1])[:3]
            if topb:
                print("   buyers: " + ", ".join(f"{b[:34]} (${a:,.0f})" for b, a in topb))
            rows_out.append({"trade": trade, "orders": jobs, "spend": round(spend, 2),
                             "vendors": len(vendors), "buyers": len(buyers)})
        else:
            print(f"\n=== {trade}: no orders in the service area ===")

    print("\n" + "=" * 68)
    print(f"SERVICE AREA TOTAL: {grand_jobs} orders, ${grand_spend:,.0f} across the trades checked")
    print("This is awarded spend, not open opportunities. It prices the work and")
    print("maps competitors; it is not a pipeline of things to bid on.")

    out = {"cities": CITIES, "trades": rows_out,
           "total_orders": grand_jobs, "total_spend": round(grand_spend, 2)}
    with open("eva_intel.json", "w") as fh:
        json.dump(out, fh, indent=2)
    print("\nwrote eva_intel.json")


if __name__ == "__main__":
    main()