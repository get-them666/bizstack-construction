"""Cost, margin, win/loss and invoicing for bid_opportunities.

One shared cost model answers all three questions:

  A. What did this job actually cost, against what we estimated?
  B. Are we winning the right jobs, and at what margin?
  C. What have we billed a won job for, and what has come back?

Everything hangs off bid_opportunities, so a bid that never converts to a
project simply has no actuals. That is the intended behaviour: a bid in
`drafting` has an estimate and nothing else.

TWO HONESTY RULES, both load-bearing:

1. Labor actual is COMPUTED from timesheets x crew.pay_rate, never entered.
   As hours are clocked the number moves, because it is derived on read. A
   stored labor total would go stale the moment someone worked an hour.

2. Materials actual CANNOT be derived. supplies_inventory holds stock on
   hand, not consumption, and there is no cost-per-unit and no link to a
   project anywhere in the schema. So materials have to be entered by hand,
   and cost_summary() reports whether they are present instead of silently
   returning labor-only as if it were the whole job. A margin that excludes
   materials is not a margin, and showing one without saying so is how a
   $54,000 paint job looks profitable at 100% margin.

Crew on salary rather than hourly is reported separately as salaried_hours
and excluded from labor_cost. Allocating an annual salary across the hours a
person happened to work this month produces a number, but it is an
allocation, not a cost of this job, and it does not belong in a margin.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

COST_CATEGORIES = ["labor", "material", "sub", "equipment", "permit", "other"]

# Categories that are derived rather than entered. labor is computed from
# timesheets; the rest must be entered as cost lines.
MANUAL_CATEGORIES = [c for c in COST_CATEGORIES if c != "labor"]

INVOICE_STATUSES = ["draft", "sent", "partial", "paid", "void"]

TERMINAL_STATUSES = ["won", "lost", "withdrawn"]


def _now():
    return datetime.now(timezone.utc)


def _money(v):
    if v in (None, ""):
        return None
    try:
        return round(float(str(v).replace("$", "").replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return None


def _dtoday():
    return date.today().isoformat()


def _table_exists(conn, name: str) -> bool:
    """Guard for tables the startup DDL owns.

    The accounting tables here are created by this module, but projects,
    timesheets and crew are created by construction_main's lifespan block. On a
    database that has not booted the app yet they are absent, and a bare
    SELECT against a missing table aborts the caller's transaction.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) AS t", (name,))
        row = cur.fetchone()
    return bool(row and row["t"])


