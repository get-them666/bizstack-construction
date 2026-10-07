"""Backfill owner contact info for address-only leads (permit_finder, etc.).

Leads imported from permit feeds carry only a property address (the permit
record has no owner name/email/phone). We reverse-enrich the address into
owner contact info via the PeopleDataLabs (PDL) Person Search API so the lead
becomes reachable by the normal email + voice sweeps.

Only runs when PDL_API_KEY is set. Each successfully-enriched lead is stamped
(enriched_at in analysis_json) so we never re-bill for the same address.
"""

import json
import os
import re
import time

import psycopg
from psycopg.rows import dict_row


def pdl_enabled() -> bool:
    return bool(os.getenv("PDL_API_KEY"))


# Populated by enrich_pending_leads(dry_run=True) so a route can return the
# hit list and the projected spend instead of a bare integer.
LAST_RUN: dict = {}


def _digits(v) -> str:
    if not v:
        return ""
    return re.sub(r"\D", "", str(v))


def _pick_email(candidates) -> str:
    if not candidates:
        return ""
    if isinstance(candidates, str):
        return candidates.strip()
    for c in candidates if isinstance(candidates, list) else []:
        if isinstance(c, dict):
            e = (c.get("address") or "").strip()
        else:
            e = str(c).strip()
        if e and not e.lower().endswith((".local", "+lead")):
            return e
    return ""


def _pick_phone(candidates) -> str:
    if not candidates:
        return ""
    if isinstance(candidates, str):
        return candidates.strip()
    for c in candidates if isinstance(candidates, list) else []:
        if isinstance(c, dict):
            p = (c.get("number") or c.get("display") or "").strip()
        else:
            p = str(c).strip()
        if 10 <= len(_digits(p)) <= 15:
            return p
    return ""


def _address_from_lead(row: dict) -> str:
    addr = (row.get("address") or "").strip()
    if addr:
        return addr
    try:
        meta = json.loads(row.get("analysis_json") or "{}")
    except (TypeError, ValueError):
        meta = {}
    addr = (meta.get("address") or "").strip()
    city = (meta.get("city") or "").strip()
    if addr and city and "," not in addr:
        addr = f"{addr}, {city}"
    return addr


def _enrich_address(address: str) -> dict:
    if not pdl_enabled():
        return {}
    if not address:
        return {}
    try:
        from peopledatalabs import PDLPY
    except ImportError:
        print("[pdl] peopledatalabs package not installed", flush=True)
        return {}
    sql = (
        'SELECT * FROM person '
        f"WHERE location_street_address LIKE '%{address}%' "
        'AND emails IS NOT NULL'
    )
    try:
        client = PDLPY(api_key=os.getenv("PDL_API_KEY", ""))
        resp = client.person.search(sql=sql, size=1, pretty=False)
        data = resp.json() if hasattr(resp, "json") else resp
    except Exception as e:
        print(f"[pdl] lookup failed for {address!r}: {e}", flush=True)
        return {}
    if not data.get("total"):
        return {}
    rec = data["data"][0]
    email = _pick_email(rec.get("emails"))
    phone = _pick_phone(rec.get("phone_numbers"))
    full_name = ""
    fn = (rec.get("first_name") or "").strip()
    ln = (rec.get("last_name") or "").strip()
    if fn and ln:
        full_name = f"{fn} {ln}"
    elif rec.get("full_name"):
        full_name = str(rec["full_name"]).strip()
    if not (email or phone):
        return {}
    return {
        "name": full_name,
        "email": email,
        "phone": phone,
        "display_name": (rec.get("display_name") or "").strip(),
    }


def _enrich_name(full_name: str, market: str) -> dict:
    """PDL person search by name + market, for leads that have no street address.

    STR hosts are the case reverse-address cannot cover: Airbnb obfuscates the property
    address, so `_enrich_address` has nothing to key on. Name + market is what is left,
    and it is deliberately stricter than the address path because a name is far less
    identifying than an address — hence the residence/business-location filter and the
    insistence on an exact `total == 1`. A wrong person here means texting a stranger.
    """
    if not pdl_enabled() or not full_name or not market:
        return {}
    try:
        from peopledatalabs import PDLPY
    except ImportError:
        print("[pdl] peopledatalabs package not installed", flush=True)
        return {}
    escaped_name = full_name.replace('"', "")
    escaped_market = market.replace('"', "")
    sql = (
        f'datasets="person" AND name.full_name="{escaped_name}" AND '
        f'location.city="{escaped_market}" AND '
        f'(location.location_type="Residence" OR location.location_type="Business")'
    )
    try:
        client = PDLPY(api_key=os.getenv("PDL_API_KEY", ""))
        resp = client.person.search(sql=sql, size=5, pretty=False)
        data = resp.json() if hasattr(resp, "json") else resp
    except Exception as e:
        print(f"[pdl] name lookup failed for {full_name!r} in {market!r}: {e}", flush=True)
        return {}
    if not data.get("total"):
        return {}
    # An ambiguous name in a tourism market is common. Refuse rather than guess.
    if data["total"] != 1:
        print(f"[pdl] {full_name!r} in {market!r} matched {data['total']} people, "
              f"skipping as ambiguous", flush=True)
        return {}
    rec = data["data"][0]
    email = _pick_email(rec.get("emails"))
    phone = _pick_phone(rec.get("phone_numbers"))
    if not (email or phone):
        return {}
    return {"name": full_name, "email": email, "phone": phone,
            "display_name": (rec.get("display_name") or "").strip()}


