"""Run Tracerfy over prepared leads, explicitly and in bounded batches.

The free prep pass (`owner_backfill_job`) fills owner names and ZIPs. This is
the paid stage the owner starts by hand: one Tracerfy instant lookup per lead,
5 credits on a hit and nothing on a miss.

A lead without a ZIP is given one first -- from the address string when it is
already there, otherwise from the free Census/ArcGIS lookup. If neither can
produce a ZIP the lead is skipped rather than guessing at a door and billing
for the wrong property.

Every paid attempt is written to `skiptrace_audit` with source 'tracerfy' and
that trail is what keeps a second click from re-buying the same miss: an
address traced in the last RETRY_DAYS is not picked up again. Single-flight on
purpose, so two clicks cannot start two passes over the same rows.
"""
from __future__ import annotations

import os
import threading
import time

import psycopg
from psycopg.rows import dict_row

import data_enrichment
import skip_sherpa_service
import skiptrace_service
import zip_lookup

_LOCK = threading.Lock()
_STATE = {
    "running": False,
    "total": 0,
    "examined": 0,
    "found": 0,
    "misses": 0,
    "skipped": 0,
    "already_traced": 0,
    "credits_used": 0,
    "current": "",
    "note": "",
    "started_at": None,
    "finished_at": None,
    "balance": None,
}

DEAD = ("won", "lost", "closed", "cancelled", "canceled", "archived")

# A miss is free, but re-asking the same address every click is pointless and
# the provider asks us not to hammer it. Hits are excluded by having a contact;
# this window covers the misses.
RETRY_DAYS = 30

ATTEMPT_TAG = "lead_skiptrace_batch"


def status() -> dict:
    with _LOCK:
        return dict(_STATE)


def _no_contact_clause() -> str:
    return (
        "  AND (COALESCE(TRIM(email), '') = '' OR LOWER(TRIM(email)) LIKE '%%@lead.local') "
        "  AND COALESCE(TRIM(phone), '') = '' "
    )


def _recently_traced(conn, keys) -> set:
    """Address keys already paid for inside the retry window.

    Missing table (fresh database, prep never run) returns an empty set rather
    than erroring: the audit trail is a cost guard, not a prerequisite.
    """
    keys = [k for k in dict.fromkeys(keys or []) if k]
    if not keys:
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('skiptrace_audit') AS t;")
        row = cur.fetchone()
        present = row.get("t") if isinstance(row, dict) else (row[0] if row else None)
        if not present:
            return set()
        cur.execute(
            "SELECT DISTINCT address_key FROM skiptrace_audit "
            "WHERE source = 'tracerfy' AND address_key = ANY(%s) "
            "  AND created_at > CURRENT_TIMESTAMP - (%s || ' days')::interval;",
            (keys, str(int(RETRY_DAYS))),
        )
        return {r["address_key"] for r in cur.fetchall() if r.get("address_key")}


def _key_for(lead: dict) -> str:
    parsed = skiptrace_service.parse_address(lead.get("address") or "")
    if not parsed.get("ok"):
        return ""
    return skiptrace_service.normalize_address(
        parsed["street"], parsed["city"], parsed["state"])


def candidates(conn, company: str = "construction", limit: int = 25) -> list:
    """No-contact leads with an address, newest first, minus recent attempts."""
    want = max(1, int(limit))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, email, phone, address, zip, source, status "
            "FROM leads WHERE company = %s AND COALESCE(address, '') <> '' "
            + _no_contact_clause() +
            f"  AND LOWER(COALESCE(status, 'new')) NOT IN ({','.join(['%s'] * len(DEAD))}) "
            "ORDER BY id DESC LIMIT %s",
            (company, *DEAD, want * 6),
        )
        pool = cur.fetchall() or []
    if not pool:
        return []
    keys = [_key_for(row) for row in pool]
    done = _recently_traced(conn, keys)
    picked = []
    for row, key in zip(pool, keys):
        if key and key in done:
            continue
        picked.append(row)
        if len(picked) >= want:
            break
    return picked


