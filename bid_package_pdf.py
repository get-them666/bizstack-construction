"""Full bid package PDF for one bid.

``bid_proposal.generate_proposal_pdf`` is the short email attachment: a header,
the cost lines, a total. That is fine for firing off a number, but a bid a GC
signs needs the rest of the package -- what is included, what is not, what the
price assumes, and how it gets paid.

This module renders that document. It reads the same rows the bid card already
shows (``bid_opportunities`` + ``bid_costs``), so the PDF can never drift from
the card: no stored copy, no second source of truth. The route regenerates it on
every request.

Design rules, learned from the existing proposal generator:

* ASCII only. FPDF core fonts are latin-1; a curly quote or an em dash raises.
* Nothing here is invented. Scope lines come from the cost lines, and the
  assumptions section is written by the caller via ``bid["notes"]`` or by the
  route's static terms. If a number is not in the database it is not on the
  page.
"""

from __future__ import annotations

from datetime import date, datetime

from fpdf import FPDF

COMPANY = "BuildStack Construction LLC"
# Full street address, not just the city. A contract that names no address for
# the contractor is weaker than one that does, and MEMORY.md records this as the
# owner-confirmed return address that already prints on the letterhead.
ADDRESS = "701 Dana Dr, Chesapeake, VA 23321"
PHONE = "757-908-7121"
EMAIL = "hello@bizstackperks.com"

VALID_DAYS = 30

# Payment structure. Mirrors quoting.py's 30% deposit convention so the PDF and
# the quote builder cannot quote two different deposit percentages.
DEPOSIT_PCT = 30
PROGRESS_PCT = 40
FINAL_PCT = 30

TERMS = [
    f"{DEPOSIT_PCT}% deposit due at signing to schedule and order materials.",
    f"{PROGRESS_PCT}% due when rough-in, drywall and substrate work are complete.",
    f"{FINAL_PCT}% due on final walkthrough and acceptance.",
    "Invoices are net 15. Work is scheduled on receipt of the deposit.",
    f"This proposal is valid for {VALID_DAYS} days from the proposal date.",
]

# Written in the document verbatim. Deliberately specific: a bid that does not
# name its exclusions is a bid that absorbs them later.
EXCLUSIONS = [
    "Building permits, plan review and inspection fees, unless listed above.",
    "Structural repair, framing replacement or joist work not listed in scope.",
    "Mold remediation beyond the sewage cleanup and disinfection listed above.",
    "Asbestos or lead abatement, testing or disposal.",
    "Moving or storing the owner's furniture, appliances or personal property.",
    "Wall or ceiling paint beyond two finish coats over prepared surfaces.",
    "Fixtures, trim profiles or finishes not listed in the scope of work.",
    "HOA approvals, architectural review fees and associated delays.",
    "Work outside normal hours, or access delays caused by the owner or other trades.",
]

ASSUMPTIONS = [
    "All sewage-affected areas are fully cleaned, disinfected and dry before any "
    "finish work begins. Finish work on a wet or contaminated substrate is not "
    "covered by this price.",
    "Pricing assumes the quantities listed in the scope of work. Concealed damage "
    "found after demolition - rot, compromised subfloor, failed plumbing - is "
    "handled as a written change order before the work proceeds.",
    "Owner provides clear access, working power and water at the site during "
    "scheduled work hours.",
    "Existing surfaces are sound and ready to receive finish unless noted.",
    "Debris haul-away covers job-generated debris only.",
]


class BidPackagePDF(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 16)
        self.cell(0, 9, COMPANY, new_x="LMARGIN", new_y="NEXT", align="L")
        self.set_font("Helvetica", "", 9)
        self.set_text_color(90, 90, 90)
        self.cell(0, 4.5, f"{ADDRESS}  |  {PHONE}  |  {EMAIL}",
                  new_x="LMARGIN", new_y="NEXT", align="L")
        self.set_text_color(0, 0, 0)
        self.ln(1.5)
        self.set_draw_color(200, 200, 200)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(5)

    def footer(self):
        self.set_y(-13)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 8, f"{COMPANY}  -  Bid package  -  page {self.page_no()}",
                  align="C")


def _money(v) -> str:
    try:
        return f"${float(v or 0):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _txt(v) -> str:
    """Coerce to latin-1 safe text so a stray unicode char cannot 500 the route."""
    s = "" if v is None else str(v)
    return s.encode("latin-1", "replace").decode("latin-1")


