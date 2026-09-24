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

    if source == "sam-gov":
        lines = [
            greeting,
            f"Thank you for the solicitation at {address}." if address else "Thank you for the solicitation.",
            "We are reviewing the requirements and intend to submit a response"
            + (f" for {service}" if service else "")
            + ". Please let us know if additional documentation or a site visit is needed to complete our bid.",
            "Our team is available at (757) 846-9275 to coordinate next steps.",
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


def _ensure_draft_column(db):
    """Idempotently add the reply-draft column used by review mode."""
    try:
        with db.cursor() as cur:
            cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS draft_reply TEXT;")
            db.commit()
    except Exception as exc:
        print(f"[auto-reply] draft column ensure failed: {exc}", flush=True)


def _review_mode() -> bool:
    """True when the bot should stage reply drafts for owner review instead of sending.

    LEAD_AUTO_SEND=1 (or on/true/yes) re-enables automatic email/text. Without it
    (or when set to 0/off) every new lead gets a review draft instead."""
    return (os.getenv("LEAD_AUTO_SEND", "") or "").strip().lower() not in ("1", "true", "yes", "on")


def _smoke_recipient_email(email: str) -> bool:
    """Quick scrub so we never hand a junk/placeholder address to the send providers."""
    email = (email or "").strip().lower()
    if not email or "@" not in email or "." not in email.split("@", 1)[1]:
        return False
    if email.endswith("@lead.local"):
        return False
    bad_tlds = ("local", "invalid", "test", "example", "fake", "none", "missing", "unknown")
    if email.rsplit(".", 1)[-1].lower() in bad_tlds:
        return False
    return True


def _outbound_cap(channel: str, default: int = 30) -> int:
    env = {"email": "EMAIL_DAILY_CAP", "text": "TEXT_DAILY_CAP", "sms": "TEXT_DAILY_CAP"}.get(channel, "")
    raw = os.getenv(env, "") if env else ""
    try:
        return max(0, int(raw or default))
    except (TypeError, ValueError):
        return default


def _outbound_sent_today(db, channel: str) -> int:
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM comms_logs WHERE direction = 'outbound' AND channel = %s "
                "AND created_at >= date_trunc('day', now());",
                (channel,),
            )
            row = cur.fetchone()
        return int(row["c"] if isinstance(row, dict) else (row[0] if row else 0))
    except Exception:
        return 0


def _channel_allowed(db, channel: str) -> tuple:
    """Return (ok, reason_or_empty). Enforces the daily outbound caps."""
    cap = _outbound_cap(channel)
    if cap <= 0:
        return True, ""
    used = _outbound_sent_today(db, channel)
    if used >= cap:
        return False, f"{channel} daily cap ({cap}) reached — {used} sent today"
    return True, ""


def _phone_e164(phone):
    digits = re.sub(r"\D", "", str(phone or ""))
    if not digits:
        return ""
    if digits.startswith("1") and len(digits) == 11:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    return "+" + digits


def _stage_draft(db, lead_id, company_key, channel, recipient, body):
    """Store a review draft on the lead and surface it in notes (both sites)."""
    if not lead_id:
        return False
    _ensure_draft_column(db)
    draft = f"[BOT DRAFT REPLY – {channel}, not sent] To: {recipient}\n{body}"
    try:
        with db.cursor() as cur:
            cur.execute("SELECT notes, draft_reply FROM leads WHERE id = %s;", (lead_id,))
            lead = cur.fetchone()
            notes = (lead.get("notes") or "") if lead else ""
            stamp = time.strftime("%b %d %Y %H:%M")
            note = f"\n[BOT DRAFT REPLY ({channel}) {stamp}] To: {recipient}\n{body[:800]}"
            notes = (notes.strip() + note).strip()[:6000] if notes.strip() else note.strip()[:6000]
            cur.execute(
                "UPDATE leads SET notes = %s, draft_reply = %s WHERE id = %s;",
                (notes, draft[:4000], lead_id),
            )
            db.commit()
        print(f"[auto-reply {company_key}] staged draft on leads#{lead_id} ({channel})", flush=True)
        return True
    except Exception as exc:
        print(f"[auto-reply {company_key}] draft stage failed for lead {lead_id}: {exc}", flush=True)
        return False


