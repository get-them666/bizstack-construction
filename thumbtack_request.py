"""Parse saved Thumbtack request pages into structured lead detail, and requote.

WHY A SAVED PAGE
----------------
A Thumbtack request page renders client-side and is login-gated, so neither
webfetch nor the public URL returns the job. Saving the page from a browser that
is already logged in captures the rendered DOM, which does contain the detail,
as plain label/value text.

WHY LABEL/VALUE AND NOT KEYWORD SCRAPING
----------------------------------------
A first pass that scanned the whole page for room names reported "attic,
basement, garage" for this lead. Those came from the sidebar navigation
("Garage, Basement or Attic Cleaning" targeting notices), not from the job.
The detail lives in label/value pairs, so extraction is anchored on the labels.
A field that is not found is left empty rather than guessed.

WHAT IT ADDS TO THE QUOTE
-------------------------
The email gives a service and a city. The page adds bedrooms, bathrooms,
whether it is a standard or deep clean, extra services, pets, and how often.
Those are exactly the inputs that decide a price, so the quote moves from a
generic range to a defensible one. Recurring frequency is surfaced as a
recurring rate rather than silently priced as a one-off.

Usage
-----
    python3 thumbtack_request.py --saved ~/bizstack-hosts/thumbtack
    python3 thumbtack_request.py --saved <dir> --commit
"""

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime

FIELDS = {
    "bedrooms": "Number of bedrooms",
    "bathrooms": "Number of bathrooms",
    "rooms": "Number of rooms",
    "cleaning_type": "Cleaning type",
    "extras": "Extra services",
    "stains": "Stains to clean",
    "method": "Cleaning method",
    "pets": "Pets",
    "smokers": "Smokers in home",
    "stairs": "Carpeted stairs",
    "frequency": "Frequency",
    "zip": "Zip code",
    "scheduling": "Scheduling preferences",
    "expires": "Job expires",
}

# Thumbtack asks each service a different set of questions. A carpet lead is
# asked about rooms and stains, not bedrooms and cleaning type, so a single
# fixed label set silently returns nothing for most services. Aliases are
# matched case-insensitively and the first hit wins.
ALIASES = {
    "bedrooms": ["Number of bedrooms", "Bedrooms", "How many bedrooms"],
    "bathrooms": ["Number of bathrooms", "Bathrooms", "How many bathrooms"],
    "rooms": ["Number of rooms", "Rooms", "How many rooms"],
    "cleaning_type": ["Cleaning type", "Type of cleaning"],
    "extras": ["Extra services", "Additional services", "Other services"],
    "stains": ["Stains to clean", "Stain types"],
    "method": ["Cleaning method", "Preferred cleaning method"],
    "pets": ["Pets", "Pets in home"],
    "smokers": ["Smokers in home", "Smoking in home"],
    "stairs": ["Carpeted stairs", "Stairs"],
    "frequency": ["Frequency", "How often"],
    "zip": ["Zip code", "ZIP"],
    "scheduling": ["Scheduling preferences", "Availability"],
    "expires": ["Job expires", "Expires"],
}

HEADER = re.compile(r"^([A-Z]{2})\s*-\s*(?:Open|Closed)\s+Lead$", re.I)

EXTRA_PRICES = {
    "baseboard": 40,
    "oven": 40,
    "refrigerator": 35,
    "fridge": 35,
    "interior window": 50,
    "exterior window": 90,
    "cabinet": 45,
    "laundry": 25,
    "dishes": 30,
    "wall": 35,
}

BASE = {
    "house cleaning": (120, 220, "standard clean, standard 3 bed / 2 bath"),
    "deep clean": (185, 340, "deep clean, 3 bed / 2 bath"),
    "carpet cleaning": (120, 240, "up to 3 rooms"),
    "pressure washing": (150, 340, "driveway, deck or siding"),
    "gutter cleaning": (120, 240, "up to 2 stories"),
    "upholstery": (90, 180, "per piece, 2 pieces minimum"),
    "floor cleaning": (150, 300, "hardwood or tile, up to 400 sqft"),
    "home organizing": (150, 400, "per room"),
}


def visible_lines(raw):
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", "\n", text)
    text = html.unescape(text)
    out = []
    for line in text.split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if line and (not out or out[-1] != line):
            out.append(line)
    return out


