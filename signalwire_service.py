import os
import re
import urllib.parse

import requests
import transport_health
from signalwire.rest.client import RestClient

import brand

DEFAULT_SPACE = "yourspace.signalwire.com"
# 30s covers SignalWire's own retry against the carrier, without leaving the
# owner's request (or a campaign pass) blocked indefinitely when the API hangs.
REQUEST_TIMEOUT = 30

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


class SignalWireError(RuntimeError):
    """A call could not be placed, with a reason the owner can act on."""


def e164(number) -> str:
    """Return a valid E.164 number, or ``""`` when the input cannot be one.

    Ten-digit US numbers get the country code and an 11-digit number starting
    with 1 is already US. Anything else has to arrive with its own ``+country``
    code: guessing a country for a residential lead is how the wrong person
    gets dialled. A malformed number must be refused *before* the carrier,
    because a rejected call is cheap but a misdialled one is not.
    """
    raw = str(number or "").strip()
    if not raw:
        return ""
    with_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if with_plus:
        candidate = "+" + digits
        return candidate if _E164_RE.fullmatch(candidate) else ""
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return ""


def _space_host(value) -> str:
    """Bare host for the API: accepts ``https://space/`` or ``space``."""
    raw = str(value or "").strip()
    if raw.startswith(("http://", "https://")):
        raw = urllib.parse.urlsplit(raw).netloc
    return raw.strip().strip("/")


def _absolute_https(url) -> bool:
    """True only for an absolute https URL the carrier can actually fetch."""
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.netloc)


