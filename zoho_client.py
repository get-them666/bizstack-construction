"""
Shared Zoho API client with OAuth token management.
Handles CRM, Books, Campaigns, etc. with token caching.
"""
import os
import json
import time
import requests
from typing import Optional, Dict, Any

DC_SUFFIX = os.getenv("ZOHO_DC_SUFFIX", "com")
MCP_TOKEN_URL = os.getenv("ZOHO_MCP_TOKEN_URL", "https://mcp.zoho.com/baas/mcp/v1/oauth/079a8f081168a82c71bf6fcf859c3233/137816000000014284/token")

# Token cache
_token_cache = {
    "access_token": None,
    "expires_at": 0
}

class ZohoAuthError(Exception):
    pass


def get_access_token(force_refresh: bool = False) -> Optional[str]:
    """Get a valid access token, refreshing if needed."""
    now = time.time()
    if not force_refresh and _token_cache["access_token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["access_token"]
    
    # A token connected from Settings (zoho_oauth) wins over the one pasted
    # into Railway: it is newer and carries the Mail/Calendar scopes.
    try:
        import zoho_oauth
        refresh_token = zoho_oauth.stored_refresh_token()
    except Exception:
        refresh_token = os.getenv("ZOHO_REFRESH_TOKEN")
    client_id = os.getenv("ZOHO_CLIENT_ID")
    client_secret = os.getenv("ZOHO_CLIENT_SECRET")
    
    if not all([client_id, client_secret, refresh_token]) or "your_" in str(client_id):
        print("[zoho_client] Missing or placeholder Zoho credentials", flush=True)
        return None
    
    token = None
    
    # Try standard OAuth first
    try:
        url = f"https://accounts.zoho.{DC_SUFFIX}/oauth/v2/token"
        params = {
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token"
        }
        response = requests.post(url, params=params, timeout=30)
        data = response.json()
        if response.status_code == 200 and data.get("access_token"):
            token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)
            _token_cache["access_token"] = token
            _token_cache["expires_at"] = now + expires_in
            return token
        print(f"[zoho_client] Token refresh failed: {response.status_code} {data}", flush=True)
    except Exception as e:
        print(f"[zoho_client] Failed to refresh token: {e}", flush=True)
    
    return None


def get_headers() -> Optional[Dict[str, str]]:
    token = get_access_token()
    if not token:
        return None
    return {
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json"
    }


def api_request(method: str, url: str, **kwargs) -> Optional[Dict[Any, Any]]:
    """Make authenticated API request with auto-retry on auth failure."""
    headers = get_headers()
    if not headers:
        return None
    
    kwargs.setdefault('headers', {}).update(headers)
    kwargs.setdefault('timeout', 60)
    
    try:
        response = requests.request(method, url, **kwargs)
        if response.status_code == 401:
            # Token might be expired, refresh and retry
            get_access_token(force_refresh=True)
            headers = get_headers()
            if headers:
                kwargs['headers'].update(headers)
                response = requests.request(method, url, **kwargs)
        return response
    except Exception as e:
        print(f"[zoho_client] Request error: {e}", flush=True)
        return None


def is_configured() -> bool:
    client_id = os.getenv("ZOHO_CLIENT_ID", "")
    client_secret = os.getenv("ZOHO_CLIENT_SECRET", "")
    try:
        import zoho_oauth
        refresh_token = zoho_oauth.stored_refresh_token()
    except Exception:
        refresh_token = os.getenv("ZOHO_REFRESH_TOKEN", "")
    return all([client_id, client_secret, refresh_token]) and "your_" not in client_id


def invalidate_token_cache() -> None:
    _token_cache["access_token"] = None
    _token_cache["expires_at"] = 0
