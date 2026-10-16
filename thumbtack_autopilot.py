"""Hourly autopilot for Thumbtack leads: file them, quote them, say what needs doing.

WHY THIS EXISTS
---------------
The manual loop was: run an extractor, open the index, read the deadlines,
open each request page, paste detail in, requote. That is four steps repeated
on a cadence Thumbtack itself imposes, since a notification says "You have N
more hours to reply" and rank depends on answering inside 4 hours.

This does the first steps on a timer and leaves a short digest, so the only
thing left is the human part: read the draft and paste it into the
Thumbtack conversation.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not reply, and it cannot. Every notification comes from
do-not-reply@pro.thumbtack.com and the conversation lives inside Thumbtack,
so an emailed reply reaches nobody. It also does not fetch request pages,
which are login-gated and client-rendered; those still have to be saved from
a browser. Anything it cannot do, it says so in the digest rather than
quietly producing an empty run.

WHAT "AUTOMATIC" ACTUALLY COVERS
--------------------------------
Automatic: reading Mail.app's store, deciding what is a job versus a promo,
collapsing the reminder emails into one lead each, ballpark quoting, drafting
a reply, and flagging deadlines. Manual: the request page, and sending.

Usage
-----
    python3 thumbtack_autopilot.py            # run once, print the digest
    python3 thumbtack_autopilot.py --quiet    # for the timer, errors only
"""

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.expanduser("~/Thumbtack Jobs")
LEADS = os.path.join(OUT, "leads")
SAVED = os.path.expanduser("~/bizstack-hosts/thumbtack")
STATE = os.path.join(OUT, ".autopilot-state.json")
PY = sys.executable

EXPIRES = re.compile(r"\*\*Reply window closed:\s*([0-9:\- ]+)\*\*")
INGEST_URL = "https://bizstackperks.com/api/leads/ingest"
SECRET_FILE = os.path.expanduser("~/subscriptions/leads_ingest.txt")

FIELD = {
    "service": re.compile(r"^- Service: (.+)$", re.M),
    "address": re.compile(r"^- Location: (.+)$", re.M),
    "zip": re.compile(r"- Zip code: (\d{5})"),
    "dates": re.compile(r"^- Dates they offered: (.+)$", re.M),
    "bedrooms": re.compile(r"^- Number of bedrooms: (.+)$", re.M),
    "bathrooms": re.compile(r"^- Number of bathrooms: (.+)$", re.M),
    "cleaning": re.compile(r"^- Cleaning type: (.+)$", re.M),
    "extras": re.compile(r"^- Extra services: (.+)$", re.M),
    "url": re.compile(r"^- Conversation: (\S+)", re.M),
}
HEADER = re.compile(r"^# (.+?) - (.+)$", re.M)


def parse_lead_file(path):
    """Turn one lead markdown file back into a structured record."""
    body = open(path).read()
    got = {}
    for key, pattern in FIELD.items():
        m = pattern.search(body)
        if m:
            got[key] = m.group(1).strip()
    head = HEADER.search(body)
    if not head:
        return None
    got["name"] = head.group(1).strip()
    got["service"] = head.group(2).strip()
    ext = got.get("url", "").rstrip("/").split("/")[-1] or os.path.basename(path)[:-3]
    parts = []
    for key in ("bedrooms", "bathrooms", "cleaning", "extras", "dates"):
        if got.get(key):
            parts.append("%s: %s" % (key, got[key]))
    return {
        "vendor": "thumbtack",
        "external_id": "tt-%s" % re.sub(r"[^a-z0-9]+", "-", ext.lower()),
        "name": got["name"],
        "service": got.get("service", ""),
        "address": got.get("address", ""),
        "zip": got.get("zip", ""),
        "url": got.get("url", ""),
        "description": "\n".join(parts),
        "campaign": "thumbtack-sync",
    }


def tls_context():
    """A python.org build on macOS can have no CA paths at all."""
    ctx = ssl.create_default_context()
    paths = ssl.get_default_verify_paths()
    if (paths.cafile and os.path.exists(paths.cafile)) or \
            (paths.capath and os.path.exists(paths.capath)):
        return ctx
    if os.path.exists("/etc/ssl/cert.pem"):
        ctx.load_verify_locations("/etc/ssl/cert.pem")
    return ctx


