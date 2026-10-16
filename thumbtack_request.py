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
import os
import re
import sys

FIELDS = {
    "bedrooms": "Number of bedrooms",
    "bathrooms": "Number of bathrooms",
    "cleaning_type": "Cleaning type",
    "extras": "Extra services",
    "pets": "Pets",
    "frequency": "Frequency",
    "zip": "Zip code",
    "scheduling": "Scheduling preferences",
    "expires": "Job expires",
}

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
    lowered = [l.lower() for l in lines]
    result = {}
    labels = {v.lower(): k for k, v in FIELDS.items()}

    for idx, low in enumerate(lowered):
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
    return result, ids


def number(value, default=None):
    m = re.search(r"\d+", value or "")
    return int(m.group(0)) if m else default


def quote(detail):
    """Price from what the page actually says, with the reasoning shown."""
    cleaning = (detail.get("cleaning_type") or "").strip()
    service = cleaning.lower()
    if not service:
        return None
    match = next((k for k in BASE if k in service), None)
    if match is None:
        for k in BASE:
            if any(tok in service for tok in k.split()):
                match = k
                break
    if match is None:
        return None
    low, high, basis = BASE[match]

    beds = number(detail.get("bedrooms"))
    baths = number(detail.get("bathrooms"))
    notes = []
    if beds or baths:
        rooms = (beds or 0) + (baths or 0)
        low, high = low + rooms * 12, high + rooms * 16
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
        low += add_low
        high += add_high
        notes.append("extra services: %s" % extras_raw[:70])

    pets = (detail.get("pets") or "").lower()
    if "pet" in pets and "no pet" not in pets:
        low += 15
        high += 25
        notes.append("pets in home")

    recurring = ""
    freq = (detail.get("frequency") or "").lower()
    if freq and freq not in ("once", "one time", "one-time", "n/a", ""):
        rec_low, rec_high = low * 0.88, high * 0.88
        recurring = "$%d - $%d per visit" % (round(rec_low / 5) * 5, round(rec_high / 5) * 5)
        notes.append("%s (recurring rate shown separately)" % freq)

    return {
        "low": round(low / 5) * 5,
        "high": round(high / 5) * 5,
        "basis": basis,
        "notes": notes,
        "recurring": recurring,
    }


def match_lead(detail, lead_dir):
    """Find the lead file this request page belongs to.

    Emails do not always carry a request id, so this matches on the zip. Zip is
    the primary key because the job's own label often differs from the service
    the notification used: this page says cleaning type "Deep cleaning" while
    the lead is filed as "House Cleaning", and those are the same job. Service
    is only a tiebreaker, and an ambiguous match is reported rather than guessed.
    """
    if not os.path.isdir(lead_dir):
        return None, []
    m = re.search(r"\b(\d{5})\b", detail.get("zip", "") or "")
    if not m:
        return None, []
    zip5 = m.group(1)
    kind = set((detail.get("cleaning_type") or "").lower().split())
    candidates = []
    for fn in sorted(os.listdir(lead_dir)):
        if not fn.endswith(".md"):
            continue
        path = os.path.join(lead_dir, fn)
        body = open(path).read()
        if zip5 not in body:
            continue
        sm = re.search(r"- Service: (.+)", body)
        service = set(sm.group(1).strip().lower().split()) if sm else set()
        overlap = kind & service
        candidates.append((len(overlap), path))
    if not candidates:
        return None, []
    candidates.sort(key=lambda t: -t[0])
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None, [p for _, p in candidates]
    return candidates[0][1], []


def apply_to_lead(path, detail, ids, q):
    body = open(path).read()
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
        target, ambiguous = match_lead(detail, args.leads)
        if ambiguous:
            print("  match:  AMBIGUOUS on zip %s, not written:" % re.search(r"\d{5}", detail.get("zip","")).group(0))
            for cand in ambiguous:
                print("           %s" % os.path.basename(cand))
        elif not target:
            print("  match:  no lead file matched on zip")
        elif args.commit:
            print("  match:  %s" % apply_to_lead(target, detail, ids, q))
        else:
            print("  match:  %s (pass --commit to write)" % os.path.basename(target))
    return 0


if __name__ == "__main__":
    sys.exit(main())