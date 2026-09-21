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


def _owner_targets():
    """Owner/admin mailbox list: NOTIFY_EMAIL, else ADMIN_EMAIL, else hello@."""
    return [t.strip() for t in (os.getenv("NOTIFY_EMAIL", "") or "").split(",") if t.strip()] or [
        (os.getenv("ADMIN_EMAIL", "") or "").strip() or "hello@bizstackperks.com"
    ]


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
    targets = _owner_targets()
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

def build_bid_inquiry(company_key, *, title="", solicitation="", contact_name="", service="",
                      address="", state="", url=""):
    """Return (subject, body) for an outbound public-contract bid inquiry."""
    company_key = company_key if company_key in COMPANIES else "construction"
    co_name = COMPANIES[company_key]["name"]
    contact = (contact_name or "").strip()
    greeting = f"Dear {contact}," if contact else "Hello,"
    sol = (solicitation or "").strip()
    ref_line = f"Solicitation {sol}" if sol else (title.strip() or "your recent opportunity")
    blurb = (
        "Buildstack Construction is a licensed and insured general contractor serving Virginia "
        "and North Carolina, specializing in residential and light-commercial remodeling, roofing, "
        "and specialty trade work."
        if company_key == "construction" else
        "Broom Service provides janitorial, custodial, grounds, and building-services support across "
        "Virginia and North Carolina."
    )
    lines = [
        greeting,
        f"{co_name} is interested in {ref_line}"
        + (f" — {title.strip()}" if title and title.strip() != ref_line else "")
        + (f" ({address.strip()})" if address else "") + ".",
        blurb,
        "Could you please send the full solicitation package (scope, specifications, submission "
        "requirements, and any pre-bid or site-visit details)? We can provide licensing, insurance, "
        "and past-performance information as needed.",
        "Thank you for your time.",
        "",
        f"— {co_name}",
        "hello@bizstackperks.com · (757) 846-9275",
    ]
    if url:
        lines.append(f"Opportunity: {url}")
    msg = "\n".join(lines)
    subject = (f"Bid inquiry — {title.strip() or ref_line}")[:140]
    return subject, msg


def send_owner_lead_digest(db, company_key, items):
    """Email the owner a digest of new public-contract leads with ready-to-send
    drafts, so they can review and fire them off themselves.

    Sent to NOTIFY_EMAIL / the business mailbox. Because that address is on our
    own verified domain it is deliverable even while SES is in sandbox, so this
    works regardless of the Resend daily quota.
    """
    company_key = company_key if company_key in COMPANIES else "construction"
    co_name = COMPANIES[company_key]["name"]
    items = [it for it in (items or []) if it]
    if not items:
        return False
    targets = _owner_targets()
    cfg = documents_service.smtp_config_from_env()
    if not documents_service.smtp_configured(cfg):
        print(f"[owner-digest {company_key}] SMTP not configured; skipped", flush=True)
        return False
    parts = [
        f"{len(items)} new public-contract lead(s) for {co_name}.",
        "Each draft below is ready to send. Copy it into your email, set the To: address, and send.",
        "",
    ]
    for i, it in enumerate(items, 1):
        subject, body = build_bid_inquiry(
            company_key,
            title=it.get("title", ""), solicitation=it.get("solicitation", ""),
            contact_name=it.get("contact_name", ""), service=it.get("service", ""),
            address=it.get("address", ""), state=it.get("state", ""), url=it.get("url", ""),
        )
        parts.append("=" * 60)
        parts.append(f"{i}. {it.get('title') or '(untitled)'}")
        if it.get("service"):
            parts.append(f"Trade/service: {it['service']}")
        if it.get("address"):
            parts.append(f"Location: {it['address']}")
        parts.append(f"To: {it.get('email') or '(no email listed — use the link)'}")
        if it.get("phone"):
            parts.append(f"Phone: {it.get('phone')}")
        if it.get("url"):
            parts.append(f"Link: {it['url']}")
        parts.append("")
        parts.append(f"--- DRAFT — subject: {subject} ---")
        parts.append(body)
        parts.append("")
    digest = "\n".join(parts)
    subject_line = f"{len(items)} new {co_name} bid lead(s) — ready-to-send drafts"
    ok = False
    for to in targets:
        if not to:
            continue
        try:
            run_coro(documents_service.send_email(cfg, to, subject_line, digest))
            ok = True
            print(f"[owner-digest {company_key}] emailed {len(items)} draft(s) to {to}", flush=True)
        except Exception as exc:
            print(f"[owner-digest {company_key}] digest to {to} failed: {exc}", flush=True)
    if ok and db is not None:
        try:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
                    "VALUES ('outbound', 'email', 'system', %s, %s);",
                    (",".join(targets), digest),
                )
                db.commit()
        except Exception as exc:
            print(f"[owner-digest {company_key}] comms log failed: {exc}", flush=True)
    return ok


