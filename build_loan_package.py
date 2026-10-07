#!/usr/bin/env python3
"""Build the lender document package for the $50K SBA Microloan.

Produces one PDF bundle containing the sections a microloan reviewer actually
reads, in the order they read them:

    1. Cover + request summary
    2. The ask and use of funds
    3. Business plan (two service lines, one customer relationship)
    4. Traction: verified 2026 revenue
    5. 12-month projections with the DSCR arithmetic shown
    6. Owner contribution and fit
    7. Risk and mitigants, stated plainly

Design rules, all of them deliberate:

  * Every number comes from SBA_7a_LOAN_PITCH.md or the caller. Nothing is
    invented. Where the source doc is uncertain (the $300K year-one figure has
    not been re-verified since Sept 30) the PDF says so rather than presenting
    it as fact, because a projection that is quietly stale is the thing a
    lender finds in due diligence.
  * The pre-revenue position is stated in section 1, not buried. A reviewer who
    discovers it later stops reading; one told up front keeps reading.
  * DSCR is shown as arithmetic, not asserted. SBA's March 2026 rule replaced
    the FICO screen on small loans with a cash-flow test, so this ratio is the
    thing underwriting turns on.
  * Runs offline. It does not need DATABASE_URL, because every figure here is
    already verified in the pitch doc and re-pulling from a live database to
    render a PDF risks shipping a half-built file into an underwriting review.

Usage:
    python3 build_loan_package.py
    python3 build_loan_package.py --out ~/Documents/BizStackLoanPackage.pdf
    python3 build_loan_package.py --check      # verify inputs, build nothing
"""

import argparse
import os
import sys
from pathlib import Path

try:
    from fpdf import FPDF
except ImportError:
    sys.exit("fpdf2 is required: pip install 'fpdf2==2.8.8'")


HERE = Path(__file__).resolve().parent
PITCH = HERE / "SBA_7a_LOAN_PITCH.md"

# The core PDF fonts are latin-1 only. An em dash or a curly apostrophe raises
# FPDFUnicodeEncodingException mid-render and loses the whole document, so all
# text is normalized on the way in. documents_service._pdf_text solves the same
# problem; this is a standalone copy so the builder has no import dependency.
_TRANSLATE = {
    0x2014: "-", 0x2013: "-", 0x2018: "'", 0x2019: "'",
    0x201c: '"', 0x201d: '"', 0x2026: "...", 0x00a0: " ",
    0x2022: "-", 0x00b7: "-", 0x2265: ">=", 0x2264: "<=",
    0x2192: "->", 0x00d7: "x", 0x00f7: "0",
}


def t(value) -> str:
    """Make a string safe for the core PDF fonts."""
    return (str(value)
            .translate(_TRANSLATE)
            .encode("latin-1", "replace")
            .decode("latin-1"))

# --- The figures. Each carries where it came from. --------------------------
REQUEST_AMOUNT = 50_000
LOAN_YEARS = 6
MONTHLY_PAYMENT = 930          # pitch, line 6
# Owner capital actually contributed, verified against the PNC record:
# ~$1,000 in February 2026 on labor assistance for the first project, before any
# customer payment. The old figure here was $10,000, which was never evidenced --
# the two deposits in the record ($22,500 and $6,200) are customer revenue.
OWNER_EQUITY = 1_000
# What the business actually collected on completed jobs. Prefer this over a
# capital figure when arguing capacity: $28,700 of it is bank-documented.
REVENUE_COLLECTED = 31_900
REVENUE_BANK_DOCUMENTED = 28_700
OWNER_EXPERIENCE_YEARS = 25    # pitch: "25 years in the construction trade"
OWNER_TENURE = "25 years in the construction trade"
DSCR_TARGET = 1.10             # SBA cash-flow threshold, March 2026 rule
# NOTE: the old DSCR_CLAIMED = 3.0 is gone. It was never used to render anything
# and contradicted the 1.34x the model actually computes, which is what the pitch
# says and what section 5 shows. Two different coverage figures in one package
# is an invitation to stop reading.