def push_to_site(paths):
    """Send the leads to bizstackperks. Failure is reported, never silent."""
    if not os.path.exists(SECRET_FILE):
        return None, "no secret at %s" % SECRET_FILE
    secret = open(SECRET_FILE).read().strip()
    records = [r for r in (parse_lead_file(p) for p in paths) if r]
    if not records:
        return None, "no parseable leads"
    req = urllib.request.Request(
        INGEST_URL,
        data=json.dumps({"leads": records}).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Leads-Secret": secret,
            # Cloudflare rejects urllib's default signature with error 1010,
            # so identify plainly rather than looking like a scraper.
            "User-Agent": "thumbtack-autopilot/1.0 (+bizstackperks)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45, context=tls_context()) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as exc:
        return None, "HTTP %s: %s" % (exc.code, exc.read().decode()[:200])
    except Exception as exc:
        return None, str(exc)[:200]


def needs_page(paths):
    """Leads with no request-page detail yet. These need a manual save."""
    return [os.path.basename(p) for p in paths
            if "## From the request page" not in open(p).read()]


def run(script, extra):
    cmd = [PY, os.path.join(HERE, script)] + extra
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE) as fh:
                return json.load(fh)
        except (ValueError, OSError):
            pass
    return {"seen": [], "last_run": None}


def save_state(state):
    with open(STATE, "w") as fh:
        json.dump(state, fh, indent=2)


def lead_files():
    if not os.path.isdir(LEADS):
        return []
    return sorted(os.path.join(LEADS, f) for f in os.listdir(LEADS) if f.endswith(".md"))


def expiring_within(hours):
    now = datetime.now()
    limit = now + timedelta(hours=hours)
    out = []
    for path in lead_files():
        body = open(path).read()
        m = EXPIRES.search(body)
        if not m:
            continue
        try:
            when = datetime.strptime(m.group(1).strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        if when <= limit:
            out.append((when, os.path.basename(path)))
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--warn-hours", type=int, default=12)
    ap.add_argument("--no-push", action="store_true", help="do not push to bizstackperks")
    args = ap.parse_args()

    problems = []
    before = {os.path.basename(p) for p in lead_files()}
    state = load_state()

    code, out, err = run("thumbtack_jobs.py", [])
    if code != 0:
        problems.append("thumbtack_jobs.py failed: %s" % (err.strip()[:200] or "no output"))

    if os.path.isdir(SAVED) and any(f.lower().endswith((".html", ".htm")) for f in os.listdir(SAVED)):
        code, out, err = run("thumbtack_request.py", ["--saved", SAVED, "--commit"])
        if code != 0:
            problems.append("thumbtack_request.py failed: %s" % (err.strip()[:200] or "no output"))

    after = {os.path.basename(p) for p in lead_files()}
    fresh = sorted(after - before)
    state["seen"] = sorted(after)
    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)

    urgent = expiring_within(args.warn_hours)
    synced, sync_err = (None, "skipped")
    if not args.no_push:
        synced, sync_err = push_to_site(lead_files())
    manual = needs_page(lead_files())

    if args.quiet:
        # Always write one line. A launchd job that prints nothing is
        # indistinguishable from one that died on a permission error, which is
        # exactly how the old cron backup failed silently.
        sys.stderr.write(
            "autopilot %s: leads=%d new=%d urgent=%d problems=%d\n"
            % (datetime.now().strftime("%Y-%m-%d %H:%M"), len(after),
               len(fresh), len(urgent), len(problems))
        )
        return 1 if problems else 0

    print("Thumbtack autopilot - %s" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    print("leads on file: %d   new this run: %d" % (len(after), len(fresh)))

    if fresh:
        print("\nNEW:")
        for name in fresh:
            print("  + %s" % name)
    else:
        print("\nNo new leads since the last run.")

    if urgent:
        print("\nEXPIRING WITHIN %d HOURS:" % args.warn_hours)
        for when, name in urgent:
            mins = int((when - datetime.now()).total_seconds() // 60)
            left = "EXPIRED" if mins < 0 else "%dh%02dm left" % (mins // 60, mins % 60)
            print("  %-46s %s" % (name, left))
    else:
        print("\nNothing expiring within %d hours." % args.warn_hours)

    if synced:
        print("\nSYNCED TO bizstackperks: %s" % json.dumps(synced))
    elif sync_err and sync_err != "skipped":
        print("\nSYNC FAILED: %s" % sync_err)

    if manual:
        print("\nNEEDS A SAVED REQUEST PAGE (%d):" % len(manual))
        for name in manual[:12]:
            print("  %s" % name)
        if len(manual) > 12:
            print("  ... and %d more" % (len(manual) - 12))

    print("\nSTILL MANUAL:")
    print("  request pages are login-gated; save new ones to %s" % SAVED)
    print("  replies are pasted into Thumbtack, never emailed")

    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print("  ! %s" % p)

    print("\nindex: %s" % os.path.join(OUT, "index.md"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())