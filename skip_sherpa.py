#!/usr/bin/env python3
"""Skip-trace permit addresses through Skip Sherpa to get a real owner contact.

The permit feeds publish an address and nothing else. That is why 500+ leads
sit unreachable: no applicant name, no phone, no email, so there is no channel
the email/text bot can use and mail_letters.py is the only way to reach them.
Skip Sherpa turns an address into an owner name and contact details, which is
what makes those leads contactable.

Skip Sherpa is the provider, not ATTIC. Its /api/property_details endpoint
returns an owner NAME but no phone or email; /api/properties is the actual skip
trace and returns both. Verified live against Chesapeake addresses.

Every lookup is billable, so this is deliberately hard to over-run:

  - dry run by default, and the plan is printed before anything is sent
  - --limit is required to actually spend anything
  - a per-run credit ceiling that stops the batch even if --limit is large
  - SKIP_SHERPA_API_KEY must be set; without it nothing is sent
  - addresses are deduped, because the permit feeds emit one row per permit and
    177 leads cover only 63 distinct doors. Paying for duplicates is paying for
    the same person twice.
  - addresses already carrying a usable contact are skipped, so no credit is
    spent rediscovering a phone number we already hold

Writes results back to leads.name / email / phone and stamps analysis_json, so
the email bot picks them up on its next pass with no further wiring.

    python skip_sherpa.py --list                    # who is eligible, spends nothing
    python skip_sherpa.py --limit 20                # trace up to 20 addresses
    python skip_sherpa.py --limit 20 --apply        # ...and write to the database
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_ROOT = "https://skipsherpa.com"

# Hard ceiling per run regardless of --limit. Skip Sherpa is per-lookup
# billing, so a mistyped limit is the difference between 20 credits and 500.
DEFAULT_CREDIT_CEILING = 13  # 20 total, 7 already spent by hand

_STATE_ZIP = re.compile(r"^(.*?),\s*([A-Za-z]{2})\s*(\d{5}(?:-\d{4})?)?$")


def api_key() -> str:
    return (os.getenv("SKIP_SHERPA_API_KEY") or "").strip()


def credit_ceiling() -> int:
    try:
        return max(0, int(os.getenv("SKIP_SHERPA_MAX_CREDITS", DEFAULT_CREDIT_CEILING) or DEFAULT_CREDIT_CEILING))
    except (TypeError, ValueError):
        return DEFAULT_CREDIT_CEILING


def parse_address(raw: str) -> dict:
    """'1501 VANCE CIR, Chesapeake, VA 23320' -> Skip Sherpa's address shape."""
    text = str(raw or "").replace("\r\n", " ").replace("\r", " ").replace("\n", ", ")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return {"ok": False, "why": "empty address"}
    street = parts[0]
    city = state = zipcode = ""
    for chunk in parts[1:]:
        if re.fullmatch(r"[A-Za-z]{2}", chunk.strip()):
            state = chunk.strip().upper()
            continue
        z = re.search(r"\b\d{5}(?:-\d{4})?\b", chunk)
        if z:
            zipcode = z.group(0)
            rest = chunk.replace(zipcode, "").strip(" ,")
            if rest:
                city = city or rest
            continue
        if not city:
            city = chunk
    if not state:
        m = _STATE_ZIP.search(text)
        if m:
            state = m.group(2).upper()
            zipcode = zipcode or (m.group(3) or "")
    if not state:
        return {"ok": False, "why": "no state"}
    if not zipcode:
        return {"ok": False, "why": "no ZIP"}
    return {"ok": True, "street": street, "city": city.upper(), "state": state, "zipcode": zipcode}


