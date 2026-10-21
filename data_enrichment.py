"""Consolidated owner/contact enrichment for leads that have an address but no contact.

WHY THIS EXISTS
---------------
An address without a contact is the single most common shape of an unusable
lead: Thumbtack hands over the job, the location and the description, and no
phone or email, because the platform withholds customer contact details from
pros by policy. Nothing downstream can act on that row. The same shape arrives
from permit feeds, which give a property address and an applicant name but
often no way to reach anyone.

The lookups needed to fix it already exist in this repo but were spread across
skiptrace_service (owner of record) and skip_sherpa_service (phone and email).
This is the one entry point that runs them in cost order, caches what it
learns, and writes back conservatively.

COST ORDER, WHICH IS THE WHOLE POINT
------------------------------------
1. skiptrace_service.lookup -- Hampton Roads public parcel data first, needs no
   paid key, and is cache-first in both directions: a cached hit is a table
   read, and so is a cached MISS, so an address already known to be empty is
   never re-spent on. This is the free tier and it supplies the owner name.
2. skip_sherpa_service.trace -- costs one credit per address, so it runs last
   and only when a cheaper step produced no phone or email. It is refused
   without a full ZIP: Skip Sherpa keys on the complete address, and a
   city-level guess spends the credit on the wrong door.

PDL used to sit between these two. Its account is not in use (every lookup
came back a failure), so as of 2026-10-21 the chain is free-then-funded with
nothing in between.

WHAT IT WILL NOT DO
-------------------
It does not fabricate contact details. A public parcel record carries a NAME
and no phone or email, and that limit is respected: a lead that gets an owner
name and nothing else is left with an owner name and nothing else, and the
run says so, rather than being written a plausible-looking phone number.

Conservative writes, by rule:
  - never overwrite a non-placeholder email, phone or name
  - never write a value the provider flagged as unusable
  - is_placeholder_name() gates every name, which is what put 126 rows named
    "Virginia Beach" in the database once already
  - a real customer-typed contact always beats a skipped-record match

Usage
-----
    python3 data_enrichment.py --dry-run
    python3 data_enrichment.py --commit --limit 25
    python3 data_enrichment.py --address "742 Elm St, Chesapeake, VA 23320"
    python3 data_enrichment.py --source thumbtack --dry-run

    DATABASE_URL must point at the leads database. --address does not need it.
"""

import argparse
import os
import re
import sys

# A python.org/framework build on macOS can ship with no CA bundle at all:
# ssl.get_default_verify_paths() returns (None, None), so every HTTPS call
# fails with CERTIFICATE_VERIFY_FAILED even though the machine trusts the site
# (curl does, because it reads the system keychain). Pointing at the system
# bundle fixes it. Set before any network module builds a context. No effect on
# the container, which has its own ca-certificates.
if not os.environ.get("SSL_CERT_FILE") and os.path.exists("/etc/ssl/cert.pem"):
    os.environ["SSL_CERT_FILE"] = "/etc/ssl/cert.pem"

import psycopg
from psycopg.rows import dict_row

import skiptrace_service
import skip_sherpa_service

PLACEHOLDER_EMAIL_SUFFIX = "@lead.local"
DEAD_STATUSES = ("cancelled", "canceled", "closed", "lost", "won", "archived")


def is_blank(value) -> bool:
    return not str(value or "").strip()


def is_placeholder_email(email) -> bool:
    return is_blank(email) or str(email).strip().lower().endswith(PLACEHOLDER_EMAIL_SUFFIX)


def classify_reachable(lead) -> bool:
    """True when this lead has at least one usable channel already.

    Enriching a lead that can already be reached spends a provider call to add
    nothing, which is the fastest way to burn a monthly budget.
    """
    return not (is_placeholder_email(lead.get("email")) and is_blank(lead.get("phone")))


def providers() -> dict:
    return {
        "skiptrace": True,
        "skip_sherpa": skip_sherpa_service.configured(),
    }


