#!/usr/bin/env python3
"""Live production smoke check for construction.bizstackperks.com.

Read-only. It never creates a lead, subscriber, payment or call. Form endpoints
are probed with invalid/missing payloads so they fail validation before any
write. Run after every deploy:

    ./venv/bin/python scripts/live_smoke.py

Exit code 0 = all checks passed; 1 = at least one failure. The output is a
PASS/FAIL line per check so a deploy log is readable without a report file.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

BASE = (os.getenv("LIVE_BASE_URL") or "https://construction.bizstackperks.com").rstrip("/")
TIMEOUT = 20
UA = "BuildStackLiveSmoke/1.0"

PUBLIC_PAGES = [
    "/", "/services", "/about", "/contact", "/get-started", "/instant-quote",
    "/portfolio", "/quote", "/rental-make-ready", "/legal", "/refund-policy",
    "/portal/login", "/login", "/finance", "/embed/quote", "/app",
    "/payments/success", "/payments/cancel", "/portal/forgot", "/portal/reset",
    "/sign/smoke",
]
API_AUTH = [
    "/api/bot/health", "/api/bids", "/api/inventory", "/api/business/summary",
    "/api/leads/addresses.csv", "/api/payroll/run/1/csv",
    "/api/health/transports", "/api/auth/status",
]
HTML_AUTH_REDIRECT = [
    "/dashboard", "/leads", "/pipeline", "/copilot", "/inbox", "/acquisition",
]
HTML_AUTH_401 = ["/bids", "/projects", "/settings", "/monitor", "/payroll", "/books", "/admin/leads"]
CRAWL_PAGES = [
    "/", "/services", "/about", "/contact", "/get-started", "/instant-quote",
    "/portfolio", "/quote", "/rental-make-ready", "/legal", "/refund-policy",
]
STATIC_ASSETS = [
    "/static/css/tailwind.css",
    "/static/construction_favicon.svg",
    "/static/favicon.svg",
    "/static/og.png",
    "/manifest.webmanifest",
    "/sw.js",
    "/static/listing/projects2/deck-build-before-after.jpg",
]

failures: list[str] = []
passes = 0


def _opener(follow: bool):
    if follow:
        return urllib.request.build_opener()
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    return urllib.request.build_opener(_NoRedirect)


def request(path: str, *, method: str = "GET", data=None, headers=None,
            follow: bool = True):
    url = path if path.startswith("http") else BASE + path
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with _opener(follow).open(req, timeout=TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except Exception as exc:  # noqa: BLE001 - report, do not crash the sweep
        return 0, {}, str(exc).encode("utf-8", "replace")


def check(name: str, path: str, allowed, *, method: str = "GET", data=None,
          headers=None, contains: str = "", content_type: str = "",
          follow: bool = True, location_contains: str = ""):
    global passes
    status, got_headers, body = request(
        path, method=method, data=data, headers=headers, follow=follow)
    text = body.decode("utf-8", "replace")
    detail = f"HTTP {status}"
    ok = status in allowed if isinstance(allowed, (tuple, list, set)) else status == allowed
    if not ok:
        detail += f" (wanted {allowed})"
    if contains and contains not in text:
        ok = False
        detail += f" missing {contains!r}"
    if content_type:
        got_type = (got_headers.get("Content-Type") or got_headers.get("content-type") or "")
        if content_type not in got_type:
            ok = False
            detail += f" content-type {got_type!r} (wanted {content_type})"
    if location_contains:
        location = got_headers.get("Location") or got_headers.get("location") or ""
        if location_contains not in location:
            ok = False
            detail += f" Location {location!r} (wanted {location_contains})"
    if ok:
        passes += 1
        print(f"[PASS] {name} ({detail})")
    else:
        failures.append(f"{name}: {detail}")
        print(f"[FAIL] {name} ({detail})")
    return status, got_headers, text


def form(path: str, fields: dict, headers=None, follow=False):
    return request(
        path,
        method="POST",
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})},
        follow=follow,
    )


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)


def main() -> int:
    global passes
    print(f"Live smoke: {BASE}")

    check("health", "/health", 200, content_type="application/json")
    for page in PUBLIC_PAGES:
        check(f"public {page}", page, 200)

    # Crawl the internal links a visitor can actually click. A 303/401 on an
    # admin link is acceptable; a 404 or 5xx is a broken link.
    links: set[str] = set()
    for page in CRAWL_PAGES:
        _, _, body = request(page)
        parser = _LinkParser()
        try:
            parser.feed(body.decode("utf-8", "replace"))
        except Exception:
            continue
        for href in parser.links:
            href = (href or "").strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "sms:", "javascript:")):
                continue
            parts = urllib.parse.urlsplit(href)
            if parts.scheme or parts.netloc:
                continue
            path = parts.path
            if not path.startswith("/"):
                continue
            if path.startswith(("/api/", "/static/", "/media/", "/uploads/",
                                "/oauth/", "/webhooks/", "/portal/pay/")):
                continue
            links.add(path)
    broken = []
    for link in sorted(links):
        status, _, _ = request(link, follow=False)
        if status not in (200, 303, 401, 403):
            broken.append(f"{link} -> HTTP {status}")
    if broken:
        failures.extend(broken)
        print(f"[FAIL] internal links ({len(broken)} broken of {len(links)})")
        for item in broken:
            print(f"       {item}")
    else:
        passes += 1
        print(f"[PASS] internal links ({len(links)} checked)")

    for path in STATIC_ASSETS:
        check(f"static {path}", path, 200)

    # Auth boundaries: JSON APIs must be 401/403.
    for path in API_AUTH:
        check(f"auth API {path}", path, (401, 403), follow=False)
    # HTML pages use 401 or a login redirect; neither may render protected data.
    for path in HTML_AUTH_401:
        check(f"auth page {path}", path, (401, 403), follow=False)
    for path in HTML_AUTH_REDIRECT:
        status, headers, _ = check(
            f"auth redirect {path}", path, 303, follow=False,
            location_contains="/login")
    status, headers, _ = check(
        "auth redirect /portal", "/portal", 303, follow=False,
        location_contains="/portal/login")

    check("expired OTP", "/login/otp", 303, follow=False,
          location_contains="/login")
    check("portal verify", "/portal/verify", 303, follow=False,
          location_contains="/portal/login")
    check("logout", "/api/auth/logout", 303, follow=False,
          location_contains="/")
    check("invalid booking token", "/book/smoke", 404)
    check("invalid call-list token", "/calllist/smoke", 404)
    check("webchat history", "/api/chat/history", 200,
          content_type="application/json")
    check("property lookup", "/api/property?address=", 200,
          content_type="application/json")
    check("embed snippet", "/embed/snippet", 200,
          content_type="application/json")

    check("docs disabled", "/docs", 404)
    check("openapi disabled", "/openapi.json", 404)
    check("api 404 is JSON", "/api/this-does-not-exist", 404,
          content_type="application/json")
    check("branded 404", "/this-page-does-not-exist", 404,
          contains="Page not found")
    check("robots", "/robots.txt", 200, contains="Sitemap:")
    check("voice.swml", "/voice.swml", 200, method="POST",
          data=b"CallSid=smoke&From=%2B12525550100&To=%2B17578469275",
          headers={"Content-Type": "application/x-www-form-urlencoded"})

    # The SWML body must contain answer + ai/dial/connect.
    status, _, swml = request(
        "/voice.swml", method="POST",
        data=b"CallSid=smoke&From=%2B12525550100&To=%2B17578469275",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        doc = json.loads(swml)
        steps = [list(step)[0] for step in doc["sections"]["main"]]
        ok = "answer" in steps and any(s in steps for s in ("ai", "dial", "connect"))
        if ok:
            passes += 1
            print(f"[PASS] voice.swml structure ({steps})")
        else:
            failures.append(f"voice.swml structure: {steps}")
            print(f"[FAIL] voice.swml structure ({steps})")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"voice.swml JSON: {exc}")
        print(f"[FAIL] voice.swml JSON ({exc})")

    # Sitemap and every URL in it.
    status, _, sitemap = request("/sitemap.xml")
    if status == 200:
        passes += 1
        print("[PASS] sitemap.xml (HTTP 200)")
        try:
            root = ET.fromstring(sitemap)
            locs = [el.text.strip() for el in root.iter()
                    if el.tag.endswith("loc") and el.text]
            for loc in locs:
                check(f"sitemap {loc}", loc, 200)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"sitemap parse: {exc}")
            print(f"[FAIL] sitemap parse ({exc})")
    else:
        failures.append(f"sitemap.xml: HTTP {status}")
        print(f"[FAIL] sitemap.xml (HTTP {status})")

    # Phone/brand consistency on the pages that carry the number.
    for page in ("/", "/contact", "/get-started", "/services"):
        _, _, body = request(page)
        text = body.decode("utf-8", "replace")
        bad = "908-7121" in text or "17579087121" in text
        good = "846-9275" in text or "17578469275" in text
        if not bad and good:
            passes += 1
            print(f"[PASS] brand phone {page}")
        else:
            failures.append(f"brand phone {page}: bad={bad} good={good}")
            print(f"[FAIL] brand phone {page} (bad={bad} good={good})")

    # Form endpoints: invalid payloads only, so no row is written.
    check("lead form validation/rate-limit", "/submit-lead", (400, 429), method="POST",
          data=b"name=Smoke&phone=&email=",
          headers={"Content-Type": "application/x-www-form-urlencoded"})
    check("get-started validation", "/get-started", 303, method="POST",
          data=b"full_name=&phone=&email=", follow=False,
          headers={"Content-Type": "application/x-www-form-urlencoded"},
          location_contains="error=")
    check("instant quote validation", "/instant-quote", 200, method="POST",
          data=b"name=&phone=&email=&address=",
          headers={"Content-Type": "application/x-www-form-urlencoded"})
    check("newsletter validation", "/api/newsletter", 400, method="POST",
          data=json.dumps({"email": "not-an-email"}).encode(),
          headers={"Content-Type": "application/json"})
    check("coupon validation", "/api/validate-coupon", 200, method="POST",
          data=json.dumps({"code": ""}).encode(),
          headers={"Content-Type": "application/json"})
    check("chat validation", "/api/chat", 422, method="POST",
          data=b"", headers={"Content-Type": "application/x-www-form-urlencoded"})
    check("SMS webhook auth", "/comms/sms-webhook", 403, method="POST",
          data=b"From=%2B12525550100&Body=smoke",
          headers={"Content-Type": "application/x-www-form-urlencoded"})
    check("Vapi webhook auth", "/api/voice/vapi-events", 401, method="POST",
          data=b"{}", headers={"Content-Type": "application/json"})
    check("inbound email auth", "/api/email/inbound", 401, method="POST",
          data=b"{}", headers={"Content-Type": "application/json"})
    check("link-lead auth", "/api/bids/1/link-lead", 401, method="POST",
          data=b"lead_id=1",
          headers={"Content-Type": "application/x-www-form-urlencoded"})

    print()
    print(f"Result: {passes} passed, {len(failures)} failed")
    if failures:
        print("Failures:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
