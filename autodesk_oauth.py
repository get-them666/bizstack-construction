"""Autodesk Platform Services / ACC OAuth, three-legged.

Mirrors google_oauth.py deliberately: same env-var shape, same token table
shape, same refresh-then-reuse helper, so there is one OAuth pattern in this
repo rather than two.

Why three-legged and not client credentials. A two-legged token identifies the
*app*. ACC project endpoints -- submittals, RFIs, issues -- are scoped to a
person who is a member of the project, so a client_credentials token is
refused. That is why the APS app has to be a Traditional Web App and the owner
has to sign in: the resulting token carries a real user identity, which is the
only thing ACC will accept.

PKCE (S256) is used even though we hold a client secret. The secret proves who
we are to APS; the verifier proves the code arriving at our callback is the one
issued for this browser's authorize request. They guard different things, and
APS supports the verifier, so there is no reason to leave it off.

Scopes are read-only. This pulls project data onto a board; it does not write
back to ACC. Widen SCOPES only if a feature actually needs it -- data:write
against someone's project is not something to grant incidentally.
"""
import datetime
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

import psycopg
from psycopg.rows import dict_row

AUTH_URL = "https://developer.api.autodesk.com/authentication/v2/authorize"
TOKEN_URL = "https://developer.api.autodesk.com/authentication/v2/token"
API_BASE = "https://developer.api.autodesk.com"

SCOPES = ["data:read", "project:read"]
SCOPE_STRING = " ".join(SCOPES)

STATE_COOKIE = "aps_oauth_state"
STATE_TTL = 600


class AutodeskError(RuntimeError):
    """Anything that makes the APS flow fail. Surfaced, never swallowed."""


# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────

def config() -> Dict[str, str]:
    return {
        "client_id": os.getenv("APS_CLIENT_ID", ""),
        "client_secret": os.getenv("APS_CLIENT_SECRET", ""),
        "redirect_uri": os.getenv("APS_REDIRECT_URI", ""),
        "project_id": os.getenv("APS_PROJECT_ID", ""),
    }


def configured() -> bool:
    c = config()
    return bool(c["client_id"] and c["client_secret"] and c["redirect_uri"])


def missing() -> list[str]:
    names = {"client_id": "APS_CLIENT_ID", "client_secret": "APS_CLIENT_SECRET",
             "redirect_uri": "APS_REDIRECT_URI", "project_id": "APS_PROJECT_ID"}
    return [v for k, v in names.items() if not config()[k]]


def _db_url() -> str:
    return os.getenv("CON_DATABASE_URL") or os.getenv("DATABASE_URL", "")


# ──────────────────────────────────────────────
# Token storage
# ──────────────────────────────────────────────

def ensure_schema(db) -> None:
    with db.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS autodesk_tokens (
                id SERIAL PRIMARY KEY,
                user_email VARCHAR(255) NOT NULL UNIQUE,
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                scope TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """)
    db.commit()


def save_tokens(user_email: str, access_token: str, refresh_token: Optional[str],
                expires_in: int, scope: str) -> None:
    dsn = _db_url()
    if not dsn:
        raise AutodeskError("DATABASE_URL is not set; cannot store the APS token.")
    expires_at = datetime.datetime.now(datetime.timezone.utc).timestamp() + int(expires_in)
    with psycopg.connect(dsn, row_factory=dict_row) as db:
        ensure_schema(db)
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO autodesk_tokens
                    (user_email, access_token, refresh_token, expires_at, scope)
                VALUES (%s, %s, %s, to_timestamp(%s), %s)
                ON CONFLICT (user_email) DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    refresh_token = COALESCE(EXCLUDED.refresh_token, autodesk_tokens.refresh_token),
                    expires_at = EXCLUDED.expires_at,
                    scope = EXCLUDED.scope,
                    updated_at = NOW();
            """, (user_email, access_token, refresh_token, expires_at, scope))
        db.commit()


def get_tokens(user_email: str) -> Optional[Dict[str, Any]]:
    dsn = _db_url()
    if not dsn:
        return None
    try:
        with psycopg.connect(dsn, row_factory=dict_row) as db:
            ensure_schema(db)
            with db.cursor() as cur:
                cur.execute("SELECT * FROM autodesk_tokens WHERE user_email = %s;",
                            (user_email,))
                row = cur.fetchone()
                return dict(row) if row else None
    except psycopg.Error as e:
        print(f"[APS] could not read stored token: {e}")
        return None


def delete_tokens(user_email: str) -> None:
    dsn = _db_url()
    if not dsn:
        return
    with psycopg.connect(dsn, row_factory=dict_row) as db:
        ensure_schema(db)
        with db.cursor() as cur:
            cur.execute("DELETE FROM autodesk_tokens WHERE user_email = %s;", (user_email,))
        db.commit()


