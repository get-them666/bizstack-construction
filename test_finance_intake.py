"""The /finance intake form must create a lead, not 404.

templates/construction/finance.html has existed since before launch and the nav
links it from every page, but no route was ever registered for /finance -- so
"Financing" 404'd sitewide and the application form underneath it was
unreachable. This pins that the route exists, validates, and persists the
funding fields the rest of the app keys off.

Runs against a throwaway database. Owner notification and auto-reply are
stubbed, so nothing is sent.
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["DISABLE_PERMIT_IMPORT"] = "1"
os.environ["DISABLE_LEAD_SCAN"] = "1"
os.environ["DISABLE_LOAN_OUTREACH"] = "1"

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import construction_main as m  # noqa: E402
import auto_reply  # noqa: E402

DSN = os.environ["DATABASE_URL"]
if not DSN or ("localhost" not in DSN and "127.0.0.1" not in DSN):
    print("SKIP: set TEST_DATABASE_URL to a throwaway database")
    raise SystemExit(0)

sent = {"notify": 0, "autoreply": 0, "followup": 0}
auto_reply.notify_owner_email = lambda *a, **k: sent.__setitem__("notify", sent["notify"] + 1)
auto_reply.auto_reply_to_lead = lambda *a, **k: sent.__setitem__("autoreply", sent["autoreply"] + 1)
m._follow_up_new_lead = lambda *a, **k: sent.__setitem__("followup", sent["followup"] + 1)

EMAIL = "regression.tester@example.com"
PAYLOAD = {
    "full_name": "Regression Tester",
    "phone": "(757) 555-0199",
    "email": EMAIL,
    "project_type": "Kitchen remodel",
    "address": "123 Test Ave, Williamsburg, VA",
    "amount_needed": "27500.50",
    "when_start": "Within a month",
    "message": "Need financing to start the cabinets.",
}


def rows(sql, args=None):
    with psycopg.connect(DSN) as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def count(sql):
    return rows(sql)[0]["c"]


def main():
    # Context-managed so the lifespan runs and builds the schema, as production does.
    with TestClient(m.app) as client:
        # site_theme's app_settings table is created lazily on first theme read, but
        # base.html reads the theme on every render -- so a genuinely fresh database
        # cannot serve a page until something has touched the theme. Pre-create it so
        # this exercises the financing route, not that unrelated bootstrap gap.
        with psycopg.connect(DSN) as c, c.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT);")
            c.commit()

        # --- 1. GET renders the form instead of 404ing --------------------------
        r = client.get("/finance")
        print(f"GET /finance -> {r.status_code}")
        assert r.status_code == 200, f"expected 200, got {r.status_code}"
        assert "Apply for Financing" in r.text, "form page did not render"
        assert 'name="amount_needed"' in r.text, "amount field missing"
        print("PASS  GET /finance renders the application form")

        # --- 2. POST creates a lead carrying the funding fields ------------------
        before = dict(sent)
        with psycopg.connect(DSN) as c, c.cursor() as cur:
            cur.execute("DELETE FROM leads WHERE email = %s", (EMAIL,))
            c.commit()

        r = client.post("/finance", data=PAYLOAD, follow_redirects=False)
        print(f"\nPOST /finance -> {r.status_code} location={r.headers.get('location')}")
        assert r.status_code == 303, f"expected a redirect, got {r.status_code}"
        assert r.headers.get("location") == "/finance?sent=1", r.headers.get("location")

        row = rows("SELECT * FROM leads WHERE email = %s", (EMAIL,))
        assert len(row) == 1, f"expected 1 lead row, got {len(row)}"
        row = row[0]
        print(f"PASS  lead #{row['id']} created: {row['name']!r} / {row['project_type']!r}")
        assert row["source"] == "financing", row["source"]
        assert row["funding_needed"] is True, f"funding_needed={row['funding_needed']!r}"
        assert row["funding_amount_cents"] == 2750050, f"funding_amount_cents={row['funding_amount_cents']!r}"
        assert row["budget"] == "27500.50", row["budget"]
        print(f"PASS  funding_needed=True funding_amount_cents={row['funding_amount_cents']} budget={row['budget']!r}")

        assert sent["notify"] > before["notify"], "owner was never notified"
        assert sent["followup"] > before["followup"], "follow-up was never scheduled"
        print("PASS  owner notified and follow-up scheduled, like /submit-lead")

        # --- 3. missing required fields bounce back, creating nothing -----------
        n_before = count("SELECT count(*) AS c FROM leads WHERE source = 'financing'")
        r = client.post("/finance", data={"full_name": "", "phone": "", "project_type": ""},
                        follow_redirects=False)
        print(f"\nPOST /finance (empty) -> {r.status_code} location={r.headers.get('location')}")
        assert r.status_code == 303 and "error=" in r.headers.get("location", "")
        assert count("SELECT count(*) AS c FROM leads WHERE source = 'financing'") == n_before
        print("PASS  invalid submission rejected without creating a lead")

        # --- 4. repeat application merges instead of duplicating ----------------
        client.post("/finance", data=PAYLOAD, follow_redirects=False)
        n_after = count(f"SELECT count(*) AS c FROM leads WHERE email = '{EMAIL}'")
        print(f"\nrepeat application -> rows {1} -> {n_after}")
        assert n_after == 1, "repeat application duplicated the lead"
        print("PASS  repeat application merges into the existing lead")

        with psycopg.connect(DSN) as c, c.cursor() as cur:
            cur.execute("DELETE FROM leads WHERE email = %s", (EMAIL,))
            c.commit()

    print("\nALL financing intake checks PASSED")


# Script-style on purpose. Booting the app runs its lifespan, which builds the
# FULL schema (projects, and its foreign key onto leads). Doing that at pytest
# import time leaves test_leads_pipeline.py unable to TRUNCATE leads, so this
# stays behind __main__ like the repo's other script-style files.
if __name__ == "__main__":
    main()