def extract(raw):
    """Pull the label/value pairs. Anything absent stays absent."""
    lines = visible_lines(raw)
    result = {}
    initials = service = ""

    for idx, line in enumerate(lines):
        head = HEADER.match(line)
        if head and not initials:
            initials = head.group(1).upper()
            if idx + 1 < len(lines):
                service = lines[idx + 1]

    labels = {}
    for key, names in ALIASES.items():
        for name in names:
            labels[name.lower()] = key

    for idx, low in enumerate([l.lower() for l in lines]):
        key = labels.get(low)
        if not key or key in result:
            continue
        values = []
        for nxt in lines[idx + 1: idx + 7]:
            if nxt.lower() in labels:
                break
            values.append(nxt)
        if values:
            joined = " ".join(values).strip()
            if key == "expires":
                m = re.search(r"At\s*[^P]*?on\s*\d{2}-\d{2}-\d{2}", joined)
                if m:
                    joined = m.group(0)
            result[key] = joined

    ids = {}
    m = re.search(r"request_pk[=/](\d+)", raw)
    if m:
        ids["request_pk"] = m.group(1)
    m = re.search(r"invite_pk=(\d+)", raw)
    if m:
        ids["invite_pk"] = m.group(1)
    m = re.search(r"thumbtack\.com/bid/(\d+)", raw)
    if m:
        ids["bid"] = m.group(1)
    if initials:
        ids["initials"] = initials
    if service:
        result["service"] = service.strip()
    return result, ids


def number(value, default=None):
    m = re.search(r"\d+", value or "")
    return int(m.group(0)) if m else default


STAIN_PRICES = {
    "pet": 25, "food": 20, "drink": 15, "wine": 15, "coffee": 15,
    "oil": 20, "grease": 20, "ink": 20, "rust": 20,
}


def quote(detail):
    """Price from what the page actually says, with the reasoning shown."""
    service = (detail.get("service") or "").strip().lower()
    cleaning = (detail.get("cleaning_type") or "").strip()
    rooms = number(detail.get("rooms"))
    kind = " ".join(x for x in (service, cleaning) if x).lower()
    if not kind and not rooms:
        return None

    notes = []

    # Carpet leads are priced per room plus stain surcharges, because that is
    # how the service is actually sold and how the customer thinks about it.
    if "carpet" in kind or "rug" in kind or "upholstery" in kind:
        if not rooms:
            return None
        base_low, base_high = 55 * rooms, 80 * rooms
        basis = "carpet cleaning, %d room%s" % (rooms, "" if rooms == 1 else "s")
        add_low = add_high = 0
        hits = []
        stains = (detail.get("stains") or detail.get("extras") or "").lower()
        for key, price in STAIN_PRICES.items():
            if key in stains:
                add_low += price
                add_high += price + 10
                hits.append(key)
        if hits:
            notes.append("stains: %s" % ", ".join(hits))
        stairs = (detail.get("stairs") or "").lower()
        if "yes" in stairs or "carpeted" in stairs and "no" not in stairs:
            add_low += 40
            add_high += 60
            notes.append("carpeted stairs")
        base_low += add_low
        base_high += add_high
    else:
        # The customer's chosen cleaning type decides the price, so it is
        # matched before the service. Matching on the combined string let
        # "house cleaning" win over "deep clean" purely on dict order, which
        # underquoted a deep clean at the standard-clean rate.
        candidates = []
        for source in (cleaning.lower(), service):
            if not source:
                continue
            hit = next((k for k in BASE if k in source), None)
            if hit:
                candidates.append(hit)
        match = candidates[0] if candidates else None
        if match is None:
            for k in BASE:
                if any(tok in kind for tok in k.split()):
                    match = k
                    break
        if match is None:
            return None
        base_low, base_high, basis = BASE[match]
        beds = number(detail.get("bedrooms"))
        baths = number(detail.get("bathrooms"))
        if beds or baths:
            rooms_n = (beds or 0) + (baths or 0)
            base_low += rooms_n * 12
            base_high += rooms_n * 16
            basis = "%s, %s bd / %s ba" % (match, beds or "?", baths or "?")
            notes.append("%s bedrooms, %s bathrooms" % (beds or "?", baths or "?"))

        extras_raw = detail.get("extras") or ""
        add_low = add_high = 0
        hit = []
        for key, price in EXTRA_PRICES.items():
            if key in extras_raw.lower():
                add_low += price
                add_high += price + 15
                hit.append(key)
        if hit:
            base_low += add_low
            base_high += add_high
            notes.append("extra services: %s" % extras_raw[:70])

    pets = ((detail.get("pets") or "") + " " + (detail.get("smokers") or "")).lower()
    if re.search(r"(?<!no )pets?\b", pets) and "don't have pets" not in pets \
            and "no pets" not in pets:
        base_low += 15
        base_high += 25
        notes.append("pets in home")

    recurring = ""
    freq = (detail.get("frequency") or "").lower()
    if freq and freq not in ("once", "one time", "one-time", "n/a", ""):
        recurring = "$%d - $%d per visit" % (round(base_low * 0.88 / 5) * 5,
                                             round(base_high * 0.88 / 5) * 5)
        notes.append("%s (recurring rate shown separately)" % freq)

    return {
        "low": round(base_low / 5) * 5,
        "high": round(base_high / 5) * 5,
        "basis": basis,
        "notes": notes,
        "recurring": recurring,
    }


