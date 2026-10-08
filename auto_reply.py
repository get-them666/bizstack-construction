import os
import re
import json
import threading
import time
from datetime import datetime, timezone

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
            delivered = run_coro(documents_service.send_email(cfg, to, subject, summary))
            if not delivered:
                print(f"[notify-owner] email to {to} not delivered (provider returned False)", flush=True)
                ok = False
            else:
                ok = True
                print(f"[notify-owner] emailed {to} (lead #{lead_id})", flush=True)
        except Exception as exc:
            print(f"[notify-owner] email to {to} failed: {exc}", flush=True)
        if delay > 0:
            time.sleep(delay)
    return ok

COMPANIES = {
    "broom": {"name": "Broom Service", "cta": "Call or text us at (757) 908-7121 anytime."},
    "construction": {"name": "Buildstack Construction", "cta": "Call or text us at (757) 908-7121 anytime."},
}


def _valid_phone(phone):
    if not phone:
        return False
    digits = re.sub(r"\D", "", str(phone))
    return 10 <= len(digits) <= 15


# City names that stand in for a missing applicant. The permit feeds publish no
# applicant, so `leads.name` holds the city until a skip trace or a manual touch
# supplies a real person. Without this list, "Hi Chesapeake" is a plausible
# first line of an email to a named homeowner, and it did go out that way.
_NOT_A_PERSON = {
    "virginia beach", "chesapeake", "williamsburg", "norfolk", "newport news",
    "hampton", "portsmouth", "suffolk", "virginia", "north carolina", "corolla",
    "elizabeth city", "currituck", "hampton roads", "unknown", "permit",
    # permit_service._seed_demo writes this as contractor_name and it reaches
    # leads.name, where it printed "Hi Self,".
    "self", "owner", "homeowner", "owner-builder", "property owner", "n/a",
}


def _person_first(name):
    """First name from a lead name field, or "" when it is not a person.

    SAM.gov leads store name as "{title} · {contact name}", web/inbound
    leads usually as a plain person name. Always prefer the actual person.

    A place name must return "" so the caller falls back to "Hi there" rather
    than greeting a homeowner by the city they live in. This fired in real
    sends: the skip-traced leads for 1501 Vance Cir and 925 Longbeeches Ave both
    opened "Hi Chesapeake," because `leads.name` was the city at the time the
    message was built.
    """
    if not name:
        return ""
    raw = (name or "").strip()
    if " · " in raw:
        raw = raw.rsplit(" · ", 1)[-1].strip()
    raw = raw.strip().strip('"').strip()
    if not raw:
        return ""
    if re.sub(r"\s+", " ", raw).strip().lower() in _NOT_A_PERSON:
        return ""
    first = raw.split()[0]
    if len(first) < 2:
        return ""
    return first


def _build_message(company_key, co_name, quote=None, **ctx):
    source = (ctx.get("source") or "").lower()
    name = (ctx.get("name") or "").strip()
    service = (ctx.get("service") or "").strip()
    budget = (ctx.get("budget") or "").strip()
    timeline = (ctx.get("timeline") or "").strip()
    address = (ctx.get("address") or "").strip()
    detail = ctx.get("message") or ""

    first = _person_first(name)
    greeting = f"Hi {first}," if first else "Hi there,"
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
            "Our team is available at (757) 908-7121 to coordinate next steps.",
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
    except Exception as exc:
        print(f"[auto-reply] outbound count failed for {channel}; assuming cap reached: {exc}", flush=True)
        return _outbound_cap(channel)


def _recent_send_guard(db, recipient: str, channel: str, days: int = 1) -> tuple:
    """Hard per-recipient cooldown. Returns (ok, reason).

    This is the guard that was missing. _recipient_recently_sent has existed
    since the two-touch policy landed but nothing ever called it, so the only
    limit on repeat contact to one address was the daily cap -- which is
    per-channel across the whole list and therefore never binds a single
    number that is being re-processed on every pass. The result was 39,222 rows
    for one phone over six days at roughly 0.25s intervals.

    Fail-closed in both directions: an unknown recipient or a failed lookup is
    treated as already-sent, so a broken query can never become a blast.
    """
    if not recipient or db is None:
        return False, "no recipient"
    if _recipient_recently_sent(db, recipient, channel, days):
        return False, f"already {channel} to {recipient} in the last {days}d"
    return True, ""


