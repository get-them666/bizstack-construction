"""Google Calendar API integration for BizStack.
Low-level Google Calendar API wrappers used by the owner's calendar tools.
"""

import os
import json
import time
import urllib.parse
import urllib.request
from typing import Optional, List, Dict, Any

import google_oauth


CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"


# ──────────────────────────────────────────────
# Calendar API calls
# ──────────────────────────────────────────────

def _api_request(
    service: str,
    user_email: str,
    method: str,
    path: str,
    params: Optional[Dict] = None,
    body: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Make authenticated request to Google Calendar API."""
    access_token = google_oauth.get_valid_access_token(service, user_email)
    if not access_token:
        raise RuntimeError(f"No valid access token for {user_email}")
    url = CALENDAR_API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body else None
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def list_calendars(service: str, user_email: str) -> List[Dict[str, Any]]:
    """List all calendars user has access to."""
    result = _api_request(service, user_email, "GET", "/users/me/calendarList")
    return result.get("items", [])


def get_calendar(service: str, user_email: str, calendar_id: str) -> Dict[str, Any]:
    """Get calendar metadata."""
    return _api_request(service, user_email, "GET", f"/calendars/{urllib.parse.quote(calendar_id)}")


def create_calendar(service: str, user_email: str, summary: str, description: str = "") -> Dict[str, Any]:
    """Create a new calendar."""
    return _api_request(service, user_email, "POST", "/calendars", body={
        "summary": summary,
        "description": description,
        "timeZone": "America/New_York",
    })


def list_events(
    service: str,
    user_email: str,
    calendar_id: str,
    time_min: Optional[str] = None,
    time_max: Optional[str] = None,
    max_results: int = 250,
) -> List[Dict[str, Any]]:
    """List events from a calendar."""
    params = {
        "maxResults": max_results,
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    if time_min:
        params["timeMin"] = time_min
    if time_max:
        params["timeMax"] = time_max
    result = _api_request(service, user_email, "GET", f"/calendars/{urllib.parse.quote(calendar_id)}/events", params=params)
    return result.get("items", [])


def create_event(service: str, user_email: str, calendar_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """Create an event in a calendar."""
    return _api_request(service, user_email, "POST", f"/calendars/{urllib.parse.quote(calendar_id)}/events", body=event)


def update_event(service: str, user_email: str, calendar_id: str, event_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """Update an event in a calendar."""
    return _api_request(service, user_email, "PUT", f"/calendars/{urllib.parse.quote(calendar_id)}/events/{urllib.parse.quote(event_id)}", body=event)


def delete_event(service: str, user_email: str, calendar_id: str, event_id: str) -> None:
    """Delete an event from a calendar."""
    _api_request(service, user_email, "DELETE", f"/calendars/{urllib.parse.quote(calendar_id)}/events/{urllib.parse.quote(event_id)}")


