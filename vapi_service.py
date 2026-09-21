"""Vapi voice-agent client for AI outbound calls.

Everything is done through Vapi's REST API so no dashboard setup is needed:
- The assistant (customLLM) points back at this site's /vapi/llm endpoint, so the
  voice brain (tools, prompts, handlers) lives entirely in this app.
- The phone number is a free Vapi US number unless VAPI_PHONE_NUMBER_ID is set.
- Pin ids ahead of time with VAPI_ASSISTANT_ID / VAPI_PHONE_NUMBER_ID if desired.

Only enabled when VAPI_API_KEY is present. Falls back to the legacy SignalWire
path elsewhere, so this is purely additive.
"""

import json
import os
import threading
import urllib.request

API_BASE = "https://api.vapi.ai"


class VapiService:
    def __init__(self, default_base: str = "https://bizstackperks.com"):
        self._default_base = default_base
        self._assistant_id = None
        self._phone_id = None
        self._lock = threading.Lock()

    def is_configured(self) -> bool:
        return bool(os.getenv("VAPI_API_KEY"))

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {os.getenv('VAPI_API_KEY')}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API_BASE + path, data=data, headers=self._headers(), method=method)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode() or ("{}" if method != "GET" else "[]")
            return json.loads(raw)

    def _app_base(self) -> str:
        return (os.getenv("APP_BASE_URL", "") or self._default_base).rstrip("/")

    def _ensure_assistant(self) -> str:
        pinned = os.getenv("VAPI_ASSISTANT_ID", "") or os.getenv("VAPI_ASSISTANT_ID")
        if pinned and pinned.strip():
            return pinned.strip()
        if self._assistant_id:
            return self._assistant_id
        with self._lock:
            if self._assistant_id:
                return self._assistant_id
            existing = self._request("GET", "/assistant")
            if isinstance(existing, list):
                for a in existing:
                    if (a.get("name") or "").startswith("bizstack-voice"):
                        self._assistant_id = a["id"]
                        return a["id"]
            created = self._request("POST", "/assistant", {
                "name": "bizstack-voice",
                "model": {
                    "provider": "customLLM",
                    "url": f"{self._app_base()}/vapi/llm",
                    "model": "voice-agent",
                },
                "firstMessage": "Hey, thanks for reaching out — how can I help?",
                "voice": {
                    "provider": "11labs",
                    "voiceId": "charlie",
                    "stability": 0.6,
                    "similarityBoost": 0.85,
                },
            })
            self._assistant_id = created["id"]
            return created["id"]

    def _ensure_phone(self, assistant_id: str) -> str:
        pinned = os.getenv("VAPI_PHONE_NUMBER_ID", "").strip()
        if pinned:
            return pinned
        if self._phone_id:
            return self._phone_id
        with self._lock:
            if self._phone_id:
                return self._phone_id
            existing = self._request("GET", "/phone-number")
            if isinstance(existing, list):
                for p in existing:
                    if (p.get("name") or "").startswith("bizstack-voice"):
                        self._phone_id = p["id"]
                        return p["id"]
            created = self._request("POST", "/phone-number", {
                "provider": "vapi",
                "name": "bizstack-voice",
                "assistantId": assistant_id,
            })
            self._phone_id = created["id"]
            return created["id"]

    def create_ai_outbound_call(self, to: str, context: str = "", notes: str = "") -> str:
        if not self.is_configured():
            raise RuntimeError("Vapi is not configured (VAPI_API_KEY missing).")
        assistant_id = self._ensure_assistant()
        phone_id = self._ensure_phone(assistant_id)
        body = {
            "assistantId": assistant_id,
            "phoneNumberId": phone_id,
            "customer": {"number": to},
        }
        seed = (context or "").strip() or (notes or "").strip()
        if seed:
            body["assistantOverrides"] = {
                "firstMessage": seed,
                "model": {"messages": [{"role": "system", "content": seed}]},
            }
        created = self._request("POST", "/call", body)
        return created.get("id") or ""