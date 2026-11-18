"""HTML email body -> readable plain text.

Almost every real email arrives as HTML, and stripping tags with `<[^>]+>` is
not enough: it removes `<style>` and `</style>` but leaves the *rules between
them*, so the owner's inbox showed a wall of `body{margin:0;padding:0}` above
every message. `<head>` and `<title>` leak the same way.

This is the readable-text cleaner for every mail path (Zoho Mail API, Zoho
IMAP, Gmail): remove the blocks that carry no prose, keep paragraph breaks and
table rows apart, then decode entities. The result is plain text, so callers
stay safe escaping it into the page.

`bid_notes._clean` solves the same problem for bid solicitations and keeps
mail-specific formatting (labels, trade lists); this module is the general
one. Deliberately no dependency outside the standard library -- `inbound_email`
imports psycopg, and `zoho_mail` must still flatten a body if that import ever
fails.
"""

from __future__ import annotations

import html as _html
import re

MAX_CHARS = 8000

# Conditional comments are style/script too ("<!--[if mso]><style>...<![endif]-->").
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)

# <script>, <style>, <head>, <title> and <noscript> carry no prose. Remove the
# whole block, not just its tags: that is exactly what used to leak CSS.
_DROP_BLOCK_RE = re.compile(
    r"<(script|style|head|title|noscript)\b[^>]*>.*?</\1\s*>", re.I | re.S)

# Malformed mail opens <style> and never closes it. Drop to end of document
# rather than printing the rules as if they were the message.
_DROP_UNCLOSED_RE = re.compile(r"<(script|style)\b[^>]*>.*\Z", re.I | re.S)

# Block boundaries become newlines so paragraphs, quotes and table rows stay
# apart in the reader instead of running into one paragraph.
_BREAK_RE = re.compile(
    r"<\s*(?:br|hr|/?p|/?div|/?tr|/?li|/?h[1-6]|/?blockquote|/?table|"
    r"/(?:ul|ol|dl|dd|dt|section|article|header|footer))\b[^>]*>", re.I)

# Table cells are one line each, horizontally separated.
_CELL_RE = re.compile(r"<\s*/?(?:td|th)\b[^>]*>", re.I)

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RUN_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def to_text(raw: str, limit: int = MAX_CHARS) -> str:
    """Flatten an HTML body to plain text (never raises on odd markup)."""
    s = raw or ""
    if not s:
        return ""
    s = _COMMENT_RE.sub("", s)
    s = _DROP_BLOCK_RE.sub("", s)
    s = _DROP_UNCLOSED_RE.sub("", s)
    s = _BREAK_RE.sub("\n", s)
    s = _CELL_RE.sub(" ", s)
    s = _TAG_RE.sub("", s)
    s = _html.unescape(s).replace("\xa0", " ")
    s = _WS_RUN_RE.sub(" ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    s = _BLANK_RUN_RE.sub("\n\n", s).strip()
    return s[:limit] if limit else s
