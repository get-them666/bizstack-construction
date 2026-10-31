"""Automatic outreach queue: SMS/email to contacted leads without bombardment.

Once a lead reaches 'contacted' status, it enters the outreach queue. The queue
respects contact policies (not more than once per channel per N days) and
sequences touches to avoid spam behavior.

Flow:
  lead → contacted → queue for SMS → queue for email → queue for call
"""

import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, List

import psycopg
from psycopg.rows import dict_row

from bizstack_leads import store as lead_store

logger = logging.getLogger("outreach_queue")


CHANNEL_SMS = "sms"
CHANNEL_EMAIL = "email"
CHANNEL_CALL = "call"

# Minimum days between touches on same channel
CONTACT_POLICY = {
    CHANNEL_SMS: 5,      # Don't SMS the same number twice in 5 days
    CHANNEL_EMAIL: 7,    # Don't email the same address twice in 7 days
    CHANNEL_CALL: 14,    # Don't call twice in 14 days
}


def _get_lead(db, lead_id: int) -> Optional[Dict]:
    """Fetch lead details."""
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, name, phone, email, project_type, address, status FROM leads WHERE id = %s",
                (lead_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"Failed to fetch lead {lead_id}: {e}")
        return None


def _last_contact(db, lead_id: int, channel: str) -> Optional[datetime]:
    """When was this lead last touched on this channel?"""
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(created_at) as last_at FROM comms_logs
                WHERE lead_id = %s AND channel = %s AND direction = 'outbound'
                """,
                (lead_id, channel),
            )
            row = cur.fetchone()
            return row["last_at"] if row and row["last_at"] else None
    except Exception as e:
        logger.error(f"Failed to check last contact for {lead_id}/{channel}: {e}")
        return None


def _can_contact(db, lead_id: int, channel: str) -> bool:
    """Has enough time passed to contact on this channel?"""
    last = _last_contact(db, lead_id, channel)
    if not last:
        return True  # Never contacted before
    
    min_days = CONTACT_POLICY.get(channel, 7)
    threshold = datetime.now(timezone.utc) - timedelta(days=min_days)
    return last < threshold


def queue_sms(db, lead_id: int, body: str) -> Dict:
    """Queue an SMS if contact policy allows."""
    lead = _get_lead(db, lead_id)
    if not lead:
        return {"queued": False, "error": "lead not found"}
    
    phone = (lead.get("phone") or "").strip()
    if not phone or len(phone.replace("+", "").replace("-", "").replace(" ", "")) < 10:
        return {"queued": False, "error": "no valid phone"}
    
    if not _can_contact(db, lead_id, CHANNEL_SMS):
        return {"queued": False, "reason": "contact policy: too recent"}
    
    try:
        import signalwire_service
        sw = signalwire_service.SignalWireService()
        result = sw.send_sms(phone, body)
        if result.get("ok"):
            with db.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO comms_logs 
                    (direction, channel, sender, recipient, message_body, lead_id, created_at)
                    VALUES ('outbound', %s, 'system', %s, %s, %s, NOW())
                    """,
                    (CHANNEL_SMS, phone, body, lead_id),
                )
            db.commit()
            try:
                lead_store.recompute(db, lead_id)
            except Exception as exc:
                logger.warning(f"queue refresh failed for lead {lead_id}: {exc}")
            logger.info(f"✅ SMS queued for lead {lead_id}: {phone}")
            return {"queued": True, "channel": CHANNEL_SMS}
        else:
            logger.error(f"SMS failed for {lead_id}: {result.get('error')}")
            return {"queued": False, "error": result.get("error")}
    except Exception as e:
        logger.error(f"Failed to queue SMS for {lead_id}: {e}")
        return {"queued": False, "error": str(e)}


def queue_email(db, lead_id: int, subject: str, body: str) -> Dict:
    """Queue an email if contact policy allows."""
    lead = _get_lead(db, lead_id)
    if not lead:
        return {"queued": False, "error": "lead not found"}
    
    email = (lead.get("email") or "").strip()
    if not email or "@" not in email or email.lower().endswith((".local", "+lead")):
        return {"queued": False, "error": "no valid email"}
    
    if not _can_contact(db, lead_id, CHANNEL_EMAIL):
        return {"queued": False, "reason": "contact policy: too recent"}
    
    try:
        import auto_reply
        result = auto_reply._send_email(email, subject, body)
        if result.get("ok"):
            with db.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO comms_logs 
                    (direction, channel, sender, recipient, message_body, lead_id, created_at)
                    VALUES ('outbound', %s, 'system', %s, %s, %s, NOW())
                    """,
                    (CHANNEL_EMAIL, email, f"{subject}\n\n{body}", lead_id),
                )
            db.commit()
            try:
                lead_store.recompute(db, lead_id)
            except Exception as exc:
                logger.warning(f"queue refresh failed for lead {lead_id}: {exc}")
            logger.info(f"✅ Email queued for lead {lead_id}: {email}")
            return {"queued": True, "channel": CHANNEL_EMAIL}
        else:
            logger.error(f"Email failed for {lead_id}: {result.get('error')}")
            return {"queued": False, "error": result.get("error")}
    except Exception as e:
        logger.error(f"Failed to queue email for {lead_id}: {e}")
        return {"queued": False, "error": str(e)}


def auto_outreach_on_contact(db, lead_id: int) -> Dict:
    """Auto-send first outreach SMS when lead becomes 'contacted'."""
    lead = _get_lead(db, lead_id)
    if not lead:
        return {"outreach": False, "error": "lead not found"}
    
    if lead.get("status") != "contacted":
        return {"outreach": False, "reason": "lead not in contacted status"}
    
    # Already sent SMS?
    if _last_contact(db, lead_id, CHANNEL_SMS):
        return {"outreach": False, "reason": "already sent SMS"}
    
    # Build message
    project = (lead.get("project_type") or "your project").lower()
    name = lead.get("name") or "there"
    message = f"Hi {name}! 👋 We're excited to help with your {project}. Let's schedule a free walkthrough. Reply or call us!"
    
    result = queue_sms(db, lead_id, message)
    if result.get("queued"):
        return {"outreach": True, "channel": CHANNEL_SMS}
    
    return {"outreach": False, "reason": result.get("reason") or result.get("error")}


def ensure_schema(db) -> None:
    """Ensure comms_logs table exists (already in main schema, but idempotent)."""
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                ALTER TABLE comms_logs ADD COLUMN IF NOT EXISTS lead_id INTEGER;
                CREATE INDEX IF NOT EXISTS idx_comms_logs_lead_channel 
                ON comms_logs(lead_id, channel, direction, created_at DESC)
                WHERE lead_id IS NOT NULL
                """
            )
        db.commit()
    except Exception as e:
        logger.error(f"Failed to ensure comms_logs schema: {e}")


if __name__ == "__main__":
    import sys
    db = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    ensure_schema(db)
    if len(sys.argv) > 1:
        lead_id = int(sys.argv[1])
        result = auto_outreach_on_contact(db, lead_id)
        print(json.dumps(result, indent=2, default=str))
    db.close()
