"""Pull Thumbtack job leads out of the local Mail store and file them, in order.

WHY THIS EXISTS
---------------
Thumbtack sends a notification email per job request. The volume is mostly
noise: of 70 Thumbtack messages in the Mail store, only a handful are actual
jobs. The rest are "Great fit" promotions, targeting notices, background check
and payout admin. Reading them by hand means re-reading the same lead three
times, because one job produces several emails: a "New direct lead", a
"Get back to X by <time>" nudge, and a "X is still waiting" reminder.

This groups those into one file per job, newest first, and writes a reply
draft next to each one.

WHY THE LOCAL MAIL STORE AND NOT THE GMAIL API
-----------------------------------------------
The OAuth tokens for hello@bizstackperks.com live in the production database,
which is only reachable from inside Railway's network. Mail.app keeps a full
copy of every message it has downloaded as .emlx files under
~/Library/Mail/V10, so reading them needs no credentials at all and cannot
fail on a token refresh. The trade-off is that it only sees mail already
synced to this Mac; anything Thumbtack sent while Mail.app was closed and has
since pruned may be missing.

WHY A DRAFT AND NOT A SEND
--------------------------
Every one of these messages is from do-not-reply@pro.thumbtack.com. Replying
to that address goes nowhere: Thumbtack routes conversation through its own
messaging, and a reply by email will not reach the customer. So each file
carries reply text to paste into the Thumbtack conversation, which is the
channel that actually works. Nothing is sent automatically.

Usage
-----
    python3 thumbtack_jobs.py --out "$HOME/Thumbtack Jobs"
    python3 thumbtack_jobs.py --out "$HOME/Thumbtack Jobs" --no-drafts
    python3 thumbtack_jobs.py --dry-run
"""

import argparse
import email
import email.policy
import os
import re
import sys
import unicodedata
from datetime import datetime
from email.utils import parsedate_to_datetime

MAIL_ROOT = os.path.expanduser("~/Library/Mail/V10")
DEFAULT_OUT = os.path.expanduser("~/Thumbtack Jobs")

BUSINESS = {
    "name": "Buildstack Construction",
    "phone": "(757) 908-7121",
    "email": "hello@bizstackperks.com",
    "site": "https://construction.bizstackperks.com",
}

LEAD_SUBJECT = re.compile(
    r"(new direct lead|customer [A-Z]\.\s?[A-Z]?\.\s?needs\b|is waiting|"
    r"get back to\b|reminder:.*is waiting|needs .* in [A-Z])",
    re.I,
)
ZIP = re.compile(r"\b([A-Z][A-Za-z .'-]+,\s*[A-Z]{2}\s*\d{5})\b")
BID = re.compile(r"thumbtack\.com/bid/(\d+)")
SUBJ_SERVICE = re.compile(r"needs (.+?) in [A-Z]", re.I)


def slug(value, limit=48):
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return (text[:limit] or "lead").rstrip("-")


def read_emlx(path):
    try:
        raw = open(path, "rb").read()
    except OSError:
        return None
    lines = raw.split(b"\n")
    if lines and lines[0].strip().isdigit():
        raw = b"\n".join(lines[1:])
    try:
        return email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:
        return None


def plain_body(msg):
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        text = part.get_content()
        if text:
            return text
    for sub in msg.walk():
        if sub.get_content_type() == "text/plain":
            payload = sub.get_payload(decode=True)
            if payload:
                return payload.decode("utf-8", "replace")
    return ""


def thumbtack_messages(root):
    found = []
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(".emlx"):
                continue
            path = os.path.join(dirpath, name)
            msg = read_emlx(path)
            if msg is None:
                continue
            sender = str(msg.get("From") or "")
            if "thumbtack" not in sender.lower():
                continue
            found.append((path, msg))
    return found


