"""Event-driven lead pipeline orchestrator.

Connects enrichment → qualification → quoting → project creation → payment.
EVERY EVENT automatically triggers the NEXT event(s) in sequence.

No manual steps. Lead created → auto-enriched → auto-contacted → auto-quoted → 
auto-project → ready for deposit/payment, all without human intervention.

Flow:
  lead.created → (if address-only) → lead.enriched → lead.contacted → 
  lead.quoted → project.created → ready for payment
"""

import os
import json
import logging
import asyncio
from typing import Dict, Optional, List
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("lead_pipeline")


# --- Event types (cascade points) ---

EVENT_LEAD_CREATED = "lead.created"
EVENT_LEAD_ENRICHED = "lead.enriched"
EVENT_LEAD_CONTACTED = "lead.contacted"
EVENT_LEAD_QUOTED = "lead.quoted"
EVENT_QUOTE_ACCEPTED = "quote.accepted"
EVENT_PROJECT_CREATED = "project.created"
EVENT_BID_WON = "bid.won"

# --- Pipeline state machine ---

LEAD_STATUS_NEW = "new"
LEAD_STATUS_ENRICHING = "enriching"
LEAD_STATUS_CONTACTED = "contacted"
LEAD_STATUS_QUOTED = "quoted"
LEAD_STATUS_DEPOSIT_PENDING = "deposit"
LEAD_STATUS_IN_PROGRESS = "in_progress"
LEAD_STATUS_COMPLETED = "completed"
LEAD_STATUS_LOST = "lost"

LEAD_STATUSES = [
    LEAD_STATUS_NEW,
    LEAD_STATUS_ENRICHING,
    LEAD_STATUS_CONTACTED,
    LEAD_STATUS_QUOTED,
    LEAD_STATUS_DEPOSIT_PENDING,
    LEAD_STATUS_IN_PROGRESS,
    LEAD_STATUS_COMPLETED,
    LEAD_STATUS_LOST,
]


def _log_event(db, event_type: str, lead_id: int, cascade: bool = True, details: str = ""):
    """Record event and optionally trigger cascading events."""
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO lead_pipeline_events 
                (lead_id, event_type, details, cascade_triggered, created_at)
                VALUES (%s, %s, %s, %s, NOW())
                RETURNING id
                """,
                (lead_id, event_type, details[:1000] if details else "", cascade),
            )
        db.commit()
    except Exception as e:
        logger.error(f"Failed to log event {event_type} for lead {lead_id}: {e}")


def _get_lead(db, lead_id: int) -> Optional[Dict]:
    """Fetch lead with full context."""
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, phone, email, project_type, address, 
                       status, source, analysis_json, created_at
                FROM leads WHERE id = %s
                """,
                (lead_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"Failed to fetch lead {lead_id}: {e}")
        return None


def _update_lead_status(db, lead_id: int, new_status: str) -> bool:
    """Move lead to a new status."""
    try:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE leads SET status = %s, updated_at = NOW() WHERE id = %s",
                (new_status, lead_id),
            )
        db.commit()
        return True
    except Exception as e:
        logger.error(f"Failed to update lead {lead_id} status: {e}")
        return False


def _is_contactable(lead: Dict) -> bool:
    """Does the lead have at least phone or email?"""
    phone = (lead.get("phone") or "").strip()
    email = (lead.get("email") or "").strip()
    has_phone = len(phone) >= 10
    has_email = email and "@" in email and not email.lower().endswith((".local", "+lead"))
    return has_phone or has_email


def _get_project_type(lead: Dict) -> Optional[str]:
    """Extract and normalize the project type for quoting."""
    pt = (lead.get("project_type") or "").strip()
    if not pt:
        try:
            meta = json.loads(lead.get("analysis_json") or "{}")
            pt = (meta.get("work_type") or "").strip()
        except (TypeError, ValueError):
            pass
    return pt if pt else None