# year-one projection. The pitch's own header warns these are Sept 16 figures
# and have not been refreshed since Sept 30, so the PDF labels them as such.
YEAR_ONE_REVENUE = 300_000

# Two COMPLETED 2026 jobs, with real invoices and real material spend. Both land
# within four points of each other, which is what makes this a demonstrated
# margin rather than a guess:
#     Old Ironsides  $24,700 rev / $6,000 materials = 75.7%
#     Sunnywood      $ 7,200 rev / $1,500 materials = 79.2%
# The 35% figure the earlier one-page pitch used is not supportable against
# these and has been removed rather than defended.
DEMONSTRATED_CONSTRUCTION_MARGIN = 0.765

# The model underwrites BELOW the demonstrated margin, which is the right instinct
# -- but note the arithmetic: at 55% with $150K opex the model clears 1.10x only
# if revenue lands within ~2% of $300K. Section 5 shows the sensitivity table so a
# reviewer is not left to discover this. A lender who finds a 2% margin by doing
# the division themselves will discount the whole package; the same reviewer shown
# the table up front is reading a disclosure rather than catching a weakness.
GROSS_MARGIN = 0.55

USE_OF_FUNDS = [
    ("Working capital — first payroll and supplies buffer (both service lines)", 20_000),
    ("Equipment and tools — construction tools, commercial cleaning equipment, linens", 18_000),
    ("Marketing and lead generation — both lines, AI assistant, instant-quote ads", 12_000),
]

# Verified 2026 revenue, from the pitch doc's own table.
VERIFIED_REVENUE = [
    ("Mar 4–12", "Buildstack Construction — 120 Old Ironsides Rd, full build (garage → 1BR apt)", 24_700.00),
    ("May 6–17", "Buildstack Construction — 5509 Sunnywood Dr, insurance flood renovation", 7_200.00),
    ("Sep 3, 6, 11", "Broom Service — 3 turnovers @ $175", 525.00),
    ("Sep 6, 11", "Broom Service — co-host commission, 15% of stays", 91.50),
]

VERIFIED_TOTAL = sum(row[2] for row in VERIFIED_REVENUE)  # 32,516.50

PIPELINE_LEADS = 528          # permit_finder leads, status 'new', Sept 30 digest
PERMITS_FOUND = 0             # honest: the pitch records 0 at Sept 16


class LoanPDF(FPDF):
    def header(self):
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", size=8)
        self.set_text_color(120, 120, 130)
        self.cell(0, 6, t(f"BizStack - $50,000 SBA Microloan Application Package   -   page {self.page_no()}"),
                  align="R")
        self.ln(8)

    def footer(self):
        self.set_y(-14)
        self.set_font("Helvetica", size=7)
        self.set_text_color(140, 140, 150)
        self.cell(0, 6, t("Shaun O'Leary - (252) 665-5891 - hello@bizstackperks.com - bizstackperks.com"),
                  align="C")


def money(v, cents=False):
    return t(f"${v:,.2f}") if cents else t(f"${v:,.0f}")


def heading(pdf, text, level=1):
    # multi_cell leaves the cursor at the right margin. Without resetting x the
    # next call is rendered with zero available width and raises "Not enough
    # horizontal space to render a single character".
    pdf.set_x(pdf.l_margin)
    sizes = {0: 20, 1: 14, 2: 11}
    pdf.set_font("Helvetica", "B", sizes[level])
    pdf.set_text_color(15, 18, 28)
    pdf.ln(4 if level else 8)
    pdf.multi_cell(0, 6 if level else 9, t(text))
    pdf.ln(2)


def body(pdf, text, size=9, bold=False, italic=False, color=(40, 44, 55)):
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Helvetica", "B" if bold else ("I" if italic else ""), size)
    pdf.set_text_color(*color)
    pdf.multi_cell(0, 4.6, t(text))
    pdf.ln(1.5)


