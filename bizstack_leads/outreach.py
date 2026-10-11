"""Review-mode outreach: compose a draft, let the owner approve, then send.

The agreed posture is "AI handles first touch and follow-ups, owner reviews the
sends". That splits cleanly:

* **Compose + stage** happens here, needs no credentials, and never touches the
  network. A staged draft is just text on the lead.
* **Send** is delegated to a ``transport`` the app injects. The transport is
  where the app's mailer/SMS and its contact policy live, so this package stays
  free of app imports, API keys and network code -- and is therefore testable.

The package owns *what* to send and *when it is due*; the app owns *how* it
leaves the building. That seam is what lets both apps share the logic while
keeping their own credentials.
"""
from __future__ import annotations

from typing import Protocol

from . import lifecycle, store


class Brand(Protocol):
    """The copy bits a company supplies: its display name and call-to-action."""

    name: str
    cta: str


def compose_first_touch(brand, lead) -> tuple[str, str]:
    """A first-touch email for a lead. ``(subject, body)``."""
    name = (lead.get("name") or "").strip()
    first = name.split(" ")[0] if name else "there"
    subject = f"Thanks for reaching out — {brand.name}"
    where = ""
    if (lead.get("address") or "").strip():
        where = f" with {lead.get('address')}"
    body = (
        f"Hi {first},\n\n"
        f"Thanks for reaching out to {brand.name}. We'd love to help{where}.\n\n"
        f"{brand.cta}\n\n"
        f"Best regards,\n{brand.name}"
    )
    return subject, body


def compose_follow_up(brand, lead) -> tuple[str, str]:
    """A nudge for a lead we contacted but who has not replied."""
    name = (lead.get("name") or "").strip()
    first = name.split(" ")[0] if name else "there"
    subject = f"Just checking in — {brand.name}"
    body = (
        f"Hi {first},\n\n"
        f"I wanted to make sure my last note reached you. If you're still "
        f"interested, I can get you a price today.\n\n"
        f"{brand.cta}\n\n"
        f"Best regards,\n{brand.name}"
    )
    return subject, body


def stage_first_touch(db, brand, lead_id) -> bool:
    """Stage a first-touch draft for the owner to review. Never sends."""
    lead = store.get_lead(db, lead_id)
    if not lead:
        return False
    subject, body = compose_first_touch(brand, lead)
    return store.stage_draft(db, lead_id, subject, body)


def stage_follow_up(db, brand, lead_id) -> bool:
    lead = store.get_lead(db, lead_id)
    if not lead:
        return False
    subject, body = compose_follow_up(brand, lead)
    return store.stage_draft(db, lead_id, subject, body)


def send(db, transport, lead_id, subject, body) -> dict:
    """Stage then send one owner-approved message through ``transport``.

    Staging first (rather than sending the raw text) means the same guardrails
    apply here as anywhere: contact policy, daily caps, the recipient scrub.
    If the transport refuses the send, the staged draft is cleared so a blocked
    message cannot be picked up later by a "send all staged" sweep as if the
    owner had queued it.
    """
    lead = store.get_lead(db, lead_id)
    if not lead:
        return {"sent": False, "why": "lead not found"}
    if lead.get("do_not_contact"):
        return {"sent": False, "why": "lead is marked do not contact"}
    if not (lead.get("email") or "").strip():
        return {"sent": False, "why": "that lead has no email address"}

    store.stage_draft(db, lead_id, subject, body)
    result = transport.send(lead, "email", f"{subject}\n{body}") or {}
    # The staged copy is consumed either way: on success it has gone out, and on
    # a block it must not linger for a "send all staged" sweep to fire later as
    # if the owner had queued it. A transport that clears it itself (the apps'
    # fire_lead_draft does) makes this a harmless no-op.
    try:
        store.clear_draft(db, lead_id)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[leads] could not clear staged draft on lead {lead_id}: {exc}", flush=True)
    store.recompute(db, lead_id)
    return {
        "sent": bool(result.get("sent")),
        "channel": result.get("channel", "email"),
        "why": result.get("why", ""),
    }
