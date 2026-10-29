"""
Zoho CRM integration - Leads, Contacts, Deals (Pipeline)
"""
import brand
import os
from typing import Dict, Any, Optional, List
try:
    from . import zoho_client
except Exception:
    import zoho_client

DC_SUFFIX = os.getenv("ZOHO_DC_SUFFIX", "com")
CRM_BASE = f"https://www.zohoapis.{DC_SUFFIX}/crm/v8"


def upsert_lead(lead_data: Dict[str, Any]) -> Optional[Dict]:
    """Create or update a lead in Zoho CRM."""
    if not zoho_client.is_configured():
        return None
    
    # Map our fields to Zoho CRM fields
    zoho_payload = {
        "First_Name": lead_data.get("name", "").split()[0] if lead_data.get("name") else "",
        "Last_Name": " ".join(lead_data.get("name", "").split()[1:]) if lead_data.get("name") and len(lead_data.get("name", "").split()) > 1 else lead_data.get("name", "") or "Unknown",
        "Email": lead_data.get("email", ""),
        "Phone": lead_data.get("phone", ""),
        "Company": lead_data.get("company_name") or brand.name(),
        "Street": lead_data.get("address", ""),
        "City": lead_data.get("city", ""),
        "State": lead_data.get("state", ""),
        "Zip_Code": lead_data.get("zip_code", ""),
        "Description": lead_data.get("description", ""),
        "Lead_Source": lead_data.get("source", "Website"),
        "Lead_Status": _map_status(lead_data.get("status", "new")),
    }
    
    # Remove empty fields
    zoho_payload = {k: v for k, v in zoho_payload.items() if v}
    
    resp = zoho_client.api_request("POST", f"{CRM_BASE}/Leads/upsert", json={"data": [zoho_payload], "duplicate_check_fields": ["Email"]})
    if not resp or resp.status_code not in (200, 201):
        if resp:
            print(f"[zoho_crm] upsert_lead failed: {resp.status_code} {resp.text[:200]}", flush=True)
        return None
    
    try:
        return resp.json()
    except Exception as e:
        print(f"[zoho_crm] upsert_lead parse error: {e}", flush=True)
        return None


def create_deal(deal_data: Dict[str, Any], lead_id: Optional[str] = None) -> Optional[Dict]:
    """Create a deal in Zoho CRM pipeline."""
    if not zoho_client.is_configured():
        return None
    
    zoho_payload = {
        "Deal_Name": deal_data.get("name") or deal_data.get("deal_name") or f"Project - {deal_data.get('address', '')[:50]}",
        "Stage": _map_deal_stage(deal_data.get("status", "new")),
        "Amount": deal_data.get("amount") or deal_data.get("estimate_high"),
        "Closing_Date": deal_data.get("closing_date"),
        "Description": deal_data.get("description", ""),
        "Lead_Source": deal_data.get("source", "Website"),
    }
    
    if lead_id:
        zoho_payload["Contact_Name"] = {"id": lead_id}  # Can link to contact/lead
    
    zoho_payload = {k: v for k, v in zoho_payload.items() if v is not None and v != ""}
    
    resp = zoho_client.api_request("POST", f"{CRM_BASE}/Deals", json={"data": [zoho_payload]})
    if not resp or resp.status_code not in (200, 201):
        if resp:
            print(f"[zoho_crm] create_deal failed: {resp.status_code} {resp.text[:200]}", flush=True)
        return None
    
    try:
        return resp.json()
    except Exception as e:
        print(f"[zoho_crm] create_deal parse error: {e}", flush=True)
        return None


def update_deal(deal_id: str, updates: Dict[str, Any]) -> Optional[Dict]:
    """Update a deal in Zoho CRM."""
    if not zoho_client.is_configured():
        return None
    
    zoho_payload = {}
    if "status" in updates:
        zoho_payload["Stage"] = _map_deal_stage(updates["status"])
    if "amount" in updates:
        zoho_payload["Amount"] = updates["amount"]
    if "description" in updates:
        zoho_payload["Description"] = updates["description"]
    
    if not zoho_payload:
        return None
    
    resp = zoho_client.api_request("PUT", f"{CRM_BASE}/Deals/{deal_id}", json={"data": [zoho_payload]})
    if not resp or resp.status_code not in (200, 201):
        if resp:
            print(f"[zoho_crm] update_deal failed: {resp.status_code}", flush=True)
        return None
    
    try:
        return resp.json()
    except Exception as e:
        print(f"[zoho_crm] update_deal parse error: {e}", flush=True)
        return None


def create_contact(contact_data: Dict[str, Any]) -> Optional[Dict]:
    """Create/update a contact in Zoho CRM."""
    if not zoho_client.is_configured():
        return None
    
    zoho_payload = {
        "First_Name": contact_data.get("name", "").split()[0] if contact_data.get("name") else "",
        "Last_Name": " ".join(contact_data.get("name", "").split()[1:]) if contact_data.get("name") and len(contact_data.get("name", "").split()) > 1 else contact_data.get("name", "") or "Unknown",
        "Email": contact_data.get("email", ""),
        "Phone": contact_data.get("phone", ""),
        "Mailing_Street": contact_data.get("address", ""),
    }
    
    zoho_payload = {k: v for k, v in zoho_payload.items() if v}
    
    resp = zoho_client.api_request("POST", f"{CRM_BASE}/Contacts/upsert", json={"data": [zoho_payload], "duplicate_check_fields": ["Email"]})
    if not resp or resp.status_code not in (200, 201):
        if resp:
            print(f"[zoho_crm] create_contact failed: {resp.status_code}", flush=True)
        return None
    
    try:
        return resp.json()
    except Exception as e:
        print(f"[zoho_crm] create_contact parse error: {e}", flush=True)
        return None


def add_note(module: str, record_id: str, note_title: str, note_content: str) -> Optional[Dict]:
    """Add a note to a CRM record (Leads/Deals/Contacts)."""
    if not zoho_client.is_configured():
        return None
    
    zoho_payload = {
        "Note_Title": note_title,
        "Note_Content": note_content,
        "Parent_Id": {"id": record_id}
    }
    
    resp = zoho_client.api_request("POST", f"{CRM_BASE}/{module}/actions/add_notes", json={"data": [zoho_payload]})
    if not resp or resp.status_code not in (200, 201):
        if resp:
            print(f"[zoho_crm] add_note failed: {resp.status_code}", flush=True)
        return None
    
    try:
        return resp.json()
    except Exception as e:
        print(f"[zoho_crm] add_note parse error: {e}", flush=True)
        return None


def _map_status(status: str) -> str:
    """Map our lead status to Zoho CRM Lead_Status."""
    mapping = {
        "new": "Not Contacted",
        "contacted": "Contacted",
        "quoted": "Qualified",
        "deposit": "Qualified",
        "in_progress": "Qualified",
        "completed": "Converted",
        "lost": "Junk Lead"
    }
    return mapping.get(status, "Not Contacted")


def _map_deal_stage(status: str) -> str:
    """Map our status to Zoho CRM Deal Stage."""
    mapping = {
        "new": "Qualification",
        "contacted": "Needs Analysis",
        "quoted": "Proposal/Price Quote",
        "deposit": "Negotiation/Review",
        "in_progress": "Closed Won",
        "completed": "Closed Won",
        "lost": "Closed Lost"
    }
    return mapping.get(status, "Qualification")