# --- schema ------------------------------------------------------------------
def ensure_accounting_schema(conn) -> None:
    """Idempotent. Safe on every boot."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bid_costs (
                id SERIAL PRIMARY KEY,
                bid_id INTEGER NOT NULL REFERENCES bid_opportunities(id) ON DELETE CASCADE,
                category VARCHAR(30) NOT NULL DEFAULT 'material',
                description TEXT,
                vendor VARCHAR(255),
                qty NUMERIC(12,2),
                unit_cost NUMERIC(14,2),
                estimated NUMERIC(14,2),
                actual NUMERIC(14,2),
                company VARCHAR(40) DEFAULT 'construction',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("ALTER TABLE bid_costs ADD COLUMN IF NOT EXISTS vendor VARCHAR(255);")
        cur.execute("ALTER TABLE bid_costs ADD COLUMN IF NOT EXISTS qty NUMERIC(12,2);")
        cur.execute("ALTER TABLE bid_costs ADD COLUMN IF NOT EXISTS unit_cost NUMERIC(14,2);")
        cur.execute("ALTER TABLE bid_costs ADD COLUMN IF NOT EXISTS estimated NUMERIC(14,2);")
        cur.execute("ALTER TABLE bid_costs ADD COLUMN IF NOT EXISTS actual NUMERIC(14,2);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bid_costs_bid ON bid_costs(bid_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bid_costs_cat ON bid_costs(bid_id, category);")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS bid_invoices (
                id SERIAL PRIMARY KEY,
                bid_id INTEGER NOT NULL REFERENCES bid_opportunities(id) ON DELETE CASCADE,
                invoice_number VARCHAR(60),
                issued_date DATE,
                due_date DATE,
                status VARCHAR(20) DEFAULT 'draft',
                amount NUMERIC(14,2) NOT NULL DEFAULT 0,
                collected NUMERIC(14,2) NOT NULL DEFAULT 0,
                description TEXT,
                company VARCHAR(40) DEFAULT 'construction',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("ALTER TABLE bid_invoices ADD COLUMN IF NOT EXISTS collected NUMERIC(14,2) DEFAULT 0;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bid_invoices_bid ON bid_invoices(bid_id);")
        # One invoice number per company. NULL numbers are allowed to repeat,
        # which is why this is a partial index rather than a plain UNIQUE.
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_bid_invoices_number
                ON bid_invoices(company, invoice_number)
                WHERE invoice_number IS NOT NULL AND invoice_number <> ''
        """)

        # Outcome fields live on the bid so analytics is a single scan.
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS won_date DATE;")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS lost_date DATE;")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS lost_reason TEXT;")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS contract_value NUMERIC(14,2);")
        cur.execute("ALTER TABLE bid_opportunities ADD COLUMN IF NOT EXISTS won_note TEXT;")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bid_opportunities_won ON bid_opportunities(company, won_date);")

        # A won bid becomes a project, and a project's timesheets become the
        # labor actual. This is the join that makes A work without anyone
        # retyping hours.
        if _table_exists(conn, "projects"):
            cur.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS bid_id INTEGER;")
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_bid_id
                    ON projects(bid_id) WHERE bid_id IS NOT NULL
            """)
            # Conditional FK, checked from Python rather than in a DO block:
            # a DO $$ ... $$ body does not survive psycopg's statement handling
            # reliably, and this reads the same as the ALTERs above it.
            cur.execute("SELECT 1 FROM pg_constraint WHERE conname = %s "
                        "AND conrelid = 'projects'::regclass", ("projects_bid_id_fkey",))
            if not cur.fetchone():
                cur.execute("ALTER TABLE projects ADD CONSTRAINT projects_bid_id_fkey "
                            "FOREIGN KEY (bid_id) REFERENCES bid_opportunities(id) "
                            "ON DELETE SET NULL")
    conn.commit()


# --- A. cost and margin ------------------------------------------------------
def labor_actual(conn, bid_id: int, company: str = "construction") -> dict:
    """Labor cost derived from timesheets x crew.pay_rate. Read-only, always live."""
    out = {"hours": 0.0, "labor_cost": 0.0, "salaried_hours": 0.0, "timesheets": 0}
    if not (_table_exists(conn, "timesheets") and _table_exists(conn, "crew")
            and _table_exists(conn, "projects")):
        return out
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                count(*) AS n,
                COALESCE(SUM(t.hours), 0) AS hours,
                COALESCE(SUM(t.hours) FILTER (WHERE c.pay_type = 'hourly'), 0) AS hourly_hours,
                COALESCE(SUM(t.hours * c.pay_rate) FILTER (WHERE c.pay_type = 'hourly'), 0) AS labor_cost,
                COALESCE(SUM(t.hours) FILTER (WHERE c.pay_type <> 'hourly'), 0) AS salaried_hours
            FROM timesheets t
            JOIN crew c ON c.id = t.crew_id
            JOIN projects p ON p.id = t.project_id
            WHERE p.bid_id = %s AND p.company = %s
        """, (bid_id, company))
        r = cur.fetchone()
    out.update({
        "timesheets": r["n"], "hours": float(r["hours"]),
        "hourly_hours": float(r["hourly_hours"]),
        "labor_cost": float(r["labor_cost"] or 0),
        "salaried_hours": float(r["salaried_hours"]),
    })
    return out


def cost_lines(conn, bid_id: int, company: str = "construction") -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM bid_costs WHERE bid_id = %s AND company = %s ORDER BY category, id",
            (bid_id, company))
        return cur.fetchall()


def add_cost_line(conn, bid_id: int, category: str = "material", description: str = "",
                  vendor: str = "", qty=None, unit_cost=None, estimated=None,
                  actual=None, company: str = "construction") -> int:
    if category not in COST_CATEGORIES:
        raise ValueError(f"category must be one of {COST_CATEGORIES}")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO bid_costs (bid_id, category, description, vendor, qty, "
            "unit_cost, estimated, actual, company) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING id",
            (bid_id, category, description.strip() if description else None,
             vendor.strip() if vendor else None, qty, unit_cost,
             _money(estimated), _money(actual), company))
        lid = cur.fetchone()["id"]
    conn.commit()
    return lid


