"""Push new leads into Kit, so every inquiry joins the list and starts the
follow-up sequence.

The app is the source of truth; Kit is the marketing channel. This runs from
the same background thread that places the first call, so a lead is in Kit
within seconds of being created, without blocking the request.

Fail-soft by design: Kit being down must never lose or delay a lead.
"""

from __future__ import annotations

import os

import requests

API = "https://api.kit.com/v4"
TIMEOUT = 20
NEW_ESTIMATE_SEQUENCE = int(os.getenv("KIT_SEQUENCE_NEW_ESTIMATE", "2977410") or 2977410)
# The newsletter welcome built 2026-11-20: two published emails (welcome, then a
# practical maintenance checklist four days later).
NEWSLETTER_SEQUENCE = int(os.getenv("KIT_SEQUENCE_NEWSLETTER", "2979613") or 2979613)
NEWSLETTER_TAG = (os.getenv("KIT_NEWSLETTER_TAG") or "newsletter").strip()
TAG = (os.getenv("KIT_LEAD_TAG") or "estimate-request").strip()


def configured() -> bool:
    return bool((os.getenv("KIT_API_KEY") or "").strip())


def _headers() -> dict:
    return {"X-Kit-Api-Key": (os.getenv("KIT_API_KEY") or "").strip(),
            "Content-Type": "application/json", "Accept": "application/json"}


def _tag_id(name: str) -> int | None:
    r = requests.get(f"{API}/tags", headers=_headers(), timeout=TIMEOUT)
    if r.status_code == 200:
        for tag in (r.json().get("tags") or []):
            if (tag.get("name") or "").lower() == name.lower():
                return int(tag["id"])
    r = requests.post(f"{API}/tags", headers=_headers(), json={"name": name}, timeout=TIMEOUT)
    if r.status_code in (200, 201):
        return int(r.json()["tag"]["id"])
    return None


def subscribe(email: str, first_name: str = "", phone: str = "") -> int | None:
    """Create or find the subscriber; returns the Kit subscriber id."""
    email = (email or "").strip()
    if "@" not in email:
        return None
    body = {"email_address": email, "first_name": (first_name or "").strip(),
            "state": "active"}
    if phone:
        body["fields"] = {"phone": phone}
    r = requests.post(f"{API}/subscribers", headers=_headers(), json=body, timeout=TIMEOUT)
    if r.status_code in (200, 201):
        return int((r.json().get("subscriber") or {})["id"])
    # Already subscribed: Kit answers 422/409; look it up by address.
    r = requests.get(f"{API}/subscribers", headers=_headers(),
                     params={"email_address": email}, timeout=TIMEOUT)
    for sub in (r.json().get("subscribers") or []):
        if (sub.get("email_address") or "").lower() == email.lower():
            return int(sub["id"])
    return None


def add_to_sequence(subscriber_id: int, sequence_id: int = NEW_ESTIMATE_SEQUENCE) -> bool:
    r = requests.post(f"{API}/sequences/{sequence_id}/subscribers", headers=_headers(),
                      json={"id": int(subscriber_id)}, timeout=TIMEOUT)
    return r.status_code in (200, 201)


def add_tag(subscriber_id: int, name: str = TAG) -> bool:
    tag_id = _tag_id(name)
    if not tag_id:
        return False
    r = requests.post(f"{API}/tags/{tag_id}/subscribers/{subscriber_id}",
                      headers=_headers(), timeout=TIMEOUT)
    return r.status_code in (200, 201)


def sequence_status(sequence_id: int) -> dict:
    """Whether a sequence is currently sending. Never raises.

    Kit returns 200 when a subscriber is added to a paused sequence, so the
    add call alone cannot tell us whether an email will ever go out. This is
    the only way to be honest in the signup response.
    """
    try:
        r = requests.get(f"{API}/sequences/{int(sequence_id)}",
                         headers=_headers(), timeout=TIMEOUT)
        if r.status_code == 200:
            seq = (r.json() or {}).get("sequence") or {}
            return {"ok": True, "hold": bool(seq.get("hold")),
                    "active": seq.get("active", True),
                    "name": seq.get("name") or ""}
        return {"ok": False, "reason": f"HTTP {r.status_code}"}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:200]}


def sync_lead(name: str = "", email: str = "", phone: str = "") -> dict:
    """Subscribe + tag + start the new-estimate sequence. Never raises.

    ``ok`` now means the sequence was actually started. A subscriber who is
    tagged but not enrolled is not a complete sync, and the caller logs the
    incomplete result instead of assuming success.
    """
    if not configured():
        return {"ok": False, "reason": "KIT_API_KEY is not set"}
    try:
        first = (name or "").strip().split()[0] if (name or "").strip() else ""
        sid = subscribe(email, first, phone)
        if not sid:
            return {"ok": False, "reason": "subscriber could not be created"}
        tagged = add_tag(sid)
        started = add_to_sequence(sid)
        if not started:
            return {"ok": False, "subscriber_id": sid, "tagged": tagged,
                    "sequence_started": False,
                    "reason": "subscriber saved but the estimate sequence was not started"}
        return {"ok": True, "subscriber_id": sid, "tagged": tagged,
                "sequence_started": True}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:200]}


def subscribe_newsletter(email: str = "", first_name: str = "") -> dict:
    """Public newsletter signup: subscribe, tag, start the welcome sequence.

    Deliberately separate from sync_lead: a newsletter reader is not an
    estimate request and must not enter the sales sequence.
    """
    if not configured():
        return {"ok": False, "reason": "Newsletter signup is not configured yet."}
    email = (email or "").strip().lower()
    local, _, domain = email.partition("@")
    if not local or "." not in domain or len(email) > 254 or " " in email:
        return {"ok": False, "reason": "That email address does not look right."}
    try:
        sid = subscribe(email, (first_name or "").strip())
        if not sid:
            return {"ok": False, "reason": "Kit did not accept that address."}
        tagged = add_tag(sid, NEWSLETTER_TAG)

        # Do not promise an inbox delivery for a sequence that is paused.
        status = sequence_status(NEWSLETTER_SEQUENCE)
        if status.get("ok") and status.get("hold"):
            return {
                "ok": True, "subscriber_id": sid, "tagged": tagged,
                "sequence_started": False, "paused": True,
                "note": ("You're on the list. The next issue will go out when "
                         "the newsletter resumes."),
            }

        started = add_to_sequence(sid, NEWSLETTER_SEQUENCE)
        if not started:
            return {
                "ok": False, "subscriber_id": sid, "tagged": tagged,
                "sequence_started": False,
                "reason": ("We couldn't start the welcome email. Please try "
                           "again or email hello@bizstackperks.com."),
            }
        return {"ok": True, "subscriber_id": sid, "tagged": tagged,
                "sequence_started": True}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:200]}
