"""Owner of record from the City of Virginia Beach assessor, for free.

Why this exists
---------------
Owner names came from RentCast, metered at 50 calls a MONTH shared with the quote
lookup, skip trace and the AVM sweep. One busy week exhausts it, and every lookup
then returns "no record" -- which reads exactly like "this owner has no contact
details" and is how the permit pipeline went quiet without anyone noticing.

Virginia Beach publishes the same information for nothing. The property search is
a Laravel app (spatialest) with two JSON endpoints:

    POST /api/v2/search            {"filters":{"term":...},"page":"1","limit":21}
    GET  /api/v1/recordcard/{PIN}

Quirks worth knowing, all found the hard way:

* **The search answers with a parcel id, not a list**: ``{"id":"14775570690000"}``.
  Only a multi-match search returns a ``results`` array. Code that expects
  ``results`` alone silently reports "not found" for every address.
* **The TLS chain is incomplete.** The server sends its leaf certificate without
  the DigiCert intermediate, so verification fails with "unable to get local
  issuer" for any CA bundle. curl papers over it via the system store; Python
  does not. The intermediate is vendored beside this module and appended to
  certifi at runtime.
* **A plain requests User-Agent gets 403.** Browser headers are required.
* Both calls need a session cookie plus the ``x-csrf-token`` header, bootstrapped
  from the search page.

Rate limit is 1000 requests/hour, which is far more than the pipeline needs.

Only Virginia Beach is covered. Norfolk, Chesapeake and the NC counties run
different systems -- adding them is a new client, not a new constant.
"""

from __future__ import annotations

import os
import urllib.parse

import certifi
import requests

BASE = "https://propertysearch.virginiabeach.gov"

_HERE = os.path.dirname(os.path.abspath(__file__))
_INTERMEDIATE = os.path.join(_HERE, "digicert_g2_intermediate.pem")

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")


class AssessorError(RuntimeError):
    """The service failed. Distinct from "not found", which returns None.

    The distinction is the whole point: a spent quota or a broken endpoint used
    to look identical to an owner with no contact details.
    """


def _ca_bundle() -> str:
    """certifi plus the missing intermediate, cached beside this module."""
    cache = os.path.join(_HERE, ".ca_bundle_vb.pem")
    try:
        if os.path.exists(cache) and os.path.exists(_INTERMEDIATE) \
                and os.path.getmtime(cache) >= os.path.getmtime(_INTERMEDIATE):
            return cache
        with open(cache, "wb") as out:
            with open(certifi.where(), "rb") as ca:
                out.write(ca.read())
            out.write(b"\n")
            with open(_INTERMEDIATE, "rb") as mid:
                out.write(mid.read())
        return cache
    except OSError:
        return certifi.where()


def _session() -> requests.Session:
    s = requests.Session()
    s.verify = _ca_bundle()
    s.headers.update({
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": BASE + "/",
        "Origin": BASE,
    })
    try:
        page = s.get(BASE + "/", timeout=30, verify=s.verify)
    except requests.RequestException as exc:
        raise AssessorError(f"assessor unreachable: {exc}") from exc
    if page.status_code >= 400:
        raise AssessorError(f"assessor search page returned {page.status_code}")
    token = s.cookies.get("XSRF-TOKEN") or ""
    if not token:
        raise AssessorError("no XSRF-TOKEN cookie on the assessor search page")
    s.headers["x-csrf-token"] = urllib.parse.unquote(token)
    s.headers["Accept"] = "application/json, text/plain, */*"
    s.headers["X-Requested-With"] = "XMLHttpRequest"
    return s


def _search_raw(session: requests.Session, term: str, page: int = 1, limit: int = 21) -> dict:
    body = {
        "filters": {"term": term, "page": str(page)},
        "page": str(page),
        "limit": limit,
        "debug": {"currentURL": "", "previousURL": ""},
    }
    try:
        r = session.post(f"{BASE}/api/v2/search", json=body, timeout=30, verify=session.verify)
    except requests.RequestException as exc:
        raise AssessorError(f"search failed: {exc}") from exc
    if r.status_code == 419:
        raise AssessorError("CSRF rejected (419)")
    if r.status_code >= 400:
        raise AssessorError(f"search returned {r.status_code}")
    try:
        return r.json() or {}
    except ValueError as exc:
        raise AssessorError("search returned non-JSON") from exc


