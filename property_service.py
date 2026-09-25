"""Property lookup for instant ballpark quotes.

Primary provider: RentCast (real API, cheap, free tier ~50 calls/month). If no
key is configured, the caller falls back to a manual square-footage entry so the
feature still works during development / smoke tests.

Because the RentCast free tier is only ~50 live calls per month, lookups are
heavily cached:

* a small in-memory cache (per process), and
* a Postgres-backed cache shared by every box (both services use the same DB).

A per-calendar-month call budget is also enforced — ``RENTCAST_MONTHLY_LIMIT``
(default 50). Once spent, only cached results are returned; live lookups of a
brand-new address return ``None`` and the caller falls back to manual sqft entry.
"""

import datetime
import os
import json
import urllib.request
import urllib.parse

RENTCAST_BASE = "https://api.rentcast.io/v1"

# Cache key among owners; also guards against pathological re-quotes.
RENTCAST_LOOKUP_TABLE = "property_lookup_cache"

_mem_cache: dict = {}
_MEM_CAPACITY = 256
_db_checked: bool = False


def _num(val):
    if isinstance(val, dict):
        v = val.get("value")
        try:
            return 0 if v is None else float(v)
        except (TypeError, ValueError):
            return 0
    try:
        return 0 if val is None else float(val)
    except (TypeError, ValueError):
        return 0


def is_configured() -> bool:
    return bool(os.getenv("RENTCAST_API_KEY", "").strip())


# --- helpers --------------------------------------------------------------

def _key(address: str) -> str:
    """Normalize an address into a stable cache key."""
    return " ".join((address or "").strip().lower().split())


def _month_key() -> str:
    return datetime.date.today().strftime("%Y-%m")


def _month_limit() -> int:
    try:
        return max(int(os.getenv("RENTCAST_MONTHLY_LIMIT", "50")), 1)
    except (TypeError, ValueError):
        return 50


def _db():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(os.getenv("DATABASE_URL", ""), row_factory=dict_row)


def _ensure_table(conn) -> None:
    global _db_checked
    if _db_checked:
        return
    with conn.cursor() as cur:
        cur.execute(
            f"""CREATE TABLE IF NOT EXISTS {RENTCAST_LOOKUP_TABLE} (
                address_key TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );"""
        )
    conn.commit()
    _db_checked = True


def _calls_this_month(conn) -> int:
    """Number of live RentCast calls fired this calendar month (atomic read)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM app_settings WHERE key = %s;",
                (f"rentcast_monthly_calls:{_month_key()}",),
            )
            row = cur.fetchone()
        return int(row["value"]) if row and row["value"] else 0
    except Exception:
        return 0


def _record_call(conn) -> None:
    """Atomically increment this month's live-call counter (reserves budget)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO app_settings (key, value, updated_at)
                   VALUES (%s, '1', CURRENT_TIMESTAMP)
                   ON CONFLICT (key) DO UPDATE
                   SET value = (COALESCE(NULLIF(app_settings.value, ''), '0')::INTEGER + 1)::TEXT,
                       updated_at = CURRENT_TIMESTAMP;""",
                (f"rentcast_monthly_calls:{_month_key()}",),
            )
        conn.commit()
    except Exception as e:
        print(f"⚠️ RentCast counter update failed: {e}")


def _read_db_cache(conn, key: str):
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT payload FROM {RENTCAST_LOOKUP_TABLE} WHERE address_key = %s;",
                (key,),
            )
            row = cur.fetchone()
        if row and row["payload"]:
            return json.loads(row["payload"])
    except Exception as e:
        print(f"⚠️ RentCast cache read failed: {e}")
    return None


def _write_db_cache(conn, key: str, payload: dict) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {RENTCAST_LOOKUP_TABLE} (address_key, payload, updated_at)
                    VALUES (%s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (address_key) DO UPDATE
                    SET payload = EXCLUDED.payload, updated_at = CURRENT_TIMESTAMP;""",
                (key, json.dumps(payload)),
            )
        conn.commit()
    except Exception as e:
        print(f"⚠️ RentCast cache write failed: {e}")


def _cache_get(key: str):
    if key in _mem_cache:
        return _mem_cache[key]
    try:
        with _db() as conn:
            _ensure_table(conn)
            hit = _read_db_cache(conn, key)
    except Exception:
        hit = None
    if hit is not None:
        _cache_put(key, hit)
    return hit


def _cache_put(key: str, payload: dict) -> None:
    _mem_cache[key] = payload
    while len(_mem_cache) > _MEM_CAPACITY:
        _mem_cache.pop(next(iter(_mem_cache)))
    try:
        with _db() as conn:
            _ensure_table(conn)
            _write_db_cache(conn, key, payload)
    except Exception as e:
        print(f"⚠️ RentCast cache store failed: {e}")


def _api_call(address: str):
    """One live RentCast request. Returns raw first property result or None."""
    query = urllib.parse.urlencode({"address": address})
    url = f"{RENTCAST_BASE}/properties?{query}"
    req = urllib.request.Request(
        url,
        headers={"X-Api-Key": os.environ["RENTCAST_API_KEY"], "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    results = data if isinstance(data, list) else data.get("properties", [])
    return (results or [None])[0]


def lookup_address(address: str):
    """Look up a US property by street address. Returns a normalized dict or None.

    Cache-first (memory → shared Postgres). Only hits the live RentCast API for a
    brand-new address, and only while this month's call budget remains.
    """
    addr = (address or "").strip()
    if not addr or not is_configured():
        return None
    key = _key(addr)

    cached = _cache_get(key)
    if cached is not None:
        return cached

    budget_left = None
    try:
        with _db() as conn:
            _ensure_table(conn)
            budget_left = _calls_this_month(conn) < _month_limit()
    except Exception:
        budget_left = None  # DB down → allow direct call without budget bookkeeping

    if budget_left is False:
        print(f"⚠️ RentCast monthly budget spent ({_month_limit()}); using cache only.")
        return None

    try:
        p = _api_call(addr)
    except Exception as e:
        print(f"❌ RentCast lookup failed for {addr!r}: {e}")
        return None
    if p is None:
        return None

    if budget_left is not False:
        try:
            with _db() as conn:
                _ensure_table(conn)
                _record_call(conn)
        except Exception:
            pass

    result = {
        "address": p.get("formattedAddress") or addr,
        "city": p.get("city") or "",
        "state": p.get("state") or "",
        "zip": p.get("zipCode") or "",
        "sqft": _num(p.get("squareFootage")),
        "lot_sqft": _num(p.get("lotSizeSqFt")),
        "beds": _num(p.get("bedrooms")),
        "baths": _num(p.get("bathrooms")),
        "year_built": _num(p.get("yearBuilt")),
        "stories": max(int(_num(p.get("stories")) or 1), 1),
        "property_type": p.get("propertyType") or "",
        "success": True,
        "source": "rentcast",
    }
    _cache_put(key, result)
    return result