def match_lead(detail, ids, lead_dir):
    """Find the lead file this request page belongs to.

    The page header carries the customer's initials ("NH - Open Lead"), and the
    lead files are named from those same initials, so that is the primary key:
    it is exact where a zip is not. Two different jobs on one street share a
    zip, which is why two pages both landed on 23456 and neither was written.

    Zip is the fallback for a page with no initials header, with the service
    used to break ties. An ambiguous match is reported, never guessed.
    """
    if not os.path.isdir(lead_dir):
        return None, []
    initials = ids.get("initials", "")
    zip5 = (re.search(r"\b(\d{5})\b", detail.get("zip", "") or "") or [None]) \
        and re.search(r"\b(\d{5})\b", detail.get("zip", "") or "").group(1)
    service = (detail.get("service") or "General").strip()

    if initials:
        want = initials.lower()
        pat = re.compile(r"(^|-)%s-%s--" % (want[0], want[1]))
        hits = [os.path.join(lead_dir, fn) for fn in sorted(os.listdir(lead_dir))
                if fn.endswith(".md") and pat.search(fn.lower())]
        if len(hits) == 1:
            return hits[0], []
        if len(hits) > 1:
            return None, hits

    if not zip5:
        return None, []
    kind = set((detail.get("cleaning_type") or detail.get("service") or "").lower().split())
    candidates = []
    for fn in sorted(os.listdir(lead_dir)):
        if not fn.endswith(".md"):
            continue
        path = os.path.join(lead_dir, fn)
        body = open(path).read()
        if zip5 not in body:
            continue
        sm = re.search(r"- Service: (.+)", body)
        svc = set(sm.group(1).strip().lower().split()) if sm else set()
        candidates.append((len(kind & svc), path))
    if not candidates:
        return None, []
    candidates.sort(key=lambda t: -t[0])
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None, [p for _, p in candidates]
    return candidates[0][1], []


def create_lead(lead_dir, detail, ids, q):
    """Write a lead file for a job that no email produced.

    Some leads never reach the leads folder because the notification email
    never synced. The request page carries strictly more detail than the email
    would have: bedrooms, bathrooms, cleaning type, extras, expiry and a quote.
    Dropping it because the match failed would lose the best information
    available, so the file is created instead and marked as page-sourced.
    """
    initials = ids.get("initials", "")
    slug_name = "%s-%s" % (initials[0].lower(), initials[1].lower()) if len(initials) == 2 \
        else "unknown"
    service = (detail.get("service") or "General").strip()
    fname = "page--%s--%s.md" % (
        slug_name, re.sub(r"[^a-z0-9]+", "-", service.lower()).strip("-"))
    path = os.path.join(lead_dir, fname)
    if os.path.exists(path):
        return path
    who = "%s. %s." % (initials[0], initials[1]) if len(initials) == 2 else "Thumbtack customer"
    lines = ["# %s - %s" % (who, service), "",
             "- Received: (from the request page; no notification email on file)",
             "- Service: %s" % service]
    if detail.get("zip"):
        lines.append("- Location: ZIP %s" % detail["zip"])
    lines.append("- Contact details: not included by Thumbtack")
    if ids.get("request_pk"):
        lines.append("- Conversation: https://www.thumbtack.com/request/%s" % ids["request_pk"])
    lines.append("")
    lines.append("## From the request page")
    lines.append("")
    for key, label in FIELDS.items():
        if detail.get(key):
            lines.append("- %s: %s" % (label, detail[key][:140]))
    if q:
        lines.append("")
        lines.append("### Quote")
        lines.append("")
        lines.append("- **$%d - $%d** (%s)" % (q["low"], q["high"], q["basis"]))
        for note in q["notes"]:
            lines.append("  - %s" % note)
        if q["recurring"]:
            lines.append("  - recurring: %s" % q["recurring"])
    lines.append("")
    os.makedirs(lead_dir, exist_ok=True)
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    return path


ENRICH_DIR = os.path.expanduser("~/Thumbtack Jobs/enriched")


def sidecar_path(initials):
    """Page detail lives beside the lead files, not inside them.

    thumbtack_jobs.py regenerates every lead file on each run, so detail
    appended into the markdown was destroyed by whatever ran next and the
    quote silently fell back to the generic rate. A sidecar keyed on the
    customer initials survives regeneration, and both scripts read the same
    file, so the order they run in stops mattering.
    """
    if not initials:
        return None
    os.makedirs(ENRICH_DIR, exist_ok=True)
    return os.path.join(ENRICH_DIR, "%s.json" % initials.lower())


