"""Import past email recipients into Kit as subscribers.

Used for the number-correction broadcast, and reusable for the newsletter.
Kit's API is HTTPS, so this works while Zoho's SMTP/API sending is throttled.

    python scripts/kit_import.py                     # dry run
    python scripts/kit_import.py --apply --tag number-correction

Env: KIT_API_KEY (Kit → Settings → Advanced → API).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import psycopg
import requests
from psycopg.rows import dict_row

API = "https://api.kit.com/v4"

SQL = """
SELECT DISTINCT ON (lower(l.email)) l.id, l.name, l.email
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
 ORDER BY lower(l.email), l.id DESC
 LIMIT %(limit)s
"""


def _headers() -> dict:
    return {"X-Kit-Api-Key": (os.getenv("KIT_API_KEY") or "").strip(),
            "Content-Type": "application/json", "Accept": "application/json"}


def _tag_id(name: str) -> int | None:
    r = requests.get(f"{API}/tags", headers=_headers(), timeout=30)
    if r.status_code != 200:
        print("tags read failed:", r.status_code, r.text[:120])
        return None
    for tag in (r.json().get("tags") or []):
        if (tag.get("name") or "").lower() == name.lower():
            return int(tag["id"])
    r = requests.post(f"{API}/tags", headers=_headers(), json={"name": name}, timeout=30)
    if r.status_code in (200, 201):
        return int(r.json()["tag"]["id"])
    print("tag create failed:", r.status_code, r.text[:120])
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--tag", default="")
    args = ap.parse_args(argv)

    if not (os.getenv("KIT_API_KEY") or "").strip():
        print("KIT_API_KEY is not set (Kit → Settings → Advanced → API)", file=sys.stderr)
        return 2
    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    tag_id = _tag_id(args.tag) if (args.apply and args.tag) else None
    if args.tag and args.apply and not tag_id:
        return 2

    added = skipped = failed = 0
    with psycopg.connect(db_url, row_factory=dict_row) as db:
        with db.cursor() as cur:
            cur.execute(SQL, {"limit": max(1, args.limit)})
            rows = cur.fetchall()
        print(f"audience: {len(rows)} | apply={args.apply} | tag={args.tag or '-'}")
        for row in rows:
            email = (row.get("email") or "").strip()
            if not email:
                continue
            if not args.apply:
                print(" would add:", email)
                continue
            first = (row.get("name") or "").strip().split()[0] if row.get("name") else ""
            r = requests.post(f"{API}/subscribers", headers=_headers(),
                              json={"email_address": email, "first_name": first,
                                    "state": "active"}, timeout=30)
            if r.status_code in (200, 201):
                added += 1
                sub_id = (r.json().get("subscriber") or {}).get("id")
            elif r.status_code in (409, 422):
                skipped += 1
                sub_id = None
            else:
                failed += 1
                print(" add failed:", email, r.status_code, r.text[:100])
                continue
            if tag_id and sub_id:
                requests.post(f"{API}/tags/{tag_id}/subscribers/{sub_id}",
                              headers=_headers(), timeout=30)
            if (added + skipped) % 25 == 0:
                print(f"  ... {added} added, {skipped} existing, {failed} failed")
            time.sleep(0.6)

    print(f"done: added={added} existing={skipped} failed={failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
