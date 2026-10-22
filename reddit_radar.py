import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SUBREDDITS = ["airbnb_hosts", "realestateinvesting", "VirginiaBeach", "norfolk", "OuterBanks"]

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

NS = {"a": "http://www.w3.org/2005/Atom"}

FUNDING_RE = re.compile(
    r"\b(funding|financ(e|ing|ial)?|loan|heloc|capital|investor|equity|mortgage|credit|borrow|collateral|money to (start|buy|convert)|how much.*cost to start)\b",
    re.I,
)

TOPIC_RE = re.compile(
    r"(turn(ing)? (my |our |the |this )?(home|house|property|condo|town.?home)\s+(in)?to( an)? (airbnb|short.?term|vrbo|vacation rental|str))"
    r"|(convert(ing)? (my |our |the |this )?(home|house|property|condo|town.?home)( to| into)? (an |a )?(airbnb|short.?term|vrbo|vacation rental|str))"
    r"|((start|launch|open).{0,30}(airbnb|short.?term rental|vacation rental).{0,40}(home|house|property))"
    r"|(first (airbnb|str|short.?term) (host|rental|property))"
    r"|(want(ing)? to (rent|list) (my |our |the )?(home|house|property).{0,20}(airbnb|short.?term|vacation))"
    r"|((airbnb|vacation rental|short.?term).{0,40}(turn (my|our|the) (home|house|property)))",
    re.I,
)


_next_request_at = 0.0


def _respect_reset():
    """Wait until the rate-limit reset window has passed, if we're inside one."""
    global _next_request_at
    remaining = _next_request_at - time.time()
    if remaining > 0:
        time.sleep(remaining)


def _record_reset(exc):
    """Remember Reddit's reset point so later requests wait it out."""
    global _next_request_at
    wait = _rate_limit_wait(exc)
    _next_request_at = max(_next_request_at, time.time() + (wait if wait is not None else 30))


def _rate_limit_wait(exc):
    """Read Reddit's rate-limit headers; returns seconds to wait or None."""
    raw = exc.headers.get("Retry-After") or exc.headers.get("X-Ratelimit-Reset")
    if raw is None:
        return None
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return None


def _fetch(url, retries=3):
    """Fetch a URL, honoring Reddit's rate-limit reset window on 429s.

    Returns the body on success, None on failure. A 429 with a reset window
    sleeps for that window so the retry actually has a chance to succeed.
    """
    for attempt in range(retries + 1):
        _respect_reset()
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/xml, application/json, */*",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < retries:
                _record_reset(exc)
                _respect_reset()
                continue
            if exc.code == 429:
                _record_reset(exc)
            return None
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
    return None


def _rss_posts(sub):
    body = _fetch(f"https://www.reddit.com/r/{urllib.parse.quote(sub)}/new/.rss")
    if body is None:
        return {"ok": False, "error": "fetch failed"}
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        return {"ok": False, "error": f"bad xml: {exc}"}
    posts = []
    for e in root.findall("a:entry", NS):
        title = (e.findtext("a:title", default="", namespaces=NS) or "").strip()
        author = (e.findtext("a:author/a:name", default="", namespaces=NS) or "").strip()
        link = ""
        link_el = e.find("a:link", NS)
        if link_el is not None:
            link = link_el.attrib.get("href") or ""
        content = (e.findtext("a:content", default="", namespaces=NS) or "")
        published = (e.findtext("a:published", default="", namespaces=NS) or "")
        posts.append({"title": title, "author": author, "link": link, "content": content, "published": published})
    return {"ok": True, "posts": posts}


def _posts_for(sub, limit):
    # JSON endpoint is 403-blocked from host/datacenter IPs; falling back to it
    # on an RSS failure only doubles the request load against the rate limit,
    # so scan the RSS feed only.
    return _rss_posts(sub)


def scan_reddit(limit=100):
    """Fetch recent posts from monitored subreddits and return matching leads."""
    matches = []
    errors = []
    for sub in SUBREDDITS:
        result = _posts_for(sub, limit)
        if not result.get("ok"):
            errors.append(f"r/{sub}: {result.get('error')}")
            continue
        for post in result["posts"]:
            title = post["title"] or ""
            content = post["content"] or ""
            body = f"{title}\n{content}"
            if not TOPIC_RE.search(body):
                continue
            link = post["link"]
            post_id = (link.rstrip("/").rsplit("/", 1)[-1] if link else "") or ""
            matches.append(
                {
                    "post_id": post_id,
                    "subreddit": sub,
                    "title": title or "(no title)",
                    "author": post["author"] or "(deleted)",
                    "permalink": link or f"https://www.reddit.com/r/{sub}/",
                    "created_utc": int(time.time()),
                    "funding": bool(FUNDING_RE.search(body)),
                    "funding_use": _funding_context(body),
                }
            )
        time.sleep(0.5)
    return {"matches": matches, "errors": errors}


def _funding_context(body, limit=180):
    m = FUNDING_RE.search(body)
    if not m:
        return ""
    start = max(0, m.start() - 60)
    return body[start : start + limit].strip()


def outreach_draft(match):
    funding_note = (
        " I can also connect you with funding partners who help owners finance the conversion."
        if match.get("funding")
        else ""
    )
    return (
        f"Hey u/{match.get('author', '')} — saw your post in r/{match.get('subreddit', '')} about "
        f"'{match.get('title', '')[:120]}'. I run an STR operations company in the Hampton Roads/OBX area "
        f"(Broom Service) that handles turnover cleaning, guest communication, and co-hosting for owners "
        f"converting homes to short-term rentals — guests fund the cleaning so there's zero out-of-pocket cost."
        f"{funding_note} Happy to share what similar conversions have earned and answer any questions. "
        f"No pressure at all."
    )