def send_bid_inquiry(db, company_key, *, title="", solicitation="", contact_name="", email="",
                     service="", address="", state="", url="", lead_id=None):
    """Send a professional bid-inquiry email to a public-contract point of contact.

    For outbound public-sector opportunities (e.g. SAM.gov). This is NOT the
    inbound auto-reply: the recipient is a buyer, so we express interest and ask
    for the full solicitation package. Email only — never SMS or AI-call a
    government contracting officer.
    """
    company_key = company_key if company_key in COMPANIES else "construction"
    co_name = COMPANIES[company_key]["name"]
    email = (email or "").strip()
    if not email:
        return False
    if db is not None:
        try:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM comms_logs WHERE channel = 'email' AND direction = 'outbound' "
                    "AND LOWER(recipient) = LOWER(%s) LIMIT 1;",
                    (email,),
                )
                if cur.fetchone():
                    print(f"[bid-inquiry {company_key}] already emailed {email}; skipping", flush=True)
                    return False
        except Exception:
            pass
    if db is not None:
        try:
            cap = int(os.getenv("LEAD_EMAIL_DAILY_CAP", "80") or 80)
        except (TypeError, ValueError):
            cap = 80
        if cap > 0:
            try:
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) AS c FROM comms_logs WHERE channel = 'email' "
                        "AND direction = 'outbound' AND created_at >= date_trunc('day', now());"
                    )
                    row = cur.fetchone()
                    sent_today = (row["c"] if isinstance(row, dict) else row[0]) if row else 0
                if sent_today >= cap:
                    print(f"[bid-inquiry {company_key}] daily email cap ({cap}) reached; skipping {email}", flush=True)
                    return False
            except Exception:
                pass
    subject, msg = build_bid_inquiry(
        company_key, title=title, solicitation=solicitation, contact_name=contact_name,
        service=service, address=address, state=state, url=url,
    )
    try:
        cfg = documents_service.smtp_config_from_env()
        if not documents_service.smtp_configured(cfg):
            print(f"[bid-inquiry {company_key}] SMTP not configured; skipped {email}", flush=True)
            return False
        run_coro(documents_service.send_email(cfg, email, subject, msg.replace("\n", "<br>")))
        if db is not None:
            try:
                with db.cursor() as cur:
                    cur.execute(
                        "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
                        "VALUES ('outbound', 'email', 'system', %s, %s);",
                        (email, msg),
                    )
                    db.commit()
            except Exception as exc:
                print(f"[bid-inquiry {company_key}] comms log failed: {exc}", flush=True)
        print(f"[bid-inquiry {company_key}] emailed {email} (lead #{lead_id})", flush=True)
        return True
    except Exception as exc:
        print(f"[bid-inquiry {company_key}] email failed for {email}: {exc}", flush=True)
        return False


def fire_pending_bid_inquiries(company_key, *, db=None, max_emails=0, dry_run=False):
    """Fire bid-inquiry emails to every scan lead that has a real email and has
    not been emailed yet (the manual 'fire them all' owner action). dry_run
    only counts and prints the pending queue without sending."""
    company_key = company_key if company_key in COMPANIES else "construction"
    db_url = os.getenv("DATABASE_URL", "")
    open_here = db is None
    if open_here:
        import psycopg
        from psycopg.rows import dict_row
        db = psycopg.connect(db_url, row_factory=dict_row)
        db.autocommit = True
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, name, phone, email, project_type, address, listing_url, source "
                "FROM leads "
                "WHERE campaign = 'lead-source-scan' AND status = 'new' AND company = %s "
                "AND email IS NOT NULL AND email <> '' AND email NOT LIKE '%@lead.local' "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM comms_logs cl "
                "  WHERE cl.channel = 'email' AND cl.direction = 'outbound' AND LOWER(cl.recipient) = LOWER(leads.email)"
                ") ORDER BY id;",
                (company_key,),
            )
            pending = cur.fetchall()
        total = len(pending or [])
        if dry_run:
            for r in (pending or []):
                rn = r.get("name") or "(untitled)"
                print(f"[bulk-fire {company_key}] pending: #{r.get('id')} {rn[:60]} <{r.get('email')}>", flush=True)
            print(f"[bulk-fire {company_key}] dry-run: {total} pending, not sending", flush=True)
            return {"pending": total, "sent": 0, "dry_run": True}
        selected = pending if max_emails <= 0 else pending[:max_emails]
        sent = 0
        for r in selected:
            rn = r.get("name") or ""
            if " · " in rn:
                title, _, contact = rn.partition(" · ")
            else:
                title, contact = rn, ""
            if send_bid_inquiry(
                db, company_key,
                title=title or (r.get("project_type") or ""),
                solicitation="",
                contact_name=contact, email=r.get("email", ""),
                service=r.get("project_type") or "",
                address=r.get("address") or "", url=r.get("listing_url") or "",
                lead_id=r.get("id"),
            ):
                sent += 1
            time.sleep(1.0)
        print(f"[bulk-fire {company_key}] fired {sent}/{len(selected)} pending (total pending {total})", flush=True)
        return {"pending": total, "selected": len(selected), "sent": sent}
    finally:
        if open_here:
            try:
                db.close()
            except Exception:
                pass
