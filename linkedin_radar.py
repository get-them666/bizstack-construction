import hashlib
import os
import re
import time
import urllib.parse

from reddit_radar import FUNDING_RE, TOPIC_RE, _funding_context

SEARCH_QUERIES = [
    "airbnb host conversion",
    "short term rental host",
    "converting home to airbnb",
    "vacation rental host startup",
    "vrbo host",
]


def _run_field(run, camel_key):
    """Read a field off an Apify run, which may be a plain dict (client < 3)
    or a Run model object (client >= 3) exposing snake_case attributes."""
    if run is None:
        return None
    if isinstance(run, dict):
        return run.get(camel_key)
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", camel_key).lower()
    return getattr(run, snake, None) or getattr(run, camel_key, None)


def scan_linkedin(max_posts=25):
    """Search LinkedIn posts via Apify and return matching leads."""
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

    for query in SEARCH_QUERIES:
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

            if item.get("type") == "pagination_probe" or not TOPIC_RE.search(content):
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
                    "post_id": post_id,
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
        " I can also connect you with financing partners who help owners fund the conversion."
        if match.get("funding")
        else ""
    )
    return (
        f"Hey {match.get('author', '')} — saw your LinkedIn post about "
        f"'{match.get('title', '')[:120]}'. I run Broom Service, an STR operations company in the "
        f"Hampton Roads/OBX area — we handle turnover cleaning, guest communication, and co-hosting for "
        f"owners converting homes to short-term rentals. Guests fund the cleaning so it's zero out-of-pocket. "
        f"{funding_note}Happy to share what similar conversions have earned and answer any questions. "
        f"No pressure at all."
    )
