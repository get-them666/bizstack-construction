"""Ask the client for the deposit, as an invoice and an email.

A bid stops being paperwork the moment it is won: someone has to ask for the
deposit, the work has to become a project so crew hours land somewhere, and
those hours have to reach payroll. Most of that machinery already existed here --
``bid_costing.convert_bid_to_project``, ``ledger_bridge.accrue_payroll``,
``payroll_runs`` -- but none of it was reachable from a bid, so every bid ended
at the PDF and the job started again by hand somewhere else.

This module owns the deposit half of that chain. It raises a real
``bid_invoices`` row (so the money reaches the books through ``ledger_sync``)
and renders the document the property manager actually receives.

Two deliberate rules:

* **The amount is 30% of the bid, never a round number.** ``DEPOSIT_PCT`` is
  imported from ``bid_package_pdf`` so the request, the proposal and the
  contract cannot quote three different percentages.
* **Nothing is invented.** The PDF prints only what is on the bid and the
  invoice row. Payment instructions name the business line rather than bank
  details that are not stored anywhere in this app.
"""

from __future__ import annotations

from datetime import date, timedelta

from fpdf import FPDF

import bid_costing
from bid_package_pdf import (  # noqa: F401
    ADDRESS,
    COMPANY,
    DEPOSIT_PCT,
    EMAIL,
    PHONE,
    BidPackagePDF,
    _heading,
    _labelled,
    _money,
    _txt,
)

# Net 15, matching the invoice terms the bid package prints. The deposit is
# what schedules the work, so it is due sooner than a progress bill.
DEPOSIT_DAYS = 15


def _mc(pdf: FPDF, w: float, h: float, text: str, **kw) -> None:
    """``multi_cell`` that always returns to the left margin.

    fpdf 2.8 defaults ``multi_cell`` to ``new_x=RIGHT``, which parks the cursor
    at the right margin and makes the next call raise "Not enough horizontal
    space to render a single character".
    """
    kw.setdefault("new_x", "LMARGIN")
    kw.setdefault("new_y", "NEXT")
    FPDF.multi_cell(pdf, w, h, text, **kw)


# --- money -------------------------------------------------------------------
def deposit_amount(bid: dict) -> float:
    """The deposit on this bid, to the cent. 0.0 when the bid has no price.

    Rounded rather than truncated so the deposit plus the balance equals the bid
    exactly; a cent that goes missing here shows up as a reconciliation problem
    on the final invoice.
    """
    try:
        total = float((bid or {}).get("bid_amount") or 0)
    except (TypeError, ValueError):
        return 0.0
    if total <= 0:
        return 0.0
    return round(total * DEPOSIT_PCT / 100.0, 2)


def balance_after_deposit(bid: dict) -> float:
    try:
        total = float((bid or {}).get("bid_amount") or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, total - deposit_amount(bid)), 2)


def deposit_due_date(issued: date | None = None) -> date:
    return (issued or date.today()) + timedelta(days=DEPOSIT_DAYS)


# --- invoice -----------------------------------------------------------------
def find_deposit_invoice(conn, bid_id: int, company: str = "construction"):
    """The live deposit invoice for this bid, or ``None``.

    Void invoices are excluded: a deposit that was voided was not asked for, so
    the card must offer to ask again rather than report it as sent.
    """
    if not bid_costing._table_exists(conn, "bid_invoices"):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM bid_invoices WHERE bid_id = %s AND company = %s "
            "AND COALESCE(kind, 'invoice') = 'deposit' AND status <> 'void' "
            "ORDER BY id DESC LIMIT 1",
            (bid_id, company))
        return cur.fetchone()


def ensure_deposit_invoice(conn, bid: dict, company: str = "construction") -> dict:
    """Create the 30% deposit invoice, or hand back the one already raised.

    Idempotent on purpose. Pressing "send deposit request" twice must not put
    two deposits on the books: that mistake is invisible until the client pays
    one and gets chased for the other.

    A *draft* is re-priced to the current bid, because it was never sent. One
    already sent keeps the number the client was quoted, even if the bid has
    been re-priced since -- the client agreed to a figure, not to a formula.
    """
    bid_id = int(bid["id"])
    existing = find_deposit_invoice(conn, bid_id, company)
    if existing:
        amount = deposit_amount(bid)
        if (str(existing.get("status") or "") == "draft" and amount > 0
                and abs(float(existing.get("amount") or 0) - amount) >= 0.01):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE bid_invoices SET amount = %s, updated_at = now() "
                    "WHERE id = %s AND company = %s", (amount, existing["id"], company))
            conn.commit()
        return find_deposit_invoice(conn, bid_id, company) or existing

    amount = deposit_amount(bid)
    if amount <= 0:
        raise ValueError(
            "This bid has no price yet, so there is no deposit to ask for. "
            "Set the bid amount first.")
    bid_costing.create_invoice(
        conn, bid_id, amount, company,
        description=f"{DEPOSIT_PCT}% deposit to schedule work and order materials",
        due_date=deposit_due_date().isoformat(), kind="deposit")
    return find_deposit_invoice(conn, bid_id, company) or {}


# --- document ----------------------------------------------------------------
class DepositRequestPDF(BidPackagePDF):
    """Same letterhead as the bid package, different footer."""

    def footer(self):
        self.set_y(-13)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 8, f"{COMPANY}  -  Deposit request  -  page {self.page_no()}",
                  align="C")


