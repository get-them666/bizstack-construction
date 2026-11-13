"""Resolve the GC/estimator email for a bid's Send box.

The send form used to depend on ``bid_opportunities.contact_email`` being filled
by the importer. When it is blank the owner has to retype an address that is
usually already sitting in the solicitation email. This module reads the bid's
existing contact, the stored solicitation body and the linked source email, and
picks the address that belongs to the GC's own domain.

It never guesses. A candidate must match the GC's domain or use a known
estimating/bid mailbox; otherwise ``ok`` is false and the owner types it.

Nothing here sends anything.
"""
from __future__ import annotations

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Bid boards and notification robots. These are never the GC's bid inbox.
_BLOCKED_DOMAINS = (
    "buildingconnected.com",
    "autodesk.com",
    "bidprime.com",
    "constructconnect.com",
    "bidnet.com",
    "bidclerk.com",
    "sam.gov",
    "bizstackperks.com",
    "example.com",
)
_BLOCKED_LOCAL = (
    "noreply", "no-reply", "donotreply", "do-not-reply", "notifications",
    "notification", "support", "help", "team", "hello", "info", "sales",
    "marketing", "admin", "webmaster", "postmaster",
)
_PREFERRED_LOCAL = (
    "estimating", "bids", "bid", "preconstruction", "proposals", "contracts",
    "purchasing",
)
# Company-name words that say nothing about the domain.
_GENERIC = {
    "construction", "group", "inc", "llc", "company", "co", "corp",
    "corporation", "the", "and", "builders", "builder", "contractors",
    "contracting", "services",
}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", (text or "").lower())
            if w not in _GENERIC}


def _score(email: str, owner: str, contact_name: str) -> int:
    local, _, domain = email.lower().partition("@")
    score = 0
    for token in _tokens(owner):
        if token in domain:
            score += 6
    if local in _PREFERRED_LOCAL:
        score += 4
    if contact_name:
        first = contact_name.strip().split()[0].lower()
        if len(first) > 2 and first in local:
            score += 4
    return score


def _usable(email: str) -> bool:
    local, _, domain = email.lower().partition("@")
    if not local or not domain:
        return False
    if any(domain == d or domain.endswith("." + d) for d in _BLOCKED_DOMAINS):
        return False
    if local in _BLOCKED_LOCAL:
        return False
    if any(local.startswith(x) for x in ("noreply", "no-reply", "donotreply")):
        return False
    return True


def find_gc_email(bid: dict, source_senders=()) -> dict:
    """Return ``{ok, email, name, source, candidates}`` for one bid.

    ``bid`` is a ``bid_opportunities`` row as a dict. ``source_senders`` are the
    ``sender`` values from ``bid_source_emails``. The bid record wins: a human
    already typed or confirmed that address.
    """
    existing = (bid.get("contact_email") or "").strip()
    if existing and "@" in existing and _usable(existing):
        return {
            "ok": True,
            "email": existing,
            "name": (bid.get("contact_name") or "").strip(),
            "source": "bid card",
            "candidates": [existing],
        }

    owner = (bid.get("owner") or "").strip()
    contact = (bid.get("contact_name") or "").strip()
    text = "\n".join(str(bid.get(k) or "") for k in ("notes", "scope", "reference"))

    found: set[str] = set()
    for chunk in [text, *[s or "" for s in source_senders]]:
        found.update(m.group(0) for m in _EMAIL.finditer(chunk))

    candidates = sorted(
        (e for e in found if _usable(e)),
        key=lambda e: (-_score(e, owner, contact), e.lower()),
    )
    if not candidates:
        return {
            "ok": False,
            "error": ("No GC email found in the bid or its solicitation. "
                      "Type the recipient before sending."),
            "candidates": [],
        }

    best = candidates[0]
    if _score(best, owner, contact) <= 0:
        return {
            "ok": False,
            "error": ("Found addresses but none clearly belongs to the GC. "
                      "Type the recipient before sending."),
            "candidates": candidates[:5],
        }

    name = contact
    if not name:
        # Only derive a person from a person-shaped mailbox (first.last). A role
        # mailbox like estimating@ stays blank; the old "word before the email"
        # guess produced names like "Please" and "Questions".
        local = best.split("@", 1)[0]
        if "." in local:
            name = " ".join(part.capitalize() for part in re.split(r"[._]", local) if part)

    return {
        "ok": True,
        "email": best,
        "name": name,
        "source": "solicitation email",
        "candidates": candidates[:5],
    }
