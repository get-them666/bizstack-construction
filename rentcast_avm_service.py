"""Property value (AVM/ARV) estimates from RentCast.

What this adds over property_service.py
--------------------------------------
property_service answers "how big is this house" -- sqft, beds, baths, year
built. Those drive a ballpark quote. This answers a different question: "what is
this thing worth *now*", as an automated valuation model (AVM) with an estimate
range and the comparable sales it was built from.

For a general contractor that number is the raw material for two things the
square-footage lookup cannot produce:

  * ARV. Sale price minus what the renovation costs is the ceiling on what a
    client can spend. Quoting above it guarantees a loss.
  * Owner equity. Sale price minus the mortgage balance is what a homeowner has
    at stake, which is the actual lever on whether they say yes.

Why it is separate rather than another field on property_service
---------------------------------------------------------------
Different failure modes and different costs. A property-record lookup is one
call per address and is cacheable forever -- a house's sqft does not change. An
AVM is a *market* reading: it moves with comparable sales, so caching one for
weeks serves a stale number to an owner being asked for money. It gets its own
cache with a short TTL, and it is opt-in per call rather than an automatic
extra charge hidden inside the quote path.

Budget
------
RentCast bills every request that reaches their servers, so an AVM call is
charged whether it returns an estimate, errors, or finds nothing. All three are
counted against the shared monthly budget in property_service -- one counter for
the whole key, not one per feature. A 50-call month is 50 calls total.
"""

import json
import os
import time

import property_service as ps

# AVM cache key prefix, distinct from the property-record cache.
AVM_TABLE = "property_avm_cache"

# A valuation is a market reading, not a fact about the building. Reusing a
# number from last month in a deal conversation is worse than having no number.
AVM_TTL_HOURS = int(os.getenv("RENTCAST_AVM_TTL_HOURS", "168") or 168)  # 7 days
_avm_mem_cache: dict = {}


def is_configured() -> bool:
    return ps.is_configured()


def _key(address: str) -> str:
    return ps._key(address)


def _ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS {AVM_TABLE} (
                address_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                fetched_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );"""
        )
    conn.commit()


def _db():
    return ps._db()


def _fetched_at_fresh(row) -> bool:
    """Whether the cached row is inside the TTL."""
    if not row:
        return False
    ts = row.get("fetched_at") if isinstance(row, dict) else None
    if ts is None:
        return False
    try:
        age = time.time() - ts.timestamp()
    except (AttributeError, TypeError, ValueError):
        return False
    return age < AVM_TTL_HOURS * 3600


def _read_cache(conn, key: str):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT payload, fetched_at FROM {AVM_TABLE} WHERE address_key = %s;",
            (key,),
        )
        row = cur.fetchone()
    if not row or not _fetched_at_fresh(row):
        return None
    try:
        return json.loads(row["payload"])
    except (TypeError, ValueError):
        return None


def _write_cache(conn, key: str, payload: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO {AVM_TABLE} (address_key, payload, fetched_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (address_key) DO UPDATE
                  SET payload = EXCLUDED.payload, fetched_at = CURRENT_TIMESTAMP;""",
            (key, json.dumps(payload, default=str)),
        )
    conn.commit()


def _cache_get(key: str):
    if key in _avm_mem_cache:
        entry = _avm_mem_cache[key]
        # The in-process copy carries its own age so a long-lived worker still
        # honours the TTL instead of serving one number for the life of the box.
        if time.time() - entry.get("_stored_at", 0) < AVM_TTL_HOURS * 3600:
            return entry
    try:
        with _db() as conn:
            _ensure_table(conn)
            hit = _read_cache(conn, key)
    except Exception:
        return None
    if hit is not None:
        hit["_stored_at"] = time.time()
        _avm_mem_cache[key] = hit
    return hit


def _cache_put(key: str, payload: dict) -> None:
    payload = dict(payload)
    payload["_stored_at"] = time.time()
    _avm_mem_cache[key] = payload
    try:
        with _db() as conn:
            _ensure_table(conn)
            _write_cache(conn, key, payload)
    except Exception as e:
        print(f"⚠️ RentCast AVM cache store failed: {e}")


