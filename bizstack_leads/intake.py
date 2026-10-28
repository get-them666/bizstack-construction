"""One way for a lead to enter the pipeline.

Every source -- the website form, the finance page, radar (Reddit/LinkedIn),
SAM.gov contracts, the permit importer, skiptrace, inbound email/SMS/voice --
calls :func:`intake_lead`. None of them decides its own status, its own dedupe,
or its own "should we auto-reply". That was the root of the mess: nine entry
points, nine behaviours.

Intake does exactly four things, in order:

1. Normalise the payload (phone/email tidy, missing phone becomes ``""`` so the
   shared ``NOT NULL`` column accepts a permit that has no phone yet).
2. Deduplicate against what we already hold.
3. Insert the new lead, or merge the new facts into the existing row.
4. Recompute the queue state, so the lead lands in the right work bucket.

It never sends anything. Sending is the owner's job (review mode) or the
outreach sweep's, both of which read the state this function writes.
"""
from __future__ import annotations

import re

from . import lifecycle, store


def normalise_phone(value) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    digits = re.sub(r"[^\d+]", "", value)
    return digits


def normalise_email(value) -> str:
    return (value or "").strip().lower()


def _sync_to_zoho_comprehensive(lead_data, created=False):
    """Sync lead to all Zoho services - CRM, Books, Campaigns."""
    try:
        # Try new comprehensive modules first
        try:
            import zoho_crm
            import zoho_books
            import zoho_campaigns
            
            # Sync to CRM as Lead
            zoho_crm.upsert_lead(lead_data)
            
            # Create Deal in CRM if it's a real project lead
            if lead_data.get("project_type") or lead_data.get("description"):
                zoho_crm.create_deal({
                    "name": lead_data.get("name") or "Project",
                    "description": lead_data.get("description", ""),
                    "status": lead_data.get("status", "new"),
                    "source": lead_data.get("source", "Website"),
                    "address": lead_data.get("address", "")
                })
            
            # Sync to Books as Customer
            zoho_books.create_customer(lead_data)
            
            # Add to Campaigns if email provided
            if lead_data.get("email"):
                zoho_campaigns.add_subscriber(lead_data)
                
        except Exception as e:
            print(f"[zoho_sync] Comprehensive sync error: {e}", flush=True)
            # Fallback to old zoho_sync
            try:
                import zoho_sync
                zoho_sync.send_to_zoho_crm({
                    "First_Name": lead_data.get("name", "").split(" ")[0] if lead_data.get("name") else "",
                    "Last_Name": " ".join(lead_data.get("name", "").split(" ")[1:]) if lead_data.get("name") and " " in lead_data.get("name", "") else lead_data.get("name", "") or "",
                    "Email": lead_data.get("email", ""),
                    "Phone": lead_data.get("phone", ""),
                    "Street": lead_data.get("address", ""),
                    "Description": lead_data.get("description", ""),
                    "Lead_Source": lead_data.get("source", "Website"),
                    "Company": "BuildStack Construction",
                })
            except Exception:
                pass
    except Exception:
        pass


def intake_lead(db, *, company, source, name="", phone="", email="", address="",
                description="", project_type="", source_ref=None, status=None,
                extra=None) -> dict:
    """Insert or merge one lead. Returns ``{lead_id, created, next_action}``.

    ``source_ref`` is the strongest identity we have -- pass it for anything
    that can be seen twice (a permit number, a post id, a Stripe session). When
    it is absent we fall back to email/phone/(name,address) matching.
    """
    phone = normalise_phone(phone)
    email = normalise_email(email)
    name = (name or "").strip()
    address = (address or "").strip()

    existing = store.find_duplicate(
        db, company, source_ref=source_ref, email=email, phone=phone,
        name=name, address=address,
    )

    if existing:
        # Merge: fill in blanks the earlier row did not have, but never throw
        # away a value we already hold with an empty one from a thinner source.
        merged = {}
        for field, value in (
            ("name", name), ("phone", phone), ("email", email),
            ("address", address), ("description", description),
            ("project_type", project_type), ("source_ref", source_ref),
        ):
            if value and not (existing.get(field) or "").strip():
                merged[field] = value
        if merged:
            store.update_lead_fields(db, existing["id"], merged)
        lead_data = {
            "name": name or existing.get("name", ""),
            "email": email or existing.get("email", ""),
            "phone": phone or existing.get("phone", ""),
            "address": address or existing.get("address", ""),
            "description": description or existing.get("description", ""),
            "source": source,
            "status": existing.get("status", "new"),
            "project_type": project_type or existing.get("project_type", ""),
        }
        _sync_to_zoho_comprehensive(lead_data, created=False)
        lead = store.recompute(db, existing["id"])
        return {
            "lead_id": existing["id"], "created": False, "merged": bool(merged),
            "next_action": lead.get("next_action"),
        }

    fields = {
        "name": name, "phone": phone, "email": email, "address": address,
        "description": description, "project_type": project_type,
        "source": source, "company": company, "source_ref": source_ref,
        "status": lifecycle.to_stage(status) if status else lifecycle.NEW,
    }
    if extra:
        fields.update(extra)

    lead_id = store.insert_lead(db, fields)
    lead_data = {
        "name": name,
        "email": email,
        "phone": phone,
        "address": address,
        "description": description,
        "source": source,
        "status": fields["status"],
        "project_type": project_type,
    }
    _sync_to_zoho_comprehensive(lead_data, created=True)
    lead = store.recompute(db, lead_id)
    return {
        "lead_id": lead_id, "created": True, "merged": False,
        "next_action": lead.get("next_action"),
    }