def lookup_address(db, raw_address: str, cur) -> dict:
    """Run the waterfall for one address. Never raises; returns a reason instead.

    The returned dict always has "name", "email", "phone" (possibly empty) and
    "steps", the ordered record of what each provider did, so a run can explain
    itself instead of just printing a count.
    """
    parsed = skiptrace_service.parse_address(raw_address)
    out = {"name": "", "email": "", "phone": "", "steps": [], "why": ""}
    if not parsed.get("ok"):
        out["why"] = parsed.get("why") or "unparseable address"
        out["steps"].append(f"parse: {out['why']}")
        return out
    street = parsed.get("street", "")
    city = parsed.get("city", "")
    state = parsed.get("state", "")
    # parse_address returns "zipcode". This used to read "zip"/"zip_code", so
    # the ZIP was ALWAYS empty here and Skip Sherpa was called without one --
    # which either misses outright or, worse, matches a neighbour. Both cost a
    # credit, so this key alone was a paying bug.
    zipcode = parsed.get("zipcode", "")

    try:
        with db.cursor() as cur:
            trace = skiptrace_service.lookup(
                cur, street, city, state, zipcode, requested_by="data_enrichment"
            )
        owner = str((trace or {}).get("owner_of_record") or "").strip()
        cached = bool((trace or {}).get("_from_cache"))
        if owner and not skiptrace_service.is_placeholder_name(owner):
            out["name"] = owner
            out["steps"].append(f"skiptrace: {owner}{' (cached)' if cached else ''}")
        else:
            out["steps"].append("skiptrace: no usable owner")
    except Exception as exc:
        out["steps"].append(f"skiptrace: error {str(exc)[:90]}")

    if (is_placeholder_email(out["email"]) or not out["phone"]) and skip_sherpa_service.configured():
        if not zipcode:
            # Free, and it stands between us and a billed wrong-door match.
            import zip_lookup
            try:
                zipcode = str(zip_lookup.lookup_zip(street, city, state).get("zipcode") or "")
            except Exception:
                zipcode = ""
        if not zipcode:
            out["steps"].append("skip_sherpa: skipped (no ZIP — never bill a guess)")
        else:
            try:
                trace = skip_sherpa_service.trace(street, city, state, zipcode)
            except Exception as exc:
                trace = {"ok": False, "found": False, "message": str(exc)[:120]}
                out["steps"].append("skip_sherpa: error")
            if trace.get("found"):
                emails = trace.get("emails") or []
                preferred = str(trace.get("preferred_phone") or trace.get("raw_phone") or "").strip()
                if is_placeholder_email(out["email"]) and emails:
                    out["email"] = str(emails[0])
                if not out["phone"] and preferred:
                    out["phone"] = preferred
                out["steps"].append("skip_sherpa: hit")
            else:
                out["steps"].append(f"skip_sherpa: no owner ({(trace.get('message') or '')[:60]})")

    return out


def merge(lead, found) -> dict:
    """Combine what the lead already has with what the waterfall found.

    Existing real values always win. A name is only replaced when the current
    one is a placeholder, so a permit applicant name is never swapped for a
    parcel owner of record.
    """
    cur_name = str(lead.get("name") or "").strip()
    new_name = cur_name
    if found["name"] and (not cur_name or skiptrace_service.is_placeholder_name(cur_name)):
        new_name = found["name"]

    new_email = str(lead.get("email") or "")
    if is_placeholder_email(new_email) and found["email"]:
        new_email = found["email"]

    new_phone = str(lead.get("phone") or "").strip()
    if not new_phone and found["phone"]:
        new_phone = str(found["phone"]).strip()

    changed = (new_name, new_email, new_phone) != (
        cur_name,
        str(lead.get("email") or ""),
        str(lead.get("phone") or "").strip(),
    )
    return {
        "name": new_name,
        "email": new_email,
        "phone": new_phone,
        "changed": changed,
        "gained_name": bool(new_name and new_name != cur_name),
        "gained_contact": bool(
            (new_email and not is_placeholder_email(new_email)) or new_phone
        )
        and not classify_reachable(lead),
    }


