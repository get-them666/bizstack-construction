"""Idempotent schema for the shared lead pipeline.

Both apps already create the ``leads`` table themselves at startup and then run
their own ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` statements. Restructuring
the pipeline needs a handful of new columns to hold the derived queue state, and
the rule is: the app owns its table, this package only *adds* the columns it
depends on. Every statement here is safe to run on every boot, against a table
that is already populated, and is a no-op once applied.

Keeping the DDL in the package rather than copying it into both ``main.py`` and
``construction_main.py`` is the whole point -- there is exactly one definition of
"what columns does the lead pipeline need".
"""
from __future__ import annotations

# Columns the queue state lives in. Each entry is (name, sql-type + default).
# They are denormalised copies of things derivable from ``comms_logs``, cached
# so the owner's queue is a single indexed query rather than a per-lead join.
LEAD_COLUMNS = (
    # Which of ACTION_* the owner should act on, and when it is due.
    ("next_action", "VARCHAR(20)"),
    ("next_action_at", "TIMESTAMP WITH TIME ZONE"),
    # Cached timestamps so the queue and the lifecycle rules agree.
    ("last_outbound_at", "TIMESTAMP WITH TIME ZONE"),
    ("last_inbound_at", "TIMESTAMP WITH TIME ZONE"),
    ("touch_count", "INTEGER DEFAULT 0"),
    # A person-level stop flag, independent of stage: they asked us to stop, or
    # an owner flagged them. Checked before any send, in both apps.
    ("do_not_contact", "BOOLEAN DEFAULT FALSE"),
    # Stable identity for dedupe, e.g. "permit:vb:2024-00123" or
    # "reddit:t3_abc123". Unique per source so the same lead seen twice updates
    # rather than duplicates.
    ("source_ref", "VARCHAR(255)"),
)

_INDEXES = (
    # The queue query: "company X, action Y, soonest first".
    "CREATE INDEX IF NOT EXISTS idx_leads_queue "
    "ON leads(company, next_action, next_action_at);",
    # Dedupe lookup.
    "CREATE INDEX IF NOT EXISTS idx_leads_source_ref "
    "ON leads(source_ref);",
)


def _table_exists(cur, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) AS t;", (name,))
    return cur.fetchone()["t"] is not None


def ensure_schema(db) -> None:
    """Add the pipeline columns to an existing ``leads`` table. Commit-safe.

    Does nothing if ``leads`` does not exist yet -- the app creates it first, and
    guessing a full table definition here would fight the app's own CREATE.
    """
    with db.cursor() as cur:
        if not _table_exists(cur, "leads"):
            return
        for name, ddl in LEAD_COLUMNS:
            cur.execute(f"ALTER TABLE leads ADD COLUMN IF NOT EXISTS {name} {ddl};")
        # comms_logs.lead_id is how activity attaches to a lead; older schemas
        # logged sends before that column existed.
        if _table_exists(cur, "comms_logs"):
            cur.execute("ALTER TABLE comms_logs ADD COLUMN IF NOT EXISTS lead_id INTEGER;")
        for stmt in _INDEXES:
            cur.execute(stmt)
        db.commit()


def create_schema(db) -> None:
    """Create a minimal, standalone schema.

    Used by the package's own tests and by any tool that wants the pipeline
    without booting an app. The columns are the *subset* the pipeline reads and
    writes; on the real database the app's richer table already exists and this
    is skipped.
    """
    with db.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255),
                phone VARCHAR(50) DEFAULT '',
                email VARCHAR(255),
                address VARCHAR(255),
                description TEXT,
                project_type VARCHAR(120),
                source VARCHAR(50) DEFAULT 'website',
                company VARCHAR(40),
                status VARCHAR(50) DEFAULT 'new',
                notes TEXT,
                draft_reply TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS comms_logs (
                id SERIAL PRIMARY KEY,
                direction VARCHAR(20),
                channel VARCHAR(20),
                sender VARCHAR(255),
                recipient VARCHAR(255),
                message_body TEXT,
                lead_id INTEGER,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                lead_id INTEGER,
                amount_cents INTEGER,
                currency VARCHAR(10) DEFAULT 'usd',
                status VARCHAR(50) DEFAULT 'paid',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        db.commit()
    ensure_schema(db)
    # Test schema is a representative subset of the real table (which the apps
    # own). These extras exist in production's leads table, so mirror them here
    # or the WRITABLE_FIELDS filtering can't be exercised by the tests.
    for name, ddl in (
        ("budget", "VARCHAR(80)"),
        ("timeline", "VARCHAR(120)"),
        ("referral_code", "VARCHAR(80)"),
        ("campaign", "VARCHAR(80)"),
        ("listing_url", "VARCHAR(512)"),
        ("analysis_json", "TEXT"),
        ("funding_needed", "VARCHAR(255)"),
        ("funding_use", "TEXT"),
        ("partner_id", "INTEGER"),
        ("property_sqft", "INTEGER"),
        ("estimate_low_cents", "INTEGER"),
        ("estimate_high_cents", "INTEGER"),
        ("estimate_json", "TEXT"),
    ):
        with db.cursor() as cur:
            cur.execute(f"ALTER TABLE leads ADD COLUMN IF NOT EXISTS {name} {ddl};")
    db.commit()
