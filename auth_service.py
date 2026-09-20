"""Authentication, OTP, roles and feature permissions for Broom Service.

Sessions are stateless HMAC-signed tokens (no dependency on itsdangerous).
Every role — admin, worker, host — logs in with a password/PIN. An email OTP
step can be layered on top; it is configurable per role and only enforced when
email delivery is actually configured (see _otp_enabled in main.py).

Workers and hosts see a restricted toolset. An admin can toggle each tool on
or off; the toggles are stored in app_settings as JSON under `perms_worker`
and `perms_host`.
"""

import os
import json
import time
import hmac
import hashlib
import secrets
from base64 import urlsafe_b64encode, urlsafe_b64decode

SESSION_COOKIE = "session_token"
OTP_COOKIE = "otp_pending"
OTP_TTL_SECONDS = 10 * 60
SESSION_TTL_SECONDS = 72 * 60 * 60
MAX_OTP_ATTEMPTS = 6

ROLES = ("admin", "worker", "host")

# Feature catalogue: key -> human label shown on the Access page.
WORKER_FEATURES = {
    "jobs": "Assigned jobs & tasks",
    "timeclock": "Time clock (clock in / out)",
    "map": "Job location maps & directions",
    "pay": "Pay summary & paychecks",
    "paystubs": "Download paystub PDFs",
    "photos": "Photo finish (room photos for the office)",
    "messages": "Messages with the office",
    "devices": "Door & alarm access codes",
}
HOST_FEATURES = {
    "bookings": "Their bookings & schedule",
    "properties": "Their properties",
    "payments": "Payment status",
    "map": "Booking location maps",
    "photos": "Cleaning photos & photo finish",
    "messages": "Messages with the office",
    "devices": "Smart devices (locks, alarms, lighting)",
}

# Sensible defaults: give workers only what they need to do the job and get
# paid; give hosts only read access to their own bookings.
DEFAULT_PERMISSIONS = {
    "worker": {k: (k in ("jobs", "timeclock", "map", "pay", "paystubs", "photos", "messages", "devices")) for k in WORKER_FEATURES},
    "host": {k: (k in ("bookings", "properties", "payments", "map", "photos", "messages", "devices")) for k in HOST_FEATURES},
}


def _secret() -> bytes:
    return (os.getenv("SESSION_COOKIE_SECRET") or os.getenv("APP_SECRET") or os.getenv("SECRET_KEY") or "dev-insecure-secret").encode()


def _b64e(raw: bytes) -> str:
    return urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_token(payload: dict) -> str:
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).digest()
    return body + "." + _b64e(sig)


def verify_token(token: str):
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = _b64e(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64d(body))
    except Exception:
        return None
    if payload.get("exp") and time.time() > payload["exp"]:
        return None
    return payload


def issue_session(role: str, actor_id, email: str, name: str) -> str:
    return sign_token({
        "t": "session", "role": role, "id": actor_id,
        "email": email or "", "name": name or "",
        "sid": secrets.token_urlsafe(24),
        "exp": time.time() + SESSION_TTL_SECONDS,
    })


def session_id_from_token(token: str):
    payload = verify_token(token)
    return payload.get("sid") if payload else None


def issue_pending(role: str, actor_id, email: str, name: str) -> str:
    return sign_token({
        "t": "otp", "role": role, "id": actor_id,
        "email": email or "", "name": name or "",
        "exp": time.time() + OTP_TTL_SECONDS,
    })


def actor_from_token(token: str):
    payload = verify_token(token)
    if not payload or payload.get("t") != "session":
        return None
    return {
        "role": payload.get("role"),
        "id": payload.get("id"),
        "email": payload.get("email") or "",
        "name": payload.get("name") or "",
    }


def pending_from_token(token: str):
    payload = verify_token(token)
    if not payload or payload.get("t") != "otp":
        return None
    return payload


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_otp(code: str) -> str:
    return hashlib.sha256((str(code) + "|").encode() + _secret()).hexdigest()


def verify_otp(code: str, code_hash: str) -> bool:
    return hmac.compare_digest(hash_otp(code), code_hash or "")


def permissions_defaults(role: str) -> dict:
    return dict(DEFAULT_PERMISSIONS.get(role, {}))


def features_for(role: str, stored_json) -> dict:
    base = permissions_defaults(role)
    if not base:
        return {}
    try:
        stored = json.loads(stored_json) if stored_json else {}
    except (TypeError, ValueError):
        stored = {}
    for key in base:
        if key in stored:
            base[key] = bool(stored[key])
    return base


def feature_label(role: str, key: str) -> str:
    if role == "worker":
        return WORKER_FEATURES.get(key, key)
    return HOST_FEATURES.get(key, key)
