"""Zoho CRM + Campaigns sync for leads.

One chokepoint for everything this app tells Zoho about a lead, so the rules
live in one place:

- Only leads with a real way to reach them go to CRM. Permit imports carry
  placeholder `…@lead.local` emails and no phone; syncing them put hundreds
  of address-only "Not Contacted" records in the CRM (the three newest CRM
  leads on 2026-10-29 were all permits). A permit lead syncs the moment
  enrichment gives it a real email or phone.
- The CRM record id is kept on the lead (leads.zoho_lead_id) so later status
  changes, notes and the won/lost outcome update THAT record instead of
  creating another one. Upsert on Email is still used for the first write
  so a homeowner who filled the form twice is one CRM lead.
- Campaigns gets inbound leads only (they asked to hear from us), to the
  list picked on Settings. A STOP unsubscribes them there too.

Every function is fail-soft and logs; a Zoho hiccup never costs a lead.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

import requests

try:
    import zoho_client
    import zoho_crm
    import zoho_campaigns
except Exception:  # pragma: no cover
    zoho_client = zoho_crm = zoho_campaigns = None  # type: ignore

DC_SUFFIX = os.getenv("ZOHO_DC_SUFFIX", "com")
CRM_BASE = f"https://www.zohoapis.{DC_SUFFIX}/crm/v8"
MCP_TOKEN_URL = os.getenv("ZOHO_MCP_TOKEN_URL", "")

STATUS_TO_CRM = {
    "new": "Not Contacted", "contacted": "Contacted", "engaged": "Contact in Future",
    "quoted": "Pre-Qualified", "won": "Qualified", "lost": "Lost Lead", "junk": "Junk Lead",
}

_SCHEMA_READY = False


def get_zoho_access_token():
    return zoho_client.get_access_token() if zoho_client else None


def ensure_schema(conn, force: bool = False) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS zoho_lead_id VARCHAR(40);")
        cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS zoho_synced_at TIMESTAMP WITH TIME ZONE;")
        cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS zoho_campaign_subscribed_at TIMESTAMP WITH TIME ZONE;")
    conn.commit()
    _SCHEMA_READY = True


def reachable(lead: dict) -> bool:
    email = (lead.get("email") or "").strip().lower()
    phone = "".join(ch for ch in (lead.get("phone") or "") if ch.isdigit())
    return (bool(email) and not email.endswith("@lead.local")) or len(phone) >= 10


def _inbound(source: str) -> bool:
    try:
        import followups
        return (source or "").strip().lower() in followups.inbound_sources()
    except Exception:
        return (source or "").lower() in ("website", "quotes_page", "financing", "acquisition", "embed_quote",
                                          "instant_quote", "sandbox", "ai-assistant", "chat", "referral")


def _load(conn, lead_id: int) -> Optional[dict]:
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, email, phone, address, project_type, status, source, notes, "
            "created_at, zoho_lead_id, zoho_campaign_subscribed_at, walkthrough_at "
            "FROM leads WHERE id = %s;", (lead_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def _payload(lead: dict) -> Dict[str, Any]:
    name = (lead.get("name") or "").strip()
    parts = name.split()
    email = (lead.get("email") or "").strip()
    if email.lower().endswith("@lead.local"):
        email = ""
    desc = (lead.get("notes") or "").strip()
    if lead.get("project_type"):
        desc = f"Project: {lead['project_type']}\n{desc}".strip()
    out = {
        "First_Name": parts[0] if len(parts) > 1 else "",
        "Last_Name": " ".join(parts[1:]) if len(parts) > 1 else (name or "Homeowner"),
        "Email": email,
        "Phone": (lead.get("phone") or "").strip(),
        "Company": "Homeowner",
        "Street": (lead.get("address") or "").strip(),
        "Description": desc[:32000],
        "Lead_Source": (lead.get("source") or "website").strip(),
        "Lead_Status": STATUS_TO_CRM.get((lead.get("status") or "new").lower(), "Not Contacted"),
    }
    return {k: v for k, v in out.items() if v}


def sync_lead(conn, lead_id: int, *, force: bool = False) -> Optional[str]:
    """Create or update the CRM record for a lead. Returns the Zoho id or None."""
    if not zoho_client or not zoho_client.is_configured():
        return None
    lead = _load(conn, lead_id)
    if not lead:
        return None
    if not reachable(lead) and not force:
        return None
    payload = _payload(lead)
    zid = lead.get("zoho_lead_id")
    try:
        if zid:
            resp = zoho_client.api_request("PUT", f"{CRM_BASE}/Leads/{zid}", json={"data": [payload]})
            if resp is not None and resp.status_code == 404:
                zid = None  # deleted on the CRM side; recreate below
        if not zid:
            body: Dict[str, Any] = {"data": [payload]}
            if payload.get("Email"):
                body["duplicate_check_fields"] = ["Email"]
            resp = zoho_client.api_request("POST", f"{CRM_BASE}/Leads/upsert", json=body)
            if resp is None or resp.status_code not in (200, 201):
                print(f"[zoho] CRM upsert failed for lead {lead_id}: "
                      f"{getattr(resp, 'status_code', '?')} {getattr(resp, 'text', '')[:200]}", flush=True)
                return None
            data = (resp.json().get("data") or [{}])[0]
            zid = ((data.get("details") or {}).get("id")) or data.get("id")
        if zid:
            with conn.cursor() as cur:
                cur.execute("UPDATE leads SET zoho_lead_id = %s, zoho_synced_at = NOW() WHERE id = %s;", (str(zid), lead_id))
            conn.commit()
        return str(zid) if zid else None
    except Exception as exc:
        print(f"[zoho] CRM sync error for lead {lead_id}: {exc}", flush=True)
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def sync_status(conn, lead_id: int) -> bool:
    """Push the current stage to the CRM record (creating it if it now qualifies)."""
    return bool(sync_lead(conn, lead_id))


def add_note(conn, lead_id: int, title: str, body: str) -> bool:
    if not zoho_client or not zoho_crm:
        return False
    lead = _load(conn, lead_id)
    if not lead:
        return False
    zid = lead.get("zoho_lead_id") or sync_lead(conn, lead_id)
    if not zid:
        return False
    try:
        return bool(zoho_crm.add_note("Leads", str(zid), title[:120], body[:30000]))
    except Exception as exc:
        print(f"[zoho] note failed for lead {lead_id}: {exc}", flush=True)
        return False


def on_booking(conn, lead_id: int, label: str, note: str = "") -> None:
    """A walkthrough was booked: CRM status + a note with the time."""
    sync_lead(conn, lead_id)
    add_note(conn, lead_id, "Walkthrough booked", f"{label}\n{note or ''}".strip())


def subscribe_lead(conn, lead_id: int) -> bool:
    """Inbound leads with a real email join the Campaigns list once."""
    if not zoho_campaigns:
        return False
    lead = _load(conn, lead_id)
    if not lead or lead.get("zoho_campaign_subscribed_at"):
        return False
    email = (lead.get("email") or "").strip()
    if not email or email.lower().endswith("@lead.local") or not _inbound(lead.get("source") or ""):
        return False
    try:
        if zoho_campaigns.add_subscriber({"name": lead.get("name") or "", "email": email}):
            with conn.cursor() as cur:
                cur.execute("UPDATE leads SET zoho_campaign_subscribed_at = NOW() WHERE id = %s;", (lead_id,))
            conn.commit()
            return True
    except Exception as exc:
        print(f"[zoho] campaigns subscribe failed for lead {lead_id}: {exc}", flush=True)
    return False


def on_opt_out(conn, lead_id: int) -> None:
    lead = _load(conn, lead_id)
    if not lead:
        return
    email = (lead.get("email") or "").strip()
    if email and zoho_campaigns and not email.lower().endswith("@lead.local"):
        try:
            zoho_campaigns.unsubscribe(email)
        except Exception as exc:
            print(f"[zoho] campaigns unsubscribe failed for lead {lead_id}: {exc}", flush=True)
    if lead.get("zoho_lead_id"):
        add_note(conn, lead_id, "Opted out", "Replied STOP — do not contact.")


def after_intake(conn, lead_id: int) -> None:
    """Call after a lead is created or gains contact details: CRM + Campaigns."""
    try:
        if sync_lead(conn, lead_id):
            subscribe_lead(conn, lead_id)
    except Exception as exc:
        print(f"[zoho] after_intake failed for lead {lead_id}: {exc}", flush=True)


# --- back-compat for the old call sites -----------------------------------------

def send_to_zoho_crm(lead_payload):
    """Legacy entry point: push a raw payload. Kept for callers not yet moved to
    after_intake(). Skips placeholder addresses the same way."""
    email = str((lead_payload or {}).get("Email") or "").lower()
    phone = "".join(ch for ch in str((lead_payload or {}).get("Phone") or "") if ch.isdigit())
    if (not email or email.endswith("@lead.local")) and len(phone) < 10:
        return None
    try:
        return zoho_crm.upsert_lead({
            "name": f"{lead_payload.get('First_Name', '')} {lead_payload.get('Last_Name', '')}".strip(),
            "email": "" if email.endswith("@lead.local") else lead_payload.get("Email", ""),
            "phone": lead_payload.get("Phone", ""), "address": lead_payload.get("Street", ""),
            "description": lead_payload.get("Description", ""), "source": lead_payload.get("Lead_Source", "website"),
            "company_name": "Homeowner",
        })
    except Exception as e:
        print(f"[zoho_sync] CRM sync failed: {e}", flush=True)
        return None
