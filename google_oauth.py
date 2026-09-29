"""Google OAuth 2.0 integration for BizStack.
Handles: Sign in with Google, token exchange, storage, refresh, and user info.
"""

import os
import json
import time
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import psycopg
from psycopg.rows import dict_row


# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────

def _get_oauth_config(service: str = "broom") -> Dict[str, str]:
    """Get OAuth config for 'broom' or 'construction' service."""
    prefix = "CON_" if service == "construction" else ""
    return {
        "client_id": os.getenv(f"{prefix}GOOGLE_CLIENT_ID", ""),
        "client_secret": os.getenv(f"{prefix}GOOGLE_CLIENT_SECRET", ""),
        "redirect_uri": os.getenv(f"{prefix}GOOGLE_REDIRECT_URI", ""),
        "service": service,
    }


AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]
SCOPE_STRING = " ".join(SCOPES)


# ──────────────────────────────────────────────
# Database helpers
# ──────────────────────────────────────────────

def _db_url(service: str = "broom") -> str:
    if service == "construction":
        return os.getenv("CON_DATABASE_URL") or os.getenv("DATABASE_URL", "")
    return os.getenv("DATABASE_URL", "")


def _ensure_google_tokens_table(db) -> None:
    with db.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS google_tokens (
                id SERIAL PRIMARY KEY,
                service VARCHAR(20) NOT NULL,
                user_email VARCHAR(255) NOT NULL,
                user_id VARCHAR(100),
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                scope TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE (service, user_email)
            );
        """)
        db.commit()


def save_google_tokens(
    service: str,
    user_email: str,
    access_token: str,
    refresh_token: Optional[str],
    expires_in: int,
    scope: str,
    user_id: Optional[str] = None,
) -> None:
    db_url = _db_url(service)
    if not db_url:
        return
    with psycopg.connect(db_url, row_factory=dict_row) as db:
        _ensure_google_tokens_table(db)
        expires_at = datetime.now(timezone.utc).timestamp() + expires_in
        with db.cursor() as cur:
            cur.execute("""
                INSERT INTO google_tokens (service, user_email, user_id, access_token, refresh_token, expires_at, scope)
                VALUES (%s, %s, %s, %s, %s, to_timestamp(%s), %s)
                ON CONFLICT (service, user_email) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    access_token = EXCLUDED.access_token,
                    refresh_token = COALESCE(EXCLUDED.refresh_token, google_tokens.refresh_token),
                    expires_at = EXCLUDED.expires_at,
                    scope = EXCLUDED.scope,
                    updated_at = NOW();
            """, (service, user_email, user_id, access_token, refresh_token, expires_at, scope))
            db.commit()


def get_google_tokens(service: str, user_email: str) -> Optional[Dict[str, Any]]:
    db_url = _db_url(service)
    if not db_url:
        return None
    with psycopg.connect(db_url, row_factory=dict_row) as db:
        _ensure_google_tokens_table(db)
        with db.cursor() as cur:
            cur.execute("""
                SELECT * FROM google_tokens WHERE service = %s AND user_email = %s;
            """, (service, user_email))
            row = cur.fetchone()
            if row:
                return dict(row)
    return None


def delete_google_tokens(service: str, user_email: str) -> None:
    db_url = _db_url(service)
    if not db_url:
        return
    with psycopg.connect(db_url, row_factory=dict_row) as db:
        with db.cursor() as cur:
            cur.execute("DELETE FROM google_tokens WHERE service = %s AND user_email = %s;", (service, user_email))
            db.commit()


# ──────────────────────────────────────────────
# OAuth flow
# ──────────────────────────────────────────────

def build_auth_url(service: str = "broom", state: Optional[str] = None) -> str:
    """Build the Google OAuth authorization URL."""
    cfg = _get_oauth_config(service)
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "response_type": "code",
        "scope": SCOPE_STRING,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if state:
        params["state"] = state
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code_for_tokens(service: str, code: str) -> Dict[str, Any]:
    """Exchange authorization code for access/refresh tokens."""
    cfg = _get_oauth_config(service)
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "redirect_uri": cfg["redirect_uri"],
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def refresh_access_token(service: str, refresh_token: str) -> Dict[str, Any]:
    """Refresh an expired access token using refresh token."""
    cfg = _get_oauth_config(service)
    data = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def get_valid_access_token(service: str, user_email: str) -> Optional[str]:
    """Get a valid access token, refreshing if necessary."""
    tokens = get_google_tokens(service, user_email)
    if not tokens:
        return None
    expires_at = tokens["expires_at"]
    if isinstance(expires_at, datetime):
        expires_ts = expires_at.timestamp()
    else:
        expires_ts = float(expires_at)
    if time.time() < expires_ts - 60:
        return tokens["access_token"]
    if not tokens.get("refresh_token"):
        return None
    try:
        new_tokens = refresh_access_token(service, tokens["refresh_token"])
        save_google_tokens(
            service=service,
            user_email=user_email,
            access_token=new_tokens["access_token"],
            refresh_token=new_tokens.get("refresh_token") or tokens["refresh_token"],
            expires_in=new_tokens["expires_in"],
            scope=new_tokens.get("scope", tokens.get("scope", "")),
        )
        return new_tokens["access_token"]
    except Exception as e:
        print(f"[GOOGLE OAUTH] Token refresh failed for {user_email}: {e}")
        return None


def get_user_info(service: str, access_token: str) -> Dict[str, Any]:
    """Fetch user info from Google using access token."""
    req = urllib.request.Request(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


# ──────────────────────────────────────────────
# High-level helpers for main.py
# ──────────────────────────────────────────────

def start_google_oauth(service: str = "broom") -> tuple[str, str]:
    """Generate auth URL with CSRF state."""
    state = secrets.token_urlsafe(32)
    return build_auth_url(service, state), state


def handle_google_callback(service: str, code: str, state: Optional[str] = None) -> Dict[str, Any]:
    """Handle OAuth callback: exchange code, fetch user info, store tokens."""
    token_data = exchange_code_for_tokens(service, code)
    access_token = token_data["access_token"]
    user_info = get_user_info(service, access_token)
    user_email = user_info.get("email", "").lower()
    user_id = user_info.get("sub")
    save_google_tokens(
        service=service,
        user_email=user_email,
        access_token=access_token,
        refresh_token=token_data.get("refresh_token"),
        expires_in=token_data["expires_in"],
        scope=token_data.get("scope", SCOPE_STRING),
        user_id=user_id,
    )
    return {
        "user_email": user_email,
        "user_name": user_info.get("name", ""),
        "user_picture": user_info.get("picture", ""),
        "user_id": user_id,
    }


def revoke_google_access(service: str, user_email: str) -> None:
    """Revoke tokens and delete from DB."""
    tokens = get_google_tokens(service, user_email)
    if tokens and tokens.get("access_token"):
        try:
            revoke_url = f"https://oauth2.googleapis.com/revoke?token={tokens['access_token']}"
            urllib.request.urlopen(revoke_url)
        except Exception:
            pass
    delete_google_tokens(service, user_email)