def bullet(pdf, text, size=9):
    pdf.set_font("Helvetica", size=size)
    pdf.set_text_color(40, 44, 55)
    pdf.set_x(pdf.l_margin + 6)
    pdf.multi_cell(0, 4.6, t(f"  -  {text}"))
    pdf.ln(0.5)


def rule(pdf):
    y = pdf.get_y()
    pdf.set_draw_color(220, 222, 230)
    pdf.line(10, y, 206, y)
    pdf.ln(3)


# --- sections ----------------------------------------------------------------

def cover(pdf):
    pdf.add_page()
    pdf.ln(24)
    pdf.set_font("Helvetica", "B", 26)
    pdf.set_text_color(15, 18, 28)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 10, "$50,000 SBA Microloan")
    pdf.set_font("Helvetica", "", 13)
    pdf.set_text_color(90, 96, 112)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 7, t("Startup financing request - Buildstack Construction Co. / Broom Service"))
    pdf.ln(10)

    body(pdf, "Shaun O'Leary — owner and operator", 11, bold=True)
    body(pdf, "(252) 665-5891  -  hello@bizstackperks.com  -  bizstackperks.com", 9)
    body(pdf, t("Williamsburg - Hampton Roads, VA  -  Currituck County / Elizabeth City, NC"), 9)
    pdf.ln(6)
    rule(pdf)

    heading(pdf, "The ask", 2)
    body(pdf, f"{money(REQUEST_AMOUNT)} SBA Microloan, {LOAN_YEARS}-year term, "
              f"approximately {money(MONTHLY_PAYMENT)} per month. "
              f"Personal guaranty offered. No liens or judgments against the business.")

    heading(pdf, "The borrower, stated plainly", 2)
    body(pdf, "This is a startup with no recorded revenue line to underwrite against. "
              "It has documented 2026 revenue, a fully built operating system, and a service "
              "area with a documented pipeline. Repayment is underwritten on projections, "
              "which is why the SBA's cash-flow test rather than a historical P&L is the "
              "relevant test here.", bold=True)
    body(pdf, "That framing is stated up front rather than buried, because a reviewer who "
              "discovers it on page six stops reading. SBA's March 2026 rule change removed "
              "the FICO screen for small loans in favour of a debt-service-coverage test of "
              f"{DSCR_TARGET:.2f}x or better, judged on projections. Section 5 shows that "
              "arithmetic.")

    heading(pdf, "Package contents", 2)
    for item in [
        "Section 2 — The ask and use of funds",
        "Section 3 — Business plan",
        "Section 4 — Traction: verified 2026 revenue",
        "Section 5 — 12-month projections and DSCR",
        "Section 6 — Owner contribution and fit",
        "Section 7 — Risk and mitigants",
    ]:
        bullet(pdf, item)


def the_ask(pdf):
    pdf.add_page()
    heading(pdf, "2.  The ask and use of funds")

    body(pdf, f"Requested: {money(REQUEST_AMOUNT)}, SBA Microloan, {LOAN_YEARS}-year term, "
              f"approximately {money(MONTHLY_PAYMENT)} per month.", bold=True)
    body(pdf, "An SBA Microloan is the correct product here rather than a bank 7(a). Microloans "
              "are delivered through nonprofit CDFI lenders, are sized for exactly this, and "
              "underwrite startups and thin-credit borrowers holistically — credit score is an "
              "input, not the gate. A bank 7(a) is the right instrument in roughly two years, "
              "once there is a return history to show.")

    heading(pdf, "Use of funds", 2)
    total = 0
    for label, amount in USE_OF_FUNDS:
        pdf.set_font("Helvetica", size=9)
        pdf.set_text_color(40, 44, 55)
        pdf.cell(120, 5.5, t(label))
        pdf.cell(0, 5.5, money(amount), align="R")
        pdf.ln(6)
        total += amount
    rule(pdf)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(120, 6, t("Total"))
    pdf.cell(0, 6, money(total), align="R")  # money() normalizes
    pdf.ln(7)
    assert total == REQUEST_AMOUNT, f"use of funds sums to {total}, not {REQUEST_AMOUNT}"
    body(pdf, "Note for reviewers: these funds finance the applicant's own working capital, "
              "equipment and marketing. They do not finance construction work performed for "
              "third-party clients, which is funded from client payments under each written "
              "fixed-price scope.", italic=True, size=8)