def _fire_sent_effect(db, lead_id, company_key, channel, recipient, body):
    """Mark a lead contacted after a successful real send; clears the staged draft."""
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT notes, draft_reply FROM leads WHERE id = %s;", (lead_id,),
            )
            lead = cur.fetchone()
            notes = (lead.get("notes") or "") if lead else ""
            notes = (_strip_draft_marker(notes))[:6000] if notes else ""
            cur.execute(
                "UPDATE leads SET notes = %s, draft_reply = NULL, status = 'contacted' "
                "WHERE id = %s AND status = 'new';",
                (notes, lead_id),
            )
            cur.execute(
                "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
                "VALUES ('outbound', %s, 'system', %s, %s);",
                (channel, recipient, body[:2000]),
            )
            db.commit()
        print(f"[auto-reply {company_key}] sent {channel} to {recipient} (lead #{lead_id})", flush=True)
        return True
    except Exception as exc:
        print(f"[auto-reply {company_key}] post-send bookkeeping failed for lead {lead_id}: {exc}", flush=True)
        return False


def _strip_draft_marker(notes: str) -> str:
    """Remove previous BOT DRAFT REPLY blocks from a lead's notes after a real send."""
    import re as _re

    cleaned = _re.sub(r"\[BOT DRAFT REPLY[^\]]*\] To: .*?(?:\n|$)", "", notes or "")
    cleaned = _re.sub(r"\n\[BOT DRAFT REPLY\b.*(?:\n|$)", "", cleaned)
    return _re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def ensure_lead_reply(db, company_key, *, name="", phone="", email="", service="", address="",
                      budget="", timeline="", message="", source="", funding=False,
                      sqft=None, lead_id=None):
    """Guarantee a reply exists for a new lead (both companies).

    In auto mode (LEAD_AUTO_SEND=1) sends the first-touch email/text immediately,
    respecting the EMAIL_DAILY_CAP / TEXT_DAILY_CAP budgets and refusing junk
    addresses. In review mode stages the drafted reply in the lead notes for the
    owner to fire manually. Returns {"sent": bool, "staged": bool, "msg": str}."""
    _ensure_draft_column(db)
    auto = not _review_mode()
    company_key = company_key if company_key in COMPANIES else "broom"
    email = (email or "").strip()
    service = (service or "").strip()

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

    co_name = COMPANIES[company_key]["name"]
    msg = _build_message(company_key, co_name, quote=quote, name=name, service=service, address=address,
                         budget=budget, timeline=timeline, message=message, source=source, funding=funding)

    sent = staged = False

    if email and _smoke_recipient_email(email):
        try:
            cfg = documents_service.smtp_config_from_env()
            has_smtp = documents_service.smtp_configured(cfg)
        except Exception:
            has_smtp = False
        if auto and has_smtp:
            ok, why = _channel_allowed(db, "email")
            if ok:
                try:
                    subject = f"Thanks for reaching out{f', {name.split()[0]}' if name else ''} — {co_name}"
                    run_coro(documents_service.send_email(cfg, email, subject, msg.replace("\n", "<br>")))
                    sent = _fire_sent_effect(db, lead_id, company_key, "email", email, msg)
                except Exception as exc:
                    print(f"[auto-reply {company_key}] email failed for {email}: {exc}", flush=True)
            else:
                print(f"[auto-reply {company_key}] email to {email} skipped — {why}", flush=True)
        if not sent:
            staged = _stage_draft(db, lead_id, company_key, "email", email, msg) or staged

    if _valid_phone(phone) and not sent:
        to = _phone_e164(phone)
        try:
            import signalwire_service
            sw = signalwire_service.SignalWireService()
            has_sw = bool(sw.is_configured())
        except Exception:
            sw = None
            has_sw = False
        if auto and has_sw:
            ok, why = _channel_allowed(db, "text")
            if ok:
                try:
                    if sw.send_sms(to, msg):
                        sent = _fire_sent_effect(db, lead_id, company_key, "text", to, msg)
                except Exception as exc:
                    print(f"[auto-reply {company_key}] sms failed for {to}: {exc}", flush=True)
            else:
                print(f"[auto-reply {company_key}] sms to {to} skipped — {why}", flush=True)
        if not sent:
            staged = _stage_draft(db, lead_id, company_key, "text", to, msg) or staged

    if not email and not _valid_phone(phone):
        print(f"[auto-reply {company_key}] no reachable channel for lead (phone={_valid_phone(phone)}, email={email})", flush=True)
    print(f"[auto-reply {company_key}] lead reply for #{lead_id}: sent={sent} staged={staged}", flush=True)
    return {"sent": sent, "staged": staged, "msg": msg}