def _heading(pdf: BidPackagePDF, text: str) -> None:
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, text.upper(), new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(225, 225, 225)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(2)


def _bullets(pdf: BidPackagePDF, items: list[str]) -> None:
    pdf.set_font("Helvetica", "", 9)
    for it in items:
        pdf.set_x(pdf.l_margin + 3)
        pdf.multi_cell(pdf.epw - 3, 4.8, "- " + _txt(it))
        pdf.ln(0.6)


CATEGORY_LABELS = {
    "material": "Materials",
    "labor": "Labor",
    "sub": "Subcontract",
    "equipment": "Equipment",
    "other": "General",
}


def _scope_items(bid: dict, costs: list[dict]) -> list[str]:
    """The scope narrative, in the order the work happens.

    Prefers ``bid["scope"]`` -- one task per line, written by whoever priced the
    job. Falls back to the cost lines' work items (labor, subcontract, general)
    when a bid has no scope text, which is better than printing nothing.
    """
    raw = (bid.get("scope") or "").strip()
    if raw:
        return [ln.strip(" -•\t") for ln in raw.splitlines() if ln.strip(" -•\t")]
    out = []
    for c in sorted(costs, key=lambda x: x.get("id") or 0):
        if (c.get("category") or "") == "material":
            continue
        desc = (c.get("description") or "").strip()
        if desc:
            out.append(desc)
    return out


def _labelled(pdf: BidPackagePDF, label: str, value: str) -> None:
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(32, 5.2, label)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5.2, _txt(value), new_x="LMARGIN", new_y="NEXT")