def select_candidates(db, source: str, limit: int) -> list:
    sql = (
        "SELECT id, name, email, phone, address, source, status FROM leads "
        "WHERE coalesce(address, '') <> '' "
    )
    params = []
    if source:
        sql += "AND lower(coalesce(source, '')) = lower(%s) "
        params.append(source)
    sql += (
        "AND lower(coalesce(status, 'new')) NOT IN ("
        + ",".join(["%s"] * len(DEAD_STATUSES))
        + ") "
    )
    params.extend(DEAD_STATUSES)
    sql += "ORDER BY id LIMIT %s"
    params.append(limit)
    with db.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def report(candidates, db, commit: bool) -> int:
    stats = {"examined": 0, "skipped_reachable": 0, "no_address": 0, "enriched": 0,
             "name_only": 0, "unchanged": 0, "written": 0}
    lines = []
    for lead in candidates:
        stats["examined"] += 1
        if classify_reachable(lead):
            stats["skipped_reachable"] += 1
            continue
        found = lookup_address(db, lead.get("address"), None)
        merged = merge(lead, found)
        if not merged["changed"]:
            stats["unchanged"] += 1
            lines.append((lead["id"], "unchanged", lead.get("address", "")[:38], "; ".join(found["steps"])))
            continue
        if merged["gained_contact"]:
            stats["enriched"] += 1
            kind = "contact+name"
        elif merged["gained_name"]:
            stats["name_only"] += 1
            kind = "name only"
        else:
            stats["unchanged"] += 1
            kind = "no gain"
        lines.append((lead["id"], kind, lead.get("address", "")[:38], "; ".join(found["steps"])))
        if commit:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE leads SET name = %s, email = %s, phone = %s WHERE id = %s;",
                    (merged["name"][:255], merged["email"][:255], merged["phone"][:50], lead["id"]),
                )
            db.commit()
            stats["written"] += 1

    print("\n%-6s %-14s %-40s %s" % ("lead", "result", "address", "steps"))
    print("-" * 110)
    for lead_id, kind, addr, steps in lines:
        print("%-6s %-14s %-40s %s" % (lead_id, kind, addr, steps[:60]))
    print("\n=== summary ===")
    for k, v in stats.items():
        print("  %-18s %d" % (k, v))
    print("\nproviders: %s" % providers())
    if not commit:
        print("dry run: nothing written. Re-run with --commit to apply.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Owner/contact enrichment for address-only leads.")
    ap.add_argument("--address", help="look up one address and print the result; needs no database")
    ap.add_argument("--source", default="", help="only leads from this source, e.g. thumbtack")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--commit", action="store_true", help="write results to the leads table")
    ap.add_argument("--dry-run", action="store_true", help="report only (the default)")
    args = ap.parse_args()

    if args.address:
        db_url = os.getenv("DATABASE_URL", "")
        if not db_url:
            print("--address needs DATABASE_URL for the skip-trace cache", file=sys.stderr)
            return 2
        with psycopg.connect(db_url, row_factory=dict_row) as db:
            found = lookup_address(db, args.address, None)
        print("address: %s" % args.address)
        print("name:    %s" % (found["name"] or "-"))
        print("email:   %s" % (found["email"] or "-"))
        print("phone:   %s" % (found["phone"] or "-"))
        print("steps:   %s" % "; ".join(found["steps"]))
        return 0

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    commit = args.commit and not args.dry_run
    with psycopg.connect(db_url, row_factory=dict_row) as db:
        candidates = select_candidates(db, args.source, args.limit)
        if not candidates:
            print("no candidate leads with an address")
            return 0
        print("examining %d lead(s), limit=%d source=%s"
              % (len(candidates), args.limit, args.source or "all"))
        return report(candidates, db, commit)


if __name__ == "__main__":
    sys.exit(main())