def fire_lead_draft(db, company_key, lead_id, *, force=False):
    """Send a staged draft reply for a lead (the owner 'Send this reply' action).

    Reads the draft stored on the lead, sends via the best channel, logs it, marks
    the lead contacted, and clears the draft. Manual fire still honors the daily
    caps and address scrub, but returns per-channel reasons when blocked."""
    result = {"sent": False, "why": ""}
    if not lead_id:
        result["why"] = "no lead id"
        return result
    _ensure_draft_column(db)
    try:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM leads WHERE id = %s;", (lead_id,))
            lead = cur.fetchone()
        if not lead:
            result["why"] = "lead not found"
            return result
        draft = (lead.get("draft_reply") or "").strip()
        body = draft.split("\n", 1)[1] if "\n" in draft else draft
        candidate_channel = "text" if draft.startswith("[BOT DRAFT REPLY – text") else "email"
        candidate_channels = ["text", "email"] if candidate_channel == "text" else ["email", "text"]
        for channel in candidate_channels:
            if channel == "email":
                recipient = (lead.get("email") or "").strip()
                if not _smoke_recipient_email(recipient):
                    continue
            else:
                recipient = _phone_e164(lead.get("phone"))
                if not _valid_phone(recipient):
                    continue
            if not force:
                ok, why = _channel_allowed(db, "text" if channel == "text" else "email")
                if not ok:
                    result["why"] = why
                    print(f"[auto-reply {company_key}] manual fire blocked \u2014 {why}", flush=True)
                    continue
            else:
                ok = True
                why = ""
            if not ok:
                result["why"] = why
                print(f"[auto-reply {company_key}] manual fire blocked — {why}", flush=True)
                continue
            if channel == "email":
                try:
                    cfg = documents_service.smtp_config_from_env()
                    if not documents_service.smtp_configured(cfg):
                        result["why"] = "email not configured"
                        continue
                    subject = (draft.split("\n", 1)[0] or "Thanks for reaching out")
                    run_coro(documents_service.send_email(cfg, recipient, subject.replace("To: ", ""), body.replace("\n", "<br>")))
                except Exception as exc:
                    print(f"[auto-reply {company_key}] fire email failed: {exc}", flush=True)
                    result["why"] = str(exc)[:200]
                    continue
            else:
                to = recipient
                try:
                    import signalwire_service
                    sw = signalwire_service.SignalWireService()
                    if not sw.is_configured():
                        result["why"] = "text not configured"
                        continue
                    sw.send_sms(to, body)
                except Exception as exc:
                    print(f"[auto-reply {company_key}] fire sms failed: {exc}", flush=True)
                    result["why"] = str(exc)[:200]
                    continue
            sent = _fire_sent_effect(db, lead_id, company_key, channel, recipient, body)
            if sent:
                result["sent"] = True
                result["channel"] = channel
                result["why"] = ""
            return result
    except Exception as exc:
        result["why"] = f"fire failed: {exc}"[:300]
        print(f"[auto-reply {company_key}] fire draft failed for lead {lead_id}: {exc}", flush=True)
    return result


