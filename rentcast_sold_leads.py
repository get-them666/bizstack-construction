"""Recently-sold property leads from RentCast.

The signal
----------
A home that changed hands in the last 90 days is the strongest renovation lead
available and it costs nothing to look at: a new owner with a mortgage has
equity, a reason to spend, and a 12-month window of "should have fixed that
before it got worse." The existing permit feed only tells you someone pulled a
permit -- by then the decision is made and often the job is already spoken for.
A sale record tells you the decision is *ahead* of you.

The economics are better than they look
---------------------------------------
`limit` on /properties goes to 500 records **per request**. One call can return
500 recently-sold properties. The cost driver is the number of distinct sweeps,
not the number of records, so the budget control here is a ceiling on sweeps and
a per-sweep cache -- not a per-lead cost. This is the opposite of the Skip
Sherpa path, where every address is a separate billable lookup.

What RentCast cannot do, and what this does instead
---------------------------------------------------
RentCast returns no email and no phone. The owner block is name, type and
mailing address, and nothing else. So this module deliberately does not pretend
otherwise: it produces name + property address + owner mailing address, which
is enough for a direct-mail canvas (mail_letters.py reads the address column).
Getting an email or a phone means calling the EXISTING skip_sherpa_service, one
door at a time, by explicit request -- see enrich_contact().

Fallback
--------
Every entry point degrades to "no leads" rather than raising. With no API key, an
exhausted budget, or a RentCast outage this returns an empty result and the
existing permit pipeline is unaffected. Nothing here is wired into a schedule,
so the 50-call month cannot be spent while nobody is watching.
"""

import hashlib
import json
import os
import time

import property_service as ps

SOLD_TABLE = "sold_lead_candidates"
DEFAULT_DAYS = 90
DEFAULT_LIMIT = 500  # the documented maximum; one call, one whole sweep

# Cached sweeps are stale-able: a 90-day window shifts every day, so a sweep
# cached last week is missing the last week's sales. Refreshing costs a call, so
# the TTL is a deliberate tradeoff (default 24h) rather than "always fresh".
SWEEP_TTL_HOURS = int(os.getenv("RENTCAST_SWEEP_TTL_HOURS", "24") or 24)
_sweep_cache: dict = {}

# Residential only. Land, apartments and 5+ unit commercial buildings are not
# renovation leads for a general contractor and would eat the result window.
RESIDENTIAL_TYPES = ("Single Family", "Condo", "Townhouse", "Manufactured")


def is_configured() -> bool:
    return ps.is_configured()


def _env_float(name, default):
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def min_sale_price() -> float:
    """Below this a buyer has no equity to renovate with."""
    return _env_float("RENTCAST_SOLD_MIN_PRICE", 150000)


def _sweep_signature(area: dict, days: int, limit: int, types) -> str:
    """Cache key for a sweep. The window end date is deliberately NOT in it.

    Two sweeps a day apart ask the same question, so they should share a cache
    entry; the TTL decides when to re-ask. Baking a date into the key would make
    every run a fresh call and quietly spend the month.
    """
    payload = {
        "area": {k: v for k, v in sorted(area.items()) if v},
        "days": days, "limit": limit, "types": sorted(types),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]


