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
def fetch_autodesk(timeout: int = DEFAULT_TIMEOUT, user_email: str = "") -> list[dict]:
    """ACC submittals for APS_PROJECT_ID, via the owner's three-legged token.

    Was client_credentials against /construction/v1/projects/{id}/submittals,
    which could not have worked twice over: that path 404s (the resource name
    belongs after /construction/, not before the version), and ACC refuses a
    two-legged token because it names the app rather than a project member.

    So the token comes from autodesk_oauth, which the owner signs into, and the
    x-construction-project-id header goes on because ACC requires it. Needs
    APS_CLIENT_ID/APS_CLIENT_SECRET/APS_REDIRECT_URI set and a completed
    sign-in; APS_PROJECT_ID too, and autodesk_oauth.list_projects is how you
    find its value.
    """
    import autodesk_oauth as ao

    if not ao.configured():
        raise SourceError(
            "Autodesk is not configured; missing "
            + ", ".join(ao.missing())
            + " (see /oauth/autodesk/start)")
    if not ao.config()["project_id"]:
        raise SourceError("APS_PROJECT_ID is not set. /oauth/autodesk/status lists "
                          "your ACC projects and their IDs.")
    if not user_email:
        raise SourceError("No Autodesk owner email; the pull needs a signed-in account.")

    data = ao.api_get(
        f"/construction/submittals/v1/projects/{ao.config()['project_id']}/submittals",
        user_email, timeout)
    rows = data.get("results") if isinstance(data, dict) else data
    return [_norm(r) for r in (rows or []) if isinstance(r, dict)]


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


# --- permits -----------------------------------------------------------------
# The one source with no external API, no credentials and no contract: our own
# permit importer already writes job_leads, so the board can be fed from a table
# we own instead of a vendor's invite list.
#
# Reads the database rather than the network, which is why it ignores `timeout`
# -- the signature is fixed by pull(), not by this adapter's needs.

PERMIT_MAX_ROWS = 400
PERMIT_DAYS = 90
PERMIT_USE_CLASSES = ("commercial",)


