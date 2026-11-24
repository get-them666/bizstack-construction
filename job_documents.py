"""Universal SOW, contract, email, and portal provisioning for any job source.

This module generalizes the /bids pipeline so it works from three entry points:
  1. Bid Board (bid_opportunities) — already wired in construction_main.py
  2. Converted Leads → Projects (projects table linked to leads)
  3. Active Projects without a bid

Every function here takes a normalized job dict, not a bid-specific ORM row.
The caller (route handler) is responsible for loading the right data from the
right table and shaping it into the dict this module expects.

The normalized dict shape (all keys optional except id and name):
  {
    "id": int,            # bid_id or project_id
    "source": str,        # "bid", "project", "lead"
    "name": str,          # job title
    "location": str,      # property address
    "contact_name": str,  # client/contractor name
    "contact_email": str, # client/contractor email
    "contact_phone": str, # client/contractor phone
    "bid_amount": float,  # total contract price
    "owner": str,         # GC / property owner name
    "description": str,   # scope summary
  }
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("construction")


def _build_job_dict_from_project(project: dict, lead: dict | None = None) -> dict:
    """Shape a projects row (+ optional leads row) into the normalized dict."""
    lead = lead or {}
    return {
        "id": project["id"],
        "source": "project",
        "name": project.get("name") or lead.get("project_type") or "Project",
        "location": project.get("address") or lead.get("address") or "",
        "contact_name": lead.get("name") or "",
        "contact_email": lead.get("email") or "",
        "contact_phone": lead.get("phone") or "",
        "bid_amount": float(project.get("contract_value") or project.get("bid_amount") or 0),
        "owner": lead.get("name") or "",
        "description": project.get("summary") or lead.get("description") or "",
    }


def _build_job_dict_from_lead(lead: dict) -> dict:
    """Shape a leads row into the normalized dict (for leads without a project)."""
    return {
        "id": lead["id"],
        "source": "lead",
        "name": f"{lead.get('project_type') or 'Project'} — {lead.get('name') or 'Client'}",
        "location": lead.get("address") or "",
        "contact_name": lead.get("name") or "",
        "contact_email": lead.get("email") or "",
        "contact_phone": lead.get("phone") or "",
        "bid_amount": float(lead.get("estimate_high_cents") or 0) / 100 if lead.get("estimate_high_cents") else 0,
        "owner": lead.get("name") or "",
        "description": lead.get("description") or "",
    }


def generate_sow(job: dict, costs: list[dict], photos: list | None = None) -> bytes:
    """Generate a Scope of Work PDF from a normalized job dict."""
    import build_bid_documents
    return build_bid_documents.build_scope_of_work(job, costs, photos or [])


def generate_contract(job: dict, costs: list[dict], photos: list | None = None,
                      signature: dict | None = None,
                      client_signature: dict | None = None) -> bytes:
    """Generate a Renovation Agreement PDF from a normalized job dict."""
    import build_bid_documents
    return build_bid_documents.build_contract(job, costs, photos or [],
                                              signature=signature,
                                              client_signature=client_signature)


def generate_itemized_pricing(job: dict, costs: list[dict]) -> bytes:
    """Generate an Itemized Pricing PDF from a normalized job dict."""
    import build_bid_documents
    return build_bid_documents.build_itemized_pricing(job, costs)


def get_owner_signature(conn, job_id: int, source: str = "project") -> dict | None:
    """Load the owner's drawn signature for this job.

    For bids, reads from bid_signatures (existing flow).
    For projects/leads, reads from the same table keyed by a synthetic bid_id
    stored in app_settings, or returns None (owner can draw it on first use).
    """
    import bid_signing
    bid_signing.ensure_schema(conn)

    if source == "bid":
        row = bid_signing.latest(conn, job_id, "construction", role="owner")
    else:
        # For projects, look up the synthetic bid_id from app_settings
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM app_settings WHERE key = %s;",
                (f"project_{job_id}_bid_id",),
            )
            setting = cur.fetchone()
        if setting and setting.get("value"):
            synthetic_bid_id = int(setting["value"])
            row = bid_signing.latest(conn, synthetic_bid_id, "construction", role="owner")
        else:
            row = None

    if not row:
        return None
    try:
        png = bid_signing.image_png(conn, int(row["id"]))
        if not png:
            return None
        when = row.get("signed_at")
        return {
            "png": png,
            "name": row.get("signer_name") or "",
            "date": when.strftime("%Y-%m-%d %H:%M UTC") if hasattr(when, "strftime") else str(when or ""),
        }
    except Exception:
        return None


def save_owner_signature(conn, job_id: int, source: str, *,
                         signature_data: str, signer_name: str) -> dict:
    """Save the owner's drawn signature for this job/project.

    For bids, saves directly to bid_signatures.
    For projects/leads, creates or reuses a synthetic bid_id row in app_settings
    and saves the signature against that.
    """
    import bid_signing

    if source == "bid":
        return bid_signing.save(
            conn, job_id,
            signature_data=signature_data,
            signer_name=signer_name,
            company="construction",
            signer_role="owner",
        )

    # For projects/leads, use a synthetic bid_id
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM app_settings WHERE key = %s;",
            (f"project_{job_id}_bid_id",),
        )
        setting = cur.fetchone()

    if setting and setting.get("value"):
        synthetic_bid_id = int(setting["value"])
    else:
        # Create a minimal bid_opportunities row to anchor the signature
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bid_opportunities (company, name, status) "
                "VALUES ('construction', %s, 'active') RETURNING id;",
                (f"Project #{job_id}",),
            )
            synthetic_bid_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = %s;",
                (f"project_{job_id}_bid_id", str(synthetic_bid_id), str(synthetic_bid_id)),
            )
        conn.commit()

    return bid_signing.save(
        conn, synthetic_bid_id,
        signature_data=signature_data,
        signer_name=signer_name,
        company="construction",
        signer_role="owner",
    )


def create_signing_request(conn, job: dict, to_email: str, contract_pdf: bytes) -> dict:
    """Create a click-and-sign link for the contractor/client.

    Works for bids, projects, and leads. For non-bid sources, creates a
    synthetic bid_opportunities row to anchor the signing request.
    """
    import contract_signing

    job_id = job["id"]
    source = job.get("source", "bid")

    if source == "bid":
        bid_id = job_id
    else:
        # Look up or create synthetic bid_id
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM app_settings WHERE key = %s;",
                (f"project_{job_id}_bid_id",),
            )
            setting = cur.fetchone()
        if setting and setting.get("value"):
            bid_id = int(setting["value"])
        else:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO bid_opportunities (company, name, location, contact_name, contact_email, bid_amount, status) "
                    "VALUES ('construction', %s, %s, %s, %s, %s, 'active') RETURNING id;",
                    (job.get("name", ""), job.get("location", ""),
                     job.get("contact_name", ""), job.get("contact_email", ""),
                     job.get("bid_amount", 0)),
                )
                bid_id = cur.fetchone()["id"]
                cur.execute(
                    "INSERT INTO app_settings (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = %s;",
                    (f"project_{job_id}_bid_id", str(bid_id), str(bid_id)),
                )
            conn.commit()

    contract_signing.ensure_schema(conn)
    digest = hashlib.sha256(contract_pdf).hexdigest()
    row = contract_signing.create_request(
        conn, bid_id, sent_to=to_email, document_sha256=digest, company="construction")
    return {
        "token": row["token"],
        "expires_at": row["expires_at"],
        "bid_id": bid_id,
    }


def send_contract_email(conn, job: dict, to_email: str, *,
                        sow_pdf: bytes | None = None,
                        contract_pdf: bytes | None = None,
                        pricing_pdf: bytes | None = None,
                        sign_link: str = "") -> bool:
    """Email the contractor the SOW + contract + signing link.

    Returns True on success, False on failure.
    """
    import documents_service

    cfg = documents_service.smtp_config_from_env()
    if not documents_service.smtp_configured(cfg):
        logger.warning("[job-docs] email transport not configured")
        return False

    company_name = os.getenv("COMPANY_NAME", "BuildStack Construction LLC")
    company_phone = os.getenv("COMPANY_PHONE", "+1 (757) 846-9275")
    expires = ""
    if sign_link:
        # Try to get expiry from the signing request
        try:
            import contract_signing
            req = contract_signing.active(conn, job["id"] if job.get("source") == "bid" else 0, "construction")
            if req and req.get("expires_at"):
                expires = req["expires_at"].strftime("%B %d, %Y") if hasattr(req["expires_at"], "strftime") else ""
        except Exception:
            pass

    contact_name = job.get("contact_name") or "there"
    subject = f"Your Contract & Scope of Work — {job.get('name') or 'Your Project'}"
    total = job.get("bid_amount") or 0

    body = (
        f"Hi {contact_name.split()[0] if contact_name else 'there'},\n\n"
        f"{company_name} has prepared your contract documents for "
        f"{job.get('name') or 'your project'}"
        f"{f' at {job.get('location')}' if job.get('location') else ''}.\n\n"
    )
    if total:
        body += f"Contract total: ${total:,.2f}\n\n"

    body += (
        "Attached: Scope of Work and Renovation Agreement.\n\n"
        "How to review and sign — it takes about two minutes:\n"
        "  1. Read the attached documents.\n"
    )
    if sign_link:
        body += f"  2. Open this private link to sign: {sign_link}\n"
        body += (
            "  3. Type your full name, draw your signature, tick the acceptance box, and press Sign.\n"
            "  4. You will receive a signed copy and your client portal login straight away.\n\n"
        )
    else:
        body += "  2. Reply to this email with any questions, or call us to schedule a walkthrough.\n\n"

    if expires:
        body += f"The signing link expires on {expires}. "
    body += (
        f"If you need a new link, call or text {company_phone} and we will send one.\n\n"
        f"— {company_name}\n{company_phone}\n"
    )

    attachments = []
    if sow_pdf:
        attachments.append((sow_pdf, "Scope-of-Work.pdf"))
    if contract_pdf:
        attachments.append((contract_pdf, "Renovation-Agreement.pdf"))
    if pricing_pdf:
        attachments.append((pricing_pdf, "Itemized-Pricing.pdf"))

    try:
        from auto_reply import run_coro
        ok = run_coro(documents_service.send_email(cfg, to_email, subject, body, attachments=attachments))
    except Exception as exc:
        logger.warning("[job-docs] email send failed for %s: %s", to_email, exc)
        return False

    if ok:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
                    "VALUES ('outbound', 'email', %s, %s, %s);",
                    (cfg.get("SMTP_FROM") or "", to_email, body[:4000]),
                )
            conn.commit()
        except Exception:
            conn.rollback()

    return ok


def _synthetic_bid_id(conn, job_id: int):
    """The bid row that anchors a non-bid job's signing request, or None.

    ``create_signing_request()`` creates one synthetic bid_opportunities row per
    project/lead and stores its id as ``project_<job_id>_bid_id``. The portal's
    access table is keyed by bid id, so this is the only safe id to link --
    using the project id made a client's portal open whichever unrelated bid
    happened to share that number.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM app_settings WHERE key = %s;",
                (f"project_{job_id}_bid_id",),
            )
            row = cur.fetchone()
    except Exception as exc:
        logger.warning("[job-docs] synthetic bid lookup failed for project %s: %s", job_id, exc)
        return None
    value = row.get("value") if isinstance(row, dict) else (row[0] if row else None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def provision_portal_account(conn, job: dict) -> dict:
    """Create or find the client portal account for this job's contact.

    Returns {"email": str, "created": bool, "password": str | None}.
    """
    import portal_service

    email = (job.get("contact_email") or "").strip()
    if not email or "@" not in email:
        return {"email": "", "created": False, "password": None, "error": "no email"}

    name = job.get("contact_name") or ""
    job_id = job["id"]
    source = job.get("source", "project")

    # For projects/leads, use the portal_service.ensure_client_for_bid flow
    # by constructing a bid-like dict. The portal access table is keyed by
    # bid_id, so a project must be linked through the synthetic bid created by
    # create_signing_request() -- never through its own project id.
    if source == "bid":
        bid_dict = job
        portal_bid_id = int(job_id)
    else:
        portal_bid_id = _synthetic_bid_id(conn, job_id)
        if not portal_bid_id:
            return {"email": email, "created": False, "password": None,
                    "error": "no signing request exists for this project yet"}
        bid_dict = {
            "id": portal_bid_id,
            "contact_email": email,
            "contact_name": name,
            "name": job.get("name", ""),
        }

    try:
        cred = portal_service.ensure_client_for_bid(conn, bid_dict)
        portal_service.link_bid(conn, int(cred["client_id"]), portal_bid_id)
        return {
            "email": cred["email"],
            "created": cred.get("created", False),
            "password": cred.get("password"),
        }
    except Exception as exc:
        logger.warning("[job-docs] portal provisioning failed for %s: %s", email, exc)
        return {"email": email, "created": False, "password": None, "error": str(exc)}


def send_portal_credentials_email(conn, job: dict, cred: dict) -> bool:
    """Email the client their portal login credentials."""
    import documents_service

    email = cred.get("email") or job.get("contact_email") or ""
    if not email:
        return False

    company_name = os.getenv("COMPANY_NAME", "BuildStack Construction LLC")
    company_phone = os.getenv("COMPANY_PHONE", "+1 (757) 846-9275")
    domain = os.getenv("COMPANY_DOMAIN", "construction.bizstackperks.com")
    link = f"https://{domain}/portal/login"
    temp = cred.get("password") or ""
    contact_name = job.get("contact_name") or "there"

    if temp:
        body = (
            f"Hi {contact_name.split()[0] if contact_name else 'there'},\n\n"
            f"Thank you — the agreement for {job.get('name') or 'your project'} is signed.\n\n"
            f"Your {company_name} client portal is ready. It shows the contract, "
            "invoices, progress photos and messages with the office, and it lets you "
            "pay an invoice by card.\n\n"
            f"  Sign in: {link}\n"
            f"  Email: {email}\n"
            f"  Temporary password: {temp}\n\n"
            "You will be asked to choose your own password the first time you sign in. "
            "If you forget it later, use \"Forgot password?\" on the sign-in page.\n\n"
            f"— {company_name}\n{company_phone}\n"
        )
        subject = "Your client portal login — signed agreement"
    else:
        body = (
            f"Hi {contact_name.split()[0] if contact_name else 'there'},\n\n"
            f"Thank you — the agreement for {job.get('name') or 'your project'} is signed.\n\n"
            f"Your client portal is at {link}. Sign in with {email} and your existing "
            "password. If you have forgotten it, use \"Forgot password?\" on that page.\n\n"
            f"— {company_name}\n{company_phone}\n"
        )
        subject = "Your signed agreement and client portal"

    cfg = documents_service.smtp_config_from_env()
    try:
        from auto_reply import run_coro
        ok = run_coro(documents_service.send_email(cfg, email, subject, body))
    except Exception as exc:
        logger.warning("[job-docs] portal email failed for %s: %s", email, exc)
        return False

    if ok:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
                    "VALUES ('outbound', 'email', %s, %s, %s);",
                    (cfg.get("SMTP_FROM") or "", email, body[:4000]),
                )
            conn.commit()
        except Exception:
            conn.rollback()

    return ok


