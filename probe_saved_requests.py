"""Probe saved Thumbtack request pages: did the browser save the detail, or just a JS shell?

WHY THIS EXISTS
---------------
A Thumbtack request page renders its detail client-side, so "Save Page" may
produce a shell with no job text in it. This reports which of the two actually
happened before any parser is written against a structure that might not exist.

It also reports the id fields it can see, because those are what let a saved
page be matched back to a lead file.

Usage
-----
    python3 probe_saved_requests.py
    python3 probe_saved_requests.py ~/Thumbtack\\ Jobs/requests
"""

import html
import os
import re
import sys

ROOM = re.compile(
    r"\b(kitchen|dining room|living room|master bedroom|bedroom|bathroom|basement|"
    r"garage|patio|deck|office|hallway|laundry|den|attic|porch|foyer|stairway)\b",
    re.I,
)
SQFT = re.compile(r"\b([\d,]{2,6})\s*(?:sq\.?\s?ft|sqft|square\s+feet)\b", re.I)
BEDS = re.compile(r"\b(\d)\s*(?:bed(?:room)?s?|br)\b", re.I)
BATHS = re.compile(r"\b(\d(?:\.\d)?)\s*(?:bath(?:room)?s?|ba)\b", re.I)
IDS = {
    "request_pk": re.compile(r"request_pk[=/](\d+)"),
    "bid": re.compile(r"thumbtack\.com/bid/(\d+)"),
    "negotiation": re.compile(r"negotiation(?:ID|Id)[=/\"']+(\d+)"),
    "invite": re.compile(r"invite_pk=(\d+)"),
}
NOISE = re.compile(
    r"(sign in|log in|create an account|javascript|enable javascript|"
    r"welcome back|subscribe|newsletter|privacy policy|terms of use)",
    re.I,
)


def strip_tags(raw):
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def probe(path):
    raw = open(path, "rb").read().decode("utf-8", "replace")
    text = strip_tags(raw)
    title = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    if m:
        title = html.unescape(m.group(1)).strip()[:70]

    ids = {k: v.findall(raw)[:1] for k, v in IDS.items()}
    ids = {k: v[0] for k, v in ids.items() if v}

    rooms = sorted({m.group(1).lower() for m in ROOM.finditer(text)})
    sqft = SQFT.findall(text)[:3]
    beds = BEDS.findall(text)[:3]
    baths = BATHS.findall(text)[:3]
    noise = bool(NOISE.search(text[:1500]))

    # A shell page is short, has no room/size words, and still says "log in".
    looks_shell = len(text) < 1200 and not rooms and not sqft and noise
    return {
        "path": path,
        "bytes": len(raw),
        "text": len(text),
        "title": title,
        "ids": ids,
        "rooms": rooms,
        "sqft": sqft,
        "beds": beds,
        "baths": baths,
        "shell": looks_shell,
    }


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
        "~/Thumbtack Jobs/requests")
    files = []
    for dp, _, fs in os.walk(target):
        for fn in sorted(fs):
            if fn.lower().endswith((".html", ".htm")) or fn.endswith(".webarchive"):
                files.append(os.path.join(dp, fn))
    if not files:
        print("no saved pages found in %s" % target)
        print("Save one from the browser with Cmd+S into that folder.")
        return 1

    print("found %d saved page(s)\n" % len(files))
    for path in files:
        r = probe(path)
        verdict = "JS SHELL - no job detail" if r["shell"] else "has content"
        print("=" * 72)
        print("%s\n  %s  (%d bytes, %d chars of text)" % (os.path.basename(path), verdict,
                                                          r["bytes"], r["text"]))
        if r["title"]:
            print("  title: %s" % r["title"])
        if r["ids"]:
            print("  ids:   %s" % ", ".join("%s=%s" % kv for kv in r["ids"].items()))
        else:
            print("  ids:   none found")
        if r["rooms"]:
            print("  rooms: %s" % ", ".join(r["rooms"]))
        if r["sqft"]:
            print("  sqft:  %s" % ", ".join(r["sqft"]))
        if r["beds"] or r["baths"]:
            print("  beds/baths: %s / %s" % (", ".join(r["beds"]) or "-",
                                             ", ".join(r["baths"]) or "-"))
        if not r["shell"] and not (r["rooms"] or r["sqft"]):
            print("  note:  content saved but no size/room words detected yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())