def preview(conn, company: str = "construction") -> dict:
    """How many leads are ready, how many still need a ZIP, and the balance."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS total, "
            "  count(*) FILTER (WHERE COALESCE(TRIM(zip), '') = '') AS no_zip, "
            "  count(*) FILTER (WHERE COALESCE(TRIM(name), '') = '') AS no_name "
            "FROM leads WHERE company = %s AND COALESCE(address, '') <> '' "
            + _no_contact_clause() +
            f"  AND LOWER(COALESCE(status, 'new')) NOT IN ({','.join(['%s'] * len(DEAD))})",
            (company, *DEAD),
        )
        row = cur.fetchone() or {}
    total = int(row.get("total") or 0)
    bal = skip_sherpa_service.tracerfy_balance()
    per_hit = skip_sherpa_service.TRACERFY_CREDITS_PER_HIT
    return {
        "ready": total,
        "no_zip": int(row.get("no_zip") or 0),
        "no_name": int(row.get("no_name") or 0),
        "balance": (bal or {}).get("balance"),
        "max_credits": total * per_hit,
        "affordable": (((bal or {}).get("balance") or 0) // per_hit) if bal else None,
        "configured": skip_sherpa_service.configured(),
        "provider": skip_sherpa_service.provider_name(),
    }


def start(company: str = "construction", limit: int = 25) -> dict:
    with _LOCK:
        if _STATE["running"]:
            return {"started": False, "reason": "already running", **_STATE}
        try:
            n = max(1, min(int(limit or 25), 500))
        except (TypeError, ValueError):
            n = 25
        _STATE.update(running=True, total=n, examined=0, found=0, misses=0,
                      skipped=0, already_traced=0, credits_used=0, current="",
                      note="starting", started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                      finished_at=None, balance=None)
    threading.Thread(target=_run, args=(company, n), daemon=True).start()
    return {"started": True, **_STATE}


def _set_zip(conn, lead: dict) -> str:
    """Return a ZIP for this lead, filling it for free when it is missing.

    The column first, then the address string (most permits already carry one),
    and only then a geocoder call.
    """
    zipcode = str(lead.get("zip") or "").strip()[:5]
    if zipcode:
        return zipcode
    parsed = skiptrace_service.parse_address(lead.get("address") or "")
    if not parsed.get("ok"):
        return ""
    inline = str(parsed.get("zipcode") or "").strip()[:5]
    if inline:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE leads SET zip = %s "
                "WHERE id = %s AND (zip IS NULL OR BTRIM(zip) = '');",
                (inline, lead["id"]))
        conn.commit()
        return inline
    try:
        got = zip_lookup.lookup_zip(parsed["street"], parsed["city"], parsed["state"])
    except Exception:
        return ""
    zipcode = str((got or {}).get("zipcode") or "").strip()[:5]
    if zipcode:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE leads SET zip = %s, address = %s "
                "WHERE id = %s AND COALESCE(address, '') = %s;",
                (zipcode, f"{str(lead.get('address')).strip()} {zipcode}",
                 lead["id"], lead.get("address")))
        conn.commit()
    return zipcode


def _audit(conn, key: str, result: dict) -> None:
    """Record the paid lookup. Owner names are in here, so the trail matters."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO skiptrace_audit "
                "  (requested_by, address_key, found, owner_of_record, "
                "   is_residential, source, cached) "
                "VALUES (%s, %s, %s, %s, %s, 'tracerfy', FALSE);",
                (ATTEMPT_TAG, key, bool(result.get("found")),
                 str(result.get("owner") or "")[:255], None))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass


def _run(company: str, limit: int) -> None:
    conn = None
    try:
        conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
        try:
            with conn.cursor() as cur:
                skiptrace_service.ensure_schema(cur)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        rows = candidates(conn, company, limit)
        with _LOCK:
            _STATE["total"] = len(rows)
        if not rows:
            with _LOCK:
                _STATE["note"] = "no untraced leads ready to trace"
            return
        for i, lead in enumerate(rows, 1):
            with _LOCK:
                _STATE["current"] = str(lead.get("address") or "")[:60]
            parsed = skiptrace_service.parse_address(lead.get("address") or "")
            if not parsed.get("ok"):
                with _LOCK:
                    _STATE["skipped"] += 1
                    _STATE["examined"] = i
                continue
            zipcode = _set_zip(conn, lead)
            if not zipcode:
                with _LOCK:
                    _STATE["skipped"] += 1
                    _STATE["examined"] = i
                    _STATE["note"] = "skipped a lead with no resolvable ZIP"
                continue
            try:
                result = skip_sherpa_service.trace(
                    parsed["street"], parsed["city"], parsed["state"], zipcode)
            except skip_sherpa_service.ProviderError as exc:
                with _LOCK:
                    _STATE["note"] = f"provider stopped the run: {exc.message}"[:200]
                if exc.status in (401, 402, 403, 429):
                    break
                with _LOCK:
                    _STATE["skipped"] += 1
                    _STATE["examined"] = i
                continue
            except Exception as exc:  # noqa: BLE001
                try:
                    conn.rollback()
                except Exception:
                    pass
                with _LOCK:
                    _STATE["skipped"] += 1
                    _STATE["examined"] = i
                    _STATE["note"] = f"last error: {str(exc)[:140]}"
                continue

            _audit(conn, skiptrace_service.normalize_address(
                parsed["street"], parsed["city"], parsed["state"]), result)

            found = {
                "name": result.get("owner") or "",
                "email": result.get("email") or "",
                "phone": result.get("preferred_phone") or result.get("raw_phone") or "",
            }
            merged = data_enrichment.merge(lead, found)
            if merged.get("changed"):
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE leads SET name = %s, email = %s, phone = %s WHERE id = %s;",
                        (merged["name"][:255], merged["email"][:255],
                         merged["phone"][:50], lead["id"]))
                conn.commit()
                try:
                    from bizstack_leads import store as lead_store
                    lead_store.recompute(conn, lead["id"])
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
            with _LOCK:
                _STATE["examined"] = i
                _STATE["credits_used"] += int(result.get("credits_used") or 0)
                if result.get("found"):
                    _STATE["found"] += 1
                else:
                    _STATE["misses"] += 1
            time.sleep(0.05)
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            _STATE["note"] = f"failed: {exc}"[:200]
    finally:
        if conn is not None:
            try:
                conn.commit()
            except Exception:
                pass
            try:
                bal = skip_sherpa_service.tracerfy_balance(force=True)
                with _LOCK:
                    _STATE["balance"] = (bal or {}).get("balance")
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
        with _LOCK:
            _STATE["running"] = False
            _STATE["current"] = ""
            _STATE["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            if not _STATE["note"]:
                _STATE["note"] = "done"
