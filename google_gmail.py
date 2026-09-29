"""Gmail API integration for BizStack.
Send/receive email via Gmail API (replaces SMTP).
"""

import os
import json
import base64
import urllib.parse
import urllib.request
from typing import Optional, List, Dict, Any

import google_oauth


GMAIL_API_BASE = "https://www.googleapis.com/gmail/v1"


# ──────────────────────────────────────────────
# Gmail API calls
# ──────────────────────────────────────────────

def _api_request(
    service: str,
    user_email: str,
    method: str,
    path: str,
    params: Optional[Dict] = None,
    body: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Make authenticated request to Gmail API."""
    access_token = google_oauth.get_valid_access_token(service, user_email)
    if not access_token:
        raise RuntimeError(f"No valid access token for {user_email}")
    url = GMAIL_API_BASE + path
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


def send_email(
    service: str,
    user_email: str,
    to: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Send an email via Gmail API."""
    # Build MIME message
    lines = [
        f"To: {to}",
        f"Subject: {subject}",
    ]
    if cc:
        lines.append(f"Cc: {cc}")
    if bcc:
        lines.append(f"Bcc: {bcc}")
    lines.append("MIME-Version: 1.0")
    if body_html:
        lines.append("Content-Type: multipart/alternative; boundary=boundary123")
        lines.append("")
        lines.append("--boundary123")
        lines.append("Content-Type: text/plain; charset=UTF-8")
        lines.append("")
        lines.append(body_text)
        lines.append("")
        lines.append("--boundary123")
        lines.append("Content-Type: text/html; charset=UTF-8")
        lines.append("")
        lines.append(body_html)
        lines.append("")
        lines.append("--boundary123--")
    else:
        lines.append("Content-Type: text/plain; charset=UTF-8")
        lines.append("")
        lines.append(body_text)
    raw = "\r\n".join(lines)
    raw_b64 = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    payload = {"raw": raw_b64}
    if thread_id:
        payload["threadId"] = thread_id

    return _api_request(service, user_email, "POST", "/users/me/messages/send", body=payload)


def list_messages(
    service: str,
    user_email: str,
    query: str = "",
    max_results: int = 50,
    page_token: Optional[str] = None,
) -> Dict[str, Any]:
    """List messages matching query."""
    params = {"maxResults": max_results}
    if query:
        params["q"] = query
    if page_token:
        params["pageToken"] = page_token
    return _api_request(service, user_email, "GET", "/users/me/messages", params=params)


def get_message(service: str, user_email: str, message_id: str, format: str = "full") -> Dict[str, Any]:
    """Get a single message by ID."""
    params = {"format": format}
    return _api_request(service, user_email, "GET", f"/users/me/messages/{message_id}", params=params)


def get_thread(service: str, user_email: str, thread_id: str) -> Dict[str, Any]:
    """Get a thread by ID."""
    return _api_request(service, user_email, "GET", f"/users/me/threads/{thread_id}")


def modify_message(
    service: str,
    user_email: str,
    message_id: str,
    add_labels: Optional[List[str]] = None,
    remove_labels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Add/remove labels on a message."""
    body = {}
    if add_labels:
        body["addLabelIds"] = add_labels
    if remove_labels:
        body["removeLabelIds"] = remove_labels
    return _api_request(service, user_email, "POST", f"/users/me/messages/{message_id}/modify", body=body)


def trash_message(service: str, user_email: str, message_id: str) -> None:
    """Move message to trash."""
    _api_request(service, user_email, "POST", f"/users/me/messages/{message_id}/trash")


# ──────────────────────────────────────────────
# Helpers for BizStack
# ──────────────────────────────────────────────

def _parse_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Extract key fields from Gmail message."""
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    body_text = ""
    body_html = ""
    parts = payload.get("parts", [])
    if not parts and payload.get("body", {}).get("data"):
        data = payload["body"]["data"]
        decoded = base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="ignore")
        if payload.get("mimeType") == "text/html":
            body_html = decoded
        else:
            body_text = decoded
    else:
        for part in parts:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                body_text = base64.urlsafe_b64decode(part["body"]["data"] + "===").decode("utf-8", errors="ignore")
            elif part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
                body_html = base64.urlsafe_b64decode(part["body"]["data"] + "===").decode("utf-8", errors="ignore")
    return {
        "id": msg["id"],
        "thread_id": msg.get("threadId"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "body_text": body_text,
        "body_html": body_html,
        "labels": msg.get("labelIds", []),
        "snippet": msg.get("snippet", ""),
    }


def fetch_recent_emails(service: str, user_email: str, max_results: int = 20) -> List[Dict[str, Any]]:
    """Fetch recent emails for inbox display."""
    result = list_messages(service, user_email, query="in:inbox", max_results=max_results)
    messages = []
    for m in result.get("messages", []):
        full = get_message(service, user_email, m["id"], format="full")
        messages.append(_parse_message(full))
    return messages


def send_booking_confirmation(
    service: str,
    user_email: str,
    to_email: str,
    customer_name: str,
    property_name: str,
    start_time: str,
    end_time: str,
    service_type: str,
) -> Dict[str, Any]:
    """Send a booking confirmation email."""
    subject = f"Booking Confirmed: {service_type} at {property_name}"
    body_text = (
        f"Hi {customer_name},\n\n"
        f"Your booking is confirmed:\n\n"
        f"  Property: {property_name}\n"
        f"  Service: {service_type}\n"
        f"  Start: {start_time}\n"
        f"  End: {end_time}\n\n"
        f"Thank you for choosing Broom Service!\n\n"
        f"— Broom Service"
    )
    body_html = f"""
    <p>Hi {customer_name},</p>
    <p>Your booking is confirmed:</p>
    <ul>
      <li><strong>Property:</strong> {property_name}</li>
      <li><strong>Service:</strong> {service_type}</li>
      <li><strong>Start:</strong> {start_time}</li>
      <li><strong>End:</strong> {end_time}</li>
    </ul>
    <p>Thank you for choosing Broom Service!</p>
    <p>— Broom Service</p>
    """
    return send_email(service, user_email, to_email, subject, body_text, body_html)