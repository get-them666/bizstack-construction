"""Pull solicitations from an external bid source into bid_opportunities.

Separate from the inbound webhook in bid_opportunities. The webhook is push:
someone POSTs to us and the route stores it. This module is pull: the owner
presses the button on /bids and we go fetch from their server.

Both directions are needed. Pull covers the button; push covers sources that
cannot be reached from here or that prefer to notify instead of poll.

Every adapter returns rows already normalised to the bid_opportunities field
shape, so the caller never has to know which source produced what.

Deliberate rule: a pull NEVER changes the status of a row that already exists.
An external listing has no idea we shortlisted it, priced it, or won it. Only
inserts take a status. bid_opportunities.upsert_many(apply_status=False)
enforces this.

Uses urllib, not requests -- requirements.txt carries no HTTP client, and every
other module here follows the same convention.
"""
import datetime
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree

UA = "BuildStack-Bids/1.0 (+construction.bizstackperks.com)"
DEFAULT_TIMEOUT = 25


class SourceError(RuntimeError):
    """Anything that makes a pull fail. Surfaced to the owner, never swallowed."""


def _get(url: str, headers: dict | None = None, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    if not str(url or "").startswith(("http://", "https://")):
        raise SourceError("url must be http or https")
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        raise SourceError(f"HTTP {e.code} from source: {body}") from e
    except urllib.error.URLError as e:
        raise SourceError(f"could not reach source: {e.reason}") from e


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?[\d,]*\.?\d+", str(v))
    return float(m.group(0).replace(",", "")) if m else None


def _date(v):
    """Parse a date from whatever shape the source used.

    YYYY-MM-DD and ISO come first. US MM/DD/YYYY is tried second and
    validated -- 13/23/2026 is not a date, and silently believing it would
    put a garbage due date on the board.
    """
    if not v:
        return None
    s = str(v).strip()
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return _ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", s)
    if m:
        return _ymd(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s*(\d{4})", s)
    if m:
        try:
            mon = ("jan feb mar apr may jun jul aug sep oct nov dec").split().index(
                m.group(1)[:3].lower()) + 1
        except ValueError:
            return None
        return _ymd(int(m.group(3)), mon, int(m.group(2)))
    return None


def _ymd(y: int, mo: int, d: int):
    try:
        return datetime.date(y, mo, d).isoformat()
    except ValueError:
        return None


def _first(d: dict, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return ""


# --- json_feed: any endpoint that returns JSON --------------------------------
# Works against most bid APIs and against a webhook receiver on their side that
# exposes what it collected. Configure with BIDS_FEED_URL.
def fetch_json_feed(timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    url = os.getenv("BIDS_FEED_URL", "")
    if not url:
        raise SourceError("BIDS_FEED_URL is not set")
    headers = {}
    if tok := os.getenv("BIDS_FEED_TOKEN"):
        hdr = os.getenv("BIDS_FEED_AUTH_HEADER", "Authorization")
        prefix = os.getenv("BIDS_FEED_AUTH_PREFIX", "Bearer ")
        headers[hdr] = f"{prefix}{tok}"
    try:
        data = json.loads(_get(url, headers, timeout))
    except json.JSONDecodeError as e:
        raise SourceError(f"response was not JSON: {e}") from e
    rows = data if isinstance(data, list) else _first(data, "bids", "results", "items", "data") or []
    if not isinstance(rows, list):
        raise SourceError("expected a list of bids")
    return [_norm(r) for r in rows if isinstance(r, dict)]


# --- rss: public bid boards that publish a feed ------------------------------
# A lot of solicitation portals do, and it needs no credentials. Configure with
# BIDS_RSS_URL.
def fetch_rss(timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    url = os.getenv("BIDS_RSS_URL", "")
    if not url:
        raise SourceError("BIDS_RSS_URL is not set")
    root = ElementTree.fromstring(_get(url, timeout=timeout))
    items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    out = []
    for it in items:
        text = {c.tag.split("}")[-1]: (c.text or "").strip() for c in it}
        body = text.get("description") or text.get("summary") or ""
        title = text.get("title", "")
        if not title and not body:
            continue
        out.append(_norm({
            "name": title,
            "notes": re.sub(r"<[^>]+>", " ", body)[:4000],
            "reference": text.get("guid") or text.get("id") or "",
            "due_date": text.get("pubDate") or text.get("published") or text.get("updated"),
        }))
    return out


# --- autodesk: Autodesk Construction Cloud via APS ----------------------------
# Configure with APS_CLIENT_ID, APS_CLIENT_SECRET, APS_PROJECT_ID.
def fetch_autodesk(timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    cid, sec, pid = (os.getenv("APS_CLIENT_ID"), os.getenv("APS_CLIENT_SECRET"),
                     os.getenv("APS_PROJECT_ID"))
    if not (cid and sec and pid):
        raise SourceError("APS_CLIENT_ID, APS_CLIENT_SECRET and APS_PROJECT_ID are all required")
    body = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "client_id": cid, "client_secret": sec}
    ).encode()
    req = urllib.request.Request("https://developer.api.autodesk.com/authentication/v2/token",
                                 data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            tok = json.load(resp).get("access_token", "")
    except urllib.error.HTTPError as e:
        raise SourceError(f"APS auth failed: HTTP {e.code}") from e
    if not tok:
        raise SourceError("APS auth returned no access token")
    base = f"https://developer.api.autodesk.com/construction/v1/projects/{pid}/submittals"
    data = json.loads(_get(base, {"Authorization": f"Bearer {tok}"}, timeout))
    rows = _first(data, "results", "items") or []
    return [_norm(r) for r in rows if isinstance(r, dict)]


def _norm(r: dict) -> dict:
    """Map whatever the source called its fields onto ours."""
    return {
        "name": str(_first(r, "name", "title", "projectName", "project_name", "subject") or "").strip(),
        "owner": str(_first(r, "owner", "gc", "generalContractor", "issuedBy", "owner_name") or "").strip(),
        "reference": str(_first(r, "reference", "solicitation", "solicitationNumber",
                                "rfp_number", "bidNumber", "id") or "").strip(),
        "trade": str(_first(r, "trade", "scope", "discipline", "scopeOfWork", "type") or "").strip(),
        "location": str(_first(r, "location", "address", "projectLocation", "site") or "").strip(),
        "due_date": _date(_first(r, "due_date", "dueDate", "due", "closeDate", "closes", "bidDueDate")),
        "bid_amount": _num(_first(r, "bid_amount", "bidAmount", "amount", "value", "price")),
        "notes": str(_first(r, "notes", "description", "summary", "scopeOfWork") or "").strip(),
        "role": "sub",
    }


SOURCES = {
    "json_feed": {"label": "JSON feed", "fn": fetch_json_feed,
                  "needs": ["BIDS_FEED_URL"]},
    "rss": {"label": "RSS feed", "fn": fetch_rss,
            "needs": ["BIDS_RSS_URL"]},
    "autodesk": {"label": "Autodesk Construction Cloud", "fn": fetch_autodesk,
                 "needs": ["APS_CLIENT_ID", "APS_CLIENT_SECRET", "APS_PROJECT_ID"]},
    "buildingconnected": {"label": "BuildingConnected", "fn": None,
                          "needs": [], "unavailable": (
                              "No public API. Access is by contract through the "
                              "Procore partner network, so this cannot be wired "
                              "from here. Keep using the inbound webhook -- point "
                              "it at POST /api/webhooks/bids and it will store.")},
}


def source_status() -> list[dict]:
    out = []
    for key, s in SOURCES.items():
        missing = [v for v in s["needs"] if not os.getenv(v)]
        out.append({
            "key": key, "label": s["label"],
            "configured": s["fn"] is not None and not missing,
            "missing": missing,
            "unavailable": s.get("unavailable"),
        })
    return out


def pull(source: str, timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    s = SOURCES.get(source)
    if not s:
        raise SourceError(f"unknown source '{source}'; try one of {sorted(SOURCES)}")
    if s["fn"] is None:
        raise SourceError(s.get("unavailable") or f"{source} has no adapter")
    missing = [v for v in s["needs"] if not os.getenv(v)]
    if missing:
        raise SourceError(f"{source} is not configured; missing {', '.join(missing)}")
    return s["fn"](timeout)