def delete_cost_line(conn, line_id: int, bid_id: int, company: str = "construction") -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM bid_costs WHERE id = %s AND bid_id = %s AND company = %s "
            "RETURNING id", (line_id, bid_id, company))
        ok = cur.fetchone() is not None
    conn.commit()
    return ok


def cost_summary(conn, bid_id: int, company: str = "construction") -> dict:
    """Estimated vs actual, split so the UI can show what is missing.

    complete is False when a won job has labor but no material or sub lines
    entered. Callers should say so rather than print a margin.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, trade, status, bid_amount, contract_value, won_date, lost_date "
            "FROM bid_opportunities WHERE id = %s AND company = %s", (bid_id, company))
        bid = cur.fetchone()

    lines = cost_lines(conn, bid_id, company)
    by_cat: dict[str, dict] = {}
    for ln in lines:
        c = ln["category"]
        slot = by_cat.setdefault(c, {"estimated": 0.0, "actual": 0.0})
        slot["estimated"] += float(ln["estimated"] or 0)
        slot["actual"] += float(ln["actual"] or 0)

    lab = labor_actual(conn, bid_id, company)

    entered_est = sum(v["estimated"] for v in by_cat.values())
    # Labor actual comes from timesheets, not from a cost line, so it is added
    # to whatever was entered for the labor category. If the owner also typed a
    # labor actual it is replaced, not doubled.
    entered_labor_actual = by_cat.get("labor", {}).get("actual", 0.0)
    actual_total = sum(v["actual"] for v in by_cat.values()) - entered_labor_actual + lab["labor_cost"]

    materials_entered = any(
        by_cat.get(c, {}).get("actual") for c in ("material", "sub")
    )
    revenue = None
    if bid and bid.get("contract_value"):
        revenue = float(bid["contract_value"])
    elif bid and bid.get("bid_amount") and bid.get("status") == "won":
        revenue = float(bid["bid_amount"])

    won = bool(bid and bid.get("status") == "won")
    # Before a bid is won there is no actual to speak of, so margin is
    # estimate-only and flagged as such.
    complete = (not won) or (lab["labor_cost"] > 0 and materials_entered)

    def margin(rev, cost):
        if rev in (None, 0):
            return None, None
        m = rev - cost
        return round(m, 2), round(m / rev * 100, 1)

    est_margin, est_pct = margin(float(bid["bid_amount"]) if bid and bid.get("bid_amount") else None,
                                 entered_est)
    act_margin, act_pct = margin(revenue, actual_total) if won else (None, None)

    return {
        "bid": bid,
        "lines": lines,
        "by_category": by_cat,
        "labor": lab,
        "estimated_cost": round(entered_est, 2),
        "actual_cost": round(actual_total, 2) if won else None,
        "revenue": revenue,
        "est_margin": est_margin,
        "est_margin_pct": est_pct,
        "actual_margin": act_margin,
        "actual_margin_pct": act_pct,
        "materials_entered": materials_entered,
        "complete": complete,
        "won": won,
    }


# --- B. win / loss and margin history ---------------------------------------
def set_outcome(conn, bid_id: int, status: str, company: str = "construction",
                won_date: str = "", lost_date: str = "", lost_reason: str = "",
                contract_value=None) -> None:
    if status not in ("won", "lost", "withdrawn") + tuple(["drafting", "new", "sent", "shortlist"]):
        raise ValueError("unsupported status")
    sets = ["status = %s", "updated_at = now()"]
    params: list = [status]
    if status == "won":
        sets.append("won_date = COALESCE(%s, COALESCE(won_date, %s))")
        params += [_date_or_none(won_date), _today()]
        if contract_value not in (None, ""):
            sets.append("contract_value = %s")
            params.append(_money(contract_value))
    if status == "lost":
        sets.append("lost_date = COALESCE(%s, COALESCE(lost_date, %s))")
        params += [_date_or_none(lost_date), _today()]
        sets.append("lost_reason = %s")
        params.append((lost_reason or "").strip() or None)
    params += [bid_id, company]
    with conn.cursor() as cur:
        cur.execute(f"UPDATE bid_opportunities SET {', '.join(sets)} "
                    f"WHERE id = %s AND company = %s", params)
    conn.commit()


def _today():
    return date.today()


def _date_or_none(v):
    if not v:
        return None
    try:
        return date.fromisoformat(str(v).strip())
    except ValueError:
        return None


def open_job_for_bid(conn, bid_id: int, company: str = "construction"):
    """Return the project feeding this bid's timesheets, if one exists."""
    if not _table_exists(conn, "projects"):
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM projects WHERE bid_id = %s AND company = %s",
                    (bid_id, company))
        return cur.fetchone()


