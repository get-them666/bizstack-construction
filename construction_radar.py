import hashlib
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from linkedin_radar import _run_field
from reddit_radar import FUNDING_RE, _funding_context

CON_SUBREDDITS = [
    "HomeImprovement",
    "Homebuilding",
    "homeowners",
    "FirstTimeHomeBuyer",
    "realestateinvesting",
    "VirginiaBeach",
    "norfolk",
    "HamptonRoads",
]

CON_TOPIC_RE = re.compile(
    r"(renovat(e|ion|ing)?|remodel|refurbish)"
    r"|((kitchen|bathroom|basement|garage|attic).{0,30}(remodel|redo|update|upgrade|replace|finish|finishing))"
    r"|(finish(ing)? (the |my |our )?(basement|garage|attic|bonus room))"
    r"|(addition|add.?on|bonus room|ADU|mother.?in.?law suite)"
    r"|(roof(ing)?( repair| replacement| replace)?)"
    r"|(siding|exterior (paint|stucco|vinyl))"
    r"|(window(s)? (replacement|replace|install))"
    r"|((deck (build|building|rebuild|replace|install|repair)|porch|patio|fence(ing)?|railing))"
    r"|(foundation|concrete|driveway|backfill|grading).{0,30}(repair|fix|pour|crack)"
    r"|(plumb|electrical|electrician|wiring|rewire|HVAC)"
    r"|((need|needs|looking for|searching for|wants|want|recommend|refer|find|hiring).{0,30}(general contractor|contractor|builder|roofer|handyman|GC|renovator|remodeler))"
    r"|((get(ting)?|request(ing)?|asking for).{0,30}(quotes|estimates|bids|proposals).{0,20}(project|renovation|remodel|roof|kitchen|bathroom))"
    r"|((contractor|builder|roofer|handyman).{0,30}(recommendation|referral|suggestions))"
    r"|(new (construction|build|home build))"
    r"|(permit(s)?( needed| required| for)?)",
    re.I,
)


def _match(body):
    return CON_TOPIC_RE.search(body or "")


USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
NS = {"a": "http://www.w3.org/2005/Atom"}
MAX_SCAN_SECONDS = 90


def _short_post_id(sub, raw):
    body = f"con-{sub}-{raw}".encode()
    return "c" + hashlib.sha256(body).hexdigest()[:15]


def _fetch_rss(sub):
    """Fetch one subreddit's RSS feed with a short bounded backoff.

    Reddit can answer 429 with a Retry-After of many minutes; we cap the wait
    at 30s so a single scan stays responsive instead of sleeping for hours."""
    url = f"https://www.reddit.com/r/{urllib.parse.quote(sub)}/new/.rss"
    deadline = time.time() + 30
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/rss+xml, application/xml, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 0:
                raw = exc.headers.get("Retry-After") or exc.headers.get("X-Ratelimit-Reset")
                try:
                    wait = max(1, min(int(raw), 30))
                except (TypeError, ValueError):
                    wait = 5
                time.sleep(min(wait, max(0, deadline - time.time())))
                continue
            return None
        except (urllib.error.URLError, TimeoutError, OSError):
            return None
    return None


def _rss_posts_bounded(sub):
    body = _fetch_rss(sub)
    if body is None:
        return {"ok": False, "error": "fetch failed"}
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return {"ok": False, "error": "bad xml"}
    posts = []
    for e in root.findall("a:entry", NS):
        title = (e.findtext("a:title", default="", namespaces=NS) or "").strip()
        author = (e.findtext("a:author/a:name", default="", namespaces=NS) or "").strip()
        link = ""
        link_el = e.find("a:link", NS)
        if link_el is not None:
            link = link_el.attrib.get("href") or ""
        content = (e.findtext("a:content", default="", namespaces=NS) or "")
        posts.append({"title": title, "author": author, "link": link, "content": content})
    return {"ok": True, "posts": posts}