class SignalWireService:
    """Outbound voice/SMS helper for the Buildstack Construction assistant."""

    def __init__(self):
        self.project_id = (os.getenv("SIGNALWIRE_PROJECT_ID") or "").strip()
        self.api_token = (os.getenv("SIGNALWIRE_API_TOKEN") or "").strip()
        self.space_url = (os.getenv("SIGNALWIRE_SPACE_URL") or DEFAULT_SPACE).strip()
        self.from_number = (os.getenv("SIGNALWIRE_PHONE") or "").strip()
        self._client = None

    @property
    def host(self) -> str:
        return _space_host(self.space_url)

    @property
    def client(self):
        if self._client is None:
            self._client = RestClient(
                project=self.project_id,
                token=self.api_token,
                host=self.host,
            )
        return self._client

    def missing(self) -> list:
        """Names of the environment variables that keep this service down.

        Returning the names (rather than a bare False) is what lets an
        owner-facing route say *which* credential is absent instead of
        "the phone line is broken".
        """
        missing = []
        if not self.project_id:
            missing.append("SIGNALWIRE_PROJECT_ID")
        if not self.api_token:
            missing.append("SIGNALWIRE_API_TOKEN")
        if not self.host or self.host.lower() == DEFAULT_SPACE:
            missing.append("SIGNALWIRE_SPACE_URL")
        if not self.from_number:
            missing.append("SIGNALWIRE_PHONE")
        return missing

    def is_configured(self) -> bool:
        return not self.missing()

    @staticmethod
    def _fail(message: str, raise_on_error: bool) -> str:
        print(f"❌ {message}", flush=True)
        if raise_on_error:
            raise SignalWireError(message)
        return ""

    def _preflight(self, to, url, label: str, raise_on_error: bool):
        """Validate everything that must be right before a call is placed.

        Returns ``(to_e164, from_e164)`` or ``None``. Every failure is logged
        once and, when the caller asked for it, raised as SignalWireError so a
        route can show the owner why nothing was dialled.
        """
        if not self.is_configured():
            problem = (f"{label}: SignalWire is not configured "
                       f"(missing {', '.join(self.missing()) or 'credentials'})")
        elif not e164(to):
            problem = f"{label}: not a usable E.164 phone number: {to!r}"
        elif not e164(self.from_number):
            problem = f"{label}: SIGNALWIRE_PHONE is not an E.164 number: {self.from_number!r}"
        elif not _absolute_https(url):
            problem = (f"{label}: instruction URL must be absolute https "
                       f"(SignalWire fetches it), got {url!r}")
        else:
            return e164(to), e164(self.from_number)
        return self._fail(problem, raise_on_error)

    def send_sms(self, to: str, body: str) -> bool:
        """Send an outbound SMS to a contact via SignalWire."""
        if not self.is_configured() or not to or not body:
            return False
        try:
            self.client.messages.create(
                from_=self.from_number,
                to=to,
                body=body,
            )
            # Recorded so a provider-side stop (an inactive A2P campaign, for
            # one) surfaces as an alert instead of three weeks of silence.
            transport_health.record("sms", True)
            return True
        except Exception as e:
            print(f"❌ SignalWire SMS send failure: {e}")
            transport_health.record("sms", False, str(e))
            return False

    def create_outbound_call(self, to: str, twiml_url: str,
                             raise_on_error: bool = False) -> str:
        """Place an outbound AI call; returns the SignalWire CallSid.

        ``raise_on_error`` is for owner-facing routes that need to show why a
        call was refused. The default is unchanged (return ``""``) so existing
        Copilot and campaign callers keep their current behaviour.
        """
        ready = self._preflight(to, twiml_url, "SignalWire outbound call", raise_on_error)
        if not ready:
            return ""
        number, sender = ready
        try:
            url = (f"https://{self.host}/api/laml/2010-04-01/Accounts/"
                   f"{self.project_id}/Calls")
            resp = requests.post(
                url,
                data={
                    "From": sender,
                    "To": number,
                    "Url": twiml_url,
                },
                auth=(self.project_id, self.api_token),
                timeout=REQUEST_TIMEOUT,
            )
            if not resp.ok:
                detail = (resp.text or "").strip()[:400]
                return self._fail(
                    f"SignalWire rejected the call: HTTP {resp.status_code}"
                    + (f" {detail}" if detail else ""), raise_on_error)
            call = resp.json()
            sid = str((call or {}).get("sid", "") or "")
            if not sid:
                return self._fail("SignalWire accepted the call but returned no sid",
                                  raise_on_error)
            return sid
        except SignalWireError:
            raise
        except Exception as e:
            return self._fail(f"SignalWire outbound call failed: {e}", raise_on_error)

    def create_ai_outbound_call(self, to: str, swml_url: str,
                                raise_on_error: bool = False) -> str:
        """Place an outbound AI call via the Calling API (SWML url variant).

        Returns the carrier call id, or ``""`` on failure. With
        ``raise_on_error=True`` a refusal raises :class:`SignalWireError`
        carrying the HTTP status and response body, which is what the owner
        route reports back instead of a silent no-op.
        """
        ready = self._preflight(to, swml_url, "SignalWire AI outbound call", raise_on_error)
        if not ready:
            return ""
        number, sender = ready
        try:
            url = f"https://{self.host}/api/calling/calls"
            headers = {"Content-Type": "application/json"}
            resp = requests.post(
                url,
                headers=headers,
                json={
                    "command": "dial",
                    "params": {
                        "from": sender,
                        "to": number,
                        "url": swml_url,
                    },
                },
                auth=(self.project_id, self.api_token),
                timeout=REQUEST_TIMEOUT,
            )
            if not resp.ok:
                detail = (resp.text or "").strip()[:400]
                return self._fail(
                    f"SignalWire rejected the AI call: HTTP {resp.status_code}"
                    + (f" {detail}" if detail else ""), raise_on_error)
            body = resp.json() or {}
            result = body.get("result") or body
            if isinstance(result, dict):
                for key in ("call_id", "call_sid", "sid", "id"):
                    if result.get(key):
                        return str(result[key])
            return self._fail("SignalWire accepted the call but returned no call id",
                              raise_on_error)
        except SignalWireError:
            raise
        except Exception as e:
            return self._fail(f"SignalWire AI outbound call failed: {e}", raise_on_error)

    def send_estimate_link(self, to: str, link: str) -> bool:
        """Text a secure link from Buildstack Construction (deposit checkout, estimate, or scheduling page)."""
        body = (
            f"Hi! Here is the secure link from {brand.name()}: "
            f"{link}. Reply here or call (757) 846-9275 anytime and we'll help."
        )
        return self.send_sms(to, body)