def fetch_permits(timeout: int = DEFAULT_TIMEOUT) -> list[dict]:
    """Turn recent commercial permits into board rows.

    Not a solicitation feed, and the board should not pretend otherwise: a
    permit says a job is happening and what it is worth, not that anyone has
    invited us to price it. So owner/amount/location are real and
    reference is the permit number, but due_date is left empty rather than
    invented. A fabricated deadline is worse than a blank one -- it sorts to
    the top of the board and implies a bid is due.

    Commercial only. job_leads carries residential permits too, and those name
    homeowners, not GCs with a bid board. Filtering to commercial keeps the
    board aimed at the work it can actually bid on and keeps private individuals
    off it, which is the same boundary the lead pipeline enforces.

    permit_number is required because it is half the dedupe key
    (company, reference, trade). Rows without one would all collapse onto the
    same conflict target.
    """
    import psycopg
    from psycopg.rows import dict_row

    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        raise SourceError("DATABASE_URL is not set; permits are read from our own database.")

    placeholders = ",".join("%s" for _ in PERMIT_USE_CLASSES)
    sql = f"""
        SELECT permit_number, address, city, state, postal_code, work_type,
               job_description, scope_summary, contractor_name, estimated_value
        FROM job_leads
        WHERE NOT is_demo
          AND COALESCE(address, '') <> ''
          AND COALESCE(permit_number, '') <> ''
          AND COALESCE(use_class, 'unknown') IN ({placeholders})
          AND status NOT IN ('won','lost','closed')
          AND found_at >= NOW() - make_interval(days => %s)
        ORDER BY found_at DESC
        LIMIT %s
    """
    params = list(PERMIT_USE_CLASSES) + [PERMIT_DAYS, PERMIT_MAX_ROWS]

    try:
        # dict_row on the connection, not the cursor: psycopg3 returns tuples by
        # default and dict(tuple) raises. Matches enrichment.py.
        with psycopg.connect(dsn, connect_timeout=15, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                src = cur.fetchall()
    except psycopg.Error as e:
        raise SourceError(f"could not read permits: {e}") from e

    rows = []
    for r in src:
        r = dict(r)
        where = ", ".join(
            p for p in (r.get("address"), r.get("city"),
                        " ".join(p for p in ((r.get("state") or "").strip(),
                                             (r.get("postal_code") or "").strip()) if p).strip())
            if p
        )
        desc = (r.get("job_description") or "").strip()
        # A permit often has no description; the address is a better title than
        # an empty card, and _upsert drops a row whose name is blank.
        name = desc[:120] if desc else where
        if not name:
            continue
        rows.append({
            "name": name,
            "owner": (r.get("contractor_name") or "").strip(),
            "reference": (r.get("permit_number") or "").strip(),
            "trade": (r.get("work_type") or "").strip(),
            "location": where,
            "due_date": "",
            "bid_amount": r.get("estimated_value"),
            "notes": (r.get("scope_summary") or "").strip() or desc,
            "role": "sub",
        })
    return rows


SOURCES = {
    "json_feed": {"label": "JSON feed", "fn": fetch_json_feed,
                  "needs": ["BIDS_FEED_URL"]},
    "permits": {"label": "Permits (commercial)", "fn": fetch_permits,
                "needs": ["DATABASE_URL"]},
    "rss": {"label": "RSS feed", "fn": fetch_rss,
            "needs": ["BIDS_RSS_URL"]},
    "autodesk": {"label": "Autodesk Construction Cloud", "fn": fetch_autodesk,
                 "needs": ["APS_CLIENT_ID", "APS_CLIENT_SECRET", "APS_REDIRECT_URI",
                           "APS_PROJECT_ID"]},
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


def pull(source: str, timeout: int = DEFAULT_TIMEOUT,
         user_email: str = "") -> list[dict]:
    s = SOURCES.get(source)
    if not s:
        raise SourceError(f"unknown source '{source}'; try one of {sorted(SOURCES)}")
    if s["fn"] is None:
        raise SourceError(s.get("unavailable") or f"{source} has no adapter")
    missing = [v for v in s["needs"] if not os.getenv(v)]
    if missing:
        raise SourceError(f"{source} is not configured; missing {', '.join(missing)}")
    # Only autodesk needs the signed-in owner's identity -- ACC will not accept
    # a token that is not attached to a project member. Adapters that do not
    # declare user_email are called exactly as before.
    import inspect
    if "user_email" in inspect.signature(s["fn"]).parameters:
        return s["fn"](timeout, user_email=user_email)
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

# Field labels as they turn up in real solicitation email. The separator is
# optional on purpose: "Due date 11/12/2026" and "Amount $152,000" are how
# people actually write, and a regex that demanded a colon matched almost
# nothing. Multi-word labels come first so "Bid Due:" is not read as
# "bid" with the value "Due: ...".
_LABELS = (r"due\s+date|bid\s+due|bid\s+amount|unit\s+price|contract\s+number|"
           r"rfp\s+number|job\s+name|project\s+name|deadline|amount|price|"
           r"reference|solicitation|project|contractor|location|address|"
           r"trade|scope|status|owner|job|due|bid|rfp|gc|ref")

_LABEL = re.compile(rf"^\s*({_LABELS})\s*[:\-]?\s*(.+)$", re.I)

# A value runs until the next label or end of line, so
# "Due date 11/12/2026. Amount $152,000." yields a due date AND an amount
# rather than one value swallowing the other.
_VALUE_END = re.compile(rf"(?=\s+(?:{_LABELS})\s*[:\-]?\s|$)", re.I)

_LABEL_TO_KEY = {
    "due date": "due_date", "due": "due_date", "bid due": "due_date",
    "deadline": "due_date",
    "amount": "amount", "bid amount": "amount", "price": "amount",
    "unit price": "amount",
    "reference": "reference", "ref": "reference", "contract number": "reference",
    "solicitation": "reference", "rfp": "reference", "rfp number": "reference",
    "job": "name", "job name": "name", "project": "name", "project name": "name",
    "trade": "trade", "scope": "trade",
    "location": "location", "address": "location",
    "contractor": "owner", "owner": "owner", "gc": "owner",
    "status": "status", "price_": "amount",
}


# Labels whose value is expected to start with a digit or a currency symbol.
# Used to tell a field from a title: "Due date 11/12/2026" is a field, but
# "Bid Documents Attached" is a subject line and must stay a title.
_STRONG = {"due date", "due", "bid due", "deadline", "amount", "bid amount",
           "price", "unit price", "reference", "ref", "contract number",
           "solicitation", "rfp number", "job name", "project name"}

_HAS_VALUE = re.compile(r"^\s*[\d$€£]")          # starts with a digit or currency


def _label_value(line: str, with_offset: bool = False):
    """Return (key, value) for a "Label: value" line, else (None, None).

    with_offset adds the index just past the value, which is what lets
    _label_fields walk several labels on one line. The regex group spans the
    whole remainder of the line, so slicing by the group end would skip
    everything after the first field.

    Two guards keep subject lines from being mistaken for fields. A title
    like "RFP addendum - cove base" starts with a keyword, so requiring a
    colon or dash is not enough on its own; but "Due date 11/12/2026" has no
    punctuation at all. So: an explicit separator always means a field, and
    without one only a strong field label whose value actually looks like a
    value counts.
    """
    m = _LABEL.match(line)
    if not m:
        return (None, None, 0) if with_offset else (None, None)
    raw = re.sub(r"\s+", " ", m.group(1).strip().lower())
    key = _LABEL_TO_KEY.get(raw)
    if not key:
        return (None, None, 0) if with_offset else (None, None)
    rest = m.group(2).strip()
    has_sep = re.match(r"^\s*[:\-]\s*", m.group(0)[m.start(2) - m.start(0):] or "") \
        or bool(re.search(rf"^\s*{re.escape(raw)}\s*[:\-]", line, re.I))
    if not has_sep and not (raw in _STRONG and _HAS_VALUE.match(rest)):
        return (None, None, 0) if with_offset else (None, None)
    raw = _VALUE_END.split(rest, maxsplit=1)[0]
    val = raw.strip(" .,-")
    if not val:
        return (None, None, 0) if with_offset else (None, None)
    if with_offset:
        return key, val, m.start(2) + len(raw)
    return key, val


def _label_fields(line: str) -> dict:
    """Every label on a line, not just the first.

    Real email puts several fields on one line -- "Due date 11/12/2026.
    Amount $152,000. Trade: Drywall." -- and a regex that matched once threw
    the other two away.
    """
    out: dict = {}
    rest = line
    for _ in range(12):
        key, val, end = _label_value(rest, with_offset=True)
        if not key or not end:
            break
        out.setdefault(key, val)
        rest = rest[end:].lstrip(" .,-;")
        if not rest:
            break
    return out


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

    # Free-text path. Line-driven rather than blank-line-blocked, because an
    # email is a title followed by fields, not a set of blank-line-delimited
    # blocks. A non-label line opens a record; label lines fill it; a blank
    # line closes it.
    records: list[dict] = []
    cur: dict = {}

    def flush():
        nonlocal cur
        if cur and str(cur.get("name", "")).strip():
            records.append(cur)
        cur = {}

    for line in lines:
        if not line.strip():
            flush()
            continue
        fields = _label_fields(line)
        if fields:
            for k, v in fields.items():
                cur.setdefault(k, v)
            continue
        if cur.get("name"):
            # Second unlabelled line in the same record: usually extra prose.
            cur["_extra"] = (cur.get("_extra", "") + " " + line.strip()).strip()
            continue
        flush()
        cur["name"] = line.strip()
    flush()

    rows = []
    for rec in records:
        extra = rec.pop("_extra", "")
        row = _norm(rec)
        # Scrape anything the labels did not capture from the prose.
        if extra:
            if not row.get("bid_amount"):
                m = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", extra)
                if m:
                    row["bid_amount"] = _num(m.group(1))
            if not row.get("due_date"):
                m = re.search(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
                              r"\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})\b",
                              extra)
                if m:
                    row["due_date"] = _date(m.group(1), _sniff_date_format([[line] for line in lines]))
            if not row.get("reference"):
                m = re.search(r"\b([A-Z]{2,}[-\w]*\d[-\w]*)\b", extra)
                if m:
                    row["reference"] = m.group(1)
        if extra and row.get("notes") is not None:
            row["notes"] = ((row.get("notes") or "") + "\n" + extra)[:4000]
        rows.append(row)
    return _finalize(rows, f"{len(records)} record(s) read as text.")


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
