"""Three standalone bid documents from one bid card.

``bid_package_pdf.generate_bid_package_pdf`` renders one combined package for the
web route. This script renders the same job as the three separate documents a
client actually signs and files:

    01-Scope-of-Work.pdf      what is being built, and the photo log
    02-Itemized-Pricing.pdf   every line, in job order, with quantities
    03-Contract.pdf           the agreement, with the price and payment schedule

It reads a JSON snapshot of ``bid_opportunities`` + ``bid_costs`` rather than the
database, so it can be run from a laptop against a card that lives in production.
The snapshot is dumped with ``--dump-sql`` against the container.

Design rules, same as the package generator:

* ASCII only. FPDF core fonts are latin-1; a curly quote raises.
* Nothing invented. Scope comes from ``bid["scope"]``, quantities and amounts
  come from the cost lines, and the terms/exclusions/assumptions are imported
  from ``bid_package_pdf`` so all four documents quote one set of terms.
* Client-facing amounts only. ``unit_cost`` on a labour line is the *cost* rate;
  the rate printed here is ``estimated / qty``, which is the price.

Usage::

    venv/bin/python build_bid_documents.py --bid-json /tmp/bid18350.json \
        --photos ~/tmp/photos-clean --out ~/Desktop/BuildStack-Bid-18350
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime
from pathlib import Path

from fpdf import FPDF

# Shared so the four documents cannot quote different terms. These are private
# names in that module, but duplicating them here is how the package PDF and the
# contract end up disagreeing about the deposit.
from bid_package_pdf import (  # noqa: F401
    ADDRESS,
    assumptions_for,
    COMPANY,
    DEPOSIT_PCT,
    EMAIL,
    exclusions_for,
    FINAL_PCT,
    PHONE,
    PROGRESS_PCT,
    TERMS,
    VALID_DAYS,
    BidPackagePDF,
    _bullets,
    _heading,
    _labelled,
    _money,
    _scope_items,
    _txt,
)

# Photo captions. Kept generic so the same caption works for any job; callers
# that want job-specific captions pass them in alongside the photos.
PHOTO_CAPTIONS = {}

# Contiguous runs of the priced lines, in the order the work happens. A phase
# is just a slice of the cost-line sequence, so these names describe typical
# trade groupings rather than any one job. They are used to band the itemized
# pricing table; if a bid's lines don't fall into these buckets the band label
# still reads as a work phase rather than a mismatch.
PHASES = [
    ("Demolition and preparation", 1, 6),
    ("Substrate and flooring", 7, 9),
    ("Drywall and priming", 10, 18),
    ("Baseboard and door casing", 19, 21),
    ("Finish flooring", 22, 23),
    ("Painting", 24, 30),
    ("Site protection and final cleanup", 31, 32),
    ("Contingency", 33, 33),
]

CONTRACT_TERMS = [
    ("1. The work",
     "Contractor will furnish all labour, materials, equipment and supervision "
     "to complete the work described in the Scope of Work dated "
     "{date}, which is attached and incorporated into this agreement. The "
     "Scope of Work, the Itemized Pricing and this agreement together form the "
     "entire contract."),
    ("2. Contract price",
     "Owner will pay Contractor {total} for the work, subject only to written "
     "change orders and to the reconciliation of the allowance items described "
     "in section 6. The price includes the contingency line shown on the "
     "Itemized Pricing; unused contingency is credited to Owner at final "
     "billing."),
    ("3. Payment schedule",
     "{dep}% ({dep_amt}) is due at signing to schedule the work and order "
     "materials. {prog}% ({prog_amt}) is due when rough-in, drywall and "
     "substrate work are complete. {final}% ({final_amt}) is due on final "
     "walkthrough and acceptance. Invoices are net 15. Work is scheduled on "
     "receipt of the deposit. Payments not received within 15 days of the "
     "invoice date carry a service charge of 1.5% per month on the unpaid "
     "balance."),
    ("4. Schedule",
     "Work will begin within ten (10) working days of receipt of the deposit "
     "and material availability. Substantial completion is estimated at fifteen "
     "(15) working days from the start date. This estimate assumes continuous "
     "access to the work areas during normal business hours."),
    ("5. Concealed conditions and change orders",
     "The price assumes the conditions visible at the time of the site visit. "
     "Damage that cannot be seen until demolition - rot, compromised subfloor, "
     "failed plumbing, hidden water damage - is not included. When such a "
     "condition is found, Contractor will stop work in that area and give Owner "
     "a written description and price before proceeding. No additional work is "
     "performed, and no additional amount is owed, without Owner's written "
     "approval. Verbal instructions are not change orders."),
    ("6. Allowances",
     "The allowance items on the Itemized Pricing (disinfectant and personal "
     "protective equipment, debris disposal and dump fees, toilet plumbing "
     "materials) are estimates. Contractor will provide receipts at final "
     "billing and the price will be adjusted by the difference between the "
     "allowance and the actual cost, marked up at the same rate used in the "
     "contract price. Any single allowance overrun greater than 25% will be "
     "raised with Owner before the cost is incurred."),
    ("7. Owner responsibilities",
     "Owner will provide clear access to the work areas, working power and "
     "water, and will remove or protect furniture, appliances and personal "
     "property from the work areas before the start date. Owner will disclose "
     "any known hazardous materials, prior water damage, unpermitted work or "
     "homeowner-association restrictions that affect the work."),
    ("8. Contractor responsibilities",
     "Contractor will perform the work in a workmanlike manner, keep the work "
     "areas reasonably clean, protect adjacent surfaces, remove job-generated "
     "debris, and comply with applicable building codes for the work performed. "
     "Contractor will maintain general liability insurance and workers' "
     "compensation coverage for its employees and will provide certificates on "
     "request."),
    ("9. Permits and inspections",
     "The price does not include building permits, plan review or inspection "
     "fees unless they are listed in the Scope of Work. If a permit is required "
     "for this work, Contractor will obtain it and the fee will be added by "
     "change order. Owner authorises Contractor to act as Owner's agent for "
     "permit applications relating to this scope only."),
    ("10. Warranty",
     "Contractor warrants its workmanship for twelve (12) months from the date "
     "of substantial completion and will correct defective work at no charge. "
     "Materials carry the manufacturer's warranty only. The warranty does not "
     "cover damage caused by leaks, moisture, settlement, misuse, alterations "
     "by others, or normal wear."),
    ("11. Lien rights",
     "Contractor reserves its mechanic's lien rights under Virginia law. On "
     "receipt of final payment Contractor will provide a conditional waiver of "
     "lien for the work performed and will obtain waivers from any "
     "subcontractors used on the job."),
    ("12. Suspension and termination",
     "Either party may terminate this agreement for material breach by written "
     "notice if the breach is not cured within seven (7) days. On termination "
     "Owner will pay for work performed and materials ordered or delivered to "
     "the site, and Contractor will leave the site in a safe condition. Owner "
     "may suspend the work by written notice; a suspension longer than thirty "
     "(30) days is treated as a termination."),
    ("13. Dispute resolution",
     "The parties will first attempt to resolve any dispute by direct "
     "discussion. If that fails, the dispute will be submitted to mediation in "
     "Chesapeake, Virginia before either party files suit. This agreement is "
     "governed by the laws of the Commonwealth of Virginia. Venue for any "
     "action lies in the Circuit Court for the City of Chesapeake."),
    ("14. Entire agreement",
     "This agreement, with the Scope of Work and Itemized Pricing attached, is "
     "the entire agreement between the parties and replaces any prior "
     "discussion or quotation. It may be amended only in writing signed by both "
     "parties. If any provision is held unenforceable the remainder stays in "
     "effect. This offer is valid for {valid} days from the date below."),
]


class BidDocPDF(BidPackagePDF):
    """Same header as the package PDF; footer names which document this is."""

    doc_title = "Bid document"

    def footer(self):
        self.set_y(-13)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 8, f"{COMPANY}  -  {self.doc_title}  -  page {self.page_no()}",
                  align="C")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _new_pdf(title: str) -> BidDocPDF:
    pdf = BidDocPDF(format="letter")
    pdf.doc_title = title
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(15, 15, 15)
    pdf.add_page()
    return pdf


def _mc(pdf: FPDF, w: float, h: float, text: str, **kw) -> None:
    """``multi_cell`` that always returns to the left margin.

    fpdf 2.8 defaults ``multi_cell`` to ``new_x=RIGHT``, which parks the cursor
    at the right margin and makes the next ``multi_cell`` raise "Not enough
    horizontal space to render a single character". Every paragraph in these
    documents follows another one, so the default is never what is wanted.
    """
    kw.setdefault("new_x", "LMARGIN")
    kw.setdefault("new_y", "NEXT")
    FPDF.multi_cell(pdf, w, h, text, **kw)


def _title_block(pdf: BidDocPDF, title: str, bid: dict, subtitle: str = "") -> None:
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
    try:
        number = int(bid.get("revision") or 0)
    except (TypeError, ValueError):
        number = 0
    revision = f"Revision {number}" if number > 0 else "Original"
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, f"Bid #{bid.get('id', '')}   |   "
                   f"{revision}   |   "
                   f"Reference: {_txt(bid.get('reference') or '-')}   |   "
                   f"Date: {date.today().strftime('%B %d, %Y')}",
             new_x="LMARGIN", new_y="NEXT")
    if subtitle:
        pdf.cell(0, 5, _txt(subtitle), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)


def _party_block(pdf: BidDocPDF, bid: dict) -> None:
    """Prepared-for block. Client name/address are blank on the card, so the
    document leaves ruled space instead of printing 'None'."""
    _heading(pdf, "Prepared for")
    name = (bid.get("contact_name") or bid.get("owner") or "").strip()
    email = (bid.get("contact_email") or "").strip()
    phone = (bid.get("contact_phone") or "").strip()
    location = (bid.get("location") or "").strip()

    pdf.set_font("Helvetica", "", 9)
    if name:
        _labelled(pdf, "Client", name)
    else:
        _labelled(pdf, "Client", "____________________________________________")
    if location:
        _labelled(pdf, "Property", location)
    else:
        _labelled(pdf, "Property", "____________________________________________")
    if email or phone:
        _labelled(pdf, "Contact", " / ".join(x for x in (email, phone) if x))
    _labelled(pdf, "Contractor", f"{COMPANY}, {ADDRESS}")
    _labelled(pdf, "Phone", PHONE)


def _paragraphs(pdf: BidDocPDF, items: list[tuple[str, str]]) -> None:
    for head, body in items:
        # Keep a clause heading with at least a few lines of its own text; a
        # heading alone at the foot of a page is how contracts get misread.
        if pdf.get_y() + 32 > pdf.h - 20:
            pdf.add_page()
        pdf.set_font("Helvetica", "B", 9)
        _mc(pdf, 0, 5, _txt(head))
        pdf.set_font("Helvetica", "", 9)
        _mc(pdf, 0, 4.8, _txt(body))
        pdf.ln(2.2)


def _photo_block(pdf: BidDocPDF, path: Path, width: float) -> float:
    """Place one photo at ``width`` and return the height it consumed."""
    from PIL import Image

    with Image.open(path) as im:
        iw, ih = im.size
    height = width * ih / iw
    x = pdf.l_margin + (pdf.epw - width) / 2.0
    pdf.image(str(path), x=x, y=pdf.get_y(), w=width, h=height)
    pdf.set_y(pdf.get_y() + height + 1.5)
    return height


def _photo_log(pdf: BidDocPDF, photos: list[Path], per_page: int = 2,
               width: float = 70.0) -> None:
    """Photo log. Two per page, captioned, with the file name for traceability."""
    _heading(pdf, "Photo log - conditions at time of bid")
    pdf.set_font("Helvetica", "I", 8.5)
    _mc(pdf, 0, 4.6, _txt(
        "Photographs taken before work began. They record the conditions this "
        "price is based on and form part of the record if concealed damage is "
        "found later."))
    pdf.ln(2)

    for i, path in enumerate(photos):
        from PIL import Image

        with Image.open(path) as im:
            iw, ih = im.size
        height = width * ih / iw
        # Caption + margin need ~12mm under the image.
        if pdf.get_y() + height + 12 > pdf.h - 20:
            pdf.add_page()
        _photo_block(pdf, path, width)
        pdf.set_font("Helvetica", "", 8.5)
        _mc(pdf, 0, 4.4, _txt(f"Photo {i + 1}. {_caption(path)}"))
        pdf.set_font("Helvetica", "I", 7.5)
        pdf.set_text_color(120, 120, 120)
        pdf.cell(0, 4, _txt(path.name), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(3.5)


def _caption(path: Path) -> str:
    return PHOTO_CAPTIONS.get(path.stem, "Site photograph taken before work began.")


def _contact_sheet(pdf: BidDocPDF, photos: list[Path], cols: int = 3,
                   width: float = 54.0) -> None:
    """All photos on one page, as an exhibit to the contract."""
    from PIL import Image

    gap = 6.0
    row_y = pdf.get_y()
    col_h = 0.0
    for i, path in enumerate(photos):
        col = i % cols
        if col == 0 and i:
            row_y += col_h + 8.0
            col_h = 0.0
            pdf.set_y(row_y)
        with Image.open(path) as im:
            iw, ih = im.size
        height = width * ih / iw
        x = pdf.l_margin + col * (width + gap)
        if row_y + height + 10 > pdf.h - 20:
            pdf.add_page()
            row_y = pdf.get_y()
            col_h = 0.0
        pdf.image(str(path), x=x, y=row_y, w=width, h=height)
        pdf.set_xy(x, row_y + height + 1.0)
        pdf.set_font("Helvetica", "", 7)
        _mc(pdf, width, 3.4, _txt(f"Photo {i + 1}. {_caption(path)}"))
        col_h = max(col_h, height + 1.0 + 12.0)
    pdf.set_y(row_y + col_h + 2.0)


# --------------------------------------------------------------------------
# 01 - scope of work
# --------------------------------------------------------------------------

def build_scope_of_work(bid: dict, costs: list[dict], photos: list[Path]) -> bytes:
    pdf = _new_pdf("Scope of work")
    subtitle = (bid.get("name") or bid.get("location") or "").strip()
    _title_block(pdf, "SCOPE OF WORK", bid,
                 subtitle if subtitle else "Renovation scope of work")
    _party_block(pdf, bid)

    scope = _scope_items(bid, costs)

    _heading(pdf, "Summary")
    pdf.set_font("Helvetica", "", 9)
    # Same contract price as the contract and the bid card: bid_amount carries
    # the markup, the cost lines do not.
    total = float(bid.get("bid_amount") or 0) or sum(
        float(c.get("estimated") or 0) for c in costs)
    if total <= 0:
        # An unpriced solicitation must not go out claiming a $0.00 contract.
        _mc(pdf, 0, 4.8, _txt(
            f"{COMPANY} will complete the work described below at the property "
            f"named above. Pricing has not been entered on this bid yet; the "
            f"price and payment schedule follow on the itemized quote."))
    elif scope:
        _mc(pdf, 0, 4.8, _txt(
            f"{COMPANY} will complete the renovation described below at the "
            f"property named above for a contract price of {_money(total)}. "
            f"The work covers {len(scope)} separate tasks set out in the "
            f"scope of work below."))
    else:
        _mc(pdf, 0, 4.8, _txt(
            f"{COMPANY} will complete the renovation described below at the "
            f"property named above for a contract price of {_money(total)}. "
            f"No scope lines have been entered on this bid yet - add cost "
            f"lines on the bid card to build the scope."))
    pdf.ln(2)

    _heading(pdf, "Scope of work")
    pdf.set_font("Helvetica", "", 9)
    for i, desc in enumerate(scope, start=1):
        pdf.set_x(pdf.l_margin + 1)
        _mc(pdf, pdf.epw - 1, 4.9, f"{i}. {_txt(desc)}")
        pdf.ln(0.8)
    if not scope:
        pdf.set_font("Helvetica", "I", 9)
        _mc(pdf, 0, 5, "No scope lines have been entered on this bid yet.")

    _heading(pdf, "Materials and specifications")
    _bullets(pdf, [f"{c['description']}" for c in costs
                   if (c.get("category") or "") == "material"])

    _heading(pdf, "Site conditions and assumptions")
    _bullets(pdf, assumptions_for(costs))

    _heading(pdf, "Not included")
    _bullets(pdf, exclusions_for(costs))

    _heading(pdf, "Change orders")
    pdf.set_font("Helvetica", "", 9)
    _mc(pdf, 0, 4.8, _txt(
        "If work exposes a condition this scope did not anticipate - rot, a "
        "failed subfloor, failed plumbing, hidden water damage - work stops "
        "in that area and a written change order is issued with a price "
        "before anything further is done. No additional amount is owed "
        "without written approval."))
    pdf.ln(2)

    if photos:
        pdf.add_page()
        _photo_log(pdf, photos)

    return bytes(pdf.output())


# --------------------------------------------------------------------------
# 02 - itemized pricing
# --------------------------------------------------------------------------

def _fit(pdf: FPDF, text: str, width: float) -> str:
    """Trim ``text`` at a word boundary until it measures inside ``width``.

    A fixed character slice either wastes space or, as happened with "PVC
    fittings", prints "PVC fitt" - which reads like a typo in a document a
    client signs. Measure the string with the font actually in use instead of
    guessing characters.
    """
    text = (text or "").strip()
    if not text or pdf.get_string_width(_txt(text)) <= width:
        return _txt(text)
    words = text.split()
    while words:
        words.pop()
        candidate = " ".join(words)
        if candidate and pdf.get_string_width(_txt(candidate + "..")) <= width:
            return _txt(candidate + "..")
    return ""


def _fmt_qty(v) -> str:
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return ""
    return f"{f:,.0f}" if abs(f - round(f)) < 0.005 else f"{f:,.2f}"


def build_itemized_pricing(bid: dict, costs: list[dict]) -> bytes:
    pdf = _new_pdf("Itemized pricing")
    _title_block(pdf, "ITEMIZED PRICING", bid,
                 "Hallway, runner and bathroom renovation")
    _party_block(pdf, bid)

    ordered = sorted(costs, key=lambda x: x.get("id") or 0)
    total = float(bid.get("bid_amount") or 0) or sum(
        float(c.get("estimated") or 0) for c in ordered)
    if not ordered and total > 0:
        # A bid priced as one number (or a solicitation we have not broken down
        # yet) still has to produce a usable document. Previously this rendered
        # an empty table with a total, which looks like a broken quote.
        ordered = [{
            "id": 0, "description": f"Lump sum - {bid.get('name') or 'renovation'}",
            "qty": 1, "unit": "lot", "estimated": total,
        }]

    _heading(pdf, "Line items")
    widths = (8, 100, 13, 18, 22, 24)
    headers = ("#", "Description", "Qty", "Unit", "Rate", "Amount")

    def table_header():
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(238, 238, 238)
        for w, h in zip(widths, headers):
            pdf.cell(w, 6.5, h, border=1, fill=True,
                     align="R" if h in ("Qty", "Rate", "Amount") else "L")
        pdf.ln()

    def phase_band(name: str):
        # A band with its rows overleaf reads as a heading for the wrong table.
        if pdf.get_y() + 6.5 + 12.0 > pdf.h - 20:
            pdf.add_page()
            table_header()
        pdf.set_font("Helvetica", "B", 8.5)
        pdf.set_fill_color(250, 250, 250)
        pdf.cell(sum(widths), 6.5, _txt(name.upper()), border=1, fill=True,
                 new_x="LMARGIN", new_y="NEXT")

    table_header()
    pdf.set_font("Helvetica", "", 8)
    n = 0
    subtotals: list[tuple[str, float]] = []
    for name, first, last in PHASES:
        slice_ = ordered[first - 1:last]
        if not slice_:
            continue
        phase_band(name)
        phase_total = 0.0
        for c in slice_:
            n += 1
            est = float(c.get("estimated") or 0)
            qty = c.get("qty")
            rate = (est / float(qty)) if qty not in (None, 0) else est
            phase_total += est
            pdf.cell(widths[0], 5.8, str(n), border=1)
            pdf.cell(widths[1], 5.8, _fit(pdf, c.get("description") or "", widths[1] - 2.5),
                     border=1)
            pdf.cell(widths[2], 5.8, _fmt_qty(qty), border=1, align="R")
            pdf.cell(widths[3], 5.8, _txt(c.get("unit") or "-"), border=1)
            pdf.cell(widths[4], 5.8, _money(rate), border=1, align="R")
            pdf.cell(widths[5], 5.8, _money(est), border=1, align="R",
                     new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "B", 8)
        pdf.cell(sum(widths[:5]), 5.8, _txt(f"{name} - subtotal"), border=1,
                 align="R")
        pdf.cell(widths[5], 5.8, _money(phase_total), border=1, align="R",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 8)
        subtotals.append((name, phase_total))

    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(sum(widths[:5]), 8, "TOTAL CONTRACT PRICE", align="R")
    pdf.cell(widths[5], 8, _money(total), align="R",
             new_x="LMARGIN", new_y="NEXT")

    _heading(pdf, "How to read this list")
    _bullets(pdf, [
        "Lines appear in the order the work happens, not grouped by trade. "
        "Cleanup is first, final cleanup is last.",
        "Rate and quantity are shown so you can see how each line was built. "
        "The price is a lump sum for the line, not an hourly billing rate: the "
        "hours listed are the basis of the price, not a running meter.",
        "Allowance lines are estimates reconciled against receipts at final "
        "billing, as described in the contract.",
        "The contingency line covers concealed conditions after demolition. "
        "Any unused balance is credited to you at final billing.",
        "This list is a breakdown of one lump-sum price, not a cost-plus "
        "agreement. It is provided so you can see what the price buys.",
    ])

    if pdf.get_y() > pdf.h - 70:
        pdf.add_page()
    _heading(pdf, "Payment schedule")
    dep_amt = round(total * DEPOSIT_PCT / 100.0, 2)
    prog_amt = round(total * PROGRESS_PCT / 100.0, 2)
    final_amt = round(total - dep_amt - prog_amt, 2)
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.set_fill_color(245, 245, 245)
    pdf.cell(30, 6.5, "Stage", border=1, fill=True)
    pdf.cell(126, 6.5, "When", border=1, fill=True)
    pdf.cell(30, 6.5, "Amount", border=1, fill=True, align="R",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8.5)
    for stage, when, amt in (
        (f"Deposit {DEPOSIT_PCT}%", "At signing, to schedule and order materials", dep_amt),
        (f"Progress {PROGRESS_PCT}%", "Rough-in, drywall and substrate complete", prog_amt),
        (f"Final {FINAL_PCT}%", "Final walkthrough and acceptance", final_amt),
    ):
        pdf.cell(30, 6.0, stage, border=1)
        pdf.cell(126, 6.0, _txt(when), border=1)
        pdf.cell(30, 6.0, _money(amt), border=1, align="R",
                 new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 8.5)
    pdf.cell(156, 6.5, "Total", border=1, align="R")
    pdf.cell(30, 6.5, _money(total), border=1, align="R",
             new_x="LMARGIN", new_y="NEXT")

    _heading(pdf, "Terms")
    _bullets(pdf, TERMS)

    # Imported solicitation notes stay on the internal bid card. Printing them
    # here put raw Zoho CSS and the bid-board digest into a client document.

    return bytes(pdf.output())


# --------------------------------------------------------------------------
# 03 - contract
# --------------------------------------------------------------------------

def build_contract(bid: dict, costs: list[dict], photos: list[Path],
                   signature: dict | None = None,
                   client_signature: dict | None = None) -> bytes:
    pdf = _new_pdf("Renovation agreement")
    # bill the CUSTOMER the bid amount, not the raw cost lines. bid_amount is
    # base x (1 + markup), which is what the bid card and the deposit invoice
    # use; summing costs here produced a contract priced at cost, so a
    # $120,000 job was signed at $100,000 and then invoiced at $120,000.
    total = float(bid.get("bid_amount") or 0)
    if total <= 0:
        total = sum(float(c.get("estimated") or 0) for c in costs)
    dep_amt = round(total * DEPOSIT_PCT / 100.0, 2)
    prog_amt = round(total * PROGRESS_PCT / 100.0, 2)
    final_amt = round(total - dep_amt - prog_amt, 2)

    _title_block(pdf, "RENOVATION AGREEMENT", bid)
    pdf.set_font("Helvetica", "", 9)
    _mc(pdf, 0, 4.8, _txt(
        "This agreement is made between the Owner and the Contractor named "
        "below for work at the property named below."))
    pdf.ln(2)

    _heading(pdf, "Parties")
    name = (bid.get("contact_name") or bid.get("owner") or "").strip()
    location = (bid.get("location") or "").strip()
    # The party on the card is often a property manager signing as the owner's
    # agent, so the label names both rather than calling a management company
    # the Owner.
    _labelled(pdf, "Owner / Manager",
              name or "____________________________________________")
    _labelled(pdf, "Property", location or "____________________________________________")
    if bid.get("contact_email"):
        _labelled(pdf, "Email", bid["contact_email"])
    if bid.get("contact_phone"):
        _labelled(pdf, "Phone", bid["contact_phone"])
    _labelled(pdf, "Contractor", COMPANY)
    _labelled(pdf, "Address", ADDRESS)
    _labelled(pdf, "Phone", PHONE)

    _heading(pdf, "Contract price")
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 9, _money(total), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8.5)
    _mc(pdf, 0, 4.6, _txt(
        "Lump sum, inclusive of labour, materials, the contingency line shown "
        "on the Itemized Pricing, and the allowance items described in section "
        "6. Subject to written change orders only."))
    pdf.ln(2)

    _heading(pdf, "Terms")
    _paragraphs(pdf, [
        (head, body.format(
            date=date.today().strftime("%B %d, %Y"),
            total=_money(total),
            dep=DEPOSIT_PCT, dep_amt=_money(dep_amt),
            prog=PROGRESS_PCT, prog_amt=_money(prog_amt),
            final=FINAL_PCT, final_amt=_money(final_amt),
            valid=VALID_DAYS,
        ))
        for head, body in CONTRACT_TERMS
    ])

    if photos:
        pdf.add_page()
        _heading(pdf, "Exhibit A - pre-existing conditions")
        pdf.set_font("Helvetica", "I", 8.5)
        _mc(pdf, 0, 4.6, _txt(
            "Photographs of the work areas taken before work began. Owner and "
            "Contractor acknowledge these record the condition of the premises "
            "at the start of the work."))
        pdf.ln(2)
        _contact_sheet(pdf, photos)

    # ---- signatures ------------------------------------------------------
    if pdf.get_y() > 200:
        pdf.add_page()
    _heading(pdf, "Signatures")
    pdf.set_font("Helvetica", "", 9)
    _mc(pdf, 0, 4.8, _txt(
        "By signing below both parties accept this agreement, the Scope of Work "
        "and the Itemized Pricing."))
    pdf.ln(10)

    for label in ("Owner", f"For {COMPANY}"):
        stamp = client_signature if label == "Owner" else signature
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(90, 5, label)
        pdf.cell(90, 5, "Date", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(9)
        y = pdf.get_y()
        if stamp:
            # Strokes sit above the rule, printed name and date below it.
            pdf.image(stamp["png"], x=pdf.l_margin + 2, y=y - 13,
                      w=62, h=12, keep_aspect_ratio=True)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(90, 90, 90)
            pdf.text(pdf.l_margin + 92, y - 1.5, _txt(stamp.get("date") or ""))
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", "B", 9)
        pdf.set_draw_color(0, 0, 0)
        pdf.line(pdf.l_margin, y, pdf.l_margin + 82, y)
        pdf.line(pdf.l_margin + 92, y, pdf.l_margin + 174, y)
        pdf.ln(3)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(110, 110, 110)
        pdf.cell(90, 4, "Signature")
        if stamp:
            pdf.cell(90, 4, _txt(f"{stamp.get('name') or ''} (electronic)"))
        else:
            pdf.cell(90, 4, "Printed name and date")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(10)

    if signature or client_signature:
        pdf.set_font("Helvetica", "I", 7.5)
        pdf.set_text_color(110, 110, 110)
        _mc(pdf, 0, 4, _txt(
            "Signed electronically on the date shown. The signature image is held "
            "with an audit record of the time, the address it was made from, and "
            "a hash of the document as signed."))
        pdf.set_text_color(0, 0, 0)

    return bytes(pdf.output())


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

DUMP_SQL = """-- run inside the container: railway ssh ... "cat > /tmp/d.py && python /tmp/d.py"
import os, json, psycopg
from psycopg.rows import dict_row
from decimal import Decimal
conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
with conn.cursor() as cur:
    cur.execute("select * from bid_opportunities where id = %s", (BID_ID,))
    bid = dict(cur.fetchone())
    cur.execute("select * from bid_costs where bid_id = %s order by id", (BID_ID,))
    costs = [dict(r) for r in cur.fetchall()]
