"""Google Calendar API integration for BizStack.
Syncs calendar_events to/from Google Calendar per property/host.
"""

import os
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
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


# ──────────────────────────────────────────────
# BizStack sync logic
# ──────────────────────────────────────────────

def _bizstack_event_to_google(event: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a calendar_events row to Google Calendar event format."""
    start_time = event["start_time"]
    end_time = event["end_time"]
    if isinstance(start_time, str):
        start_time = start_time.replace(" ", "T")
    if isinstance(end_time, str):
        end_time = end_time.replace(" ", "T")
    return {
        "summary": f"{event.get('customer_name', 'Booking')} — {event.get('service_type', 'Cleaning')}",
        "description": (
            f"Property: {event.get('property_name', 'N/A')}\n"
            f"Customer: {event.get('customer_name', 'N/A')}\n"
            f"Phone: {event.get('phone', 'N/A')}\n"
            f"Service: {event.get('service_type', 'N/A')}\n"
            f"Payment: {event.get('payment_status', 'N/A')}\n"
            f"Source: {event.get('channel_source', 'manual')}\n"
            f"Booking ID: {event.get('channel_booking_id', event['id'])}"
        ),
        "start": {"dateTime": start_time, "timeZone": "America/New_York"},
        "end": {"dateTime": end_time, "timeZone": "America/New_York"},
        "extendedProperties": {
            "private": {
                "bizstack_event_id": str(event["id"]),
                "bizstack_channel_source": event.get("channel_source", ""),
                "bizstack_channel_booking_id": event.get("channel_booking_id", ""),
            }
        },
    }


def _google_event_to_bizstack(g_event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract BizStack fields from Google Calendar event extendedProperties."""
    props = g_event.get("extendedProperties", {}).get("private", {})
    if not props.get("bizstack_event_id"):
        return None
    return {
        "event_id": int(props["bizstack_event_id"]),
        "channel_source": props.get("bizstack_channel_source"),
        "channel_booking_id": props.get("bizstack_channel_booking_id"),
        "google_event_id": g_event["id"],
    }


def sync_property_calendar(service: str, user_email: str, property_id: int, db) -> Dict[str, Any]:
    """Sync a single property's calendar_events to Google Calendar."""
    # Get or create Google Calendar for this property
    with db.cursor() as cur:
        cur.execute("""
            SELECT name, google_calendar_id FROM properties WHERE id = %s;
        """, (property_id,))
        prop = cur.fetchone()
    if not prop:
        return {"status": "error", "message": "Property not found"}

    calendar_id = prop.get("google_calendar_id")
    if not calendar_id:
        # Create new calendar
        cal = create_calendar(service, user_email, f"Broom: {prop['name']}", f"Bookings for {prop['name']}")
        calendar_id = cal["id"]
        with db.cursor() as cur:
            cur.execute("UPDATE properties SET google_calendar_id = %s WHERE id = %s;", (calendar_id, property_id))
            db.commit()

    # Get local events for this property (last 90 days, next 365 days)
    with db.cursor() as cur:
        cur.execute("""
            SELECT ce.*, p.name AS property_name
            FROM calendar_events ce
            LEFT JOIN properties p ON p.id = ce.property_id
            WHERE ce.property_id = %s
              AND ce.start_time >= NOW() - INTERVAL '90 days'
              AND ce.start_time <= NOW() + INTERVAL '365 days'
            ORDER BY ce.start_time;
        """, (property_id,))
        local_events = cur.fetchall()

    # Get Google events for same window
    time_min = (datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)).isoformat()
    time_max = (datetime.now(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=999999)).isoformat()
    google_events = list_events(service, user_email, calendar_id, time_min=time_min, time_max=time_max)

    # Map Google events by bizstack_event_id
    google_map = {}
    for ge in google_events:
        parsed = _google_event_to_bizstack(ge)
        if parsed:
            google_map[parsed["event_id"]] = parsed["google_event_id"]

    results = {"created": 0, "updated": 0, "deleted": 0, "skipped": 0}

    for le in local_events:
        event_id = le["id"]
        g_event = _bizstack_event_to_google(le)
        if event_id in google_map:
            # Update existing
            update_event(service, user_email, calendar_id, google_map[event_id], g_event)
            results["updated"] += 1
        else:
            # Create new
            created = create_event(service, user_email, calendar_id, g_event)
            results["created"] += 1

    # Delete Google events that no longer exist locally (optional - skip for safety)
    # for local_id, google_id in google_map.items():
    #     if local_id not in [e["id"] for e in local_events]:
    #         delete_event(service, user_email, calendar_id, google_id)
    #         results["deleted"] += 1

    return {"status": "success", **results, "calendar_id": calendar_id}


def sync_all_properties(service: str, user_email: str, db) -> Dict[str, Any]:
    """Sync all properties for a user."""
    with db.cursor() as cur:
        cur.execute("SELECT id FROM properties WHERE host_id = (SELECT id FROM hosts WHERE email ILIKE %s);", (user_email,))
        properties = cur.fetchall()

    summary = {"properties": 0, "total_created": 0, "total_updated": 0}
    for prop in properties:
        result = sync_property_calendar(service, user_email, prop["id"], db)
        if result["status"] == "success":
            summary["properties"] += 1
            summary["total_created"] += result["created"]
            summary["total_updated"] += result["updated"]
    return summary