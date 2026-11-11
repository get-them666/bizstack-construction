"""Pull the paperwork and drawings out of a solicitation email onto its bid card.

A GC's invitation to bid usually arrives as an email with the plans, the scope,
the bid form and an addendum attached. `inbound_email` already parses the body
onto the bid board, but the body is the least useful part -- the drawings are
the job. This module closes that gap: it asks Zoho for the attachments on the
exact message the bid came from, stores them through photo_store, and files each
one under the document type the card already knows how to show.

Deliberately on demand, not at ingest. A background poll downloading plan sets
would be a surprise bandwidth bill and a new way for the poller to be slow; the
owner pressing "Pull attachments" is the consent, and re-pressing it is safe.

Extension handling lives in photo_store.ALLOWED_EXT. Anything outside it (an
.exe, a .html) is skipped and reported rather than stored, so an inbound email
cannot put an executable behind a /media link.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
import urllib.parse
import urllib.request

# Filenames are the strongest signal available: solicitation emails rarely set
# a useful MIME type, and a PDF called "A-101 Floor Plans" is a drawing while a
# PDF called "Scope of Work" is not.
_DRAWING_HINTS = (
    "plan", "drawing", "elevation", "floor", "section", "sheet", "blueprint",
    "architect", "structural", "foundation", "framing", "mep", "site",
    "detail", "a-1", "a1-", "s-1", "s1-", "sheet index", "plot",
)
_SOW_HINTS = ("scope", "sow", "spec", "specification", "statement of work",
              "instructions to bidder", "bid instructions")
_CONTRACT_HINTS = ("contract", "agreement", "terms", "conditions")
_ITEMIZED_HINTS = ("itemized", "line item", "line-item", "takeoff", "take-off",
                   "quantity", "pricing schedule")
_QUOTE_HINTS = ("quote", "quotation", "proposal", "bid form", "bidform",
                "price", "pricing")

_DRAWING_EXT = {".dwg", ".dxf", ".ifc", ".rvt"}

MAX_FILES = 40


def classify(filename: str, content_type: str = "") -> str:
    """Map an attachment to the bid card's document_type vocabulary.

    Order matters: "Bid Form - Scope of Work.pdf" is a bid form, but a plan sheet
    that happens to mention scope is still a drawing. Drawings are checked
    first for exactly that reason.
    """
    name = (filename or "").lower()
    ext = os.path.splitext(name)[1].lower()
    if ext in _DRAWING_EXT or any(h in name for h in _DRAWING_HINTS):
        return "plans"
    if any(h in name for h in _SOW_HINTS):
        return "sow"
    if any(h in name for h in _CONTRACT_HINTS):
        return "contract"
    if any(h in name for h in _ITEMIZED_HINTS):
        return "itemized"
    if any(h in name for h in _QUOTE_HINTS):
        return "quote"
    return "other"


def _folder_for(zoho_mail, account_id: str, folder_id: str, message_id: str) -> str:
    """Recover a folder id for a source email stored without one.

    Older rows predate the folder column. The list endpoint is authoritative and
    already cached at the app level, so borrowing it is cheaper than a new API.
    """
    if folder_id:
        return folder_id
    try:
        for head in zoho_mail.list_recent(days=365, limit=200):
            if str(head.get("id") or "") == str(message_id):
                return str(head.get("folder_id") or "")
    except Exception:
        return ""
    return ""


def _safe_stem(filename: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", os.path.splitext(filename or "")[0])[:40] or "attachment"


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_DOC_EXT_RE = re.compile(
    r"\.(pdf|dwg|dxf|ifc|rvt|zip|docx?|xlsx?|csv|txt|md)(?:[?#]|$)", re.I)
_FILE_PATH_RE = re.compile(
    r"/(files?|documents?|attachments?|download|plans?|drawings?)(/|$|\?)", re.I)

# Hosts that serve a login page, not a file. Trying them from the server gets
# an HTML login screen and wastes a request; the owner has to open the bid
# package in their own signed-in browser, which is why the card links to it.
_LOGIN_WALL_HOSTS = (
    "app.buildingconnected.com", "buildingconnected.com", "app.planhub.com",
    "planhub.com", "procore.com", "isqft.com", "constructconnect.com",
    "smartbid.co", "bidnet.com", "bidnetdirect.com", "app.biddingo.com",
)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def document_links(body: str) -> list:
    """Links in a solicitation email that plausibly point at a document.

    Boards send a mixture: a deep link into their plan room (needs the owner's
    session), a pre-signed storage URL (works from here), or nothing but prose.
    Both kinds are returned with a flag so the card can say which is which
    instead of silently downloading a login page.
    """
    out, seen = [], set()
    for match in _URL_RE.finditer(body or ""):
        url = match.group(0).rstrip(".,;:)]}>'\"")
        low = url.lower()
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        wall = any(host == h or host.endswith("." + h) for h in _LOGIN_WALL_HOSTS)
        if wall:
            # A board link to the opportunity IS the plan room even when the URL
            # carries no filename. Footer/unsubscribe links on the same host do
            # not, so require a job-shaped path.
            if not re.search(r"opportunit|/bids?/|/projects?/|/packages?/", low):
                continue
        elif not (_DOC_EXT_RE.search(low) or _FILE_PATH_RE.search(low)):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "host": host, "login_wall": wall})
    return out


def _public_http(url: str) -> tuple:
    """(ok, reason). A URL from an email must not reach the private network."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        return False, "not an http(s) link"
    host = p.hostname or ""
    if not host:
        return False, "no host"
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError:
        return False, "host does not resolve"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return False, "link points inside the network"
    return True, ""