def _enrich_lead_internal(db, lead_id: int) -> Dict:
    """INTERNAL: Enrich and return result WITHOUT cascading."""
    import enrichment
    
    lead = _get_lead(db, lead_id)
    if not lead:
        return {"enriched": False, "error": "lead not found"}

    # Already enriched or has contact
    if _is_contactable(lead):
        return {"enriched": False, "reason": "already contactable"}

    # Try address-based enrichment (permits)
    address = (lead.get("address") or "").strip()
    if address and enrichment.pdl_enabled():
        found = enrichment._enrich_address(address)
        if found and not found.get("_auth_failed") and not found.get("_provider_error"):
            try:
                with db.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE leads SET 
                            name = CASE WHEN %s <> '' THEN %s ELSE name END,
                            email = CASE WHEN %s <> '' THEN %s ELSE email END,
                            phone = CASE WHEN %s <> '' THEN %s ELSE phone END,
                            analysis_json = CASE WHEN analysis_json IS NULL THEN %s
                                                ELSE analysis_json::jsonb || %s::jsonb END,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (
                            found.get("name") or "",
                            found.get("name") or "",
                            found.get("email") or "",
                            found.get("email") or "",
                            found.get("phone") or "",
                            found.get("phone") or "",
                            json.dumps({"enriched": True, "enriched_at": datetime.now(timezone.utc).isoformat()}),
                            json.dumps({"enriched": True, "enriched_at": datetime.now(timezone.utc).isoformat()}),
                            lead_id,
                        ),
                    )
                db.commit()
                logger.info(f"✅ Enriched lead {lead_id}: {found.get('email')} / {found.get('phone')}")
                return {"enriched": True, "found": found, "method": "address"}
            except Exception as e:
                logger.error(f"Failed to update enriched lead {lead_id}: {e}")
                return {"enriched": False, "error": str(e)}
    
    return {"enriched": False, "reason": "enrichment unavailable"}


def _contact_lead_internal(db, lead_id: int) -> Dict:
    """INTERNAL: Move to contacted WITHOUT cascading."""
    lead = _get_lead(db, lead_id)
    if not lead:
        return {"contacted": False, "error": "lead not found"}

    current_status = lead.get("status") or "new"
    if current_status not in ("new", "enriching"):
        return {"contacted": False, "reason": f"already {current_status}"}

    if not _is_contactable(lead):
        return {"contacted": False, "reason": "not contactable (no phone/email)"}

    # Move to contacted
    if _update_lead_status(db, lead_id, LEAD_STATUS_CONTACTED):
        logger.info(f"✅ Lead {lead_id} moved to CONTACTED")
        
        # AUTO OUTREACH: Send first SMS
        try:
            import outreach_queue
            project = (_get_project_type(lead) or "your project").lower()
            name = (lead.get("name") or "there").split()[0]
            msg = f"Hi {name}! 👋 We're excited to help with your {project}. Let's schedule a free walkthrough. Reply or call us!"
            result = outreach_queue.queue_sms(db, lead_id, msg)
            if result.get("queued"):
                logger.info(f"✅ SMS auto-queued for lead {lead_id}")
        except Exception as e:
            logger.error(f"Outreach queue failed for {lead_id}: {e}")
        
        return {"contacted": True}
    
    return {"contacted": False, "error": "status update failed"}


