"""Backfill leads.address for permit leads written before the column was set.

WHY
---
`_ingest_permits` in construction_main.py inserted permit leads with the
address written ONLY into `analysis_json`, never into the `address` column.
Every consumer that reads the column -- mail_letters.py above all -- therefore
saw these leads as having no address and skipped them.

Measured on production before this script ran:
    367 permit leads, 337 unique doors, address populated on 0 of them,
    and 19 of those doors were already in the 56-door mail pool.

The INSERT is fixed, so new permits populate the column. This repairs history.

SAFE BY CONSTRUCTION
--------------------
  - Writes ONLY leads.address, and only where that column is currently empty
    or whitespace. A non-empty value is never touched.
  - Never writes a permit lead's address onto a different row.
  - Dry-run by default. Pass --commit to apply.
  - Prints a per-address summary so the result can be eyeballed before commit.

Usage
-----
    python backfill_permit_addresses.py                # dry run
    python backfill_permit_addresses.py --commit       # apply
    python backfill_permit_addresses.py --limit 500    # dry run on a slice
"""

import argparse
import json
import os
import sys


def fetch_targets(cur, company: str, limit: int = 0) -> list:
    """Permit leads with an address available in analysis_json but not in the column."""
    sql = """
        SELECT id, address AS current_address, analysis_json
        FROM leads
        WHERE company = %(company)s
          AND email LIKE 'con-permit-%%@lead.local'
          AND (address IS NULL OR btrim(address) = '')
          AND analysis_json IS NOT NULL AND analysis_json <> ''
    """
    if limit:
        sql += " LIMIT %(limit)s"
    cur.execute(sql, {"company": company, "limit": limit} if limit else {"company": company})
    return cur.fetchall()


def propose(cur, rows: list) -> list:
    """Build (id, address) pairs, skipping any whose JSON has no usable address."""
    out, skipped = [], 0
    for r in rows:
        try:
            meta = json.loads(r["analysis_json"]) if isinstance(r["analysis_json"], str) else r["analysis_json"]
        except (TypeError, ValueError):
            meta = None
        addr = ""
        if isinstance(meta, dict):
            # Strip the separate "city" field: several feeds repeat the city
            # inside property_address, and "X, Virginia Beach, Virginia Beach,
            # VA 23455" is a worse mailing address than the one without it.
            addr = (meta.get("address") or "").strip()
            city = (meta.get("city") or "").strip().strip(",")
            if city and addr.endswith(city):
                addr = addr[: -len(city)].strip().strip(",")
        if addr:
            out.append((r["id"], addr))
        else:
            skipped += 1
    return out, skipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true", help="actually write (default is dry run)")
    ap.add_argument("--company", default="construction")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        print(f"psycopg unavailable: {exc}", file=sys.stderr)
        return 1

    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 1

    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=15) as db:
        with db.cursor() as cur:
            rows = fetch_targets(cur, args.company, args.limit)
            if not rows:
                print("Nothing to backfill — every permit lead already has an address.")
                return 0

            pairs, skipped = propose(cur, rows)
            doors = len({a.lower() for _, a in pairs})
            print(f"rows needing address : {len(rows)}")
            print(f"rows backfilled      : {len(pairs)}")
            print(f"rows skipped (no addr): {skipped}")
            print(f"unique doors         : {doors}")

            if not pairs:
                return 0

            print("\nsample (first 12):")
            for lead_id, addr in pairs[:12]:
                print(f"  #{lead_id:<6} {addr[:66]}")

            if not args.commit:
                print(f"\nDRY RUN. {len(pairs)} row(s) would be updated. Re-run with --commit.")
                return 0

            # Re-assert the guard inside the UPDATE. If anything else populated
            # address between the SELECT and here, this row is left alone.
            cur.executemany(
                "UPDATE leads SET address = %(a)s "
                "WHERE id = %(i)s AND (address IS NULL OR btrim(address) = '')",
                [{"i": i, "a": a} for i, a in pairs],
            )
            print(f"\nCOMMITTED: {cur.rowcount} row(s) updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())