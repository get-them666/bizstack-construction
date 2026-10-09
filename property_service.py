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
import urllib.error
import urllib.request
import urllib.parse

RENTCAST_BASE = "https://api.rentcast.io/v1"

# Cache key among owners; also guards against pathological re-quotes.
RENTCAST_LOOKUP_TABLE = "property_lookup_cache"

_mem_cache: dict = {}
_MEM_CAPACITY = 256
_db_checked: bool = False

# Cached "RentCast has no record of this address". Stored so a repeat lookup of a
# bad address is free instead of re-spending a call every single time. Keyed
# distinctly from a real payload, which always carries "success": True.
_MISS = {"__rentcast_miss__": True}


class RentCastError(Exception):
    """An upstream RentCast failure, as distinct from "no record at this address".

    `reached_api` is the field that matters for budget accounting. RentCast bills
    every request that reaches their servers, so a 401, a 429, a 500, and even a
    200 whose body cannot be parsed all cost a real call. Only a failure that
    happened before the request left this box (DNS, refused connection, TLS) is
    genuinely free. Collapsing these two cases is what lets a real 50-call month
    be spent while this module's own counter still reads zero.
    """

    def __init__(self, message: str, reached_api: bool = False):
        super().__init__(message)
        self.message = message
        self.reached_api = reached_api


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


# --- the one budget ----------------------------------------------------------
# Every RentCast consumer shares this counter. The free tier is 50 requests per
# MONTH for the whole key -- not 50 per feature -- so the AVM estimate path and
# the sold-property sweep must not keep private tallies. A second counter would
# be invisible to the first and the month would overspend by however much the
# second one could spend.

def calls_remaining() -> int:
    """Live calls left this calendar month. -1 means "unknown" (DB unreachable).

    -1 rather than 0 on failure: a caller must be able to tell "the month is
    genuinely spent" apart from "I could not check", and only the former should
    block a request.
    """
    try:
        with _db() as conn:
            _ensure_table(conn)
            return max(_month_limit() - _calls_this_month(conn), 0)
    except Exception:
        return -1


def budget_summary() -> dict:
    """For the settings page: how much of the month is gone."""
    try:
        with _db() as conn:
            _ensure_table(conn)
            spent = _calls_this_month(conn)
    except Exception:
        return {"configured": is_configured(), "limit": _month_limit(),
                "spent": None, "remaining": None}
    limit = _month_limit()
    return {"configured": is_configured(), "limit": limit, "spent": spent,
            "remaining": max(limit - spent, 0)}


def budget_allows(cost: int = 1) -> bool:
    """True if `cost` more calls fit in the month. Unknown DB => allow."""
    if cost <= 0:
        return True
    left = calls_remaining()
    if left < 0:
        return True
    return left >= cost


def charge_call(n: int = 1) -> None:
    """Record `n` calls as spent. Best-effort; never raises."""
    if n <= 0:
        return
    try:
        with _db() as conn:
            _ensure_table(conn)
            for _ in range(n):
                _record_call(conn)
    except Exception as e:
        print(f"⚠️ RentCast counter update failed: {e}")


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


def _request(path: str, params: dict, timeout: int = 25):
    """One live RentCast GET. Returns the parsed body.

    Raises RentCastError with reached_api set honestly. This is the only place
    that knows the difference between "RentCast answered (and billed us)" and
    "the request never made it out", so the distinction is made here once and
    every caller inherits it rather than re-guessing.
    """
    clean = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    url = f"{RENTCAST_BASE}{path}"
    if clean:
        url = f"{url}?{urllib.parse.urlencode(clean, doseq=True)}"
    req = urllib.request.Request(
        url,
        headers={"X-Api-Key": os.getenv("RENTCAST_API_KEY", ""), "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # The server answered, so this request was billed.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise RentCastError(
            f"RentCast HTTP {exc.code}: {detail or exc.reason}", reached_api=True
        ) from None
    except TimeoutError:
        # socket.timeout is an alias of TimeoutError on 3.10+. The request was
        # fully sent; RentCast probably processed it. Count it.
        raise RentCastError("RentCast request timed out", reached_api=True) from None
    except urllib.error.URLError as exc:
        # DNS, refused connection, TLS failure -- nothing reached RentCast.
        raise RentCastError(f"Could not reach RentCast: {exc.reason}", reached_api=False) from None

    try:
        return json.loads(body)
    except ValueError as exc:
        # HTTP 200 with a body we cannot read. Billed, and not a miss.
        raise RentCastError(f"RentCast sent an unreadable body: {exc}", reached_api=True) from None


def _api_call(address: str):
    """One live property-record lookup. Returns the first raw record or None."""
    data = _request("/properties", {"address": address, "limit": 1})
    results = data if isinstance(data, list) else (data or {}).get("properties") or []
    return (results or [None])[0]


def lookup_address(address: str):
    """Look up a US property by street address. Returns a normalized dict or None.

    Cache-first (memory → shared Postgres). Only hits the live RentCast API for a
    brand-new address, and only while this month's call budget remains.

    Misses are cached too. A "no record" answer costs a real call from RentCast,
    so re-asking for the same bad address on every quote attempt would drain the
    month on an address RentCast will never find.
    """
    addr = (address or "").strip()
    if not addr or not is_configured():
        return None
    key = _key(addr)

    cached = _cache_get(key)
    if cached is not None:
        # A cached miss is a real answer, not a failure — report it as absent
        # rather than paying RentCast to ask the same question again.
        if isinstance(cached, dict) and cached.get("__rentcast_miss__"):
            return None
        return cached

    budget_ok = budget_allows(1)
    if not budget_ok:
        print(f"⚠️ RentCast monthly budget spent ({_month_limit()}); using cache only.")
        return None

    reached_api = False
    try:
        p = _api_call(addr)
        # Any HTTP answer — including an empty result set — was billed.
        reached_api = True
    except RentCastError as exc:
        reached_api = exc.reached_api
        print(f"❌ RentCast lookup failed for {addr!r}: {exc.message}")
        return None
    except Exception as e:
        # Unknown failure. Assume it was billed: over-counting one call is
        # recoverable, under-counting is how the quota silently overruns.
        print(f"❌ RentCast lookup failed for {addr!r}: {type(e).__name__}: {e}")
        return None
    finally:
        # Reserve the budget BEFORE acting on the result. Previously this only ran
        # on the happy path, so misses and 4xx/5xx responses were never counted
        # even though RentCast charged for every one of them.
        if reached_api and budget_ok:
            charge_call(1)

    if p is None:
        _cache_put(key, dict(_MISS))
        return None

    result = {
        "address": p.get("formattedAddress") or addr,
        "city": p.get("city") or "",
        "state": p.get("state") or "",
        "zip": p.get("zipCode") or "",
        "sqft": _num(p.get("squareFootage")),
        # The documented field is lotSize. lotSizeSqFt never existed, so this
        # silently read 0 for every property.
        "lot_sqft": _num(p.get("lotSize") or p.get("lotSizeSqFt")),
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