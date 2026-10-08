#!/usr/bin/env python3
"""Skip trace: address -> owner of record -> registered agent if the owner is an entity.

Production port of the local tool in ~/skipTraced/main.py, rewritten against
urllib to match this repo (nothing here needs httpx, and requirements.txt does
not carry it). Exposed as POST /api/skiptrace, admin-only.

WHY THE CACHE IS NOT OPTIONAL
Hampton Roads addresses use HRGEO's free public parcel layer first. RentCast's
free Developer plan allows only 50 calls per month, so it is an optional
fallback for a GIS miss and is never called without a configured key. Results
are cached by normalized address. A successful parcel-service miss is cached;
an unavailable service is an error and is not cached as "no owner."

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
_HRGEO_PARCELS_URL = (
    "https://geo.hrsd.com/hrgeo/rest/services/regionalgis/"
    "HRGeo_Parcels_Public/MapServer/0/query"
)
HRGEO_SOURCE_URL = "https://www.hrgeo.org/pages/regional-parcels"

_STREET_SUFFIXES = {
    "AVENUE": "AVE", "BOULEVARD": "BLVD", "CIRCLE": "CIR", "COURT": "CT",
    "CRESCENT": "CRES", "DRIVE": "DR", "HIGHWAY": "HWY", "LANE": "LN",
    "PARKWAY": "PKWY", "PLACE": "PL", "ROAD": "RD", "STREET": "ST",
    "TERRACE": "TER", "TRAIL": "TRL",
}


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
    # A missing ZIP is NOT a failure. 217 of 500 permit rows have no ZIP, and
    # RentCast resolves street+city+state on its own (verified live: "201 OAK
    # GROVE ROAD, Norfolk, VA" returns an owner). Rejecting those would throw
    # away 43% of the permits for no reason.
    return {"ok": True, "street": street, "city": city.upper(), "state": state,
            "zipcode": zipcode, "no_zip": not zipcode}


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


def _normalize_situs_street(value: str) -> str:
    """Normalize common street suffixes so parcel and input addresses compare."""
    text = re.sub(r"[.,#]", " ", str(value or "").upper())
    text = re.sub(r"\b(?:APT|APARTMENT|UNIT|STE|SUITE)\s+[A-Z0-9-]+\b.*$", "", text)
    tokens = re.sub(r"\s+", " ", text).strip().split()
    if tokens:
        tokens[-1] = _STREET_SUFFIXES.get(tokens[-1], tokens[-1])
    return " ".join(tokens)


def _arcgis_literal(value: str) -> str:
    """Quote a value for ArcGIS' SQL where expression."""
    return "'" + str(value).replace("'", "''") + "'"