def _hard_daily_ceiling(channel: str) -> int:
    """Absolute floor on the daily cap, so a mistyped env var cannot uncap a channel.

    EMAIL_DAILY_CAP and TEXT_DAILY_CAP are read from the environment on every
    call. Setting either to 0, -1, an empty string or a nonsense value falls
    through to a default that permits sending, which is the wrong direction for
    a safety limit. This clamps the configured value into a sane band.
    """
    configured = os.getenv(
        {"email": "EMAIL_DAILY_CAP", "text": "TEXT_DAILY_CAP", "sms": "TEXT_DAILY_CAP"}.get(channel, ""), "")
    if not (configured or "").strip():
        # Unset is not "unlimited" and not "disabled" -- it is the documented
        # default. _outbound_cap already defaults to 30 per channel; return that
        # rather than 0, which would read as "no automated contact" and silently
        # switch the whole bot off.
        return _outbound_cap(channel, default=30)
    raw = _outbound_cap(channel, default=30)
    ceiling = 50 if channel == "email" else 20
    if raw <= 0:
        # 0 means "no automated contact" in _channel_allowed, so a non-positive
        # configured cap must not be reinterpreted as unlimited.
        return 0
    return min(raw, ceiling)


def _recipient_recently_sent(db, recipient: str, channel: str, days: int) -> bool:
    """True if we already sent `channel` to this recipient within `days`.

    Fail-closed: if the lookup errors we return True (treat as already sent) so a
    broken query can never turn into a duplicate blast."""
    if not recipient or db is None:
        return False
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM comms_logs WHERE direction = 'outbound' AND channel = %s "
                "AND LOWER(recipient) = LOWER(%s) "
                "AND created_at > now() - make_interval(days => %s) LIMIT 1;",
                (channel, recipient, int(days)),
            )
            return cur.fetchone() is not None
    except Exception as exc:
        print(f"[auto-reply] suppression lookup failed for {recipient} ({channel}); treating as sent: {exc}", flush=True)
        return True


def _lead_replied(db, lead_id) -> bool:
    """True if this lead has replied since we last contacted them. A reply ends
    the automated cadence until the owner restarts it."""
    if not lead_id or db is None:
        return False
    try:
        with db.cursor() as cur:
            cur.execute("SELECT last_reply_at FROM leads WHERE id = %s;", (lead_id,))
            row = cur.fetchone()
        return bool(row and (row.get("last_reply_at") if isinstance(row, dict) else row[0]))
    except Exception:
        return False


def _suppression_days() -> int:
    try:
        return max(0, int(os.getenv("LEAD_SUPPRESSION_DAYS", "30")))
    except (TypeError, ValueError):
        return 30


def _max_touches() -> int:
    """Touches allowed for a lead who has never responded."""
    try:
        return max(0, int(os.getenv("LEAD_MAX_TOUCHES", "2") or 2))
    except (TypeError, ValueError):
        return 2


def _retouch_days() -> int:
    """Days before the one timed follow-up may go out without a response."""
    try:
        return max(0, int(os.getenv("LEAD_RETOUCH_DAYS", "5") or 5))
    except (TypeError, ValueError):
        return 5


def _max_touches_replied() -> int:
    """Hard ceiling once a lead has responded.

    A reply unlocks a further follow-up, but only a bounded number of them:
    _lead_replied() means "ever replied", so without a ceiling one response would
    leave the address permanently eligible and the sweep would never stop.
    """
    try:
        return max(_max_touches(), int(os.getenv("LEAD_MAX_TOUCHES_REPLIED", "3") or 3))
    except (TypeError, ValueError):
        return 3


