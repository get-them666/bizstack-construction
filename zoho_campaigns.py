"""
Zoho Campaigns integration - Email lists/subscribers
"""
import os
from typing import Dict, Any, Optional
try:
    from . import zoho_client
except Exception:
    import zoho_client

DC_SUFFIX = os.getenv("ZOHO_DC_SUFFIX", "com")
CAMPAIGNS_BASE = f"https://campaigns.zoho.{DC_SUFFIX}/api/v1.1"
LIST_KEY = os.getenv("ZOHO_CAMPAIGNS_LIST_KEY", "")


def add_subscriber(subscriber_data: Dict[str, Any]) -> Optional[Dict]:
    """Add a subscriber to a mailing list."""
    if not zoho_client.is_configured():
        return None
    if not LIST_KEY:
        print("[zoho_campaigns] ZOHO_CAMPAIGNS_LIST_KEY not set", flush=True)
        return None
    
    headers = zoho_client.get_headers()
    if not headers:
        return None
    
    # Zoho Campaigns expects form data or specific format
    name = subscriber_data.get("name", "")
    first_name = name.split()[0] if name else subscriber_data.get("first_name", "")
    last_name = " ".join(name.split()[1:]) if name and len(name.split()) > 1 else subscriber_data.get("last_name", "")
    
    payload = {
        "listkey": LIST_KEY,
        "contactinfo": {
            "First Name": first_name,
            "Last Name": last_name,
            "Email": subscriber_data.get("email", "")
        }
    }
    
    resp = zoho_client.api_request("POST", f"{CAMPAIGNS_BASE}/addlistsubscribers", json=payload, headers=headers)
    if not resp:
        return None
    if resp.status_code not in (200, 201):
        print(f"[zoho_campaigns] add_subscriber failed: {resp.status_code} {resp.text[:200]}", flush=True)
        return None
    
    try:
        return resp.json()
    except Exception as e:
        print(f"[zoho_campaigns] add_subscriber parse error: {e}", flush=True)
        return None


def unsubscribe(email: str) -> Optional[Dict]:
    """Unsubscribe from mailing list."""
    if not zoho_client.is_configured() or not LIST_KEY:
        return None
    
    headers = zoho_client.get_headers()
    if not headers:
        return None
    
    payload = {
        "listkey": LIST_KEY,
        "contactinfo": {
            "Email": email
        }
    }
    
    resp = zoho_client.api_request("POST", f"{CAMPAIGNS_BASE}/removelistsubscribers", json=payload, headers=headers)
    if not resp or resp.status_code not in (200, 201):
        if resp:
            print(f"[zoho_campaigns] unsubscribe failed: {resp.status_code}", flush=True)
        return None
    
    try:
        return resp.json()
    except Exception as e:
        print(f"[zoho_campaigns] unsubscribe parse error: {e}", flush=True)
        return None