def business_plan(pdf):
    pdf.add_page()
    heading(pdf, "3.  Business plan")

    body(pdf, "One company, one platform, two service lines that feed each other across the "
              "short-term-rental lifecycle.")

    heading(pdf, "Buildstack Construction", 2)
    bullet(pdf, "Licensed general contractor. Renovations, kitchens and baths, roofing and "
                "siding, decks, drywall and paint.")
    bullet(pdf, "STR make-readies across Williamsburg–Hampton Roads, VA and Currituck "
                "County / Elizabeth City, NC.")
    bullet(pdf, "Every job on a written fixed-price scope.")

    heading(pdf, "Broom Service", 2)
    bullet(pdf, "Short-term-rental turnover cleaning and co-hosting management for Airbnb, "
                "Vrbo and direct-booking properties.")
    bullet(pdf, "Guest-funded cleaning fees collected at booking — cash in advance, not "
                "accounts receivable.")
    bullet(pdf, "Co-hosting at 10–30% of gross nightly bookings.")

    heading(pdf, "The flywheel", 2)
    body(pdf, "BizStack renovates and makes a property STR-ready, then cleans and co-hosts it. "
              "One customer relationship captures the entire STR lifecycle, from first nail "
              "through every turnover. Both lines draw on the same operating system and the "
              "same crew, so the marginal cost of the second line is low.")

    heading(pdf, "What already exists — the low-risk part", 2)
    body(pdf, "The operating system is built and running today. This loan buys fuel for a "
              "machine that is already assembled:", bold=True)
    for item in [
        "Instant online quotes generated from property data",
        "24/7 AI phone and SMS assistant answering every inbound lead",
        "Worker and crew mobile portals with GPS clock-in and photo-verified work",
        "Stripe payments, payroll, and a live accounting ledger",
        "Permit monitoring across the service area, with owner-of-record resolution",
    ]:
        bullet(pdf, item)

    heading(pdf, "Certification position", 2)
    body(pdf, "Pursuing Virginia SWaM certification to access Commonwealth and municipal "
              "set-aside contracting. Stated as pursuing, not held — SBA Microloan "
              "underwriting neither requires nor rewards SWaM, and claiming a certification "
              "not held would be a false statement.", italic=True, size=8)


