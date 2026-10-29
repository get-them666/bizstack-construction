"""Zoho Calendar: put a booked walkthrough on the owner's calendar.

Needs ZohoCalendar.event.ALL (granted by the Connect Zoho button). The
target calendar is BOOKING_CALENDAR_UID when set, else the user's default
calendar from /api/v1/calendars.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import time

try:
    import zoho_client
    import zoho_oauth
except Exception:  # pragma: no cover
    zoho_client = zoho_oauth = None  # type: ignore

DC = (os.getenv("ZOHO_DC_SUFFIX") or "com").strip()
CAL_BASE = f"https://calendar.zoho.{DC}/api/v1"

_cal_cache: dict = {"uid": "", "at": 0.0}


def available() -> bool:
    try:
        return bool(zoho_oauth and zoho_oauth.status().get("calendar"))
    except Exception:
        return False


def _calendar_uid() -> str:
    explicit = (os.getenv("BOOKING_CALENDAR_UID") or "").strip()
    if explicit:
        return explicit
    if _cal_cache["uid"] and time.time() - _cal_cache["at"] < 3600:
        return _cal_cache["uid"]
    resp = zoho_client.api_request("GET", f"{CAL_BASE}/calendars")
    if resp is None or resp.status_code != 200:
        raise RuntimeError(f"Zoho Calendar list failed: {getattr(resp, 'status_code', '?')}")
    cals = resp.json().get("calendars") or []
    chosen = next((c for c in cals if c.get("isdefault")), cals[0] if cals else None)
    if not chosen:
        raise RuntimeError("no Zoho calendar on this login")
    _cal_cache.update({"uid": str(chosen.get("uid")), "at": time.time()})
    return _cal_cache["uid"]


def _zstamp(dt: _dt.datetime) -> str:
    """Zoho wants yyyyMMddTHHmmssZ in UTC (or local with a timezone field)."""
    return dt.astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def create_event(title: str, start: _dt.datetime, end: _dt.datetime, *, location: str = "",
                 description: str = "") -> bool:
    if not zoho_client or not zoho_client.is_configured():
        return False
    uid = _calendar_uid()
    eventdata = {
        "title": title[:200],
        "dateandtime": {"timezone": "UTC", "start": _zstamp(start), "end": _zstamp(end)},
        "location": location[:300],
        "description": description[:2000],
        "reminders": [{"action": "popup", "minutes": 60}],
    }
    resp = zoho_client.api_request("POST", f"{CAL_BASE}/calendars/{uid}/events",
                                   params={"eventdata": json.dumps(eventdata)})
    if resp is None or resp.status_code not in (200, 201):
        print(f"[zoho-cal] create failed: {getattr(resp, 'status_code', '?')} {getattr(resp, 'text', '')[:160]}", flush=True)
        return False
    return True
