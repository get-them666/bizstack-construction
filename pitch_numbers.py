#!/usr/bin/env python3
"""Pull the real numbers you need for the $50K loan pitch from your live database.

Read-only. Safe to run against production. Works against EITHER database:
run it from the buildstack-construction folder (construction tables) or the
bizstack-hosts folder (Broom Service / hospitality tables) — it auto-detects.

Usage:
    cd <project folder>
    source .venv/bin/activate
    export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
    python pitch_numbers.py

Get DATABASE_URL from your Railway project: Postgres service -> Variables -> DATABASE_URL.
"""

import os
import sys

try:
    import psycopg
except ImportError:
    sys.exit("psycopg not installed. Run: pip install 'psycopg[binary]'")


def money(cents):
    return f"${(cents or 0) / 100:,.0f}"


def one(cur, sql, default=(0,)):
    "Run a one-row query, tolerating a missing table/column."
    try:
        cur.execute(sql)
        row = cur.fetchone()
        return row if row is not None else default
    except Exception:
        return default


def main():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit(
            "Set DATABASE_URL first, e.g.\n"
            "  export DATABASE_URL='postgresql://user:pass@host:5432/db'"
        )

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public';"
            )
            tables = {r[0] for r in cur.fetchall()}

            construction = "projects" in tables and "job_leads" in tables
            hospitality = "bookings" in tables and "ledger_entries" in tables

            if not construction and not hospitality:
                print("No recognizable Buildstack or Broom Service tables found.")
                print("Tables present:", ", ".join(sorted(tables)) or "(none)")
                return

            print("\n=== BizStack pitch numbers (from your live database) ===\n")

            if construction:
                print("-- Buildstack Construction --")
                (total_leads,) = one(cur, "SELECT COUNT(*) FROM leads;")
                (quotes,) = one(
                    cur,
                    "SELECT COUNT(*) FROM leads "
                    "WHERE estimate_low_cents IS NOT NULL AND estimate_low_cents > 0;",
                )
                open_leads, pipeline_cents = one(
                    cur,
                    "SELECT "
                    " COUNT(*) FILTER (WHERE status NOT IN ('completed','lost')), "
                    " COALESCE(SUM(estimate_high_cents) "
                    "   FILTER (WHERE status NOT IN ('completed','lost')), 0) "
                    "FROM leads;",
                    (0, 0),
                )
                (projects,) = one(cur, "SELECT COUNT(*) FROM projects;")
                permits, permit_value = one(
                    cur,
                    "SELECT COUNT(*), COALESCE(SUM(estimated_value), 0) "
                    "FROM job_leads WHERE is_demo = FALSE;",
                    (0, 0),
                )
                print(f"  Leads captured.................. {total_leads}")
                print(f"  Quotes issued (estimate built).. {quotes}")
                print(f"  Open / live leads............... {open_leads}")
                print(f"  Pipeline value (open, quoted)... {money(pipeline_cents)}")
                print(f"  Converted projects.............. {projects}")
                print(
                    f"  Real permits found (Job Finder). {permits}"
                    f"  (est. value ${float(permit_value or 0):,.0f})"
                )
                print()

            if hospitality:
                print("-- Broom Service (hospitality) --")
                (hosts,) = one(cur, "SELECT COUNT(*) FROM hosts;")
                (customers,) = one(cur, "SELECT COUNT(*) FROM customers;")
                (bookings,) = one(cur, "SELECT COUNT(*) FROM bookings;")
                (workers,) = one(
                    cur, "SELECT COUNT(*) FROM workers;"
                ) if "workers" in tables else (0,)
                rev, exp = one(
                    cur,
                    "SELECT "
                    " COALESCE(SUM(amount_cents) FILTER (WHERE tx_type IN ('income','revenue')),0), "
                    " COALESCE(SUM(amount_cents) FILTER (WHERE tx_type='expense'),0) "
                    "FROM ledger_entries;",
                    (0, 0),
                )
                print(f"  Hosts........................... {hosts}")
                print(f"  Customers....................... {customers}")
                print(f"  Bookings........................ {bookings}")
                print(f"  Workers......................... {workers}")
                print(f"  Revenue (ledger)................ {money(rev)}")
                print(f"  Expenses (ledger)............... {money(exp)}")
                print(f"  Net............................. {money((rev or 0) - (exp or 0))}")
                print()

    print("Paste these into SBA_7a_LOAN_PITCH.md and the outreach emails.")
    print("If a number is zero, don't hide it and don't inflate it — it's telling you")
    print("what activity to generate before you apply.\n")


if __name__ == "__main__":
    main()