def _party(pdf: DepositRequestPDF, bid: dict) -> None:
    name = (bid.get("contact_name") or bid.get("owner") or "").strip()
    if name:
        _labelled(pdf, "Client", name)
    if bid.get("location"):
        _labelled(pdf, "Property", bid["location"])
    contact = " / ".join(x for x in (bid.get("contact_email"), bid.get("contact_phone")) if x)
    if contact:
        _labelled(pdf, "Contact", contact)


def build_deposit_pdf(bid: dict, invoice: dict, pay_lines: list[str] | None = None) -> bytes:
    """Render the deposit request. Regenerated per send, never stored.

    ``pay_lines`` is ``deposit_instructions.payment_lines()``: the Zelle handle
    and the ACH routing/account the client should pay into. With none supplied
    the generic "reply and we will send details" paragraph is printed, because
    a PDF quoting bank details nobody configured would be wrong on paper.
    """
    bid = dict(bid or {})
    invoice = dict(invoice or {})
    amount = float(invoice.get("amount") or deposit_amount(bid) or 0)

    pdf = DepositRequestPDF(format="letter")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(15, 15, 15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, "DEPOSIT REQUEST", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90, 90, 90)
    if invoice.get("invoice_number"):
        pdf.cell(0, 4.6, f"Invoice {_txt(invoice['invoice_number'])}",
                 new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 4.6, f"Issued {_txt(invoice.get('issued_date') or date.today().isoformat())}"
                     f"   |   Due {_txt(invoice.get('due_date') or deposit_due_date().isoformat())}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)

    _heading(pdf, "Job")
    _labelled(pdf, "Project", bid.get("name") or "")
    _party(pdf, bid)

    _heading(pdf, "Amount due")
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(245, 245, 245)
    pdf.cell(125, 8, "Description", border=1, fill=True)
    pdf.cell(40, 8, "Amount", border=1, fill=True, align="R",
             new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 9)
    desc = (f"{DEPOSIT_PCT}% deposit on a {_money(bid.get('bid_amount'))} contract "
            f"to schedule work and order materials")
    # Measure the wrapped description instead of assuming one line: it is 74
    # characters in a 125mm column, so it always wraps, and a fixed rewind puts
    # the amount against the wrong line.
    row_h = pdf.multi_cell(125, 7, desc, dry_run=True, output="HEIGHT")
    y0 = pdf.get_y()
    pdf.multi_cell(125, 7, desc, border=1, new_x="LMARGIN", new_y="NEXT")
    pdf.set_xy(pdf.l_margin + 125, y0)
    pdf.cell(40, row_h, _money(amount), border=1, align="R",
             new_x="LMARGIN", new_y="NEXT")

    pdf.ln(1)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(125, 9, "TOTAL DUE NOW", align="R")
    pdf.cell(40, 9, _money(amount), align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(125, 5.5, "Balance after deposit", align="R")
    pdf.cell(40, 5.5, _money(balance_after_deposit(bid)), align="R",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)

    _heading(pdf, "Payment")
    pdf.set_font("Helvetica", "", 9)
    lines = list(pay_lines or [
        f"Payment by check, ACH or Zelle. Reply to this email or call {PHONE} "
        "to arrange it, and quote the invoice number above."])
    for line in lines:
        _mc(pdf, 0, 4.8, line)
    pdf.ln(1.5)
    _mc(pdf, 0, 4.8,
        "Work is scheduled and materials are ordered on receipt of this deposit. "
        "The remaining balance is invoiced on substantial completion.")

    _heading(pdf, "Payment schedule")
    pdf.set_font("Helvetica", "", 9)
    for label, pct in (("Deposit, on signing", DEPOSIT_PCT),
                       ("Progress, at rough-in complete", 40),
                       ("Final, on walkthrough", 100 - DEPOSIT_PCT - 40)):
        if pct <= 0:
            continue
        pdf.cell(125, 5, _txt(label))
        pdf.cell(40, 5, f"{pct}%", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(110, 110, 110)
    _mc(pdf, 0, 4.4,
        f"{COMPANY}  |  {ADDRESS}  |  {PHONE}  |  {EMAIL}")

    return bytes(pdf.output())


def build_deposit_email(bid: dict, invoice: dict, pay_lines: list[str] | None = None) -> tuple[str, str]:
    """Plain-text covering email. The PDF carries the detail."""
    amount = float((invoice or {}).get("amount") or deposit_amount(bid) or 0)
    number = (invoice or {}).get("invoice_number") or ""
    due = (invoice or {}).get("due_date") or deposit_due_date().isoformat()
    subject = f"Deposit request - {bid.get('name') or 'your project'}"
    if number:
        subject += f" ({number})"
    if pay_lines:
        payment = " ".join(pay_lines)
    else:
        payment = ("Payment can be made by check, ACH or Zelle. Reply to this email or "
                   f"call {PHONE} and we will send the details.")
    body = (
        f"Hello,\n\n"
        f"Thank you for awarding us {bid.get('name') or 'this project'}"
        f"{' at ' + bid['location'] if bid.get('location') else ''}.\n\n"
        f"The deposit to schedule the work and order materials is {_money(amount)}"
        f" ({DEPOSIT_PCT}% of the contract), due {due}.\n"
        f"The deposit request is attached as a PDF"
        f"{' and is invoice ' + number if number else ''}.\n\n"
        f"{payment}\n\n"
        f"Once the deposit is in, we will confirm the start date.\n\n"
        f"Thank you,\n"
        f"{COMPANY}\n"
        f"{ADDRESS}\n"
        f"{PHONE}\n"
    )
    return subject, body
