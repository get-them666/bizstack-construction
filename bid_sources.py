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
import csv
import datetime
import io
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


def _date(v, fmt: str = ""):
    """Parse a date from whatever shape the source used.

    YYYY-MM-DD is unambiguous and tried first. For numeric d/m/y the order is
    only knowable from context, so `fmt` carries the caller's reading of the
    whole file; without it we assume month-first. Every result is validated,
    so an impossible date yields None rather than a wrong due date.
    """
    if not v:
        return None
    s = str(v).strip()
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return _ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        mo, d = (b, a) if fmt == "dmy" else (a, b)
        return _ymd(y, mo, d)
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


def _norm(r: dict, date_format: str = "") -> dict:
    """Map whatever the source called its fields onto ours.

    The name aliases are long because a pasted export can spell this a dozen
    ways -- "Job Name" collapses to job_name, "Project Title" to project_title,
    and a column with no match means a blank name, which drops the row.
    """
    return {
        "name": str(_first(r, "name", "title", "projectName", "project_name",
                           "project_title", "job", "job_name", "jobname",
                           "bid_name", "solicitation_name", "subject",
                           "item", "description_name") or "").strip(),
        "owner": str(_first(r, "owner", "gc", "generalContractor", "issuedBy", "owner_name") or "").strip(),
        "reference": str(_first(r, "reference", "solicitation", "solicitationNumber",
                                "rfp_number", "bidNumber", "id") or "").strip(),
        "trade": str(_first(r, "trade", "scope", "discipline", "scopeOfWork", "type") or "").strip(),
        "location": str(_first(r, "location", "address", "projectLocation", "site") or "").strip(),
        "due_date": _date(_first(r, "due_date", "dueDate", "due", "closeDate",
                                  "closes", "bidDueDate", "deadline", "bid_due"),
                          date_format),
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

# --- paste / CSV ingest ------------------------------------------------------
# Zero-configuration path so the button does something on day one. The owner
# pastes whatever their bid system exports -- a CSV dump, or the text of a
# solicitation email -- and it becomes board rows. No credentials, no external
# API, works from the deployed site.
#
# Deliberately not a "pull": this is the owner handing the data over. It writes
# through the same upsert_pulled path, so it still cannot overwrite a status.

_CSV_HINTS = {"name", "title", "project", "project_name", "project_title",
              "job", "job_name", "jobname", "description", "scope",
              "trade", "discipline", "reference", "solicitation", "rfp",
              "amount", "bid_amount", "price", "value", "due", "due_date",
              "gc", "owner", "contractor", "location", "address", "status"}

_LABEL = re.compile(
    r"^\s*(job|project|trade|scope|due|due date|deadline|amount|bid|price|"
    r"reference|ref|solicitation|rfp|gc|owner|contractor|location|address|"
    r"status)\s*[:\-]\s*(.+)$", re.I)


def _sniff_delim(line: str) -> str:
    """Pick the delimiter, ignoring commas that sit inside a quoted field.

    Naively counting commas misreads the most common thing a bid export
    contains -- a price like "$150,000" -- as a column break.
    """
    try:
        dialect = csv.Sniffer().sniff(line, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        return "," if "," in line else ("\t" if "\t" in line else ";")


def _sniff_date_format(rows: list[list[str]]) -> str:
    """Decide whether the feed is month-first or day-first, once, for the whole file."""
    vals = []
    for r in rows[:40]:
        for cell in r:
            s = (cell or "").strip()
            if re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", s):
                vals.append(tuple(int(x) for x in re.split(r"[/-]", s)[:2]))
    if not vals:
        return "iso"
    # A component above 12 can only be the day. If the FIRST one is over 12
    # the file is day-first (25/11/2026); if the SECOND is, it is month-first
    # (11/25/2026). Getting this backwards turns 25/11 into an invalid month
    # and silently drops the due date.
    first_over = sum(1 for a, b in vals if a > 12)
    second_over = sum(1 for a, b in vals if b > 12)
    if first_over and not second_over:
        return "dmy"
    if second_over and not first_over:
        return "mdy"
    return "iso"


def parse_pasted(text: str, date_format: str = "") -> tuple[list[dict], str]:
    """Parse pasted text into normalized bid rows. Returns (rows, note)."""
    if not text or not text.strip():
        return [], "Nothing pasted."
    # Blank lines are load-bearing here: they are what separates two pasted
    # records. Only comments are dropped, and only from the ends, so an
    # interior blank still splits a block.
    lines = [l for l in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [l for l in lines if not l.strip().startswith("#")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    lines = [l for l in lines if l.strip()]
    if not lines:
        return [], "Nothing to read."

    # CSV / TSV path: a header row that names at least two known columns.
    delim = _sniff_delim(lines[0])
    try:
        _hdr = next(csv.reader(io.StringIO(lines[0]), delimiter=delim))
    except (csv.Error, StopIteration):
        _hdr = lines[0].split(delim)
    header = [h.strip().strip('"').lower().replace(" ", "_") for h in _hdr]
    if len(header) >= 2 and sum(1 for h in header if h in _CSV_HINTS) >= 2:
        body = list(csv.reader(io.StringIO("\n".join(lines[1:])), delimiter=delim))
        body = [[(c or "") for c in r] for r in body]
        body = [r for r in body if any(c.strip() for c in r)]
        fmt = date_format or _sniff_date_format(body)
        rows = []
        for r in body:
            d = {header[i]: (r[i].strip() if i < len(r) else "")
                 for i in range(len(header)) if header[i]}
            rows.append(_norm(d, fmt))
        return _finalize(rows, f"{len(rows)} row(s) read as "
                                f"{'tab' if delim == chr(9) else delim}-separated.")

    # Free-text path: records separated by blank lines, labelled fields inside.
    blocks, cur = [], []
    for l in [x for x in lines if x.strip()] if False else _with_blanks(text):
        if not l.strip():
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(l)
    if cur:
        blocks.append(cur)

    rows = []
    for blk in blocks:
        fields: dict[str, str] = {}
        for line in blk:
            m = _LABEL.match(line)
            if m:
                key = m.group(1).lower().replace(" ", "_")
                fields[key] = m.group(2).strip()
        # A title is the first line that is not a "Label: value" pair. Done
        # before the unlabelled fallback so a block with *some* labels still
        # gets a name -- otherwise "Trade: Painting\nAmount: $54,000" parsed
        # perfectly and was then thrown away for having no name.
        if not fields.get("name"):
            for line in blk:
                if not _LABEL.match(line):
                    fields["name"] = line.strip()
                    break
        if not fields:
            fields["name"] = blk[0].strip()
        if "name" not in fields or not fields["name"]:
            fields["name"] = blk[0].strip()
        if not fields.get("_had_labels") and fields.get("name"):
            rest = " ".join(l for l in blk[1:])
            labels = [l for l in blk if _LABEL.match(l)]
            extra = " ".join(l for l in blk[1:] if l not in labels)
            amt = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", extra)
            if amt and "amount" not in fields:
                fields["amount"] = amt.group(1)
            dm = re.search(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
                           r"\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})\b", extra)
            if dm and not any(k in fields for k in ("due_date", "due")):
                fields["due_date"] = dm.group(1)
            rm = re.search(r"\b([A-Z]{0,4}[-\w]*\d[-\w]*)\b", extra)
            if rm and len(rm.group(1)) > 4 and "reference" not in fields:
                fields["reference"] = rm.group(1)
        rows.append(_norm(fields))
    return _finalize(rows, f"{len(rows)} block(s) read as text.")


def _finalize(rows: list[dict], note: str) -> tuple[list[dict], str]:
    out, skipped = [], 0
    for r in rows:
        name = str(r.get("name", "")).strip()
        # A name of nothing but separators is a formatting artefact, not a job.
        # Without this, a stray ",,,," line becomes a bid called ",,,,".
        if not name or not re.search(r"[A-Za-z0-9]", name):
            skipped += 1
            continue
        # A name carrying several separators is almost certainly a whole CSV
        # line that was pasted without its header, so it never got split. One
        # row that bad poisons the board with a bid called
        # "Hilton Drywall,Drywall,VVH026TB100001,...". Better to skip it and
        # tell the owner to include the header row.
        if sum(name.count(d) for d in (",", "\t", "|")) >= 3:
            skipped += 1
            continue
        out.append(r)
    if skipped:
        note += f" {skipped} skipped for having no name."
    return out, note


def _reformat_date(v, fmt: str):
    """Flip an ambiguous numeric date to ISO once we know the file's order."""
    if not v:
        return v
    m = re.fullmatch(r"\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\s*", str(v))
    if not m:
        return v
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    mo, d = (a, b) if fmt == "mdy" else (b, a)
    return _ymd(y, mo, d)


def _with_blanks(text: str) -> list[str]:
    """Lines with interior blanks intact, comments removed."""
    out = [l for l in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
           if not l.strip().startswith("#")]
    return out
