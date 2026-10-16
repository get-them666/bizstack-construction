#!/usr/bin/env python3
"""Render docs/subcontractor_application.md to a .docx and a .pdf.

Two formats from one source, because the two jobs differ: the .docx is the
fillable master a person edits after dropping in a license number, and the .pdf
is what gets emailed to a general contractor, which must not change once it is
sent. Editing the markdown and re-running keeps them in step.

    venv/bin/python docs/build_subapplication.py             # Parts 1-2, for the GC
    venv/bin/python docs/build_subapplication.py --internal  # Parts 1-6, working copy
    venv/bin/python docs/build_subapplication.py --outdir /tmp/check

Everything the markdown supports is handled here: ATX headings, paragraphs,
`-`/`*` bullets, `1.` numbered lists, `- [ ]` checkboxes, pipe tables,
`>` blockquotes, `---` rules, and `**bold**` / `*italic*` / `` `code` `` runs.
That is the whole feature set on purpose -- a document converter that grows a
dependency is worse than one that covers exactly what this file uses.

Kept off the runtime dependency list. `python-docx` and `fpdf2` are both already
installed for other modules (lob_send/mail_letters use fpdf2), but this script is
a build tool, not something the app imports, so it does not claim a place in
requirements.txt.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DOC_DIR = Path(__file__).resolve().parent
SRC = DOC_DIR / "subcontractor_application.md"
STEM = "subcontractor_application"

# ── inline formatting ───────────────────────────────────

_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITAL = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")


def _strip_inline(text: str) -> str:
    """Flatten the inline markers. fpdf cannot switch weight mid-line."""
    text = _CODE.sub(r"\1", text)
    text = _BOLD.sub(r"\1", text)
    return _ITAL.sub(r"\1", text)


def _runs(text: str):
    """Split into (text, bold, italic, code) runs for python-docx."""
    out, pos = [], 0
    pattern = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*|(?<!\*)\*[^*\n]+\*(?!\*))")
    for m in pattern.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False, False, False))
        tok = m.group(0)
        if tok.startswith("`"):
            out.append((tok[1:-1], False, False, True))
        elif tok.startswith("**"):
            out.append((tok[2:-2], True, False, False))
        else:
            out.append((tok[1:-1], False, True, False))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False, False, False))
    return [r for r in out if r[0]] or [("", False, False, False)]


def ascii_safe(text: str) -> str:
    """WinAnsi/latin-1 fold.

    fpdf2's core fonts are latin-1 and raise on a character outside it, so an
    em-dash or curly quote anywhere in the markdown would abort the render
    rather than degrade. This mirrors documents_service._pdf_text, which
    mail_letters.py uses for the same reason.
    """
    repl = {
        "—": "--", "–": "-", "‘": "'", "’": "'",
        "“": '"', "”": '"', "·": ".", "•": "-",
        "…": "...", " ": " ", "→": "->",
    }
    for bad, good in repl.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "replace").decode("latin-1")


# ── markdown parsing ────────────────────────────────────

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET = re.compile(r"^[-*]\s+\[[ xX]?\]\s+(.*)$")
ULI = re.compile(r"^[-*]\s+(.*)$")
OLI = re.compile(r"^(\d+)[.)]\s+(.*)$")
HR = re.compile(r"^(-{3,}|_{3,}|\*{3,})$")
QUOTE = re.compile(r"^>\s?(.*)$")
TABLE_SEP = re.compile(r"^\|?[\s:|-]+\|[\s:|-]*$")
INTERNAL_ON = re.compile(r"^<!--\s*internal\s*-->$", re.I)
INTERNAL_OFF = re.compile(r"^<!--\s*/\s*internal\s*-->$", re.I)


def _table_cells(line: str):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse(text: str, keep_internal: bool):
    """markdown -> a flat list of block dicts.

    Blocks: h1/h2/h3, p, ul, ol, check, quote, table, rule.
    """
    blocks, internal_on = [], False
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()

        if INTERNAL_ON.match(stripped):
            internal_on = True
            i += 1
            continue
        if INTERNAL_OFF.match(stripped):
            internal_on = False
            i += 1
            continue
        if internal_on and not keep_internal:
            i += 1
            continue

        if not stripped:
            i += 1
            continue

        if HR.match(stripped):
            blocks.append({"t": "rule"})
            i += 1
            continue

        m = HEADING.match(stripped)
        if m:
            level = min(len(m.group(1)), 3)
            blocks.append({"t": f"h{level}", "text": m.group(2).strip()})
            i += 1
            continue

        # Pipe table: a header row plus a |---|---| separator.
        if "|" in stripped and i + 1 < len(lines) and TABLE_SEP.match(lines[i + 1].strip()):
            header = _table_cells(stripped)
            rows, i = [], i + 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_table_cells(lines[i]))
                i += 1
            blocks.append({"t": "table", "header": header, "rows": rows})
            continue

        if QUOTE.match(stripped):
            buf = []
            while i < len(lines) and (QUOTE.match(lines[i].strip()) or not lines[i].strip()):
                q = QUOTE.match(lines[i].strip())
                if q:
                    buf.append(q.group(1))
                elif buf:
                    buf.append("")
                i += 1
            blocks.append({"t": "quote", "text": "\n".join(buf).strip()})
            continue

        if BULLET.match(stripped):
            items = []
            while i < len(lines):
                bm = BULLET.match(lines[i].strip())
                if bm:
                    items.append(bm.group(1))
                elif lines[i].strip() and ULI.match(lines[i].strip()):
                    items.append(ULI.match(lines[i].strip()).group(1))
                else:
                    break
                i += 1
            blocks.append({"t": "check", "items": items})
            continue

        if ULI.match(stripped):
            items = []
            while i < len(lines) and ULI.match(lines[i].strip()):
                items.append(ULI.match(lines[i].strip()).group(1))
                i += 1
            blocks.append({"t": "ul", "items": items})
            continue

        if OLI.match(stripped):
            items = []
            while i < len(lines) and OLI.match(lines[i].strip()):
                items.append(OLI.match(lines[i].strip()).group(2))
                i += 1
            blocks.append({"t": "ol", "items": items})
            continue

        # Paragraph: consume until a blank line or the start of another block.
        buf = []
        while i < len(lines) and lines[i].strip():
            nxt = lines[i].strip()
            if HEADING.match(nxt) or HR.match(nxt) or ULI.match(nxt) or OLI.match(nxt) \
                    or QUOTE.match(nxt) or nxt.startswith("|") or INTERNAL_ON.match(nxt) \
                    or INTERNAL_OFF.match(nxt):
                break
            buf.append(nxt)
            i += 1
        if buf:
            blocks.append({"t": "p", "text": " ".join(buf)})

    return blocks


# ── docx ────────────────────────────────────────────────

def render_docx(blocks, path: Path) -> None:
    try:
        import docx
        from docx.enum.table import WD_TABLE_ALIGNMENT
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Pt, RGBColor, Inches
    except ImportError:
        sys.exit("python-docx is not installed in this venv.")

    d = docx.Document()
    normal = d.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)

    def emit(par, text, *, bold=False, italic=False, size=None, color=None, font=None):
        for txt, b, it, code in _runs(text):
            r = par.add_run(txt)
            r.bold = bold or b
            r.italic = italic or it
            if code:
                r.font.name = "Consolas"
                r.font.size = Pt((size or 10.5) - 0.5)
            elif font:
                r.font.name = font
            if size:
                r.font.size = Pt(size)
            if color:
                r.font.color.rgb = RGBColor(*color)
        return par

    for b in blocks:
        t = b["t"]
        if t == "h1":
            p = d.add_heading("", level=0)
            emit(p, _strip_inline(b["text"]), size=20, bold=True, color=(17, 24, 39))
        elif t == "h2":
            p = d.add_heading("", level=1)
            emit(p, _strip_inline(b["text"]), size=15, bold=True, color=(31, 41, 55))
        elif t == "h3":
            p = d.add_heading("", level=2)
            emit(p, _strip_inline(b["text"]), size=12.5, bold=True, color=(75, 85, 99))
        elif t == "p":
            emit(d.add_paragraph(), b["text"])
        elif t in ("ul", "ol"):
            style = "List Bullet" if t == "ul" else "List Number"
            for item in b["items"]:
                emit(d.add_paragraph(style=style), item)
        elif t == "check":
            for item in b["items"]:
                emit(d.add_paragraph(style="List Bullet"), "[ ]  " + item)
        elif t == "quote":
            for line in b["text"].split("\n"):
                p = d.add_paragraph()
                if line.strip():
                    emit(p, line, italic=True)
                else:
                    p.paragraph_format.space_after = Pt(4)
                p.paragraph_format.left_indent = Inches(0.3)
        elif t == "table":
            header, rows = b["header"], b["rows"]
            ncols = max([len(header)] + [len(r) for r in rows] or [1])
            table = d.add_table(rows=1, cols=ncols)
            table.style = "Table Grid"
            table.alignment = WD_TABLE_ALIGNMENT.LEFT
            for c, val in enumerate(header):
                cell = table.rows[0].cells[c]
                cell.text = ""
                emit(cell.paragraphs[0], val, bold=True, size=9.5)
            for row in rows:
                cells = table.add_row().cells
                for c in range(ncols):
                    cells[c].text = ""
                    if c < len(row):
                        emit(cells[c].paragraphs[0], row[c], size=9.5)
            d.add_paragraph()
        elif t == "rule":
            p = d.add_paragraph()
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after = Pt(6)
            r = p.add_run("_" * 92)
            r.font.color.rgb = RGBColor(210, 214, 220)

    d.save(str(path))


# ── pdf ─────────────────────────────────────────────────

def render_pdf(blocks, path: Path) -> None:
    try:
        from fpdf import FPDF
    except ImportError:
        sys.exit("fpdf2 is not installed in this venv.")

    pdf = FPDF(format="Letter")
    pdf.set_auto_page_break(True, margin=20)
    pdf.set_margins(18, 16, 18)
    pdf.set_title("Buildstack Construction Co. - Subcontractor Application")
    pdf.alias_nb_pages()

    W, L, R = pdf.w - 36, 18, pdf.w - 18
    INK, MUTE, RED = (17, 24, 39), (100, 108, 120), (176, 58, 58)

    def footer():
        pdf.set_auto_page_break(False)
        pdf.set_y(-14)
        pdf.set_font("helvetica", size=7)
        pdf.set_text_color(140, 145, 155)
        pdf.cell(0, 4, ascii_safe(
            f"Buildstack Construction Co.  |  page {pdf.page_no()}/{{nb}}  |  DRAFT - "
            "replace every [BLANK] before submission"), align="C")
        pdf.set_auto_page_break(True, margin=20)

    def room(needed):
        if pdf.get_y() + needed > pdf.h - 22:
            footer()
            pdf.add_page()
            return True
        return False

    pdf.add_page()

    for b in blocks:
        t = b["t"]

        if t == "h1":
            room(26)
            pdf.set_font("helvetica", "B", 19)
            pdf.set_text_color(*INK)
            pdf.multi_cell(0, 9, ascii_safe(_strip_inline(b["text"])))
            pdf.ln(2)
        elif t in ("h2", "h3"):
            size, gap = (14, 6) if t == "h2" else (11.5, 4)
            room(size + gap + 8)
            if t == "h2":
                pdf.ln(3)
            pdf.set_font("helvetica", "B", size)
            pdf.set_text_color(*INK if t == "h2" else MUTE)
            pdf.multi_cell(0, size * 0.62, ascii_safe(_strip_inline(b["text"])))
            pdf.ln(gap)
        elif t == "p":
            room(20)
            pdf.set_font("helvetica", size=10)
            pdf.set_text_color(31, 41, 55)
            txt = _strip_inline(b["text"])
            if "[BLANK]" in txt or "[____" in txt:
                pdf.set_text_color(*RED)
            pdf.multi_cell(0, 5.6, ascii_safe(txt))
            pdf.ln(3)
            pdf.set_text_color(31, 41, 55)
        elif t in ("ul", "ol", "check"):
            pdf.set_font("helvetica", size=10)
            pdf.set_text_color(31, 41, 55)
            for n, item in enumerate(b["items"], 1):
                if t == "ol":
                    bullet = f"{n}."
                elif t == "check":
                    bullet = "[ ]"
                else:
                    bullet = "-"
                txt = _strip_inline(item)
                pdf.set_x(L)
                pdf.cell(12, 5.6, bullet)
                w = W - 12
                lines = _wrap(txt, pdf, w, 5.6)
                room(len(lines) * 5.6)
                for k, line in enumerate(lines):
                    pdf.set_x(L + 12)
                    pdf.cell(w, 5.6, ascii_safe(line))
                    pdf.ln(5.6)
                pdf.ln(0.6)
            pdf.ln(2.4)
        elif t == "quote":
            room(16)
            pdf.set_fill_color(248, 249, 251)
            x0, y0 = pdf.get_x(), pdf.get_y()
            lines = _wrap(_strip_inline(b["text"]).replace("\n", "  "), pdf, W - 10, 5.6)
            hgt = len(lines) * 5.6 + 6
            if y0 + hgt > pdf.h - 22:
                footer()
                pdf.add_page()
                x0, y0 = pdf.get_x(), pdf.get_y()
            pdf.rect(x0, y0, W, hgt, style="F")
            pdf.set_x(x0 + 5)
            pdf.set_y(y0 + 3)
            pdf.set_font("helvetica", size=9.5)
            pdf.set_text_color(55, 65, 81)
            for line in lines:
                pdf.cell(W - 10, 5.6, ascii_safe(line))
                pdf.ln(5.6)
            pdf.set_y(y0 + hgt + 3)
        elif t == "table":
            header, rows = b["header"], b["rows"]
            ncols = max([len(header)] + [len(r) for r in rows] or [1])
            weights = _col_widths(header, rows, ncols)
            widths = [W * wt / sum(weights) for wt in weights]
            rh = 5.4

            pdf.set_font("helvetica", "B", 8.6)
            pdf.set_fill_color(243, 244, 246)
            pdf.set_text_color(*INK)
            y = pdf.get_y()
            if y + rh * 2 > pdf.h - 22:
                footer()
                pdf.add_page()
            x = L
            pdf.rect(L, pdf.get_y(), W, rh, style="F")
            for c in range(ncols):
                pdf.set_xy(x + 1.6, pdf.get_y() + 1.2)
                pdf.cell(widths[c] - 3, rh - 2, ascii_safe(_strip_inline(header[c])), align="L")
                x += widths[c]
            pdf.ln(rh)
            pdf.set_draw_color(215, 219, 225)
            pdf.line(L, pdf.get_y(), R, pdf.get_y())

            pdf.set_font("helvetica", size=8.6)
            pdf.set_text_color(45, 55, 70)
            for row in rows:
                cells = [_strip_inline(row[c]) if c < len(row) else "" for c in range(ncols)]
                nlines = max(len(_wrap(cells[c], pdf, widths[c] - 3, 4.6)) for c in range(ncols))
                hgt = max(rh, nlines * 4.6 + 2.2)
                if pdf.get_y() + hgt > pdf.h - 22:
                    footer()
                    pdf.add_page()
                ytop = pdf.get_y()
                x = L
                for c in range(ncols):
                    sub = _wrap(cells[c], pdf, widths[c] - 3, 4.6)
                    pdf.set_xy(x + 1.6, ytop + 1.1)
                    for line in sub:
                        pdf.cell(widths[c] - 3, 4.6, ascii_safe(line), align="L")
                        pdf.set_xy(x + 1.6, pdf.get_y() + 4.6)
                    x += widths[c]
                pdf.set_xy(L, ytop + hgt)
                pdf.set_draw_color(232, 235, 240)
                pdf.line(L, pdf.get_y(), R, pdf.get_y())
            pdf.ln(4)
        elif t == "rule":
            room(10)
            pdf.set_draw_color(220, 224, 230)
            pdf.line(L, pdf.get_y(), R, pdf.get_y())
            pdf.ln(5)

    footer()
    pdf.output(str(path))


def _wrap(text: str, pdf, width_mm: float, line_h: float):
    """Greedy wrap in millimetres, mirroring multi_cell's own algorithm.

    Set at the current font/size, so this must be called with the font already
    set to the size the text will actually be drawn at.
    """
    if not text:
        return [""]
    lines, cur = [], ""
    for word in text.split(" "):
        trial = f"{cur} {word}".strip()
        if pdf.get_string_width(ascii_safe(trial)) <= width_mm or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def _col_widths(header, rows, ncols):
    """Relative column widths from the longest cell, clamped so one long cell
    in a two-column table cannot starve the other."""
    longest = []
    for c in range(ncols):
        vals = [header[c]] + [r[c] for r in rows if c < len(r)]
        longest.append(max((len(v) for v in vals), default=8))
    avg = sum(longest) / max(len(longest), 1)
    return [min(max(w, 6), max(avg * 2.2, 10)) for w in longest]


# ── main ────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--internal", action="store_true",
                    help="include the internal working sections (Parts 3-6)")
    ap.add_argument("--src", default=str(SRC))
    ap.add_argument("--outdir", default=str(DOC_DIR))
    args = ap.parse_args()

    src = Path(args.src)
    if not src.exists():
        sys.exit(f"source not found: {src}")

    blocks = parse(src.read_text(encoding="utf-8"), keep_internal=args.internal)
    if not blocks:
        sys.exit("no renderable blocks; is the source empty?")

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    suffix = "-internal" if args.internal else ""

    docx_path = out / f"{STEM}{suffix}.docx"
    pdf_path = out / f"{STEM}{suffix}.pdf"

    render_docx(blocks, docx_path)
    render_pdf(blocks, pdf_path)

    kinds = {}
    for b in blocks:
        kinds[b["t"]] = kinds.get(b["t"], 0) + 1
    blanks = sum(1 for b in blocks
                 if b["t"] in ("p", "ul", "ol", "check", "quote")
                 and ("[BLANK]" in b.get("text", "") or "[____" in b.get("text", "")
                      or any("[____" in i or "[BLANK]" in i for i in b.get("items", []))))

    scope = "Parts 1-6 (internal)" if args.internal else "Parts 1-2 (submission)"
    print(f"built {scope}")
    print(f"  {docx_path}  ({docx_path.stat().st_size:,} bytes)")
    print(f"  {pdf_path}  ({pdf_path.stat().st_size:,} bytes)")
    print("  blocks: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    if blanks:
        print(f"\n  {blanks} block(s) still contain [BLANK] placeholders -- "
              "fill these before sending anything to a GC.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