def _quote_lead_internal(db, lead_id: int) -> Dict:
    """INTERNAL: Create quote WITHOUT cascading."""
    import quoting
    
    lead = _get_lead(db, lead_id)
    if not lead:
        return {"quote_created": False, "error": "lead not found"}

    current_status = lead.get("status") or "new"
    if current_status != LEAD_STATUS_CONTACTED:
        return {"quote_created": False, "reason": f"lead status is {current_status}"}

    project_type = _get_project_type(lead)
    if not project_type or project_type.lower() in quoting.UNQUOTABLE_TRADES:
        return {"quote_created": False, "reason": f"unquotable trade: {project_type}"}

    # Check if quote exists
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id FROM quotes WHERE lead_id = %s AND status != 'declined' ORDER BY id DESC LIMIT 1",
                (lead_id,),
            )
            existing = cur.fetchone()
            if existing:
                return {"quote_created": False, "reason": "quote already exists", "quote_id": existing["id"]}
    except Exception as e:
        logger.error(f"Failed to check for existing quote on {lead_id}: {e}")

    # Build and persist quote
    try:
        quoting.ensure_schema(db)
        quote_result = quoting.create_quote(
            db,
            lead_id,
            trade=project_type,
            sqft=None,
            band=quoting.BAND_MID,
            status=quoting.STATUS_DRAFT,
        )
        if quote_result.get("ok"):
            quote_id = quote_result["id"]
            logger.info(f"✅ Draft quote {quote_id} created for lead {lead_id}: ${quote_result.get('job_price_cents', 0) / 100:.2f}")
            
            # Move lead to QUOTED
            _update_lead_status(db, lead_id, LEAD_STATUS_QUOTED)
            
            return {
                "quote_created": True,
                "quote_id": quote_id,
                "price_cents": quote_result.get("job_price_cents"),
            }
        else:
            return {"quote_created": False, "error": quote_result.get("error", "unknown")}
    except Exception as e:
        logger.error(f"Failed to create quote for lead {lead_id}: {e}")
        return {"quote_created": False, "error": str(e)}


# --- CASCADE ENTRY POINTS (called on each event) ---

def on_lead_created(db, lead_id: int):
    """EVENT: Lead created → trigger enrichment."""
    _log_event(db, EVENT_LEAD_CREATED, lead_id, cascade=True)
    
    # Cascade: Try enrichment immediately
    try:
        enrich_result = _enrich_lead_internal(db, lead_id)
        if enrich_result.get("enriched"):
            _log_event(db, EVENT_LEAD_ENRICHED, lead_id, cascade=True, 
                      details=f"enriched via {enrich_result.get('method')}")
            on_lead_enriched(db, lead_id)
        else:
            # If we have contact already without enrichment, move forward
            lead = _get_lead(db, lead_id)
            if lead and _is_contactable(lead):
                on_lead_enriched(db, lead_id)
    except Exception as e:
        logger.error(f"Failed to cascade on_lead_created for {lead_id}: {e}")


def on_lead_enriched(db, lead_id: int):
    """EVENT: Lead enriched → trigger contact qualification."""
    _log_event(db, EVENT_LEAD_ENRICHED, lead_id, cascade=True)
    
    # Cascade: Try moving to contacted
    try:
        contact_result = _contact_lead_internal(db, lead_id)
        if contact_result.get("contacted"):
            _log_event(db, EVENT_LEAD_CONTACTED, lead_id, cascade=True)
            on_lead_contacted(db, lead_id)
    except Exception as e:
        logger.error(f"Failed to cascade on_lead_enriched for {lead_id}: {e}")


def on_lead_contacted(db, lead_id: int):
    """EVENT: Lead contacted → trigger quoting."""
    _log_event(db, EVENT_LEAD_CONTACTED, lead_id, cascade=True)
    
    # Cascade: Try creating quote
    try:
        quote_result = _quote_lead_internal(db, lead_id)
        if quote_result.get("quote_created"):
            _log_event(db, EVENT_LEAD_QUOTED, lead_id, cascade=True,
                      details=f"quote {quote_result['quote_id']} created")
            on_lead_quoted(db, lead_id)
    except Exception as e:
        logger.error(f"Failed to cascade on_lead_contacted for {lead_id}: {e}")


def on_lead_quoted(db, lead_id: int):
    """EVENT: Lead quoted → ready for owner review & deposit."""
    _log_event(db, EVENT_LEAD_QUOTED, lead_id, cascade=False)
    logger.info(f"🎯 Lead {lead_id} ready for deposit/payment")


