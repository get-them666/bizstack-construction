#!/usr/bin/env python3
"""Draft print-ready letters on company letterhead for address-only leads.

A lead that has a postal address but no email and no phone cannot be reached by
the automated email/text cadence in auto_reply.py -- there is no channel to send
on. This renders one Letter-format PDF per such lead so the owner can print and
mail them by hand.

Deliberately OFFLINE: this never sends anything and never writes to comms_logs.
It only reads leads and writes PDFs + a manifest to disk, so the automated touch
accounting in auto_reply.py is untouched and the owner keeps the decision on
every letter.

    python mail_letters.py --list          # show who would get a letter
    python mail_letters.py --out outbox    # render PDFs + manifest.csv
    python mail_letters.py --out outbox --id 42 --id 43   # specific leads
"""

import argparse
import csv
import os
import re
import sys
from datetime import date
from pathlib import Path

from fpdf import FPDF

from documents_service import _pdf_text

BRAND = {
    "name": os.getenv("LETTER_BRAND_NAME", "BizStack"),
    "legal": os.getenv("LETTER_BRAND_LEGAL", "Buildstack Construction"),
    # Full contact block on the letter: phone, email, website and return
    # address, which is also what a homeowner needs in order to call back.
    "phone": os.getenv("LETTER_BRAND_PHONE", "252-665-5891"),
    "email": os.getenv("LETTER_BRAND_EMAIL", "hello@bizstackperks.com"),
    "web": os.getenv("LETTER_BRAND_WEB", "bizstackperks.com"),
    # Street is the owner's supplied return address. Still env-overridable.
    # License is intentionally blank: the owner does not publish the business ID
    # on cold mail. It stays wired so LETTER_BRAND_LICENSE can print it if that
    # ever changes, but nothing in the repo sets it.
    "street": os.getenv("LETTER_BRAND_STREET", "701 Dana Dr"),
    "city": os.getenv("LETTER_BRAND_CITYSTATEZIP", "Chesapeake, VA 23321"),
    "license": os.getenv("LETTER_BRAND_LICENSE", ""),
}

SENDER_NAME = os.getenv("LETTER_SENDER_NAME", "Shaun O'Leary")

ADDRESS_ONLY_SQL = """
    SELECT id, name, address, project_type, source, status, notes, created_at
    FROM leads
    WHERE company = %(company)s
      AND status <> 'archived'
      AND address IS NOT NULL AND BTRIM(address) <> ''
      AND (email IS NULL OR BTRIM(email) = '' OR LOWER(email) LIKE '%@lead.local')
      AND (phone IS NULL OR BTRIM(phone) = '' OR phone IN ('unknown', 'n/a', 'N/A'))
    ORDER BY id
"""


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def format_recipient(name) -> str:
    """'Ada Lovelace' -> 'Ada L.'."""
    parts = _clean(name).split()
    if not parts:
        return "Homeowner"
    first = parts[0]
    if first.lower() in {"mr.", "mr", "mrs.", "mrs", "ms.", "ms", "miss", "dr.", "dr"}:
        return _clean(name)
    if len(parts) == 1:
        return first
    return f"{first[0].upper()}. {parts[-1]}"