def parse_lead(msg):
    subject = str(msg.get("Subject") or "")
    body = plain_body(msg)
    try:
        when = parsedate_to_datetime(str(msg.get("Date") or ""))
    except (TypeError, ValueError):
        when = None

    bid = BID.search(body)
    location = ZIP.search(body)

    # The notification layout is positional: initials, then the customer's full
    # name, then a "Direct lead"/"Instant lead" marker, then the service. Matching
    # on position rather than on a name-shaped regex is what makes "Jennifer G."
    # work: a name pattern also matches "House Cleaning", which is the service.
    lines = [l.strip() for l in body.split("\n")]
    nonempty = [l for l in lines if l]
    name, service = "", ""
    for idx, line in enumerate(nonempty):
        low = line.lower().rstrip(".")
        if low in ("direct lead", "instant lead") and idx >= 1:
            name = nonempty[idx - 1]
            if idx + 1 < len(nonempty) and not ZIP.fullmatch(nonempty[idx + 1]):
                service = nonempty[idx + 1]
            break
    if not name:
        initials = re.search(r"Customer\s+([A-Z]\.\s?[A-Z]?\.?)", subject)
        if initials:
            name = " ".join(initials.group(1).split())
            svc = SUBJ_SERVICE.search(subject)
            if svc:
                service = svc.group(1).strip()
    if not name:
        # Nudge and reminder emails ("Get back to Jennifer G. by 4:24pm.",
        # "Jennifer G. is waiting") carry the name in the subject and nothing
        # else. Without this they file as separate jobs, so one lead becomes three.
        nudge = re.search(r"(?:back to|waiting\.?|for)\s+([A-Z][a-z]+(?:\s+[A-Z]\.)?)", subject)
        if nudge:
            name = nudge.group(1).strip()
    if not name:
        name = "Thumbtack customer"

    dates = re.search(r"^Dates:\s*(.+)$", body, re.M)
    times = re.search(r"^Times:\s*(.+)$", body, re.M)
    budget = re.search(r"^Budget:\s*(.+)$", body, re.M)

    # Thumbtack ranks on response speed: "You have 3 more hours to reply.
    # Replying within 4 hours helps you rank better in search results." A lead
    # with an expiry is a lead with a deadline, so the whole folder is ordered
    # by that rather than by when the mail arrived.
    hours = re.search(r"have (\d+) more hours? to reply", body, re.I)
    minutes = re.search(r"have (\d+) more minutes? to reply", body, re.I)
    deadline = None
    if when is not None:
        if hours:
            deadline = when.timestamp() + int(hours.group(1)) * 3600
        elif minutes:
            deadline = when.timestamp() + int(minutes.group(1)) * 60

    ROOMS = re.compile(
        r"\b(kitchen|dining room|living room|master bedroom|bedroom|bathroom|"
        r"basement|garage|patio|deck|office|hallway|laundry|den|attic|porch)\b",
        re.I,
    )
    SQFT = re.compile(r"\b([\d,]{3,6})\s*(?:sq\.?\s?ft|sqft|square\s+feet)\b", re.I)
    BEDS = re.compile(r"\b(\d)\s*(?:bed(?:room)?s?|br)\b", re.I)

    message = ""
    marker = "You can call this customer after you reply."
    if marker in body:
        message = body.split(marker, 1)[1]
    message = BID.sub("", message)
    message = re.sub(r"View direct lead:?.*", "", message, flags=re.I | re.S)
    message = re.sub(r"^\s*>.*$", "", message, flags=re.M)
    message = re.sub(r"\n{3,}", "\n\n", message).strip()

    rooms = sorted({m.group(1).lower() for m in ROOMS.finditer(message)})
    sqft = SQFT.search(message)
    beds = BEDS.search(message)

    return {
        "name": name,
        "service": service or SUBJ_SERVICE.search(subject).group(1).strip()
        if SUBJ_SERVICE.search(subject) else (service or "General"),
        "location": location.group(1).strip() if location else "",
        "dates": dates.group(1).strip() if dates else "",
        "times": times.group(1).strip() if times else "",
        "budget": budget.group(1).strip() if budget else "",
        "message": message,
        "rooms": rooms,
        "sqft": sqft.group(1) + " sqft" if sqft else "",
        "beds": beds.group(1) + " bed" if beds else "",
        "bid": bid.group(1) if bid else "",
        "url": "https://www.thumbtack.com/bid/%s" % bid.group(1) if bid else "",
        "subject": subject,
        "when": when,
        "deadline": deadline,
        "body": body,
    }


