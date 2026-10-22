"""Fetch Thumbtack job pages using a browser profile you have already logged into.

WHAT THIS IS
------------
You sign in once, by hand, in a real browser. This reuses that saved session to
open the Jobs list and each job's detail page, and hands the HTML to the
existing parser. It never sees or stores your password.

WHY A BROWSER AND NOT curl
--------------------------
Thumbtack renders these pages client-side, so curl returns the sign-in shell
even with valid cookies. Chrome itself executes the script and produces the
DOM, which is why saving the page from Chrome worked earlier. curl can get you
the session; it cannot get you the rendered page.

THE RISK, STATED ONCE
---------------------
Thumbtack's terms prohibit automated collection, and their bot detection watches
this page specifically. Using it on a schedule risks a lockout on the account
your leads and income depend on. Mitigations below: one request at a time, a
delay between them, a hard cap per run, and no credentials in code.

    python3 thumbtack_fetch.py --setup     one-time: opens a browser, you log in
    python3 thumbtack_fetch.py             fetch the jobs list, print job links
    python3 thumbtack_fetch.py --save      also fetch each job and save the HTML
    python3 thumbtack_fetch.py --limit 5   cap it, default 10
"""

import argparse
import os
import re
import sys
import time

PROFILE = os.path.expanduser("~/Library/Application Support/thumbtack-profile")
OUT = os.path.expanduser("~/bizstack-hosts/thumbtack")
JOBS_URL = "https://www.thumbtack.com/jobs"
JOB_LINK = re.compile(r"https?://(?:www\.)?thumbtack\.com/(?:jobs|request|negotiation)/\d+[^\s\"'<>]*")


def context(pw):
    from playwright.sync_api import sync_playwright
    return pw.chromium.launch_persistent_context(
        PROFILE,
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1440, "height": 900},
    )


def setup():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright not installed: pip install playwright && playwright install chromium")
    os.makedirs(PROFILE, exist_ok=True)
    print("A browser will open. Log in to Thumbtack there, then close it. Nothing else.")
    with sync_playwright() as pw:
        ctx = context(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(JOBS_URL, wait_until="domcontentloaded")
        input("Press Enter here after you are logged in and the Jobs page is visible...")
        ctx.close()
    print("Saved. Now run: python3 thumbtack_fetch.py")


def fetch(pw, limit, save, delay):
    ctx = context(pw)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(JOBS_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        if "log in" in page.title().lower() or page.url.rstrip("/").endswith("login"):
            print("Not logged in. Run --setup first.")
            return 1

        html = page.content()
        links = sorted({m.group(0) for m in JOB_LINK.finditer(html)})
        print("jobs page: %d job link(s) found" % len(links))

        if not save:
            for link in links:
                print("  %s" % link)
            return 0

        os.makedirs(OUT, exist_ok=True)
        for n, link in enumerate(links[:limit], 1):
            page.goto(link, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3500)
            body = page.content()
            m = re.search(r"^([A-Z]{2})\s*-\s*(?:Open|Closed)\s+Lead", page.inner_text("body")[:4000],
                          re.M)
            name = m.group(1) if m else "job%02d" % n
            path = os.path.join(OUT, "%s.html" % name)
            with open(path, "w") as fh:
                fh.write(body)
            print("  %2d/%d %s -> %s" % (n, min(limit, len(links)), name,
                                         os.path.basename(path)))
            time.sleep(delay)
        print("\nNow run: python3 thumbtack_request.py --saved %s --commit" % OUT)
        return 0
    finally:
        ctx.close()


def main():
    ap = argparse.ArgumentParser(description="Fetch Thumbtack job pages via a saved login.")
    ap.add_argument("--setup", action="store_true", help="one-time interactive login")
    ap.add_argument("--save", action="store_true", help="fetch each job and save the HTML")
    ap.add_argument("--limit", type=int, default=10, help="max jobs per run")
    ap.add_argument("--delay", type=float, default=6.0, help="seconds between requests")
    args = ap.parse_args()

    if args.setup:
        setup()
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright not installed: pip install playwright && playwright install chromium")
    with sync_playwright() as pw:
        return fetch(pw, args.limit, args.save, args.delay)


if __name__ == "__main__":
    sys.exit(main())