def write_sidecar(detail, ids, q):
    path = sidecar_path(ids.get("initials", ""))
    if not path:
        return None
    payload = {"ids": ids, "detail": detail, "quote": q,
               "saved": datetime.now().isoformat(timespec="seconds")}
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2, default=str)
    return path


def read_sidecar(initials):
    path = sidecar_path(initials)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return None


def apply_to_lead(path, detail, ids, q):
    body = open(path).read()
    write_sidecar(detail, ids, q)
    if "## From the request page" in body:
        return "already applied"
    lines = ["", "## From the request page", ""]
    if ids:
        lines.append("- Thumbtack request id: %s"
                     % ", ".join("%s=%s" % kv for kv in ids.items()))
    for key, label in FIELDS.items():
        if detail.get(key):
            lines.append("- %s: %s" % (label, detail[key][:140]))
    if q:
        lines.append("")
        lines.append("### Requoted from the page detail")
        lines.append("")
        lines.append("- **$%d - $%d** (%s)" % (q["low"], q["high"], q["basis"]))
        for note in q["notes"]:
            lines.append("  - %s" % note)
        if q["recurring"]:
            lines.append("  - recurring: %s" % q["recurring"])
    lines.append("")
    with open(path, "a") as fh:
        fh.write("\n".join(lines))
    return "updated %s" % os.path.basename(path)


def initials_path(lead_dir, initials):
    """The lead file for these initials, if one exists."""
    if not initials or not os.path.isdir(lead_dir):
        return None
    want = initials.lower()
    # Anywhere in the name, not just the start: email-derived lead files are
    # date-prefixed ("2026-10-16--a-d--carpet-cleaning.md"), so a startswith
    # check silently missed every one of them and created a duplicate instead.
    pat = re.compile(r"(^|-)%s-%s--" % (want[0], want[1]))
    for fn in sorted(os.listdir(lead_dir)):
        if fn.endswith(".md") and pat.search(fn.lower()):
            return os.path.join(lead_dir, fn)
    return None


def main():
    ap = argparse.ArgumentParser(description="Parse saved Thumbtack request pages.")
    ap.add_argument("--saved", required=True, help="folder of saved HTML pages")
    ap.add_argument("--leads", default=os.path.expanduser("~/Thumbtack Jobs/leads"),
                    help="lead folder to update")
    ap.add_argument("--commit", action="store_true",
                    help="append the detail and requote onto the matched lead file")
    args = ap.parse_args()

    files = []
    for dp, _, fs in os.walk(args.saved):
        for fn in sorted(fs):
            if fn.lower().endswith((".html", ".htm")):
                files.append(os.path.join(dp, fn))
    if not files:
        print("no HTML pages in %s" % args.saved)
        return 1

    for path in files:
        raw = open(path, "rb").read().decode("utf-8", "replace")
        detail, ids = extract(raw)
        print("=" * 72)
        print("%s   %s" % (os.path.basename(path), ids or "(no ids)"))
        for key, label in FIELDS.items():
            if detail.get(key):
                print("  %-14s %s" % (key.replace("_", " "), detail[key][:90]))
        missing = [k for k in FIELDS if not detail.get(k)]
        if missing:
            print("  missing: %s" % ", ".join(missing))
        q = quote(detail)
        if not q:
            print("  quote: no rate for this cleaning type")
            continue
        print("  QUOTE   $%d - $%d  (%s)" % (q["low"], q["high"], q["basis"]))
        for note in q["notes"]:
            print("          - %s" % note)
        if q["recurring"]:
            print("  RECUR   %s" % q["recurring"])
        target, ambiguous = match_lead(detail, ids, args.leads)
        if ambiguous:
            existing = initials_path(args.leads, ids.get("initials", ""))
            if existing:
                print("  match:  %s (zip %s is ambiguous, so initials won)"
                      % (os.path.basename(existing),
                         re.search(r"\d{5}", detail.get("zip", "") or "").group(0)))
            elif args.commit:
                made = create_lead(args.leads, detail, ids, q)
                print("  match:  zip ambiguous across %d leads, so created page file %s"
                      % (len(ambiguous), os.path.basename(made)))
            else:
                print("  match:  zip ambiguous and no lead carries these initials;")
                print("           pass --commit to create a page-sourced file")
        elif target:
            if args.commit:
                print("  match:  %s" % apply_to_lead(target, detail, ids, q))
            else:
                print("  match:  %s (pass --commit to write)" % os.path.basename(target))
        elif args.commit:
            made = create_lead(args.leads, detail, ids, q)
            print("  match:  no email lead existed, created %s" % os.path.basename(made))
        else:
            print("  match:  no email lead exists; pass --commit to create one")
    return 0


if __name__ == "__main__":
    sys.exit(main())