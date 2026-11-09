"""BatchData skip trace: address -> owner, phones, emails, DNC flags.

Where this fits
---------------
``assessor_lookup`` gives an owner NAME and a mailing address from the city for
free, but no way to reach anyone. BatchData sells the other half: phones, emails
and, critically, Do-Not-Call flags per number.

    permit -> address -> assessor (name, free) -> BatchData (reachable contact)

That removes the dependence on RentCast's 50 calls a month, which is what made
the pipeline go quiet.

The DNC flag is the important part
-----------------------------------
The obvious implementation prints "DNC" and moves on, which stops nothing: the
number still gets texted. :func:`suppress_dnc` writes every flagged number into
the shared suppression list, so auto_reply's pre-send check refuses it on every
channel -- DNC is about the telephone, not the transport.

Shape uncertainty
-----------------
BatchData's documented response nests people under ``results[].persons[]`` (with
``phones``/``emails``), while other BatchData products use ``contacts[]``. Rather
than bet on one, :func:`_people` accepts either, and :func:`raw_probe` returns the
unmodified payload so the real shape can be confirmed with one live call instead
of guessed at.

Auth: ``Authorization: Bearer $BATCHDATA_API_KEY`` against
``https://api.batchdata.com/api/v1/property/skip-trace``. A GET to that path
answers 405, which is how it was confirmed to exist.
"""

from __future__ import annotations

import json
import os

import requests

API_URL = "https://api.batchdata.com/api/v1/property/skip-trace"
TIMEOUT = 45


class BatchDataError(RuntimeError):
    """The service failed. Distinct from 'no record', which returns None."""


def configured() -> bool:
    return bool((os.getenv("BATCHDATA_API_KEY") or "").strip())


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {(os.getenv('BATCHDATA_API_KEY') or '').strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "bizstack-construction/1.0",
    }


def raw_probe(address: dict) -> dict:
    """One live call, unmodified response. Used to confirm the schema."""
    if not configured():
        raise BatchDataError("BATCHDATA_API_KEY is not set")
    payload = {"requests": [{"propertyAddress": {
        "street": address.get("street", ""),
        "city": address.get("city", ""),
        "state": address.get("state", ""),
        "zip": address.get("zip", ""),
    }}]}
    try:
        r = requests.post(API_URL, headers=_headers(), json=payload, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise BatchDataError(f"unreachable: {exc}") from exc
    if r.status_code == 401:
        raise BatchDataError("401 unauthorised - API key rejected")
    if r.status_code == 429:
        raise BatchDataError("429 rate limited")
    if r.status_code >= 400:
        raise BatchDataError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except ValueError as exc:
        raise BatchDataError("non-JSON response") from exc


def _people(record: dict) -> list[dict]:
    """People live under either ``persons`` or ``contacts`` depending on product."""
    for key in ("persons", "contacts", "people"):
        rows = record.get(key)
        if isinstance(rows, list) and rows:
            return rows
    return []


def _phone_rows(person: dict) -> list[dict]:
    out = []
    for key in ("phones", "phoneNumbers"):
        for p in (person.get(key) or []):
            if not isinstance(p, dict):
                continue
            number = p.get("phoneNumber") or p.get("number") or p.get("phone") or ""
            if not number:
                continue
            out.append({
                "number": str(number).strip(),
                "type": str(p.get("type") or "").strip(),
                "dnc": bool(p.get("isDnc") or p.get("dnc") or p.get("is_dnc")),
            })
    return out


def _email_rows(person: dict) -> list[str]:
    out = []
    for key in ("emails", "emailAddresses"):
        for e in (person.get(key) or []):
            if isinstance(e, dict):
                addr = e.get("emailAddress") or e.get("email") or ""
            else:
                addr = str(e or "")
            addr = addr.strip()
            if addr and "@" in addr:
                out.append(addr)
    return out


def skip_trace(address: dict) -> dict | None:
    """Address -> {owner_name, phones:[{number,type,dnc}], emails:[...], raw_people}.

    None means no record. BatchDataError means the call failed and is retryable.
    """
    data = raw_probe(address)
    results = data.get("results") or []
    if not results:
        return None
    record = results[0] if isinstance(results[0], dict) else {}

    phones: list[dict] = []
    emails: list[str] = []
    names: list[str] = []
    for person in _people(record):
        if not isinstance(person, dict):
            continue
        first = str(person.get("firstName") or "").strip()
        last = str(person.get("lastName") or "").strip()
        if first or last:
            names.append(f"{first} {last}".strip())
        phones.extend(_phone_rows(person))
        emails.extend(_email_rows(person))

    return {
        "owner_name": names[0] if names else "",
        "all_names": names,
        "phones": phones,
        "emails": sorted(set(emails)),
        "source": "batchdata",
    }


def suppress_dnc(conn, phones: list[dict], *, actor: str = "batchdata") -> list[str]:
    """Write every DNC-flagged number into the shared suppression list.

    Without this the flag is decorative: the pipeline would keep texting a number
    the provider already knows is registered. Returns the numbers suppressed.
    """
    added: list[str] = []
    if not phones:
        return added
    try:
        import suppression
        suppression.ensure_schema(conn)
    except Exception as exc:  # noqa: BLE001
        print(f"[batchdata] suppression unavailable: {exc}", flush=True)
        return added
    for p in phones:
        if not p.get("dnc"):
            continue
        number = p.get("number") or ""
        try:
            r = suppression.add(
                conn, suppression.KIND_DNC, suppression.VALUE_PHONE, number,
                reason="BatchData DNC flag", source="batchdata", actor=actor)
            if r.get("ok"):
                added.append(number)
        except Exception as exc:  # noqa: BLE001
            print(f"[batchdata] could not suppress {number}: {exc}", flush=True)
    return added


if __name__ == "__main__":
    import sys
    if not configured():
        print("Set BATCHDATA_API_KEY first (or export it from the key file).")
        raise SystemExit(1)
    addr = {"street": sys.argv[1] if len(sys.argv) > 1 else "",
            "city": sys.argv[2] if len(sys.argv) > 2 else "Virginia Beach",
            "state": sys.argv[3] if len(sys.argv) > 3 else "VA",
            "zip": sys.argv[4] if len(sys.argv) > 4 else ""}
    print(json.dumps(raw_probe(addr), indent=1)[:4000])