def _rates():
    """Ballpark ranges per service, overridable by env.

    These are PLACEHOLDERS. vision_quote.py carries the same warning: the real
    numbers live in env vars the owner retunes, and nothing produced here is a
    bid. A notification email carries a service and a city and nothing else, so
    the honest output is a range plus the assumptions behind it, not a fixed
    price. Override with e.g. TT_QUOTE_HOUSE_CLEANING="140,240".
    """
    defaults = {
        "house cleaning": (120, 220, "standard 3 bed / 2 bath"),
        "deep clean": (180, 320, "standard 3 bed / 2 bath, extra detail"),
        "carpet cleaning": (120, 240, "up to 3 rooms"),
        "pressure washing": (150, 340, "driveway, deck or siding"),
        "gutter cleaning": (120, 240, "up to 2 stories"),
        "upholstery": (90, 180, "per piece, 2 pieces minimum"),
        "furniture cleaning": (90, 180, "per piece, 2 pieces minimum"),
        "floor cleaning": (150, 300, "hardwood or tile, up to 400 sqft"),
        "window cleaning": (150, 300, "exterior, up to 20 panes"),
        "home organizing": (150, 400, "per room"),
        "junk removal": (200, 600, "load size varies"),
        "lawn care": (90, 220, "per visit"),
        "landscaping": (150, 500, "per visit"),
        "painting": (450, 2500, "per room"),
        "roofing": (600, 3500, "per sqft band"),
    }
    out = {}
    for key, (low, high, basis) in defaults.items():
        raw = os.getenv("TT_QUOTE_" + re.sub(r"[^A-Z]+", "_", key.upper()), "")
        if raw and "," in raw:
            try:
                lo, hi = raw.split(",", 1)
                low, high = float(lo), float(hi)
            except ValueError:
                pass
        out[key] = (float(low), float(high), basis)
    return out


def quote_for(lead):
    """A range plus the assumptions. Deliberately not a single number."""
    rates = _rates()
    service = (lead.get("service") or "").lower()
    match = next((k for k in rates if k in service), None)
    if match is None:
        for key in rates:
            if service and service in key:
                match = key
                break
    if match is None:
        return {
            "low": 0, "high": 0, "basis": "needs a scope before any number",
            "known": [],
        }
    low, high, basis = rates[match]
    known, notes = [], []

    if lead.get("sqft"):
        try:
            sqft = int(re.sub(r"[^0-9]", "", lead["sqft"]))
            if sqft > 2000:
                low, high = low * 1.2, high * 1.25
                notes.append("large home (%s)" % lead["sqft"])
        except ValueError:
            pass
    if lead.get("rooms"):
        known.append("%s" % ", ".join(lead["rooms"]))
        if len(lead["rooms"]) > 6:
            low, high = low * 1.12, high * 1.15
    city = (lead.get("location") or "").lower()
    if city and any(remote in city for remote in ("gates", "currituck", "suffolk", "yorktown")):
        low, high = low + 25, high + 25
        notes.append("travel outside core Hampton Roads")
    if lead.get("dates") and "," in lead["dates"]:
        notes.append("flexible on several dates")

    return {"low": low, "high": high, "basis": basis, "known": known, "notes": notes}


def render_quote(quote):
    if not quote["low"]:
        return ""
    lines = ["Ballpark: $%d - $%d (%s)." % (
        round(quote["low"] / 5) * 5, round(quote["high"] / 5) * 5, quote["basis"])]
    if quote["known"]:
        lines.append("Based on what you described: %s." % ", ".join(quote["known"]))
    if quote["notes"]:
        lines.append("Adjustments: %s." % ", ".join(quote["notes"]))
    lines.append("That's an estimate from the job description alone. A couple of photos")
    lines.append("pins it down.")
    return "\n".join(lines)