def enrich_str_leads(limit: int = 15) -> int:
    """Backfill contact for str_seed rows: address first, then host name + market.

    Separate from enrich_pending_leads() because that one is hard-scoped to
    source='permit_finder' and reverse-address only.
    """
    if not pdl_enabled():
        return 0
    try:
        db = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    except Exception as e:
        print(f"📡[pdl] db failure: {e}")
        return 0
    updated = 0
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, name, email, phone, address, analysis_json FROM leads "
                "WHERE source = 'str_seed' "
                "AND COALESCE(phone, '') = '' "
                "AND (email IS NULL OR LOWER(email) LIKE '%@lead.local') "
                "AND (analysis_json::text NOT LIKE '%\"enriched\"%') "
                "ORDER BY id DESC LIMIT %s;",
                (limit,),
            )
            rows = cur.fetchall()
        for r in rows:
            address = _address_from_lead(r)
            found = _enrich_address(address) if address else {}
            how = "address"
            if not found:
                try:
                    meta = json.loads(r.get("analysis_json") or "{}")
                except (TypeError, ValueError):
                    meta = {}
                host = (meta.get("host_name") or "").strip()
                market = (meta.get("market") or "").strip()
                if host and market:
                    found = _enrich_name(host, market)
                    how = "name+market"
            if not found:
                continue
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE leads SET "
                    "name = CASE WHEN %s <> '' THEN %s ELSE name END, "
                    "email = CASE WHEN %s <> '' THEN %s ELSE email END, "
                    "phone = CASE WHEN %s <> '' THEN %s ELSE phone END, "
                    "analysis_json = CASE WHEN analysis_json IS NULL THEN %s "
                    "ELSE analysis_json::jsonb || %s::jsonb END "
                    "WHERE id = %s;",
                    (found.get("name") or "", found.get("name") or "",
                     found.get("email") or "", found.get("email") or "",
                     found.get("phone") or "", found.get("phone") or "",
                     json.dumps({"enriched": True, "enriched_via": how}),
                     json.dumps({"enriched": True, "enriched_via": how}),
                     r["id"]),
                )
            db.commit()
            updated += 1
            print(
                f"📡[pdl] enriched str_seed lead {r['id']} via {how} -> "
                f"{found.get('email') or 'no-email'} / {found.get('phone') or 'no-phone'}",
                flush=True,
            )
            time.sleep(0.2)
    finally:
        db.close()
    return updated


def enrich_pending_leads(limit: int = 15, dry_run: bool = False) -> int:
    """Backfill contact info for address-only leads (permit_finder, etc.).

    Set dry_run=True to spend nothing and see what WOULD be found. Each lookup
    is a billable PDL record, so the hit/miss ratio and the per-address cost
    should be measured on a small batch before committing to a full pass.
    """
    if not pdl_enabled():
        return 0
    try:
        db = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    except Exception as e:
        print(f"📡[pdl] db failure: {e}")
        return 0
    updated = 0
    hits: list = []
    misses: list = []
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, name, email, phone, address, analysis_json FROM leads "
                "WHERE company = 'construction' "
                "AND source = 'permit_finder' "
                "AND COALESCE(phone, '') = '' "
                "AND (email IS NULL OR LOWER(email) LIKE '%@lead.local') "
                "AND (analysis_json::text NOT LIKE '%\"enriched\"%') "
                "ORDER BY id DESC LIMIT %s;",
                (limit,),
            )
            rows = cur.fetchall()
        for r in rows:
            address = _address_from_lead(r)
            if not address:
                continue
            found = _enrich_address(address)
            if not found:
                misses.append({"lead_id": r["id"], "address": address})
                continue
            if dry_run:
                # Report without writing. The UPDATE below is skipped entirely
                # so a dry run cannot be mistaken for a real enrichment.
                hits.append({
                    "lead_id": r["id"], "address": address,
                    "name": found.get("name") or "",
                    "email": found.get("email") or "",
                    "phone": found.get("phone") or "",
                })
                print(f"🔎[pdl:dry] lead {r['id']} {address!r} -> "
                      f"{found.get('email') or 'no-email'} / {found.get('phone') or 'no-phone'}",
                      flush=True)
                time.sleep(0.2)
                continue
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE leads SET "
                    "name = CASE WHEN %s <> '' THEN %s ELSE name END, "
                    "email = CASE WHEN %s <> '' THEN %s ELSE email END, "
                    "phone = CASE WHEN %s <> '' THEN %s ELSE phone END, "
                    "analysis_json = CASE WHEN analysis_json IS NULL THEN %s "
                    "ELSE analysis_json::jsonb || %s::jsonb END "
                    "WHERE id = %s;",
                    (found.get("name") or "", found.get("name") or "",
                     found.get("email") or "", found.get("email") or "",
                     found.get("phone") or "", found.get("phone") or "",
                     json.dumps({"enriched": True}),
                     json.dumps({"enriched": True}),
                     r["id"]),
                )
            db.commit()
            updated += 1
            print(
                f"📡[pdl] enriched lead {r['id']} {address!r} -> "
                f"{found.get('email') or 'no-email'} / {found.get('phone') or 'no-phone'}",
                flush=True,
            )
            time.sleep(0.2)
    finally:
        db.close()

    if dry_run:
        # Deliberately NOT returning `updated`: a caller checking
        # `if enrich_pending_leads(...)` would read "0 enriched" when the truth
        # is "0 written because this was a dry run". Conflating those is how a
        # dry run gets mistaken for a real, silently-empty result.
        LAST_RUN.clear()
        LAST_RUN.update({
            "dry_run": True,
            "examined": len(hits) + len(misses),
            "hits": len(hits),
            "misses": len(misses),
            "found": hits,
            "no_match": misses,
            # PDL Person Search bills per record returned.
            "cost_usd": round((len(hits) + len(misses)) * 0.20, 2),
        })
        return 0
    return updated