# ──────────────────────────────────────────────
# Flow
# ──────────────────────────────────────────────

def new_state() -> tuple[str, str, str]:
    """Fresh (state, code_verifier, code_challenge) for one authorize hop."""
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = hashlib.sha256(verifier.encode()).digest()
    import base64
    return state, verifier, base64.urlsafe_b64encode(challenge).decode().rstrip("=")


def build_auth_url(state: str, code_challenge: str) -> str:
    c = config()
    if not c["client_id"] or not c["redirect_uri"]:
        raise AutodeskError("APS_CLIENT_ID and APS_REDIRECT_URI are required.")
    params = {
        "client_id": c["client_id"],
        "redirect_uri": c["redirect_uri"],
        "response_type": "code",
        "scope": SCOPE_STRING,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _post_token(fields: dict, timeout: int = 25) -> Dict[str, Any]:
    c = config()
    if not c["client_id"] or not c["client_secret"]:
        raise AutodeskError("APS_CLIENT_ID and APS_CLIENT_SECRET are required.")
    body = urllib.parse.urlencode({**fields,
                                   "client_id": c["client_id"],
                                   "client_secret": c["client_secret"]}).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # APS puts the useful part in the body: invalid_client, invalid_code,
        # access_denied. The status alone sends people hunting the wrong thing.
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AutodeskError(f"APS token request failed: HTTP {e.code} {detail}") from e
    except urllib.error.URLError as e:
        raise AutodeskError(f"could not reach APS: {e.reason}") from e


def exchange_code(code: str, code_verifier: str, timeout: int = 25) -> Dict[str, Any]:
    return _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": config()["redirect_uri"],
    }, timeout)


def refresh(refresh_token: str, timeout: int = 25) -> Dict[str, Any]:
    return _post_token({"grant_type": "refresh_token",
                        "refresh_token": refresh_token}, timeout)


def usable_token(user_email: str, timeout: int = 25) -> str:
    """A live access token, refreshed if needed.

    Refreshes 60s early so a token cannot expire between the check and the API
    call that uses it -- the failure mode being a 401 that looks like bad
    credentials and sends people off to rotate the wrong secret.
    """
    row = get_tokens(user_email)
    if not row:
        raise AutodeskError(f"Autodesk is not connected for {user_email}. "
                            "Sign in at /oauth/autodesk/start first.")
    expires = row["expires_at"]
    expires_ts = expires.timestamp() if isinstance(expires, datetime.datetime) else float(expires)
    if time.time() < expires_ts - 60:
        return row["access_token"]
    if not row.get("refresh_token"):
        raise AutodeskError(
            "The stored APS access token expired and there is no refresh token. "
            "Reconnect with offline access enabled in the APS app.")
    try:
        new = refresh(row["refresh_token"], timeout)
    except AutodeskError as e:
        raise AutodeskError(f"APS refresh failed ({e}). Reconnect at /oauth/autodesk/start.") from e
    save_tokens(user_email, new["access_token"],
                new.get("refresh_token") or row["refresh_token"],
                new.get("expires_in", 3600), new.get("scope", row.get("scope", "")))
    return new["access_token"]


def api_get(path: str, user_email: str, timeout: int = 25) -> Any:
    """GET an APS path with the owner's token and the ACC project header.

    ACC endpoints reject a bearer token that is not also told which project the
    request is about, so the header is mandatory there and harmless on Data
    Management, which is why it goes on every call.
    """
    tok = usable_token(user_email, timeout)
    url = path if path.startswith("http") else f"{API_BASE}{path}"
    headers = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
    pid = config()["project_id"]
    if pid:
        headers["x-construction-project-id"] = pid
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AutodeskError(f"APS GET {path} failed: HTTP {e.code} {detail}") from e
    except urllib.error.URLError as e:
        raise AutodeskError(f"could not reach APS: {e.reason}") from e


def list_projects(user_email: str, timeout: int = 25) -> list[dict]:
    """Every ACC project this account can see.

    This is the discovery call for APS_PROJECT_ID: the value lives in the
    response, so nobody has to find it in a menu. No project header, because
    this is the call that decides which project the header should name.
    """
    tok = usable_token(user_email, timeout)
    req = urllib.request.Request(
        f"{API_BASE}/construction/admin/v1/projects",
        headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AutodeskError(f"ACC project list failed: HTTP {e.code} {detail}") from e
    except urllib.error.URLError as e:
        raise AutodeskError(f"could not reach APS: {e.reason}") from e
    results = data.get("results") if isinstance(data, dict) else data
    return [r for r in (results or []) if isinstance(r, dict)]