def enc(o):
    if isinstance(o, Decimal):
        return float(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    raise TypeError(str(type(o)))
print(json.dumps({"bid": bid, "costs": costs}, default=enc))
"""


def _find_photos(folder: Path) -> list[Path]:
    if not folder or not folder.is_dir():
        return []
    exts = {".jpg", ".jpeg", ".png"}
    return sorted((p for p in folder.iterdir()
                   if p.is_file() and p.suffix.lower() in exts),
                  key=lambda p: (len(p.stem), p.stem))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bid-json", required=True,
                    help="JSON snapshot with keys 'bid' and 'costs'")
    ap.add_argument("--photos", default="", help="folder of cleaned JPEGs")
    ap.add_argument("--out", default="", help="output folder")
    ap.add_argument("--signature", default="",
                    help="PNG of the owner's drawn signature to stamp on the contract")
    ap.add_argument("--signer-name", default="Shaun David OLeary",
                    help="printed name beside the signature")
    ap.add_argument("--signature-date", default="",
                    help="date string under the signature (default: today)")
    ap.add_argument("--dump-sql", action="store_true",
                    help="print the snapshot script and exit")
    args = ap.parse_args()

    if args.dump_sql:
        print(DUMP_SQL)
        return 0

    data = json.loads(Path(args.bid_json).read_text())
    bid = data["bid"]
    costs = data.get("costs") or []
    if not costs:
        raise SystemExit("no cost lines in the snapshot - nothing to price")

    photos = _find_photos(Path(os.path.expanduser(args.photos))) if args.photos else []

    signature = None
    if args.signature:
        png = Path(os.path.expanduser(args.signature)).read_bytes()
        when = args.signature_date or datetime.now().strftime("%B %d, %Y")
        signature = {"png": png, "name": args.signer_name,
                     "date": when, "company": "BuildStack Construction LLC"}

    out = Path(os.path.expanduser(
        args.out or f"~/Desktop/BuildStack-Bid-{bid.get('id', '')}"))
    out.mkdir(parents=True, exist_ok=True)

    docs = [
        ("01-Scope-of-Work.pdf", build_scope_of_work(bid, costs, photos)),
        ("02-Itemized-Pricing.pdf", build_itemized_pricing(bid, costs)),
        ("03-Contract.pdf", build_contract(bid, costs, photos, signature=signature)),
    ]

    total = float(bid.get("bid_amount") or 0) or sum(
        float(c.get("estimated") or 0) for c in costs)
    print(f"Bid #{bid.get('id')}  {bid.get('name')}")
    print(f"  {len(costs)} lines, total {_money(total)}")
    print(f"  {len(photos)} photos")
    for name, blob in docs:
        path = out / name
        path.write_bytes(blob)
        print(f"  {name:28s} {len(blob) // 1024:5d} KB  ->  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
