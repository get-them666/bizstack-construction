#!/usr/bin/env python3
"""Send the Hampton Roads property-manager vendor applications.

Five B2B emails, one recipient each. Item 6 of the outreach list is portal copy
for FirstService Residential / City Wide Facility Solutions -- there is no email
address for it, so it is not handled here.

Wording notes (2026-11-22):
  * No W-9, COI or DPOR license file exists in this repo or on the owner's
    machine, so every "I've attached..." line is replaced with an offer to send
    the documents on request.
  * CONTRACTOR_LICENSE is unset and the owner confirmed there is no active
    Virginia DPOR contractor licence, so "licensed through Virginia DPOR" is
    replaced with "registered as BuildStack Construction LLC". Do not put a
    licence claim back into this copy without a licence number to back it.

Usage:
    ./venv/bin/python scripts/send_vendor_applications.py --dry-run
    ./venv/bin/python scripts/send_vendor_applications.py --send
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SIGNATURE = """Best regards,
Shaun O'Leary
Owner, BuildStack Construction LLC & Broom Service
Direct: (252) 665-5891 | Construction: (757) 846-9275 | Broom Service: (757) 908-7121
Email: hello@bizstackperks.com
Chesapeake, VA"""

EMAILS = [
    {
        "key": "lawson",
        "to": "mfeehery@lawsoncompanies.com",
        "subject": "Vendor Book Application: Turnkey Construction & Final Cleaning \u2013 BuildStack Construction LLC & Broom Service",
        "body": """Hi Mike,

I'm writing to request consideration for Lawson Realty Corporation's approved Vendor Book for BuildStack Construction LLC and our sister cleaning operation, Broom Service, based right here in Hampton Roads.

We specialize in helping multifamily and residential property managers streamline unit turns and capital projects by combining contracting and final cleaning under one roof:

\u2022 BuildStack Construction LLC: Unit-turn renovations, drywall, painting, flooring, trim/carpentry, door/window replacements, and punch-list repairs.
\u2022 Broom Service: Rough and final post-construction cleanup, deep move-out/turnover cleaning, debris removal, and common-area janitorial/porter services.

Because our cleaning team works directly alongside our trades crew, we eliminate handoff delays between repairs and final presentation\u2014cutting days off unit vacancy times. We carry full General Liability and Workers' Compensation coverage and can immediately add Lawson Realty Corporation as Additional Insured upon approval.

Our completed W-9, Certificate of Insurance, and business registration are ready to send\u2014just let me know and I'll forward them for the Vendor Committee's review, along with a 1-year price list if that is helpful ahead of your next meeting.

""" + SIGNATURE,
    },
    {
        "key": "keyrenter",
        "to": "info@keyrenterhamptonroads.com",
        "subject": "Approved Vendor Inquiry: Turnkey Unit-Turn Contracting & Cleaning \u2013 BuildStack Construction LLC & Broom Service",
        "body": """Hi Brian and Team,

I'm reaching out to introduce BuildStack Construction LLC and our sister cleaning company, Broom Service. We are a local Hampton Roads contracting and cleaning team looking to partner with Keyrenter Hampton Roads as an approved maintenance and turnover vendor.

We know how tight turnarounds can be between tenants\u2014especially with your 30-day leasing guarantee\u2014so we offer a single point of contact to get properties rent-ready fast:

\u2022 BuildStack Construction LLC: General maintenance repairs, drywall, painting, flooring, carpentry, and inspection punch-list items.
\u2022 Broom Service: Move-out deep cleans, post-renovation final cleaning, and trash-out/debris hauling.

Having one team handle both the repairs and the final clean eliminates scheduling gaps and ensures properties are spotless for showings. We are registered as BuildStack Construction LLC and carry full General Liability and Workers' Compensation insurance.

Our W-9, COI, and business registration are ready for your vendor file\u2014just say the word and I'll send them over. I'd welcome the chance to quote an upcoming maintenance ticket or unit turn across the Southside or Peninsula.

""" + SIGNATURE,
    },
    {
        "key": "pmi",
        "to": "patti@pmivirginia.com",
        "subject": "Local Contracting & Turnover Cleaning Vendor \u2013 BuildStack Construction LLC & Broom Service",
        "body": """Hi Patti,

I'm reaching out to introduce BuildStack Construction LLC and Broom Service, a Hampton Roads-based construction and cleaning partnership serving residential and commercial properties across the 757.

We partner with property managers like PMI Virginia to simplify maintenance and property turnovers by combining two essential services under one roof:

\u2022 BuildStack Construction LLC: Interior/exterior repairs, drywall, painting, flooring, trim carpentry, and light commercial build-outs/maintenance.
\u2022 Broom Service: Move-in/move-out deep cleaning, post-construction cleanup, debris removal, and scheduled commercial janitorial/porter services.

Working with one coordinated team saves your coordinators from juggling multiple vendor schedules and invoices while keeping turnaround times tight. We are registered as BuildStack Construction LLC and carry full General Liability and Workers' Compensation insurance.

I can send our W-9, COI, and business registration so we can be added to your affiliated vendor network\u2014just let me know where to send them. Please also tell me if there is any additional onboarding paperwork we can complete for your team.

""" + SIGNATURE,
    },
    {
        "key": "osprey",
        "to": "info@ospreypm.com",
        "subject": "Rent-Ready Contracting & Cleaning Vendor for Osprey PM \u2013 BuildStack Construction LLC & Broom Service",
        "body": """Hi Osprey Property Management Team,

I'm reaching out to get BuildStack Construction LLC and our sister cleaning operation, Broom Service, added to your approved vendor roster for Hampton Roads.

