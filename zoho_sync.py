import os
import requests

DC_SUFFIX = os.getenv("ZOHO_DC_SUFFIX", "com")  # Change to "eu" or "in" if your account isn't US-based
MCP_TOKEN_URL = os.getenv("ZOHO_MCP_TOKEN_URL", "https://mcp.zoho.com/baas/mcp/v1/oauth/079a8f081168a82c71bf6fcf859c3233/137816000000014284/token")


def get_zoho_access_token():
    refresh_token = os.getenv("ZOHO_REFRESH_TOKEN")
    client_id = os.getenv("ZOHO_CLIENT_ID")
    client_secret = os.getenv("ZOHO_CLIENT_SECRET")
    
    # Try MCP token URL first (for MCP-based OAuth)
    try:
        body = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret
        }
        response = requests.post(MCP_TOKEN_URL, data=body)
        data = response.json()
        if response.status_code == 200 and data.get("access_token"):
            return data.get("access_token")
        print(f"[zoho_sync] MCP token refresh failed: {response.status_code} {data}", flush=True)
    except Exception as e:
        print(f"[zoho_sync] MCP token refresh error: {e}", flush=True)
    
    # Fallback to standard Zoho OAuth
    url = f"https://accounts.zoho.{DC_SUFFIX}/oauth/v2/token"
    params = {
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token"
    }
    try:
        response = requests.post(url, params=params)
        data = response.json()
        if response.status_code != 200 or not data.get("access_token"):
            print(f"[zoho_sync] Token refresh failed: {response.status_code} {data}", flush=True)
            return None
        return data.get("access_token")
    except Exception as e:
        print(f"[zoho_sync] Failed to refresh token: {e}", flush=True)
        return None


def send_to_zoho_crm(lead_payload):
    client_id = os.getenv("ZOHO_CLIENT_ID")
    client_secret = os.getenv("ZOHO_CLIENT_SECRET")
    refresh_token = os.getenv("ZOHO_REFRESH_TOKEN")
    
    if not all([client_id, client_secret, refresh_token]) or "your_" in str(client_id):
        print("[zoho_sync] Missing or placeholder Zoho credentials - skipping sync. Set ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN", flush=True)
        return None
    
    token = get_zoho_access_token()
    if not token:
        print("[zoho_sync] No access token obtained, skipping Zoho sync", flush=True)
        return None

    url = f"https://www.zohoapis.{DC_SUFFIX}/crm/v8/Leads"
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(url, json={"data": [lead_payload]}, headers=headers)
        print(f"[zoho_sync] POST {url} -> {response.status_code}", flush=True)
        if response.status_code not in (200, 201):
            print(f"[zoho_sync] Error body: {response.text}", flush=True)
        else:
            print(f"[zoho_sync] Success: {response.text[:200]}", flush=True)
        return response.json()
    except Exception as e:
        print(f"[zoho_sync] Request failed: {e}", flush=True)
        return None
