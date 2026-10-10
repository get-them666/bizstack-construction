"""Contact channels for an address, via People Data Labs (PDL).

The gap this fills
-----------------
skiptrace_service resolves an address to an owner NAME using free public parcel
data (HRGEO, with RentCast as fallback). Its own docstring says the consequence:
a name is not a channel. So an address-only permit lead stays unreachable --
nothing to email, nothing to call.

This module supplies the email and phone that the parcel record does not carry.

Why PDL and not the alternatives
--------------------------------
RentCast cannot do this at all. Its owner block is name, type and mailing
address; there is no email or phone on any endpoint. That is why commit c4a77ea
swapped PDL out of skiptrace_service -- an AI chasing 502s from RentCast deleted
a working contact layer as collateral, and the name-only behaviour has been live
since.

Skip Sherpa also works, and is still wired (skip_sherpa_service.py). It bills
per lookup and returns DNC flags. PDL returns consumer contact data WITHOUT any
DNC signal, which is a real difference and is surfaced per-result rather than
hidden: see `dnc_unknown` below.

Safety design
-------------
* One address at a time, on an explicit click. There is no batch path and no
  background caller, because PDL bills per person search.
* A ZIP is REQUIRED. Without one PDL falls back to a city-level match and can
  return a neighbour's contact details -- which means texting a stranger.
* Misses are cached. A dead address must not be re-billed on every retry.
* Every lookup is audited, with who asked.
"""

import json
import os
import time

SCHEMA_READY = False

TABLE_CACHE = "pdl_contact_cache"
TABLE_AUDIT = "pdl_contact_audit"


def configured() -> bool:
    return bool((os.getenv("PDL_API_KEY") or "").strip())


def ensure_schema(cur) -> None:
    """Create cache + audit tables. Idempotent, once per process."""
    global SCHEMA_READY
    if SCHEMA_READY:
        return
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_CACHE} (
            address_key VARCHAR(255) PRIMARY KEY,
            found BOOLEAN NOT NULL DEFAULT FALSE,
            payload TEXT,
            source VARCHAR(40),
            checked_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_AUDIT} (
            id SERIAL PRIMARY KEY,
            requested_by VARCHAR(255),
            address_key VARCHAR(255),
            found BOOLEAN,
            owner_name VARCHAR(255),
            email_found BOOLEAN DEFAULT FALSE,
            phone_found BOOLEAN DEFAULT FALSE,
            source VARCHAR(40),
            cached BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_pdl_audit_created "
                f"ON {TABLE_AUDIT} (created_at DESC);")
    SCHEMA_READY = True


def cache_get(cur, key: str):
    """Cached contact payload for an address, or None. Never spends quota.

    Mirrors skiptrace_service.cache_get: the `found` column is authoritative
    rather than whatever the payload happens to carry, so a row written before a
    field was added cannot disagree with itself.
    """
    cur.execute(f"SELECT found, payload, source FROM {TABLE_CACHE} WHERE address_key = %s;",
                (key,))
    row = cur.fetchone()
    if not row:
        return None
    if isinstance(row, dict):
        found, payload, source = row.get("found"), row.get("payload"), row.get("source")
    else:
        found, payload, source = row[0], row[1], row[2]
    try:
        data = json.loads(payload) if payload else {}
    except (TypeError, ValueError):
        return None
    data["found"] = bool(found)
    data["_cached"] = True
    data["_cache_source"] = source
    return data


def cache_put(cur, key: str, result: dict) -> None:
    """Cache a result, misses included."""
    payload = {k: v for k, v in result.items() if not k.startswith("_")}
    cur.execute(
        f"""INSERT INTO {TABLE_CACHE} (address_key, found, payload, source)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (address_key) DO UPDATE
             SET found = EXCLUDED.found, payload = EXCLUDED.payload,
                 source = EXCLUDED.source, checked_at = CURRENT_TIMESTAMP;""",
        (key, bool(result.get("found")), json.dumps(payload, default=str),
         result.get("source") or "pdl"),
    )


