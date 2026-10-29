"""Zoho Campaigns: mailing-list subscribe / unsubscribe, list discovery.

The Campaigns API is form/query based, not JSON-bodied: `listkey` and
`contactinfo` (itself a JSON string) go as parameters with `resfmt=JSON`.
The previous version posted a JSON body to /addlistsubscribers, which the
API ignores, so nothing was ever added -- and the list key was never set
anyway. The list key now comes from app_settings (picked on Settings) with
ZOHO_CAMPAIGNS_LIST_KEY as the fallback.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

try:
    from . import zoho_client  # type: ignore
except Exception:
    import zoho_client

DC_SUFFIX = os.getenv("ZOHO_DC_SUFFIX", "com")
CAMPAIGNS_BASE = f"https://campaigns.zoho.{DC_SUFFIX}/api/v1.1"
LIST_KEY_SETTING = "zoho_campaigns_list_key"


def _setting(key: str) -> str:
    try:
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(os.getenv("DATABASE_URL", ""), row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_settings WHERE key = %s;", (key,))
                row = cur.fetchone()
        return (row or {}).get("value") or ""
    except Exception:
        return ""


def list_key() -> str:
    return _setting(LIST_KEY_SETTING) or (os.getenv("ZOHO_CAMPAIGNS_LIST_KEY") or "").strip()


def set_list_key(conn, key: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();",
            (LIST_KEY_SETTING, (key or "").strip()))
    conn.commit()


def _call(path: str, params: Dict[str, Any]) -> Optional[Dict]:
    if not zoho_client.is_configured():
        return None
    params = {**params, "resfmt": "JSON"}
    resp = zoho_client.api_request("POST", f"{CAMPAIGNS_BASE}/{path}", params=params,
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    if resp is None:
        return None
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:300]}
    if resp.status_code not in (200, 201) or str(body.get("status", "")).lower() == "error":
        print(f"[zoho_campaigns] {path} failed: {resp.status_code} {str(body)[:200]}", flush=True)
        return None
    return body


def mailing_lists() -> List[Dict[str, Any]]:
    """[{listkey, listname, contacts}] for the Settings picker."""
    if not zoho_client.is_configured():
        return []
    resp = zoho_client.api_request("GET", f"{CAMPAIGNS_BASE}/getmailinglists",
                                   params={"resfmt": "JSON", "range": 100})
    if resp is None or resp.status_code != 200:
        return []
    try:
        rows = resp.json().get("list_of_details") or []
    except Exception:
        return []
    return [{"listkey": r.get("listkey"), "listname": r.get("listname"),
             "contacts": r.get("noofcontacts")} for r in rows if r.get("listkey")]


def add_subscriber(subscriber_data: Dict[str, Any]) -> Optional[Dict]:
    """Add one contact to the configured list."""
    key = list_key()
    if not key:
        print("[zoho_campaigns] no mailing list selected (Settings → Zoho)", flush=True)
        return None
    email = (subscriber_data.get("email") or "").strip()
    if not email:
        return None
    name = (subscriber_data.get("name") or "").strip()
    parts = name.split()
    info = {"Contact Email": email}
    if parts:
        info["First Name"] = parts[0]
    if len(parts) > 1:
        info["Last Name"] = " ".join(parts[1:])
    return _call("json/listsubscribe", {"listkey": key, "contactinfo": json.dumps(info)})


def unsubscribe(email: str) -> Optional[Dict]:
    key = list_key()
    if not key or not (email or "").strip():
        return None
    return _call("json/listunsubscribe", {"listkey": key, "contactinfo": json.dumps({"Contact Email": email.strip()})})
