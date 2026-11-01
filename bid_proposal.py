"""Bid proposal generation - price it, make a proposal, email the GC."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from fpdf import FPDF

from documents_service import send_email, smtp_config_from_env


class ProposalPDF(FPDF):
    def header(self):
        self.set_font('Helvetica', 'B', 16)
        self.cell(0, 10, 'BuildStack Construction LLC', ln=True, align='L')
        self.set_font('Helvetica', '', 9)
        self.cell(0, 5, '701 Dana Dr, Chesapeake, VA 23321 | 757-908-7121',
                  ln=True, align='L')
        self.ln(5)
        self.set_draw_color(200, 200, 200)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font('Helvetica', 'I', 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, 'BuildStack Construction LLC - Thank you for the opportunity', align='C')


def generate_proposal_pdf(bid: dict, costs: list, summary: dict, markup_pct: float = 0,
                          signature: dict | None = None) -> bytes:
    pdf = ProposalPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    
    # Proposal info
    pdf.set_font('Helvetica', 'B', 14)
    pdf.cell(0, 10, 'PROPOSAL', ln=True)
    pdf.ln(2)
    
    pdf.set_font('Helvetica', '', 10)
    pdf.cell(0, 6, f'Proposal Date: {datetime.now().strftime("%B %d, %Y")}', ln=True)
    pdf.cell(0, 6, f'Project: {bid.get("name", "")}', ln=True)
    if bid.get('location'):
        pdf.cell(0, 6, f'Location: {bid["location"]}', ln=True)
    if bid.get('owner'):
        pdf.cell(0, 6, f'GC/Owner: {bid["owner"]}', ln=True)
    if bid.get('due_date'):
        pdf.cell(0, 6, f'Bid Due: {bid["due_date"]}', ln=True)
    pdf.ln(8)
    
    # Cost breakdown
    pdf.set_font('Helvetica', 'B', 12)
    pdf.cell(0, 10, 'Cost Breakdown', ln=True)
    pdf.ln(2)
    
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_fill_color(245, 245, 245)
    pdf.cell(60, 8, 'Category', border=1, fill=True)
    pdf.cell(80, 8, 'Description', border=1, fill=True)
    pdf.cell(50, 8, 'Amount', border=1, fill=True, ln=True)
    
    pdf.set_font('Helvetica', '', 9)
    subtotal = 0.0
    for c in costs:
        est = float(c.get('estimated') or 0)
        subtotal += est
        pdf.cell(60, 7, str(c.get('category', '')).title(), border=1)
        desc = (c.get('description') or '')[:40]
        pdf.cell(80, 7, desc, border=1)
        pdf.cell(50, 7, f'${est:,.2f}', border=1, ln=True, align='R')

    # No itemised lines (or a zero sum): price from the bid amount already on
    # the card rather than printing $0.00.
    if subtotal <= 0:
        try:
            total = float((bid or {}).get('bid_amount') or 0)
        except (TypeError, ValueError):
            total = 0.0
        markup_pct = 0
    else:
        if markup_pct and markup_pct > 0:
            markup = subtotal * (markup_pct / 100.0)
            total = subtotal + markup
        else:
            markup = 0.0
            total = subtotal

    pdf.ln(2)
    pdf.set_font('Helvetica', 'B', 10)
    pdf.cell(140, 8, 'Subtotal:', border=0)
    pdf.cell(50, 8, f'${subtotal:,.2f}', border=0, ln=True, align='R')

    if markup_pct and markup_pct > 0 and subtotal > 0:
        markup = subtotal * (markup_pct / 100.0)
        pdf.cell(140, 8, f'Markup ({markup_pct}%):', border=0)
        pdf.cell(50, 8, f'${markup:,.2f}', border=0, ln=True, align='R')

    pdf.set_font('Helvetica', 'B', 12)
    pdf.cell(140, 10, 'TOTAL BID PRICE:', border=0)
    pdf.cell(50, 10, f'${total:,.2f}', border=0, ln=True, align='R')
    
    pdf.ln(10)
    
    # Terms
    pdf.set_font('Helvetica', 'B', 11)
    pdf.cell(0, 8, 'Terms & Conditions', ln=True)
    pdf.set_font('Helvetica', '', 9)
    pdf.multi_cell(0, 5, 'This proposal is valid for 30 days from the date of submission.\nPayment terms to be discussed upon award.\nBuildStack Construction LLC is licensed and insured.')
    
    pdf.ln(5)
    pdf.set_font('Helvetica', '', 10)
    pdf.cell(0, 6, 'We appreciate the opportunity to bid on this project.', ln=True)
    pdf.cell(0, 6, 'We look forward to hearing from you.', ln=True)

    if signature:
        # Signed by the contractor, so the proposal arrives ready to accept
        # rather than as a number to argue with.
        pdf.ln(8)
        y = pdf.get_y()
        pdf.image(signature["png"], x=pdf.l_margin + 1, y=y, w=58, h=13,
                  keep_aspect_ratio=True)
        pdf.line(pdf.l_margin, y + 14, pdf.l_margin + 70, y + 14)
        pdf.set_font('Helvetica', '', 8)
        pdf.set_text_color(90, 90, 90)
        pdf.cell(0, 4, f'Signed electronically by {signature.get("name") or ""}'
                       f' on {signature.get("date") or ""}', ln=True)
        pdf.set_font('Helvetica', '', 9)
        pdf.cell(0, 4, f'For {signature.get("company") or "BuildStack Construction LLC"}', ln=True)
        pdf.set_text_color(0, 0, 0)

    return bytes(pdf.output())


def build_proposal_email(bid: dict, total: float) -> tuple[str, str]:
    subject = f"Bid Proposal - {bid.get('name', 'Project')}"
    body = f"""Subject: Bid Proposal for {bid.get('name', '')}

Hello {bid.get('contact_name') or bid.get('owner') or 'there'},

Please find attached our proposal for the {bid.get('name', '')} project{' at ' + bid.get('location') if bid.get('location') else ''}.

Total Bid Price: ${total:,.2f}

We look forward to the opportunity to work with you on this project. Please let us know if you have any questions or need additional information.

Best regards,
BuildStack Construction LLC
757-908-7121
hello@bizstackperks.com
"""
    return subject, body
