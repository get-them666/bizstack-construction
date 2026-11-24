"""The public footer must link the Facebook page, safely, on the construction brand.

Why this exists
---------------
The page was connected to the Graph API and posting to it from the admin screen,
but nothing on the public site linked to it: `facebook.com/BuildStackConstructionLLC`
appeared only in `templates/construction/marketing.html`, which is admin-only.
Every visitor who would have followed the page had no way to find it.

Two things this locks down:

1. The link is present, and points at the Page rather than a share dialog or a
   generic facebook.com URL.
2. It opens in a new tab with `rel="noopener"`. A `target="_blank"` without
   `noopener` lets the opened page reach back through `window.opener`.

The brand check is here because the footer is shared by every construction page:
the Broom Service number must never appear in it (AGENTS.md section 1).
"""

from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "templates", "construction", "base.html")

PAGE_URL = "https://www.facebook.com/BuildStackConstructionLLC/"


def footer_html() -> str:
    with open(BASE, encoding="utf-8") as fh:
        html = fh.read()
    # The last <footer> in base.html is the site-wide one.
    idx = html.rfind("<footer")
    assert idx != -1, "base.html has no footer"
    return html[idx:]


def test_footer_links_the_facebook_page():
    footer = footer_html()
    assert PAGE_URL in footer, "public footer does not link the Facebook page"
    assert 'href="https://www.facebook.com/apps' not in footer, \
        "footer links a Facebook apps page, not the business Page"


def test_link_opens_safely():
    footer = footer_html()
    tag = re.search(r"<a\b[^>]*facebook\.com/BuildStackConstructionLLC[^>]*>", footer)
    assert tag, "facebook link tag not found"
    attrs = tag.group(0)
    assert 'target="_blank"' in attrs, "external link should open in a new tab"
    assert 'rel="noopener"' in attrs, "target=_blank without rel=noopener"


def test_footer_carries_no_broom_number():
    footer = footer_html()
    for bad in ("908-7121", "9087121", "17579087121"):
        assert bad not in footer, f"Broom number {bad} leaked into the construction footer"


if __name__ == "__main__":
    test_footer_links_the_facebook_page()
    print("  PASS  footer links the Facebook page")
    test_link_opens_safely()
    print("  PASS  link opens with rel=noopener")
    test_footer_carries_no_broom_number()
    print("  PASS  no Broom number in the construction footer")
