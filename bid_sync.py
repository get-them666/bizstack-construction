"""Nightly BuildingConnected -> bid board sync.

The owner should not have to press "Sync": invitations arrive on the board on
their own, and the Today screen shows what is due. Runs every
BID_SYNC_HOURS (default 6) in a daemon thread, only when the APS app is
configured and the owner has completed the Autodesk sign-in; otherwise it
logs once and waits. Every run is recorded in bid_pull_runs exactly like a
manual pull, so a failing sync is visible on /bids rather than silent.

DISABLE_BID_SYNC kills it (explicit parsing -- Railway's string "false" is
not ON).
"""
from __future__ import annotations

import os
import threading
import time

import psycopg
from psycopg.rows import dict_row


def _disabled() -> bool:
    return (os.getenv("DISABLE_BID_SYNC") or "").strip().lower() in ("1", "true", "yes", "on")


def run_once(owner_email: str, company: str = "construction") -> dict:
    import autodesk_oauth as ao
    import bid_opportunities
    import bid_sources

    if not ao.configured():
        return {"skipped": "Autodesk app not configured"}
    if not ao.get_tokens(owner_email):
        return {"skipped": "owner has not connected Autodesk yet"}
    url = os.getenv("DATABASE_URL", "")
    with psycopg.connect(url, row_factory=dict_row) as conn:
        bid_opportunities.ensure_schema(conn)
        run_id = bid_opportunities.start_pull(conn, "buildingconnected", company)
        try:
            rows = bid_sources.pull("buildingconnected", user_email=owner_email)
        except Exception as e:
            bid_opportunities.finish_pull(conn, run_id, ok=False, error=str(e)[:500])
            return {"ok": False, "error": str(e)[:200]}
        result = bid_opportunities.upsert_pulled(conn, rows, company)
        detail = f"{len(result['errors'])} row(s) rejected" if result["errors"] else None
        bid_opportunities.finish_pull(conn, run_id, fetched=len(rows), created=result["created"],
                                      updated=result["updated"], ok=True, error=detail)
        return {"ok": True, "fetched": len(rows), **{k: result[k] for k in ("created", "updated")}}


def start(owner_email_fn, interval_hours: float | None = None) -> threading.Thread | None:
    if _disabled():
        print("[bid-sync] disabled by DISABLE_BID_SYNC", flush=True)
        return None
    try:
        hours = float(interval_hours or os.getenv("BID_SYNC_HOURS", "6") or 6)
    except (TypeError, ValueError):
        hours = 6.0

    def _loop():
        # Let the app finish booting before the first pull.
        time.sleep(90)
        while True:
            try:
                res = run_once(owner_email_fn())
                print(f"[bid-sync] {res}", flush=True)
            except Exception as e:
                print(f"[bid-sync] failed: {e}", flush=True)
            time.sleep(max(1.0, hours) * 3600)

    t = threading.Thread(target=_loop, name="bid-sync", daemon=True)
    t.start()
    return t