def audit(cur, requested_by: str, key: str, result: dict, cached: bool) -> None:
    """Homeowner contact details are in here, so the trail is the point."""
    cur.execute(
        f"""INSERT INTO {TABLE_AUDIT}
             (requested_by, address_key, found, owner_name, email_found,
              phone_found, source, cached)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s);""",
        (requested_by or "", key, bool(result.get("found")),
         result.get("owner_name") or "", bool(result.get("email")),
         bool(result.get("phone")), result.get("source") or "pdl", bool(cached)),
    )


def fetch_contact(address: str, owner_name: str = "") -> dict:
    """One PDL person search for one address.

    Returns a dict carrying email/phone plus `dnc_unknown: True` on a hit.
    Never raises for a miss -- a miss is an answer, not a failure.
    """
    key = (address or "").strip()
    if not key:
        return {"found": False, "source": "pdl", "message": "address is required"}
    if not configured():
        return {"found": False, "source": "pdl",
                "message": "PDL is not configured (PDL_API_KEY is not set)"}

    import enrichment

    try:
        got = enrichment._enrich_address(key)
    except Exception as exc:  # a provider fault must not 500 the page
        return {"found": False, "source": "pdl",
                "message": f"PDL lookup failed: {type(exc).__name__}: {exc}"}

    email = (got.get("email") or "").strip()
    phone = (got.get("phone") or "").strip()

    # A rejected key must never be reported as "this person has no contact
    # details". It is a credential fault that affects every address, and
    # reporting it as a miss trains the operator to believe the data is thin.
    if got.get("_auth_failed"):
        return {"found": False, "source": "pdl", "auth_failed": True,
                "status": got.get("_status"),
                "message": got.get("message", "People Data Labs rejected the API key.")}

    if not (email or phone):
        return {"found": False, "source": "pdl", "owner_name": owner_name,
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "message": "No contact found for this address."}

    return {
        "found": True,
        "source": "pdl",
        "owner_name": got.get("name") or owner_name,
        "email": email,
        "phone": phone,
        # PDL exposes no do-not-call signal. Skip Sherpa does. Without this flag
        # a caller cannot tell "cleared for outreach" from "never checked" --
        # which is exactly how a DNC-registered number ends up on a call list.
        "dnc_unknown": True,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def lookup(street: str, city: str, state: str, zip_code: str,
           owner_name: str = "", cur=None, requested_by: str = "") -> dict:
    """Cached, audited contact lookup for one address.

    Refuses without a ZIP. PDL matches on a full address string; without a ZIP it
    can match city-level and return a different person's details, and this data
    feeds emails and phone calls. Refusing costs nothing -- no call is made.
    """
    street, city = (street or "").strip(), (city or "").strip()
    state, zip_code = (state or "").strip().upper(), (zip_code or "").strip()

    if not configured():
        return {"found": False, "source": "pdl", "credit_spent": False,
                "message": "PDL is not configured (PDL_API_KEY is not set). "
                           "No lookup was made."}
    if not (street and city):
        return {"found": False, "source": "pdl", "credit_spent": False,
                "message": "street and city are required"}
    if not zip_code:
        return {"found": False, "source": "pdl", "credit_spent": False,
                "message": "A ZIP is required. Without one PDL can match at city "
                           "level and return a different person's contact details. "
                           "No lookup was made."}

    address = f"{street}, {city}, {state} {zip_code}".strip()
    import skiptrace_service
    key = skiptrace_service.normalize_address(street, city, state, zip_code)

    # Cache first. A repeat address is free, which is the whole reason the
    # enrichment queue can be re-run without re-billing.
    if cur is not None:
        try:
            ensure_schema(cur)
            hit = cache_get(cur, key)
        except Exception:
            hit = None
        if hit is not None:
            hit["owner_name"] = hit.get("owner_name") or owner_name
            return hit

    result = fetch_contact(address, owner_name=owner_name)
    result["address"] = address

    if cur is not None:
        try:
            ensure_schema(cur)
            # A credential fault is deliberately NOT cached. A miss is worth
            # caching so a dead address is not re-billed; a bad key is not, or
            # fixing it would change nothing and every address would stay
            # negative until someone cleared the table by hand.
            if not result.get("auth_failed"):
                cache_put(cur, key, result)
                audit(cur, requested_by, key, result, cached=False)
        except Exception as exc:
            print(f"⚠️ PDL cache/audit write failed: {exc}")

    return result