def _ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS {SOLD_TABLE} (
                rentcast_id VARCHAR(255) PRIMARY KEY,
                address VARCHAR(255),
                city VARCHAR(120),
                state VARCHAR(5),
                postal_code VARCHAR(10),
                owner_name VARCHAR(255),
                mailing_address VARCHAR(255),
                sale_price NUMERIC(14,2),
                sale_date VARCHAR(40),
                square_footage NUMERIC(12,2),
                year_built NUMERIC(8,0),
                property_type VARCHAR(60),
                owner_occupied BOOLEAN,
                score NUMERIC(6,2),
                reasons TEXT,
                found_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );"""
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS idx_sold_leads_found "
            f"ON {SOLD_TABLE} (found_at DESC);"
        )
    conn.commit()


def to_candidate(prop: dict) -> dict:
    """Normalize one RentCast property record into a lead candidate.

    Returns None for anything that is not a usable door. The filters are here
    rather than at the call site because they are the difference between a
    canvas list a salesperson can work and 500 rows nobody reads.
    """
    if not isinstance(prop, dict):
        return None
    address = (prop.get("formattedAddress") or "").strip()
    ptype = (prop.get("propertyType") or "").strip()
    owner = prop.get("owner") or {}
    names = owner.get("names") or []
    owner_name = (names[0] if names else "").strip()

    sale_price = prop.get("lastSalePrice")
    try:
        sale_price = float(sale_price) if sale_price not in (None, "") else 0.0
    except (TypeError, ValueError):
        sale_price = 0.0

    reasons = []
    # Hard filters. Each one is a reason we would have wasted a door on.
    if not address:
        return None
    if ptype and ptype not in RESIDENTIAL_TYPES:
        return None
    if not owner_name:
        return None  # nobody to address the letter to
    if sale_price and sale_price < min_sale_price():
        return None  # no equity to renovate

    # Soft signals, scored rather than filtered.
    if prop.get("ownerOccupied"):
        reasons.append("owner-occupied")
    if sale_price:
        reasons.append(f"sold ${sale_price:,.0f}")
    year = prop.get("yearBuilt")
    try:
        year = int(year) if year not in (None, "") else 0
    except (TypeError, ValueError):
        year = 0
    if year and year <= 1950:
        reasons.append(f"built {year} — likely original systems")
    elif year and year >= 2010:
        reasons.append(f"built {year} — new-build warranty work")

    mailing = owner.get("mailingAddress") or {}
    mailing_line = (mailing.get("formattedAddress") or "").strip()
    if mailing_line and mailing_line.lower() != address.lower():
        reasons.append("owner mailing address differs from property")

    # Rank: owner-occupied buyers are the best odds, and equity scales the job.
    score = 40.0
    if prop.get("ownerOccupied"):
        score += 25
    score += min(sale_price / 20000.0, 25.0)
    if year and year <= 1950:
        score += 10

    try:
        sqft = float(prop.get("squareFootage") or 0)
    except (TypeError, ValueError):
        sqft = 0.0

    return {
        "rentcast_id": prop.get("id") or address,
        "address": address,
        "city": (prop.get("city") or "").strip(),
        "state": (prop.get("state") or "").strip(),
        "postal_code": (prop.get("zipCode") or "").strip(),
        "owner_name": owner_name,
        "mailing_address": mailing_line or address,
        "sale_price": sale_price,
        "sale_date": (prop.get("lastSaleDate") or "").strip(),
        "square_footage": sqft,
        "year_built": year,
        "property_type": ptype,
        "owner_occupied": bool(prop.get("ownerOccupied")),
        "score": round(score, 2),
        "reasons": reasons,
        # Explicit, not implied. Nothing downstream should assume this field
        # exists -- RentCast has no contact endpoint.
        "email": "",
        "phone": "",
    }


def find_recent_sales(area: dict, days: int = DEFAULT_DAYS, limit: int = DEFAULT_LIMIT,
                      property_types=None, force: bool = False) -> dict:
    """One call. Up to `limit` recently-sold residential properties in `area`.

    `area` takes any of the documented search shapes:
      {"zipCode": "23320"}
      {"city": "Chesapeake", "state": "VA"}
      {"address": "...", "radius": 3}
      {"latitude": ..., "longitude": ..., "radius": ...}

    Returns {"ok", "leads", "calls_spent", "cached", "reason"}.
    """
    if not is_configured():
        return {"ok": False, "leads": [], "calls_spent": 0, "cached": False,
                "reason": "RENTCAST_API_KEY is not set"}
    days = max(int(days or DEFAULT_DAYS), 1)
    limit = max(1, min(int(limit or DEFAULT_LIMIT), 500))
    types = tuple(property_types or RESIDENTIAL_TYPES)
    sig = _sweep_signature(area, days, limit, types)

    if not force:
        hit = _sweep_cache.get(sig)
        if hit and time.time() - hit.get("_stored_at", 0) < SWEEP_TTL_HOURS * 3600:
            return {"ok": True, "leads": hit["leads"], "calls_spent": 0,
                    "cached": True, "reason": ""}

    # One sweep is one call, and it is refused before the request rather than
    # after. Checking first means a spent month costs a message, not a call.
    if not ps.budget_allows(1):
        return {"ok": False, "leads": [], "calls_spent": 0, "cached": False,
                "reason": f"monthly RentCast budget spent ({ps._month_limit()} calls)"}

    params = {
        "saleDateRange": days,
        "propertyType": ",".join(types),
        "limit": limit,
    }
    params.update({k: v for k, v in (area or {}).items() if v not in (None, "")})

    reached_api = False
    try:
        data = ps._request("/properties", params)
        reached_api = True
    except ps.RentCastError as exc:
        # Set reached_api before returning: the finally block is what actually
        # charges the budget. Reporting `calls_spent: 1` here without charging
        # would claim a spend the counter does not know about -- the exact bug
        # this module exists to avoid.
        reached_api = exc.reached_api
        print(f"❌ RentCast sold sweep failed: {exc.message}")
        return {"ok": False, "leads": [], "calls_spent": 1 if exc.reached_api else 0,
                "cached": False, "reason": exc.message}
    except Exception as e:
        # Charged defensively: an unknown failure may still have been billed.
        reached_api = True
        print(f"❌ RentCast sold sweep failed: {type(e).__name__}: {e}")
        return {"ok": False, "leads": [], "calls_spent": 1, "cached": False,
                "reason": f"{type(e).__name__}: {e}"}
    finally:
        if reached_api:
            ps.charge_call(1)

    records = data if isinstance(data, list) else (data or {}).get("properties") or []
    leads = [c for c in (to_candidate(r) for r in records) if c]
    leads.sort(key=lambda c: c["score"], reverse=True)

    result = {"ok": True, "leads": leads, "calls_spent": 1, "cached": False,
              "reason": "", "records_seen": len(records)}
    _sweep_cache[sig] = {"leads": leads, "_stored_at": time.time()}
    return result


def save_candidates(conn, leads: list) -> dict:
    """Persist candidates, skipping any door already seen.

    Dedupe is on RentCast's own property id, which is stable across sweeps. The
    same house sold last month must not become a second lead next month.
    """
    _ensure_table(conn)
    added, skipped = 0, 0
    with conn.cursor() as cur:
        for lead in leads:
            rid = lead.get("rentcast_id")
            if not rid:
                continue
            cur.execute(f"SELECT 1 FROM {SOLD_TABLE} WHERE rentcast_id = %s;", (rid,))
            if cur.fetchone():
                skipped += 1
                continue
            cur.execute(
                f"""INSERT INTO {SOLD_TABLE}
                    (rentcast_id, address, city, state, postal_code, owner_name,
                     mailing_address, sale_price, sale_date, square_footage,
                     year_built, property_type, owner_occupied, score, reasons)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);""",
                (rid, lead.get("address"), lead.get("city"), lead.get("state"),
                 lead.get("postal_code"), lead.get("owner_name"),
                 lead.get("mailing_address"), lead.get("sale_price"),
                 lead.get("sale_date"), lead.get("square_footage"),
                 lead.get("year_built"), lead.get("property_type"),
                 lead.get("owner_occupied"), lead.get("score"),
                 "; ".join(lead.get("reasons") or [])),
            )
            added += 1
    conn.commit()
    return {"added": added, "skipped": skipped}


def enrich_contact(lead: dict) -> dict:
    """Get an email/phone for ONE already-chosen door, via the existing wiring.

    This is deliberately a separate call the operator makes on purpose. RentCast
    cannot supply contact details, and the provider that can (Skip Sherpa) bills
    per lookup -- so the sequence is: sweep cheaply, pick the doors worth a
    credit, then ask. Nothing in this module calls this on its own.
    """
    import skip_sherpa_service

    parts = (lead.get("address") or "").split(",")
    street = parts[0].strip()
    city = (lead.get("city") or (parts[1].strip() if len(parts) > 1 else "")).strip()
    state = (lead.get("state") or "").strip().upper()
    zipcode = (lead.get("postal_code") or "").strip()
    if not (street and city and zipcode):
        return {"ok": False, "message": "street, city and ZIP are required"}

    try:
        result = skip_sherpa_service.trace(street, city, state, zipcode)
    except skip_sherpa_service.ProviderError as exc:
        return {"ok": False, "message": exc.message, "credit_spent": exc.spendable}

    return {
        "ok": True,
        "owner": result.get("owner") or lead.get("owner_name"),
        "email": result.get("email") or "",
        "emails": result.get("emails") or [],
        "phone": result.get("preferred_phone") or result.get("raw_phone") or "",
        "owner_occupied": result.get("owner_occupied"),
    }