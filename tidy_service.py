import os
import json
import urllib.request
import urllib.error
import leads_db

TIDY_API_KEY = os.getenv("TIDY_API_KEY", "")
TIDY_BASE_URL = "https://tidy.com"


def _tidy_post(endpoint: str, payload: dict) -> dict:
    if not TIDY_API_KEY:
        return {"status": "skipped", "reason": "TIDY_API_KEY not set"}

    req = urllib.request.Request(
        f"{TIDY_BASE_URL}{endpoint}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {TIDY_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"status": "ok", "data": json.loads(resp.read().decode("utf-8"))}
    except urllib.error.HTTPError as e:
        return {"status": "error", "code": e.code, "detail": e.read().decode("utf-8")}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def enrich_and_sync_tidy(
    *,
    lead_id: int | str = "",
    name: str = "",
    phone: str = "",
    email: str = "",
    address: str = "",
    project_type: str = "",
    goal: str = "",
    budget: str = "",
    timeline: str = "",
    message: str = "",
) -> dict:
    """Enriches an inbound website lead via PDL/location parser and imports it into TIDY."""
    enriched = {}
    if address:
        try:
            import enrichment
            if enrichment.pdl_enabled():
                enriched = enrichment._enrich_address(address) or {}
        except Exception as e:
            print(f"⚠️ TIDY PDL address enrich failed: {e}", flush=True)

    final_name = (name or enriched.get("name") or "Website Lead").strip()
    final_phone = (phone or enriched.get("phone") or "").strip()
    final_email = (email or enriched.get("email") or "").strip()

    city, state, zip_code = "", "VA", ""
    try:
        parsed = leads_db._parse_location(address or "")
        if len(parsed) >= 3:
            city, state, zip_code = parsed[0] or "", parsed[1] or "VA", parsed[2] or ""
    except Exception:
        pass

    is_str = any(
        k in f"{project_type} {goal} {message}".lower()
        for k in ("short-term", "airbnb", "vrbo", "turnover", "rental", "broom")
    )
    division = "Broom Service STR" if is_str else "Post-Reno Clean"

    addr_res = _tidy_post(
        "/addresses",
        {
            "street": address or "TBD Address",
            "city": city or "Chesapeake",
            "state": state or "VA",
            "zip": zip_code or "23321",
            "nickname": f"{final_name} ({division} #{lead_id})",
        },
    )

    msg_res = _tidy_post(
        "/message-tidy",
        {
            "message": (
                f"New {division} lead (#{lead_id}) from {final_name} "
                f"({final_phone} | {final_email}). Address: {address}. "
                f"Project: {project_type} | Goal: {goal} | Budget: {budget} | "
                f"Timeline: {timeline}. Notes: {message}"
            )
        },
    )

    print(f"🧹 [tidy] synced lead #{lead_id} ({division}) -> addr={addr_res.get('status')}, msg={msg_res.get('status')}", flush=True)
    return {"division": division, "enriched": enriched, "address": addr_res, "message": msg_res}


def record_tidy_event(payload: dict) -> dict:
    """Stores outbound TIDY job/status webhook events in leads.db."""
    import sqlite3

    event_type = str(payload.get("event") or payload.get("type") or "unknown")
    conn = sqlite3.connect("leads.db")
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tidy_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                payload_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO tidy_events (event_type, payload_json) VALUES (?, ?)",
            (event_type, json.dumps(payload)),
        )
        conn.commit()
    finally:
        conn.close()
    return {"recorded": True, "event_type": event_type}
