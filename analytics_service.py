#!/usr/bin/env python3
"""Server-side RudderStack event tracking.

Deliberately server-side rather than a browser snippet. A browser snippet needs
the write key in the page source, which is fine for a write key and a
disaster for anything else. Keeping ingestion here means no RudderStack
credential is ever served to a visitor, and the admin dashboard, crew portal
and every other logged-in surface stay untracked without any of them having to
remember to opt out.

Credentials come from the environment. Nothing is hardcoded:

    RUDDERSTACK_WRITE_KEY     required
    RUDDERSTACK_DATA_PLANE_URL required

THE ONE RULE IN THIS FILE: analytics must never be able to break a page. Every
public entry point swallows everything -- a missing key, an import error, a
network timeout, a bad payload. An exception escaping into a request handler
turns a page view into a 500, which is the worst possible outcome for a tool
that exists to count page views. `track()` therefore never raises.

Only three events are instrumented, because they are the three that answer
"is the marketing spend working?":

    page_view              someone looked at a page
    instant_quote_started  they began a quote
    instant_quote_submitted  they sent one

That last number is the one that matters. Everything upstream of it is cost.
"""

import os
import threading

_lock = threading.Lock()
_ready = False


def config() -> tuple:
    """(write_key, data_plane_url) from the environment, blanks if unset."""
    return (
        (os.getenv("RUDDERSTACK_WRITE_KEY") or "").strip(),
        (os.getenv("RUDDERSTACK_DATA_PLANE_URL") or "").strip(),
    )


def is_configured() -> bool:
    write_key, plane = config()
    return bool(write_key and plane)


def _ensure_client():
    """Configure the module-level SDK once per process.

    rudderstack.analytics is a module singleton with module-level attributes,
    not an instantiable client, so this sets them rather than constructing one.
    The lock matters because several request threads can reach this at once on
    a cold start.
    """
    global _ready
    if _ready:
        return True
    write_key, plane = config()
    if not (write_key and plane):
        return False
    with _lock:
        if _ready:
            return True
        try:
            import rudderstack.analytics as rudder

            rudder.write_key = write_key
            rudder.dataPlaneUrl = plane
            _ready = True
            return True
        except Exception as exc:  # pragma: no cover - import failure only
            print(f"[analytics] rudderstack unavailable: {exc}", flush=True)
            return False


def identify(user_id: str, traits: dict = None) -> None:
    """Associate events with a person. Never raises."""
    if not user_id or not _ensure_client():
        return
    try:
        import rudderstack.analytics as rudder

        rudder.identify(user_id=str(user_id), **(traits or {}))
    except Exception as exc:
        print(f"[analytics] identify failed: {exc}", flush=True)


def track(event: str, user_id: str = "", properties: dict = None) -> None:
    """Record an event. Never raises, under any circumstance."""
    if not event or not _ensure_client():
        return
    try:
        import rudderstack.analytics as rudder

        props = dict(properties or {})
        rudder.track(user_id=str(user_id) if user_id else "anonymous",
                     event=str(event), properties=props)
    except Exception as exc:
        print(f"[analytics] track({event}) failed: {exc}", flush=True)


def page_view(path: str, referrer: str = "", user_id: str = "") -> None:
    """Record a page view. `path` only -- never the full URL with its query
    string, which on a quote page would carry homeowner addresses into an
    analytics store nobody expects to hold them."""
    if not path:
        return
    track("page_view", user_id, {
        "path": str(path)[:300],
        "referrer": str(referrer or "")[:300],
    })


def quote_started(company: str, user_id: str = "") -> None:
    track("instant_quote_started", user_id, {"company": company or ""})


def quote_submitted(company: str, has_photo: bool = False, user_id: str = "") -> None:
    track("instant_quote_submitted", user_id,
          {"company": company or "", "has_photo": bool(has_photo)})


def flush() -> None:
    """Push the buffer. Safe to call anywhere; used by tests and scripts."""
    if not _ensure_client():
        return
    try:
        import rudderstack.analytics as rudder

        rudder.flush()
    except Exception:
        pass
