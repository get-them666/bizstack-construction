import os
import re
import json
import threading
import time

import documents_service
import estimating_service
import stripe_service


def run_coro(coro):
    """Run a coroutine from sync code whether an event loop is running or not."""
    import asyncio

    def _runner():
        try:
            return asyncio.run(coro)
        except Exception as e:
            return e

    try:
        asyncio.get_running_loop()
        running = True
    except RuntimeError:
        running = False

    if not running:
        out = _runner()
        if isinstance(out, BaseException):
            raise out
        return out

    result = {}

    def _thread():
        result["value"] = _runner()

    t = threading.Thread(target=_thread, daemon=True)
    t.start()
    t.join()
    out = result.get("value")
    if isinstance(out, BaseException):
        raise out
    return out


def _usd(cents):
    return f"${cents // 100:,}"


def notify_owner_email(company_key, *, lead_id, name="", phone="", email="", service="", address="",
                       budget="", timeline="", message="", source=""):
    """Email the owner/admin whenever a new lead is captured.

    Target is NOTIFY_EMAIL (comma-separated list allowed); falls back to
    ADMIN_EMAIL and then the business mailbox. Sent via documents_service
    (Resend preferred), from SMTP_FROM."""
    co_name = (COMPANIES.get(company_key, {}).get("name")) or "BizStack"
    name = (name or "").strip()
    email = (email or "").strip()
    message = (message or "").strip()
    if not name and not phone and not email:
        return False
    targets = [
        t.strip() for t in (os.getenv("NOTIFY_EMAIL", "") or "").split(",")
    ] or [
        (os.getenv("ADMIN_EMAIL", "") or "").strip() or "hello@bizstackperks.com"
    ]
    summary = (
        f"New {co_name} lead (#{lead_id}) — {name}, {phone}"
        + (f", {email}" if email else "")
        + (f"\nProject: {service}" if service else "")
        + (f"\nAddress: {address}" if address else "")
        + (f"\nBudget: {budget}" if budget else "")
        + (f"\nTimeline: {timeline}" if timeline else "")
        + (f"\n{message[:200]}" if message else "")
        + (f"\nSource: {source}" if source else "")
    )
    subject = f"New {co_name} lead: {name or 'New inquiry'}"
    cfg = documents_service.smtp_config_from_env()
    if not documents_service.smtp_configured(cfg):
        return False
    ok = False
    try:
        delay = float(os.getenv("NOTIFY_SEND_DELAY", "0.35") or 0.35)
    except (TypeError, ValueError):
        delay = 0.35
    for to in targets:
        if not to:
            continue
        try:
            run_coro(documents_service.send_email(cfg, to, subject, summary))
            ok = True
            print(f"[notify-owner] emailed {to} (lead #{lead_id})", flush=True)
        except Exception as exc:
            print(f"[notify-owner] email to {to} failed: {exc}", flush=True)
        if delay > 0:
            time.sleep(delay)
    return ok

COMPANIES = {
    "broom": {"name": "Broom Service", "cta": "Call or text us at (757) 846-9275 anytime."},
    "construction": {"name": "Buildstack Construction", "cta": "Call or text us at (757) 846-9275 anytime."},
}


def _valid_phone(phone):
    if not phone:
        return False
    digits = re.sub(r"\D", "", str(phone))
    return 10 <= len(digits) <= 15


def _build_message(company_key, co_name, quote=None, **ctx):
    source = (ctx.get("source") or "").lower()
    name = (ctx.get("name") or "").strip()
    service = (ctx.get("service") or "").strip()
    budget = (ctx.get("budget") or "").strip()
    timeline = (ctx.get("timeline") or "").strip()
    address = (ctx.get("address") or "").strip()
    detail = ctx.get("message") or ""

    greeting = f"Hi {name.split()[0]}," if name else "Hi there,"
    footer = f"\n\n{COMPANIES[company_key]['cta']} Reply STOP to opt out."
    quote_line = f" Quick ballpark quote: {quote}." if quote else ""

    if source == "finance":
        lines = [
            greeting,
            f"Thanks for applying for financing with {co_name} — we got your application"
            + (f" for a {service} project" if service else "")
            + (f" (~{budget})" if budget else "")
            + ".",
            "A financing specialist and estimator will reach out within one business day to walk you through your options."
            + quote_line
            + " No credit impact, no obligation.",
        ]
        return " ".join(lines) + footer

    if source == "permit_finder":
        lines = [
            greeting,
            f"We noticed a recent permit for work at {address}" if address else "We came across your project recently,"
            + (f" ({service})" if service else ""),
            "Buildstack Construction is available for an estimate"
            + quote_line
            + " We'd love to walk through it with you this week. Free, no obligation.",
        ]
        return " ".join(lines) + footer

    # generic inbound (get-started funnel, website form, contact)
    lines = [
        greeting,
        f"Thanks for reaching out to {co_name}"
        + (f" about {'your ' if service else 'a '}{service} project" if service else ""),
        (
            f"We received your request{', budget ' + budget if budget else ''}"
            f"{', starting ' + timeline.lower() if timeline else ''}."
            if (budget or timeline)
            else "We received your request."
        )
        + quote_line + ".",
        "A project estimator will text you within one business day with next steps and a free walkthrough.",
    ]
    if detail:
        lines.append(f"In the meantime, here's what we noted: {detail[:180]}")
    return " ".join(lines) + footer