def contact_policy_allows(db, recipient: str, channel: str, lead_id=None) -> tuple:
    """Contact once. A follow-up may follow a reply, or once 5 days have passed.

    After that lead is never contacted again unless they respond: elapsed time
    alone buys exactly one timed follow-up and no more. A response reopens the
    door, bounded by LEAD_MAX_TOUCHES_REPLIED so it cannot become a cadence.

    Fail-closed: a failed lookup counts as "do not send".
    """
    max_touches = _max_touches()
    if max_touches <= 0:
        return False, "automated contact disabled (LEAD_MAX_TOUCHES=0)"
    if not recipient or db is None:
        return False, "no recipient"
    try:
        with db.cursor() as cur:
            # When the lead is known, count EVERY outbound touch to that lead
            # across all channels, because the policy is per lead, not per
            # address: a letter mailed to a lead who has no email must consume
            # the same single touch an email would, or the lead gets two letters
            # plus two emails. Older rows have lead_id NULL, so fall back to the
            # original per-(channel, recipient) count for those and take the
            # larger of the two -- under-counting here would re-contact a lead
            # who already ignored us.
            if lead_id:
                cur.execute(
                    "SELECT COUNT(*) AS n, MAX(created_at) AS last_at FROM comms_logs "
                    "WHERE direction = 'outbound' AND ("
                    "  lead_id = %s OR (channel = %s AND LOWER(recipient) = LOWER(%s))"
                    ");",
                    (lead_id, channel, recipient),
                )
                row = cur.fetchone() or {}
                touches = int(row.get("n") or 0)
                last_at = row.get("last_at")
                cur.execute(
                    "SELECT COUNT(*) AS n, MAX(created_at) AS last_at FROM comms_logs "
                    "WHERE direction = 'outbound' AND lead_id = %s;",
                    (lead_id,),
                )
                by_lead = cur.fetchone() or {}
                lead_touches = int(by_lead.get("n") or 0)
                lead_last = by_lead.get("last_at")
                if lead_touches > touches:
                    touches, last_at = lead_touches, lead_last
            else:
                cur.execute(
                    "SELECT COUNT(*) AS n, MAX(created_at) AS last_at FROM comms_logs "
                    "WHERE direction = 'outbound' AND channel = %s AND LOWER(recipient) = LOWER(%s);",
                    (channel, recipient),
                )
                row = cur.fetchone() or {}
                touches = int(row.get("n") or 0)
                last_at = row.get("last_at")
    except Exception as exc:
        print(f"[auto-reply] touch count failed for {recipient} ({channel}); withholding: {exc}", flush=True)
        return False, "touch count unavailable"

    if touches == 0:
        return True, ""
    replied = bool(lead_id) and _lead_replied(db, lead_id)
    if replied:
        ceiling = _max_touches_replied()
        if touches >= ceiling:
            return False, f"already contacted {touches}x after replies (max {ceiling})"
        return True, f"follow-up allowed: lead replied ({touches} prior touch)"
    if touches >= max_touches:
        return False, f"contacted {touches}x with no reply — never again without a response"
    if last_at is None:
        return True, ""
    last = last_at if last_at.tzinfo else last_at.replace(tzinfo=timezone.utc)
    waited = (datetime.now(timezone.utc) - last).total_seconds() / 86400.0
    if waited < _retouch_days():
        return False, f"follow-up waits {_retouch_days() - waited:.1f}d"
    return True, ""


def record_manual_touch(db, lead_id, channel: str, detail: str = "") -> dict:
    """Record an owner-initiated contact: a letter, a door knock, a phone call.

    The bot only ever sees its own outbound touches, so a lead the owner reached
    in person still looked untouched and the email sweep would contact them
    again -- the same person getting a letter and an email against a policy that
    allows one touch. Writing to comms_logs makes the manual touch count, which
    is the whole point of the contact policy being per-lead.

    Channels are the bot's own vocabulary plus the physical ones, because the
    policy counts across every channel:

        letter | door-knock | phone | sms | in-person | other

    The row is attributed to the lead via lead_id, which is the column
    contact_policy_allows prefers. Without a recipient address, the
    per-(channel, recipient) half of that query cannot match, so lead_id is what
    makes this visible to the policy at all.
    """
    channel = (channel or "").strip().lower()
    allowed = {"letter", "door-knock", "phone", "sms", "in-person", "other"}
    if channel not in allowed:
        return {"ok": False, "error": f"channel must be one of {sorted(allowed)}"}
    if not lead_id or db is None:
        return {"ok": False, "error": "lead_id and db are required"}
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    note = (detail or "").strip()
    body = f"[owner {channel} {stamp}]" + (f" {note}" if note else "")
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body, lead_id) "
                "VALUES ('outbound', %s, 'owner', %s, %s, %s);",
                (channel, f"lead:{lead_id}", body, lead_id),
            )
            # A manual touch is a first touch for most leads. Marking it
            # 'contacted' keeps /leads honest; the policy, not this status, is
            # what decides whether another automated touch may go out.
            cur.execute(
                "UPDATE leads SET status = 'contacted' "
                "WHERE id = %s AND status = 'new';",
                (lead_id,),
            )
            cur.execute(
                "UPDATE leads SET notes = CASE WHEN COALESCE(notes, '') = '' THEN %s "
                "ELSE notes || chr(10) || %s END WHERE id = %s;",
                (body, body, lead_id),
            )
            db.commit()
    except Exception as exc:
        print(f"[auto-reply] could not record manual touch for lead {lead_id}: {exc}", flush=True)
        return {"ok": False, "error": str(exc)[:200]}
    print(f"[auto-reply] recorded owner {channel} on lead {lead_id}", flush=True)
    return {"ok": True, "channel": channel, "lead_id": lead_id, "at": stamp}


