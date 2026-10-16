"""Stage local bid rows for the 'Load my current bids' button on /bids.

Reads bids_local.json (gitignored AND railwayignored) and writes the rows into
the private bid_seed_rows table, so the deployed button has something to load
without the pricing ever entering the repo or the build context.

Usage:
    venv/bin/python stage_bids.py                     # stage + load
    venv/bin/python stage_bids.py --status            # just show counts
    venv/bin/python stage_bids.py --no-load           # stage only
"""
import argparse
import json
import os
import sys

import bid_opportunities as bo

SEED_FILE = os.getenv("BIDS_LOCAL_SEED", "bids_local.json")


def _conn():
    url = os.getenv("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set. Reaching the live database needs a "
                 "tcp-proxy; see MEMORY.md.")
    import psycopg
    return psycopg.connect(url)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="show counts, write nothing")
    ap.add_argument("--no-load", action="store_true", help="stage but do not upsert into the board")
    ap.add_argument("file", nargs="?", default=SEED_FILE)
    a = ap.parse_args()

    if a.status:
        conn = _conn()
        bo.ensure_schema(conn)
        print(json.dumps(bo.seed_status(conn), indent=2))
        return 0

    if not os.path.exists(a.file):
        sys.exit(f"{a.file} not found. That file is deliberately gitignored; "
                 "it is the local source of truth for the pipeline.")
    rows = bo.load_local_bids(a.file)
    print(f"{len(rows)} bids in {a.file}")

    conn = _conn()
    bo.ensure_schema(conn)

    bad = [r for r in rows if not str(r.get("name", "")).strip()]
    if bad:
        print(f"  {len(bad)} row(s) have no name and will be skipped")
    for r in rows:
        if str(r.get("name", "")).strip():
            amt = r.get("bid_amount")
            amt = f"${amt:,.0f}" if isinstance(amt, (int, float)) else "-"
            print(f"  {r.get('trade', '?'):<32} {amt:>12}  due {r.get('due_date', '-')}")

    print("\nstaging...")
    print(" ", bo.stage_seed(conn, rows))
    print(" ", bo.seed_status(conn))

    if not a.no_load:
        print("\nloading into the board...")
        res = bo.upsert_many(conn, bo.seed_rows(conn), "construction")
        bo.mark_seed_loaded(conn)
        print(f"  created={res['created']} updated={res['updated']} errors={len(res['errors'])}")
        for e in res["errors"]:
            print(f"   ! {e}")
        print(" ", bo.seed_status(conn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())