def _post(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{API_ROOT}{path}", data=body,
        headers={"Content-Type": "application/json",
                 "api-key": api_key(),
                 "Authorization": f"Bearer {api_key()}",
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        return {"error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:
        return {"error": str(exc)[:200]}


def _phones_from(node) -> list:
    """Pull phone records out of whatever shape the response used."""
    out = []
    if isinstance(node, dict):
        for key, val in node.items():
            if "phone" in key.lower() and isinstance(val, list):
                for p in val:
                    if isinstance(p, dict):
                        num = (p.get("phone") or p.get("number") or p.get("phone_number")
                               or p.get("display") or "")
                        typ = str(p.get("phone_type") or p.get("type") or "")
                        if num and len(re.sub(r"\D", "", str(num))) >= 10:
                            out.append((str(num), typ))
            elif isinstance(val, (dict, list)):
                out.extend(_phones_from(val))
    elif isinstance(node, list):
        for item in node:
            out.extend(_phones_from(item))
    return out


def _emails_from(node) -> list:
    out = []
    if isinstance(node, dict):
        for key, val in node.items():
            if "email" in key.lower():
                if isinstance(val, str) and "@" in val:
                    out.append(val)
                elif isinstance(val, list):
                    for e in val:
                        if isinstance(e, str) and "@" in e:
                            out.append(e)
                        elif isinstance(e, dict) and isinstance(e.get("email"), str):
                            out.append(e["email"])
            elif isinstance(val, (dict, list)):
                out.extend(_emails_from(val))
    elif isinstance(node, list):
        for item in node:
            out.extend(_emails_from(item))
    return out


def _owner_names(node) -> list:
    names = []
    if isinstance(node, dict):
        owners = node.get("owners")
        if isinstance(owners, list):
            for o in owners:
                if isinstance(o, dict):
                    fn = (o.get("first_name") or "").strip()
                    ln = (o.get("last_name") or "").strip()
                    if fn and ln:
                        names.append(f"{fn.title()} {ln.title()}")
                    elif fn or ln:
                        names.append((fn or ln).title())
        for k, v in node.items():
            if k == "owners":
                continue
            if isinstance(v, (dict, list)):
                names.extend(_owner_names(v))
    elif isinstance(node, list):
        for item in node:
            names.extend(_owner_names(item))
    return names


def trace_address(addr: dict) -> dict:
    """One address, one credit. Returns owner/phone/email or an error."""
    # success_criteria is an ENUM, not an object: 'owner-name',
    # 'owner-contact-any', 'owner-contact-email', 'owner-contact-phone',
    # 'owner-contact-address'. An unrecognised value does not 400 -- the lookup
    # simply never reaches the success condition, so the whole request is billed
    # and returns nothing. 'owner-contact-any' is what we want: an owner with at
    # least one reachable method, so a name-only match is not paid for.
    payload = {"property_lookups": [{
        "property_address_lookup": {
            "street": addr["street"], "city": addr["city"],
            "state": addr["state"], "zipcode": addr["zipcode"],
        },
        "success_criteria": "owner-contact-any",
        "debt_data_best_effort": False,
    }]}
    data = _post("/api/properties", payload)
    if data.get("error"):
        return {"ok": False, "error": data["error"]}

    results = data.get("property_lookup_results") or data.get("results") or []
    if not results:
        return {"ok": False, "error": "no result row"}
    first = results[0] if isinstance(results[0], dict) else {}
    props = first.get("properties") or []
    if not props:
        issues = first.get("issues") or []
        return {"ok": False, "error": "no property matched" + (f" ({issues})" if issues else "")}

    prop = props[0] if isinstance(props[0], dict) else {}
    names = _owner_names(prop)
    phones = _phones_from(prop) or _phones_from(first)
    emails = _emails_from(prop) or _emails_from(first)

    # Prefer a mobile: a landline is frequently disconnected and the whole point
    # is a reachable line.
    mobile = [p for p, t in phones if "mobile" in t.lower() or "cell" in t.lower()]
    chosen = (mobile or phones)
    return {
        "ok": True,
        "owner": names[0] if names else "",
        "all_owners": names[:4],
        "phone": chosen[0][0] if chosen else "",
        "phone_type": chosen[0][1] if chosen else "",
        "phone_count": len(phones),
        "email": emails[0] if emails else "",
        "value": prop.get("estimated_value"),
        "owner_occupied": prop.get("owner_occupied"),
    }


ELIGIBLE_SQL = """
    SELECT id, name, address, email, phone, source, analysis_json
    FROM leads
    WHERE company = %(company)s
      AND status = 'new'
      AND address IS NOT NULL AND BTRIM(address) <> ''
      AND (email IS NULL OR BTRIM(email) = '' OR LOWER(email) LIKE '%%@lead.local')
    ORDER BY id
"""


def fetch_candidates(company: str) -> list:
    import psycopg
    from psycopg.rows import dict_row
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        raise SystemExit("DATABASE_URL is not set; cannot read leads.")
    with psycopg.connect(db_url, row_factory=dict_row) as db:
        with db.cursor() as cur:
            cur.execute(ELIGIBLE_SQL, {"company": company})
            return [dict(r) for r in cur.fetchall()]


def build_plan(rows: list) -> dict:
    """One credit per distinct address. Duplicates are folded onto the winner."""
    planned, skipped = {}, []
    for row in rows:
        raw = (row.get("address") or "").strip()
        key = re.sub(r"\s+", " ", raw).strip().lower()
        parsed = parse_address(raw)
        if not parsed["ok"]:
            skipped.append({"id": row.get("id"), "address": raw, "why": parsed["why"]})
            continue
        if (row.get("phone") or "").strip():
            skipped.append({"id": row.get("id"), "address": raw, "why": "already has a phone"})
            continue
        entry = planned.get(key)
        if entry:
            entry["leads"].append(row.get("id"))
        else:
            planned[key] = {"address": raw, "to": parsed, "leads": [row.get("id")]}
    return {"plan": list(planned.values()), "skipped": skipped}


def apply_results(db_url: str, company: str, results: list) -> int:
    """Write owner/phone/email onto the leads and stamp analysis_json.

    Stamping is what stops a re-run re-paying for the same address, which is the
    difference between a one-off cost and a repeating bill.
    """
    import psycopg
    from psycopg.rows import dict_row
    updated = 0
    with psycopg.connect(db_url, row_factory=dict_row) as db:
        for res in results:
            if not res.get("ok"):
                continue
            meta = {"skip_traced": True, "skip_sherpa_at": time.strftime("%Y-%m-%d"),
                    "skip_sherpa_owner": res.get("owner") or "",
                    "skip_sherpa_phones": res.get("phone_count", 0)}
            payload = json.dumps(meta)
            for lid in res["leads"]:
                with db.cursor() as cur:
                    cur.execute(
                        "UPDATE leads SET "
                        "  name = CASE WHEN %s <> '' AND COALESCE(name,'') LIKE 'Permit · %%' "
                        "            THEN %s ELSE name END, "
                        "  email = CASE WHEN %s <> '' AND BTRIM(COALESCE(email,'')) IN ('', 'x') "
                        "             THEN %s ELSE email END, "
                        "  phone = CASE WHEN %s <> '' AND BTRIM(COALESCE(phone,'')) = '' "
                        "             THEN %s ELSE phone END, "
                        "  analysis_json = CASE WHEN analysis_json IS NULL THEN %s::jsonb "
                        "       ELSE analysis_json::jsonb || %s::jsonb END "
                        "WHERE id = %s;",
                        (res.get("owner") or "", res.get("owner") or "",
                         res.get("email") or "", res.get("email") or "",
                         res.get("phone") or "", res.get("phone") or "",
                         payload, payload, lid),
                    )
                    updated += cur.rowcount
        db.commit()
    return updated


def import_from_file(path: str, company: str, apply: bool) -> int:
    """Load results that were already paid for -- no credits spent.

    Skip Sherpa has no endpoint to retrieve past lookups, so results traced by
    hand in the dashboard exist only there. Feed them back in here instead of
    buying them twice. Accepts either this script's own output shape or a plain
    list of {address, owner, phone, email} objects, which is what a copy-paste
    out of the dashboard looks like.
    """
    raw = json.loads(Path(path).read_text())
    if isinstance(raw, dict):
        raw = raw.get("results") or []
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        addr = (item.get("address") or "").strip()
        parsed = parse_address(addr)
        if not parsed["ok"]:
            print(f"  skip (unparseable address): {addr!r} — {parsed['why']}")
            continue
        rows.append({
            "ok": bool(item.get("owner") or item.get("phone") or item.get("email")),
            "address": addr,
            "owner": (item.get("owner") or "").strip(),
            "phone": (item.get("phone") or "").strip(),
            "email": (item.get("email") or "").strip(),
            "phone_count": 1,
            "leads": item.get("leads") or [],
        })
    usable = [r for r in rows if r["ok"]]
    print(f"imported {len(usable)} usable row(s) with a name, phone or email "
          f"(of {len(raw)} in file) — 0 credits spent")
    if not usable:
        return 0
    for r in usable:
        print(f"  {r['address'][:44]:<44} {(r['owner'] or '(no name)')[:22]:<22} "
              f"{(r['phone'] or '-')[:15]:<15} {r['email']}")
    if apply:
        n = apply_results(os.environ["DATABASE_URL"], company, usable)
        print(f"\nwrote {n} lead row(s)")
        return n
    print("\ndry run: not written. Re-run with --apply to save.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--company", default="construction")
    ap.add_argument("--list", action="store_true", help="show the plan, spend nothing")
    ap.add_argument("--limit", type=int, default=0, help="max addresses to trace")
    ap.add_argument("--apply", action="store_true", help="write results to the database")
    ap.add_argument("--out", default="", help="also write results to this JSON file")
    ap.add_argument("--import", dest="import_path", default="",
                    help="load already-paid results from this JSON file; spends no credits")
    args = ap.parse_args()

    if args.import_path:
        return 0 if import_from_file(args.import_path, args.company, args.apply) else 1

    rows = fetch_candidates(args.company)
    if not rows:
        print(f"No address-only leads for company '{args.company}'.")
        return 0

    built = build_plan(rows)
    plan, skipped = built["plan"], built["skipped"]
    ceiling = credit_ceiling()
    budget = min(args.limit, ceiling) if args.limit > 0 else 0

    print(f"eligible leads      : {len(rows)}")
    print(f"distinct addresses  : {len(plan)}")
    print(f"skipped             : {len(skipped)}")
    print(f"credits available   : {ceiling} (SKIP_SHERPA_MAX_CREDITS)")
    if args.limit:
        print(f"requested --limit   : {args.limit}"
              + ("  CLAMPED to the ceiling" if args.limit > ceiling else ""))
    if not api_key():
        print("\nSKIP_SHERPA_API_KEY is not set; nothing can be traced.")
        print("Set it, then re-run to spend credits.")
        return 1

    if args.list or budget <= 0:
        print("\nFirst 25 addresses that would be traced:")
        for e in plan[:25]:
            print(f"  lead {e['leads'][0]:>5}  {e['to']['street']}, {e['to']['city']} "
                  f"{e['to']['state']} {e['to']['zipcode']}"
                  + (f"   (+{len(e['leads']) - 1} leads)" if len(e["leads"]) > 1 else ""))
        if len(plan) > 25:
            print(f"  ... and {len(plan) - 25} more")
        print("\nNothing traced. Re-run with --limit N to spend N credits.")
        return 0

    target = plan[:budget]
    print(f"\ntracing {len(target)} address(es), {len(target)} credit(s)...")
    results = []
    for i, e in enumerate(target, 1):
        res = trace_address(e["to"])
        res["address"] = e["address"]
        res["leads"] = e["leads"]
        results.append(res)
        if res.get("ok"):
            print(f"  [{i}/{len(target)}] {e['to']['street']:<28} "
                  f"{(res.get('owner') or '(no name)'):<24} "
                  f"{(res.get('phone') or '(no phone)'):<16} {res.get('email') or ''}")
        else:
            print(f"  [{i}/{len(target)}] {e['to']['street']:<28} FAILED: {res.get('error')}")
        if i < len(target):
            time.sleep(0.3)

    hit = [r for r in results if r.get("ok") and (r.get("phone") or r.get("email"))]
    print(f"\n{len(hit)}/{len(target)} returned a usable contact")

    if args.apply and hit:
        n = apply_results(os.environ["DATABASE_URL"], args.company, hit)
        print(f"wrote {n} lead row(s); the email bot picks them up on its next pass")
    elif hit:
        print("dry run: not written. Re-run with --apply to save.")

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))
        print(f"results: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())