def note_withheld(db, lead_id, reason: str) -> None:
    """Record why a lead was not contacted, once, so the owner can audit it.

    Written to leads.notes so it is visible in Postico alongside the lead, and
    guarded by a marker so a 15-minute poll does not append the same line
    hundreds of times.
    """
    if not lead_id or db is None:
        return
    marker = "[outreach-policy]"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    try:
        with db.cursor() as cur:
            cur.execute("SELECT notes FROM leads WHERE id = %s;", (lead_id,))
            row = cur.fetchone() or {}
            notes = row.get("notes") or ""
            line = f"\n{marker} {stamp} withheld: {reason}"
            if marker in notes:
                cur.execute(
                    "UPDATE leads SET notes = REPLACE(%s, %s, %s) WHERE id = %s;",
                    (notes + line, marker, marker, lead_id),
                )
            else:
                cur.execute("UPDATE leads SET notes = %s WHERE id = %s;", (notes + line, lead_id))
            db.commit()
    except Exception as exc:
        print(f"[auto-reply] could not record withheld note for lead {lead_id}: {exc}", flush=True)


def _blocked_sources() -> set:
    """Lead sources that must never enter the automated email cadence.

    LEAD_SOURCE_AUTO_EMAIL only stops new ingestion from these sources; it does
    nothing about rows already in the table.
    """
    raw = os.getenv("LEAD_SOURCE_EMAIL_BLOCK", "sam-gov") or ""
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def blocked_reason(email: str, source: str = "") -> str:
    """Why this lead/address must not be auto-contacted, or "" if it may be.

    Federal procurement contacts are excluded by default: a .gov/.mil buyer is
    not a customer lead, and auto-emailing them is the same category of conduct
    as the original runaway-send incident, at smaller scale.
    """
    if (source or "").strip().lower() in _blocked_sources():
        return f"source '{source}' is blocked from automated email"
    addr = (email or "").strip().lower()
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    if (os.getenv("BLOCK_GOV_MIL_EMAIL", "1") or "1").lower() in ("1", "true", "yes", "on"):
        if _is_federal_domain(domain):
            return "federal domain"
    return ""


# Suffix matching on (".gov", ".mil") missed real federal procurement contacts
# that are still in the leads table: us.af.mil, Wendy_Deleon@nps.gov,
# mwalker05@fs.fed.us. .mil and .gov still catch the common cases, but the
# narrower point is that any government domain is a federal buyer regardless of
# TLD, so this checks the registrable labels rather than a fixed suffix list.
_FEDERAL_EXACT = {"fed.us", "gov", "mil", "fed"}


def _is_federal_domain(domain: str) -> bool:
    """True for .gov/.mil plus the subdomains and .fed.us that carry them.

    Matching the registrable pair is what catches us.af.mil (labels mil.mil ->
    not a registrable pair, but the last label is mil) and fs.fed.us.

    'gov' as a *label* only counts when it is the public suffix, i.e. the
    address ends in .gov. cityofhampton.gov.us has a 'gov' label too, but .us
    is the public suffix there and that is a Virginia city site, not a federal
    buyer -- matching it would block a legitimate municipal contact. Nothing in
    the leads table uses a .gov.us shape, so only .gov, .mil and .fed.us are
    treated as federal.
    """
    d = (domain or "").strip().lower().rstrip(".")
    if not d:
        return False
    # A leading dot is not a domain: '.gov' must not normalise to 'gov' and
    # then look like the TLD.
    if d.startswith("."):
        return False
    labels = d.split(".")
    if labels[-1] in ("gov", "mil"):
        return True
    return labels[-1] == "us" and len(labels) >= 2 and labels[-2] == "fed"