def _num(val, default=0.0):
    try:
        return default if val in (None, "") else float(val)
    except (TypeError, ValueError):
        return default


def parse_estimate(data: dict) -> dict:
    """Pull the estimate, its range, and the subject record out of an AVM response.

    RentCast returns `price`/`priceRangeLow`/`priceRangeHigh` for /avm/value and
    `rent`/`rentRangeLow`/`rentRangeHigh` for /avm/rent/long-term. Both shapes are
    accepted so a rent call is not silently read as a zero-valued estimate.
    """
    if not isinstance(data, dict):
        return {}
    subject = data.get("subjectProperty") or {}
    comps = data.get("comparables") or []
    price = data.get("price")
    rent = data.get("rent")
    low = data.get("priceRangeLow", data.get("rentRangeLow"))
    high = data.get("priceRangeHigh", data.get("rentRangeHigh"))
    if price in (None, "") and rent in (None, ""):
        return {}
    out = {
        "estimate": _num(price if price not in (None, "") else rent),
        "range_low": _num(low),
        "range_high": _num(high),
        "kind": "value" if price not in (None, "") else "rent",
        "comparable_count": len(comps) if isinstance(comps, list) else 0,
        "address": subject.get("formattedAddress") or "",
        "sqft": _num(subject.get("squareFootage")),
        "beds": _num(subject.get("bedrooms")),
        "baths": _num(subject.get("bathrooms")),
        "year_built": _num(subject.get("yearBuilt")),
        "property_type": subject.get("propertyType") or "",
        "last_sale_price": _num(subject.get("lastSalePrice")),
    }
    # The spread is the confidence signal. A wide range means the comps disagree
    # and the number should not be presented to a homeowner as a firm figure.
    span = out["range_high"] - out["range_low"]
    out["range_spread"] = span
    out["confidence"] = "narrow" if span <= 0.10 * out["estimate"] else "wide" if out["estimate"] else ""
    return out


def _fetch_live(address: str, kind: str, force: bool = False) -> dict:
    path = "/avm/value" if kind == "value" else "/avm/rent/long-term"
    # RentCast's documented defaults for a result close to its own website. The
    # docs are explicit that omitting these changes the answer, so they are sent
    # rather than left to the server default.
    params = {"address": address, "compCount": 20, "maxRadius": 5, "daysOld": 270}
    data = ps._request(path, params)
    estimate = parse_estimate(data)
    if estimate:
        estimate["source"] = "rentcast_avm"
        estimate["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return estimate


def estimate_value(address: str, kind: str = "value", force: bool = False) -> dict:
    """Current market value (kind='value') or long-term rent (kind='rent').

    Returns {} when RentCast is unconfigured, the budget is spent, the address is
    unknown to them, or the call failed. Never raises: an estimate is an
    enrichment, and a quote or an equity letter must still be produced without it.
    """
    addr = (address or "").strip()
    if not addr or not is_configured():
        return {}
    if kind not in ("value", "rent"):
        kind = "value"
    key = _key(addr)

    if not force:
        cached = _cache_get(key)
        if cached is not None:
            cached["cached"] = True
            return cached

    if not ps.budget_allows(1):
        print("⚠️ RentCast AVM skipped: monthly budget spent.")
        return {}

    reached_api = False
    try:
        estimate = _fetch_live(addr, kind, force=force)
        reached_api = True
    except ps.RentCastError as exc:
        reached_api = exc.reached_api
        print(f"❌ RentCast AVM failed for {addr!r}: {exc.message}")
        return {}
    except Exception as e:
        print(f"❌ RentCast AVM failed for {addr!r}: {type(e).__name__}: {e}")
        return {}
    finally:
        if reached_api:
            ps.charge_call(1)

    if not estimate:
        return {}
    _cache_put(key, estimate)
    estimate["cached"] = False
    return estimate