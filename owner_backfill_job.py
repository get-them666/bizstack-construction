"""One-button owner backfill for every lead that has an address and no name.

The Skip Trace tool capped a run at 10-25 addresses because it used to spend
metered RentCast calls. Owner names now come from the free city assessors
(Norfolk Socrata, Virginia Beach) and HRGEO, so the cap only exists to keep an
HTTP request from running for hours.

This runs the same waterfall in a background thread and reports progress, so
the owner clicks once and watches it work instead of pressing a button ten
times. Only names are written, and only over a placeholder -- the compare-and-
swap on the name we read means a real person is never overwritten.

Runs are single-flight: a second click while one is running returns the live
progress rather than starting a duplicate pass over the same rows.
"""
from __future__ import annotations

import os
import threading
import time

import psycopg
from psycopg.rows import dict_row

import skiptrace_service as st

_LOCK = threading.Lock()
_STATE = {
    "running": False,
    "total": 0,
    "examined": 0,
    "updated": 0,
    "skipped": 0,
    "current": "",
    "started_at": None,
    "finished_at": None,
    "note": "",
}

DEAD = ("won", "lost", "closed", "cancelled", "canceled", "archived")


def status() -> dict:
    with _LOCK:
        return dict(_STATE)


def _candidates(cur, company: str) -> list:
    """Leads with an address whose name is still a placeholder.

    is_placeholder_name also accepts a bare city ("Virginia Beach"), which the
    permit feed writes, so the SQL can only pre-filter the obvious shapes;
    the final test is in Python.
    """
    cur.execute(
        "SELECT id, name, address FROM leads "
        "WHERE company = %s AND COALESCE(address, '') <> '' "
        f"  AND COALESCE(status, 'new') NOT IN ({','.join(['%s'] * len(DEAD))}) "
        "ORDER BY id DESC",
        (company, *DEAD),
    )
    return [r for r in cur.fetchall()
            if st.is_placeholder_name(r.get("name"))]


def start(company: str = "construction", max_leads: int = 0) -> dict:
    """Kick off a pass. Returns immediately; poll status() for progress."""
    with _LOCK:
        if _STATE["running"]:
            return {"started": False, "reason": "already running", **_STATE}
        _STATE.update(running=True, total=0, examined=0, updated=0, skipped=0,
                      current="", started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                      finished_at=None, note="starting")
    threading.Thread(target=_run, args=(company, max_leads), daemon=True).start()
    return {"started": True, **status()}


def _run(company: str, max_leads: int) -> None:
    conn = None
    try:
        conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
        with conn.cursor() as cur:
            rows = _candidates(cur, company)
        if max_leads and max_leads > 0:
            rows = rows[:max_leads]
        with _LOCK:
            _STATE["total"] = len(rows)
        if not rows:
            with _LOCK:
                _STATE["note"] = "every lead with an address already has an owner name"
            return

        for i, row in enumerate(rows, 1):
            parsed = st.parse_address(row.get("address") or "")
            if not parsed.get("ok"):
                with _LOCK:
                    _STATE["skipped"] += 1
                    _STATE["examined"] = i
                continue
            with _LOCK:
                _STATE["current"] = str(row.get("address") or "")[:60]
            try:
                # lookup() is cursor-based: it reads and writes the cache
                # through the object it is given. Passing the connection made
                # every address fail with "'Connection' object has no attribute
                # 'fetchone'" and the whole run count as "no record".
                with conn.cursor() as cur:
                    result = st.lookup(
                        cur, parsed["street"], parsed["city"],
                        parsed["state"], parsed["zipcode"],
                        requested_by="owner_backfill_all")
                conn.commit()
                owner = str((result or {}).get("owner_of_record") or "").strip()
                if not owner or st.is_placeholder_name(owner):
                    with _LOCK:
                        _STATE["skipped"] += 1
                else:
                    with conn.cursor() as cur:
                        # Compare-and-swap on the exact name we judged. If the
                        # card was edited in the meantime the name no longer
                        # matches and nothing is written.
                        cur.execute(
                            "UPDATE leads SET name = %s WHERE id = %s "
                            "  AND COALESCE(name, '') = %s RETURNING id;",
                            (owner[:255], row["id"], row.get("name") or ""))
                        touched = cur.rowcount or 0
                    conn.commit()
                    if touched:
                        with _LOCK:
                            _STATE["updated"] += 1
                        try:
                            from bizstack_leads import store as lead_store
                            lead_store.recompute(conn, row["id"])
                        except Exception:
                            pass
            except st.ProviderError as exc:
                # A provider-wide stop (429, outage) would fail every remaining
                # address the same way; end the run with the reason instead.
                if exc.status in (429, 401, 403) or "budget" in str(exc).lower():
                    with _LOCK:
                        _STATE["note"] = f"provider stopped the run: {exc.message}"[:200]
                    break
                with _LOCK:
                    _STATE["skipped"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad address must not end the pass
                # A failed statement leaves the transaction aborted; without
                # this every later address in the run fails on "current
                # transaction is aborted".
                try:
                    conn.rollback()
                except Exception:
                    pass
                with _LOCK:
                    _STATE["skipped"] += 1
                    _STATE["note"] = f"last error: {str(exc)[:140]}"
            with _LOCK:
                _STATE["examined"] = i
            # The Virginia Beach assessor allows 1000 requests/hour and each
            # address costs up to three; a short pause keeps a full backfill
            # inside that budget without making the run feel stuck.
            time.sleep(0.15)
    except Exception as exc:  # noqa: BLE001 - the thread must always clear `running`
        with _LOCK:
            _STATE["note"] = f"failed: {exc}"[:200]
    finally:
        if conn is not None:
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