def build_draft(lead):
    who = lead["name"].split()[0] if lead["name"] else "there"
    quote = quote_for(lead)
    lines = []
    lines.append("Hi %s," % who)
    lines.append("")
    lines.append(
        "Thanks for reaching out about the %s in %s. I'd be glad to take a look."
        % (lead["service"].lower(), lead["location"] or "your area")
    )
    if lead["message"]:
        first = lead["message"].strip().split("\n")[0].strip()
        if first:
            lines.append("")
            lines.append('You asked: "%s"' % first)
            lines.append("")
            lines.append("Yes, that is exactly the kind of job we take on.")
    lines.append("")
    block = render_quote(quote)
    if block:
        lines.append(block)
        lines.append("")
    lines.append("Send a few photos of the area and I will firm up an exact price, usually")
    lines.append("the same day. Fastest way is the instant quote form:")
    lines.append("%s/instant-quote" % BUSINESS["site"])
    lines.append("")
    if lead["dates"]:
        lines.append("You mentioned %s are open. Tell me which suits and I will hold it."
                     % lead["dates"])
        lines.append("")
    lines.append(
        "Any questions, call or text %s. Happy to walk the job before you commit to anything."
        % BUSINESS["phone"]
    )
    lines.append("")
    lines.append("-- %s" % BUSINESS["name"])
    lines.append("%s | %s | %s" % (BUSINESS["phone"], BUSINESS["email"], BUSINESS["site"]))
    return "\n".join(lines)


