"""Ensure every standalone construction page layout includes the Google tag once."""

from pathlib import Path
import re


TEMPLATES = Path(__file__).resolve().parent / "templates" / "construction"
TAG = 'https://www.googletagmanager.com/gtag/js?id=G-744XSDL07S'
CONFIG = "gtag('config', 'G-744XSDL07S');"


def test_shared_and_standalone_layouts_include_google_tag_once_after_head():
    for name in ("base.html", "app.html"):
        source = (TEMPLATES / name).read_text()
        head = source.index("<head>")
        tag = source.index(TAG)
        assert source.count(TAG) == 1
        assert source.count(CONFIG) == 1
        assert re.match(
            r"<head>\s*<!-- Google tag \(gtag\.js\) -->\s*<script async "
            r"src=\"https://www\.googletagmanager\.com/gtag/js\?id=G-744XSDL07S\"></script>",
            source[head:],
        )
