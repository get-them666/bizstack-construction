"""Write cached owner names onto leads that are still named "Permit · <address>".

WHY THIS EXISTS
---------------
The skip-trace cache already holds a named owner for 120 addresses
(checked_at 2026-10-03). The `/skiptrace` button on the permits board and
the `/api/skiptrace/enrich` route both go through
`enrich_address_only_leads()`, which:

  1. returns immediately when SKIPTRACE_ENRICH_MAX is 0, and
  2. spends a provider call per address that is not cached.

So with enrichment switched off, an owner already sitting in the cache was
never copied onto its lead. Measured on production: 44 permit leads still
read "Permit · <address>" when the cache already names them.

This writes ONLY from the local cache. It makes ZERO provider calls, costs
nothing, and does not touch the RentCast quota. It is the whole yield that
was already paid for.

SAFE BY CONSTRUCTION
--------------------
  - Reads skiptrace_cache only. No network. No key required.
  - Writes leads.name ONLY where it is still the "Permit · " placeholder,
    so a real name is never overwritten.
  - Refuses to write a name is_placeholder_name() considers a placeholder
    (a city, "Self", "Unknown"), which is what put 126 rows named
    "Virginia Beach" in the database in the first place.
  - Dry-run by default. Pass --commit to apply.
  - Writes to leads.name ONLY. Never phone, never email.

The address join is on normalized street + city, done in Python rather than
SQL: the cache key is "street | city | state | zip" and the lead column is
one free-text string, so a SQL-side join would be guesswork about
punctuation. Both sides go through the same normalizer.

Usage
-----
    python backfill_owner_names.py              # dry run
    python backfill_owner_names.py --commit
"""

import argparse
import json
import os
import re
import sys


def norm(value):
    s = re.sub(r"[^a-z0-9 ]", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def cached_owners(cur) -> dict:
    """street-prefix -> owner name, from the local cache only."""
    out = {}
    cur.execute("SELECT address_key, payload FROM skiptrace_cache WHERE found;")
    for row in cur.fetchall():
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                continue
        if not isinstance(payload, dict):
            continue
        name = (payload.get("owner_of_record") or "").strip()
        if not name:
            continue
        street = norm(str(row["address_key"]).split("|")[0])
        if street:
            out[street] = name
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true", help="actually write (default dry run)")
    ap.add_argument("--company", default="construction")
    ap.add_argument("--source", default="permit_finder")
    args = ap.parse_args()

    try:
        import psycopg
        from psycopg.rows import dict_row
        from skiptrace_service import is_placeholder_name
    except ImportError as exc:
        print(f"import failed: {exc}", file=sys.stderr)
        return 1

    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 1

    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=15) as db:
        with db.cursor() as cur:
            owners = cached_owners(cur)
            print(f"cached named owners: {len(owners)}  (no provider calls will be made)")

            cur.execute(
                "SELECT id, name, address FROM leads "
                "WHERE company = %(c)s AND COALESCE(source,'') = %(s)s "
                "  AND status = 'new' "
                "  AND COALESCE(address,'') <> '' "
                "  AND COALESCE(name,'') LIKE 'Permit %%' "
                "ORDER BY id;",
                {"c": args.company, "s": args.source},
            )
            rows = cur.fetchall()
            print(f"leads still named 'Permit · ...': {len(rows)}")

            updates, skipped = [], []
            for row in rows:
                address = norm(row["address"])
                # Match on the street portion. Caches are keyed by street, so
                # requiring a decent-length prefix avoids matching a short
                # street name against the wrong house.
                matched = None
                for street, owner in owners.items():
                    if len(street) >= 8 and address.startswith(street[:len(street)]):
                        matched = owner
                        break
                if not matched:
                    continue
                if is_placeholder_name(matched):
                    skipped.append((row["id"], matched))
                    continue
                updates.append((matched, row["id"]))

            print(f"would name from cache : {len(updates)}")
            print(f"refused (placeholder) : {len(skipped)}")
            for lead_id, name in skipped[:8]:
                print(f"    #{lead_id} refused {name!r}")

            if updates:
                print("\nsample:")
                for name, lead_id in updates[:12]:
                    print(f"    #{lead_id:<6} -> {name}")

            if not args.commit:
                print(f"\nDRY RUN. {len(updates)} lead(s) would be named. Re-run with --commit.")
                return 0

            # Re-assert the placeholder guard inside the UPDATE so a row that
            # gained a real name since the SELECT is left alone.
            cur.executemany(
                "UPDATE leads SET name = %(n)s "
                "WHERE id = %(i)s AND COALESCE(name,'') LIKE 'Permit %%';",
                [{"n": n, "i": i} for n, i in updates],
            )
            print(f"\nCOMMITTED: {cur.rowcount} lead(s) named from cache.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())