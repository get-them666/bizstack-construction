"""Email service for deposit instructions and review notifications."""
from __future__ import annotations

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional


# ─── Configuration ───────────────────────────────────────────────────────────

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
FROM_ADDRESS = os.getenv("FROM_ADDRESS", "hello@bizstackperks.com")
FROM_NAME = os.getenv("FROM_NAME", "BizStack Perks")
OWNER_EMAIL = os.getenv("OWNER_EMAIL", "hello@bizstackperks.com")


# ─── Send Email Helper ──────────────────────────────────────────────────────

async def send_email(
    to_addresses: List[str],
    subject: str,
    html_body: str,
    reply_to: Optional[str] = None,
) -> bool:
    """Send an HTML email to one or more recipients."""
    if not SMTP_USER or not SMTP_PASS:
        print(f"[EMAIL] SMTP not configured. Would send to {to_addresses}: {subject}")
        return False
    
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{FROM_NAME} <{FROM_ADDRESS}>"
        msg["To"] = ", ".join(to_addresses)
        if reply_to:
            msg["Reply-To"] = reply_to
        
        msg.attach(MIMEText(html_body, "html"))
        
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(FROM_ADDRESS, to_addresses, msg.as_string())
        
        print(f"[EMAIL] Sent to {to_addresses}: {subject}")
        return True
    except Exception as e:
        print(f"[EMAIL] Failed to send: {e}")
        return False


# ─── Deposit Instructions Email ─────────────────────────────────────────────

def build_deposit_email_html(
    customer_name: str,
    quote_id: int,
    brand: str,
    scope_description: str,
    deposit_amount: float,
    final_price: float,
    schedule_url: str,
    zelle_handle: str,
    ach_routing: str,
    ach_account: str,
    bank_name: str = "Your Bank",
) -> str:
    """Build HTML email for deposit instructions."""
    
    brand_name = "BuildStack Construction" if brand == "construction" else "Broom Service"
    brand_color = "#f59e0b" if brand == "construction" else "#10b981"
    phone = "+1 (757) 846-9275" if brand == "construction" else "+1 (757) 908-7121"
    
    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 640px; margin: 0 auto; padding: 20px; background: #f5f5f5;">
    <div style="background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
        <div style="background: {brand_color}; color: white; padding: 24px;">
            <h1 style="margin: 0; font-size: 24px;">{brand_name}</h1>
            <p style="margin: 8px 0 0; opacity: 0.9;">Quote #{quote_id} — Deposit Instructions</p>
        </div>
        
        <div style="padding: 24px;">
            <p>Hi {customer_name},</p>
            
            <p>Thank you for accepting our quote! Here are your payment instructions to lock in your slot.</p>
            
            <div style="background: #f9f9f9; border-left: 4px solid {brand_color}; padding: 16px; margin: 20px 0; border-radius: 4px;">
                <h3 style="margin: 0 0 12px; color: #333;">Written Scope</h3>
                <p style="margin: 0; white-space: pre-wrap; color: #555; font-size: 14px;">{scope_description}</p>
            </div>
            
            <div style="background: #f0f9ff; border-radius: 8px; padding: 20px; margin: 20px 0; text-align: center;">
                <p style="margin: 0 0 4px; color: #666; font-size: 14px;">Total Project Cost</p>
                <p style="margin: 0; font-size: 28px; font-weight: bold; color: #1a1a2e;">${final_price:,.2f}</p>
                <p style="margin: 12px 0 4px; color: #666; font-size: 14px;">Deposit Due Now</p>
                <p style="margin: 0; font-size: 32px; font-weight: bold; color: {brand_color};">${deposit_amount:,.2f}</p>
                <p style="margin: 8px 0 0; color: #999; font-size: 12px;">Use Quote #{quote_id} as memo/reference</p>
            </div>
            
            <h3 style="color: #333; margin-top: 24px;">Payment Options</h3>
            
            <div style="border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px; margin: 12px 0;">
                <h4 style="margin: 0 0 8px; color: {brand_color};">📱 Zelle</h4>
                <p style="margin: 0; font-size: 16px; font-weight: bold;">{zelle_handle}</p>
                <p style="margin: 8px 0 0; color: #666; font-size: 13px;">Send ${deposit_amount:,.2f} with memo: <strong>Quote #{quote_id}</strong></p>
            </div>
            
            <div style="border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px; margin: 12px 0;">
                <h4 style="margin: 0 0 8px; color: {brand_color};">🏦 ACH / Bank Transfer</h4>
                <table style="font-size: 14px; width: 100%;">
                    <tr><td style="padding: 4px 0; color: #666;">Bank:</td><td style="padding: 4px 0; font-weight: bold;">{bank_name}</td></tr>
                    <tr><td style="padding: 4px 0; color: #666;">Routing:</td><td style="padding: 4px 0; font-weight: bold; font-family: monospace;">{ach_routing}</td></tr>
                    <tr><td style="padding: 4px 0; color: #666;">Account:</td><td style="padding: 4px 0; font-weight: bold; font-family: monospace;">{ach_account}</td></tr>
                    <tr><td style="padding: 4px 0; color: #666;">Amount:</td><td style="padding: 4px 0; font-weight: bold;">${deposit_amount:,.2f}</td></tr>
                    <tr><td style="padding: 4px 0; color: #666;">Memo:</td><td style="padding: 4px 0; font-weight: bold;">Quote #{quote_id}</td></tr>
                </table>
            </div>
            
            <div style="text-align: center; margin: 24px 0;">
                <a href="{schedule_url}" style="display: inline-block; background: {brand_color}; color: white; padding: 14px 32px; border-radius: 8px; text-decoration: none; font-weight: bold; font-size: 16px;">
                    📅 Book Your {'Walkthrough' if brand == 'construction' else 'Cleaning'} Date
                </a>
            </div>
            
            <p style="color: #666; font-size: 13px;">Once we receive your deposit, we'll confirm your booking within 2 business hours.</p>
            
            <hr style="border: none; border-top: 1px solid #eee; margin: 24px 0;">
            
            <p style="color: #999; font-size: 12px; text-align: center;">
                {brand_name} | 701 Dana Dr, Chesapeake, VA 23321<br>
                {phone} | hello@bizstackperks.com
            </p>
        </div>
    </div>
