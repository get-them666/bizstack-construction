#!/usr/bin/env python3
"""Skip trace: address -> owner of record -> registered agent if the owner is an entity.

Production port of the local tool in ~/skipTraced/main.py, rewritten against
urllib to match this repo (nothing here needs httpx, and requirements.txt does
not carry it). Exposed as POST /api/skiptrace, admin-only.

WHY THE CACHE IS NOT OPTIONAL
RentCast's free Developer plan allows 50 calls per MONTH. The permit feeds
publish hundreds of addresses, and MEMORY.md records that 177 leads cover only
58 distinct doors, so even a modest pass would blow the quota several times
over. Every lookup is therefore cached on the normalized address forever: a
repeat lookup is a table read, not a billable call. Without this the endpoint
returns 429 within minutes of anyone using it twice.

The cache is also why an address that misses stays missing -- NOT_FOUND is
cached too, so a known-empty address is never re-spent on.

WHAT THIS RETURNS AND THE RULE THAT GOVERNS IT
This reads the assessor's public record of who owns a property. That record is
public by statute. What carries legal weight is the NEXT step: calling or
texting a residential number is TCPA territory and needs a DNC check.

The owner decided on 2026-10-02 that lead sourcing is business contacts only
("a roofer or plumber with a real business line is the lead, not a
homeowner"), enforced in copilot_ops._BLOCKED_CONTACT_DOMAINS. This module is
the deliberate exception, on the owner's explicit instruction, so every lookup
is written to skiptrace_audit with the requesting user and the resulting owner
type. Residential-owner results are returned but flagged (`is_residential`),
which makes the exception visible in the data instead of leaving it to be
discovered later. If that tradeoff is ever reversed, filter on the flag -- no
other module needs to change.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

TIMEOUT = 20

# Suffixes meaning "look this up as a company, not a person".
ENTITY_SUFFIXES = ("LLC", "L L C", "INC", "CORP", "TRUST", "HOLDINGS",
                   "PARTNERS", "LP", "LLP", "CO", "COMPANY", "ENTERPRISES")

NOT_FOUND = object()  # valid request, no such property. Not an error.

# Set by ensure_schema once the tables exist, so DDL stays out of the hot path.
_SCHEMA_READY = False

_RENTCAST_URL = "https://api.rentcast.io/v1/properties"
_OPENCORP_URL = "https://api.opencorporates.com/v0.4/companies"


class ProviderError(Exception):
    """An upstream failure worth surfacing, as distinct from 'no record found'."""

    def __init__(self, message, status=502, spendable=False):
        super().__init__(message)
        self.message = message
        self.status = status
        # True when the call consumed quota even though it failed. A 429 does;
        # a connection timeout does not. Used to decide whether to cache.
        self.spendable = spendable


_STATE_ZIP = re.compile(r"^(.*?),\s*([A-Za-z]{2})\s*(\d{5}(?:-\d{4})?)?$")


def parse_address(raw: str) -> dict:
    """'1501 VANCE CIR, Chesapeake, VA 23320' -> the four parts the APIs want.

    Lives here rather than in a caller because the string form is the only
    thing stored on a permit or property row, so anything that wants to trace
    one has to break it apart. Providers reject a malformed address with a 400,
    which is indistinguishable from a real miss unless it is parsed first.

    Returns {"ok": False, "why": ...} rather than raising, so a bad row is a
    reported reason and not a failed request.
    """
    text = str(raw or "").replace("\r\n", " ").replace("\r", " ").replace("\n", ", ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return {"ok": False, "why": "empty address"}

    # Peel a trailing "... VA 23320" off the end before splitting on commas.
    # Permit feeds sometimes emit one unbroken token ("1 A ST NORFOLK VA 23510"),
    # where comma-splitting puts the city and state inside the street and the
    # address then looks like it has no state at all.
    tail = re.search(r"\s+([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)\s*$", text)
    peeled_state = peeled_zip = ""
    if tail:
        peeled_state, peeled_zip = tail.group(1).upper(), tail.group(2)
        text = text[:tail.start()].strip(" ,")

    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return {"ok": False, "why": "empty address"}

    street = parts[0]
    city, state, zipcode = "", peeled_state, peeled_zip
    for chunk in parts[1:]:
        if re.fullmatch(r"[A-Za-z]{2}", chunk.strip()):
            state = state or chunk.strip().upper()
            continue
        found = re.search(r"\b\d{5}(?:-\d{4})?\b", chunk)
        if found:
            zipcode = zipcode or found.group(0)
            rest = chunk.replace(found.group(0), "").strip(" ,")
            # A 2-letter leftover is the state, never a city name. Without
            # this, "1501 VANCE CIR, VA 23320" parses with city="VA" and the
            # lookup goes out as "VA, VA 23320" -- a miss indistinguishable
            # from the property not being in the database.
            if re.fullmatch(r"[A-Za-z]{2}", rest):
                state = state or rest.upper()
            else:
                # "Chesapeake VA" -- city with the state still glued on.
                glued = re.fullmatch(r"(.+?)\s+([A-Za-z]{2})", rest)
                if glued:
                    if not city:
                        city = glued.group(1).strip()
                    state = state or glued.group(2).upper()
                elif rest and not city:
                    city = rest
            continue
        if not city:
            city = chunk

    # Last resort: the state/ZIP may have arrived glued to the last chunk.
    if not state:
        m = _STATE_ZIP.search(text)
        if m:
            state = m.group(2).upper()
            zipcode = zipcode or (m.group(3) or "")
    if not state:
        return {"ok": False, "why": "no state"}
    if not zipcode:
        return {"ok": False, "why": "no ZIP"}
    return {"ok": True, "street": street, "city": city.upper(), "state": state,
            "zipcode": zipcode}


def normalize_address(street: str, city: str, state: str, zip_code: str = "") -> str:
    """Collapse an address to a stable cache key.

    '151 battle green  dr.' and '151 Battle Green Dr' are one house, and the
    cache must not treat them as two billable lookups.
    """
    def squash(value):
        value = (value or "").strip().lower()
        value = re.sub(r"[.,#]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    parts = [squash(street), squash(city), squash(state)]
    if squash(zip_code):
        parts.append(squash(zip_code))
    return " | ".join(p for p in parts if p)


def looks_like_entity(name: str) -> bool:
    """True when the owner is a company rather than a person.

    Guarded on the string itself: .upper() on None is a TypeError.
    """
    if not name:
        return False
    upper = name.upper()
    return any(s in upper for s in ENTITY_SUFFIXES)


def _get(url: str, params: dict, headers: dict = None, timeout: int = TIMEOUT):
    """GET returning (status, parsed_body_or_None).

    Returns the status instead of raising, because for these providers a 400
    or 404 is a legitimate ANSWER -- 'I have no record of that address' -- and
    must not be confused with a failure. Raising on them is what made the
    original local version throw on most of Virginia.
    """
    full = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        status = exc.code
    except Exception as exc:
        # Connection reset, DNS, timeout. No quota was spent.
        raise ProviderError(f"network: {type(exc).__name__}: {exc}", 502, spendable=False)

    body = None
    if raw.strip():
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
    return status, body


# --- LAYER 1: RentCast --------------------------------------------------------
# Field names verified against RentCast's Property Data docs. The owner block is
#   "owner": {"names": [...], "type": "Individual", "mailingAddress": {...}}
# `names` is a LIST and the field is `type`, not `name`/`ownerName`.
def fetch_rentcast(address: str):
    key = (os.getenv("RENTCAST_API_KEY") or "").strip()
    if not key:
        return None  # not configured; caller tries the next layer

    status, body = _get(
        _RENTCAST_URL,
        {"address": address, "limit": 1},
        {"X-Api-Key": key, "Accept": "application/json"},
    )

    if status == 401:
        raise ProviderError("RentCast rejected RENTCAST_API_KEY (401)", 502, spendable=False)
    if status == 429:
        raise ProviderError(
            "RentCast monthly quota exhausted (50/month on the Developer plan). "
            "Results already in the cache still work; new addresses will not.", 429, spendable=True)
    if status in (400, 404):
        return NOT_FOUND  # not in their database / unparseable. An answer, not an error.
    if status != 200:
        raise ProviderError(f"RentCast HTTP {status}", 502, spendable=True)

    results = body if isinstance(body, list) else ((body or {}).get("properties") or [])
    if not results:
        return NOT_FOUND

    prop = results[0] or {}
    owner = prop.get("owner") or {}
    names = owner.get("names") or []
    owner_name = (names[0] if names else None)
    if not owner_name:
        return NOT_FOUND

    mailing = owner.get("mailingAddress") or {}
    assessments = prop.get("taxAssessments") or {}
    latest = max(assessments) if assessments else None

    return {
        "owner_name": owner_name,
        "owner_type": owner.get("type"),
        "owner_occupied": prop.get("ownerOccupied"),
        "owner_mailing_city": mailing.get("city"),
        "parcel": prop.get("assessorID"),
        "county": prop.get("county"),
        "assessed_value": (assessments.get(latest) or {}).get("value") if latest else None,
        "year_built": prop.get("yearBuilt"),
        "square_footage": prop.get("squareFootage"),
        "property_type": prop.get("propertyType"),
        "last_sale_date": prop.get("lastSaleDate"),
        "last_sale_price": prop.get("lastSalePrice"),
        "_source": "rentcast",
    }


# --- LAYER 2: OpenCorporates (optional, entity owners only) --------------------
def fetch_registered_agent(company_name: str) -> str:
    """Resolve a company owner's registered agent. Empty string if unavailable."""
    token = (os.getenv("OPENCORPORATES_API_KEY") or "").strip()
    if not token:
        return ""

    status, body = _get(_OPENCORP_URL + "/search",
                        {"q": company_name, "api_token": token, "status": "Active"})
    if status != 200:
        return ""

    companies = ((body or {}).get("results") or {}).get("companies") or []
    if not companies:
        return ""

    company = companies[0].get("company") or {}
    jur, num = company.get("jurisdiction_code"), company.get("company_number")
    if not (jur and num):
        return ""

    status, body = _get(f"{_OPENCORP_URL}/{jur}/{num}", {"api_token": token})
    if status != 200:
        return ""

    return ((body or {}).get("results") or {}).get("company", {}).get(
        "registered_agent_name") or ""