def convert_bid_to_project(conn, bid_id: int, company: str = "construction",
                           start_date: str = "") -> int:
    """Turn a won bid into a project so crew hours land against it.

    Idempotent on projects.bid_id, so pressing it twice does not create a
    second project and split the hours across both.
    """
    ensure_accounting_schema(conn)
    if not _table_exists(conn, "projects"):
        raise RuntimeError("projects table does not exist on this database")
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM bid_opportunities WHERE id = %s AND company = %s",
                    (bid_id, company))
        bid = cur.fetchone()
        if not bid:
            raise ValueError("bid not found")
        cur.execute("SELECT id FROM projects WHERE bid_id = %s", (bid_id,))
        found = cur.fetchone()
        if found:
            return found["id"]
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'projects' AND column_name = 'company'")
        has_company = cur.fetchone() is not None
        name = (bid.get("name") or "Job")[:255]
        addr = (bid.get("location") or "")[:255]
        cols = ["name", "address", "status", "start_date"]
        vals = [name, addr or None, "active", _date_or_none(start_date) or bid.get("start_date")]
        if has_company:
            cols.append("company")
            vals.append(company)
        if bid.get("bid_amount"):
            cols.append("summary")
            vals.append(f"Converted from bid #{bid_id} — {bid.get('trade') or 'trade not set'}")
        cur.execute(
            f"INSERT INTO projects ({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))}) "
            f"RETURNING id", vals)
        pid = cur.fetchone()["id"]
    conn.commit()
    return pid