def scan_reddit(limit=100):
    """Fetch recent posts from construction/home-improvement subreddits and return matches."""
    matches = []
    errors = []
    started = time.time()
    for sub in CON_SUBREDDITS:
        if time.time() - started > MAX_SCAN_SECONDS:
            errors.append("scan hit 90s budget; remaining subreddits skipped")
            break
        result = _rss_posts_bounded(sub)
        if not result.get("ok"):
            errors.append(f"r/{sub}: {result.get('error')}")
            continue
        for post in result["posts"]:
            body = f"{post['title'] or ''}\n{post.get('content') or ''}"
            if not _match(body):
                continue
            link = post["link"]
            raw_id = (link.rstrip("/").rsplit("/", 1)[-1] if link else "") or ""
            matches.append(
                {
                    "post_id": _short_post_id(sub, raw_id),
                    "subreddit": sub,
                    "title": post["title"] or "(no title)",
                    "author": post["author"] or "(deleted)",
                    "permalink": link or f"https://www.reddit.com/r/{sub}/",
                    "created_utc": int(time.time()),
                    "funding": bool(FUNDING_RE.search(body)),
                    "funding_use": _funding_context(body),
                }
            )
        time.sleep(0.5)
    return {"matches": matches, "errors": errors}


CON_SEARCH_QUERIES = [
    "contractor needed renovation",
    "renovating my home looking for contractor",
    "kitchen remodel contractor",
    "bathroom remodel contractor",
    "home addition build",
    "roof replacement contractor",
    "finishing my basement",
    "deck build contractor",
    "general contractor recommendation",
]


def scan_linkedin(max_posts=25):
    """Search LinkedIn posts via Apify for construction demand."""
    token = os.getenv("APIFY_TOKEN") or os.getenv("APIFY_TOKEN_ID")
    if not token:
        return {"matches": [], "errors": ["APIFY_TOKEN not set"]}
    try:
        from apify_client import ApifyClient
    except ImportError:
        return {"matches": [], "errors": ["apify_client not installed — pip install apify-client"]}

    client = ApifyClient(token)
    matches = []
    errors = []

    for query in CON_SEARCH_QUERIES:
        run_input = {
            "searchQueries": [query],
            "maxPosts": max_posts,
        }
        try:
            run = client.actor("harvestapi/linkedin-post-search").call(run_input=run_input)
        except Exception as exc:
            errors.append(f"query '{query}': {exc}"[:200])
            continue

        dataset_id = _run_field(run, "defaultDatasetId") if run else None
        if not dataset_id:
            errors.append(f"query '{query}': no dataset returned")
            continue
        for item in client.dataset(dataset_id).iterate_items():
            content = (item.get("content") or item.get("socialContent") or "")[:3000]
            title = content.splitlines()[0][:300] if content else "(no title)"

            if item.get("type") == "pagination_probe" or not _match(content):
                continue

            link = item.get("linkedinUrl") or item.get("shareLinkedinUrl") or ""
            author_obj = item.get("author") or {}
            author = (
                author_obj.get("name")
                or author_obj.get("publicIdentifier")
                or item.get("query")
                or "LinkedIn user"
            )
            post_id = str(item.get("id") or "")
            if not post_id:
                post_id = hashlib.sha256((link or content).encode()).hexdigest()[:16]
            posted_at = (item.get("postedAt") or {})
            ts = posted_at.get("timestamp")
            created_utc = int(ts / 1000) if ts is not None else int(time.time())

            matches.append(
                {
                    "post_id": _short_post_id("linkedin", post_id),
                    "subreddit": "linkedin",
                    "title": title[:300],
                    "author": author,
                    "permalink": link or f"https://www.linkedin.com/feed/?query={urllib.parse.quote(query)}",
                    "created_utc": created_utc,
                    "funding": bool(FUNDING_RE.search(content)),
                    "funding_use": _funding_context(content),
                }
            )
        time.sleep(0.5)

    return {"matches": matches, "errors": errors}


def outreach_draft(match):
    funding_note = (
        " I can also connect you with financing partners who help homeowners fund the work."
        if match.get("funding")
        else ""
    )
    return (
        f"Hey {match.get('author', '')} — saw your post in r/{match.get('subreddit', '')} about "
        f"'{match.get('title', '')[:120]}'. I'm with Buildstack Construction, a licensed & insured general "
        f"contractor in the Williamsburg–Hampton Roads area — we handle renovations, roofs, additions, and "
        f"everything in between, usually same-week walkthroughs with free estimates."
        f"{funding_note} Happy to help — no pressure at all."
    )