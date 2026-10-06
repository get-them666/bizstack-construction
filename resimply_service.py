"""REsimpli Open API client.

REsimpli is the investor CRM. This pushes a lead we already own a verified
contact for into it, so the deal team works one pipeline instead of two.

Scope, deliberately narrow:

  - One endpoint: POST /lead/add. Reading lists back out is not needed; the
    leads page here is the system of record and REsimpli is downstream of it.
  - Nothing is sent automatically. push_lead() is called from an explicit
    route after a human has staged and reviewed the draft. A cold residential
    homeowner is TCPA/CAN-SPAM territory and must never be emailed by a
    background job.

UNVERIFIED AGAINST THE LIVE API
-------------------------------
The token in the settings screenshot (Open API Token, not the Zapier one)
answers 401 "Record not found!" on every endpoint -- byte-identical to what a
random string returns, while a *missing* header gives a different error
("Invalid param!"). So the Authorization header is being parsed and reaching
the server, but no integration record matches. Two possibilities:

  1. That key belongs to a different REsimpli account than the one behind
     this site, or
  2. REsimpli 6.0 has not issued an Open API key for this account yet.

Because of that, every call here is built from the published endpoint catalog
(https://developer.resimpli.com) and has NEVER been executed successfully.
Treat the request shapes as a best reading of the docs, not as fact. The
methods below return a structured error instead of raising, and
configured() reports honestly, so the app degrades to "not connected" rather
than to a 500 on the leads page.

To turn this on, set a working key:

    RESIMPLY_API_TOKEN=<token>            # env, or
    _set_setting(db, "resimply_api_token", "<token>")   # app_settings

Then re-verify with:

    python3 -c "import resimply_service as r; print(r.verify())"

Auth shape, from the tester bundle: base https://api.resimpli.com/api/v6/openapi,
all endpoints POST, token sent as `Authorization: Bearer <token>`.
"""

import json
import os
import time
import urllib.error
import urllib.request

API_BASE = "https://api.resimpli.com/api/v6/openapi"
TIMEOUT = 30
UA = "bizstack-perks-resimply/1.0"

# key -> app_settings fallback, mirroring _turno_cfg() in construction_main.py.
TOKEN_ENV = "RESIMPLY_API_TOKEN"
TOKEN_SETTING = "resimply_api_token"

# lead/add marks these required. They are ObjectIds that exist only inside one
# REsimpli account and must be read from that account (marketingCampaignList,
# marketList, getPipelineList), so they are configurable rather than hardcoded.
ID_FIELDS = {
    "marketingCampignId": ("RESIMPLY_CAMPAIGN_ID", "resimply_campaign_id"),
    "marketId": ("RESIMPLY_MARKET_ID", "resimply_market_id"),
    "pipelineId": ("RESIMPLY_PIPELINE_ID", "resimply_pipeline_id"),
    "mainStatusId": ("RESIMPLY_STATUS_ID", "resimply_status_id"),
}


