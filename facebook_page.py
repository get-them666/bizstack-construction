"""Post to the BuildStack Construction Facebook Page.

The Page is https://www.facebook.com/BuildStackConstructionLLC/ and posting needs
two env vars from a Meta app (Page access token + Page id):

    FB_PAGE_ID=...
    FB_PAGE_TOKEN=...

Nothing here runs until both are set; `configured()` is false and the owner-facing
page explains what is missing rather than failing silently. Every call returns the
Graph API error verbatim, because "it did not post" is useless without the reason
(token expired, missing pages_manage_posts scope, photo URL unreachable).
"""

from __future__ import annotations

import os

import requests

GRAPH = "https://graph.facebook.com/v21.0"
TIMEOUT = 30


def page_id() -> str:
    return (os.getenv("FB_PAGE_ID") or "").strip()


def token() -> str:
    return (os.getenv("FB_PAGE_TOKEN") or "").strip()


def configured() -> bool:
    return bool(page_id() and token())


def missing() -> list[str]:
    return [name for name, value in (("FB_PAGE_ID", page_id()),
                                     ("FB_PAGE_TOKEN", token())) if not value]


def _error(resp: requests.Response) -> str:
    try:
        detail = resp.json().get("error") or {}
        msg = detail.get("message") or resp.text
        code = detail.get("code")
        return f"{msg} (code {code})" if code else str(msg)
    except Exception:
        return f"HTTP {resp.status_code}: {resp.text[:200]}"


def page_info() -> dict:
    """Name, follower count and link for the connected Page."""
    if not configured():
        return {"ok": False, "reason": "Facebook is not connected (missing "
                                       + ", ".join(missing()) + ")."}
    try:
        r = requests.get(f"{GRAPH}/{page_id()}",
                         params={"fields": "name,fan_count,link",
                                 "access_token": token()},
                         timeout=TIMEOUT)
        if r.status_code != 200:
            return {"ok": False, "reason": _error(r)}
        body = r.json()
        return {"ok": True, "name": body.get("name"), "fans": body.get("fan_count"),
                "link": body.get("link")}
    except Exception as exc:  # noqa: BLE001 - status must never 500 the page
        return {"ok": False, "reason": str(exc)[:200]}


def post_photo(image_url: str, caption: str) -> dict:
    """Publish a photo post. Graph fetches the image from our public URL."""
    if not configured():
        return {"ok": False, "reason": "Facebook is not connected (missing "
                                       + ", ".join(missing()) + ")."}
    if not image_url.startswith("https://"):
        return {"ok": False, "reason": "The photo URL must be a public https URL."}
    try:
        r = requests.post(f"{GRAPH}/{page_id()}/photos",
                          data={"url": image_url, "caption": caption,
                                "access_token": token()},
                          timeout=TIMEOUT)
        if r.status_code != 200:
            return {"ok": False, "reason": _error(r)}
        body = r.json()
        return {"ok": True, "post_id": body.get("post_id") or body.get("id")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)[:200]}


def post_message(message: str) -> dict:
    """Publish a text-only post."""
    if not configured():
        return {"ok": False, "reason": "Facebook is not connected (missing "
                                       + ", ".join(missing()) + ")."}
    try:
        r = requests.post(f"{GRAPH}/{page_id()}/feed",
                          data={"message": message, "access_token": token()},
                          timeout=TIMEOUT)
        if r.status_code != 200:
            return {"ok": False, "reason": _error(r)}
        return {"ok": True, "post_id": (r.json() or {}).get("id")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": str(exc)[:200]}
