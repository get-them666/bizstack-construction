"""The one place the company's name lives.

BuildStack Construction LLC was organized with the Virginia State Corporation
Commission on 2026-10-28 (entity ID 12097281). Before that the code carried the
name in three spellings across ~20 modules -- "Buildstack Construction",
"Buildstack Construction Co.", "BuildStack Construction" -- so a legal-name
change meant hunting strings in emails, SMS, Stripe products, PDFs and the
knowledge files, and missing one would put the wrong entity on a document.

Everything customer-facing should read from here. ``COMPANY_NAME`` in the
environment overrides the default so the deploy stays configurable without a
code change; the defaults are the registered name so a bare dev box is still
correct.
"""
import os

DEFAULT_NAME = "BuildStack Construction LLC"
DEFAULT_ENTITY_ID = "12097281"
DEFAULT_STATE = "Virginia"
DEFAULT_ADDRESS = "701 Dana Dr, Chesapeake, VA 23321"


def name() -> str:
    """Registered legal name, e.g. on invoices, the legal page, CAN-SPAM footers."""
    return (os.getenv("COMPANY_NAME") or DEFAULT_NAME).strip()


def short_name() -> str:
    """The name without its entity suffix, for greetings and subject lines.

    "Hello from BuildStack Construction" reads better than "...LLC" in an
    inbox; the full legal name still appears in the body footer.
    """
    n = name()
    for suffix in (" LLC", ", LLC", " L.L.C.", " Co.", " Inc."):
        if n.endswith(suffix):
            return n[: -len(suffix)].rstrip(",").strip()
    return n


def entity_id() -> str:
    return (os.getenv("COMPANY_ENTITY_ID") or DEFAULT_ENTITY_ID).strip()


def entity_line() -> str:
    """'BuildStack Construction LLC, a Virginia limited liability company (SCC ID 12097281)'."""
    eid = entity_id()
    tail = f" (SCC ID {eid})" if eid else ""
    return f"{name()}, a {DEFAULT_STATE} limited liability company{tail}"


def mailing_address() -> str:
    """Street address only; callers prefix the name when they need a full block."""
    raw = (os.getenv("SENDER_PHYSICAL_ADDRESS") or "").strip()
    if raw:
        # SENDER_PHYSICAL_ADDRESS is "Name, street, city ST zip" for the email
        # footer; strip the leading name if it is ours.
        for candidate in (name(), DEFAULT_NAME, "Buildstack Construction Co.", "Buildstack Construction"):
            if raw.lower().startswith(candidate.lower()):
                return raw[len(candidate):].lstrip(" ,").strip() or DEFAULT_ADDRESS
        return raw
    return DEFAULT_ADDRESS