class ResimplyError(Exception):
    """An upstream failure worth showing the owner.

    `auth` distinguishes "your key is wrong or not issued yet" from every other
    failure, because that one needs a completely different fix and is by far
    the most likely thing to go wrong here.
    """

    def __init__(self, message, status=502, auth=False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.auth = auth


def token(get_setting=None) -> str:
    """Env first, then the app_settings row. Same order as _turno_cfg."""
    env = (os.getenv(TOKEN_ENV, "") or "").strip()
    if env:
        return env
    if get_setting:
        return (get_setting(TOKEN_SETTING) or "").strip()
    return ""


def _account_ids(get_setting=None) -> dict:
    """The four account-scoped ObjectIds lead/add requires."""
    out = {}
    for field, (env_name, setting_key) in ID_FIELDS.items():
        value = (os.getenv(env_name, "") or "").strip()
        if not value and get_setting:
            value = (get_setting(setting_key) or "").strip()
        out[field] = value
    return out


def configured(get_setting=None) -> bool:
    return bool(token(get_setting))


def _post(path, payload, tok):
    url = API_BASE + path
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {tok}",
            "User-Agent": UA,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        try:
            body = json.loads(detail)
            message = body.get("message") or detail
        except Exception:
            message = detail or str(exc.reason)
        # REsimpli returns 401 "Record not found!" for a token it does not
        # recognise -- the same body it returns for a garbage token. Saying so
        # plainly is the difference between the owner re-checking the key and
        # the owner assuming the CRM is down.
        if exc.code in (401, 403):
            raise ResimplyError(
                "REsimpli rejected the API key (401). The key parsed but matches no "
                "integration record -- confirm it is the Open API Token (not the Zapier "
                "one) and that it belongs to this REsimpli account. Nothing was sent.",
                status=401, auth=True,
            ) from None
        raise ResimplyError(f"REsimpli API error {exc.code}: {message}") from None
    except urllib.error.URLError as exc:
        raise ResimplyError(f"Could not reach REsimpli API: {exc.reason}") from None


def verify(get_setting=None) -> dict:
    """Validate the key. Uses the endpoint documented for exactly this."""
    tok = token(get_setting)
    if not tok:
        return {"ok": False, "configured": False, "error": "No REsimpli token configured."}
    try:
        data = _post("/getUserInfo", {}, tok)
    except ResimplyError as exc:
        return {"ok": False, "configured": True, "auth": exc.auth, "error": exc.message}
    contact = (data.get("data") or {}).get("contactDetails") or {}
    return {
        "ok": True,
        "configured": True,
        "first_name": contact.get("firstName") or "",
        "last_name": contact.get("lastName") or "",
        "raw": data,
    }


def missing_ids(get_setting=None) -> list:
    """Which of the four required ObjectIds are not set yet."""
    return [f for f, v in _account_ids(get_setting).items() if not v]


def _digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _normalise_phone(phone: str) -> str:
    """10 digits, which is what lead/add documents ('10-digit phone number').

    lead/add requires a phone, so this returns "" rather than a guess when there
    is no usable number -- and the caller stages nothing rather than inventing
    one. A wrong phone on a CRM record is worse than a missing one: it dials a
    stranger.
    """
    digits = _digits(phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def _split_name(full_name: str) -> tuple:
    """'Leonard G Barlow' -> ('Leonard', 'Barlow'). REsimpli wants first+last.

    First token first, last token last, any middle name dropped. REsimpli has no
    middle-name field, so folding the middle into lastName (the obvious
    implementation) puts "G Barlow" in front of the owner, which reads as a
    data-entry error on a record someone may email. A dropped middle initial is
    a much smaller thing to correct by hand.
    """
    parts = [p for p in str(full_name or "").strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def build_lead_payload(*, name, email, phone, address, property_type="residential",
                       comment="", lead_created="", is_hot_lead=False, get_setting=None):
    """Assemble the lead/add body. Returns (payload, problems).

    Problems is non-empty when the lead cannot be created faithfully -- a
    missing name, phone, email, or one of the account ObjectIds. The caller
    reports those instead of sending a half-built record.

    propertyType is land|residential per the docs; a skip-traced assessor hit
    is a house, so residential is the default.
    """
    problems = []
    first, last = _split_name(name)
    if not first:
        problems.append("no owner name on this lead")

    clean_email = str(email or "").strip().lower()
    if not clean_email or "@" not in clean_email:
        problems.append("no email address on this lead")

    tel = _normalise_phone(phone)
    if not tel:
        problems.append("no usable 10-digit phone (lead/add requires one)")

    ids = _account_ids(get_setting)
    for field, value in ids.items():
        if not value:
            problems.append(
                f"{field} not set — read it from this REsimpli account "
                f"(marketingCampaignList / marketList / getPipelineList)"
            )

    if property_type not in ("land", "residential"):
        property_type = "residential"

    payload = {
        "contactDetails": {},
        "propertyType": property_type,
        "address": str(address or "").strip(),
    }
    if first:
        payload["contactDetails"]["firstName"] = first
    if last:
        payload["contactDetails"]["lastName"] = last
    if tel:
        payload["contactDetails"]["phoneNumbers"] = [
            {"phoneNumber": tel, "isPrimary": True}
        ]
    if clean_email and "@" in clean_email:
        payload["contactDetails"]["emails"] = [
            {"email": clean_email, "isPrimary": True}
        ]
        # The address is a person's own mailbox, so it is not marketing consent.
        # Defaulting these false keeps REsimpli's own filters honest.
        payload["contactDetails"]["marketingOptIn"] = False
    if comment:
        payload["comment"] = str(comment)[:1000]
    if lead_created:
        payload["leadCreated"] = lead_created
    if is_hot_lead:
        payload["isHotLead"] = True

    payload.update({k: v for k, v in ids.items() if v})
    return payload, problems


def push_lead(*, name, email, phone, address, comment="", property_type="residential",
              get_setting=None, tok=None):
    """Create the lead in REsimpli. Raises ResimplyError; never sends partially.

    Called from an explicit route after a human has reviewed the staged draft.
    No background job calls this.
    """
    tok = (tok or token(get_setting) or "").strip()
    if not tok:
        raise ResimplyError("REsimpli is not configured.", status=400)

    payload, problems = build_lead_payload(
        name=name, email=email, phone=phone, address=address,
        property_type=property_type, comment=comment, get_setting=get_setting,
    )
    if problems:
        raise ResimplyError(
            "Not enough to create this lead: " + "; ".join(problems), status=400
        )

    data = _post("/lead/add", payload, tok)
    return {
        "ok": True,
        "response": data,
        "pushed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "payload": payload,
    }