def trace_address(street: str, city: str, state: str, zip_code: str = "") -> dict:
    """Run the waterfall. Raises ProviderError only on genuine upstream failure.

    Returns a dict with `found` either True or False. A miss is a successful
    call, not an exception.
    """
    street = (street or "").strip()
    city = (city or "").strip()
    state = (state or "").strip().upper()
    zip_code = (zip_code or "").strip()

    if not street or not city:
        raise ProviderError("street and city are required", 400)
    if len(state) != 2:
        raise ProviderError("state must be a 2-letter abbreviation", 400)

    address = f"{street}, {city}, {state} {zip_code}".strip()
    attempted = []
    record = None

    # Layer 1. Layers 2 and 3 (eStated, Regrid) are wired in the local tool but
    # need keys that are unset, so only RentCast is live here. A missing key is
    # a skip, not a failure -- the caller still gets a clean "no record".
    got = fetch_rentcast(address)

    if got is None:
        attempted.append("rentcast: RENTCAST_API_KEY not set, skipped")
    elif got is NOT_FOUND:
        attempted.append("rentcast: no record for this address")
    else:
        record = got

    if record is None:
        return {"found": False, "layers_tried": attempted,
                "address": address, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    owner = record.get("owner_name")
    is_entity = looks_like_entity(owner)
    agent = ""
    if is_entity:
        try:
            agent = fetch_registered_agent(owner)
        except Exception:
            agent = ""  # optional layer; never fail the whole call for it

    return {
        "found": True,
        "address": address,
        "owner_of_record": owner,
        "owner_is_entity": is_entity,
        # The business-contacts-only exception, made visible in the data.
        "is_residential": not is_entity,
        "registered_agent": agent or None,
        "property": record,
        "layers_tried": attempted,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# --- persistence --------------------------------------------------------------
# Self-initializing, matching the CREATE TABLE IF NOT EXISTS pattern the rest of
# this repo uses (google_oauth.py, copilot_memory.py, construction_main.py).
def ensure_schema(cur) -> None:
    """Create the two tables if absent. Idempotent, and once per process.

    CREATE TABLE IF NOT EXISTS still takes a lock and a round trip. Running it
    on every lookup would put DDL in the hot path of a request that mostly hits
    the cache and should do no I/O at all. The flag is per-process and the
    tables are shared, so a race between two workers just runs the DDL twice.
    """
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return

    cur.execute("""
        CREATE TABLE IF NOT EXISTS skiptrace_cache (
            address_key VARCHAR(255) PRIMARY KEY,
            found BOOLEAN NOT NULL DEFAULT FALSE,
            payload TEXT,
            source VARCHAR(40),
            checked_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # Every lookup is recorded with who asked. Homeowner names are in here, so
    # the trail is what makes that defensible.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS skiptrace_audit (
            id SERIAL PRIMARY KEY,
            requested_by VARCHAR(255),
            address_key VARCHAR(255),
            found BOOLEAN,
            owner_of_record VARCHAR(255),
            is_residential BOOLEAN,
            source VARCHAR(40),
            cached BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_skiptrace_audit_created "
                "ON skiptrace_audit (created_at DESC);")

    _SCHEMA_READY = True


def cache_get(cur, key: str):
    """Return the cached payload for a key, or None. Never spends quota.

    get_db() passes row_factory=dict_row, so rows arrive as dicts. Indexing
    them positionally raises TypeError on every cache hit -- which would look
    like a hard failure rather than a cache miss.
    """
    cur.execute("SELECT found, payload, source FROM skiptrace_cache WHERE address_key = %s;",
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
    # The found column is authoritative, not whatever the payload happens to
    # carry. An older row, or a payload written before a field was added, can
    # disagree with the column; trusting the column keeps the two from drifting.
    data["found"] = bool(found)
    data["_cached"] = True
    data["_cache_source"] = source
    return data


def cache_put(cur, key: str, result: dict) -> None:
    """Cache a result, misses included.

    Caching the miss is what stops a repeated dead address from consuming quota
    on every attempt.
    """
    payload = {k: v for k, v in result.items() if not k.startswith("_")}
    cur.execute(
        """INSERT INTO skiptrace_cache (address_key, found, payload, source)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (address_key) DO UPDATE
             SET found = EXCLUDED.found, payload = EXCLUDED.payload,
                 source = EXCLUDED.source, checked_at = CURRENT_TIMESTAMP;""",
        (key, bool(result.get("found")), json.dumps(payload, default=str),
         (result.get("property") or {}).get("_source")),
    )


def audit(cur, requested_by: str, key: str, result: dict, cached: bool) -> None:
    cur.execute(
        """INSERT INTO skiptrace_audit
             (requested_by, address_key, found, owner_of_record, is_residential, source, cached)
           VALUES (%s, %s, %s, %s, %s, %s, %s);""",
        (requested_by or "", key, bool(result.get("found")),
         result.get("owner_of_record") or "", bool(result.get("is_residential")),
         (result.get("property") or {}).get("_source"), bool(cached)),
    )


def lookup(cur, street: str, city: str, state: str, zip_code: str = "",
           requested_by: str = "") -> dict:
    """Cache-first wrapper around trace_address. The entry point the route uses.

    Cached hits and misses both short-circuit before any provider is called, so
    repeat traffic costs nothing against the 50/month plan.
    """
    key = normalize_address(street, city, state, zip_code)
    if not key:
        raise ProviderError("street and city are required", 400)

    ensure_schema(cur)
    cached = cache_get(cur, key)
    if cached is not None:
        cached["found"] = bool(cached.get("found"))
        audit(cur, requested_by, key, cached, cached=True)
        return cached

    result = trace_address(street, city, state, zip_code)
    cache_put(cur, key, result)
    audit(cur, requested_by, key, result, cached=False)
    return result
