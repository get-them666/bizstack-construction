"""Owner of record from Norfolk's open property assessments, for free.

Norfolk publishes its assessment roll as a Socrata dataset
(``qva7-tzrf``, refreshed yearly as "Property Assessment and Sales - FY.."), and
unlike the Virginia Beach assessor it needs no session, no CSRF token and no
TLS fix -- one GET returns the owner.

Field choice matters: the dataset carries ``owner`` plus the *situs* address
split into number / name / type / zip. There is no mailing address here, so a
letter to the owner of record goes to the property itself; that is still a
named letter rather than "current resident".

Matching is deliberately loose on the street suffix: permit feeds write
"LINDENWOOD AVENUE" while the roll stores "Lindenwood AV". Number + street-name
prefix + ZIP is unambiguous enough and avoids a suffix translation table that
would need maintaining for every jurisdiction.
"""

from __future__ import annotations

import urllib.parse

import requests

DATASET = "https://data.norfolk.gov/resource/qva7-tzrf.json"
TIMEOUT = 30

# Longest first so "AVENUE" is not eaten by "AVE".
_SUFFIXES = ("AVENUE", "STREET", "DRIVE", "COURT", "CRESCENT", "PLACE", "TERRACE",
             "BOULEVARD", "LANE", "ROAD", "PARKWAY", "CIRCLE", "TRAIL", "WAY",
             "AV", "ST", "DR", "CT", "CR", "PL", "TER", "BLVD", "LN", "RD", "PKWY", "CIR")


class NorfolkError(RuntimeError):
    """The dataset could not be reached. Distinct from 'not found'."""


def _split(address: str) -> tuple[str, str, str]:
    """'833 LINDENWOOD AVENUE, Norfolk, VA 23504' -> ('833', 'LINDENWOOD', '23504')."""
    number = ""
    street = ""
    zipcode = ""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    if parts:
        first = parts[0].split()
        if first and first[0].isdigit():
            number = first[0]
            rest = first[1:]
            if rest and rest[-1].upper() in _SUFFIXES:
                rest = rest[:-1]
            street = " ".join(rest).upper()
        else:
            street = parts[0].upper()
    for p in parts[1:]:
        token = p.strip()
        if token.isdigit() and len(token) == 5:
            zipcode = token
        elif token.upper().startswith("VA"):
            tail = token.split()[-1]
            if tail.isdigit():
                zipcode = tail
    return number, street, zipcode


def owner_for_address(address: str) -> dict | None:
    """Address -> {owner_name, situs_address, pin, property_class, total_value}."""
    number, street, zipcode = _split(address)
    if not number or not street:
        return None
    where = f"property_street_number='{number}' AND upper(property_street_name) LIKE '{street}%'"
    if zipcode:
        # The roll stores ZIP+4 ("23513-1728"); an equality match on the five
        # digit ZIP found nothing and every lead came back empty.
        where += f" AND property_zip LIKE '{zipcode}%'"
    params = {"$where": where, "$limit": 5}
    try:
        r = requests.get(DATASET, params=params, timeout=TIMEOUT,
                         headers={"User-Agent": "bizstack-construction/1.0"})
    except requests.RequestException as exc:
        raise NorfolkError(f"unreachable: {exc}") from exc
    if r.status_code >= 400:
        raise NorfolkError(f"HTTP {r.status_code}: {r.text[:120]}")
    try:
        rows = r.json()
    except ValueError as exc:
        raise NorfolkError("non-JSON response") from exc
    if not rows:
        return None
    row = rows[0] if isinstance(rows[0], dict) else {}
    owner = str(row.get("owner") or "").strip()
    if not owner:
        return None
    situs = " ".join(str(row.get(k) or "").strip() for k in
                     ("property_street_number", "property_street_name", "property_street_type")).strip()
    return {
        "owner_name": owner,
        "situs_address": situs,
        "mailing_address": situs,
        "pin": str(row.get("gpin") or row.get("parcel_id") or "").strip(),
        "property_class": str(row.get("property_class_description") or "").strip(),
        "total_value": row.get("current_total_value") or "",
        "source": "norfolk_assessor",
    }


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(owner_for_address(" ".join(sys.argv[1:]) or
                                       "833 LINDENWOOD AVENUE, Norfolk, VA 23504"), indent=1))
