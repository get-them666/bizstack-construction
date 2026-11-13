"""Readable rendering for imported bid notes.

The bid importer stores the solicitation email exactly as it arrived. That is
right for the record, but the Zoho wrapper CSS and the quoted email chain make
the card unreadable. Keep the raw text in ``bid_opportunities.notes``; format a
display copy here. Everything is HTML-escaped before any tags are added, so a
solicitation cannot inject markup into the card.
"""
from __future__ import annotations

import html
import re

# Labels that appear inline in the middle of the imported email. All-caps
# section headers are matched generically below; these are the Title Case ones
# the importer joins onto the previous sentence.
_TITLE_LABELS = (
    "Bid Due", "Bid Documents", "Bid Submission", "Project Address", "Project",
    "Project Details", "Project Summary", "Loberg Estimator", "Location", "Trade",
    "Trades", "Trades Involved", "Estimated Start Date", "Estimated Completion Date",
    "Estimated Finish Date", "Tax Exemption", "Site Visits", "Bid Questions/RFIs",
    "Special Instructions", "Schedule", "Scope", "Notes", "Attn", "Description",
    "Job walk",
)
_TITLE_LABEL_RE = re.compile(
    r"(?<=[a-z0-9.,)!:])\s+(?=(" + "|".join(re.escape(x) for x in _TITLE_LABELS) + r")\s*:)",
    re.I)
_CAPS_LABEL_RE = re.compile(r"(?<=[a-z0-9.,)!])\s+(?=([A-Z][A-Z0-9 &/.'-]{3,45})\s*:)")

# A Zoho/email stylesheet rule. Selectors carry zm_/x_/Mso, and the properties
# never contain braces. Remove rules rather than trying to parse CSS.
_CSS_RULE = re.compile(
    r"(?is)[^{}]{0,400}(?:zm_|x_|mso-|@media|@page)[^{}]*\{[^{}]*\}\s*")
_TAG = re.compile(r"(?is)<(style|script)[^>]*>.*?</\1>|<[^>]+>")
_LABEL_LINE = re.compile(
    r"(?im)^(" + "|".join(re.escape(x) for x in _TITLE_LABELS) + r"|"
    r"[A-Z][A-Z0-9 &/.'-]{3,45})\s*:")

# "024119 - Selective Demolition, 038000 - Concrete Cutting, ..." — a trade
# list, which is far easier to scan as bullets than as one comma run.
_TRADE_ITEM = re.compile(r"(?<!^)\s*,\s*(?=\d{6}\s*-)")


def _clean(raw: str) -> str:
    text = html.unescape(raw or "")
    text = _TAG.sub(" ", text)
    for _ in range(40):
        new = _CSS_RULE.sub(" ", text)
        if new == text:
            break
        text = new
    # Any @media block that survived nested braces.
    text = re.sub(r"(?is)@media[^{]*\{.*", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    # A hard <br> in the solicitation leaves "VA." hanging under the address.
    text = re.sub(r",\n(?=[A-Z]{2}\.)", ", ", text)

    # Put inline section headers on their own line so the card reads as a form.
    text = _CAPS_LABEL_RE.sub("\n\n", text)
    text = _TITLE_LABEL_RE.sub("\n\n", text)

    # A solicitor's "Reply to Painting / Reply to Flooring" list and the
    # importer's "Job walk · RFIs due · Start" line are both bullet lists.
    text = re.sub(r"(?im)^(Trades Involved:)\s*(\d{6}\s*-)", r"\1\n• \2", text)
    text = re.sub(r"\s+(?=Reply to\s)", "\n• ", text)
    text = text.replace(" · ", "\n• ")
    text = _TRADE_ITEM.sub("\n• ", text)
    text = text.replace("&bull;", "•")
    # Trim the stray bullet opening the very first line, if any.
    text = re.sub(r"(?m)^•\s*", "• ", text)
    return text.strip()


def _render_block(block: str) -> str:
    escaped = html.escape(block or "")
    lines = [line.strip() for line in escaped.splitlines() if line.strip()]

    # A line that is only a section header is a heading; one carrying a value
    # keeps the label bold inline and the value normal.
    out: list[str] = []
    in_list = False

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in lines:
        if line.startswith(("•", "-", "*")) or re.match(r"^[A-Z][A-Z0-9 &/.'-]{3,45}:$", line):
            if line.startswith(("•", "-", "*")):
                if not in_list:
                    out.append('<ul class="list-disc pl-5 space-y-1 text-sm leading-6 text-slate-700">')
                    in_list = True
                out.append(f"<li>{line.lstrip('•-* ').strip()}</li>")
                continue
        close_list()
        label = _LABEL_LINE.match(line)
        if label:
            rest = line[label.end():].strip()
            if rest:
                out.append(
                    '<p class="text-sm leading-6 text-slate-700">'
                    f"<strong>{label.group(1)}:</strong> {rest}</p>")
            else:
                out.append(
                    '<p class="mt-3 text-[11px] font-bold uppercase tracking-wide '
                    f'text-slate-500">{label.group(1)}</p>')
            continue
        out.append(f'<p class="text-sm leading-6 text-slate-700">{line}</p>')
    close_list()
    return "".join(out)


def format_notes(raw: str) -> str:
    """Return safe HTML for a bid's imported notes."""
    text = _clean(raw)
    if not text:
        return ""
    parts = re.split(r"\s*---\s*from email\s+", text, flags=re.I)
    main = parts[0].strip()
    older = parts[1:]
    if not main and older:
        main, older = older[0], older[1:]
    out = [_render_block(main)]
    for part in older:
        header, sep, body = part.partition("---")
        header = header.strip() or "Earlier email"
        if not sep:
            body = part
        out.append(
            '<details class="mt-3 rounded-xl border border-slate-200 bg-slate-50 p-3">'
            '<summary class="cursor-pointer text-xs font-bold text-slate-600">'
            f"Earlier email: {html.escape(header[:180])}</summary>"
            f'<div class="mt-2 space-y-2">{_render_block(body)}</div></details>')
    return "".join(out)