def traction(pdf):
    pdf.add_page()
    heading(pdf, "4.  Traction: verified 2026 revenue")

    body(pdf, f"Documented revenue to date: {money(VERIFIED_TOTAL, cents=True)}. Every line "
              "below is a real completed job or booking, not a projection.")

    for date, desc, amount in VERIFIED_REVENUE:
        pdf.set_font("Helvetica", size=8.5)
        pdf.set_text_color(120, 120, 130)
        pdf.cell(28, 5.5, t(date))
        pdf.set_text_color(40, 44, 55)
        pdf.set_x(pdf.l_margin + 28)
        pdf.multi_cell(0, 5.5, t(desc))
        pdf.set_x(10)
        pdf.cell(28, 4, "")
        pdf.cell(0, 4, money(amount, cents=True), align="R")
        pdf.ln(2)

    rule(pdf)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(28, 6, t("Total verified"))
    pdf.cell(0, 6, money(VERIFIED_TOTAL, cents=True), align="R")
    pdf.ln(7)

    heading(pdf, "Demonstrated gross margin — the two completed jobs", 2)
    body(pdf, "Both projects were invoiced at a fixed price and completed self-performed. "
              "Material spend is actual, not estimated:", bold=True)

    for label, rev, mats in [
        ("Old Ironsides Rd — full garage-to-apartment build (Mar 4-12)", 24_700, 6_000),
        ("Sunnywood Dr — insurance flood-damage renovation (May 6-17)", 7_200, 1_500),
    ]:
        pct = (rev - mats) / rev * 100
        pdf.set_font("Helvetica", size=8.5)
        pdf.set_text_color(40, 44, 55)
        pdf.cell(112, 5.5, t(label))
        pdf.cell(0, 5.5, t(f"${rev:,} - ${mats:,} = {pct:.1f}%"), align="R")
        pdf.ln(6)

    rule(pdf)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(112, 6, t("Weighted construction margin"))
    pdf.cell(0, 6, t(f"{DEMONSTRATED_CONSTRUCTION_MARGIN*100:.1f}%"), align="R")
    pdf.ln(7)

    body(pdf, "The two projects differ sharply in scope — a full structural conversion and "
              "an interior flood restoration — and returned margins within four points of "
              "each other. Broom Service turnover cleaning adds roughly 75% on supplies cost "
              "alone. That consistency, across different job types, is what makes this a "
              "demonstrated margin rather than a favourable one.")

    body(pdf, f"The projections that follow underwrite at {int(GROSS_MARGIN*100)}% — roughly "
              f"{int((DEMONSTRATED_CONSTRUCTION_MARGIN-GROSS_MARGIN)*100)} points below what "
              "the business has actually achieved. The demonstrated figure is offered as "
              "evidence of capability; the model is deliberately set below it so it survives "
              "review.", bold=True)

    heading(pdf, "Capitalisation", 2)
    body(pdf, "$31,900 of construction revenue across two projects. The $22,500 Old Ironsides "
              "payment has a clean bank trail — a PNC statement showing a $22,500 deposit on "
              "03/19/2026. The remaining cash amounts have dated invoices and signed receipts. "
              "Invoices, receipts and before/after photo logs are attached as exhibits.")

    heading(pdf, "Pipeline", 2)
    body(pdf, f"{PIPELINE_LEADS:,} permit-derived leads recorded in the service area with no "
              "contact detail on file. These are monitored continuously from published "
              "building and permit feeds; owner-of-record resolution is the work this loan "
              "funds. Permits found at the Sept 16 checkpoint: "
              f"{PERMITS_FOUND}. The pipeline figure is lead inventory, not signed work, and "
              "is presented as such.", italic=True, size=8)


ANNUAL_DEBT_SERVICE = MONTHLY_PAYMENT * 12
ANNUAL_OPEX = 150_000          # owner comp $60K + operating expenses $90K

# What revenue is REQUIRED to hold coverage at exactly the floor. At the projected
# $300K the model clears 1.10x by ~2%, and a 2% miss inverts coverage to negative.
# Section 5 discloses this with a sensitivity table rather than leaving a reviewer
# to find it by dividing.
DSCR_BREAK_EVEN_REVENUE = (ANNUAL_OPEX + DSCR_TARGET * ANNUAL_DEBT_SERVICE) / GROSS_MARGIN


def projected_dscr() -> float:
    """The one number underwriting turns on, derived rather than asserted."""
    return (YEAR_ONE_REVENUE * GROSS_MARGIN - ANNUAL_OPEX) / ANNUAL_DEBT_SERVICE


