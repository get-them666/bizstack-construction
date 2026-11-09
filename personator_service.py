"""Melissa Personator Consumer: name + address -> email and phone.

Where this fits
---------------
``assessor_lookup`` gets an owner name and mailing address from the city for
free but cannot reach anyone. Personator appends contact details to a name and
address, and on a free developer allotment that is materially cheaper than a
paid skip-trace provider for this volume.

    permit -> address -> assessor (name) -> Personator (phone, email)

The compliance gap, stated once
-------------------------------
Unlike BatchData, Personator does **not** return a Do-Not-Call flag per number.
A number it hands back may be on the national registry and this code cannot tell.
The suppression list is therefore the only DNC control on this path, which is why
:func:`store_contacts` never bypasses it -- the number goes in as a candidate and
auto_reply's pre-send check remains the gate.

API shape (verified against developer.melissa.com):
    GET https://personator.melissadata.net/v3/WEB/ContactVerify/doContactVerify
    params: id, act, full, a1, city, state, postal, format=json
at least one of (a1+postal) / (a1+city+state) must be supplied.

``act`` controls what is returned. "Check" validates; "Append" fills missing
data, which is what phone/email require. Whether the append actually returns a
phone or an email depends on the licence on the key, so the parser treats both
as optional rather than assuming either.
"""

from __future__ import annotations

import os

import requests

API_URL = "https://personator.melissadata.net/v3/WEB/ContactVerify/doContactVerify"
TIMEOUT = 45


class PersonatorError(RuntimeError):
    """Service failure. None from :func:`lookup` means 'no match', which is
    different and must not be confused with a broken key."""


def configured() -> bool:
    return bool((os.getenv("MELISSA_API_KEY") or "").strip())


def _act() -> str:
    return (os.getenv("MELISSA_ACT") or "Check,Append").strip()


def lookup(*, name: str = "", street: str = "", city: str = "",
           state: str = "", zip_code: str = "") -> dict | None:
    """Resolve contact details for one person at one address.

    Returns {phones:[{number,type,dnc:None}], emails:[...], record:raw} or None.
    ``dnc`` is always None here because the provider does not supply it -- the
    field exists so downstream code does not have to special-case this source.
    """
    key = (os.getenv("MELISSA_API_KEY") or "").strip()
    if not key:
        raise PersonatorError("MELISSA_API_KEY is not set")
    street = (street or "").strip()
    postal = (zip_code or "").strip()
    city = (city or "").strip()
    state = (state or "").strip()
    if not street or not (postal or (city and state)):
        raise PersonatorError("need street+postal, or street+city+state")

    params = {
        "id": key,
        "act": _act(),
        "full": (name or "").strip(),
        "a1": street,
        "city": city,
        "state": state,
        "postal": postal,
        "format": "json",
    }
    try:
        r = requests.get(API_URL, params=params, timeout=TIMEOUT,
                         headers={"User-Agent": "bizstack-construction/1.0"})
    except requests.RequestException as exc:
        raise PersonatorError(f"unreachable: {exc}") from exc
    if r.status_code in (401, 403):
        raise PersonatorError(f"{r.status_code} - key rejected or not licensed")
    if r.status_code == 429:
        raise PersonatorError("429 rate limited")
    if r.status_code >= 400:
        raise PersonatorError(f"HTTP {r.status_code}: {r.text[:160]}")
    try:
        data = r.json()
    except ValueError as exc:
        raise PersonatorError("non-JSON response") from exc

    records = data.get("Records") or []
    if not records:
        return None
    rec = records[0] if isinstance(records[0], dict) else {}

    phones = []
    raw_phone = str(rec.get("Phone") or "").strip()
    if raw_phone:
        for part in raw_phone.replace(";", "|").split("|"):
            number = part.strip()
            if number:
                phones.append({"number": number, "type": "", "dnc": None})

    emails = []
    raw_email = str(rec.get("EmailAddress") or "").strip()
    if raw_email:
        for part in raw_email.replace(";", "|").split("|"):
            addr = part.strip()
            if addr and "@" in addr:
                emails.append(addr)

    if not phones and not emails:
        return None
    return {
        "phones": phones,
        "emails": sorted(set(emails)),
        "results": str(rec.get("Results") or ""),
        "source": "melissa_personator",
    }


if __name__ == "__main__":
    import json
    import sys
    if not configured():
        print("Set MELISSA_API_KEY first.")
        raise SystemExit(1)
    print(json.dumps(lookup(
        name=sys.argv[1] if len(sys.argv) > 1 else "",
        street=sys.argv[2] if len(sys.argv) > 2 else "",
        city=sys.argv[3] if len(sys.argv) > 3 else "Virginia Beach",
        state=sys.argv[4] if len(sys.argv) > 4 else "VA",
        zip_code=sys.argv[5] if len(sys.argv) > 5 else "",
    ), indent=1))
