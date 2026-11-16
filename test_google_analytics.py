"""Ensure the public-site layout (base.html) includes the Google tag once,
and that the crew PWA shell (app.html) does NOT -- tracking crew members is
unwanted (see health check 3.57)."""

from pathlib import Path
import re


TEMPLATES = Path(__file__).resolve().parent / "templates" / "construction"
TAG = 'https://www.googletagmanager.com/gtag/js?id=G-744XSDL07S'
CONFIG = "gtag('config', 'G-744XSDL07S');"


def test_public_layout_includes_google_tag_once_after_head():
    """Public pages should carry the Google tag exactly once, immediately
    after <head>."""
    source = (TEMPLATES / "base.html").read_text()
    head = source.index("<head>")
    tag = source.index(TAG)
    assert source.count(TAG) == 1
    assert source.count(CONFIG) == 1
    assert re.match(
        r"<head>\s*<!-- Google tag \(gtag\.js\) -->\s*<script async "
        r"src=\"https://www\.googletagmanager\.com/gtag/js\?id=G-744XSDL07S\"></script>",
        source[head:],
    )


def test_crew_app_does_not_include_google_tag():
    """The crew PWA is internal -- crew members are not marketing traffic."""
    source = (TEMPLATES / "app.html").read_text()
    assert TAG not in source, "crew app.html should not load Google Analytics"
    assert CONFIG not in source
