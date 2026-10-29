"""Zoho OAuth: connect once from Settings, store the refresh token, know its scopes.

The deploy started with a refresh token pasted into Railway. It works, but
its scopes are CRM + Books + Campaigns only -- Mail and Calendar answer
INVALID_OAUTHSCOPE -- and widening a self-client token means generating a new
one by hand. This module gives the owner a Connect button instead: the
authorize URL asks for everything the app uses, the callback stores the
refresh token and the granted scope string in app_settings, and
zoho_client prefers that token over the environment one.

Zoho specifics worth knowing:
- access_type=offline + prompt=consent is what yields a refresh token.
- The token endpoint lives on accounts.zoho.<dc>; API hosts differ per
  product (www.zohoapis, mail.zoho, calendar.zoho, campaigns.zoho).
- Refresh tokens do not expire; they are revoked only by the user or by
  exceeding 20 refresh tokens per client, so re-connecting is safe.
"""
from __future__ import annotations

import os
import secrets
import time
import urllib.parse
import urllib.request
import json

import psycopg
from psycopg.rows import dict_row

DC = (os.getenv("ZOHO_DC_SUFFIX") or "com").strip()
ACCOUNTS = f"https://accounts.zoho.{DC}"

SCOPES = [
    "ZohoCRM.modules.ALL", "ZohoCRM.settings.READ", "ZohoCRM.users.READ",
    "ZohoBooks.fullaccess.all",
    "ZohoCampaigns.contact.ALL", "ZohoCampaigns.campaign.READ",
    "ZohoMail.accounts.READ", "ZohoMail.messages.CREATE", "ZohoMail.messages.READ",
    "ZohoCalendar.calendar.ALL", "ZohoCalendar.event.ALL",
]

TOKEN_KEY = "zoho_refresh_token"
SCOPE_KEY = "zoho_scopes"
ACCOUNT_KEY = "zoho_connected_email"
STATE_COOKIE = "zoho_oauth_state"

_states: dict[str, float] = {}


def _db():
    return psycopg.connect(os.getenv("DATABASE_URL", ""), row_factory=dict_row)


def client() -> tuple[str, str]:
    return (os.getenv("ZOHO_CLIENT_ID") or "").strip(), (os.getenv("ZOHO_CLIENT_SECRET") or "").strip()


def redirect_uri() -> str:
    explicit = (os.getenv("ZOHO_REDIRECT_URI") or "").strip()
    if explicit:
        return explicit
    domain = (os.getenv("COMPANY_DOMAIN") or "construction.bizstackperks.com").strip()
    return f"https://{domain}/oauth/zoho/callback"


def configured() -> bool:
    cid, sec = client()
    return bool(cid and sec and "your_" not in cid)


def new_state() -> str:
    st = secrets.token_urlsafe(24)
    now = time.time()
    for k, t in list(_states.items()):
        if now - t > 900:
            _states.pop(k, None)
    _states[st] = now
    return st


def consume_state(st: str) -> bool:
    return _states.pop(st or "", None) is not None


def auth_url(state: str) -> str:
    cid, _ = client()
    q = urllib.parse.urlencode({
        "scope": ",".join(SCOPES), "client_id": cid, "response_type": "code",
        "access_type": "offline", "prompt": "consent", "redirect_uri": redirect_uri(), "state": state,
    })
    return f"{ACCOUNTS}/oauth/v2/auth?{q}"


def exchange_code(code: str) -> dict:
    cid, sec = client()
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code", "client_id": cid, "client_secret": sec,
        "redirect_uri": redirect_uri(), "code": code,
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(f"{ACCOUNTS}/oauth/v2/token", data=data, method="POST"),
                                timeout=30) as r:
        body = json.loads(r.read())
    if not body.get("refresh_token"):
        raise RuntimeError(f"Zoho returned no refresh token: {str(body)[:200]}")
    return body


def _setting(conn, key: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s;", (key,))
        row = cur.fetchone()
    return (row or {}).get("value") or ""


def _set(conn, key: str, value: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();", (key, value))
    conn.commit()


def save_connection(refresh_token: str, scopes: str, email: str = "") -> None:
    with _db() as conn:
        _set(conn, TOKEN_KEY, refresh_token)
        _set(conn, SCOPE_KEY, scopes or "")
        if email:
            _set(conn, ACCOUNT_KEY, email)


def stored_refresh_token() -> str:
    """DB token first (connected from Settings), else the environment one."""
    try:
        with _db() as conn:
            tok = _setting(conn, TOKEN_KEY)
            if tok:
                return tok
    except Exception:
        pass
    return (os.getenv("ZOHO_REFRESH_TOKEN") or "").strip()


def status() -> dict:
    """What is connected and what each product can do, without a network call."""
    out = {"configured": configured(), "connected": False, "source": "", "scopes": [], "email": "",
           "crm": False, "campaigns": False, "books": False, "mail": False, "calendar": False,
           "redirect_uri": redirect_uri()}
    scopes = ""
    try:
        with _db() as conn:
            tok = _setting(conn, TOKEN_KEY)
            scopes = _setting(conn, SCOPE_KEY)
            out["email"] = _setting(conn, ACCOUNT_KEY)
        if tok:
            out["connected"], out["source"] = True, "settings"
    except Exception:
        pass
    if not out["connected"] and (os.getenv("ZOHO_REFRESH_TOKEN") or "").strip():
        out["connected"], out["source"] = True, "environment"
        scopes = scopes or (os.getenv("ZOHO_TOKEN_SCOPES") or "ZohoCRM.modules.ALL ZohoBooks.fullaccess.all ZohoCampaigns.contact.ALL")
    sc = scopes.replace(",", " ").split()
    out["scopes"] = sc
    low = " ".join(sc).lower()
    out["crm"] = "zohocrm." in low
    out["campaigns"] = "zohocampaigns." in low
    out["books"] = "zohobooks." in low
    out["mail"] = "zohomail.messages.create" in low
    out["calendar"] = "zohocalendar." in low
    return out
