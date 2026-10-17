"""Bid opportunities — solicitations BuildStack is pursuing.

Deliberately separate from `leads`. The `leads` table is inbound-customer intake:
creating a row there fires `auto_reply.auto_reply_to_lead` and the outbound voice
agent (`_follow_up_new_lead`). A GC contact is not a customer, so a bid must never
land in that table. Nothing in this module sends email, dials, or schedules follow-up.

Own module with its own `ensure_schema()` rather than growing construction_main.py,
which is already 8k lines. Nothing here is imported by the startup DDL block, so
the ALTER-before-CREATE ordering trap at :393-409 cannot be reached.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import date, datetime, timedelta, timezone

# Bid lifecycle. `drafting` -> `sent` -> `shortlist` -> `won` / `lost` / `withdrawn`.
STATUSES = [
    "new",
    "drafting",
    "sent",
    "shortlist",
    "won",
    "lost",
    "withdrawn",
]

# Which role we are playing. Determines bonding, risk, and who holds the RFI.
ROLES = ["prime", "sub"]

ROLE_NOTES = {
    "prime": "We hold the contract. Bond, RFI, insurance and references are ours.",
    "sub": "Another GC holds the contract. We price a trade package to them.",
}

# Incoming webhook payloads. Only these keys are read; anything else in the body is
# ignored rather than mass-assigned, so a sender cannot set id/company/status freely.
_WEBHOOK_FIELDS = {
    "name", "owner", "contact_name", "contact_email", "contact_phone",
    "reference", "role", "trade", "location", "due_date", "bid_amount",
    "status", "notes", "external_url",
}


def webhook_secret() -> str:
    """Shared secret for inbound webhooks. Empty means the endpoint refuses everything."""
    return (os.getenv("BIDS_WEBHOOK_SECRET", "") or "").strip()


def webhook_enabled() -> bool:
    return bool(webhook_secret())


def sign_payload(raw: str, secret: str | None = None) -> str:
    """HMAC-SHA256 over the raw body, hex. Same value the sender must send in X-Bids-Signature."""
    secret = secret if secret is not None else webhook_secret()
    if not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_signature(raw: str, signature: str, secret: str | None = None) -> bool:
    """Constant-time compare. Returns False if unconfigured -- fail closed."""
    secret = secret if secret is not None else webhook_secret()
    if not secret or not signature:
        return False
    sig = signature.strip().lower()
    if sig.startswith("sha256="):
        sig = sig[7:]
    return hmac.compare_digest(sig, sign_payload(raw, secret))


def _coerce_date(v):
    """Accept ISO date/datetime or the common US forms. Returns None on anything else."""
    if v is None or v == "":
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _coerce_amount(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^0-9.\-]", "", str(v))
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


LOCAL_SEED_FILE = os.getenv("BIDS_LOCAL_SEED", "bids_local.json")


def load_local_bids(path: str | None = None) -> list[dict]:
    """Read the local seed file. Raises FileNotFoundError so the route can 404.

    The file is gitignored on purpose -- it carries live contract pricing and GC
    contacts, which must not land in a public repo. Missing file is a normal
    state, not an error worth logging loudly.
    """
    p = path or os.getenv("BIDS_LOCAL_SEED", LOCAL_SEED_FILE)
    if not os.path.exists(p):
        raise FileNotFoundError(p)
    with open(p, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data.get("bids") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("seed file must contain a 'bids' list")
    return rows


def upsert_many(conn, rows: list[dict], company: str = "construction") -> dict:
    """Upsert a list of bid dicts. Returns counts plus per-row errors.

    status IS applied here, unlike the webhook path -- this is an explicit
    owner action ("load my bids"), not an untrusted sender.
    """
    return _upsert(conn, rows, company, apply_status=True)


def upsert_pulled(conn, rows: list[dict], company: str = "construction") -> dict:
    """Upsert rows fetched from an external source. Never touches status.

    An external listing does not know we shortlisted, priced, won or lost a
    solicitation, so a pull that reset status would quietly destroy the
    pipeline it was supposed to refresh. Status is honoured on insert only.

    Also stamps first_seen_at (insert only) and last_seen_at (every touch), so
    a returned list can report which solicitations are genuinely new.
    """
    return _upsert(conn, rows, company, apply_status=False)


def _upsert(conn, rows: list[dict], company: str = "construction",
            apply_status: bool = True) -> dict:
    created = updated = 0
    errors: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or not str(row.get("name", "")).strip():
            errors.append({"name": row.get("name") if isinstance(row, dict) else None,
                           "error": "name is required"})
            continue
        vals = {k: row.get(k) for k in _WEBHOOK_FIELDS if k in row}
        vals["role"] = str(vals.get("role") or "sub").strip().lower()
        vals["status"] = str(vals.get("status") or "new").strip().lower()
        if vals["role"] not in ROLES:
            errors.append({"name": vals.get("name"), "error": f"role must be one of {ROLES}"})
            continue
        if not apply_status:
            # Pull path: whatever status the source claims is only a default
            # for a row we have never seen. Validate it only if it was given.
            if vals["status"] not in STATUSES:
                vals["status"] = "new"
        elif vals["status"] not in STATUSES:
            errors.append({"name": vals.get("name"),
                           "error": f"status must be one of {STATUSES}"})
            continue
        for k in ("name", "owner", "contact_name", "contact_email",
                  "contact_phone", "reference", "trade", "location", "notes"):
            vals[k] = str(vals.get(k) or "").strip()

        # A pull must not blank a field it simply did not return. Only overwrite
        # with a non-empty value; that keeps partial feeds from erasing notes
        # the owner typed in themselves.
        if not apply_status:
            updates = []
            params: list = []
            for k in ("name", "owner", "reference", "trade", "location", "notes"):
                if vals.get(k):
                    updates.append(f"{k} = EXCLUDED.{k}")
                    params.append(vals[k])
            if _num_ok(vals.get("bid_amount")) is not None:
                updates.append("bid_amount = EXCLUDED.bid_amount")
                params.append(_coerce_amount(vals.get("bid_amount")))
            if _coerce_date(vals.get("due_date")) is not None:
                updates.append("due_date = EXCLUDED.due_date")
                params.append(_coerce_date(vals.get("due_date")))
            updates.append("last_seen_at = now()")
            set_clause = ", ".join(updates)
        else:
            set_clause = (
                "  name = EXCLUDED.name, owner = EXCLUDED.owner, "
                "  contact_name = EXCLUDED.contact_name, contact_email = EXCLUDED.contact_email, "
                "  contact_phone = EXCLUDED.contact_phone, role = EXCLUDED.role, "
                "  due_date = EXCLUDED.due_date, bid_amount = EXCLUDED.bid_amount, "
                "  status = EXCLUDED.status, location = EXCLUDED.location, "
                "  notes = EXCLUDED.notes, updated_at = now(), "
            "  external_url = COALESCE(NULLIF(EXCLUDED.external_url,''), bid_opportunities.external_url)"
            )

        # Build the column list as a dict so it cannot contain duplicates.
        # due_date, bid_amount and status are in _WEBHOOK_FIELDS *and* were
        # being appended explicitly, which produced
        #   ERROR: column "due_date" specified more than once
        # and failed every row in the batch.
        colvals: dict = {k: vals.get(k) for k in vals}
        colvals["due_date"] = _coerce_date(vals.get("due_date"))
        colvals["bid_amount"] = _coerce_amount(vals.get("bid_amount"))
        colvals["status"] = vals["status"]
        colvals["company"] = company
        colvals["source"] = "pull" if not apply_status else "local"
        # Stamped on insert. first_seen_at is deliberately absent from every
        # ON CONFLICT set clause, so it records when we first ever saw the
        # solicitation and stays true across later pulls -- that is what makes
        # the board's "New" badge mean "new to us" rather than "new today".
        colvals["first_seen_at"] = datetime.now(timezone.utc)
        if not apply_status:
            colvals["last_seen_at"] = datetime.now(timezone.utc)
        cols = list(colvals)
        sql = (
            f"INSERT INTO bid_opportunities ({','.join(cols)}) "
            f"VALUES ({','.join(['%s'] * len(cols))}) "
            f"ON CONFLICT (company, COALESCE(reference,''), COALESCE(trade,'')) "
            f"DO UPDATE SET {set_clause} "
            f"RETURNING id, (xmax = 0) AS inserted"
        )
        params = [colvals[c] for c in cols]
        try:
            # Savepoint per row. Without one, a single malformed row aborts the
            # whole transaction and every later row fails with
            # "current transaction is aborted" -- so one bad record silently
            # emptied the batch instead of being skipped.
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    row = cur.fetchone()
            if row["inserted"]:
                created += 1
            else:
                updated += 1
        except Exception as e:
            errors.append({"name": vals.get("name"), "error": str(e)})
    conn.commit()
    return {"created": created, "updated": updated, "errors": errors,
            "loaded": created + updated}


def _num_ok(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def start_pull(conn, source: str, company: str = "construction") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bid_pull_runs (source, company) VALUES (%s, %s) RETURNING id",
            (source, company))
        rid = cur.fetchone()["id"]
    conn.commit()
    return rid


def finish_pull(conn, run_id: int, fetched: int = 0, created: int = 0,
                updated: int = 0, ok: bool = True, error: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE bid_pull_runs SET finished_at = now(), fetched = %s, "
            "created_count = %s, updated_count = %s, ok = %s, error = %s WHERE id = %s",
            (fetched, created, updated, ok, (error or "")[:2000] or None, run_id))
    conn.commit()


def recent_pulls(conn, company: str = "construction", limit: int = 8) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, source, started_at, finished_at, fetched, created_count, "
            "updated_count, ok, error FROM bid_pull_runs WHERE company = %s "
            "ORDER BY started_at DESC LIMIT %s", (company, limit))
        return cur.fetchall()


def upsert_from_webhook(conn, payload: dict, company: str = "construction") -> dict:
    """Create or update one bid from a webhook body. Idempotent on (reference, trade).

    On conflict, quantity/contact/dates/notes are refreshed but ``status`` is NOT.
    The webhook creates the record; the owner drives the bidding workflow
    (drafting -> sent -> shortlist -> won). A sender cannot walk a bid to "won".

    Returns {"id", "created", "status_kept", "error"}. Never touches `leads` and
    never sends anything outbound.
    """
    if not isinstance(payload, dict):
        return {"id": None, "created": False, "status_kept": None,
                "error": "payload must be an object"}

    fields = {k: payload[k] for k in _WEBHOOK_FIELDS if k in payload}

    name = str(fields.get("name", "")).strip()
    if not name:
        return {"id": None, "created": False, "status_kept": None, "error": "name is required"}

    role = str(fields.get("role", "sub") or "sub").strip().lower()
    if role not in ROLES:
        return {"id": None, "created": False, "status_kept": None,
                "error": f"role must be one of {ROLES}"}

    status = str(fields.get("status", "new") or "new").strip().lower()
    if status not in STATUSES:
        return {"id": None, "created": False, "status_kept": None,
                "error": f"status must be one of {STATUSES}"}

    due = _coerce_date(fields.get("due_date"))
    amount = _coerce_amount(fields.get("bid_amount"))

    vals = {
        "name": name,
        "owner": str(fields.get("owner", "") or "").strip(),
        "contact_name": str(fields.get("contact_name", "") or "").strip(),
        "contact_email": str(fields.get("contact_email", "") or "").strip(),
        "contact_phone": str(fields.get("contact_phone", "") or "").strip(),
        "reference": str(fields.get("reference", "") or "").strip(),
        "role": role,
        "trade": str(fields.get("trade", "") or "").strip(),
        "location": str(fields.get("location", "") or "").strip(),
        "notes": str(fields.get("notes", "") or "").strip(),
    }

    cols = list(vals) + ["due_date", "bid_amount", "status", "company"]
    sql = (
        f"INSERT INTO bid_opportunities ({','.join(cols)}, source) "
        f"VALUES ({','.join(['%s'] * len(cols))}, 'webhook') "
        f"ON CONFLICT (company, COALESCE(reference,''), COALESCE(trade,'')) "
        f"DO UPDATE SET "
        f"  name = EXCLUDED.name, owner = EXCLUDED.owner, "
        f"  contact_name = EXCLUDED.contact_name, contact_email = EXCLUDED.contact_email, "
        f"  contact_phone = EXCLUDED.contact_phone, due_date = EXCLUDED.due_date, "
        f"  bid_amount = EXCLUDED.bid_amount, location = EXCLUDED.location, "
        f"  notes = EXCLUDED.notes, updated_at = now(), "
        f"  external_url = COALESCE(NULLIF(EXCLUDED.external_url,''), bid_opportunities.external_url) "
        f"RETURNING id, (xmax = 0) AS inserted"
    )
    params = [vals[c] for c in vals] + [due, amount, status, company]

    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        inserted = bool(row.get("inserted", True))
        kept = None
        if not inserted:
            cur.execute(
                "SELECT status FROM bid_opportunities WHERE id = %s AND company = %s;",
                (row["id"], company),
            )
            prev = cur.fetchone()
            kept = prev["status"] if prev else None
    conn.commit()
    return {
        "id": row["id"],
        "created": inserted,
        "status_kept": kept,
        "error": None,
    }


def _safe(name: str) -> str:
    """Quote an identifier for interpolation. psycopg has no identifier params."""
    if not re.fullmatch(r"[a-z_]+", name or ""):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


def ensure_schema(conn) -> None:
    """Idempotent. Safe to call on every boot."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bid_opportunities (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                owner VARCHAR(255),
                contact_name VARCHAR(255),
                contact_email VARCHAR(255),
                contact_phone VARCHAR(50),
                reference VARCHAR(120),
                role VARCHAR(20) DEFAULT 'sub',
                trade VARCHAR(120),
                location VARCHAR(255),
                due_date DATE,
                bid_amount NUMERIC(14,2),
                status VARCHAR(30) DEFAULT 'new',
                notes TEXT,
                source VARCHAR(40) DEFAULT 'manual',
                company VARCHAR(40) DEFAULT 'construction',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # ADDs only. Nothing is referenced by a CREATE in this module, so there is
        # no ordering constraint to violate the way the startup block has one.
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS owner VARCHAR(255);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS contact_name VARCHAR(255);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS contact_email VARCHAR(255);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS contact_phone VARCHAR(50);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS reference VARCHAR(120);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS role VARCHAR(20) DEFAULT 'sub';")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS trade VARCHAR(120);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS location VARCHAR(255);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS due_date DATE;")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS bid_amount NUMERIC(14,2);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS status VARCHAR(30) DEFAULT 'new';")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS notes TEXT;")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS source VARCHAR(40) DEFAULT 'manual';")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS company VARCHAR(40) DEFAULT 'construction';")
        # Where the bid actually lives, so the board can deep-link out to it.
        # BuildingConnected accepts
        #   https://app.buildingconnected.com/opportunities/{id}/info
        # with {id} being the `id` from GET opportunities. Without a column for
        # it there was nowhere to put that URL, so every bid on the board was a
        # dead card -- you could read it but not open it anywhere.
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS external_url TEXT;")
        # first_seen_at never moves; last_seen_at moves on every pull. Comparing
        # them is how the board knows a solicitation is one it has never seen
        # before, which is the whole point of pulling on a button rather than
        # keeping a manually maintained list.
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMP WITH TIME ZONE;")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP WITH TIME ZONE;")
        cur.execute("UPDATE bid_opportunities SET first_seen_at = COALESCE(first_seen_at, created_at) WHERE first_seen_at IS NULL;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bid_opportunities_first_seen ON bid_opportunities(first_seen_at DESC);")
        # Pull history, so the button can report "3 new since you last looked"
        # instead of just dumping a count.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bid_pull_runs (
                id SERIAL PRIMARY KEY,
                source VARCHAR(40) NOT NULL,
                company VARCHAR(40) DEFAULT 'construction',
                started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP WITH TIME ZONE,
                fetched INTEGER DEFAULT 0,
                created_count INTEGER DEFAULT 0,
                updated_count INTEGER DEFAULT 0,
                ok BOOLEAN DEFAULT TRUE,
                error TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bid_pull_runs_company ON bid_pull_runs(company, started_at DESC);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bid_opportunities_company ON bid_opportunities(company);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bid_opportunities_status ON bid_opportunities(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bid_opportunities_due ON bid_opportunities(due_date);")
        # Dedupe key so one row per solicitation + trade per company. Kept
        # because it is cheap and makes an importer idempotent later; rows are
        # entered through the board, not seeded at boot, so nothing in this
        # module carries live pipeline into version control.
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_bid_opportunities_ref_trade
                ON bid_opportunities(company, COALESCE(reference,''), COALESCE(trade,''))
        """)
    conn.commit()


def _db_url() -> str:
    return os.getenv("DATABASE_URL", "")


def list_bids(conn, company: str = "construction", status: str = "") -> list[dict]:
    sql = ["SELECT * FROM bid_opportunities WHERE company = %s"]
    params: list = [company]
    if status:
        sql.append("AND status = %s")
        params.append(status)
    sql.append("ORDER BY due_date NULLS LAST, id DESC")
    with conn.cursor() as cur:
        cur.execute(" ".join(sql), params)
        return cur.fetchall()


def get_bid(conn, bid_id: int, company: str = "construction") -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM bid_opportunities WHERE id = %s AND company = %s;",
            (bid_id, company),
        )
        return cur.fetchone()


def create_bid(conn, company: str = "construction", **f) -> int:
    cols = [
        "name", "owner", "contact_name", "contact_email", "contact_phone",
        "reference", "role", "trade", "location", "due_date", "bid_amount",
        "status", "notes",
    ]
    use = [_safe(c) for c in cols if c in f]
    if not use:
        raise ValueError("no writable fields")
    vals = [f[c] for c in use]
    placeholders = ",".join(["%s"] * len(use))
    sql = (f"INSERT INTO bid_opportunities ({','.join(use)}, company) "
           f"VALUES ({placeholders}, %s) RETURNING id")
    with conn.cursor() as cur:
        cur.execute(sql, vals + [company])
        row = cur.fetchone()
    conn.commit()
    return row["id"]


def set_status(conn, bid_id: int, status: str, company: str = "construction") -> bool:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE bid_opportunities
               SET status = %s, updated_at = now()
             WHERE id = %s AND company = %s
        """, (status, bid_id, company))
        ok = cur.rowcount > 0
    conn.commit()
    return ok