def fire_all_drafts(db, company_key=None):
    """Owner action: fire every staged review draft for a company at once.

    Loops over leads with a draft_reply set, sends each via fire_lead_draft
    (which re-checks caps + scrub per lead), and reports sent/skipped. Returns
    {"pending": n, "sent": m, "skipped": k, "reasons": {reason: count}}."""
    company_key = company_key if company_key in COMPANIES else None
    _ensure_draft_column(db)
    pending = []
    try:
        with db.cursor() as cur:
            if company_key:
                cur.execute(
                    "SELECT id FROM leads WHERE company = %s AND draft_reply IS NOT NULL "
                    "AND LOWER(COALESCE(draft_reply, '')) <> '' ORDER BY id;",
                    (company_key,),
                )
                pending = [r["id"] for r in cur.fetchall()]
            else:
                for ck in COMPANIES:
                    cur.execute(
                        "SELECT id FROM leads WHERE company = %s AND draft_reply IS NOT NULL "
                        "AND LOWER(COALESCE(draft_reply, '')) <> '' ORDER BY id;",
                        (ck,),
                    )
                    pending += [(ck, r["id"]) for r in cur.fetchall()]
    except Exception as exc:
        print(f"[auto-reply] fire-all query failed: {exc}", flush=True)
        return {"pending": 0, "sent": 0, "skipped": 0, "errors": [f"query failed: {exc}"]}
    sent = 0
    skipped = 0
    reasons = {}
    for ck, lead_id in pending:
        res = fire_lead_draft(db, ck, lead_id, force=True)
        if res.get("sent"):
            sent += 1
        else:
            skipped += 1
            why = res.get("why") or "unknown"
            reasons[why] = reasons.get(why, 0) + 1
    print(f"[auto-reply] fire-all: {len(pending)} pending, {sent} sent, {skipped} skipped", flush=True)
    return {'pending': len(pending), 'sent': sent, 'skipped': skipped, 'reasons': reasons}


def auto_reply_to_lead(db, company_key, *, name="", phone="", email="", service="", address="",
                       budget="", timeline="", message="", source="", funding=False,
                       sqft=None, lead_id=None):
    """Back-compat wrapper around ensure_lead_reply: returns the message text when
    a reply was actually sent, otherwise None (callers treat None as 'not sent')."""
    if os.getenv("AUTO_REPLY_ENABLED", "1").lower() not in ("1", "true", "yes"):
        return None
    out = ensure_lead_reply(
        db, company_key, name=name, phone=phone, email=email, service=service,
        address=address, budget=budget, timeline=timeline, message=message,
        source=source, funding=funding, sqft=sqft, lead_id=lead_id,
    )
    return out["msg"] if out.get("sent") else None


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


def bot_health(db):
    """Owner check: bot activity summary right now (no logs scrolling).

    Queries the real outbound tables + leads to report how many review drafts were
    sent today, how many are still staged, and today's scrub counts — so an owner
    can check "have any leads been contacted / sent today?" at a glance. Returns
    {"sent_today": n, "staged": m, "caps": {"email": x, "text": y}, "error": ""}."""
    _ensure_draft_column(db)
    result = {"sent_today": 0, "staged": 0, "caps": {}, "error": ""}
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(CASE WHEN channel = 'email' THEN 1 ELSE 0 END), 0) AS email, "
                "COALESCE(SUM(CASE WHEN channel = 'text' THEN 1 ELSE 0 END), 0) AS text "
                "FROM outbound_log WHERE sent_at::date = CURRENT_DATE;",
            )
            row = cur.fetchone() or {}
            result["sent_today"] = (row.get("email") or 0) + (row.get("text") or 0)
            result["caps"] = {"email": row.get("email") or 0, "text": row.get("text") or 0}
            cur.execute(
                "SELECT COUNT(*) AS n FROM leads WHERE draft_reply IS NOT NULL "
                "AND LOWER(COALESCE(draft_reply, '')) <> '';",
            )
            result["staged"] = (cur.fetchone() or {}).get("n") or 0
    except Exception as exc:
        result["error"] = f"query failed: {exc}"
        print(f"[auto-reply] bot_health query failed: {exc}", flush=True)
    print(f"[auto-reply] bot-health: {result['sent_today']} sent today, "
          f"{result['staged']} staged, caps {result['caps']}", flush=True)
    return result