def record_card(session: requests.Session, pin: str) -> dict:
    if not pin:
        return {}
    try:
        r = session.get(f"{BASE}/api/v1/recordcard/{urllib.parse.quote(str(pin))}",
                        timeout=30, verify=session.verify)
    except requests.RequestException as exc:
        raise AssessorError(f"record card failed: {exc}") from exc
    if r.status_code >= 400:
        return {}
    try:
        return r.json() or {}
    except ValueError:
        return {}


def _mailing_address(card: dict) -> str:
    """The mailing address lives deep inside the record card's display sections."""
    found = {"v": ""}

    def walk(o):
        if found["v"]:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if not found["v"] and str(k).lower() in (
                        "fullmailingaddress", "mailingaddress", "mailaddr", "mail_address"):
                    found["v"] = str(v or "").strip().rstrip(",")
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk((card.get("parcel") or {}).get("sections") or [])
    return found["v"]


def owner_for_address(address: str, session: requests.Session | None = None) -> dict | None:
    """Address -> {pin, owner_name, owner_name2, mailing_address, situs_address,
    total_value, property_class}.

    None means the assessor has no match for that text. AssessorError means the
    lookup itself failed and should be retried later.
    """
    term = (address or "").strip()
    if len(term) < 4:
        return None
    s = session or _session()
    data = _search_raw(s, term)

    pin = str(data.get("id") or "").strip()
    if not pin:
        results = data.get("results") or []
        if not results:
            return None
        first = results[0] if isinstance(results[0], dict) else {}
        for key in ("pin", "PIN", "gpin", "id", "parcel"):
            if first.get(key):
                pin = str(first[key]).strip()
                break
        if not pin:
            return None

    card = record_card(s, pin)
    header = (card.get("parcel") or {}).get("header") or {}
    return {
        "pin": pin,
        "owner_name": str(header.get("OwnerName1") or "").strip(),
        "owner_name2": str(header.get("OwnerName2") or "").strip(),
        "mailing_address": _mailing_address(card),
        "situs_address": str(header.get("FullAddress") or header.get("SitusStreet") or "").strip(),
        "total_value": header.get("TotalValue") or "",
        "property_class": header.get("ClassCdDesc") or "",
        "source": "vb_assessor",
    }


# --- Point -> parcel --------------------------------------------------------
# Permit records carry coordinates, and a point is more reliable than matching an
# address string: no abbreviations, no suite numbers, no "Dr" vs "Drive".
GIS_PARCELS = ("https://geo.vbgov.com/mapservices/rest/services/Basemaps/"
               "Property_Information/MapServer/12/query")


def pin_for_point(lat, lon, timeout: int = 30) -> str:
    """Return the GPIN of the parcel containing a lat/lon, or "" if none."""
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return ""
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return ""
    params = {
        "geometry": f"{lon_f},{lat_f}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "PAR_GPIN",
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        r = requests.get(GIS_PARCELS, params=params, timeout=timeout,
                         verify=_ca_bundle(), headers={"User-Agent": _UA})
    except requests.RequestException as exc:
        raise AssessorError(f"parcel lookup failed: {exc}") from exc
    if r.status_code >= 400:
        raise AssessorError(f"parcel lookup returned {r.status_code}")
    try:
        feats = (r.json() or {}).get("features") or []
    except ValueError as exc:
        raise AssessorError("parcel lookup returned non-JSON") from exc
    if not feats:
        return ""
    attrs = feats[0].get("attributes") or {}
    return str(attrs.get("PAR_GPIN") or "").strip()


def owner_for_point(lat, lon, session: requests.Session | None = None) -> dict | None:
    """Coordinates -> the same record owner_for_address returns.

    Use this when the permit carries geometry: it avoids every address-string
    mismatch. Returns None when the point is outside Virginia Beach.
    """
    pin = pin_for_point(lat, lon)
    if not pin:
        return None
    s = session or _session()
    card = record_card(s, pin)
    header = (card.get("parcel") or {}).get("header") or {}
    if not header:
        return None
    return {
        "pin": pin,
        "owner_name": str(header.get("OwnerName1") or "").strip(),
        "owner_name2": str(header.get("OwnerName2") or "").strip(),
        "mailing_address": _mailing_address(card),
        "situs_address": str(header.get("FullAddress") or header.get("SitusStreet") or "").strip(),
        "total_value": header.get("TotalValue") or "",
        "property_class": header.get("ClassCdDesc") or "",
        "source": "vb_assessor_point",
    }


if __name__ == "__main__":
    import sys
    target = " ".join(sys.argv[1:]) or "252 Town Center Drive"
    print(owner_for_address(target))