def _filename_from(headers, url: str) -> str:
    cd = headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, re.I)
    name = urllib.parse.unquote(m.group(1)) if m else ""
    if not name:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
        name = m.group(1) if m else ""
    if not name:
        name = os.path.basename(urllib.parse.urlparse(url).path) or "attachment"
    return os.path.basename(name).strip()[:200]


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Re-check every hop, so a public link cannot bounce to 127.0.0.1."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ok, why = _public_http(newurl)
        if not ok:
            raise ValueError(f"redirect refused: {why}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect)


def _download(url: str, max_bytes: int) -> tuple:
    """(bytes, filename, content_type). Refuses an HTML login page."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
    with _opener.open(req, timeout=25) as resp:
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype in ("text/html", "application/xhtml+xml"):
            raise ValueError("that link is a web page, not a file")
        data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("file is over the upload limit")
        return data, _filename_from(resp.headers, url), ctype


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _detect_source(db, bid_id: int, company: str) -> int:
    """Find the email a pre-existing bid came from, by sender or subject.

    Bids imported before the link existed have no source row, and the owner
    should not have to re-create them to get their drawings. Sender match is the
    strongest signal; a reference in the subject is next; the bid name last.
    Returns how many messages were linked (0 or 1).
    """
    import bid_opportunities
    import zoho_mail
    from email.utils import parseaddr

    bid = bid_opportunities.get_bid(db, bid_id, company)
    if not bid:
        return 0
    try:
        heads = zoho_mail.list_recent(days=365, limit=200)
        _account_id, address = zoho_mail.account()
    except Exception:
        return 0
    me = (address or "").lower()
    contact = (bid.get("contact_email") or "").strip().lower()
    ref = _norm(bid.get("reference"))
    name = _norm(bid.get("name"))

    best = None
    best_score = 0
    for head in heads:
        sender = (parseaddr(head.get("from") or "")[1] or "").lower()
        if not sender or sender == me:
            continue
        subject = _norm(head.get("subject"))
        score = 0
        if contact and sender == contact:
            score += 3
        if ref and len(ref) >= 3 and ref in subject:
            score += 2
        if name and len(name) >= 4 and name in subject:
            score += 1
        if score > best_score:
            best, best_score = head, score
    if not best:
        return 0
    try:
        bid_opportunities.remember_source_email(
            db, bid_id, company,
            account_id=str(best.get("account_id") or ""),
            folder_id=str(best.get("folder_id") or ""),
            message_id=str(best.get("id") or ""),
            sender=str(best.get("from") or ""),
            subject=str(best.get("subject") or ""))
        return 1
    except Exception:
        return 0


def extract(db, bid_id: int, company: str = "construction") -> dict:
    """Download every attachment on the bid's source email(s) onto the card.

    Idempotent: keys are derived from the Zoho attachment id, so a second click
    reuses the same object key and `add_document` recognises it instead of
    stacking a duplicate row.
    """
    import bid_opportunities
    import photo_store

    result = {"ok": False, "checked": 0, "saved": 0, "skipped": 0,
              "errors": [], "notes": [], "reason": ""}

    try:
        bid_opportunities.ensure_schema(db)
        sources = bid_opportunities.source_emails(db, bid_id, company)
    except Exception as exc:
        result["reason"] = f"Could not read the linked solicitation email: {str(exc)[:200]}"
        return result

    try:
        import zoho_mail
        if not zoho_mail.available():
            result["reason"] = ("Zoho Mail is not connected. Reconnect it on Settings, "
                                "then try again.")
            return result
    except Exception as exc:
        result["reason"] = f"Zoho Mail is unavailable: {str(exc)[:200]}"
        return result

    # A bid imported before the link existed has no source row. Search the
    # mailbox for the email by sender/reference/name before giving up, so the
    # owner does not have to re-create a bid to get its drawings.
    if not sources:
        if _detect_source(db, bid_id, company):
            try:
                sources = bid_opportunities.source_emails(db, bid_id, company)
            except Exception:
                sources = []
    if not sources:
        result["reason"] = ("No solicitation email could be matched to this bid, so there "
                            "is nothing to pull. Hand-entered bids have no email source.")
        return result

    try:
        existing = {d.get("key") for d in bid_opportunities.list_documents(db, bid_id, company)}
    except Exception:
        existing = set()

    for source in sources:
        account_id = str(source.get("account_id") or "")
        message_id = str(source.get("message_id") or "")
        folder_id = _folder_for(zoho_mail, account_id, str(source.get("folder_id") or ""),
                                message_id)
        label = (source.get("subject") or message_id)[:120]
        if not (account_id and message_id and folder_id):
            result["notes"].append(f"{label}: the mailbox location for this email is incomplete.")
            continue
        try:
            items = zoho_mail.attachments(account_id, folder_id, message_id)
        except Exception as exc:
            result["errors"].append(f"{label}: {str(exc)[:160]}")
            continue

        for item in items:
            if result["saved"] + result["skipped"] >= MAX_FILES:
                result["notes"].append(f"Stopped at {MAX_FILES} files.")
                break
            if item.get("inline"):
                continue
            result["checked"] += 1
            name = item.get("name") or "attachment"
            ext = os.path.splitext(name)[1].lower()
            if ext not in photo_store.ALLOWED_EXT:
                result["skipped"] += 1
                result["notes"].append(f"{name}: {ext or '(no extension)'} is not a document type this app stores.")
                continue
            size = int(item.get("size") or 0)
            if size and size > photo_store.MAX_BYTES:
                result["skipped"] += 1
                result["notes"].append(
                    f"{name}: {size // (1024 * 1024)} MB is over the "
                    f"{photo_store.MAX_BYTES // (1024 * 1024)} MB upload limit.")
                continue
            stored_name = f"{_safe_stem(name)}-{item.get('id')}{ext}"
            key = f"bids/{bid_id}/{photo_store.safe_key(stored_name)}"
            if key in existing:
                result["skipped"] += 1
                continue
            try:
                raw = zoho_mail.attachment_bytes(
                    account_id, folder_id, message_id, item.get("id") or "")
                if not raw:
                    raise ValueError("empty file")
                stored = photo_store.save(raw, stored_name, key_prefix=f"bids/{bid_id}")
                doc_id = bid_opportunities.add_document(
                    db, bid_id, company,
                    filename=name[:500], key=stored["key"], url=stored["url"],
                    content_type=photo_store._content_type(stored["key"]),
                    bytes_count=len(raw), uploaded_by="solicitation email",
                    document_type=classify(name))
            except ValueError as exc:
                result["errors"].append(f"{name}: {str(exc)[:160]}")
                continue
            except Exception as exc:
                result["errors"].append(f"{name}: {str(exc)[:160]}")
                continue
            if doc_id:
                existing.add(stored["key"])
                result["saved"] += 1
            else:
                result["skipped"] += 1

    # Links in the email body. A board's deep link needs the owner's session and
    # is called out; a pre-signed storage link can be fetched here. This is the
    # source-agnostic half of "pull the drawings from whatever board sent it".
    try:
        body = bid_opportunities.source_email_body(db, bid_id, company)
    except Exception:
        body = ""
    all_links = document_links(body)
    walled = [l for l in all_links if l["login_wall"]]
    if walled:
        hosts = sorted({l["host"] for l in walled if l["host"]})
        result["notes"].append(
            "The plan room on " + ", ".join(hosts) +
            " needs your sign-in — use the bid package link at the top of this card for those drawings.")

    for link in [l for l in all_links if not l["login_wall"]]:
        if result["saved"] + result["skipped"] >= MAX_FILES:
            result["notes"].append(f"Stopped at {MAX_FILES} files.")
            break
        url = link["url"]
        digest = hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()[:12]
        if any(str(k or "").startswith(f"bids/{bid_id}/link-{digest}") for k in existing):
            result["skipped"] += 1
            continue
        ok, why = _public_http(url)
        if not ok:
            result["skipped"] += 1
            result["notes"].append(f"{link['host'] or url}: {why}")
            continue
        result["checked"] += 1
        fname = ""
        try:
            raw, fname, _ctype = _download(url, photo_store.MAX_BYTES)
            if not raw:
                raise ValueError("empty file")
            ext = os.path.splitext(fname)[1].lower()
            if ext not in photo_store.ALLOWED_EXT:
                raise ValueError(f"{ext or '(no extension)'} is not a document type this app stores")
            stored_name = f"{_safe_stem(fname)}-{digest}{ext}"
            stored = photo_store.save(raw, stored_name, key_prefix=f"bids/{bid_id}")
            doc_id = bid_opportunities.add_document(
                db, bid_id, company,
                filename=fname[:500], key=stored["key"], url=stored["url"],
                content_type=photo_store._content_type(stored["key"]),
                bytes_count=len(raw), uploaded_by="solicitation link",
                document_type=classify(fname))
        except Exception as exc:
            result["errors"].append(f"{fname or link['host'] or url}: {str(exc)[:160]}")
            continue
        if doc_id:
            existing.add(stored["key"])
            result["saved"] += 1
        else:
            result["skipped"] += 1

    result["ok"] = True
    if not result["checked"]:
        if walled:
            result["reason"] = ("The drawings live behind the bid board's sign-in. Open the bid "
                                "package at the top of this card; nothing was downloaded here.")
        else:
            result["reason"] = ("That solicitation has no email attachments and no downloadable "
                                "document links.")
    return result


# --- BuildingConnected Bid Board --------------------------------------------
# The Bid Board API (APS /construction/buildingconnected/v2) is the same
# three-legged API the nightly sync already reads opportunities from, so the
# owner's existing sign-in is all this needs. The API does not publish a
# single documented "files" route; the candidates below are tried in order and
# the first one that returns rows wins, so a future API addition needs no code
# change here and a wrong guess costs one cheap GET, not a broken button.
_BC_FILE_PATHS = ("files", "documents", "attachments", "plans")
_BC_NAME_KEYS = ("name", "fileName", "filename", "title", "displayName")
_BC_URL_KEYS = ("downloadUrl", "download_url", "url", "href", "storageUrl", "signedUrl")


def _bc_opportunity_id(bid: dict) -> str:
    """The Bid Board opportunity id from the card's deep link or reference."""
    text = f"{bid.get('external_url') or ''} {bid.get('reference') or ''}"
    m = re.search(r"/opportunities/([A-Za-z0-9_-]{8,64})", text)
    if m:
        return m.group(1)
    m = re.search(r"\bbc:([A-Za-z0-9_-]{8,64})", text)
    return m.group(1) if m else ""


def _walk_file_nodes(node, out: list, depth: int = 0) -> None:
    """Collect {name, url} pairs from any response shape."""
    if depth > 6 or len(out) >= MAX_FILES:
        return
    if isinstance(node, dict):
        url = next((node.get(k) for k in _BC_URL_KEYS
                    if isinstance(node.get(k), str) and node.get(k).startswith("http")), "")
        name = next((node.get(k) for k in _BC_NAME_KEYS
                     if isinstance(node.get(k), str) and node.get(k).strip()), "")
        if url and name:
            out.append({"name": name.strip(), "url": url})
            return
        for v in node.values():
            _walk_file_nodes(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _walk_file_nodes(v, out, depth + 1)


def extract_buildingconnected(db, bid_id: int, company: str = "construction",
                              user_email: str = "") -> dict:
    """Download the plans and bid documents attached to a Bid Board invitation.

    Returns the same summary shape as extract(), so the route can report one
    result whether the paperwork came by email or from the Bid Board.
    """
    import bid_opportunities
    import bid_sources
    import photo_store
    import autodesk_oauth as ao

    result = {"ok": False, "checked": 0, "saved": 0, "skipped": 0,
              "errors": [], "notes": [], "reason": ""}
    bid = bid_opportunities.get_bid(db, bid_id, company)
    if not bid:
        result["reason"] = "Bid not found."
        return result
    oid = _bc_opportunity_id(dict(bid))
    if not oid:
        result["reason"] = ("This bid has no BuildingConnected opportunity id, so there is "
                            "nothing to pull from the Bid Board.")
        return result
    if not ao.configured():
        result["reason"] = "Autodesk is not configured (" + ", ".join(ao.missing()) + ")."
        return result
    if not user_email:
        result["reason"] = "No signed-in Autodesk account; sign in at /oauth/autodesk/start."
        return result

    nodes: list = []
    tried = []
    for leaf in _BC_FILE_PATHS:
        path = f"{bid_sources.BC_API}/opportunities/{oid}/{leaf}"
        tried.append(leaf)
        try:
            data = ao.api_get(path, user_email, 25)
        except Exception:
            continue
        _walk_file_nodes(data, nodes)
        if nodes:
            break
    if not nodes:
        try:
            detail = ao.api_get(f"{bid_sources.BC_API}/opportunities/{oid}",
                                user_email, 25)
            _walk_file_nodes(detail, nodes)
        except Exception as exc:
            result["notes"].append(f"opportunity detail: {str(exc)[:140]}")
    if not nodes:
        result["reason"] = ("The Bid Board returned no downloadable files for this "
                            f"opportunity (tried: {', '.join(tried)}). Plans may be "
                            "visible only inside Bid Board.")
        return result

    try:
        existing = {d.get("key") for d in bid_opportunities.list_documents(db, bid_id, company)}
    except Exception:
        existing = set()

    for node in nodes[:MAX_FILES]:
        name = node["name"][:500]
        ext = os.path.splitext(name)[1].lower()
        if ext not in photo_store.ALLOWED_EXT:
            result["skipped"] += 1
            result["notes"].append(f"{name}: {ext or '(no extension)'} is not a type this app stores.")
            continue
        result["checked"] += 1
        try:
            raw, ctype = ao.api_get_bytes(node["url"], user_email)
            if not raw:
                raise ValueError("empty file")
            stored_name = f"{_safe_stem(name)}-{hashlib.sha1(node['url'].encode()).hexdigest()[:10]}{ext}"
            key = f"bids/{bid_id}/{photo_store.safe_key(stored_name)}"
            if key in existing:
                result["skipped"] += 1
                continue
            stored = photo_store.save(raw, stored_name, key_prefix=f"bids/{bid_id}")
            doc_id = bid_opportunities.add_document(
                db, bid_id, company,
                filename=name, key=stored["key"], url=stored["url"],
                content_type=ctype or photo_store._content_type(stored["key"]),
                bytes_count=len(raw), uploaded_by="BuildingConnected",
                document_type=classify(name, ctype or ""))
        except ValueError as exc:
            result["errors"].append(f"{name}: {str(exc)[:160]}")
            continue
        except Exception as exc:
            result["errors"].append(f"{name}: {str(exc)[:160]}")
            continue
        if doc_id:
            existing.add(stored["key"])
            result["saved"] += 1
        else:
            result["skipped"] += 1
    result["ok"] = bool(result["saved"] or result["skipped"])
    return result