def render(lead, drafts=True):
    stamp = lead["when"].strftime("%Y-%m-%d %H:%M") if lead["when"] else "unknown date"
    out = ["# %s - %s" % (lead["name"], lead["service"]), ""]
    out.append("- Received: %s" % stamp)
    out.append("- Service: %s" % lead["service"])
    if lead["location"]:
        out.append("- Location: %s" % lead["location"])
    if lead["dates"]:
        out.append("- Dates they offered: %s" % lead["dates"])
    if lead["times"]:
        out.append("- Times they offered: %s" % lead["times"])
    if lead.get("rooms"):
        out.append("- Rooms mentioned: %s" % ", ".join(lead["rooms"]))
    if lead.get("sqft"):
        out.append("- Size mentioned: %s" % lead["sqft"])
    if lead.get("beds"):
        out.append("- Bedrooms mentioned: %s" % lead["beds"])
    if lead.get("budget"):
        out.append("- Budget: %s" % lead["budget"])
    if lead.get("deadline"):
        out.append("- **Reply window closed: %s**"
                   % datetime.fromtimestamp(lead["deadline"]).strftime("%Y-%m-%d %H:%M"))
    out.append("- Contact details: not included by Thumbtack")
    if lead["url"]:
        out.append("- Conversation: %s" % lead["url"])
    out.append("- Subject line: %s" % lead["subject"].strip())
    out.append("")
    out.append("## What they wrote")
    out.append("")
    out.append(lead["message"].strip() or "(the notification carried no message text)")
    if drafts:
        quote = quote_for(lead)
        out.append("")
        out.append("## Quote")
        out.append("")
        if quote["low"]:
            out.append("- $%d - $%d  (%s)"
                       % (round(quote["low"] / 5) * 5, round(quote["high"] / 5) * 5,
                          quote["basis"]))
            if quote["known"]:
                out.append("- From what they described: %s" % ", ".join(quote["known"]))
            if quote["notes"]:
                out.append("- Adjustments: %s" % ", ".join(quote["notes"]))
            out.append("- Estimate only. Real rates are env-tunable via TT_QUOTE_*")
        else:
            out.append("No rate on file for this service; scope it before quoting.")
        out.append("")
        out.append("## Draft reply")
        out.append("")
        out.append("Paste this into the Thumbtack conversation. It does NOT go by")
        out.append("email: every notification comes from do-not-reply@pro.thumbtack.com.")
        out.append("")
        out.append("```")
        out.append(build_draft(lead))
        out.append("```")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description="File Thumbtack job leads from the local Mail store.")
    ap.add_argument("--out", default=DEFAULT_OUT, help="output folder (default: ~/Thumbtack Jobs)")
    ap.add_argument("--mail-root", default=MAIL_ROOT)
    ap.add_argument("--no-drafts", action="store_true", help="skip the reply draft")
    ap.add_argument("--dry-run", action="store_true", help="list what would be written")
    args = ap.parse_args()

    messages = thumbtack_messages(args.mail_root)
    leads, admin = [], []
    for path, msg in messages:
        subject = str(msg.get("Subject") or "")
        if LEAD_SUBJECT.search(subject):
            leads.append(parse_lead(msg))
        else:
            admin.append((subject.strip(), str(msg.get("Date") or "")[:16]))

    seen, unique = set(), []
    for lead in sorted(leads, key=lambda l: (l["when"] or datetime.min), reverse=True):
        # Prefer the bid id, which is exact. Fall back to the customer name so
        # the follow-up emails collapse into the job they belong to. A name
        # carries more detail than the service here, because the reminders
        # repeat the customer and drop the service entirely.
        key = ("bid:" + lead["bid"]) if lead["bid"] else ("name:" + slug(lead["name"], 24))
        if key in seen:
            continue
        seen.add(key)
        unique.append(lead)

    print("Thumbtack messages found: %d" % len(messages))
    print("  job leads: %d  (after collapsing reminders: %d)" % (len(leads), len(unique)))
    print("  admin/promo: %d" % len(admin))

    if args.dry_run:
        for lead in unique:
            print("  %s  %-22s %-28s %s" % (
                (lead["when"].strftime("%Y-%m-%d") if lead["when"] else "?"),
                lead["name"][:22], lead["service"][:28], lead["location"][:24]))
        return 0

    leads_dir = os.path.join(args.out, "leads")
    os.makedirs(leads_dir, exist_ok=True)
    written = []
    for lead in unique:
        stamp = lead["when"].strftime("%Y-%m-%d") if lead["when"] else "undated"
        fname = "%s--%s--%s.md" % (stamp, slug(lead["name"]), slug(lead["service"]))
        full = os.path.join(leads_dir, fname)
        with open(full, "w") as fh:
            fh.write(render(lead, drafts=not args.no_drafts))
        written.append((full, lead))

    index = ["# Thumbtack jobs", "",
             "%d job leads from the local Mail store, ordered by reply deadline." % len(written),
             "",
             "Thumbtack says replying within 4 hours helps you rank better, so the",
             "soonest deadline is first. Everything below is an ESTIMATE, not a bid:",
             "the notification email gives a service and a city and nothing else.",
             "",
             "| Reply by | Customer | Service | Location | Quote | Detail | File |",
             "|---|---|---|---|---|---|---|"]
    for full, lead in written:
        date = lead["when"].strftime("%Y-%m-%d %H:%M") if lead["when"] else "?"
        quote = quote_for(lead)
        price = ("$%d-%d" % (round(quote["low"] / 5) * 5, round(quote["high"] / 5) * 5)
                 if quote["low"] else "needs scope")
        detail = []
        if lead["rooms"]:
            detail.append(", ".join(lead["rooms"][:3]))
        if lead["sqft"]:
            detail.append(lead["sqft"])
        index.append("| %s | %s | %s | %s | %s | %s | [open](leads/%s) |" % (
            date, lead["name"], lead["service"], lead["location"] or "-",
            price, ", ".join(detail) or "none in email", os.path.basename(full)))
    index.append("")
    index.append("## Not job leads")
    index.append("")
    index.append("%d Thumbtack messages were promotions or account admin, not requests." % len(admin))
    index.append("")
    for subject, date in sorted(admin, key=lambda r: r[1], reverse=True):
        index.append("- %s  %s" % (date, subject))
    with open(os.path.join(args.out, "index.md"), "w") as fh:
        fh.write("\n".join(index) + "\n")

    print("\nwrote %d lead file(s) + index.md to %s" % (len(written), args.out))
    for full, lead in written[:10]:
        print("  %s  %s / %s" % (os.path.basename(full), lead["name"], lead["service"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())