def _auto_quote(company_key, service, sqft):
    """Return (quote_text, est_or_None). est can be stamped on the lead."""
    try:
        if company_key == "construction" and service:
            est = estimating_service.auto_quote(service, sqft)
            if est:
                if est.get("assumed_sqft"):
                    note = " (based on a typical ~1,750 sq ft home)"
                elif est.get("kind") == "sqft" and est.get("sqft"):
                    note = f" for ~{est['sqft']:,} sq ft"
                elif est.get("kind") == "square" and est.get("roof_squares"):
                    note = f" for ~{est['roof_squares']:,.0f} roof squares"
                else:
                    note = ""
                text = f"{_usd(est['low_cents'])}–{_usd(est['high_cents'])} for a {est['label']}{note}"
                return text, est
        elif company_key == "broom" and service:
            cents = stripe_service.StripeService().get_price(service)
            if cents:
                text = f"{_usd(cents)} for {service}"
                return text, {"low_cents": cents, "high_cents": cents, "label": service}
    except Exception as exc:
        print(f"[auto-reply {company_key}] quote failed: {exc}", flush=True)
    return None, None


def auto_reply_to_lead(db, company_key, *, name="", phone="", email="", service="", address="",
                       budget="", timeline="", message="", source="", funding=False,
                       sqft=None, lead_id=None):
    """Send an automatic first-touch reply to a newly captured lead (both companies).

    Optionally includes a quick ballpark quote (construction: estimating models;
    broom: cleaning price list) and stamps the quoted range on the lead row.
    Prefers SMS; falls back to email when there's no valid phone number, we already
    auto-texted this number in the last 10 minutes, or the SMS send fails.
    Skips entirely when AUTO_REPLY_ENABLED is false and there's no reachable channel."""
    if os.getenv("AUTO_REPLY_ENABLED", "1").lower() not in ("1", "true", "yes"):
        return None
    company_key = company_key if company_key in COMPANIES else "broom"
    email = (email or "").strip()
    service = (service or "").strip()
    if not email:
        return None
    co_name = COMPANIES[company_key]["name"]

    quote, est = _auto_quote(company_key, service, sqft)
    if est and company_key == "construction" and lead_id:
        try:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE leads SET estimate_low_cents = %s, estimate_high_cents = %s, estimate_json = %s "
                    "WHERE id = %s AND company = 'construction';",
                    (est["low_cents"], est["high_cents"], json.dumps(est), lead_id),
                )
                db.commit()
        except Exception as exc:
            print(f"[auto-reply {company_key}] estimate stamp failed for lead {lead_id}: {exc}", flush=True)

    msg = _build_message(company_key, co_name, quote=quote, name=name, service=service, address=address,
                         budget=budget, timeline=timeline, message=message, source=source,
                         funding=funding)

    sent_email = False
    if email:
        try:
            cfg = documents_service.smtp_config_from_env()
            if documents_service.smtp_configured(cfg):
                subject = f"Thanks for reaching out{f', {name.split()[0]}' if name else ''} — {co_name}"
                run_coro(documents_service.send_email(cfg, email, subject, msg.replace("\n", "<br>")))
                with db.cursor() as cur:
                    cur.execute(
                        "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
                        "VALUES ('outbound', 'email', 'system', %s, %s);",
                        (email, msg),
                    )
                    db.commit()
                sent_email = True
                print(f"[auto-reply {company_key}] sent email to {email} (source={source})", flush=True)
        except Exception as exc:
            print(f"[auto-reply {company_key}] email failed for {email}: {exc}", flush=True)

    if sent_email:
        return msg
    print(f"[auto-reply {company_key}] no email on file for lead (phone={_valid_phone(phone)})", flush=True)
    return None