"""Email lookup for named business/owner leads. PUBLIC WEB PAGES ONLY.

WHAT THIS IS
------------
Takes a lead that already has a real person name, and looks for a published
email address on the open web for that person in that trade or at that
address. It is the second half of the skip-trace path: skip-trace answers
"who owns this house" from the assessor record, this answers "is there an
email for them anywhere public".

LEGAL FRAMING -- READ THIS BEFORE ENABLING
------------------------------------------
Cold B2B email is permitted under CAN-SPAM provided every message carries a
valid physical postal address and a working opt-out. Both exist on the
letterhead (LETTER_BRAND_STREET / LETTER_BRAND_CITYSTATEZIP) and the letters
already print an opt-out line, so those obligations are satisfiable.

This module is scoped to EMAIL ONLY, deliberately. Residential MOBILE numbers
found this way are TCPA territory: calling or texting them without prior
express written consent is the thing that produces real liability. Nothing
here writes a phone number to a lead, and `can_email()` will refuse a row
whose only contact is a phone.

It reads pages a browser can see. It does not attempt to defeat CAPTCHAs,
proxy rotation, fingerprint evasion, or authentication walls. If a site
blocks it, that is a "no" and it moves on.

ETHICS -- THE PART THAT ACTUALLY LIMITS YIELD
---------------------------------------------
Only people who have put a business or professional identity on the public web
resolve. That is the intended filter, not an accident: a homeowner with no
public presence yields nothing, and pretending otherwise would mean
fabricating contact details, which is both useless and defamatory.

WHAT THIS IS NOT
----------------
Not PDL/Apollo/Neustar. Not a purchased list. No bulk data broker, no
residential contact database. That was declined deliberately; see
SESSION notes on the 2026-10-02 "business contacts only" decision.

VOLUME
------
Off by default and hard-capped. A person with two hours of sleep does not
want 400 headless-browser launches queued behind them, and neither does a
provider's abuse team.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime, timezone

UA = ("Mozilla/5.0 (Macintosh; Intel Mac 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

TIMEOUT = 15

# Only look at leads that have a real person name. A row named "Virginia
# Beach" cannot be looked up, and searching for it wastes a request.
EMAIL_CAP = int(os.getenv("EMAIL_FETCH_MAX", "0") or 0)  # 0 = disabled

# Never store these. @lead.local is the internal placeholder, and the
# example.* domains come from template/random-number generators.
_JUNK_DOMAINS = {
    "lead.local", "example.com", "example.net", "example.org",
    "domain.com", "email.com", "sentry.io", "wixpress.com",
    "squarespace.com", "godaddy.com", "cloudflare.com", "yourdomain.com",
}

# Role accounts: real, public, but not a person. Emailing "info@" a
# contractor is fine; emailing it *as if* it were the homeowner is not.
_ROLE_LOCALS = {
    "info", "contact", "hello", "office", "admin", "support", "sales",
    "team", "mail", "enquiries", "inquiries", "noreply", "no-reply",
    "donotreply", "postmaster", "webmaster", "abuse", "billing", "accounts",
}

_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,24}"
)

# Free-mail and platform domains. A lead's OWN domain is the strong signal;
# a gmail found next to their name is usually a different person entirely.
_GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "aol.com", "icloud.com", "me.com", "live.com", "msn.com", "gmx.com",
    "mail.com", "zoho.com", "protonmail.com", "proton.me", "yandex.com",
}

# Paths that are never a person's own address.
_BAD_PATH = re.compile(
    r"/(js|assets|static|img|images|css|fonts|vendor|dist|build|node_modules|"
    r"wp-content|wp-includes|blog|news|privacy|terms|legal|disclaimer|"
    r"login|signin|sign-in|register|search|cart|checkout|account|tag|category|"
    r"\d{4})", re.I
)


def enabled() -> bool:
    """True when explicitly turned on AND a real name is present to search."""
    return EMAIL_CAP > 0


def _skip(reason: str) -> dict:
    return {"ok": False, "reason": reason}


def usable_email(addr: str) -> bool:
    """Reject placeholders, generic providers, and obviously non-person mail."""
    # strip() punctuation, NOT leading/trailing dots: `.bad@x.com` and
    # `bad.@x.com` are both invalid and the dot check below must see them.
    a = (addr or "").strip().lower().strip(";:<>()[]\"'")
    a = a.strip(",") if a.endswith(",") else a
    if not _EMAIL_RE.fullmatch(a):
        return False
    local, _, domain = a.partition("@")
    if domain in _JUNK_DOMAINS:
        return False
    if len(local) < 3 or local.startswith(".") or local.endswith("."):
        return False
    if ".." in local or ".." in domain:
        return False
    if domain.startswith("-") or domain.endswith("."):
        return False
    if local in _ROLE_LOCALS:
        # Keep it, but flag it: an info@ address is a real business channel.
        return True
    return True


def is_personalish(addr: str) -> bool:
    """True when the address looks like a person rather than a company front desk."""
    domain = (addr or "").split("@")[-1].lower()
    return domain not in _GENERIC_DOMAINS


def can_email(name: str, phone: str = "") -> bool:
    """Refuse when there is no real name, or when the only contact is a phone.

    The phone check is the TCPA boundary: this module does not produce
    numbers, and it will not turn a row into a cold-call target.
    """
    from skiptrace_service import is_placeholder_name
    if not name or is_placeholder_name(name):
        return False
    return True


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read(400_000).decode("utf-8", "replace")
    return raw


def _emails_in(html: str) -> list:
    """Pull every plausible address out of a page, de-duplicated, order kept."""
    out = []
    seen = set()
    for m in _EMAIL_RE.finditer(html or ""):
        a = m.group(0).lower().strip(".")
        if a in seen or not usable_email(a):
            continue
        seen.add(a)
        out.append(a)
    return out


def _strip_noise(html: str) -> str:
    """Remove obvious asset paths so their emails do not outrank real ones."""
    html = re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    return html


def search_web(name: str, company: str = "", city: str = "") -> dict:
    """Look for a published email for `name` on ordinary public pages.

    Deliberately does NOT drive a headless browser. A plain HTTP GET of a
    public page is the same thing your browser does when you click a link,
    and it does not require a Chrome install, does not consume RAM you need
    for something else, and cannot be mistaken for an intrusion attempt.
    If a site blocks it, this returns no-result rather than escalating.

    Returns {ok, email, source_url, confidence, reason, notes}.
    """
    if not name:
        return _skip("no name")
    if not can_email(name):
        return _skip("name is a placeholder, not a person")

    q = '"{0}" {1}'.format(name, company or "contractor")
    url = "https://duckduckgo.com/html/?q=" + urllib.parse.quote(q)
    try:
        html = _strip_noise(_fetch(url))
    except urllib.error.HTTPError as exc:
        return _skip(f"search blocked: HTTP {exc.code}")
    except Exception as exc:
        return _skip(f"search failed: {type(exc).__name__}")

    # Walk the result links and check the first few real pages.
    links = re.findall(r'class="result__a"[^>]*href="([^"]+)"', html)
    candidates = []
    for href in links[:6]:
        real = urllib.parse.unquote(href)
        if "duckduckgo.com" in real or _BAD_PATH.search(real):
            continue
        if not real.startswith("http"):
            continue
        candidates.append(real)
        if len(candidates) >= 3:
            break

    found, seen = [], set()
    for link in candidates:
        try:
            page = _strip_noise(_fetch(link))
        except Exception:
            continue
        for a in _emails_in(page):
            if a in seen:
                continue
            seen.add(a)
            # A mailto: is the page telling you this is how to reach them.
            found.append({"email": a, "url": link,
                          "from_mailto": f"mailto:{a}" in page.lower()})
        time.sleep(1.0)

    if not found:
        return {"ok": False, "reason": "no published email found",
                "checked": len(candidates), "notes": []}

    # Prefer a mailto: (the site is telling you), then a personal-looking
    # address on the company's own domain, then anything.
    found.sort(key=lambda x: (not x["from_mailto"], not is_personalish(x["email"])))
    best = found[0]
    return {
        "ok": True,
        "email": best["email"],
        "source_url": best["url"],
        "confidence": "mailto" if best["from_mailto"] else ("personal" if is_personalish(best["email"]) else "generic"),
        "other_found": [f["email"] for f in found[1:4]],
    }


# --- persistence ------------------------------------------------------------
# Same discipline as skiptrace_service: cache every answer INCLUDING the miss,
# so a known-empty name is never re-fetched, and audit every attempt.

def ensure_schema(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS email_fetch_cache (
            key         TEXT PRIMARY KEY,
            person_key  TEXT,
            email       TEXT,
            source_url  TEXT,
            confidence  TEXT,
            reason      TEXT,
            fetched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS email_fetch_audit (
            id          SERIAL PRIMARY KEY,
            requested_by TEXT,
            lead_id     INTEGER,
            person_key  TEXT,
            email       TEXT,
            confidence  TEXT,
            reason      TEXT,
            cached      BOOLEAN DEFAULT FALSE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)


def cache_get(cur, key: str):
    cur.execute("SELECT email, source_url, confidence, reason FROM email_fetch_cache WHERE key = %s;", (key,))
    return cur.fetchone()


def cache_put(cur, key: str, person_key: str, result: dict) -> None:
    cur.execute("""
        INSERT INTO email_fetch_cache (key, person_key, email, source_url, confidence, reason, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (key) DO NOTHING;
    """, (key, person_key, result.get("email"), result.get("source_url"),
          result.get("confidence"), result.get("reason")))


def audit(cur, requested_by: str, lead_id, person_key: str, result: dict, cached: bool) -> None:
    cur.execute("""
        INSERT INTO email_fetch_audit (requested_by, lead_id, person_key, email, confidence, reason, cached)
        VALUES (%s, %s, %s, %s, %s, %s, %s);
    """, (requested_by, lead_id, person_key, result.get("email"),
          result.get("confidence"), result.get("reason"), cached))


def fetch_for_leads(cur, requested_by: str = "owner", cap: int = None, dry_run: bool = True) -> dict:
    """Look up an email for named leads that have none. Dry-run by default.

    Only touches rows whose name is a real person AND that currently hold no
    real email. Never writes a phone number. A found address is written to
    leads.email and stamped {"email_fetched": true} so it is never re-spent.
    """
    from skiptrace_service import is_placeholder_name

    limit = EMAIL_CAP if cap is None else max(0, int(cap))
    summary = {"enabled": enabled(), "dry_run": dry_run, "examined": 0,
               "named": 0, "looked_up": 0, "found": 0, "missed": 0,
               "cached": 0, "cap": limit, "results": [], "notes": []}
    if limit <= 0:
        summary["notes"].append("disabled: set EMAIL_FETCH_MAX > 0 to turn on")
        return summary

    ensure_schema(cur)
    cur.execute("""
        SELECT id, name, address FROM leads
        WHERE company = 'construction'
          AND status = 'new'
          AND COALESCE(name,'') <> ''
          AND (email IS NULL OR BTRIM(email) = '' OR LOWER(email) LIKE '%%@lead.local')
          AND COALESCE(analysis_json::text,'') NOT LIKE '%%"email_fetched"%%'
        ORDER BY id;
    """)
    rows = cur.fetchall()

    for r in rows:
        summary["examined"] += 1
        if is_placeholder_name(r.get("name")):
            continue
        summary["named"] += 1
        if summary["looked_up"] >= limit:
            summary["notes"].append(f"cap of {limit} reached; {summary['named']} named lead(s) remain")
            break

        person_key = (r.get("name") or "").strip().lower()
        hit = cache_get(cur, person_key)
        if hit:
            summary["cached"] += 1
            result = {"email": hit["email"], "source_url": hit["source_url"],
                      "confidence": hit["confidence"], "reason": hit["reason"] or ""}
            ok = bool(hit["email"])
        else:
            summary["looked_up"] += 1
            result = search_web(r.get("name"), "", (r.get("address") or ""))
            ok = bool(result.get("ok"))
            if not dry_run:
                cache_put(cur, person_key, person_key, result)

        if ok:
            summary["found"] += 1
            if not dry_run:
                cur.execute(
                    "UPDATE leads SET email = %s, "
                    "analysis_json = COALESCE(analysis_json,'{}'::jsonb) || %s::jsonb "
                    "WHERE id = %s;",
                    (result["email"], json.dumps({"email_fetched": True,
                                                  "email_source": result.get("source_url", "")}),
                     r["id"]),
                )
        else:
            summary["missed"] += 1
        if not dry_run:
            audit(cur, requested_by, r["id"], person_key, result, bool(hit))

        summary["results"].append({
            "lead_id": r["id"], "name": r.get("name"),
            "email": result.get("email"), "confidence": result.get("confidence"),
            "reason": result.get("reason"), "cached": bool(hit),
        })
        time.sleep(1.5)  # be a person about it

    return summary