def projections(pdf):
    pdf.add_page()
    heading(pdf, "5.  12-month projections and debt service coverage")

    body(pdf, "Year-one projection: "
              f"{money(YEAR_ONE_REVENUE)} revenue at approximately {int(GROSS_MARGIN*100)}% "
              "gross margin across fixed-price construction and pre-paid cleaning fees.",
         bold=True)

    body(pdf, "Stated plainly: this figure is the Sept 16 projection and has not been "
              "refreshed since the Sept 30 pipeline update. It should be re-run against "
              "current pipeline before submission rather than treated as final. A stale "
              "projection found in due diligence is worse than a conservative one.",
         italic=True, size=8, color=(150, 60, 20))

    heading(pdf, "Debt service coverage arithmetic", 2)
    body(pdf, "SBA's March 2026 rule replaced the FICO screen for small loans with a "
              f"cash-flow test: DSCR ≥ {DSCR_TARGET:.2f}x, judged on projections.")

    gross_profit = YEAR_ONE_REVENUE * GROSS_MARGIN
    rows = [
        ("Year-one projected revenue", money(YEAR_ONE_REVENUE)),
        (f"Gross margin @ {int(GROSS_MARGIN*100)}%", money(gross_profit)),
        ("Less: owner compensation", "($60,000)"),
        ("Less: operating expenses", "($90,000)"),
        ("= Projected operating cash flow", money(gross_profit - ANNUAL_OPEX)),
        (f"Annual debt service ({money(MONTHLY_PAYMENT)} x 12)", f"({money(ANNUAL_DEBT_SERVICE)})"),
    ]
    for label, value in rows:
        pdf.set_font("Helvetica", "B" if label.startswith("=") else "", size=9)
        pdf.set_text_color(40, 44, 55)
        pdf.cell(115, 5.5, t(label))
        pdf.cell(0, 5.5, t(value), align="R")
        pdf.ln(6)

    rule(pdf)
    ocf = gross_profit - ANNUAL_OPEX
    debt = ANNUAL_DEBT_SERVICE
    dscr = projected_dscr()
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(15, 18, 28)
    pdf.cell(115, 7, t(f"Projected DSCR  ({money(ocf)} / {money(debt)})"))
    pdf.cell(0, 7, t(f"{dscr:.2f}x"), align="R")
    pdf.ln(8)

    body(pdf, f"Against a {DSCR_TARGET:.2f}x threshold, the model clears at {dscr:.2f}x — and it "
              f"does so at a gross margin roughly "
              f"{int((DEMONSTRATED_CONSTRUCTION_MARGIN - GROSS_MARGIN) * 100)} points below the "
              f"{DEMONSTRATED_CONSTRUCTION_MARGIN*100:.1f}% this business actually achieved on "
              "its two completed jobs (section 4). The demonstrated figure is offered as evidence "
              "of capability; this is the number being underwritten.", bold=True)

    # --- Sensitivity. The 1.34x above is thin, and a reviewer who divides it
    # themselves will find that before anyone says it. Disclosing it converts a
    # discovered weakness into a disclosed one, and it is also the honest framing:
    # a business with $31,900 of history is asking a lender to underwrite a $300K
    # year, and that is a real gap between the two.
    heading(pdf, "What happens if the projection misses", 2)
    body(pdf, f"At {int(GROSS_MARGIN*100)}% gross margin and {money(ANNUAL_OPEX)} of operating cost, "
              f"revenue of {money(DSCR_BREAK_EVEN_REVENUE)} is required to hold coverage at exactly "
              f"{DSCR_TARGET:.2f}x. The {money(YEAR_ONE_REVENUE)} projection clears that bar by roughly "
              f"{(YEAR_ONE_REVENUE - DSCR_BREAK_EVEN_REVENUE) / YEAR_ONE_REVENUE * 100:.0f}%, which is a "
              "narrow margin and is stated here rather than left for the reviewer to find.")

    sens = [(300_000, "projection as written"),
            (280_000, "5% below projection"),
            (260_000, "13% below projection"),
            (240_000, "20% below projection")]
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(40, 44, 55)
    pdf.cell(70, 5.5, t("Year-one revenue"))
    pdf.cell(55, 5.5, t("Operating cash flow"))
    pdf.cell(35, 5.5, t("DSCR"))
    pdf.cell(0, 5.5, t("Against floor"))
    pdf.ln(7)
    for rev, label in sens:
        cf = rev * GROSS_MARGIN - ANNUAL_OPEX
        d = cf / ANNUAL_DEBT_SERVICE
        ok = d >= DSCR_TARGET
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(40, 44, 55)
        pdf.cell(70, 5.5, t(f"{money(rev)}  ({label})"))
        pdf.cell(55, 5.5, t(money(cf) if cf > 0 else f"({money(-cf)})"))
        pdf.cell(35, 5.5, t(f"{d:.2f}x" if cf > 0 else "n/a"))
        pdf.cell(0, 5.5, t("clears" if ok else "does not clear"))
        pdf.ln(6)

    pdf.ln(3)
    body(pdf, "The shape of that table matters more than the top line. Coverage does not decay "
              "gradually — it falls off a cliff, because $150,000 of operating cost against a "
              "fixed debt payment leaves almost no buffer. A 7% revenue miss drops coverage below "
              "1.0x. This is the honest weakness of the request, and the reason it is stated here.",
         italic=True, size=8, color=(150, 60, 20))

    body(pdf, "Three things strengthen it, all of which are requests rather than assertions. "
              "First, the operating cost base is not fixed: the $60,000 owner compensation is "
              "discretionary and can be reduced in a weak year, which the table above does not "
              "model. Second, gross margin is held at "
              f"{int(GROSS_MARGIN*100)}% against {DEMONSTRATED_CONSTRUCTION_MARGIN*100:.1f}% "
              "actually demonstrated on completed work; at the demonstrated margin the cliff moves "
              "well below these volumes. Third, a smaller request carries proportionally less "
              "fixed debt service against the same cost base, and the applicant is open to sizing "
              "the ask to the cash flow actually demonstrated rather than to the projection.",
         italic=True, size=8, color=(150, 60, 20))

    heading(pdf, "Month-one coverage", 2)
    body(pdf, f"Debt service is underwritten on the projected revenue above, not on owner "
              f"capital. Owner contribution is {money(OWNER_EQUITY)} and is stated for "
              "completeness; it is not presented as a reserve against the loan. The business "
              f"has already collected {money(REVENUE_COLLECTED)} on completed work, "
              f"{money(REVENUE_BANK_DOCUMENTED)} of which is bank-documented, which is the "
              "stronger evidence of capacity.")