def full_onboarding_flow(conn, job: dict, costs: list[dict],
                         photos: list | None = None) -> dict:
    """Run the complete onboarding flow: generate docs, sign, email, portal.

    This is the one-call entry point for the manual trigger button.
    Returns a result dict with status of each step.
    """
    result = {
        "sow_generated": False,
        "contract_generated": False,
        "email_sent": False,
        "signing_link": "",
        "portal_created": False,
        "portal_email": "",
        "errors": [],
    }

    email = (job.get("contact_email") or "").strip()
    if not email or "@" not in email:
        result["errors"].append("No contact email on this job — cannot send contract or create portal account")
        return result

    # 1. Generate documents
    try:
        sow_pdf = generate_sow(job, costs, photos)
        result["sow_generated"] = True
    except Exception as exc:
        result["errors"].append(f"SOW generation failed: {exc}")
        sow_pdf = None

    try:
        owner_sig = get_owner_signature(conn, job["id"], job.get("source", "project"))
        contract_pdf = generate_contract(job, costs, photos, signature=owner_sig)
        result["contract_generated"] = True
    except Exception as exc:
        result["errors"].append(f"Contract generation failed: {exc}")
        contract_pdf = None

    pricing_pdf = None
    try:
        pricing_pdf = generate_itemized_pricing(job, costs)
    except Exception:
        pass

    if not sow_pdf and not contract_pdf:
        result["errors"].append("Could not generate any documents — check cost lines")
        return result

    # 2. Create signing request and get link
    sign_link = ""
    if contract_pdf:
        try:
            sign_req = create_signing_request(conn, job, email, contract_pdf)
            domain = os.getenv("COMPANY_DOMAIN", "construction.bizstackperks.com")
            sign_link = f"https://{domain}/sign/{sign_req['token']}"
            result["signing_link"] = sign_link
        except Exception as exc:
            result["errors"].append(f"Signing request failed: {exc}")

    # 3. Send email
    try:
        sent = send_contract_email(
            conn, job, email,
            sow_pdf=sow_pdf, contract_pdf=contract_pdf, pricing_pdf=pricing_pdf,
            sign_link=sign_link,
        )
        result["email_sent"] = sent
        if not sent:
            result["errors"].append("Email send failed — check transport configuration")
    except Exception as exc:
        result["errors"].append(f"Email send failed: {exc}")

    # 4. Provision portal account
    try:
        cred = provision_portal_account(conn, job)
        result["portal_created"] = cred.get("created", False)
        result["portal_email"] = cred.get("email", "")
        if cred.get("password"):
            send_portal_credentials_email(conn, job, cred)
    except Exception as exc:
        result["errors"].append(f"Portal provisioning failed: {exc}")

    return result
