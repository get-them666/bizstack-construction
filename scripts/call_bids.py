"""Call the live bid contacts with the AI voice agent.

These are commercial GCs with open bids — business calls, not residential
telemarketing. Each call gets the project name as context so the assistant can
follow up on the actual bid instead of asking "what project?".

    python scripts/call_bids.py                 # dry run
    python scripts/call_bids.py --apply         # dial
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse

import psycopg
from psycopg.rows import dict_row

import outbound_campaign
import signalwire_service
import suppression

OPEN_STATUSES = ("sent", "submitted", "shortlist")

SQL = """
SELECT DISTINCT ON (right(regexp_replace(coalesce(contact_phone,''), '\\D', '', 'g'), 10))
       id, name, contact_name, contact_phone, status, due_date, contact_email
  FROM bid_opportunities
 WHERE company = 'construction'
   AND coalesce(contact_phone, '') <> ''
   AND lower(coalesce(status, '')) = ANY(%(open)s)
 ORDER BY right(regexp_replace(coalesce(contact_phone,''), '\\D', '', 'g'), 10),
          due_date NULLS LAST, id DESC
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args(argv)

    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    wire = signalwire_service.SignalWireService()
    base = (os.getenv("APP_BASE_URL")
            or f"https://{os.getenv('COMPANY_DOMAIN', 'construction.bizstackperks.com')}").rstrip("/")

    if args.apply:
        ok, why = outbound_campaign.in_call_window()
        if not ok:
            print("not calling:", why)
            return 1
        if not wire.is_configured():
            print("SignalWire is not configured")
            return 1

    dialed, skipped = [], []
    with psycopg.connect(db_url, row_factory=dict_row) as db:
        with db.cursor() as cur:
            cur.execute(SQL, {"open": list(OPEN_STATUSES)})
            rows = cur.fetchall()[: max(1, args.limit)]

        for row in rows:
            raw_phone = re.split(r"(?i)\s*(?:ext\.?|extension|x|#)\s*\d*",
                                 str(row.get("contact_phone") or ""))[0]
            to = signalwire_service.e164(raw_phone)
            label = {"bid_id": row["id"], "company": row.get("contact_name") or row["name"],
                     "phone": to, "status": row.get("status")}
            if not to:
                skipped.append({**label, "reason": "no usable phone"})
                continue
            try:
                allowed, why = suppression.check_outbound(
                    db, suppression.CHANNEL_VOICE, to,
                    source="bid", name=row.get("contact_name") or "")
            except Exception as exc:
                skipped.append({**label, "reason": f"suppression check failed: {exc}"})
                continue
            if not allowed:
                skipped.append({**label, "reason": why})
                continue
            if not args.apply:
                dialed.append({**label, "dry_run": True, "topic": row["name"]})
                continue
            query = urllib.parse.urlencode({
                "to": to,
                "name": row.get("contact_name") or "",
                "topic": f"the bid for {row['name']}",
            })
            sid = wire.create_ai_outbound_call(to, f"{base}/comms/outbound-voice.swml?{query}")
            if not sid:
                skipped.append({**label, "reason": "carrier did not accept the call"})
                continue
            try:
                with db.cursor() as cur:
                    cur.execute(
                        "INSERT INTO bot_calls (call_sid, lead_id, direction, status) "
                        "VALUES (%s, NULL, 'outbound', 'dialed') ON CONFLICT (call_sid) DO NOTHING;",
                        (sid,))
                    cur.execute(
                        "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
                        "VALUES ('outbound', 'voice', 'system', %s, %s);",
                        (to, f"Bid follow-up call: {row['name']}"))
                db.commit()
            except Exception as exc:
                db.rollback()
                print("record failed:", str(exc)[:120])
            dialed.append({**label, "call_sid": sid, "topic": row["name"]})

    print(json.dumps({"ok": True, "applied": args.apply,
                      "dialed": dialed, "skipped": skipped}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