def _channel_allowed(db, channel: str) -> tuple:
    """Return (ok, reason_or_empty). Enforces the daily outbound caps.

    The configured cap is clamped by _hard_daily_ceiling so a mistyped or
    negative env var cannot silently turn a safety limit into unlimited sends.
    """
    cap = _hard_daily_ceiling(channel)
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
                "WHERE id = %s;",
                (notes, lead_id),
            )
            cur.execute(
                "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body, lead_id) "
                "VALUES ('outbound', %s, 'system', %s, %s, %s);",
                (channel, recipient, body[:2000], lead_id),
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
    to = ""

    may_email = False
    if email and _smoke_recipient_email(email):
        may_email, why_email = contact_policy_allows(db, email, "email", lead_id)
        if not may_email:
            print(f"[auto-reply {company_key}] not emailing {email} — {why_email}", flush=True)
            note_withheld(db, lead_id, f"email {why_email}")

    if may_email:
        try:
            cfg = documents_service.smtp_config_from_env()
            has_smtp = documents_service.smtp_configured(cfg)
        except Exception:
            has_smtp = False
        if auto and has_smtp:
            cool, cool_why = _recent_send_guard(db, email, "email", 1)
            ok, why = _channel_allowed(db, "email") if cool else (False, cool_why)
            if ok:
                try:
                    subject = f"Thanks for reaching out{f', {_person_first(name)}' if _person_first(name) else ''} — {co_name}"
                    delivered = run_coro(documents_service.send_email(cfg, email, subject, msg.replace("\n", "<br>")))
                    if delivered:
                        sent = _fire_sent_effect(db, lead_id, company_key, "email", email, msg)
                    else:
                        print(f"[auto-reply {company_key}] email to {email} not delivered (provider returned False); staging draft", flush=True)
                except Exception as exc:
                    print(f"[auto-reply {company_key}] email failed for {email}: {exc}", flush=True)
            else:
                print(f"[auto-reply {company_key}] email to {email} skipped — {why}", flush=True)
        if not sent:
            staged = _stage_draft(db, lead_id, company_key, "email", email, msg) or staged

    if _valid_phone(phone) and not sent:
        to = _phone_e164(phone)
        may_text, why_text = contact_policy_allows(db, to, "text", lead_id)
        if not may_text:
            print(f"[auto-reply {company_key}] not texting {to} — {why_text}", flush=True)
            note_withheld(db, lead_id, f"text {why_text}")
            to = ""
    if to and not sent:
        try:
            import signalwire_service
            sw = signalwire_service.SignalWireService()
            has_sw = bool(sw.is_configured())
        except Exception:
            sw = None
            has_sw = False
        if auto and has_sw:
            # Per-recipient cooldown first. The daily cap alone cannot stop this:
            # it is a whole-list budget, so it never binds a single number that
            # gets reprocessed on every pass. That gap produced 39,222 rows for
            # one phone in six days at ~0.25s intervals.
            cool, cool_why = _recent_send_guard(db, to, "text", 1)
            ok, why = _channel_allowed(db, "text") if cool else (False, cool_why)
            if ok:
                try:
                    if sw.send_sms(to, msg):
                        sent = _fire_sent_effect(db, lead_id, company_key, "text", to, msg)
                    else:
                        print(f"[auto-reply {company_key}] sms to {to} not delivered (provider returned False); staging draft", flush=True)
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
        if not draft:
            result["why"] = "no draft staged"
            return result
        body = draft.split("\n", 1)[1] if "\n" in draft else draft
        candidate_channel = "text" if draft.startswith("[BOT DRAFT REPLY – text") else "email"
        candidate_channels = ["text", "email"] if candidate_channel == "text" else ["email", "text"]
        # A lead who has replied is out of the automated cadence; the owner
        # restarts it manually.
        if not force and _lead_replied(db, lead_id):
            result["why"] = "lead has replied — manual send required"
            print(f"[auto-reply {company_key}] lead #{lead_id} replied; auto-flush skipping", flush=True)
            return result
        supp = _suppression_days()
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
                ok, why = contact_policy_allows(db, recipient, channel, lead_id)
                if not ok:
                    result["why"] = why
                    note_withheld(db, lead_id, f"{channel} {why}")
                    print(f"[auto-reply {company_key}] lead #{lead_id} {channel} withheld — {why}", flush=True)
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
                    delivered = run_coro(documents_service.send_email(cfg, recipient, subject.replace("To: ", ""), body.replace("\n", "<br>")))
                    if not delivered:
                        print(f"[auto-reply {company_key}] fire email to {recipient} not delivered (provider returned False)", flush=True)
                        result["why"] = "transport refused"
                        continue
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
    blocked = blocked_reason(email, source)
    if blocked:
        print(f"[auto-reply {company_key}] withheld — {blocked}", flush=True)
        note_withheld(db, lead_id, blocked)
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
        "hello@bizstackperks.com · (757) 908-7121",
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
                     service="", address="", state="", url="", lead_id=None, source=""):
    """Send a professional bid-inquiry email to a public-contract point of contact.

    For outbound public-sector opportunities (e.g. SAM.gov). This is NOT the
    inbound auto-reply: the recipient is a buyer, so we express interest and ask
    for the full solicitation package. Email only — never SMS or AI-call a
    government contracting officer.

    `source` is the lead's provenance. It is not defaulted to sam-gov: this
    function is also the send path for permit and Scrap.io leads, and hardcoding
    the federal source blocked every recipient including the residential ones
    this business actually wants.
    """
    company_key = company_key if company_key in COMPANIES else "construction"
    co_name = COMPANIES[company_key]["name"]
    email = (email or "").strip()
    if not email:
        return False
    # The federal block has to be enforced HERE, not just on the auto-reply
    # path. auto_reply_to_lead calls blocked_reason and the emailbot logged
    # "withheld" correctly, but this function never did -- so SAM.gov leads were
    # emailed straight to .gov and .mil points of contact anyway, which is the
    # exact conduct the block exists to prevent. 194,660 of those sends are in
    # comms_logs. Only auto_reply_to_lead consulted it, so the guard looked
    # armed from the outside while this path walked straight past it.
    blocked = blocked_reason(email, source)
    if blocked:
        print(f"[bid-inquiry {company_key}] withheld — {blocked}", flush=True)
        note_withheld(db, lead_id, blocked)
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
        # documents_service.send_email returns False when the Gmail transport is
        # unavailable, and that return value used to be discarded: the
        # comms_logs row was written and True returned regardless. That is what
        # produced 194,660 "sent" rows against 85 distinct recipients, and
        # because every owner-facing count is a COUNT over comms_logs, the
        # dashboard reported a bot that had been working when nothing had left
        # the building. Only a confirmed send may be logged as one.
        if not run_coro(documents_service.send_email(cfg, email, subject, msg.replace("\n", "<br>"))):
            print(f"[bid-inquiry {company_key}] send to {email} did not deliver; not logged", flush=True)
            return False
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
                "AND email IS NOT NULL AND email <> '' AND email NOT LIKE '%%@lead.local' "
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
            # There is no `outbound_log` table in this schema, so this query
            # always raised and the owner dashboard always showed
            # "relation outbound_log does not exist". comms_logs is the real
            # outbound record; created_at is the insert time.
            cur.execute(
                "SELECT COALESCE(SUM(CASE WHEN channel = 'email' THEN 1 ELSE 0 END), 0) AS email, "
                "COALESCE(SUM(CASE WHEN channel IN ('text','sms') THEN 1 ELSE 0 END), 0) AS text "
                "FROM comms_logs WHERE direction = 'outbound' "
                "AND created_at >= date_trunc('day', now());",
            )
            row = cur.fetchone() or {}
            result["sent_today"] = (row.get("email") or 0) + (row.get("text") or 0)
            result["caps"] = {"email": row.get("email") or 0, "text": row.get("text") or 0}
            # Surface the repeat-send risk directly: any address contacted more
            # than once in the suppression window is a bug, not a lead.
            supp = _suppression_days()
            cur.execute(
                "SELECT recipient, channel, COUNT(*) AS n FROM comms_logs "
                "WHERE direction = 'outbound' "
                "AND created_at > now() - make_interval(days => %s) "
                "GROUP BY 1,2 HAVING COUNT(*) > 1 ORDER BY n DESC LIMIT 10;",
                (supp,),
            )
            result["repeat_sends"] = [
                {"recipient": r.get("recipient"), "channel": r.get("channel"), "count": r.get("n")}
                for r in (cur.fetchall() or [])
            ]
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
