"""One-off: tell past email recipients the business line changed.

The old number (757-908-7121) is the Vapi line; the public line is now
757-846-9275. Every lead who received an outbound email still has the old
number. This sends one short correction, in batches, and never to someone who
opted out or is suppressed.

    python scripts/email_number_correction.py --dry-run --limit 150
    python scripts/email_number_correction.py --apply  --limit 150

Idempotent: a lead who already got the correction is skipped, so the daily run
finishes the list over a couple of days without duplicates.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

import psycopg
from psycopg.rows import dict_row

SUBJECT = "BuildStack's number changed — (757) 846-9275"
MARKER = "Our business number changed"
NEW_PHONE = "(757) 846-9275"

AUDIENCE = """
SELECT DISTINCT ON (lower(l.email))
       l.id, l.name, l.email
  FROM leads l
 WHERE l.company = 'construction'
   AND position('@' in coalesce(l.email, '')) > 1
   AND coalesce(l.opted_out_at::text, '') = ''
   AND coalesce(l.do_not_contact, false) = false
   AND lower(coalesce(l.status, 'new')) NOT IN
       ('won','lost','closed','cancelled','canceled','archived')
   AND EXISTS (SELECT 1 FROM comms_logs c
                WHERE c.lead_id = l.id AND c.channel = 'email'
                  AND c.direction = 'outbound')
   AND NOT EXISTS (SELECT 1 FROM suppression_list s
                    WHERE s.active AND s.value_type = 'email'
                      AND s.value = lower(l.email))
   AND NOT EXISTS (SELECT 1 FROM comms_logs c2
                    WHERE c2.lead_id = l.id AND c2.channel = 'email'
                      AND c2.direction = 'outbound'
                      AND c2.message_body LIKE %(marker)s)
 ORDER BY lower(l.email), l.id DESC
 LIMIT %(limit)s
"""


def body_for(name: str) -> str:
    first = (name or "").strip().split()[0] if (name or "").strip() else "there"
    import brand
    address = brand.mailing_address() or "701 Dana Dr, Chesapeake, VA 23321"
    return (
        f"Hi {first},\n\n"
        f"Quick correction: our business line is now {NEW_PHONE}. "
        f"The old 757-908-7121 is no longer the number to use, so please call or "
        f"text {NEW_PHONE} for anything about your project.\n\n"
        f"If you'd like a free estimate, just reply here or call/text {NEW_PHONE}.\n\n"
        f"Thanks,\nShaun\nBuildStack Construction LLC\n"
        f"{address}\nhello@bizstackperks.com\n\n"
        f"Reply STOP and we won't email you again."
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    import documents_service
    from auto_reply import run_coro
    cfg = documents_service.smtp_config_from_env()
    if not documents_service.smtp_configured(cfg):
        print("email transport is not configured", file=sys.stderr)
        return 2

    sent = failed = 0
    with psycopg.connect(db_url, row_factory=dict_row) as db:
        with db.cursor() as cur:
            cur.execute(AUDIENCE, {"marker": MARKER + "%", "limit": max(1, args.limit)})
            rows = cur.fetchall()
        print(f"audience: {len(rows)} lead(s); apply={args.apply}")
        for row in rows:
            to = (row.get("email") or "").strip()
            if not to:
                continue
            if not args.apply:
                print(" would send:", to, "|", row.get("name") or "")
                continue
            body = body_for(row.get("name") or "")
            try:
                ok = run_coro(documents_service.send_email(cfg, to, SUBJECT, body))
            except Exception as exc:
                print(" send failed:", to, str(exc)[:120])
                failed += 1
                continue
            if ok is not True:
                print(" transport refused:", to)
                failed += 1
                continue
            try:
                with db.cursor() as cur:
                    cur.execute(
                        "INSERT INTO comms_logs (direction, channel, sender, recipient, "
                        "message_body, lead_id) VALUES ('outbound','email',%s,%s,%s,%s);",
                        (cfg.get("SMTP_FROM") or "", to, f"{SUBJECT}\n\n{body}", row["id"]))
                db.commit()
            except Exception as exc:
                print(" logged-but-not-recorded:", to, str(exc)[:80])
                db.rollback()
            sent += 1
            print(" sent:", to, f"({sent})")
            time.sleep(float(os.getenv("CORRECTION_DELAY", "0.6") or 0.6))
    print(f"done: sent={sent} failed={failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
