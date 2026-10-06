"""Owner contact lookup via Skip Sherpa — web-facing, single address, opt-in.

Why this exists
---------------
skiptrace_service.py reads the public assessor record, which yields an owner
NAME and nothing else. Its own docstring says so. So an owner lookup on the
/skiptrace page cannot produce a phone or an email, and an address-only permit
lead stays unreachable: no channel for the email bot, no way to feed a CRM
that requires a contact.

Skip Sherpa's PUT /api/properties is a real skip trace and returns owner name
plus phone_numbers and emails. It is per-lookup billable, so this module is
built for ONE address at a time, initiated by a person who has just looked at
the owner and decided they want to reach them. There is no batch path and no
background caller. That is the whole safety design: the cost is spent one
click at a time, never in a sweep.

Ported from skip_sherpa.py, which does the same API calls but as a batch CLI.
The request/response parsing is unchanged; the batching, the --apply writes,
and the credit ceilings stay in the CLI where they belong. This file has no
database writes at all -- the caller decides what to save, because what is
safe to save is app-specific.

Two behaviours are carried over on purpose and must not be dropped:

  - Phones are RETURNED but not written. Measured on a real Chesapeake owner:
    9 numbers came back, 7 flagged DNC by the registry, 2 last confirmed in
    2011. This is consumer mobiles belonging to people who registered against
    being called. The DNC flag and last_seen are surfaced to the caller rather
    than filtered here, so the decision stays visible.
  - Junk email domains are dropped. A throwaway address is technically valid
    and useless for outreach.

Requires SKIP_SHERPA_API_KEY. Without it, configured() is False and the route
returns a clear message rather than spending a credit.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

API_ROOT = "https://skipsherpa.com"

# Cloudflare fronts this API and answers python-urllib with Error 1010 "Access
# denied" -- a bot-fingerprint rule. It does not 403 as an auth failure, it
# looks like one, so the key looks wrong when it is fine. Both failures cost
# zero credits because the request never reaches the API.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

TIMEOUT = 60

# success_criteria is an ENUM, not an object. An unrecognised value does not
# 400 -- the lookup simply never reaches the success condition, so the whole
# request is billed and returns nothing. 'owner-contact-any' means an owner
# with at least one reachable method, so a name-only match is never paid for.
SUCCESS_CRITERIA = "owner-contact-any"

# Disposable/webmail addresses are valid and useless for outreach.
JUNK_EMAIL_DOMAINS = {
    "example.com", "domain.com", "email.com", "test.com", "yopmail.com",
    "mailinator.com", "guerrillamail.com", "tempmail.com", "throwawaymail.com",
}

_STATE_ZIP = re.compile(r"^(.*?),\s*([A-Za-z]{2})\s*(\d{5}(?:-\d{4})?)?$")


class ProviderError(Exception):
    """An upstream failure, as distinct from 'no contact found at this address'.

    `spendable` is True when the request reached the API and consumed a credit
    even though it failed, so the caller can tell the owner whether retrying is
    free. A malformed address or a Cloudflare block spends nothing.
    """

    def __init__(self, message, status=502, spendable=False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.spendable = spendable


def api_key() -> str:
    return (os.getenv("SKIP_SHERPA_API_KEY") or "").strip()


def configured() -> bool:
    return bool(api_key())


def parse_address(raw: str) -> dict:
    """'1501 VANCE CIR, Chesapeake, VA 23320' -> the address shape the API wants.

    Unlike skiptrace_service.parse_address, a missing ZIP IS a failure here.
    Skip Sherpa keys its data on the full property address; a lookup without a
    ZIP either misses or, worse, matches a different door. Both cost a credit,
    so it is refused before the request is sent.
    """
    text = str(raw or "").replace("\r\n", " ").replace("\r", " ").replace("\n", ", ")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return {"ok": False, "why": "empty address"}
    street = parts[0]
    city = state = zipcode = ""
    for chunk in parts[1:]:
        if re.fullmatch(r"[A-Za-z]{2}", chunk.strip()):
            state = chunk.strip().upper()
            continue
        z = re.search(r"\b\d{5}(?:-\d{4})?\b", chunk)
        if z:
            zipcode = z.group(0)
            rest = chunk.replace(zipcode, "").strip(" ,")
            if rest:
                city = city or rest
            continue
        if not city:
            city = chunk
    if not state:
        m = _STATE_ZIP.search(text)
        if m:
            state = m.group(2).upper()
            zipcode = zipcode or (m.group(3) or "")
    if not state:
        return {"ok": False, "why": "no 2-letter state in the address"}
    if not city:
        return {"ok": False, "why": "no city in the address"}
    if not zipcode:
        return {"ok": False, "why": "no ZIP — Skip Sherpa needs the full address or it may match a different property"}
    return {"ok": True, "street": street, "city": city.upper(), "state": state, "zipcode": zipcode}


def _put(path: str, payload: dict) -> dict:
    """Every /api/* lookup is PUT, not POST, and answers 405 to a POST."""
    key = api_key()
    req = urllib.request.Request(
        f"{API_ROOT}{path}",
        data=json.dumps(payload).encode(),
        method="PUT",
        headers={
            "Content-Type": "application/json",
            "api-key": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if exc.code in (401, 403):
            lowered = (detail or "").lower()
            if "1010" in lowered or "cloudflare" in lowered or "just a moment" in lowered:
                raise ProviderError(
                    "Skip Sherpa answered with a Cloudflare challenge (Error 1010). That is a "
                    "bot-fingerprint block, not a bad key — do not rotate it. No credit was spent.",
                    status=502, spendable=False,
                ) from None
            raise ProviderError(
                "Skip Sherpa rejected the API key (401). Check SKIP_SHERPA_API_KEY.",
                status=502, spendable=False,
            ) from None
        # 4xx other than auth: the request reached the API and was billed.
        raise ProviderError(f"Skip Sherpa API error {exc.code}: {detail or exc.reason}",
                            status=502, spendable=exc.code not in (404,)) from None
    except urllib.error.URLError as exc:
        raise ProviderError(f"Could not reach Skip Sherpa: {exc.reason}", spendable=False) from None


def _persons(node) -> list:
    """Walk the nested person objects out of the response.

    The shape is property_results[].property.owners[].person, with the name under
    person_name and contacts under emails / phone_numbers. Guessing these paths
    returns nothing while the API is returning HTTP 200 with all the data,
    which looks identical to a genuine miss.
    """
    found = []
    if isinstance(node, dict):
        if isinstance(node.get("person"), dict):
            found.append(node["person"])
        owners = node.get("owners")
        if isinstance(owners, list):
            for o in owners:
                if isinstance(o, dict) and isinstance(o.get("person"), dict):
                    found.append(o["person"])
        for k, v in node.items():
            if k in ("owners", "person"):
                continue
            if isinstance(v, (dict, list)):
                found.extend(_persons(v))
    elif isinstance(node, list):
        for item in node:
            found.extend(_persons(item))
    return found


def _person_name(person: dict) -> str:
    pn = person.get("person_name") or {}
    fn = str(pn.get("first_name") or "").strip()
    ln = str(pn.get("last_name") or "").strip()
    if fn and ln:
        return f"{fn.title()} {ln.title()}"
    full = str(pn.get("full_name") or person.get("display_name") or "").strip()
    return full or (fn or ln).title()


def _person_phones(person: dict) -> list:
    """Phones as (e164, type, is_dnc, last_seen).

    The DNC flag is carried through and NOT filtered here. Whether a
    do-not-call registry entry blocks outreach depends on whose number it is
    and which channel is used, so the decision is left visible to the caller
    rather than silently made in a parser.
    """
    out = []
    for p in person.get("phone_numbers") or []:
        if not isinstance(p, dict):
            continue
        e164 = str(p.get("e164_format") or "").strip()
        digits = re.sub(r"\D", "", e164)
        # Require a real E.164. '+' alone passes a naive length check on the
        # non-digit characters, and that empty string was being picked as the
        # preferred number.
        if not e164.startswith("+") or not (10 <= len(digits) <= 15):
            continue
        dnc = False
        for st in p.get("dnc_statuses") or []:
            if isinstance(st, dict) and st.get("is_dnc"):
                dnc = True
                break
        out.append((e164, str(p.get("type") or ""), dnc, str(p.get("last_seen") or "")))
    return out


def _person_emails(person: dict) -> list:
    out = []
    for e in person.get("emails") or []:
        addr = (e.get("email_address") if isinstance(e, dict) else e) or ""
        addr = str(addr).strip().lower()
        if "@" in addr and addr.rsplit("@", 1)[-1] not in JUNK_EMAIL_DOMAINS:
            out.append(addr)
    return out


def phone_is_usable(phone_type: str, dnc: bool, last_seen: str) -> bool:
    """DNC is never usable. Stale numbers are not either.

    This data returns numbers last confirmed in 2011, and a stale landline is
    worse than no number at all: it dials a stranger or rings forever.
    """
    if dnc:
        return False
    if (phone_type or "").lower() in ("landline", ""):
        return False
    if last_seen and last_seen < (time.strftime("%Y") + "-01-01"):
        return False
    return True


def trace(street: str, city: str, state: str, zipcode: str) -> dict:
    """One address, one credit. Owner name, phones and emails, or an error.

    Returns a dict with `phones` (each carrying type/dnc/last_seen/usable),
    `emails`, and a `preferred_phone` that is ONLY set when it passed
    phone_is_usable(). `raw_phone` is the top-ranked number regardless, for
    display. Nothing here writes anything anywhere.
    """
    if not api_key():
        raise ProviderError("Skip Sherpa is not configured (SKIP_SHERPA_API_KEY is not set).",
                            status=400, spendable=False)

    payload = {"property_lookups": [{
        "property_address_lookup": {
            "street": street, "city": city, "state": state, "zipcode": zipcode,
        },
        "success_criteria": SUCCESS_CRITERIA,
        "debt_data_best_effort": False,
    }]}
    data = _put("/api/properties", payload)
    if data.get("error"):
        raise ProviderError(f"Skip Sherpa: {data['error']}", spendable=True)

    results = data.get("property_results") or data.get("property_lookup_results") or []
    if not results:
        raise ProviderError("Skip Sherpa returned no result row for this address.", spendable=True)
    first = results[0] if isinstance(results[0], dict) else {}

    code = first.get("status_code")
    if code and int(code) != 200:
        issues = first.get("issues") or []
        detail = issues[0].get("detail") if issues and isinstance(issues[0], dict) else ""
        # Not an error worth a 500: the address genuinely has no reachable owner.
        return {
            "ok": False, "found": False,
            "message": f"No contact found for this address ({detail or code}). "
                       "That is a real answer, not a failure — a credit was spent.",
        }

    prop = first.get("property") or {}
    if not isinstance(prop, dict) or not prop:
        return {
            "ok": False, "found": False,
            "message": "Skip Sherpa did not match this address to a property. A credit was spent.",
        }

    people = _persons(prop)
    if not people:
        return {
            "ok": False, "found": False,
            "message": "Skip Sherpa matched the property but returned no owner person. A credit was spent.",
        }

    names, phones, emails = [], [], []
    for person in people:
        nm = _person_name(person)
        if nm and nm not in names:
            names.append(nm)
        phones.extend(_person_phones(person))
        emails.extend(_person_emails(person))

    def rank(rec):
        e164, typ, _dnc, seen = rec
        is_mobile = "mobile" in typ.lower() or "cell" in typ.lower()
        return (0 if is_mobile else 1, "" if seen else "9999", seen, e164)

    ordered = sorted(phones, key=rank)
    seen_phones = []
    for e164, typ, dnc, seen in ordered:
        if any(p["e164"] == e164 for p in seen_phones):
            continue
        seen_phones.append({
            "e164": e164, "type": typ, "dnc": dnc, "last_seen": seen,
            "usable": phone_is_usable(typ, dnc, seen),
        })

    unique_emails = []
    for e in emails:
        if e not in unique_emails:
            unique_emails.append(e)

    usable = [p for p in seen_phones if p["usable"]]
    return {
        "ok": True,
        "found": True,
        "owner": names[0] if names else "",
        "all_owners": names[:4],
        "phones": seen_phones[:6],
        "preferred_phone": usable[0]["e164"] if usable else "",
        "raw_phone": seen_phones[0]["e164"] if seen_phones else "",
        "phone_count": len(phones),
        "dnc_count": sum(1 for p in seen_phones if p["dnc"]),
        "emails": unique_emails[:6],
        "email": unique_emails[0] if unique_emails else "",
        "email_count": len(unique_emails),
        "owner_occupied": prop.get("owner_occupied"),
        "value": prop.get("estimated_value"),
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