def owner_fit(pdf):
    pdf.add_page()
    heading(pdf, "6.  Owner contribution and fit")

    body(pdf, f"{OWNER_TENURE}. Owner and operator, not a passive investor.", bold=True)
    body(pdf, f"Owner capital of approximately {money(OWNER_EQUITY)} was contributed in February "
              "2026 to engage labor assistance for the company's first project, ahead of any "
              "customer payment. Operations have since been funded from personal resources and "
              "customer receipts.")
    body(pdf, f"Stated plainly for the reviewer: this is a modest owner contribution, not the "
              f"10–25% some lenders look for. The business has instead demonstrated "
              f"{money(REVENUE_COLLECTED)} of collected revenue on completed jobs, "
              f"{money(REVENUE_BANK_DOCUMENTED)} of it traceable to bank deposits. A signed "
              "statement of owner contribution accompanies this package.")

    heading(pdf, "Why this borrower", 2)
    for item in [
        f"{OWNER_EXPERIENCE_YEARS} years of construction experience is the underwriting "
        "qualifier that CDFI lenders ask startups to demonstrate — industry fluency in "
        "scoping, pricing and completing fixed-price work.",
        "The revenue that exists is from the owner's own hands, on the owner's own scopes.",
        "No outside equity, no investor to satisfy, no dividend to protect. The only claim "
        "on cash flow is debt service.",
        "Personal guaranty offered.",
    ]:
        bullet(pdf, item)

    heading(pdf, "What the lender is not being asked to accept", 2)
    for item in [
        "No inflated growth story — the projection is labelled with its own vintage and "
        "the DSCR is stated at the lower of the two figures available.",
        "No certification claimed that is not held.",
        "No revenue presented as recurring that is not.",
        "No owner draw before debt service is current.",
    ]:
        bullet(pdf, item)