We specialize in getting rental properties "rent-ready" quickly and reliably by handling both the repair work and the final clean with a single point of contact:

\u2022 BuildStack Construction LLC: General property maintenance, drywall, painting, flooring, door/hardware repairs, and unit-turn renovations.
\u2022 Broom Service: Deep move-out/turnover cleaning, post-construction dust and debris cleanup, and trash-outs.

Because our cleaning crew steps in immediately after our trades finish, your team avoids vendor handoff delays and gets units market-ready faster. We are registered as BuildStack Construction LLC and carry General Liability and Workers' Compensation insurance.

I can send our W-9, Certificate of Insurance, and business registration for your review. We'd love to bid on an upcoming turnover or maintenance work order whenever your team needs reliable backup.

""" + SIGNATURE,
    },
    {
        "key": "vbcdc",
        "to": "Adminservices@vbcdc.org",
        "subject": "Vendor Registration: Construction, Rehab, Cleaning & Hauling \u2013 BuildStack Construction LLC & Broom Service",
        "body": """Hi VBCDC Procurement Team,

I'm writing to introduce BuildStack Construction LLC and Broom Service and express our interest in partnering with VBCDC as an approved vendor across multiple service categories.

Based right here in Hampton Roads, our two companies provide turnkey rehabilitation, maintenance, and property support services tailored to residential and community housing portfolios:

\u2022 Construction & Rehabilitation (BuildStack Construction LLC): Interior/exterior renovations, carpentry, drywall, painting, flooring, and general property maintenance.
\u2022 Property Support & Cleaning (Broom Service): Post-rehab final cleaning, unit-turnover deep cleaning, debris removal, and hauling.

Offering both trades and post-construction cleaning/hauling under one roof allows us to deliver complete, move-in-ready units on tight schedules. We carry full General Liability and Workers' Compensation insurance and are registered as BuildStack Construction LLC.

Our W-9, COI, and business registration are ready to accompany our online vendor registration\u2014I will send them on request. Please let us know if you have any upcoming solicitations or unit turnovers we can quote.

""" + SIGNATURE,
    },
]


def load_env(path: Path) -> None:
    """Tolerant .env loader.

    python-dotenv aborts on SENDER_PHYSICAL_ADDRESS (it contains a quoted
    "unsubscribe"), so every key after line 30 is dropped. This reads the file
    line by line and keeps the plain KEY="value" pairs we need here.

    A missing file is fine: in the production container the environment is
    already populated by docker compose.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        os.environ.setdefault(key, val)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="actually deliver the mail")
    ap.add_argument("--dry-run", action="store_true", help="write previews only")
    ap.add_argument("--only", default="", help="comma-separated keys to send")
    ap.add_argument("--transport", default="auto", choices=["auto", "smtp"],
                    help="auto = app ladder (Zoho API in prod); smtp = pin the SMTP ladder")
    args = ap.parse_args()

    if not args.send and not args.dry_run:
        ap.error("choose --dry-run or --send")

    load_env(ROOT / ".env")

    import documents_service  # noqa: E402  (needs env loaded first)

    cfg = documents_service.smtp_config_from_env()
    cfg["SMTP_NAME"] = "Shaun O'Leary, BuildStack Construction"
    if not (cfg.get("SMTP_FROM") or "").strip():
        print("No SMTP_FROM configured; refusing to send.")
        return 2

    # Local SMTP works but the shared mailbox flags "unusual sending activity"
    # from a residential/cloud IP; the Zoho HTTPS API does not. "auto" lets the
    # app pick (Zoho API in the production container). "smtp" pins the SMTP
    # ladder for a deliberate test.
    os.environ["EMAIL_TRANSPORT"] = "legacy" if args.transport == "smtp" else "auto"

    only = {k.strip().lower() for k in args.only.split(",") if k.strip()}
    targets = [e for e in EMAILS if not only or e["key"] in only]
    stamp = date.today().isoformat()

    preview_dir = ROOT / "outbox" / f"vendor-applications-{stamp}"
    preview_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i, msg in enumerate(targets, 1):
        preview = preview_dir / f"{i:02d}-{msg['key']}.txt"
        preview.write_text(
            f"To: {msg['to']}\nSubject: {msg['subject']}\n\n{msg['body']}\n",
            encoding="utf-8",
        )
        if args.dry_run:
            print(f"[dry-run] {i}/{len(targets)} {msg['key']} -> {msg['to']}  ({preview})")
            results.append({"key": msg["key"], "to": msg["to"], "sent": None, "dry_run": True})
            continue

        try:
            ok = asyncio.run(
                documents_service.send_email(cfg, msg["to"], msg["subject"], msg["body"])
            )
        except Exception as exc:  # noqa: BLE001 - report, never crash mid-batch
            ok, err = False, f"{type(exc).__name__}: {exc}"
        else:
            err = "" if ok else "transport refused (see log above)"
        print(f"[{'sent' if ok else 'FAILED'}] {i}/{len(targets)} {msg['key']} -> {msg['to']}"
              f"{'' if ok else '  ' + err}")
        results.append({"key": msg["key"], "to": msg["to"], "subject": msg["subject"],
                        "sent": bool(ok), "error": err, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        if ok and i < len(targets):
            time.sleep(3)  # keep the mailbox under its rate limit

    receipt = preview_dir / ("sent.json" if args.send else "dry-run.json")
    receipt.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nreceipt: {receipt}")

    if args.send:
        failed = [r["key"] for r in results if not r["sent"]]
        if failed:
            print("FAILED: " + ", ".join(failed))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
