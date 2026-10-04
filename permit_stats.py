"""The permit-work statistics, computed fresh from the database every time.

WHY A FUNCTION AND NOT A COUNTER
--------------------------------
A cached count drifts. Somebody works a permit, a permit refresh ingests a
new batch, the Copilot moves a card -- and a stored integer is now wrong
with nothing to notice it. So nothing here is stored. Every read is a
GROUP BY against job_leads, and every writer updates the number for free
because they all go through the same table.

This is why it stays correct no matter WHO changed the data: the owner
dragging a card on /pipeline, the Copilot via update_lead_status, the
voice agent, /api/job-leads/refresh, or a manual SQL edit.

WHAT COUNTS AS "WORK"
---------------------
"Permits to work" used to be `status NOT IN ('won','lost','closed')` over
every row, which counted three kinds of row that are not work:

  - demo rows (Chesapeake and Williamsburg fall back to clearly-marked
    demo data on purpose; 9 rows)
  - commercial permits, where a contractor is already engaged (277)
  - unreviewed rows, which are unclassified rather than residential

The headline is therefore ACTIONABLE DOORS: distinct addresses with a
real, residential, still-open permit. Doors rather than rows, because one
house routinely has several permits -- 862 permits sat at 655 doors, and
"862 permits to work on" is how a list becomes 862 knock-wasted trips.

Nothing is hidden. The excluded rows are returned too, so the UI can show
what was dropped and why.
"""

# Demo rows are never real work. Permits without an address cannot be
# worked by canvass at all -- there is no door to knock.
_NOT_WORKABLE = "NOT is_demo AND COALESCE(address, '') <> ''"


def permit_stats(cur) -> dict:
    """All permit tallies, computed live. One connection, one round trip set.

    Returns a dict the /leads view and the /api/permits/stats JSON endpoint
    both render, so the page and any live poll can never disagree.
    """
    out = {}

    # The headline: distinct doors that are real, open, residential.
    cur.execute(f"""
        SELECT count(DISTINCT upper(btrim(address))) AS doors,
               count(*) AS permits
        FROM job_leads
        WHERE {_NOT_WORKABLE}
          AND use_class = 'residential'
          AND status NOT IN ('won','lost','closed')
    """)
    r = cur.fetchone()
    out["actionable_doors"] = int(r["doors"] or 0)
    out["actionable_permits"] = int(r["permits"] or 0)

    # Per-status counts, for the column headers. Counted over the same
    # workable set so the headers sum to something meaningful.
    cur.execute(f"""
        SELECT status, count(*) AS c
        FROM job_leads
        WHERE {_NOT_WORKABLE}
        GROUP BY status
    """)
    out["workable_by_status"] = {r["status"]: int(r["c"]) for r in cur.fetchall()}

    # The breakdown, so the total is explainable rather than a bare number.
    cur.execute(f"""
        SELECT
            count(*) FILTER (WHERE use_class = 'commercial')                          AS commercial,
            count(*) FILTER (WHERE COALESCE(use_class,'') IN ('', 'unknown'))          AS unreviewed,
            count(*) FILTER (WHERE is_demo)                                            AS demo,
            count(*) FILTER (WHERE COALESCE(address,'') = '')                           AS no_address,
            count(*) FILTER (WHERE status IN ('won','lost','closed'))                   AS closed,
            count(*)                                                                   AS total_rows
        FROM job_leads
    """)
    r = cur.fetchone()
    out["commercial"] = int(r["commercial"] or 0)
    out["unreviewed"] = int(r["unreviewed"] or 0)
    out["demo"] = int(r["demo"] or 0)
    out["no_address"] = int(r["no_address"] or 0)
    out["closed"] = int(r["closed"] or 0)
    out["total_rows"] = int(r["total_rows"] or 0)

    # use_class tallies over real rows with an address, for the list-quality bar.
    cur.execute(f"""
        SELECT COALESCE(NULLIF(use_class, ''), 'unknown') AS cls, count(*) AS c
        FROM job_leads
        WHERE {_NOT_WORKABLE}
        GROUP BY 1
    """)
    out["by_use_class"] = {r["cls"]: int(r["c"]) for r in cur.fetchall()}

    # Doors per city: where the work actually is.
    cur.execute(f"""
        SELECT COALESCE(NULLIF(city, ''), 'unknown') AS city,
               count(DISTINCT upper(btrim(address))) AS doors,
               count(*) AS permits
        FROM job_leads
        WHERE {_NOT_WORKABLE}
          AND use_class = 'residential'
          AND status NOT IN ('won','lost','closed')
        GROUP BY 1
        ORDER BY doors DESC
    """)
    out["by_city"] = [
        {"city": r["city"], "doors": int(r["doors"]), "permits": int(r["permits"])}
        for r in cur.fetchall()
    ]

    # Doors already reached, so "workable" means untouched, not merely open.
    cur.execute(f"""
        SELECT count(DISTINCT upper(btrim(address))) AS c
        FROM job_leads
        WHERE {_NOT_WORKABLE}
          AND use_class = 'residential'
          AND status IN ('contacted', 'reached', 'mailed', 'knocked', 'won')
    """)
    out["touched_doors"] = int(cur.fetchone()["c"] or 0)

    out["untouched_doors"] = max(
        0, out["actionable_doors"] - out["touched_doors"]
    )
    return out


def classify_backfill(conn) -> int:
    """Re-run use_classification over rows still marked unknown.

    Wraps permit_service.backfill_classification so a caller with a raw
    connection can top up the classification without importing permit_service.
    """
    from permit_service import backfill_classification
    return backfill_classification(conn)