def fetch_hrgeo_owner(street: str, city: str, zip_code: str = ""):
    """Resolve one Hampton Roads situs address against HRGEO's public parcels.

    ArcGIS returns a prefix candidate set, so the complete normalized street,
    city, and (when available) ZIP are checked locally before accepting a hit.
    Ambiguous parcel matches are treated as misses rather than guessed.
    """
    street_key = _normalize_situs_street(street)
    city_key = re.sub(r"\s+", " ", str(city or "").strip()).upper()
    if not street_key or not city_key:
        return NOT_FOUND

    prefix_tokens = street_key.split()
    if len(prefix_tokens) < 2:
        return NOT_FOUND
    # Exclude a final road suffix from the prefix so the service can still find
    # records whose locality data uses the long or abbreviated suffix form.
    prefix = prefix_tokens[:-1] if prefix_tokens[-1] in set(_STREET_SUFFIXES.values()) else prefix_tokens
    if len(prefix) < 2:
        prefix = prefix_tokens

    where = (
        f"UPPER(PSTLCITY) = {_arcgis_literal(city_key)} AND "
        f"UPPER(PSTLADDRESS) LIKE {_arcgis_literal(' '.join(prefix) + '%')}"
    )
    if zip_code:
        where += f" AND PSTLZIP5 = {_arcgis_literal(str(zip_code).strip()[:5])}"

    status, body = _get(
        _HRGEO_PARCELS_URL,
        {
            "where": where,
            "outFields": (
                "OWNERNME1,PSTLADDRESS,PSTLCITY,PSTLZIP5,PARCELID,TOTVALUE,"
                "RESYRBLT,SRCAGENCY,LASTUPDATE"
            ),
            "returnGeometry": "false",
            "resultRecordCount": 100,
            "f": "json",
        },
    )
    if status != 200:
        raise ProviderError(f"HRGEO public parcel service HTTP {status}", 502)
    if not isinstance(body, dict):
        raise ProviderError("HRGEO public parcel service returned invalid JSON", 502)
    if body.get("error"):
        error = body["error"]
        code = error.get("code") if isinstance(error, dict) else ""
        raise ProviderError(
            f"HRGEO public parcel service error{f' {code}' if code else ''}", 502
        )

    features = body.get("features") or []
    matches = []
    input_zip = str(zip_code or "").strip()[:5]
    for feature in features:
        attrs = feature.get("attributes") if isinstance(feature, dict) else None
        if not isinstance(attrs, dict):
            continue
        if _normalize_situs_street(attrs.get("PSTLADDRESS")) != street_key:
            continue
        if str(attrs.get("PSTLCITY") or "").strip().upper() != city_key:
            continue
        parcel_zip = str(attrs.get("PSTLZIP5") or "").strip()[:5]
        if input_zip and parcel_zip and input_zip != parcel_zip:
            continue
        owner = str(attrs.get("OWNERNME1") or "").strip()
        if owner:
            matches.append(attrs)

    # Multiple parcels can share a street address. Do not guess which is the
    # lead's property unless the public data resolves to a single parcel.
    parcels = {
        str(row["PARCELID"]): row
        for row in matches if str(row.get("PARCELID") or "").strip()
    }
    if len(parcels) != 1:
        return NOT_FOUND

    parcel = next(iter(parcels.values()))
    return {
        "owner_name": str(parcel["OWNERNME1"]).strip(),
        "owner_type": None,
        "owner_occupied": None,
        "owner_mailing_city": None,
        "parcel": parcel.get("PARCELID"),
        "county": parcel.get("SRCAGENCY") or parcel.get("PSTLCITY"),
        "assessed_value": parcel.get("TOTVALUE"),
        "year_built": parcel.get("RESYRBLT"),
        "square_footage": None,
        "property_type": None,
        "last_sale_date": None,
        "last_sale_price": None,
        "last_update": parcel.get("LASTUPDATE"),
        "source_url": HRGEO_SOURCE_URL,
        "_source": "hrgeo",
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

    # Prefer HRGEO's free public parcel data. RentCast remains the fallback for
    # addresses outside Hampton Roads, ambiguous parcel matches, or GIS misses.
    hrgeo_error = None
    rentcast_result = None
    try:
        hrgeo_result = fetch_hrgeo_owner(street, city, zip_code)
    except ProviderError as exc:
        hrgeo_error = exc
        attempted.append(f"hrgeo: {exc.message}")
        rentcast_result = fetch_rentcast(address)
    else:
        if hrgeo_result is not None and hrgeo_result is not NOT_FOUND:
            hrgeo_result["no_zip"] = not zip_code
            hrgeo_result["provider"] = "hrgeo"
            record = hrgeo_result
            attempted.append("hrgeo: parcel matched")
        else:
            attempted.append("hrgeo: no unique parcel match")
            rentcast_result = fetch_rentcast(address)

    if record is None:
        got = rentcast_result
        if got is not None and got is not NOT_FOUND:
            got["no_zip"] = not zip_code
            if not zip_code:
                # Without a ZIP the provider falls back to a city-level match,
                # so the owner may belong to a different house on the street.
                # Carried in the payload (not a `_` key) so it survives cache.
                got["_low_confidence"] = True
            got["provider"] = "rentcast"
            record = got
            attempted.append("rentcast: matched")
        elif got is NOT_FOUND:
            attempted.append("rentcast: no record for this address")
        else:
            attempted.append("rentcast: RENTCAST_API_KEY not set, skipped")

    if record is None and hrgeo_error:
        raise hrgeo_error

    if record is None:
        return {"found": False, "layers_tried": attempted,
                "address": address, "no_zip": not zip_code, "provider": "hrgeo",
                "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}

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
        # True when there was no ZIP, so the match may be a neighbouring house.
        "no_zip": not zip_code,
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
    if not data["found"] and source != "hrgeo":
        # Invalidate permanent misses created before the free parcel source was
        # added; otherwise newly covered addresses would stay undiscoverable.
        return None
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
         (result.get("property") or {}).get("_source") or result.get("provider")),
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


def cache_get_many(cur, keys) -> dict:
    """Read many cache rows in ONE query. Returns {address_key: payload}.

    The permits lane renders up to 500 rows at a time. Calling cache_get()
    per row would be 500 round trips to display data we already hold, so the
    keys go in as one array parameter instead.

    Returns {} when the table does not exist, so a fresh database renders
    without it rather than erroring.
    """
    keys = [k for k in dict.fromkeys(keys or []) if k]
    if not keys:
        return {}

    # A failed statement poisons the whole transaction until rollback, so the
    # existence check has to happen before the query rather than being caught.
    cur.execute("SELECT to_regclass('skiptrace_cache') AS t;")
    row = cur.fetchone()
    present = (row.get("t") if isinstance(row, dict) else (row[0] if row else None))
    if not present:
        return {}

    cur.execute("SELECT address_key, found, payload, source FROM skiptrace_cache "
                "WHERE address_key = ANY(%s);", (keys,))
    out = {}
    for r in cur.fetchall():
        if not r["found"] and r.get("source") != "hrgeo":
            # A negative written before the free regional lookup was added is
            # stale; let the user recheck it rather than presenting it as final.
            continue
        payload = {}
        if r["payload"]:
            try:
                payload = json.loads(r["payload"])
            except (TypeError, ValueError):
                payload = {}
        payload["found"] = bool(r["found"])
        payload["_cached"] = True
        payload["_cache_source"] = r.get("source")
        out[r["address_key"]] = payload
    return out


def owners_for_rows(cur, rows, id_key: str = "id", address_key: str = "address") -> dict:
    """Map row id -> cached owner payload for many rows, parsing each once.

    This is what makes "Who owns this property?" survive leaving the page.
    The per-permit lookup button deliberately does not write to the permit --
    resolving who owns a house is not the same as having reached them, and
    filling contractor_name would make an unverified owner look like they
    applied for the permit. So the answer lived only in the browser until the
    next click.

    It does not need to be written anywhere: skiptrace_cache already holds it,
    keyed by normalized address, for every permit on that house. Reading it here
    costs nothing, which matters against a 50-call/month plan, and it lights up
    the ~119 addresses already traced rather than making them be looked up again.
    """
    row_key = {}
    for r in rows or []:
        raw = r.get(address_key) if isinstance(r, dict) else None
        parsed = parse_address(raw)
        if not parsed["ok"]:
            continue
        key = normalize_address(parsed["street"], parsed["city"],
                                parsed["state"], parsed["zipcode"])
        # First row wins for a given key, but every row on that house gets it.
        row_key.setdefault(key, r.get(id_key))

    cached = cache_get_many(cur, list(row_key))
    if not cached:
        return {}

    out = {}
    for r in rows or []:
        raw = r.get(address_key) if isinstance(r, dict) else None
        parsed = parse_address(raw)
        if not parsed["ok"]:
            continue
        key = normalize_address(parsed["street"], parsed["city"],
                                parsed["state"], parsed["zipcode"])
        hit = cached.get(key)
        if hit:
            out[r.get(id_key)] = hit
    return out


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


# --- batch enrichment of address-only leads ----------------------------------
# Every lead that arrives with a postal address and no contact is run through
# the lookup and written back. Two facts shape this entirely:
#
# 1. Hampton Roads parcels come from the free HRGEO layer. RentCast remains a
#    capped, optional fallback for unmatched addresses. Results are cached and
#    deduplicated by normalized situs address because permit feeds can emit
#    multiple rows for one property.
#
# 2. The assessor record contains a NAME and no phone or email. So this fills in
#    the one field that is genuinely empty and truthful. It does not fabricate
#    contact details, and it never overwrites a name that is already a person.
ENRICH_DEFAULT_CAP = 25  # per run; also bounds optional paid fallbacks


def enrich_cap() -> int:
    try:
        return max(0, int(os.getenv("SKIPTRACE_ENRICH_MAX", ENRICH_DEFAULT_CAP) or 0))
    except (TypeError, ValueError):
        return ENRICH_DEFAULT_CAP


_PLACEHOLDER_TOKENS = (
    "permit", "homeowner", "asleep", "morning", "lead.local", "backlog",
    "instant_quote", "reddit", "website", "linkedin", "shovels", "sam-gov",
    "n/a", "not available", "tbd", "applicant", "owner name", "n/a name",
    # Words that are common fragments of REAL names -- "test" in "Testament",
    # "city" in "Cityscape", "unknown" in "Unknown Soldier", "owner" in
    # "Owner John Smith" -- are deliberately NOT substring tokens; they are
    # whole-token matches in _PLACEHOLDER_EXACT below. Keeping them as
    # substrings silently ate three real names in testing.
    # "permit" stays a substring on purpose: "permit_finder" has no word
    # boundary after "permit" because "_" is a word character.
)
# Whole-token matches, checked BEFORE substring matching. "Self" is the
# common case (SELF_APPLICANTS in permit_service). "Self" and "Myself" share
# a substring, so self must never be a substring token.
_PLACEHOLDER_EXACT = {
    "self", "self applied", "self-applied", "owner", "owner unknown",
    "applicant", "city", "unknown", "homeowner", "test", "test lead",
    "tbd", "na", "n a", "none", "null", "not available", "permits",
}


def is_placeholder_name(name: str) -> bool:
    """True when leads.name holds something that is not a person.

    permit_finder writes the bare CITY into name ("Chesapeake"), so most
    address-only leads carry a place where a name belongs. Overwriting one of
    those with a real owner is the whole point; overwriting a real person is
    data loss.

    Substring matching, not \\bword\\b -- "permit_finder" has no word boundary
    after "permit" because "_" is a word character, so a \\b pattern silently
    misses every source slug. The tokens are all long enough that a substring
    hit is meaningful: an earlier version included "na" and matched "Leonard".
    """
    cleaned = re.sub(r"\s+", " ", str(name or "")).strip().lower()
    if not cleaned:
        return True
    if cleaned in _PLACEHOLDER_EXACT:
        return True
    # "Label · detail" form: "Permit · 415 Carlisle Way", "Owner · 123",
    # "Reddit · /u/someone". The LABEL is what says whether this is a person.
    # Check the label alone so "Owner · 123" is caught without making "owner"
    # a substring token again.
    label = re.split(r"\s·\s", cleaned, maxsplit=1)[0].strip()
    if label and label != cleaned and label in _PLACEHOLDER_EXACT:
        return True
    if any(token in cleaned for token in _PLACEHOLDER_TOKENS):
        return True
    # A bare ZIP, or a "City, ST" pair.
    if re.fullmatch(r"\d{5}(-\d{4})?", cleaned):
        return True
    if "," in cleaned:
        return True
    # Every word is a known place word: "Virginia Beach", "Newport News".
    words = [w for w in re.split(r"[^a-z']+", cleaned) if w]
    if words and all(w in _CITY_WORDS for w in words):
        return True
    return False


_CITY_WORDS = {
    "virginia", "beach", "chesapeake", "norfolk", "newport", "news", "hampton",
    "portsmouth", "suffolk", "williamsburg", "va", "yorktown", "poquoson",
    "hampton", "vbsb", "vb", "suffolk", "chuckatuck", "cbf", "nnd",
}


def enrich_address_only_leads(cur, company: str = "construction", cap: int = None,
                              source: str = "permit_finder") -> dict:
    """Fill in the owner name on leads that have an address and no contact.

    `source` defaults to 'permit_finder', and that default matters. The first
    version ran over every address-only lead and spent 25 of the 50 monthly
    calls to update nothing, because the loose population is mostly completed
    jobs and the owner's own STR properties -- addresses RentCast has no record
    of. The permit-derived leads are the ones that genuinely lack a name and do
    resolve; pass source='' for the old unfiltered behaviour.

    Deliberately NOT job_leads. Its only name column is contractor_name, which
    renders on the card as "Contractor on permit". Writing a skip-traced owner
    there would claim that person applied for a permit they never applied for.

    Returns a summary: {examined, traced, cached, updated, skipped, spent,
    cap, notes}. `spent` is the number of billable provider calls, which is the
    number that matters against a 50/month plan.

    Fails soft throughout. A provider outage leaves every lead untouched and is
    reported, never written with a half-answer.
    """
    limit = enrich_cap() if cap is None else max(0, int(cap))
    summary = {"examined": 0, "traced": 0, "cached": 0, "updated": 0,
               "skipped": 0, "spent": 0, "cap": limit, "source": source or "(any)",
               "notes": []}
    if limit <= 0:
        summary["notes"].append("enrichment disabled (SKIPTRACE_ENRICH_MAX=0)")
        return summary

    ensure_schema(cur)
    cur.execute(
        "SELECT id, name, address FROM leads "
        "WHERE company = %(company)s AND status = 'new' "
        "  AND (%(source)s = '' OR COALESCE(source, '') = %(source)s) "
        "  AND address IS NOT NULL AND BTRIM(address) <> '' "
        "  AND (email IS NULL OR BTRIM(email) = '' OR LOWER(email) LIKE '%%@lead.local') "
        "  AND (phone IS NULL OR BTRIM(phone) = '' "
        "       OR LOWER(BTRIM(phone)) IN ('unknown','n/a','none','-')) "
        "ORDER BY id;",
        {"company": company, "source": source or ""},
    )
    rows = cur.fetchall()
    summary["examined"] = len(rows)
    if not rows:
        return summary

    by_key, key_to_leads = {}, {}
    for row in rows:
        parsed = parse_address(row.get("address"))
        if not parsed["ok"]:
            summary["skipped"] += 1
            continue
        key = normalize_address(parsed["street"], parsed["city"], parsed["state"], parsed["zipcode"])
        # One lookup per door. Every permit on the same house shares the result.
        key_to_leads.setdefault(key, []).append((row["id"], row.get("name")))
        by_key[key] = parsed

    spent = 0
    for key, leads in key_to_leads.items():
        if spent >= limit:
            summary["notes"].append(f"hit the {limit}-call run cap; "
                                    f"{len(key_to_leads) - spent} address(es) deferred to the next run")
            break

        cached = cache_get(cur, key)
        if cached is not None:
            summary["cached"] += 1
            result = cached
        else:
            parsed = by_key[key]
            try:
                result = trace_address(parsed["street"], parsed["city"],
                                       parsed["state"], parsed["zipcode"])
            except ProviderError as exc:
                # Stop rather than skip ahead: a 429 means the month is gone and
                # every remaining address would fail the same way.
                summary["notes"].append(f"provider stopped the run ({exc.status}: {exc.message})")
                break
            spent += 1
            summary["traced"] += 1
            cache_put(cur, key, result)

        audit(cur, "enrichment", key, result, cached=cached is not None)

        owner = (result.get("owner_of_record") or "").strip() if result.get("found") else ""
        if not owner:
            summary["skipped"] += len(leads)
            continue

        touched = 0
        for lead_id, current_name in leads:
            if not is_placeholder_name(current_name):
                summary["skipped"] += 1
                continue
            # Compare-and-swap on the exact name we judged, not a second,
            # stricter test of it. The WHERE clause used to require
            # name IS NULL / BTRIM(name) = '' / LIKE '%@lead.local', while
            # is_placeholder_name() above also accepts a city name -- which is
            # what the permit feeds put there ("Virginia Beach", 122 rows;
            # "Chesapeake", 31; "Williamsburg", 14). The two guards disagreed,
            # the SQL one matched none of them, and this function spent RentCast
            # quota to update exactly zero rows on every run.
            #
            # Swapping on the value we read keeps the real guarantee (we never
            # overwrite a person) and adds lost-update safety: if the card was
            # edited between the SELECT and here, the name no longer matches
            # and nothing is written.
            cur.execute(
                "UPDATE leads SET name = %s WHERE id = %s "
                "  AND COALESCE(name, '') = %s RETURNING id;",
                (owner, lead_id, current_name or ""),
            )
            touched += cur.rowcount or 0
        summary["updated"] += touched

    summary["spent"] = spent
    return summary
