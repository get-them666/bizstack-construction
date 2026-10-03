#!/usr/bin/env python3
"""Accurate Append: turn a name + address into an email and a phone number.

This is the step the assessor lookup cannot do. RentCast returns an owner NAME
from the public record and nothing else, because no public record contains an
email address. Accurate Append searches a consumer contact database, so it can
answer name -> email and name -> phone, which is what makes a permit-derived
lead contactable.

    GET https://api.accurateappend.com/Services/V2/AppendEmail/{key}/
    GET https://api.accurateappend.com/Services/V2/AppendPhone/SBMMobile/{key}/

Both require a Content-Type: application/json header or they 400, and both take
the license key in the URL path. Errors come back as {"Error": "..."} with a 4xx,
which is a body, not a transport failure -- the same distinction skip_sherpa
gets wrong.

CURRENTLY BLOCKED: as of 2026-10-03 the configured key returns
401 "You do not have an active subscription or are not authorized to access this
endpoint" on every endpoint. The key itself is recognised (a wrong key returns
"License key is required"), so it is the ACCOUNT that needs the subscription or
trial activated. Until then every call here returns an error shape and spends
nothing. See MEMORY.md.

Credentials come from the environment only:

    ACCURATE_APPEND_API_KEY

Match levels, per Accurate Append's own definition, and the reason
GOOD_MATCH_LEVELS below is not simply "everything":

    E1  exact match on all supplied fields
    E2  exact match, first name does not necessarily match
    N1  name + address match
    N2  name match
    B1/B2  household/basic match -- often the WRONG person at the address

A B-level hit on a property owner is frequently a different adult living there.
Writing that number against a named lead is how a contractor ends up calling a
stranger and thinking it is a customer, so B levels are excluded by default and
reported rather than silently dropped.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.accurateappend.com/Services/V2"
TIMEOUT = 30

NOT_FOUND = object()   # valid request, no match. Not an error.

# E1/E2 are exact, N1/N2 are name matches. B1/B2 are household-level and are
# excluded on purpose -- see the module docstring.
GOOD_MATCH_LEVELS = ("E1", "E2", "N1", "N2")


class ProviderError(Exception):
    """A real upstream failure, as distinct from 'no match found'."""

    def __init__(self, message, status=502, spendable=False):
        super().__init__(message)
        self.message = message
        self.status = status
        # Whether the request was billed. Quota exhaustion was; a 401 was not,
        # because authorization is refused before any lookup happens.
        self.spendable = spendable


def api_key() -> str:
    return (os.getenv("ACCURATE_APPEND_API_KEY") or "").strip()


def is_configured() -> bool:
    return bool(api_key())


def _get(path: str, params: dict):
    """GET and parse. Returns (status, body_dict).

    Errors are returned rather than raised because this API signals failure in
    the body alongside the status: a 401 comes back as {"Error": "..."}. Raising
    on the status alone would lose the provider's actual explanation, which is
    the only useful part of the message.
    """
    key = api_key()
    if not key:
        return None, {"Error": "ACCURATE_APPEND_API_KEY is not set"}

    url = f"{API_ROOT}/{path}/{urllib.parse.quote(key)}/"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Content-Type": "application/json",
        "User-Agent": "bizstack-enrichment/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        status = exc.code
    except Exception as exc:
        # Connection reset, DNS, timeout. Nothing was billed.
        raise ProviderError(f"network: {type(exc).__name__}", 502, spendable=False)

    body = None
    if raw.strip():
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
    if not isinstance(body, dict):
        body = {"Error": (raw or "")[:200]}
    return status, body


def _match_ok(level: str) -> bool:
    return (level or "").strip().upper() in GOOD_MATCH_LEVELS


def append_email(first: str, last: str, street: str = "", city: str = "",
                 state: str = "", postalcode: str = "") -> dict:
    """Name + address -> verified email address.

    Accurate Append returns up to five candidates, each with a match level.
    Only E1/E2/N1/N2 are returned; weaker household-level matches are counted
    in `rejected_weak` so the miss is explainable rather than a bare null.
    """
    first, last = (first or "").strip(), (last or "").strip()
    if not first or not last:
        return {"found": False, "error": "first and last name are required"}

    params = {"firstname": first, "lastname": last}
    if street:
        params["address"] = street.strip()
    if city:
        params["city"] = city.strip()
    if state:
        params["state"] = state.strip()
    if postalcode:
        params["postalcode"] = postalcode.strip()

    status, body = _get("AppendEmail", params)

    if status in (401, 403):
        raise ProviderError(
            f"Accurate Append rejected the account ({status}): "
            f"{body.get('Error', 'no detail')}. The key is read but the account "
            f"needs an active subscription.",
            status, spendable=False)
    if status == 429:
        raise ProviderError("Accurate Append quota exhausted.", 429, spendable=True)
    if status != 200:
        raise ProviderError(f"Accurate Append HTTP {status}: {body.get('Error', '')[:200]}",
                            502, spendable=True)

    emails = body.get("Emails") or []
    good = [e for e in emails if _match_ok(e.get("MatchLevel"))]
    weak = [e for e in emails if not _match_ok(e.get("MatchLevel"))]
    if not good:
        return {"found": False, "rejected_weak": len(weak),
                "error": "no email at an acceptable match level"}

    best = good[0]
    return {
        "found": True,
        "email": (best.get("Email") or "").strip().lower(),
        "match_level": best.get("MatchLevel"),
        "first_name": best.get("FirstName"),
        "last_name": best.get("LastName"),
        "alternates": [{"email": (e.get("Email") or "").strip().lower(),
                        "match_level": e.get("MatchLevel")} for e in good[1:3]],
        "rejected_weak": len(weak),
        "_source": "accurate_append",
    }


def append_mobile(first: str, last: str, street: str = "", city: str = "",
                  state: str = "", postalcode: str = "") -> dict:
    """Name + address -> mobile number.

    Returns the number in E.164 where possible. Residential numbers are the norm
    for this data, so a result here is NOT authorisation to call: a DNC check is
    required before dialling or texting, which is a legal requirement rather
    than a policy preference.
    """
    first, last = (first or "").strip(), (last or "").strip()
    if not first or not last:
        return {"found": False, "error": "first and last name are required"}

    params = {"firstname": first, "lastname": last}
    if street:
        params["address"] = street.strip()
    if city:
        params["city"] = city.strip()
    if state:
        params["state"] = state.strip()
    if postalcode:
        params["postalcode"] = postalcode.strip()

    status, body = _get("AppendPhone/SBMMobile", params)

    if status in (401, 403):
        raise ProviderError(
            f"Accurate Append rejected the account ({status}): "
            f"{body.get('Error', 'no detail')}. The key is read but the account "
            f"needs an active subscription.",
            status, spendable=False)
    if status == 429:
        raise ProviderError("Accurate Append quota exhausted.", 429, spendable=True)
    if status != 200:
        raise ProviderError(f"Accurate Append HTTP {status}: {body.get('Error', '')[:200]}",
                            502, spendable=True)

    phones = body.get("Phones") or []
    good = [p for p in phones if _match_ok(p.get("MatchLevel"))]
    weak = [p for p in phones if not _match_ok(p.get("MatchLevel"))]
    if not good:
        return {"found": False, "rejected_weak": len(weak),
                "error": "no mobile at an acceptable match level"}

    best = good[0]
    area = re.sub(r"\D", "", best.get("AreaCode") or "")
    digits = re.sub(r"\D", "", best.get("PhoneNumber") or "")
    if len(area) == 3 and len(digits) == 7:
        e164 = f"+1{area}{digits}"
    elif len(area + digits) == 10:
        e164 = f"+1{area}{digits}"
    else:
        e164 = f"+1{(area + digits)[:10]}" if area + digits else ""

    return {
        "found": True,
        "phone": e164,
        "line_type": best.get("LineType"),
        "match_level": best.get("MatchLevel"),
        "max_validation_level": best.get("MaxValidationLevel"),
        "rejected_weak": len(weak),
        "dnc_checked": False,
        "_source": "accurate_append",
    }


def enrich_lead(first: str, last: str, street: str = "", city: str = "",
                state: str = "", postalcode: str = "") -> dict:
    """Both lookups in one call site. Never raises.

    Email and phone are attempted independently so a phone failure does not cost
    an email that already succeeded, and vice versa. Costs up to two lookups
    against the monthly quota, so callers should cache.
    """
    out = {"first_name": first, "last_name": last,
           "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    for label, fn in (("email", append_email), ("mobile", append_mobile)):
        try:
            out[label] = fn(first, last, street, city, state, postalcode)
        except ProviderError as exc:
            out[label] = {"found": False, "error": exc.message, "status": exc.status}
            out.setdefault("errors", []).append(f"{label}: {exc.message}")
    out["found"] = bool(out.get("email", {}).get("found") or out.get("mobile", {}).get("found"))
    return out