def analytics(conn, company: str = "construction") -> dict:
    """Win rate, margin on won work, and where the losses went.

    Margin is only reported for won bids that have both a revenue and some
    actual cost. A won bid with no timesheets yet would otherwise report 100%
    margin, which is the most dangerous number this module could produce.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT status, count(*) AS n, COALESCE(SUM(bid_amount), 0) AS amt
            FROM bid_opportunities
            WHERE company = %s AND status IS NOT NULL
            GROUP BY status
        """, (company,))
        by_status = {r["status"]: {"count": r["n"], "amount": float(r["amt"] or 0)}
                     for r in cur.fetchall()}

        decided = (by_status.get("won", {}).get("count", 0)
                   + by_status.get("lost", {}).get("count", 0))
        won = by_status.get("won", {})

    has_time = (_table_exists(conn, "timesheets") and _table_exists(conn, "crew")
                and _table_exists(conn, "projects"))
    # Without crew and timesheets there is no cost to report, so revenue only.
    labor_sql = """
        COALESCE((
            SELECT SUM(t.hours * cr.pay_rate) FROM timesheets t
            JOIN crew cr ON cr.id = t.crew_id
            JOIN projects p ON p.id = t.project_id
            WHERE p.bid_id = b.id AND cr.pay_type = 'hourly'), 0)
    """ if has_time else "0"
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT b.id, b.name, b.trade,
                   COALESCE(b.contract_value, b.bid_amount) AS revenue,
                   b.won_date,
                   COALESCE((SELECT SUM(actual) FROM bid_costs c
                             WHERE c.bid_id = b.id AND c.category <> 'labor'), 0)
                   + {labor_sql} AS cost
            FROM bid_opportunities b
            WHERE b.company = %s AND b.status = 'won'
            ORDER BY b.won_date DESC NULLS LAST
        """, (company,))
        won_rows = cur.fetchall()

    priced = []
    for r in won_rows:
        rev = float(r["revenue"] or 0)
        cost = float(r["cost"] or 0)
        # cost > 0 means there is real actual behind it. Otherwise we know
        # nothing about the margin and must not print one.
        priced.append({
            "id": r["id"], "name": r["name"], "trade": r["trade"],
            "revenue": rev, "cost": round(cost, 2),
            "margin": round(rev - cost, 2) if rev else None,
            "margin_pct": round((rev - cost) / rev * 100, 1) if rev and cost > 0 else None,
            "priced": bool(cost > 0),
        })

    total_rev = sum(p["revenue"] for p in priced)
    total_cost = sum(p["cost"] for p in priced if p["priced"])
    priced_only = [p for p in priced if p["priced"]]

    by_trade: dict[str, dict] = {}
    for p in priced_only:
        t = p["trade"] or "Unset"
        slot = by_trade.setdefault(t, {"count": 0, "revenue": 0.0, "cost": 0.0})
        slot["count"] += 1
        slot["revenue"] += p["revenue"]
        slot["cost"] += p["cost"]
    for slot in by_trade.values():
        slot["margin_pct"] = round((slot["revenue"] - slot["cost"]) / slot["revenue"] * 100, 1) \
            if slot["revenue"] else None

    with conn.cursor() as cur:
        cur.execute("""
            SELECT COALESCE(NULLIF(lost_reason, ''), 'not recorded') AS reason, count(*) AS n
            FROM bid_opportunities WHERE company = %s AND status = 'lost'
            GROUP BY 1 ORDER BY n DESC
        """, (company,))
        loss_reasons = [{"reason": r["reason"], "count": r["n"]} for r in cur.fetchall()]

    return {
        "by_status": by_status,
        "decided": decided,
        "won_count": won.get("count", 0),
        "won_value": won.get("amount", 0.0),
        "win_rate": round(won.get("count", 0) / decided * 100, 1) if decided else None,
        "won_jobs": priced,
        "priced_count": len(priced_only),
        "portfolio_revenue": total_rev,
        "portfolio_cost": round(total_cost, 2),
        "portfolio_margin": round(total_rev - total_cost, 2) if priced_only else None,
        "portfolio_margin_pct": round((total_rev - total_cost) / total_rev * 100, 1)
            if priced_only and total_rev else None,
        "by_trade": [{"trade": k, **v} for k, v in
                     sorted(by_trade.items(), key=lambda kv: -kv[1]["revenue"])],
        "loss_reasons": loss_reasons,
    }


# --- C. invoicing ------------------------------------------------------------
def next_invoice_number(conn, company: str = "construction") -> str:
    year = date.today().year
    with conn.cursor() as cur:
        cur.execute("SELECT invoice_number FROM bid_invoices WHERE company = %s "
                    "AND invoice_number LIKE %s", (company, f"INV-{year}-%"))
        nums = []
        for r in cur.fetchall():
            tail = (r["invoice_number"] or "").rsplit("-", 1)[-1]
            if tail.isdigit():
                nums.append(int(tail))
    return f"INV-{year}-{(max(nums) + 1) if nums else 1:04d}"


def create_invoice(conn, bid_id: int, amount, company: str = "construction",
                   description: str = "", issued_date: str = "", due_date: str = "",
                   invoice_number: str = "", status: str = "draft") -> dict:
    if status not in INVOICE_STATUSES:
        raise ValueError(f"status must be one of {INVOICE_STATUSES}")
    amt = _money(amount)
    if amt is None or amt <= 0:
        raise ValueError("amount must be a positive number")
    num = (invoice_number or "").strip() or next_invoice_number(conn, company)
    issued = _date_or_none(issued_date) or _today()
    due = _date_or_none(due_date) or (issued + timedelta(days=30))
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO bid_invoices (bid_id, invoice_number, issued_date, due_date,
                                     status, amount, description, company)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (bid_id, num, issued, due, status, amt,
              description.strip() or None, company))
        iid = cur.fetchone()["id"]
    conn.commit()
    return {"id": iid, "invoice_number": num}


def list_invoices(conn, bid_id: int, company: str = "construction") -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM bid_invoices WHERE bid_id = %s AND company = %s "
                    "ORDER BY issued_date DESC, id DESC", (bid_id, company))
        return cur.fetchall()


def set_invoice_status(conn, invoice_id: int, status: str, company: str = "construction",
                       collected=None) -> bool:
    """status and collected move together, because 'paid' implies money arrived."""
    if status not in INVOICE_STATUSES:
        raise ValueError(f"status must be one of {INVOICE_STATUSES}")
    with conn.cursor() as cur:
        cur.execute("SELECT amount, collected, bid_id FROM bid_invoices "
                    "WHERE id = %s AND company = %s", (invoice_id, company))
        inv = cur.fetchone()
        if not inv:
            return False
        amt = float(inv["amount"] or 0)
        coll = float(inv["collected"] or 0)
        if collected not in (None, ""):
            coll = max(0.0, min(amt, _money(collected)))
        if status == "paid":
            coll = amt
        elif status in ("draft", "void"):
            coll = 0.0
        derived = status
        if status not in ("void",) and coll > 0 and coll < amt:
            derived = "partial"
        cur.execute(
            "UPDATE bid_invoices SET status = %s, collected = %s, updated_at = now() "
            "WHERE id = %s AND company = %s", (derived, coll, invoice_id, company))
    conn.commit()
    return True


def delete_invoice(conn, invoice_id: int, company: str = "construction") -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM bid_invoices WHERE id = %s AND company = %s RETURNING id",
                    (invoice_id, company))
        ok = cur.fetchone() is not None
    conn.commit()
    return ok


def invoice_summary(conn, bid_id: int, company: str = "construction") -> dict:
    rows = list_invoices(conn, bid_id, company)
    live = [r for r in rows if r["status"] != "void"]
    billed = sum(float(r["amount"] or 0) for r in live)
    collected = sum(float(r["collected"] or 0) for r in live)
    outstanding = billed - collected
    # Days overdue, from the oldest unpaid invoice, not from today alone.
    today = date.today()
    overdue = [r for r in live if r["due_date"] and r["due_date"] < today
               and float(r["collected"] or 0) < float(r["amount"] or 0)]
    oldest = min((r["due_date"] for r in overdue), default=None)
    return {
        "invoices": rows,
        "count": len(live),
        "billed": round(billed, 2),
        "collected": round(collected, 2),
        "outstanding": round(outstanding, 2),
        "overdue_count": len(overdue),
        "days_overdue": (today - oldest).days if oldest else 0,
    }


# --- per-row board numbers ---------------------------------------------------
def board_numbers(conn, company: str = "construction") -> dict[int, dict]:
    """Cost snapshot for every bid, keyed by bid id, for the list view."""
    out: dict[int, dict] = {}
    if not (_table_exists(conn, "timesheets") and _table_exists(conn, "crew")
            and _table_exists(conn, "projects")):
        return out
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.bid_id AS bid_id,
                   COALESCE(SUM(t.hours * c.pay_rate) FILTER (WHERE c.pay_type = 'hourly'), 0) AS labor
            FROM timesheets t
            JOIN crew c ON c.id = t.crew_id
            JOIN projects p ON p.id = t.project_id
            WHERE p.company = %s AND p.bid_id IS NOT NULL
            GROUP BY p.bid_id
        """, (company,))
        labor = {r["bid_id"]: float(r["labor"] or 0) for r in cur.fetchall()}
        cur.execute("""
            SELECT bid_id,
                   COALESCE(SUM(actual), 0) AS actual,
                   COALESCE(SUM(estimated), 0) AS estimated
            FROM bid_costs WHERE company = %s GROUP BY bid_id
        """, (company,))
        for r in cur.fetchall():
            bid_id = r["bid_id"]
            slot = out.setdefault(bid_id, {"labor": 0.0, "other": 0.0, "estimated": 0.0})
            slot["labor"] = labor.get(bid_id, 0.0)
            entered = float(r["actual"] or 0)
            slot["other"] = entered
            slot["estimated"] = float(r["estimated"] or 0)
    for bid_id, slot in out.items():
        # entered labor lines are replaced by the derived figure, matching
        # cost_summary, so the list and the detail view never disagree.
        slot["actual"] = round(slot["labor"] + slot["other"], 2)
    return out