def on_quote_accepted(db, quote_id: int):
    """EVENT: Quote accepted → create project."""
    try:
        with db.cursor() as cur:
            cur.execute("SELECT lead_id, trade, job_price_cents, scope_text FROM quotes WHERE id = %s",
                       (quote_id,))
            quote = cur.fetchone()
            if not quote:
                logger.error(f"Quote {quote_id} not found")
                return

            lead_id = quote["lead_id"]
            
            # Check if project exists
            cur.execute(
                "SELECT id FROM projects WHERE lead_id = %s AND status != 'cancelled' LIMIT 1",
                (lead_id,),
            )
            if cur.fetchone():
                logger.info(f"Project already exists for lead {lead_id}")
                return

            # Create project
            cur.execute(
                """
                INSERT INTO projects (lead_id, name, summary, status, created_at)
                VALUES (%s, %s, %s, %s, NOW())
                RETURNING id
                """,
                (
                    lead_id,
                    quote.get("trade") or "Job",
                    quote.get("scope_text") or f"${quote.get('job_price_cents', 0) / 100:.2f}",
                    "planned",
                ),
            )
            project_id = cur.fetchone()["id"]
        db.commit()

        _log_event(db, EVENT_PROJECT_CREATED, lead_id, cascade=False,
                  details=f"project {project_id} created from quote")
        
        # Update lead status
        _update_lead_status(db, lead_id, LEAD_STATUS_DEPOSIT_PENDING)
        
        logger.info(f"✅ Project {project_id} created from accepted quote {quote_id}")
        
    except Exception as e:
        logger.error(f"Failed to create project from quote {quote_id}: {e}")


def on_bid_won(db, bid_id: int):
    """EVENT: Bid won → create project."""
    try:
        import bid_opportunities
        
        with db.cursor() as cur:
            bid = bid_opportunities.get_bid(cur.connection, bid_id, "construction")
            if not bid:
                logger.error(f"Bid {bid_id} not found")
                return

            # Look for matching lead by address
            lead_id = None
            address = bid.get("location") or ""
            if address:
                cur.execute(
                    "SELECT id FROM leads WHERE address ILIKE %s AND company='construction' ORDER BY id DESC LIMIT 1",
                    (f"%{address}%",),
                )
                row = cur.fetchone()
                if row:
                    lead_id = row["id"]

            # Create project
            cur.execute(
                """
                INSERT INTO projects (lead_id, name, summary, status, created_at)
                VALUES (%s, %s, %s, %s, NOW())
                RETURNING id
                """,
                (
                    lead_id,
                    bid.get("name") or "Bid Project",
                    bid.get("notes") or "",
                    "planned",
                ),
            )
            project_id = cur.fetchone()["id"]
        db.commit()

        _log_event(db, EVENT_BID_WON, lead_id or 0, cascade=False,
                  details=f"project {project_id} from bid")
        
        logger.info(f"✅ Project {project_id} created from won bid {bid_id}")
        
    except Exception as e:
        logger.error(f"Failed to create project from bid {bid_id}: {e}")


def ensure_schema(db) -> None:
    """Create pipeline audit table."""
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS lead_pipeline_events (
                    id BIGSERIAL PRIMARY KEY,
                    lead_id INTEGER,
                    event_type VARCHAR(60) NOT NULL,
                    details TEXT,
                    cascade_triggered BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_lead_pipeline_events_lead ON lead_pipeline_events(lead_id, created_at DESC)"
            )
        db.commit()
    except Exception as e:
        logger.error(f"Failed to create pipeline schema: {e}")


if __name__ == "__main__":
    import sys
    db = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    ensure_schema(db)
    if len(sys.argv) > 2:
        action = sys.argv[1]
        lead_id = int(sys.argv[2])
        
        if action == "created":
            on_lead_created(db, lead_id)
        elif action == "enriched":
            on_lead_enriched(db, lead_id)
        elif action == "contacted":
            on_lead_contacted(db, lead_id)
        elif action == "quoted":
            on_lead_quoted(db, lead_id)
    db.close()