def set_bid_amount(conn, bid_id: int, amount, company: str = "construction") -> bool:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE bid_opportunities
               SET bid_amount = %s, updated_at = now()
             WHERE id = %s AND company = %s
        """, (amount, bid_id, company))
        ok = cur.rowcount > 0
    conn.commit()
    return ok


def add_note(conn, bid_id: int, note: str, company: str = "construction") -> bool:
    """Append to notes. Bids accrue context; overwriting it loses the trail."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE bid_opportunities
               SET notes = CASE
                              WHEN notes IS NULL OR notes = '' THEN %s
                              ELSE notes || E'\\n---\\n' || %s
                          END,
                   updated_at = now()
             WHERE id = %s AND company = %s
        """, (note, note, bid_id, company))
        ok = cur.rowcount > 0
    conn.commit()
    return ok


def delete_bid(conn, bid_id: int, company: str = "construction") -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM bid_opportunities WHERE id = %s AND company = %s;",
            (bid_id, company),
        )
        ok = cur.rowcount > 0
    conn.commit()
    return ok


def stats(conn, company: str = "construction") -> dict:
    """Priced value by status. Counts only -- no contact data leaves this module."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT status,
                   COUNT(*)                AS n,
                   COALESCE(SUM(bid_amount), 0) AS total
              FROM bid_opportunities
             WHERE company = %s
          GROUP BY status
        """, (company,))
        rows = cur.fetchall()
    out = {
        "by_status": {r["status"]: {"count": r["n"], "total": float(r["total"])} for r in rows},
        "open": sum(r["n"] for r in rows if r["status"] not in ("won", "lost", "withdrawn")),
        "open_value": sum(float(r["total"]) for r in rows
                          if r["status"] not in ("won", "lost", "withdrawn")),
    }
    return out