"""Resolve a permit lead from its placeholder tracking email.

WHY THIS IS A DATABASE LOOKUP AND NOT AN API CALL
-------------------------------------------------
The tracking email is `con-permit-<digest>@lead.local`, where digest is
`sha256(permit_number)[:15]` (see construction_main.py `_ingest_permits`).
That digest cannot be reversed -- there is no function that turns it back
into a permit number. So an earlier draft of this tried to POST it to
Shovels and could never have worked, for three separate reasons:

  1. `https://app.shovels.ai` is the web dashboard, not the API. The API is
     `https://api.shovels.ai/v2` (permit_service.API_BASE).
  2. It dropped `/v2`.
  3. It sent `Authorization: Bearer`; Shovels expects `X-API-Key`
     (permit_service.py), and addresses permits by `?id=<mongo _id>` -- a
     sha256 of a permit number will never match that.

It does not need to. The full permit payload is written into
`leads.analysis_json` at ingest time, so the digest is a lookup key into
our own database. No API key, no network, works offline, cannot be rate
limited, and costs nothing.

WHAT THIS ACTUALLY RETURNS
--------------------------
Address, work type, permit number and estimated value. NOT a homeowner
name or phone: neither the Virginia Beach nor the Norfolk open feed
publishes an applicant field (see MEMORY.md). `contractor_name` is empty
on most rows for the same reason. An empty value here is "unpublished",
not "we failed to find it" -- do not read it as a gap to be filled.
"""

import json
import os
import re
from typing import Dict, Optional

# con-permit-<15 hex>@lead.local
_TRACKING_RE = re.compile(r"^con-permit-([a-f0-9]{15})@lead\.local$")


def parse_tracking_email(email_address: str) -> Optional[str]:
    """Extract the 15-hex digest from a tracking email, or None."""
    if not email_address:
        return None
    m = _TRACKING_RE.match(email_address.lower().strip())
    return m.group(1) if m else None


def _payload(row: Dict) -> Dict:
    """Pull the address out of analysis_json, falling back to the column."""
    raw = row.get("analysis_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    return raw if isinstance(raw, dict) else {}


def enrich(email_address: str) -> Dict:
    """Resolve one permit tracking email into its permit detail.

    Returns {"status": "error", ...} for anything unresolvable rather than
    raising, so a caller enriching a batch of leads is never stopped by one
    bad row.
    """
    digest = parse_tracking_email(email_address)
    if not digest:
        return {"status": "error", "message": "Invalid tracking email format"}

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover
        return {"status": "error", "message": f"psycopg unavailable: {exc}"}

    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        return {"status": "error", "message": "DATABASE_URL is not set"}

    try:
        with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10) as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT id, name, address, status, analysis_json FROM leads "
                    "WHERE email = %s ORDER BY id",
                    (f"con-permit-{digest}@lead.local",),
                )
                rows = cur.fetchall()
    except Exception as exc:
        return {"status": "error", "message": f"Lookup failed: {exc}"}

    if not rows:
        return {"status": "error", "message": f"No lead for digest {digest}"}

    # Several rows can share one digest (permits re-ingested before dedupe was
    # added). Return the newest and say how many matched rather than pretending
    # it was unique -- the caller may be counting doors.
    row = rows[-1]
    meta = _payload(row)
    address = (row.get("address") or meta.get("address") or "").strip()

    return {
        "status": "success",
        "source_id": digest,
        "lead_ids": [r["id"] for r in rows],
        "matched_rows": len(rows),
        "contractor_name": meta.get("contractor_name") or "Unpublished by city feed",
        "property_address": address or "N/A",
        "city": meta.get("city") or "",
        "permit_number": meta.get("permit_number") or row.get("name") or "",
        "permit_id": meta.get("permit_id") or "",
        "job_valuation": meta.get("value") or 0,
        "permit_type": meta.get("work_type") or "Unknown",
        "lead_status": row.get("status") or "",
    }


if __name__ == "__main__":
    import sys

    for addr in (sys.argv[1:] or ["con-permit-422d36ec92d018b@lead.local"]):
        print(json.dumps(enrich(addr), indent=2))