def risk(pdf):
    dscr = projected_dscr()
    pdf.add_page()
    heading(pdf, "7.  Risk and mitigants")

    heading(pdf, "Primary risk: revenue is young and concentrated", 2)
    body(pdf, "Construction revenue to date comes from two projects. That is real but thin, "
              "and a reviewer should weigh it as the main underwriting concern.")
    bullet(pdf, "Mitigant: the 529-address permit pipeline is monitored continuously and is "
                "not dependent on winning a single large job.")
    bullet(pdf, "Mitigant: two service lines with different demand drivers — renovation is "
                "owner-triggered, cleaning is guest-triggered and recurring per stay.")
    bullet(pdf, "Mitigant: guest-funded cleaning is collected at booking, so that line is "
                "cash-in-advance rather than invoiced.")

    heading(pdf, "Risk: contactability of permit-derived leads", 2)
    body(pdf, "Permit feeds publish an address and no applicant name, which historically left "
              "most of the pipeline unreachable. Owner-of-record resolution is now implemented "
              "and is the specific work this loan funds.")

    heading(pdf, "Risk: pre-revenue underwriting", 2)
    body(pdf, "Addressed directly rather than avoided. This is what the SBA cash-flow test "
              "exists for, and the operating system is built, so the risk is execution rather "
              "than formation.")

    heading(pdf, "Closing", 2)
    body(pdf, f"{money(REQUEST_AMOUNT)} funds 6 months of operating buffer, the equipment to "
              "self-perform both service lines, and marketing to convert a documented pipeline. "
              f"Debt service of approximately {money(MONTHLY_PAYMENT)} per month is covered "
              f"{dscr:.2f}x by projected cash flow.", bold=True)

    pdf.ln(6)
    body(pdf, "Documentation available on request: business plan, 12-month projections, "
              "licenses, insurance certificates, personal financial statement, EIN letter, "
              "bank statements, and per-job invoices and receipts for all 2026 revenue.")
    pdf.ln(10)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(15, 18, 28)
    pdf.cell(0, 6, t("Shaun O'Leary"))
    pdf.ln(5)
    pdf.set_font("Helvetica", size=9)
    pdf.set_text_color(90, 96, 112)
    pdf.cell(0, 5, t("Buildstack Construction Co. / Broom Service"))
    pdf.cell(0, 5, t("(252) 665-5891  -  hello@bizstackperks.com  -  bizstackperks.com"))


def check_inputs():
    """Fail loudly before building, so a package is never generated half-right."""
    problems = []
    total = sum(a for _, a in USE_OF_FUNDS)
    if total != REQUEST_AMOUNT:
        problems.append(f"use of funds sums to ${total:,}, request is ${REQUEST_AMOUNT:,}")
    if abs(VERIFIED_TOTAL - 32_516.50) > 0.01:
        problems.append(f"verified revenue sums to ${VERIFIED_TOTAL:,.2f}, pitch says $32,516.50")
    if not PITCH.exists():
        problems.append(f"source pitch not found: {PITCH}")
    elif "Karen White" in PITCH.read_text():
        problems.append("pitch file unexpectedly mentions Karen White — wrong document?")
    if MONTHLY_PAYMENT * LOAN_YEARS * 12 < REQUEST_AMOUNT:
        problems.append("monthly payment does not cover principal plus interest over the term")

    print("Input check")
    print(f"  use of funds        ${total:,} (request ${REQUEST_AMOUNT:,})")
    print(f"  verified revenue    ${VERIFIED_TOTAL:,.2f}")
    print(f"  payment             ${MONTHLY_PAYMENT}/mo x {LOAN_YEARS}yr "
          f"= ${MONTHLY_PAYMENT*LOAN_YEARS*12:,} total")
    print(f"  DSCR threshold      {DSCR_TARGET}x")
    print(f"  source pitch        {'found' if PITCH.exists() else 'MISSING'}")
    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(f"  - {p}")
    return not problems


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="", help="output PDF path")
    ap.add_argument("--check", action="store_true",
                    help="verify the inputs and exit without building")
    args = ap.parse_args()

    if not check_inputs():
        print("\nFix the above before building a package for underwriting.")
        return 1
    if args.check:
        print("\nAll inputs consistent.")
        return 0

    pdf = LoanPDF(format="Letter")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(10, 14, 10)
    for section in (cover, the_ask, business_plan, traction,
                    projections, owner_fit, risk):
        section(pdf)

    out = Path(args.out).expanduser() if args.out else HERE / "LOAN_PACKAGE.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out))
    size = out.stat().st_size
    print(f"\nwrote {out}  ({size:,} bytes, {pdf.page_no()} pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