def generate_bid_package_pdf(bid: dict, costs: list[dict], markup_pct: float = 0,
                             summary: dict | None = None,
                             signature: dict | None = None) -> bytes:
    """Render the full package. Regenerated per request, never stored.

    ``signature`` is ``{"png": bytes, "name": str, "date": str}`` or None. It is
    applied here rather than to a stored PDF so a re-priced bid can never go out
    carrying a signature that was put to the old numbers.
    """
    bid = dict(bid or {})
    costs = list(costs or [])
    try:
        markup_pct = float(markup_pct or 0)
    except (TypeError, ValueError):
        markup_pct = 0

    subtotal = sum(float(c.get("estimated") or 0) for c in costs)
    if subtotal <= 0:
        # No itemised lines: price from the bid amount already on the card.
        try:
            total = float((bid or {}).get("bid_amount") or 0)
        except (TypeError, ValueError):
            total = 0.0
        markup = 0.0
        markup_pct = 0
    else:
        markup = subtotal * (markup_pct / 100.0)
        total = subtotal + markup

    pdf = BidPackagePDF(format="letter")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(15, 15, 15)
    pdf.add_page()

    # ---- title block -----------------------------------------------------
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 8, "BID PROPOSAL", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, f"Bid #{bid.get('id', '')}   |   "
                   f"Proposal date: {date.today().strftime('%B %d, %Y')}   |   "
                   f"Valid {VALID_DAYS} days",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    _heading(pdf, "Project")
    _labelled(pdf, "Project", bid.get("name") or "Interior renovation")
    if bid.get("location"):
        _labelled(pdf, "Location", bid["location"])
    if bid.get("owner"):
        _labelled(pdf, "Prepared for", bid["owner"])
    if bid.get("contact_name") or bid.get("contact_email"):
        _labelled(pdf, "Contact",
                  " / ".join(x for x in [bid.get("contact_name"), bid.get("contact_email")] if x))
    if bid.get("trade"):
        _labelled(pdf, "Trade", bid["trade"])
    if bid.get("due_date"):
        _labelled(pdf, "Bid due", str(bid["due_date"])[:10])

    # ---- scope -----------------------------------------------------------
    _heading(pdf, "Scope of work")
    scope = _scope_items(bid, costs)
    if scope:
        pdf.set_font("Helvetica", "", 9)
        for i, desc in enumerate(scope, start=1):
            pdf.set_x(pdf.l_margin + 1)
            pdf.multi_cell(pdf.epw - 1, 4.8, f"{i}. {_txt(desc)}")
            pdf.ln(0.6)
    else:
        pdf.set_font("Helvetica", "I", 9)
        pdf.multi_cell(0, 5, "No scope lines have been entered on this bid yet.")

    # ---- conditions ------------------------------------------------------
    _heading(pdf, "Conditions and assumptions")
    _bullets(pdf, ASSUMPTIONS)

    # ---- pricing ---------------------------------------------------------
    _heading(pdf, "Pricing")
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_fill_color(245, 245, 245)
    pdf.cell(22, 7, "Category", border=1, fill=True)
    pdf.cell(116, 7, "Description", border=1, fill=True)
    pdf.cell(32, 7, "Amount", border=1, fill=True, align="R",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8.5)
    # Entry order, not category order: the lines were priced in job sequence, so
    # the client reads sewage cleanup first and final cleanup last.
    for c in sorted(costs, key=lambda x: x.get("id") or 0):
        cat = CATEGORY_LABELS.get((c.get("category") or "").strip(), "General")
        desc = _txt((c.get("description") or "")[:74])
        pdf.cell(22, 6, cat, border=1)
        pdf.cell(116, 6, desc, border=1)
        pdf.cell(32, 6, _money(c.get("estimated")), border=1, align="R",
                 new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(138, 6.5, "Subtotal", align="R")
    pdf.cell(32, 6.5, _money(subtotal), align="R", new_x="LMARGIN", new_y="NEXT")
    if markup_pct:
        pdf.cell(138, 6.5, f"Overhead and profit ({markup_pct:g}%)", align="R")
        pdf.cell(32, 6.5, _money(markup), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(138, 9, "TOTAL BID PRICE", align="R")
    pdf.cell(32, 9, _money(total), align="R", new_x="LMARGIN", new_y="NEXT")

    # ---- payment ---------------------------------------------------------
    _heading(pdf, "Payment terms")
    _bullets(pdf, TERMS)

    # ---- exclusions ------------------------------------------------------
    _heading(pdf, "Not included")
    _bullets(pdf, EXCLUSIONS)

    # ---- notes -----------------------------------------------------------
    notes = (bid.get("notes") or "").strip()
    if notes:
        _heading(pdf, "Notes")
        pdf.set_font("Helvetica", "", 8.5)
        for line in notes.splitlines():
            line = line.strip()
            if line:
                pdf.multi_cell(0, 4.6, _txt(line))
                pdf.ln(0.4)

    # ---- acceptance ------------------------------------------------------
    if pdf.get_y() > 225:
        pdf.add_page()
    _heading(pdf, "Acceptance")
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(0, 5, "Signing below accepts this proposal, its scope, "
                         "assumptions and exclusions.")
    pdf.ln(10)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(85, 5, "Client signature")
    pdf.cell(85, 5, "Date", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + 80, pdf.get_y())
    pdf.line(pdf.l_margin + 88, pdf.get_y(), pdf.l_margin + 168, pdf.get_y())
    pdf.ln(10)
    pdf.cell(85, 5, f"For {COMPANY}")
    pdf.cell(85, 5, "Date", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    line_y = pdf.get_y()
    if signature:
        # Above the rule, not on it: the strokes sit on the line the way a pen
        # would, and the printed name and date go under the rule.
        pdf.image(signature["png"], x=pdf.l_margin + 2, y=line_y - 13,
                  w=62, h=12, keep_aspect_ratio=True)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(90, 90, 90)
        pdf.text(pdf.l_margin + 88, line_y - 1.5, _txt(signature.get("date") or ""))
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 9)
    pdf.line(pdf.l_margin, line_y, pdf.l_margin + 80, line_y)
    pdf.line(pdf.l_margin + 88, line_y, pdf.l_margin + 168, line_y)
    if signature:
        pdf.ln(4)
        pdf.set_font("Helvetica", "I", 7.5)
        pdf.set_text_color(110, 110, 110)
        pdf.cell(85, 4, _txt(f"Signed electronically by {signature.get('name') or ''}"))
        pdf.set_text_color(0, 0, 0)

    return bytes(pdf.output())


def package_filename(bid: dict) -> str:
    """Stable, filesystem-safe name for the download."""
    name = str((bid or {}).get("name") or "bid")
    keep = "".join(ch if (ch.isalnum() or ch in " -_") else "" for ch in name)
    slug = "-".join(keep.split())[:60] or "bid"
    return f"BuildStack-Bid-{bid.get('id', '')}-{slug}.pdf"