</body>
</html>
"""


async def send_deposit_instructions(
    customer_name: str,
    customer_email: str,
    quote_id: int,
    brand: str,
    scope_description: str,
    deposit_amount: float,
    final_price: float,
    schedule_url: str,
) -> bool:
    """Send deposit instructions email to customer and owner."""
    
    zelle_handle = os.getenv("ZELLE_HANDLE", "hello@bizstackperks.com")
    ach_routing = os.getenv("ACH_ROUTING", "000000000")
    ach_account = os.getenv("ACH_ACCOUNT", "000000000000")
    bank_name = os.getenv("BANK_NAME", "BizStack Perks LLC")
    
    html_body = build_deposit_email_html(
        customer_name=customer_name,
        quote_id=quote_id,
        brand=brand,
        scope_description=scope_description,
        deposit_amount=deposit_amount,
        final_price=final_price,
        schedule_url=schedule_url,
        zelle_handle=zelle_handle,
        ach_routing=ach_routing,
        ach_account=ach_account,
        bank_name=bank_name,
    )
    
    brand_name = "BuildStack Construction" if brand == "construction" else "Broom Service"
    subject = f"{brand_name} — Quote #{quote_id} Deposit Instructions (${deposit_amount:,.2f})"
    
    # Send to customer + owner
    recipients = [customer_email, OWNER_EMAIL]
    
    return await send_email(
        to_addresses=recipients,
        subject=subject,
        html_body=html_body,
    )


# ─── Review Feedback Email ──────────────────────────────────────────────────

async def send_negative_feedback_alert(
    customer_name: str,
    customer_email: str,
    rating: int,
    feedback: str,
    quote_id: Optional[int] = None,
    brand: str = "broom",
) -> bool:
    """Send negative feedback alert to owner for immediate resolution."""
    
    brand_name = "BuildStack Construction" if brand == "construction" else "Broom Service"
    
    html_body = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; max-width: 640px; margin: 0 auto; padding: 20px;">
    <div style="background: #fef2f2; border-left: 4px solid #ef4444; padding: 16px; border-radius: 4px;">
        <h2 style="margin: 0; color: #991b1b;">⚠️ Negative Feedback Received</h2>
        <p style="margin: 8px 0; color: #7f1d1d;">Immediate attention required — resolve offline.</p>
    </div>
    
    <div style="background: white; border: 1px solid #e5e7eb; border-radius: 8px; padding: 20px; margin-top: 16px;">
        <table style="font-size: 14px; width: 100%;">
            <tr><td style="padding: 6px 0; color: #666;">Customer:</td><td style="font-weight: bold;">{customer_name}</td></tr>
            <tr><td style="padding: 6px 0; color: #666;">Email:</td><td>{customer_email}</td></tr>
            <tr><td style="padding: 6px 0; color: #666;">Rating:</td><td style="color: #ef4444; font-weight: bold;">{'★' * rating}{'☆' * (5 - rating)}</td></tr>
            <tr><td style="padding: 6px 0; color: #666;">Brand:</td><td>{brand_name}</td></tr>
            {'<tr><td style=\"padding: 6px 0; color: #666;\">Quote:</td><td>#' + str(quote_id) + '</td></tr>' if quote_id else ''}
        </table>
        
        <h3 style="margin: 16px 0 8px; color: #333;">Customer Feedback:</h3>
        <div style="background: #f9fafb; padding: 12px; border-radius: 6px; font-style: italic;">
            "{feedback}"
        </div>
        
        <p style="margin-top: 16px; color: #666; font-size: 13px;">
            Action: Contact customer directly to resolve. Do NOT let this become a public review.
        </p>
    </div>
</body>
</html>
"""
    
    return await send_email(
        to_addresses=[OWNER_EMAIL],
        subject=f"[ALERT] {rating}-Star Feedback from {customer_name} — {brand_name}",
        html_body=html_body,
    )