def address_lines(address) -> list:
    """Split a stored address into postal lines.

    lead_sources.py stores "street, city, state zip" in one field; hand-entered
    website leads often use newlines instead. Normalize both, and fall back to
    comma-splitting so nothing is silently dropped from the mailing block.
    """
    raw = str(address or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return []
    lines = []
    for block in raw.split("\n"):
        for part in block.split(","):
            piece = _clean(part)
            if piece:
                lines.append(piece)
    return lines


def letterhead(pdf: FPDF) -> None:
    """Draw the brand header and reset the cursor below the rule.

    Positions are pinned explicitly instead of relying on cell()/ln() stacking:
    fpdf2's cell() does not advance y, so ln() after it moves relative to the
    top of that cell and the 22pt wordmark collides with the contact line.
    """
    x, w = 20, 176
    top = pdf.get_y()

    pdf.set_font("helvetica", "B", 22)
    pdf.set_text_color(17, 24, 39)
    pdf.set_xy(x, top)
    pdf.cell(w, 12, _pdf_text(BRAND["name"]), align="L")
    y = top + 13

    pdf.set_font("helvetica", "", 9)
    pdf.set_text_color(107, 114, 128)
    contact = "  ·  ".join(
        v for v in (BRAND["phone"], BRAND["email"], BRAND["web"]) if v
    )
    for line in (contact, BRAND["legal"],
                 "  ".join(v for v in (BRAND["street"], BRAND["city"]) if v)):
        if not line:
            continue
        pdf.set_xy(x, y)
        pdf.cell(w, 5, _pdf_text(line), align="L")
        y += 5
    y += 4

    pdf.set_draw_color(17, 24, 39)
    pdf.set_line_width(0.6)
    pdf.line(x, y, x + w, y)
    pdf.set_y(y + 7)


def footer(pdf: FPDF) -> None:
    """Opt-out line + a loud marker for any unfilled letterhead field.

    Auto page-break is disabled first: the footer sits below the normal text
    area, so leaving the break on at a 26mm bottom margin pushes it onto a
    second page (and a blank-looking page one, since the footer is invisible).
    """
    pdf.set_auto_page_break(False)
    pdf.set_y(-27)
    pdf.set_font("helvetica", size=7)
    pdf.set_text_color(120, 120, 130)
    # Return address only: the license is withheld on purpose, so a blank
    # license is not an incomplete letterhead.
    missing = [lbl for lbl, val in (
        ("LETTER_BRAND_STREET", BRAND["street"]),
        ("LETTER_BRAND_CITYSTATEZIP", BRAND["city"]),
    ) if not val]
    # Opt-out must name only channels that are actually printed on the letter:
    # a number nobody can see is not a working opt-out route.
    route = "email " + BRAND["email"] if BRAND["email"] else "reply to this letter"
    if BRAND["phone"]:
        route += f" or call {BRAND['phone']}"
    pdf.multi_cell(0, 3.5, _pdf_text(
        f"To stop receiving mail from us, {route} and we will remove your address. "
        f"— {_clean(BRAND['legal']) or _clean(BRAND['name'])}, "
        f"{BRAND['street'] or '[street pending]'}, {BRAND['city'] or '[city pending]'}"
    ), align="C")
    if BRAND["license"]:
        pdf.ln(1)
        pdf.cell(0, 3.5, _pdf_text(f"License {BRAND['license']}"), align="C")
    if missing:
        pdf.ln(1)
        pdf.set_text_color(180, 60, 60)
        pdf.cell(0, 3.5, _pdf_text(f"DRAFT - letterhead incomplete: set {', '.join(missing)}"), align="C")


def render_letter(lead: dict) -> bytes:
    """One Letter-format letter: letterhead, mailing block, body, opt-out."""
    pdf = FPDF(format="Letter")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    pdf.set_margins(20, 18, 20)
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(True, margin=26)

    letterhead(pdf)

    pdf.set_font("helvetica", size=10)
    pdf.set_text_color(31, 41, 55)
    pdf.cell(0, 6, _pdf_text(format_recipient(lead.get("name"))), align="L")
    pdf.ln(5)
    for line in address_lines(lead.get("address")):
        pdf.cell(0, 6, _pdf_text(line), align="L")
        pdf.ln(5)
    pdf.ln(8)

    pdf.set_text_color(17, 24, 39)
    pdf.set_font("helvetica", size=10)
    pdf.cell(0, 6, _pdf_text(date.today().strftime("%B %-d, %Y")), align="R")
    pdf.ln(10)

    pdf.set_font("helvetica", "B", 11)
    pdf.cell(0, 7, _pdf_text(
        f"Re: {lead.get('project_type') or 'your property'}"
    ), align="L")
    pdf.ln(12)

    greeting = format_recipient(lead.get("name"))
    body = [
        f"Dear {greeting},",
        "",
        "We handle construction and renovation work in this area, and I am writing "
        "because your property came up on a recent permit record.",
        "",
        "I am not sure you are looking for anything right now, so I will keep this to a "
        "single page. If you are planning any work, a free walkthrough and an honest "
        "written estimate costs you nothing and carries no obligation.",
        "",
        "If you would like a walkthrough or an estimate, call or text "
        f"{BRAND['phone']} and ask for me by name, or email {BRAND['email']}. "
        "If the timing is wrong, I will not follow up again.",
        "",
        "Thank you for your time,",
        "",
        SENDER_NAME,
        BRAND["name"],
    ] + ([BRAND["phone"]] if BRAND["phone"] else [])
    # An empty BRAND["phone"] would print a blank signature line, and the
    # call-to-action above names a channel that must actually exist, so both are
    # derived from the same value rather than hardcoded to the phone.
    pdf.set_font("helvetica", size=10)
    for para in body:
        if not para:
            pdf.ln(6)
            continue
        pdf.multi_cell(0, 6, _pdf_text(para))
        pdf.ln(1)
        pdf.ln(5)

    footer(pdf)
    return bytes(pdf.output())


def fetch_leads(company: str, only_ids=None) -> list:
    import psycopg
    from psycopg.rows import dict_row

    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        raise SystemExit("DATABASE_URL is not set; cannot read leads.")
    with psycopg.connect(db_url, row_factory=dict_row) as db:
        with db.cursor() as cur:
            cur.execute(ADDRESS_ONLY_SQL, {"company": company})
            leads = [dict(r) for r in cur.fetchall()]
    if only_ids:
        wanted = {int(i) for i in only_ids}
        leads = [ld for ld in leads if int(ld["id"]) in wanted]
    return leads


def safe_name(lead: dict) -> str:
    base = _clean(lead.get("name")) or f"lead-{lead.get('id')}"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", base)[:60]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--company", default="construction")
    ap.add_argument("--list", action="store_true", help="show eligible leads, write nothing")
    ap.add_argument("--out", default="", help="output directory for PDFs + manifest.csv")
    ap.add_argument("--id", action="append", help="restrict to specific lead id (repeatable)")
    args = ap.parse_args()

    leads = fetch_leads(args.company, args.id)

    if not leads:
        print(f"No address-only leads for company '{args.company}'.")
        return 0

    if args.list or not args.out:
        print(f"{len(leads)} address-only lead(s) eligible for a letter:\n")
        for ld in leads:
            addr = ", ".join(address_lines(ld.get("address")))
            print(f"  #{ld['id']:<5} {_clean(ld.get('name'))[:34]:<34} {addr[:60]}")
        if not args.list:
            print("\nNothing written. Re-run with --out <dir> to render.")
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    rows = []
    for ld in leads:
        stem = f"{int(ld['id']):05d}-{safe_name(ld)}"
        path = out / f"{stem}.pdf"
        try:
            path.write_bytes(render_letter(ld))
        except Exception as exc:
            print(f"  FAILED #{ld['id']} {safe_name(ld)}: {exc}", file=sys.stderr)
            continue
        rows.append({
            "id": ld["id"],
            "name": _clean(ld.get("name")),
            "address": ", ".join(address_lines(ld.get("address"))),
            "project_type": _clean(ld.get("project_type")),
            "source": _clean(ld.get("source")),
            "status": _clean(ld.get("status")),
            "pdf": path.name,
        })
        print(f"  wrote {path}")

    if rows:
        manifest = out / "manifest.csv"
        with manifest.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\n{len(rows)} letter(s) + manifest: {manifest}")

    # Return address only: the license is withheld on purpose, so a blank
    # license is not an incomplete letterhead.
    missing = [lbl for lbl, val in (
        ("LETTER_BRAND_STREET", BRAND["street"]),
        ("LETTER_BRAND_CITYSTATEZIP", BRAND["city"]),
    ) if not val]
    if missing:
        print(
            "\nWARNING: letterhead is incomplete and every PDF says so in the footer."
            f"\n  Set {', '.join(missing)} before printing."
            "\n  A letter without a return street address cannot be actioned if it bounces.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())