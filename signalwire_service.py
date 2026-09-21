import os

import requests
from signalwire.rest.client import RestClient


class SignalWireService:
    """Outbound voice/SMS helper for the Broom Service AI guest assistant."""

    def __init__(self):
        self.project_id = os.getenv("SIGNALWIRE_PROJECT_ID", "")
        self.api_token = os.getenv("SIGNALWIRE_API_TOKEN", "")
        self.space_url = os.getenv("SIGNALWIRE_SPACE_URL", "yourspace.signalwire.com")
        self.from_number = os.getenv("SIGNALWIRE_PHONE", "")
        self._client = None

    @property
    def client(self):
        if self._client is None:
            host = (self.space_url or "").strip()
            if host.startswith("http://"):
                host = host[7:]
            elif host.startswith("https://"):
                host = host[8:]
            self._client = RestClient(
                project=self.project_id,
                token=self.api_token,
                host=host,
            )
        return self._client

    def is_configured(self) -> bool:
        return bool(
            self.project_id
            and self.api_token
            and self.space_url != "yourspace.signalwire.com"
            and self.from_number
        )

    def send_sms(self, to: str, body: str) -> bool:
        """Send an outbound SMS to a guest via SignalWire."""
        if not self.is_configured() or not to or not body:
            return False
        try:
            self.client.messages.create(
                from_=self.from_number,
                to=to,
                body=body,
            )
            return True
        except Exception as e:
            print(f"❌ SignalWire SMS send failure: {e}")
            return False

    def send_link_sms(self, to: str, entry_link: str) -> bool:
        """Send a secure digital entry link mid-call."""
        body = (
            "Hi! Here is your secure entry link for your stay: "
            f"{entry_link}. Your host (Broom Service) wishes you a great stay!"
        )
        return self.send_sms(to, body)

    def create_outbound_call(self, to: str, twiml_url: str) -> str:
        """Place an outbound AI call to a lead; returns the SignalWire CallSid."""
        if not self.is_configured() or not to or not twiml_url:
            return ""
        try:
            host = (self.space_url or "").strip()
            if host.startswith("http://"):
                host = host[7:]
            elif host.startswith("https://"):
                host = host[8:]
            url = f"https://{host}/api/laml/2010-04-01/Accounts/{self.project_id}/Calls"
            resp = requests.post(
                url,
                data={
                    "From": self.from_number,
                    "To": to,
                    "Url": twiml_url,
                },
                auth=(self.project_id, self.api_token),
                timeout=30,
            )
            if not resp.ok:
                print(f"❌ SignalWire outbound call failure: HTTP {resp.status_code} {resp.text[:400]}")
                return ""
            call = resp.json()
            return str(call.get("sid", "") or "")
        except Exception as e:
            print(f"❌ SignalWire outbound call failure: {e}")
            return ""

    def create_ai_outbound_call(self, to: str, swml_url: str) -> str:
        """Place an outbound AI call via the Calling API (SWML url variant); returns the call id."""
        if not self.is_configured() or not to or not swml_url:
            return ""
        try:
            host = (self.space_url or "").strip()
            if host.startswith("http://"):
                host = host[7:]
            elif host.startswith("https://"):
                host = host[8:]
            url = f"https://{host}/api/calling/calls"
            headers = {"Content-Type": "application/json"}
            resp = requests.post(
                url,
                headers=headers,
                json={
                    "command": "dial",
                    "params": {
                        "from": self.from_number,
                        "to": to,
                        "url": swml_url,
                    },
                },
                auth=(self.project_id, self.api_token),
                timeout=30,
            )
            if not resp.ok:
                print(f"❌ SignalWire AI outbound call failure: HTTP {resp.status_code} {resp.text[:400]}")
                return ""
            body = resp.json()
            result = body.get("result") or body
            if isinstance(result, dict):
                for key in ("call_id", "call_sid", "sid", "id"):
                    if result.get(key):
                        return str(result[key])
            return ""
        except Exception as e:
            print(f"❌ SignalWire AI outbound call failure: {e}")
            return ""

    def send_estimate_link(self, to: str, link: str) -> bool:
        """Text a secure link from Buildstack Construction (deposit checkout, estimate, or scheduling page)."""
        body = (
            "Hi! Here is the secure link from Buildstack Construction Co.: "
            f"{link}. Reply here or call (757) 846-9275 anytime and we'll help."
        )
        return self.send_sms(to, body)
